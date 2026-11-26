"""Projects: one client's crawls together, each page and each sentence once.

A project groups runs — a site and its staging copy, the brand sites it links
to, a re-crawl after a fix, a signed-in crawl of the members' area — and
consolidates them (app/consolidation.py) into what a quote needs: only the new
words. This module is its first step, the pages. Two pages are the same page
when

- their text is the same, under any address on any site — by the fingerprint
  the duplicate guard already takes, or by one of the page's saved Markdown
  (rebuilt from the files on disk for older runs that saved it); or
- they sit at the same address on the same site — stg-v2.example.com/rooms and
  www.example.com/rooms — whatever their text, since a page can change
  between a staging crawl and a live one.

Across two different sites only the text matches: example.com/contact and
example.org/contact are different pages that happen to share a path.

Which crawls count: of a site crawled more than once, the latest completed
crawl; the earlier ones are replaced by it. A crawl that didn't finish
(cancelled, failed) counts too, but only fills gaps: it comes after every
completed crawl, so it adds the pages they don't have and never replaces their
copy of a page. Among completed crawls, the newest copy of a page counts.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit

from crawl4ai.utils import get_base_domain

from app import db, markdown_store, report
from app.models import RunRecord
from app.url_policy import _is_tracking_param

# Runs in one project, at most.
MAX_RUNS = 25
# Shared pages listed on the project page; the CSV has all of them.
MAX_GROUPS = 200

IN_PROGRESS = ("crawling", "queued", "starting", "resuming", "paused")
# Runs that stopped before the end: they fill gaps, after the completed ones.
UNFINISHED = ("cancelled", "failed")
_INDEX_PAGE = re.compile(r"/index\.(?:html?|php)$", re.IGNORECASE)
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_WORD = re.compile(r"\w+")


def markdown_fingerprint(text: str | None) -> str | None:
    """A page's saved Markdown reduced to its words — links and images out,
    formatting and spacing ignored — and hashed. None when there are none."""
    if not text:
        return None
    words = _WORD.findall(_MD_LINK.sub(r"\1", _MD_IMAGE.sub("", text)))
    if not words:
        return None
    return hashlib.blake2b(" ".join(words).encode("utf-8", "replace"), digest_size=16).hexdigest()


def _rebuild_markdown_hashes(run: RunRecord) -> bool:
    changed = False
    for page in run.pages:
        if page.success and page.markdown_hash is None:
            page.markdown_hash = markdown_fingerprint(markdown_store.read_text(run.id, page.url))
            changed = changed or page.markdown_hash is not None
    return changed


async def backfill_fingerprints(run: RunRecord) -> bool:
    """For a finished run from before Markdown fingerprints that saved its
    Markdown: rebuild them from the files, once, and keep them on the run.
    A run with only the counting fingerprint needs them too — that one can't
    be compared with an older run's Markdown fingerprint."""
    if run.status in IN_PROGRESS or not run.markdown_pages:
        return False
    if any(page.markdown_hash for page in run.pages):
        return False
    if not await asyncio.to_thread(_rebuild_markdown_hashes, run):
        return False
    return await db.update_run_pages(run.id, run.pages)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def url_key(url: str) -> str:
    """A page's address without its host: path (no trailing slash, no
    index.html) and its query, tracking parameters dropped and the rest sorted."""
    parts = urlsplit(url)
    path = _INDEX_PAGE.sub("/", parts.path or "/").rstrip("/") or "/"
    params = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking_param(k))
    return path + ("?" + urlencode(params) if params else "")


def site_of(url: str) -> str:
    """The registrable domain: www.example.com and stg-v2.example.com are one site."""
    host = (urlsplit(url).hostname or "").lower()
    try:
        return get_base_domain(url) or host
    except Exception:
        return host


@dataclass
class _Group:
    winner_run: str
    winner: object
    members: list[tuple[str, object]] = field(default_factory=list)

    @property
    def runs(self) -> list[str]:
        return [run_id for run_id, _ in self.members]

    @property
    def kind(self) -> str:
        pages = [page for _, page in self.members]
        for field_name in ("content_hash", "markdown_hash"):
            hashes = [getattr(page, field_name) for page in pages]
            if all(hashes) and len(set(hashes)) == 1:
                return "same_text"
        if all(p.content_hash for p in pages) or all(p.markdown_hash for p in pages):
            return "changed_text"
        return "address_only"


def _text_keys(page) -> list[tuple[str, str]]:
    keys = []
    if page.content_hash:
        keys.append(("text", page.content_hash))
    if page.markdown_hash:
        keys.append(("markdown", page.markdown_hash))
    return keys


def run_states(runs) -> dict[str, str]:
    """counted | gap_filler | superseded | in_progress, per run id.

    Per site (source_url) the latest completed crawl counts and older crawls of
    it — completed or not — are replaced by it. A crawl that didn't finish and
    isn't older than a completed one fills gaps. A cancelled re-crawl never
    replaces the completed crawl before it."""
    latest_completed: dict[str, tuple[str, str]] = {}
    for run in runs:
        if run.status == "completed":
            mark = (run.created_at, run.id)
            if mark > latest_completed.get(run.source_url, ("", "")):
                latest_completed[run.source_url] = mark
    states = {}
    for run in runs:
        newest = latest_completed.get(run.source_url)
        if run.status in IN_PROGRESS:
            states[run.id] = "in_progress"
        elif run.status == "completed":
            states[run.id] = "counted" if newest == (run.created_at, run.id) else "superseded"
        elif newest is not None and run.created_at < newest[0]:
            states[run.id] = "superseded"
        else:
            states[run.id] = "gap_filler"
    return states


def walk_order(runs, states: dict[str, str]) -> list:
    """The order crawls are read in — completed ones newest first, then the
    ones that didn't finish, newest first. The first copy of a page and the
    first time a sentence is seen are the ones counted, so each crawl adds what
    the ones before it don't have. Changing this only moves words between
    crawls, never the total."""
    newest_first = sorted(runs, key=lambda r: (r.created_at, r.id), reverse=True)
    return ([run for run in newest_first if states[run.id] == "counted"]
            + [run for run in newest_first if states[run.id] == "gap_filler"])


def summarize_project(runs) -> dict:
    """The pages step: which copy of every page counts, and which crawls share
    pages. Works on RunRecords or anything with the same attributes."""
    states = run_states(runs)
    order = walk_order(runs, states)
    names = {run.id: run.shown_url for run in runs}

    groups: list[_Group] = []
    by_hash: dict[tuple[str, str], _Group] = {}
    by_address: dict[tuple[str, str], _Group] = {}
    failed: dict[tuple[str, str], object] = {}

    for run in order:  # the first copy of a page is the one counted
        for page in run.pages:
            address = (site_of(page.url), url_key(page.url))
            if not page.success:
                failed.setdefault(address, page)
                continue
            keys = _text_keys(page)
            group = next((by_hash[key] for key in keys if key in by_hash), None)
            if group is None:
                group = by_address.get(address)
            if group is not None and run.id in group.runs:
                group = None  # two pages of one crawl are never the same page
            if group is None:
                group = _Group(run.id, page)
                groups.append(group)
            group.members.append((run.id, page))
            for key in keys:
                by_hash.setdefault(key, group)
            by_address.setdefault(address, group)

    added_up = sum(page.word_count for group in groups for _, page in group.members)
    total = sum(group.winner.word_count for group in groups)
    shared = [group for group in groups if len(group.members) > 1]

    pairs: dict[tuple[str, str], dict] = {}
    position = {run.id: i for i, run in enumerate(order)}
    for group in shared:
        members = list(dict.fromkeys(group.runs))
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                key = tuple(sorted((a, b), key=position.__getitem__))
                pair = pairs.setdefault(key, {"a": key[0], "b": key[1], "pages": 0, "words": 0,
                                               "same_text": 0, "changed_text": 0, "address_only": 0})
                pair["pages"] += 1
                pair["words"] += group.winner.word_count
                pair[group.kind] += 1

    unique_pages = [group.winner for group in groups] + list(failed.values())
    linked_hosts: dict[str, int] = {}
    linked_languages: dict[str, int] = {}
    other_languages: dict[str, dict] = {}
    for run in order:
        for host, n in (run.linked_hosts or {}).items():
            linked_hosts[host] = linked_hosts.get(host, 0) + n
        for language, n in (run.linked_languages or {}).items():
            linked_languages[language] = linked_languages.get(language, 0) + n
        for code, info in (run.other_languages or {}).items():
            merged = other_languages.setdefault(code, {"pages": 0, "skipped": 0, "example": info.get("example", "")})
            merged["pages"] += info.get("pages", 0)
            merged["skipped"] += info.get("skipped", 0)

    shared.sort(key=lambda g: -g.winner.word_count)
    return {
        "states": states,
        "walk_order": [run.id for run in order],
        "names": names,
        "totals": {
            "words": total,
            "pages": len(groups),
            "added_up": added_up,
            "overlap_words": added_up - total,
            "shared_pages": len(shared),
            "runs_counted": len(order),
            "runs": len(runs),
            "in_progress": sum(1 for state in states.values() if state == "in_progress"),
            "duplicates": sum(run.duplicate_count for run in order),
            "login_walls": sum(run.login_blocked_count for run in order),
        },
        "pairs": sorted(pairs.values(), key=lambda p: -p["words"]),
        "groups": [
            {
                "path": url_key(group.winner.url),
                "url": group.winner.url,
                "words": group.winner.word_count,
                "kind": group.kind,
                "counted_from": group.winner_run,
                "copies": [{"run": run_id, "url": page.url, "words": page.word_count}
                           for run_id, page in group.members if run_id != group.winner_run],
                "moved": len({url_key(page.url) for _, page in group.members}) > 1,
            }
            for group in shared[:MAX_GROUPS]
        ],
        "groups_capped": len(shared) > MAX_GROUPS,
        "report": {
            **report.summarize(unique_pages),
            **report.left_out(unique_pages, linked_hosts, linked_languages, other_languages),
        },
        "_groups": groups,
    }
