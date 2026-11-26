"""A repetition estimate, the way a CAT tool's analysis works at its simplest.

Agencies quote from a CAT/TMS analysis: the text is split into segments
(roughly sentences) and each is new, a repetition of one seen earlier in the
job (billed at a fraction of the rate), or a fuzzy match. Menus, footers and
whole duplicate pages are already kept out of the count; what's left is
repetition inside the main content — CTAs, disclaimers, author boxes, room
descriptions reused across a hotel's pages, spec boilerplate.

This splits each counted page into segments and tallies them across the crawl:
the first occurrence is new, every later one is a repetition, and a segment
that matches an earlier one once digits, case and punctuation are set aside
("Room 101, 2 guests" / "Room 204, 3 guests") is a near repetition. No fuzzy
bands and no translation memory — it's an estimate, and the report says so.

Only 8-byte hashes are kept per segment, plus the text of segments once they
repeat, for the "most repeated" list. Those are written next to the database at
every checkpoint (see save/load below), so a crawl picked back up after a
restart — every deploy is one — carries on with the tally it had.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from array import array
from dataclasses import dataclass
from pathlib import Path

from app.word_count import count_words

TALLY_DIR = Path(os.environ.get("REPETITION_DIR") or (Path(os.environ.get("DB_PATH", "data/wordcount.db")).parent / "repetition"))
_FORMAT = 1
# The version of segments() and _exact_key(). Sentence fingerprints kept per
# page (app/segment_store.py) are only comparable under the same rules, so this
# goes up whenever either changes.
SEGMENT_RULES = 1

# A sentence ends at . ! ? followed by space and something that can start one;
# CJK full stops need no space after them.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"“‘(\[¿¡0-9A-ZÀ-ÖØ-ÞĀ-Ž])|(?<=[。！？])")
# Tokens that end in a full stop without ending a sentence.
_ABBREVIATIONS = {
    "e.g.", "i.e.", "etc.", "vs.", "mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "no.",
    "inc.", "ltd.", "co.", "corp.", "approx.", "u.s.", "u.k.", "p.m.", "a.m.", "jan.", "feb.",
    "mar.", "apr.", "jun.", "jul.", "aug.", "sep.", "sept.", "oct.", "nov.", "dec.",
}
_TABLE_RULE = re.compile(r"^\|?\s*:?-{2,}")
_LEADING_MARKER = re.compile(r"^(?:#{1,6}|[-*+>]|\d+[.)])\s+")
_DIGITS = re.compile(r"\d+")
_NOT_WORD = re.compile(r"[^\w#\s]+")

# Past this many distinct segments the tally stops learning new ones (and says
# so) — around 20M words of unique text, far past any site crawled so far.
MAX_SEGMENTS = 1_500_000
# The "most repeated" list: how many are tracked, and how many are reported.
MAX_TRACKED = 5_000
TOP = 20
SNIPPET = 160


def _sentences(line: str) -> list[str]:
    pieces = _SENTENCE_END.split(line)
    merged: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if merged and merged[-1].split()[-1].lower() in _ABBREVIATIONS:
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    return merged


def segments(text: str | None) -> list[str]:
    """Paragraphs and list items are segments; so is each table cell; within
    them, each sentence."""
    out: list[str] = []
    for line in (text or "").splitlines():
        line = _LEADING_MARKER.sub("", line.strip())
        if not line or _TABLE_RULE.match(line):
            continue
        cells = [c for c in line.strip("|").split("|")] if line.count("|") >= 2 else [line]
        for cell in cells:
            for sentence in _sentences(cell):
                if any(ch.isalnum() for ch in sentence):
                    out.append(sentence)
    return out


def _hash(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=8).digest(), "big")


def _exact_key(segment: str) -> int:
    return _hash(" ".join(segment.split()))


def _near_key(segment: str) -> int:
    normal = _NOT_WORD.sub(" ", _DIGITS.sub("#", segment.lower()))
    return _hash(" ".join(normal.split()))


def page_segments(text: str | None) -> list[tuple[str, int, int]]:
    """A page's segments as (text, exact fingerprint, words) — one pass shared
    by the tally and the fingerprints a project compares across crawls."""
    return [(segment, _exact_key(segment), count_words(segment)) for segment in segments(text)]


@dataclass
class Tallied:
    repeated: int
    # Indexes of the page's segments the tally hadn't seen before.
    first_seen: list[int]


class RepetitionTally:
    def __init__(self) -> None:
        self._seen: set[int] = set()
        self._near_seen: set[int] = set()
        self._repeats: dict[int, list] = {}   # key -> [snippet, times seen, words added]
        self.words_total = 0
        self.words_repeated = 0
        self.words_near = 0
        self.segments_total = 0
        self.capped = False

    def add(self, text: str | None, page_words: int) -> int:
        """Tallies one counted page and returns its repeated words (exact
        repetitions only — the figure the CSV carries). Never more than the
        page's own count, which stays the truth."""
        return self.add_segments(page_segments(text), page_words).repeated

    def add_segments(self, segs: list[tuple[str, int, int]], page_words: int) -> Tallied:
        """add() for segments already split by page_segments(), with which of
        them were new to the tally."""
        repeated = near = 0
        first_seen: list[int] = []
        for index, (segment, key, words) in enumerate(segs):
            self.segments_total += 1
            if key in self._seen:
                repeated += words
                entry = self._repeats.get(key)
                if entry is not None:
                    entry[1] += 1
                    entry[2] += words
                elif len(self._repeats) < MAX_TRACKED:
                    self._repeats[key] = [segment[:SNIPPET], 2, words]
                continue
            near_key = _near_key(segment)
            if near_key in self._near_seen:
                near += words
            if len(self._seen) < MAX_SEGMENTS:
                self._seen.add(key)
                self._near_seen.add(near_key)
                first_seen.append(index)
            else:
                self.capped = True
        repeated = min(repeated, page_words)
        near = min(near, page_words - repeated)
        self.words_total += page_words
        self.words_repeated += repeated
        self.words_near += near
        return Tallied(repeated, first_seen)

    def summary(self, partial: bool = False) -> dict:
        top = sorted(self._repeats.values(), key=lambda e: (-e[2], -e[1]))[:TOP]
        return {
            "words_total": self.words_total,
            "words_repeated": self.words_repeated,
            "words_near": self.words_near,
            "segments_total": self.segments_total,
            "segments_unique": len(self._seen),
            "top": [{"text": text, "count": count, "words": words} for text, count, words in top],
            # The tally lives in memory: a server restart mid-crawl starts a
            # fresh one, so it only covers the pages read after it.
            "partial": partial,
            "capped": self.capped,
        }

    def dump(self, partial: bool = False) -> bytes:
        """The whole tally as bytes: a JSON header, then both hash sets as
        packed 64-bit integers — about 16 bytes per distinct segment."""
        header = json.dumps({
            "format": _FORMAT,
            "byteorder": sys.byteorder,
            "words_total": self.words_total,
            "words_repeated": self.words_repeated,
            "words_near": self.words_near,
            "segments_total": self.segments_total,
            "capped": self.capped,
            "partial": partial,
            "seen": len(self._seen),
            "near_seen": len(self._near_seen),
            "repeats": [[key, *entry] for key, entry in self._repeats.items()],
        }).encode()
        return (
            len(header).to_bytes(4, "big") + header
            + array("Q", self._seen).tobytes() + array("Q", self._near_seen).tobytes()
        )

    @classmethod
    def load(cls, data: bytes) -> tuple["RepetitionTally", bool]:
        """The tally dump() wrote, and whether it was already partial."""
        size = int.from_bytes(data[:4], "big")
        header = json.loads(data[4:4 + size])
        if header.get("format") != _FORMAT:
            raise ValueError(f"unknown tally format {header.get('format')!r}")
        hashes = array("Q")
        hashes.frombytes(data[4 + size:])
        if header["byteorder"] != sys.byteorder:
            hashes.byteswap()
        if len(hashes) != header["seen"] + header["near_seen"]:
            raise ValueError("truncated tally")
        tally = cls()
        tally._seen = set(hashes[:header["seen"]])
        tally._near_seen = set(hashes[header["seen"]:])
        tally._repeats = {key: [text, count, words] for key, text, count, words in header["repeats"]}
        tally.words_total = header["words_total"]
        tally.words_repeated = header["words_repeated"]
        tally.words_near = header["words_near"]
        tally.segments_total = header["segments_total"]
        tally.capped = header["capped"]
        return tally, bool(header["partial"])


# ---------- Kept across restarts ----------
# One file per run in progress (crawling or paused), replaced whole at every
# checkpoint and removed once the run is over — its summary is in the database
# by then. A file that doesn't match the run's checkpoint is ignored (see
# app/job_store.restore_job), so a crash between the two only costs coverage.

def _path(run_id: str) -> Path:
    return TALLY_DIR / f"{run_id}.tally"


def save(run_id: str, data: bytes) -> None:
    TALLY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(run_id).with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, _path(run_id))


def load(run_id: str) -> tuple[RepetitionTally, bool] | None:
    try:
        return RepetitionTally.load(_path(run_id).read_bytes())
    except FileNotFoundError:
        return None


def delete(run_id: str) -> None:
    for path in (_path(run_id), _path(run_id).with_suffix(".tmp")):
        path.unlink(missing_ok=True)


def stored_run_ids() -> list[str]:
    if not TALLY_DIR.is_dir():
        return []
    return [path.stem for path in TALLY_DIR.glob("*.tally")]
