"""The repetition estimate: segments, the tally, a real crawl and the report.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.repetition import RepetitionTally, segments  # noqa: E402
from test_app_flows import app_env, run, save  # noqa: E402,F401


class TestSegments:
    def test_sentences_split_where_a_cat_tool_would(self):
        text = "Book directly and save. Members get late check-out, e.g. until 2 p.m. on request! Dr. Lee agrees?"
        assert segments(text) == [
            "Book directly and save.",
            "Members get late check-out, e.g. until 2 p.m. on request!",
            "Dr. Lee agrees?",
        ]

    def test_headings_list_markers_and_table_cells(self):
        text = "## Rooms\n- Free Wi-Fi\n1. Breakfast included\n| Room | Size |\n|---|---|\n| Deluxe | 40 m² |"
        assert segments(text) == ["Rooms", "Free Wi-Fi", "Breakfast included", "Room", "Size", "Deluxe", "40 m²"]

    def test_cjk_full_stops_need_no_space(self):
        assert segments("这是第一句。这是第二句！") == ["这是第一句。", "这是第二句！"]

    def test_punctuation_only_pieces_are_not_segments(self):
        assert segments("—\n* * *\n...") == []


class TestTally:
    def test_the_first_occurrence_is_new_and_later_ones_repeat(self):
        tally = RepetitionTally()
        page = "Our story begins here. Book now and save 10%."
        assert tally.add(page, 9) == 0
        assert tally.add("A different page entirely. Book now and save 10%.", 9) == 5
        summary = tally.summary()
        assert summary["words_total"] == 18 and summary["words_repeated"] == 5
        assert summary["top"] == [{"text": "Book now and save 10%.", "count": 2, "words": 5}]

    def test_repeats_within_one_page_count(self):
        assert RepetitionTally().add("Learn more.\nLearn more.\nLearn more.", 6) == 4

    def test_differing_only_by_numbers_is_a_near_repetition(self):
        tally = RepetitionTally()
        tally.add("Room 101, 2 guests.", 4)
        assert tally.add("Room 204, 3 guests.", 4) == 0, "not an exact repetition"
        assert tally.summary()["words_near"] == 4

    def test_never_more_repeated_than_the_page_has(self):
        tally = RepetitionTally()
        tally.add("Same sentence here.", 3)
        assert tally.add("Same sentence here.", 1) == 1

    def test_a_restart_marks_the_estimate_partial(self):
        assert RepetitionTally().summary(partial=True)["partial"] is True

    def test_a_saved_tally_carries_on_exactly_as_if_never_stopped(self):
        from app.word_count import count_words

        pages = [
            "Welcome to the resort. Book now and save 10%.",
            "Spa and dining. Book now and save 10%.",
            "Room 101, 2 guests.",
            "Room 204, 3 guests. Book now and save 10%.",
        ]
        steady = RepetitionTally()
        for page in pages:
            steady.add(page, count_words(page))
        restarted = RepetitionTally()
        for page in pages[:2]:
            restarted.add(page, count_words(page))
        restarted, partial = RepetitionTally.load(restarted.dump())
        assert partial is False
        for page in pages[2:]:
            restarted.add(page, count_words(page))
        assert restarted.summary() == steady.summary()

    def test_a_cut_short_tally_is_refused(self):
        tally = RepetitionTally()
        tally.add("One sentence here. Another one there.", 6)
        with pytest.raises(ValueError):
            RepetitionTally.load(tally.dump()[:-8])

    def test_a_tally_written_on_the_other_byte_order_still_loads(self):
        import json
        from array import array

        tally = RepetitionTally()
        tally.add("One sentence here. Another one there. One sentence here.", 9)
        data = tally.dump()
        size = int.from_bytes(data[:4], "big")
        header = json.loads(data[4:4 + size])
        header["byteorder"] = "big" if sys.byteorder == "little" else "little"
        hashes = array("Q")
        hashes.frombytes(data[4 + size:])
        hashes.byteswap()
        header_bytes = json.dumps(header).encode()
        loaded, _ = RepetitionTally.load(len(header_bytes).to_bytes(4, "big") + header_bytes + hashes.tobytes())
        assert loaded.summary() == tally.summary()
        assert loaded.add("One sentence here.", 3) == 3, "still recognises what it had seen"


# ---------------------------------------------------------- across a restart

def test_a_restarted_crawl_picks_its_tally_back_up(app_env):
    from app import repetition
    from app.job_store import JOBS, restore_job

    tally = RepetitionTally()
    tally.add("Book now and save. Welcome to the hotel.", 8)
    tally.add("Book now and save. See you soon.", 7)
    words = tally.words_total

    def restored(total_words):
        save(app_env, run_id="restarted", status="crawling", total_words=total_words)
        job = restore_job(run(app_env.db.get_run("restarted")))
        JOBS.pop(job.id, None)
        return job

    repetition.save("restarted", tally.dump())
    job = restored(words)
    assert job.repetition.summary() == tally.summary()
    assert job.repetition_partial is False, "nothing was lost"

    # The process died after a checkpoint but before its tally was written:
    # the tally from the one before covers fewer words, and says so.
    job = restored(words + 50)
    assert job.repetition.words_total == words and job.repetition_partial is True

    # A tally ahead of the run can't be squared with it.
    job = restored(words - 7)
    assert job.repetition.words_total == 0 and job.repetition_partial is True

    repetition.delete("restarted")
    job = restored(words)
    assert job.repetition.words_total == 0 and job.repetition_partial is True


# ------------------------------------------------------------ a real crawl

CTA = "Book directly with us and members save up to 10% more on every single stay."
DISCLAIMER = "All prices include local taxes and service fees, and member terms and conditions apply."
UNIQUE = [
    "Our flagship resort sits on a quiet bay with private villas and a spa among the trees.",
    "The city hotel puts you steps from the old market, with a rooftop bar overlooking the river.",
    "Mountain lodges offer guided hikes each morning and long dinners by the fire every evening.",
    "Island retreats come with overwater pavilions, reef snorkelling and sunset sailing trips.",
    "Family suites include connecting rooms, a kids club and a pool that stays warm all year.",
]


@pytest.fixture(scope="module")
def repetitive_site():
    pages = {"/": 0, "/one": 1, "/two": 2, "/three": 3, "/four": 4}
    links = "".join(f"<a href='{p}'>{p.strip('/')} page</a> " for p in pages if p != "/")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in pages:
                self.send_response(404)
                self.end_headers()
                return
            i = pages[self.path]
            body = (f"<h1>Resort number {'ABCDE'[i]}</h1><p>{UNIQUE[i]}</p><p>{CTA}</p><p>{DISCLAIMER}</p>"
                    + (f"<main><p>{links}</p></main>" if self.path == "/" else ""))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html lang='en'><head><title>{self.path}</title></head><body>{body}</body></html>".encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_a_crawl_counts_shared_paragraphs_as_repetitions(app_env, repetitive_site, monkeypatch):
    from crawl4ai.deep_crawling.filters import URLFilter

    from app import crawler, repetition
    from app.job_store import create_job
    from app.word_count import count_words

    async def no_email(*args, **kwargs):
        pass

    class AllowAll(URLFilter):  # crawl4ai's DomainFilter can't match a host with a port
        def __init__(self, *args, **kwargs):
            super().__init__(name="AllowAll")

        def apply(self, url):
            return True

    monkeypatch.setattr(crawler, "send_crawl_notification", no_email)
    monkeypatch.setattr(crawler, "DomainFilter", AllowAll)
    # Checkpoint often enough that the tally is written mid-crawl.
    monkeypatch.setattr(crawler, "_CHECKPOINT_EVERY", 2)
    tallies_written = []
    save_tally = repetition.save
    monkeypatch.setattr(repetition, "save", lambda run_id, data: (tallies_written.append(run_id), save_tally(run_id, data)))

    job = create_job(source_url=repetitive_site + "/", user_id=1, max_pages=float("inf"))
    job.concurrency = 2
    try:
        run(crawler.run_crawl(job.id, repetitive_site + "/", float("inf")))
    except Exception as exc:
        pytest.skip(f"browser unavailable: {exc}")

    counted = [p for p in job.pages.values() if p.success]
    assert job.status == "completed", job.error
    assert len(counted) == 5
    shared = count_words(CTA) + count_words(DISCLAIMER)
    # Five pages share both paragraphs: the first is new, four repeat them.
    assert sum(p.repeated_words for p in counted) == 4 * shared
    saved = run(app_env.db.get_run(job.id))
    assert saved.repetition["words_repeated"] == 4 * shared
    assert saved.repetition["words_total"] == saved.total_words, "the total itself is unchanged"
    assert {t["text"] for t in saved.repetition["top"][:2]} == {CTA, DISCLAIMER}

    assert tallies_written, "saved at checkpoints, for a restart to pick up"
    assert repetition.load(job.id) is None, "and removed once the run is over"

    # Its sentences are kept, page by page, for a project to compare.
    from app import segment_store

    pages = segment_store.read(job.id)
    assert set(pages) == {segment_store.url_key(p.url) for p in counted}, "every counted page, nothing else"
    cta = repetition._exact_key(CTA)
    assert sum(list(hashes).count(cta) for hashes, _ in pages.values()) == 5, "on each of the five pages"
    assert segment_store.read_texts(job.id, {cta}) == {cta: CTA}, "its text, kept once"
    assert not any(segment_store.url_key(url) in pages for url in job.duplicates), "duplicates have no record"

    csv_text = app_env.client.get(f"/crawl/{job.id}/export.csv").text
    assert "Repeated words" in csv_text.splitlines()[0]


# ---------------------------------------------------------------- the report

def test_the_report_shows_the_estimate_and_hides_it_without_data(app_env):
    save(app_env, run_id="rep", total_words=840, repetition={
        "words_total": 840, "words_repeated": 210, "words_near": 42, "segments_total": 90,
        "segments_unique": 60, "top": [{"text": "Book now and save.", "count": 12, "words": 44}],
        "partial": False, "capped": False})
    save(app_env, run_id="partial", total_words=840, repetition={
        "words_total": 420, "words_repeated": 105, "words_near": 0, "segments_total": 45,
        "segments_unique": 30, "top": [], "partial": True, "capped": False})
    save(app_env, run_id="old")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    from test_report_page_script import ORIGIN

    def answer(route):
        url = route.request.url
        if not url.startswith(ORIGIN):
            return route.abort()
        res = app_env.client.get(url[len(ORIGIN):], headers={"host": "report.test"})
        route.fulfill(status=res.status_code, headers=dict(res.headers), body=res.content)

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.route("**/*", answer)
        page.goto(ORIGIN + "/crawl/rep")
        page.wait_for_load_state("networkidle")
        shown = page.locator("#repetition").is_visible()
        repeated = page.inner_text("#rep-repeated")
        top = page.inner_text("#rep-top")
        hero = page.inner_text("#hero-repeated")
        coverage_shown = page.locator("#coverage").is_visible()
        unchecked_shown = page.locator("#cov-unchecked-item").is_visible()
        page.goto(ORIGIN + "/crawl/partial")
        page.wait_for_load_state("networkidle")
        partial_hero = page.inner_text("#hero-repeated-sub")
        partial_unchecked = page.inner_text("#cov-unchecked-item")
        partial_pill = page.inner_text("#rep-coverage-pill")
        page.goto(ORIGIN + "/crawl/old")
        page.wait_for_load_state("networkidle")
        hidden = not page.locator("#repetition").is_visible()
        hero_hidden = not page.locator("#hero-repeated").is_visible()
        coverage_hidden = not page.locator("#coverage").is_visible()
        link_hidden = not page.locator("#report-nav a[href='#repetition']").is_visible()
        browser.close()

    assert errors == []
    assert shown and "210 words" in repeated and "25%" in repeated
    # Seen 12 times, 44 words added: 4 words each.
    assert "Book now and save." in top and "12×" in top and "44" in top and "4" in top.split()
    assert "25%" in hero and "210 words" in hero, "the repeated share sits next to the total"
    assert coverage_shown and not unchecked_shown, "a complete estimate has nothing unchecked"
    assert "of the 420 words checked" in partial_hero
    assert "Not checked" in partial_unchecked and "420" in partial_unchecked
    assert "Covers 420 of 840 words" in partial_pill
    assert hidden and hero_hidden and coverage_hidden and link_hidden, \
        "runs from before the estimate don't show an empty block"
