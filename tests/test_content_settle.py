"""Pages that fill themselves in after loading must be read once they have.

A hotel's accommodation page on a Next.js site came back as 79 words and 3
links on one load and 835 words and 22 links on the next: the room list
arrives after the page loads, and reading too early lost the text and every
link to a room — so two crawls of one site disagreed by 2x. The crawler now
waits for the visible text to stop changing before it reads a page.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_app_flows import app_env, run  # noqa: E402,F401

LATE = "Every room has a private terrace with views across the bay and a deep soaking tub for two guests."
# The list, and the link to the room, come from a data request the server
# answers 1.5s late — the way a Next.js page fetches its rooms.
_LISTING = """<html lang="en"><head><title>Accommodation</title></head><body>
<h1>Accommodation</h1><p>Choose from our collection of rooms and suites by the sea.</p>
<main id="list"></main>
<script>
fetch("/rooms.json").then(r => r.json()).then(data => {
  document.getElementById("list").innerHTML =
    "<p>" + data.text + "</p><p><a href='" + data.href + "'>Ocean Suite</a></p>";
});
</script></body></html>"""
_ROOM = """<html lang="en"><head><title>Ocean Suite</title></head><body><h1>Ocean Suite</h1>
<p>The ocean suite spans two floors with a living room, a kitchenette and a wide balcony facing the water.</p>
</body></html>"""


@pytest.fixture(scope="module")
def late_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/rooms.json":
                time.sleep(1.5)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"text": LATE, "href": "/ocean-suite"}).encode())
                return
            body = {"/": _LISTING, "/ocean-suite": _ROOM}.get(self.path)
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((body or "").encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_content_that_arrives_after_load_is_counted_and_followed(app_env, late_site, monkeypatch):
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

    job = create_job(source_url=late_site + "/", user_id=1, max_pages=float("inf"))
    job.concurrency = 2
    try:
        run(crawler.run_crawl(job.id, late_site + "/", float("inf")))
    except Exception as exc:
        pytest.skip(f"browser unavailable: {exc}")

    assert job.status == "completed", job.error
    counted = {url.removeprefix(late_site): page for url, page in job.pages.items() if page.success}
    assert "/ocean-suite" in counted, "a link that arrived late was followed"
    assert counted["/"].word_count >= 30, "the late list was counted"
