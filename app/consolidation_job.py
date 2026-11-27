"""Runs a project's consolidation in the background (app/consolidation.py).

Nothing here crawls: it reads the crawls' pages, sentence fingerprints and
saved Markdown, so it costs this server a few seconds and the client's sites
nothing. A project is consolidated

- when a crawl is added to it or taken out, and when one of its crawls finishes
  (refresh: only if something it depends on changed);
- when its owner presses Consolidate (request).

One row per project in project_consolidations holds the state and the last
result; pages read the result, they never compute it. A job holds a lease it
renews while it works, so one left by a process that stopped — every deploy
stops one — is picked up again by the next. A request made while a job runs
queues the project again for when it finishes; a failure keeps the previous
result.
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import json
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import consolidation, db, markdown_store, projects, segment_store
from app.i18n import N_
from app.repetition import SEGMENT_RULES

logger = logging.getLogger(__name__)

OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
LEASE_SECONDS = 120
BEAT_SECONDS = 5
MAX_RESUMES = 3
CONCURRENCY = max(1, int(os.environ.get("CONSOLIDATION_CONCURRENCY", "1")))
# Pages across a project's crawls past which this server won't try.
MAX_PAGES = int(os.environ.get("CONSOLIDATION_MAX_PAGES", "400000"))
CONSOLIDATION_DIR = Path(os.environ.get("CONSOLIDATION_DIR")
                         or (Path(os.environ.get("DB_PATH", "data/wordcount.db")).parent / "consolidations"))
# Tests run the job in the request that asks for it.
INLINE = os.environ.get("CONSOLIDATION_INLINE") == "1"

PAGES_HEADER = ["URL", "Title", "Crawl", "Words", "Same page as", "Also in", "Repeated in this crawl",
                "Repeated from other crawls", "New words", "Checked by"]
SENTENCES_HEADER = ["Sentence", "Words", "Times in the project", "Crawls", "Words removed", "First seen on",
                    "First seen in"]
_CHECKED_BY = {
    consolidation.EXACT: "sentences",
    consolidation.MARKDOWN: "estimated from saved Markdown",
    consolidation.PAGE_ONLY: "page only",
    "same_page": "same page, counted from another crawl",
}
# How far along each step takes the bar.
_STEPS = {"loading": (0, 10), "pages": (10, 15), "sentences": (15, 70), "compare": (70, 95), "save": (95, 100)}

_tasks: dict[str, asyncio.Task] = {}
_kicking = False
_stopping = False


class LostLease(Exception):
    """Another process took the job over; this one stops without writing."""


class TooLarge(Exception):
    pass


def fingerprint(summaries: list[dict]) -> str:
    """What a consolidation depends on: the finished crawls in it and the
    rules it applies. A crawl still running doesn't change it until it ends."""
    parts = sorted(
        (s["id"], s["status"], s["total_words"], s["page_count"], s["markdown_pages"])
        for s in summaries if s["status"] not in projects.IN_PROGRESS
    )
    return hashlib.sha1(json.dumps([consolidation.ALGO_VERSION, SEGMENT_RULES, parts]).encode()).hexdigest()


async def refresh(project_id: str, reason: str = "changed") -> None:
    """Consolidates the project if anything it depends on changed since the
    last time it was asked."""
    summaries = await db.list_project_run_summaries(project_id)
    wanted = fingerprint(summaries)
    row = await db.get_consolidation(project_id)
    if row is None or row["wanted_inputs"] != wanted:
        await request(project_id, reason, wanted)
    elif row["status"] in ("queued", "running"):
        await _start(project_id)


async def request(project_id: str, reason: str = "asked", wanted: str | None = None) -> int:
    """Consolidates the project (again), whatever changed."""
    if wanted is None:
        wanted = fingerprint(await db.list_project_run_summaries(project_id))
    gen = await db.request_consolidation(project_id, reason, wanted)
    await _start(project_id)
    return gen


async def refresh_for_run(run_id: str) -> None:
    for project_id in await db.project_ids_for_run(run_id):
        try:
            await refresh(project_id, "crawl finished")
        except Exception:
            logger.exception("Could not queue the consolidation of project %s", project_id)


async def _start(project_id: str) -> None:
    if INLINE:
        claimed = await db.claim_consolidation(OWNER, _now(), _stale_before(), project_id)
        if claimed:
            await _run_guarded(claimed[0], claimed[1], claimed[2])
        return
    kick()


def kick() -> None:
    """Starts queued consolidations, up to CONCURRENCY at once."""
    global _kicking
    if _kicking or _stopping or len(_tasks) >= CONCURRENCY:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _kicking = True
    loop.create_task(_claim_loop())


async def _claim_loop() -> None:
    global _kicking
    try:
        while len(_tasks) < CONCURRENCY and not _stopping:
            claimed = await db.claim_consolidation(OWNER, _now(), _stale_before())
            if claimed is None:
                break
            project_id, gen, resumed = claimed
            _tasks[project_id] = asyncio.create_task(_run_guarded(project_id, gen, resumed))
    except Exception:
        logger.exception("Could not start a consolidation")
    finally:
        _kicking = False


async def _run_guarded(project_id: str, gen: int, resumed: int = 0) -> None:
    try:
        if resumed > MAX_RESUMES:
            # Stored in English and translated where it's shown (report.stored_text).
            await db.fail_consolidation(project_id, OWNER, gen,
                                        N_("Stopped: the server restarted while it ran, several times in a row."))
            return
        await run(project_id, gen)
    except asyncio.CancelledError:
        await asyncio.shield(db.release_consolidation(project_id, OWNER))
        raise
    except LostLease:
        logger.info("Consolidation of project %s was taken over by another process", project_id)
    except TooLarge as exc:
        await db.fail_consolidation(project_id, OWNER, gen, str(exc))
    except Exception as exc:
        logger.exception("Consolidation of project %s failed", project_id)
        await db.fail_consolidation(project_id, OWNER, gen, f"{type(exc).__name__}: {exc}"[:500])
    finally:
        _tasks.pop(project_id, None)
        if not INLINE:
            kick()


class _Progress:
    """Written by the worker thread, beaten into the database by the loop."""

    def __init__(self) -> None:
        self.state = {"step": "loading", "done": 0, "total": 1, "percent": 0}

    def __call__(self, step: str, done: int, total: int) -> None:
        low, high = _STEPS.get(step, (0, 100))
        share = (done / total) if total else 1
        self.state = {"step": step, "done": done, "total": total, "percent": round(low + (high - low) * share)}


async def _beat(project_id: str, progress: _Progress) -> None:
    if await db.heartbeat_consolidation(project_id, OWNER, progress.state) is None:
        raise LostLease()


async def _beat_while(project_id: str, progress: _Progress, work):
    """Runs work in a thread and keeps the lease meanwhile."""
    task = asyncio.ensure_future(asyncio.to_thread(work))
    lost = False
    while not task.done():
        await asyncio.wait({task}, timeout=BEAT_SECONDS)
        if not task.done() and await db.heartbeat_consolidation(project_id, OWNER, progress.state) is None:
            lost = True
    result = await task
    if lost:
        raise LostLease()
    return result


async def run(project_id: str, gen: int) -> None:
    started = time.monotonic()
    progress = _Progress()
    if await db.get_project(project_id) is None:
        return
    summaries = await db.list_project_run_summaries(project_id)
    inputs = fingerprint(summaries)
    settled = [s for s in summaries if s["status"] not in projects.IN_PROGRESS]
    pages = sum(s["page_count"] or 0 for s in settled)
    if pages > MAX_PAGES:
        raise TooLarge(f"Too large to consolidate here: {pages:,} pages across its crawls"
                       f" (this server's limit is {MAX_PAGES:,}).")

    runs: list[consolidation.Run] = []
    for i, summary in enumerate(settled):
        progress("loading", i, len(settled))
        await _beat(project_id, progress)
        await _wait_for_memory()
        record = await db.get_run(summary["id"])
        if record is None:
            continue
        await projects.backfill_fingerprints(record)
        runs.append(await asyncio.to_thread(consolidation.slim, record))
        del record
    # Shown on the page, not compared: still crawling.
    live = [consolidation.Run(id=s["id"], source_url=s["source_url"], shown_url=s["shown_url"],
                              created_at=s["created_at"], status=s["status"], total_words=s["total_words"],
                              page_count=s["page_count"], repeated_pct=s["repeated_pct"])
            for s in summaries if s["status"] in projects.IN_PROGRESS]

    await _wait_for_memory()
    done = await _beat_while(project_id, progress, lambda: consolidation.consolidate(
        runs + live, _load_segments, _load_texts, progress))
    await _beat_while(project_id, progress, lambda: _write_csvs(project_id, done))

    result = done.result
    result.update({"computed_at": _now(), "duration_ms": round(1000 * (time.monotonic() - started)),
                   "generation": gen, "inputs": inputs})
    if not await db.finish_consolidation(project_id, OWNER, gen, result, inputs):
        raise LostLease()
    totals = result["totals"]
    await db.update_project_cache(project_id, totals["each_page_once"], totals["pages"], totals["same_pages"],
                                  totals["runs_counted"], new_words=totals["new_words"],
                                  repeated_words=totals["repeated_within"] + totals["repeated_across"])


def _load_segments(run: consolidation.Run, urls: list[str]) -> dict:
    """Sentences for these pages: the crawl's own, else rebuilt from its saved
    Markdown (built once, the first time it's needed), else none."""
    keys = {segment_store.url_key(url): url for url in urls}
    found: dict[str, tuple] = {}
    exact = segment_store.read(run.id, segment_store.COUNTED, set(keys))
    for key, (hashes, words) in (exact or {}).items():
        found[keys[key]] = (hashes, words, consolidation.EXACT)
    missing = {segment_store.url_key(url) for url in urls if url not in found}
    if missing and run.markdown_pages:
        markdown = segment_store.read(run.id, segment_store.MARKDOWN, missing)
        if markdown is None:
            segment_store.build_markdown(run.id, [p.url for p in run.pages if p.success], markdown_store.read_text)
            markdown = segment_store.read(run.id, segment_store.MARKDOWN, missing) or {}
        for key, (hashes, words) in markdown.items():
            found[keys[key]] = (hashes, words, consolidation.MARKDOWN)
    return found


def _load_texts(run: consolidation.Run, fingerprints: set[int]) -> dict[int, str]:
    texts = segment_store.read_texts(run.id, fingerprints, segment_store.COUNTED)
    missing = fingerprints - set(texts)
    if missing:
        texts.update(segment_store.read_texts(run.id, missing, segment_store.MARKDOWN))
    return texts


def csv_path(project_id: str, kind: str) -> Path:
    return CONSOLIDATION_DIR / f"{project_id}-{kind}.csv.gz"


def _write_csv(path: Path, header: list[str], rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    os.replace(tmp, path)


def _write_csvs(project_id: str, done: consolidation.Consolidated) -> None:
    _write_csv(csv_path(project_id, "pages"), PAGES_HEADER,
               (row[:9] + [_CHECKED_BY.get(row[9], row[9])] for row in done.page_rows))
    _write_csv(csv_path(project_id, "sentences"), SENTENCES_HEADER, done.sentence_rows)


def delete_files(project_id: str) -> None:
    for kind in ("pages", "sentences"):
        path = csv_path(project_id, kind)
        for candidate in (path, path.with_name(path.name + ".tmp")):
            candidate.unlink(missing_ok=True)


def stored_project_ids() -> set[str]:
    if not CONSOLIDATION_DIR.is_dir():
        return set()
    return {path.name.rsplit("-", 1)[0] for path in CONSOLIDATION_DIR.glob("*.csv.gz*")}


async def status(project_id: str) -> dict:
    """What the project page polls while a consolidation runs."""
    row = await db.get_consolidation(project_id)
    if row is None:
        return {"status": "none", "percent": 0, "step": None, "computed_gen": None, "requested_gen": 0}
    progress = row["progress"] or {}
    if row["status"] in ("queued", "running"):
        kick()
    return {
        "status": row["status"],
        "step": progress.get("step"),
        "percent": progress.get("percent", 0),
        "computed_gen": row["computed_gen"],
        "requested_gen": row["requested_gen"],
        "finished_at": row["finished_at"],
        "error": row["error"],
    }


async def resume_after_restart() -> None:
    """Startup: carries on with what's queued; a job left running by a
    process that stopped is taken over once its lease runs out."""
    kick()


async def shutdown() -> None:
    global _stopping
    _stopping = True
    for task in list(_tasks.values()):
        task.cancel()
    for task in list(_tasks.values()):
        try:
            await task
        except BaseException:
            pass


async def _wait_for_memory() -> None:
    """Never the thing that pushes a customer's crawl over the memory limit."""
    from app.crawler import _MEMORY_LIMIT_BYTES, _process

    for _ in range(60):
        if _process.memory_info().rss < 0.85 * _MEMORY_LIMIT_BYTES:
            return
        await asyncio.sleep(10)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stale_before() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS)).isoformat()
