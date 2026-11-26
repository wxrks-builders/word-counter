"""Sentence fingerprints per counted page, kept after a crawl finishes.

A crawl's repetition tally (app/repetition.py) only knows which sentences it has
seen; it's thrown away at the end, so two crawls could never be compared
sentence by sentence. This keeps, for every counted page, the fingerprint and
word count of each of its sentences — about 10 bytes a sentence — plus the text
of each sentence the first time the crawl met it, cut to 200 bytes, so a
project can show which sentences it found in more than one crawl.

Two files per run, both beside the database:

- ``<run_id>.seg``: written by the crawl from the text it counted, appended at
  every checkpoint and kept once the run is over;
- ``<run_id>.mdseg``: rebuilt once from the run's saved Markdown, for runs from
  before this existed. Main content only, so a project calls its figures an
  estimate.

Layout: a 16-byte header (magic, format, source, the segmentation rules it was
written under), then chunks of ``length | crc32 | zlib(records)``. A record is
either a page (``P``: URL fingerprint, n, n sentence fingerprints, n word
counts) or a sentence's text (``T``). A page written twice — the pages after a
checkpoint are crawled again after a restart — counts with its last record. A
reader stops at the first chunk that doesn't check out, and the writer cuts a
torn tail off before appending to it again.
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
import sys
import threading
import zlib
from array import array
from pathlib import Path

from app.repetition import SEGMENT_RULES, page_segments
from app.word_count import clean_markdown_for_counting

logger = logging.getLogger(__name__)

SEGMENT_DIR = Path(os.environ.get("SEGMENT_DIR") or (Path(os.environ.get("DB_PATH", "data/wordcount.db")).parent / "segments"))
MAX_RUN_BYTES = int(os.environ.get("SEGMENT_MAX_RUN_MB", "200")) * 1024 * 1024
SNIPPET_BYTES = 200

COUNTED = 0
MARKDOWN = 1
_SUFFIX = {COUNTED: ".seg", MARKDOWN: ".mdseg"}
_MAGIC = b"WCSG"
_FORMAT = 1
_HEADER_SIZE = 16
_CHUNK_HEAD = struct.Struct("<II")
_PAGE_HEAD = struct.Struct("<QI")
_TEXT_HEAD = struct.Struct("<QB")
_LITTLE = sys.byteorder == "little"


def url_key(url: str) -> int:
    return int.from_bytes(hashlib.blake2b(url.encode("utf-8", "replace"), digest_size=8).digest(), "big")


def path_for(run_id: str, source: int = COUNTED) -> Path:
    return SEGMENT_DIR / f"{run_id}{_SUFFIX[source]}"


def _header(source: int) -> bytes:
    return _MAGIC + bytes([_FORMAT, source]) + SEGMENT_RULES.to_bytes(2, "little") + bytes(8)


def _header_ok(head: bytes, source: int) -> bool:
    return len(head) == _HEADER_SIZE and head == _header(source)


def _le(values: array) -> bytes:
    if not _LITTLE:
        values = array(values.typecode, values)
        values.byteswap()
    return values.tobytes()


def _from_le(typecode: str, data: bytes) -> array:
    values = array(typecode)
    values.frombytes(data)
    if not _LITTLE:
        values.byteswap()
    return values


def _snippet(text: str) -> bytes:
    data = text.encode("utf-8", "replace")
    if len(data) <= SNIPPET_BYTES:
        return data
    return data[:SNIPPET_BYTES].decode("utf-8", "ignore").encode("utf-8")


def _page_record(url: str, segs: list[tuple[str, int, int]], first_seen) -> bytes:
    hashes = array("Q", (key for _, key, _ in segs))
    words = array("H", (min(n, 0xFFFF) for _, _, n in segs))
    out = bytearray(b"P")
    out += _PAGE_HEAD.pack(url_key(url), len(segs))
    out += _le(hashes) + _le(words)
    for index in first_seen:
        text, key, _ = segs[index]
        snippet = _snippet(text)
        out += b"T" + _TEXT_HEAD.pack(key, len(snippet)) + snippet
    return bytes(out)


def _chunk(records: bytes) -> bytes:
    body = zlib.compress(records, 6)
    return _CHUNK_HEAD.pack(len(body), zlib.crc32(body)) + body


def _good_chunks(path: Path, source: int):
    """(records, end offset) for every chunk up to the first bad one. Nothing
    when the file is missing or was written under other rules."""
    try:
        with path.open("rb") as handle:
            if not _header_ok(handle.read(_HEADER_SIZE), source):
                return
            offset = _HEADER_SIZE
            while True:
                head = handle.read(_CHUNK_HEAD.size)
                if len(head) < _CHUNK_HEAD.size:
                    return
                size, crc = _CHUNK_HEAD.unpack(head)
                body = handle.read(size)
                if len(body) < size or zlib.crc32(body) != crc:
                    return
                try:
                    records = zlib.decompress(body)
                except zlib.error:
                    return
                offset += _CHUNK_HEAD.size + size
                yield records, offset
    except FileNotFoundError:
        return


def _records(data: bytes):
    """("P", url fingerprint, hashes, words) and ("T", hash, text) records."""
    pos, end = 0, len(data)
    while pos < end:
        kind = data[pos:pos + 1]
        pos += 1
        if kind == b"P":
            key, n = _PAGE_HEAD.unpack_from(data, pos)
            pos += _PAGE_HEAD.size
            hashes = _from_le("Q", data[pos:pos + 8 * n])
            pos += 8 * n
            words = _from_le("H", data[pos:pos + 2 * n])
            pos += 2 * n
            yield "P", key, hashes, words
        elif kind == b"T":
            key, size = _TEXT_HEAD.unpack_from(data, pos)
            pos += _TEXT_HEAD.size
            yield "T", key, data[pos:pos + size].decode("utf-8", "replace")
            pos += size
        else:  # a record this version doesn't know: nothing after it can be read
            return


class Writer:
    """Collects a crawl's pages between checkpoints and appends them in one
    chunk. Never fatal: a write that fails, or a run past MAX_RUN_BYTES, stops
    the fingerprints for the rest of the run and the crawl carries on."""

    def __init__(self, run_id: str, source: int = COUNTED) -> None:
        self.run_id = run_id
        self.source = source
        self.stopped = False
        self._pending = bytearray()
        self._lock = threading.Lock()
        self._ready = False
        self._size = 0

    def add_page(self, url: str, segs: list[tuple[str, int, int]], first_seen=()) -> None:
        if self.stopped:
            return
        record = _page_record(url, segs, first_seen)
        with self._lock:
            self._pending += record

    def _prepare(self, path: Path) -> None:
        """Before the first append: keep the good part of what's there, or
        start the file over when it's missing, foreign or from older rules."""
        end = None
        for _, offset in _good_chunks(path, self.source):
            end = offset
        if end is None:
            try:
                with path.open("rb") as handle:
                    end = _HEADER_SIZE if _header_ok(handle.read(_HEADER_SIZE), self.source) else None
            except FileNotFoundError:
                end = None
        if end is None:
            path.write_bytes(_header(self.source))
            end = _HEADER_SIZE
        elif path.stat().st_size != end:
            with path.open("r+b") as handle:
                handle.truncate(end)
        self._size = end
        self._ready = True

    def flush(self) -> None:
        with self._lock:
            records, self._pending = bytes(self._pending), bytearray()
        if not records or self.stopped:
            return
        try:
            path = path_for(self.run_id, self.source)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not self._ready:
                self._prepare(path)
            chunk = _chunk(records)
            if self._size + len(chunk) > MAX_RUN_BYTES:
                self.stopped = True
                logger.info("Sentence fingerprints for %s stopped at %d bytes", self.run_id, self._size)
                return
            with path.open("ab") as handle:
                handle.write(chunk)
            self._size += len(chunk)
        except OSError:
            logger.exception("Sentence fingerprints for %s failed — the crawl carries on without them", self.run_id)
            self.stopped = True


def has(run_id: str, source: int = COUNTED) -> bool:
    try:
        with path_for(run_id, source).open("rb") as handle:
            return _header_ok(handle.read(_HEADER_SIZE), source)
    except FileNotFoundError:
        return False


def read(run_id: str, source: int = COUNTED, wanted: set[int] | None = None) -> dict[int, tuple[array, array]] | None:
    """{URL fingerprint: (sentence fingerprints, words)} for the pages wanted
    (all when None). None when there's no usable file."""
    if not has(run_id, source):
        return None
    pages: dict[int, tuple[array, array]] = {}
    for records, _ in _good_chunks(path_for(run_id, source), source):
        for record in _records(records):
            if record[0] == "P" and (wanted is None or record[1] in wanted):
                pages[record[1]] = (record[2], record[3])
    return pages


def read_texts(run_id: str, wanted: set[int], source: int = COUNTED) -> dict[int, str]:
    """The text kept for the sentence fingerprints wanted."""
    texts: dict[int, str] = {}
    if not wanted:
        return texts
    for records, _ in _good_chunks(path_for(run_id, source), source):
        for record in _records(records):
            if record[0] == "T" and record[1] in wanted and record[1] not in texts:
                texts[record[1]] = record[2]
    return texts


def build_markdown(run_id: str, urls, read_text) -> int:
    """Writes <run_id>.mdseg from the run's saved Markdown — read_text(run_id,
    url) — cleaned the way the count cleans its text. Returns the pages
    written; 0 writes nothing."""
    seen: set[int] = set()
    records = bytearray()
    pages = 0
    for url in urls:
        text = read_text(run_id, url)
        if text is None:
            continue
        segs = page_segments(clean_markdown_for_counting(text))
        first = []
        for index, (_, key, _) in enumerate(segs):
            if key not in seen:
                seen.add(key)
                first.append(index)
        records += _page_record(url, segs, first)
        pages += 1
    if not pages:
        return 0
    path = path_for(run_id, MARKDOWN)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp(path)
    # Written whole, in one chunk: it's rebuilt from scratch if anything goes
    # wrong, so there's never a tail to save.
    tmp.write_bytes(_header(MARKDOWN) + _chunk(bytes(records)))
    os.replace(tmp, path)
    return pages


def _tmp(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def delete(run_id: str) -> None:
    for source in (COUNTED, MARKDOWN):
        path = path_for(run_id, source)
        for candidate in (path, _tmp(path)):
            candidate.unlink(missing_ok=True)


def stored_run_ids() -> set[str]:
    if not SEGMENT_DIR.is_dir():
        return set()
    return {path.name.split(".", 1)[0] for path in SEGMENT_DIR.iterdir()
            if path.name.endswith((".seg", ".mdseg", ".tmp"))}
