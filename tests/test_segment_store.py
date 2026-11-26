"""Sentence fingerprints kept per counted page (app/segment_store.py).

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import repetition, segment_store  # noqa: E402
from app.repetition import RepetitionTally, page_segments  # noqa: E402

HOME = "Welcome to the hotel. Book now and save."
ROOMS = "Sea view rooms. Book now and save."


def _write(run_id: str, pages: dict[str, str], flush_every: int = 1) -> RepetitionTally:
    writer = segment_store.Writer(run_id)
    tally = RepetitionTally()
    for i, (url, text) in enumerate(pages.items(), 1):
        segs = page_segments(text)
        tallied = tally.add_segments(segs, len(text.split()))
        writer.add_page(url, segs, tallied.first_seen)
        if i % flush_every == 0:
            writer.flush()
    writer.flush()
    return tally


def test_pages_come_back_as_they_were_written_across_several_flushes():
    _write("r", {"https://x.test/": HOME, "https://x.test/rooms": ROOMS})
    pages = segment_store.read("r")
    assert set(pages) == {segment_store.url_key("https://x.test/"), segment_store.url_key("https://x.test/rooms")}
    hashes, words = pages[segment_store.url_key("https://x.test/rooms")]
    assert list(hashes) == [key for _, key, _ in page_segments(ROOMS)]
    assert list(words) == [3, 4]


def test_only_the_pages_asked_for_are_read():
    _write("r", {"https://x.test/": HOME, "https://x.test/rooms": ROOMS})
    wanted = {segment_store.url_key("https://x.test/")}
    assert set(segment_store.read("r", wanted=wanted)) == wanted


def test_a_page_crawled_again_after_a_restart_counts_with_its_last_record():
    _write("r", {"https://x.test/": HOME})
    _write("r", {"https://x.test/": ROOMS})        # a new Writer appends to the same file
    hashes, _ = segment_store.read("r")[segment_store.url_key("https://x.test/")]
    assert list(hashes) == [key for _, key, _ in page_segments(ROOMS)]


def test_the_text_of_each_sentence_is_kept_once_from_where_it_was_first_seen():
    _write("r", {"https://x.test/": HOME, "https://x.test/rooms": ROOMS})
    book = repetition._exact_key("Book now and save.")
    sea = repetition._exact_key("Sea view rooms.")
    assert segment_store.read_texts("r", {book, sea}) == {book: "Book now and save.", sea: "Sea view rooms."}
    assert segment_store.read_texts("r", set()) == {}


def test_a_long_sentence_is_cut_on_a_character_boundary():
    text = "é" * 300 + "."
    _write("r", {"https://x.test/": text})
    kept = segment_store.read_texts("r", {repetition._exact_key(text)})
    snippet = next(iter(kept.values()))
    assert len(snippet.encode("utf-8")) <= segment_store.SNIPPET_BYTES and set(snippet) == {"é"}


def test_a_torn_tail_is_cut_off_and_appending_carries_on():
    _write("r", {"https://x.test/": HOME})
    path = segment_store.path_for("r")
    with path.open("ab") as handle:
        handle.write(b"\x10\x00\x00\x00garbage")        # a chunk cut short by a crash
    assert set(segment_store.read("r")) == {segment_store.url_key("https://x.test/")}, "the reader stops before it"
    _write("r", {"https://x.test/rooms": ROOMS})
    assert set(segment_store.read("r")) == {segment_store.url_key("https://x.test/"),
                                            segment_store.url_key("https://x.test/rooms")}


def test_a_corrupt_chunk_stops_the_reader_there():
    _write("r", {"https://x.test/": HOME, "https://x.test/rooms": ROOMS}, flush_every=1)
    data = bytearray(segment_store.path_for("r").read_bytes())
    data[-3] ^= 0xFF                                       # the second chunk's body
    segment_store.path_for("r").write_bytes(bytes(data))
    assert set(segment_store.read("r")) == {segment_store.url_key("https://x.test/")}


def test_files_from_other_segmentation_rules_are_ignored(monkeypatch):
    _write("r", {"https://x.test/": HOME})
    monkeypatch.setattr(segment_store, "SEGMENT_RULES", repetition.SEGMENT_RULES + 1)
    assert segment_store.read("r") is None and not segment_store.has("r")


def test_a_run_past_its_budget_stops_without_failing(monkeypatch):
    monkeypatch.setattr(segment_store, "MAX_RUN_BYTES", 40)
    writer = segment_store.Writer("r")
    writer.add_page("https://x.test/", page_segments(HOME * 20))
    writer.flush()
    assert writer.stopped
    writer.add_page("https://x.test/rooms", page_segments(ROOMS))
    writer.flush()                                        # ignored, never raises


def test_markdown_rebuilds_the_same_fingerprints_and_both_files_go_with_the_run():
    texts = {"https://x.test/": "# Welcome\n\nWelcome to the hotel. Book now and save.", "https://x.test/gone": None}
    assert segment_store.build_markdown("old", list(texts), lambda run_id, url: texts[url]) == 1
    pages = segment_store.read("old", segment_store.MARKDOWN)
    hashes, _ = pages[segment_store.url_key("https://x.test/")]
    assert repetition._exact_key("Book now and save.") in set(hashes), "the same sentence, the same fingerprint"
    assert segment_store.read("old") is None, "no crawl file for that run"

    _write("old", {"https://x.test/": HOME})
    assert segment_store.stored_run_ids() == {"old"}
    segment_store.delete("old")
    assert segment_store.stored_run_ids() == set()


def test_the_tally_is_the_same_whichever_way_it_is_fed():
    a, b = RepetitionTally(), RepetitionTally()
    for text in (HOME, ROOMS, HOME):
        assert a.add(text, len(text.split())) == b.add_segments(page_segments(text), len(text.split())).repeated
    assert a.summary() == b.summary()
