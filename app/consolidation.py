"""Consolidating a project: only the new words, and what overlapped.

Two steps over the crawls in a project, reading what they saved — nothing is
crawled again:

1. Pages (app/projects.py): the same page in more than one crawl counts once,
   from the first crawl in walk order that has it.
2. Sentences: every counted page is split into sentences, the way a crawl's
   own repetition estimate does (app/repetition.py), and walked crawl by crawl.
   A sentence's words count the first time it's seen anywhere in the project;
   after that it's repeated — within its crawl when that's where it was first
   seen, across crawls otherwise.

Where the sentences come from, per page:
- exact: the fingerprints the crawl kept of the text it counted
  (app/segment_store.py, every crawl from now on);
- markdown: rebuilt from the page's saved Markdown — main content only, so the
  page's counted words are split in the same proportions (an estimate);
- page_only: neither, so the whole page counts as new.

Everything adds up: added up − same page in more than one crawl − repeated
within a crawl − repeated across crawls = new words, for the project and for
every crawl in it.

Pure: the caller loads the runs and hands in how to read their sentences.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass, field

import numpy as np

from app import projects, report

# Goes up whenever what a consolidation produces changes, so stored results
# are redone.
ALGO_VERSION = 1

EXACT = "exact"
MARKDOWN = "markdown"
PAGE_ONLY = "page_only"

TOP_SENTENCES = 50
CSV_SENTENCES = 2_000
MOSTLY_REPEATED = 50
# Sentences compared at once; more are split by fingerprint into parts, each
# exact on its own, so memory stays bounded on big projects.
PARTITION_SIZE = 4_000_000


@dataclass(slots=True)
class Page:
    url: str
    title: str | None
    word_count: int
    success: bool
    blocked_by_host: bool = False
    error: str | None = None
    content_hash: str | None = None
    markdown_hash: str | None = None


@dataclass
class Run:
    id: str
    source_url: str
    shown_url: str
    created_at: str
    status: str
    total_words: int = 0
    page_count: int = 0
    markdown_pages: int = 0
    signed_in: bool = False
    password_protected: bool = False
    detected_cms: str | None = None
    duplicate_count: int = 0
    login_blocked_count: int = 0
    repeated_pct: int | None = None
    linked_hosts: dict = field(default_factory=dict)
    linked_languages: dict = field(default_factory=dict)
    other_languages: dict = field(default_factory=dict)
    pages: list[Page] = field(default_factory=list)


def slim(run) -> Run:
    """A RunRecord reduced to what consolidating needs — a fraction of the
    memory of its pydantic pages, which matters across 25 big crawls."""
    repetition = run.repetition or {}
    words_total = repetition.get("words_total") or 0
    return Run(
        id=run.id, source_url=run.source_url, shown_url=run.shown_url, created_at=run.created_at,
        status=run.status, total_words=run.total_words, page_count=run.page_count,
        markdown_pages=run.markdown_pages, signed_in=bool(run.signed_in),
        password_protected=bool(run.password_protected), detected_cms=run.detected_cms,
        duplicate_count=run.duplicate_count, login_blocked_count=run.login_blocked_count,
        repeated_pct=round(100 * repetition["words_repeated"] / words_total) if words_total else None,
        linked_hosts=dict(run.linked_hosts or {}), linked_languages=dict(run.linked_languages or {}),
        other_languages=dict(run.other_languages or {}),
        pages=[Page(p.url, p.title, p.word_count, p.success, p.blocked_by_host, p.error,
                    p.content_hash, p.markdown_hash) for p in run.pages],
    )


@dataclass
class Consolidated:
    result: dict
    page_rows: list[list]
    sentence_rows: list[list]


def _array(values, dtype) -> np.ndarray:
    if isinstance(values, (array, bytes, bytearray, memoryview)):
        return np.frombuffer(values, dtype=dtype).astype(dtype, copy=False)
    return np.asarray(values, dtype=dtype)


def apportion(words: int, source: str, total: float, within: float, across: float) -> tuple[int, int, int, str]:
    """A page's counted words split into repeated within, repeated across and
    new, from its sentences' words. Exact sentences are the counted text
    itself; Markdown ones are scaled to the counted words. The page's own count
    stays the truth either way."""
    if source == MARKDOWN and total <= 0:
        source = PAGE_ONLY
    if source == EXACT:
        w = min(int(within), words)
        a = min(int(across), words - w)
    elif source == MARKDOWN:
        w = min(int(round(words * within / total)), words)
        a = min(int(round(words * across / total)), words - w)
    else:
        w = a = 0
    return w, a, words - w - a, source


def consolidate(runs: list[Run], load_segments, load_texts, progress=None) -> Consolidated:
    """load_segments(run, urls) -> {url: (sentence fingerprints, words, EXACT|MARKDOWN)};
    load_texts(run, fingerprints) -> {fingerprint: text}; progress(step, done, total)."""
    progress = progress or (lambda *args: None)
    progress("pages", 0, 1)
    step = projects.summarize_project(runs)
    states, names = step["states"], step["names"]
    order = step["walk_order"]
    by_id = {run.id: run for run in runs}
    position = {run_id: i for i, run_id in enumerate(order)}
    R = max(len(order), 1)

    # The chosen copy of every page, crawl by crawl in walk order.
    chosen: dict[str, list] = {run_id: [] for run_id in order}
    for group in step["_groups"]:
        chosen[group.winner_run].append(group)

    entries: list[list] = []        # [group, run position, source]
    h_parts, w_parts, p_parts = [], [], []
    for i, run_id in enumerate(order):
        progress("sentences", i, len(order))
        groups = chosen[run_id]
        found = load_segments(by_id[run_id], [g.winner.url for g in groups]) if groups else {}
        for group in groups:
            record = found.get(group.winner.url)
            index = len(entries)
            if record is None:
                entries.append([group, i, PAGE_ONLY])
                continue
            hashes, words, source = record
            entries.append([group, i, source])
            if len(hashes):
                h_parts.append(_array(hashes, np.uint64))
                w_parts.append(_array(words, np.uint16).astype(np.int64))
                p_parts.append(np.full(len(hashes), index, dtype=np.int64))
    progress("sentences", len(order), len(order))

    n_pages = len(entries)
    h = np.concatenate(h_parts) if h_parts else np.zeros(0, dtype=np.uint64)
    w = np.concatenate(w_parts) if w_parts else np.zeros(0, dtype=np.int64)
    pg = np.concatenate(p_parts) if p_parts else np.zeros(0, dtype=np.int64)
    del h_parts, w_parts, p_parts
    run_of_page = np.array([e[1] for e in entries], dtype=np.int64)

    seg_total = np.bincount(pg, weights=w, minlength=n_pages) if n_pages else np.zeros(0)
    within_w = np.zeros(n_pages)
    across_w = np.zeros(n_pages)
    pair_sentence = np.zeros(R * R)
    from_keys, from_words = [], []
    candidates: list[tuple] = []     # (removed, times, words, fingerprint, runs, first page)
    across_sentences = 0

    parts = max(1, math.ceil(len(h) / PARTITION_SIZE))
    for part in range(parts):
        progress("compare", part, parts)
        if parts == 1:
            hh, ww, pp = h, w, pg
        else:
            pick = np.nonzero(h % np.uint64(parts) == np.uint64(part))[0]   # keeps walk order
            hh, ww, pp = h[pick], w[pick], pg[pick]
        if not len(hh):
            continue
        sort = np.argsort(hh, kind="stable")      # grouped by sentence, walk order inside
        hs = hh[sort]
        starts = np.ones(len(hs), dtype=bool)
        starts[1:] = hs[1:] != hs[:-1]
        grp = np.cumsum(starts) - 1
        occ_page = pp[sort]
        occ_words = ww[sort]
        occ_run = run_of_page[occ_page]
        first_run = occ_run[starts][grp]
        within = ~starts & (occ_run == first_run)
        across = ~starts & (occ_run != first_run)
        within_w += np.bincount(occ_page, weights=occ_words * within, minlength=n_pages)
        across_w += np.bincount(occ_page, weights=occ_words * across, minlength=n_pages)
        pair_sentence += np.bincount(occ_run * R + first_run, weights=occ_words * across, minlength=R * R)
        if across.any():
            from_keys.append(occ_page[across] * R + first_run[across])
            from_words.append(occ_words[across])

        times = np.bincount(grp)
        group_words = occ_words[starts]
        removed = group_words * (times - 1)
        run_keys = np.unique(grp * R + occ_run)
        runs_per = np.bincount(run_keys // R, minlength=len(times))
        across_sentences += int((runs_per >= 2).sum())
        repeated = np.nonzero(times > 1)[0]
        if len(repeated) > CSV_SENTENCES:
            repeated = repeated[np.argpartition(-removed[repeated], CSV_SENTENCES)[:CSV_SENTENCES]]
        first_page = occ_page[starts]
        first_hash = hs[starts]
        for g in repeated:
            lo = np.searchsorted(run_keys, g * R)
            hi = np.searchsorted(run_keys, (g + 1) * R)
            candidates.append((int(removed[g]), int(times[g]), int(group_words[g]), int(first_hash[g]),
                               [order[int(k % R)] for k in run_keys[lo:hi]], int(first_page[g])))
    progress("compare", parts, parts)
    candidates.sort(key=lambda c: (-c[0], -c[1], c[3]))
    candidates = candidates[:CSV_SENTENCES]

    # Where each page's sentences repeated from other crawls were first seen.
    repeated_from: dict[int, dict[str, int]] = {}
    if from_keys:
        keys = np.concatenate(from_keys)
        unique, inverse = np.unique(keys, return_inverse=True)
        sums = np.bincount(inverse, weights=np.concatenate(from_words))
        for key, total in zip(unique.tolist(), sums.tolist()):
            repeated_from.setdefault(key // R, {})[order[key % R]] = int(total)

    # Per page, then per crawl.
    progress("save", 0, 1)
    rows = {run.id: _row(run, states[run.id]) for run in runs}
    page_new: list[tuple] = []        # (entry, within, across, new, source)
    for k, (group, ri, source) in enumerate(entries):
        words = group.winner.word_count
        wi, ac, new, source = apportion(words, source, seg_total[k], within_w[k], across_w[k])
        entries[k][2] = source
        row = rows[order[ri]]
        row["pages"] += 1
        row["words"] += words
        row["repeated_within"] += wi
        row["repeated_across"] += ac
        row["new_words"] += new
        row["coverage"][source] += words
        page_new.append((wi, ac, new))
        for run_id, page in group.members:
            if run_id == group.winner_run:
                continue
            other = rows[run_id]
            other["pages"] += 1
            other["words"] += page.word_count
            other["same_page_pages"] += 1
            other["same_page_words"] += page.word_count
            tally = other["_same_in"].setdefault(group.winner_run, [0, 0])
            tally[0] += 1
            tally[1] += page.word_count

    counted_rows = [rows[run_id] for run_id in order]
    for row in rows.values():
        row["same_page_in"] = [{"id": run_id, "pages": n[0], "words": n[1]}
                               for run_id, n in sorted(row.pop("_same_in").items(), key=lambda kv: -kv[1][1])]
    totals = {
        "added_up": sum(r["words"] for r in counted_rows),
        "same_pages": sum(r["same_page_words"] for r in counted_rows),
        "repeated_within": sum(r["repeated_within"] for r in counted_rows),
        "repeated_across": sum(r["repeated_across"] for r in counted_rows),
        "new_words": sum(r["new_words"] for r in counted_rows),
        "pages": n_pages,
        "shared_pages": step["totals"]["shared_pages"],
        "runs_counted": len(order),
        "runs": len(runs),
        "estimated_words": sum(r["coverage"][MARKDOWN] for r in counted_rows),
        "page_only_words": sum(r["coverage"][PAGE_ONLY] for r in counted_rows),
        "duplicates": step["totals"]["duplicates"],
        "login_walls": step["totals"]["login_walls"],
    }
    totals["each_page_once"] = totals["added_up"] - totals["same_pages"]

    pairs = []
    for pair in step["pairs"]:
        a, b = position[pair["a"]], position[pair["b"]]
        pairs.append({**pair, "sentence_words": int(pair_sentence[a * R + b] + pair_sentence[b * R + a])})
    for a in range(len(order)):          # crawls sharing sentences but no page
        for b in range(a + 1, len(order)):
            words = int(pair_sentence[a * R + b] + pair_sentence[b * R + a])
            if words and not any({p["a"], p["b"]} == {order[a], order[b]} for p in pairs):
                pairs.append({"a": order[a], "b": order[b], "pages": 0, "words": 0, "same_text": 0,
                              "changed_text": 0, "address_only": 0, "sentence_words": words})
    pairs.sort(key=lambda p: -(p["words"] + p["sentence_words"]))

    mostly = []
    for k, (group, ri, source) in enumerate(entries):
        wi, ac, new = page_new[k]
        words = group.winner.word_count
        if words >= 20 and ac * 2 >= words:
            sources = repeated_from.get(k, {})
            mostly.append({"url": group.winner.url, "run": order[ri], "words": words, "across": ac,
                           "from": [run_id for run_id, _ in sorted(sources.items(), key=lambda kv: -kv[1])]})
    mostly.sort(key=lambda m: -m["across"])

    # The text of the sentences listed, from the crawl that saw each first.
    wanted: dict[str, set[int]] = {}
    for _, _, _, fingerprint, _, first in candidates:
        wanted.setdefault(order[entries[first][1]], set()).add(fingerprint)
    texts: dict[int, str] = {}
    for run_id, fingerprints in wanted.items():
        texts.update(load_texts(by_id[run_id], fingerprints))
    sentence_list = []
    for removed, times, words, fingerprint, crawl_ids, first in candidates:
        group = entries[first][0]
        sentence_list.append({
            "text": texts.get(fingerprint, ""), "text_known": fingerprint in texts,
            "words": words, "times": times, "removed": removed, "runs": crawl_ids,
            "first_url": group.winner.url, "first_run": order[entries[first][1]],
        })

    report_part = step["report"]
    new_by = {"folders": {}, "hosts": {}, "languages": {}}
    keyers = {"folders": report.folder_for_url, "hosts": report.host_for_url, "languages": report.language_for_url}
    for k, (group, _, _) in enumerate(entries):
        for name, keyer in keyers.items():
            key = keyer(group.winner.url)
            new_by[name][key] = new_by[name].get(key, 0) + page_new[k][2]
    for name in keyers:
        for item in report_part.get(name, []):
            item["new_words"] = new_by[name].get(item["name"], 0)

    result = {
        "version": ALGO_VERSION,
        "order": order,
        "names": names,
        "totals": totals,
        "runs": counted_rows + [rows[run.id] for run in sorted(runs, key=lambda r: r.created_at, reverse=True)
                                if run.id not in position],
        "pairs": pairs,
        "overlap_pages": {"count": step["totals"]["shared_pages"], "top": step["groups"],
                          "capped": step["groups_capped"], "mostly_repeated": mostly[:MOSTLY_REPEATED],
                          "mostly_repeated_count": len(mostly)},
        "sentences": {"count": across_sentences, "words": totals["repeated_across"],
                      "top": [s for s in sentence_list if len(s["runs"]) > 1][:TOP_SENTENCES]},
        "report": report_part,
        "notes": {
            "markdown_runs": [r["id"] for r in counted_rows if r["coverage"][MARKDOWN]],
            "page_only_runs": [r["id"] for r in counted_rows if r["coverage"][PAGE_ONLY]],
        },
    }

    page_rows = []
    for k, (group, ri, source) in enumerate(entries):
        wi, ac, new = page_new[k]
        also = [names[run_id] for run_id in dict.fromkeys(group.runs) if run_id != group.winner_run]
        page_rows.append([group.winner.url, group.winner.title or "", names[order[ri]], group.winner.word_count,
                          "", "; ".join(also), wi, ac, new, source])
        for run_id, page in group.members:
            if run_id != group.winner_run:
                page_rows.append([page.url, page.title or "", names[run_id], page.word_count,
                                  names[group.winner_run], "", 0, 0, 0, "same_page"])
    sentence_rows = [[s["text"] if s["text_known"] else "", s["words"], s["times"],
                      "; ".join(names[r] for r in s["runs"]), s["removed"], s["first_url"], names[s["first_run"]]]
                     for s in sentence_list]
    progress("save", 1, 1)
    return Consolidated(result, page_rows, sentence_rows)


def _row(run: Run, state: str) -> dict:
    return {
        "id": run.id,
        "state": state,
        "status": run.status,
        "created_at": run.created_at,
        "own_words": run.total_words,
        "own_pages": run.page_count,
        "repeated_pct": run.repeated_pct,
        "signed_in": run.signed_in,
        "password_protected": run.password_protected,
        "detected_cms": run.detected_cms,
        "pages": 0,
        "words": 0,
        "same_page_pages": 0,
        "same_page_words": 0,
        "_same_in": {},
        "repeated_within": 0,
        "repeated_across": 0,
        "new_words": 0,
        "coverage": {EXACT: 0, MARKDOWN: 0, PAGE_ONLY: 0},
    }
