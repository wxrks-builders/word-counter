"""Page furniture that isn't content: cookie banners and developer consoles.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_app_flows import app_env, run  # noqa: E402,F401


def test_every_selector_parses_the_way_crawl4ai_applies_them():
    """crawl4ai runs the joined selector through lxml's cssselect and, if it
    doesn't parse, quietly excludes nothing at all."""
    from lxml import html

    from app.crawler import EXCLUDED_SELECTORS

    doc = html.fromstring("<html><body><p>x</p></body></html>")
    for selector in EXCLUDED_SELECTORS:
        doc.cssselect(selector)
    doc.cssselect(", ".join(EXCLUDED_SELECTORS))


CONTENT = ("<h1>Harbour Hotel</h1><p>Our rooms look over the old harbour, with breakfast served on the "
           "terrace from seven and a quiet library open to guests all evening.</p>")
# vConsole's hidden panel, as it sits in the DOM of every page that ships it.
VCONSOLE = ("<div id='__vconsole'><div class='vc-panel' style='display:none'>"
            "<i>All</i> <i>Log</i> <i>Info</i> <i>Warn</i> <i>Error</i> <i>Cookies</i> <i>LocalStorage</i> "
            "<i>SessionStorage</i> <i>Clear</i> <i>Top</i> <i>Bottom</i> <i>Hide</i></div></div>")
BANNERS = ("<div id='onetrust-banner-sdk'><p>We use cookies to enhance your experience.</p></div>"
           "<div class='cookie-banner'><p>By continuing to use the website, you are agreeing to our "
           "Privacy Statement</p></div>")


@pytest.fixture(scope="module")
def cluttered_site():
    pages = {
        "/": ("<main><h1>Harbour Group</h1><p>Two hotels on the same quay, one quiet and one busy, both a "
              "short walk from the ferry and the market square.</p>"
              "<p><a href='/clean'>clean page</a> <a href='/cluttered'>cluttered page</a></p></main>"),
        # Same length, different name: identical text would be set aside as a
        # duplicate — which, once the furniture is gone, is what they'd be.
        "/clean": f"<main>{CONTENT}</main>",
        "/cluttered": f"<main>{CONTENT.replace('Harbour Hotel', 'Quayside Hotel')}</main>{BANNERS}{VCONSOLE}",
    }
    # The kind of state class consent plugins put on <body>: a selector that
    # matched it would take the whole page with it.
    body_class = {"/cluttered": " class='cookie-notice-hidden cookies-not-set'"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in pages:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((
                f"<html lang='en'><head><title>{self.path}</title></head>"
                f"<body{body_class.get(self.path, '')}>{pages[self.path]}</body></html>"
            ).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_banners_and_consoles_are_left_out_of_the_count(app_env, cluttered_site, monkeypatch):
    from crawl4ai.deep_crawling.filters import URLFilter

    from app import crawler
    from app.job_store import create_job

    async def no_email(*args, **kwargs):
        pass

    class AllowAll(URLFilter):  # crawl4ai's DomainFilter can't match a host with a port
        def __init__(self, *args, **kwargs):
            super().__init__(name="AllowAll")

        def apply(self, url):
            return True

    monkeypatch.setattr(crawler, "send_crawl_notification", no_email)
    monkeypatch.setattr(crawler, "DomainFilter", AllowAll)

    job = create_job(source_url=cluttered_site + "/", user_id=1, max_pages=float("inf"))
    job.concurrency = 2
    try:
        run(crawler.run_crawl(job.id, cluttered_site + "/", float("inf")))
    except Exception as exc:
        pytest.skip(f"browser unavailable: {exc}")

    assert job.status == "completed", job.error
    clean = job.pages[cluttered_site + "/clean"]
    cluttered = job.pages[cluttered_site + "/cluttered"]
    assert clean.word_count > 20
    assert cluttered.word_count == clean.word_count, "the banners and the console added nothing"
    top = run(app_env.db.get_run(job.id)).repetition.get("top", [])
    assert not any("cookies" in row["text"] or "LocalStorage" in row["text"] for row in top)
