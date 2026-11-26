"""Crawling a site behind a browser password pop-up (HTTP Basic Auth).

The promises this feature makes, each tested here: the password reaches the
site and nothing else, it is never stored, a site that doesn't ask for one
never receives it, and someone without it can't be handed the report.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import base64
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))

from app.site_auth import SiteAuth, from_form, probe  # noqa: E402
from test_app_flows import app_env, run  # noqa: E402,F401

USER, PASSWORD = "admin", "s3cret-staging-pw"
_BODY = " ".join(f"word{i}" for i in range(80))


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture(scope="module")
def third_party():
    """Stands in for a font host or CDN the page loads from: it must never see
    the password."""
    seen: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "image/gif")
            self.end_headers()
            self.wfile.write(b"GIF89a")

        def log_message(self, *args):
            pass

    server, origin = _serve(Handler)
    yield origin, seen
    server.shutdown()


@pytest.fixture(scope="module")
def staging(third_party):
    third_origin, _ = third_party
    expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    requested: list[str] = []
    pages = {
        "/": f"<h1>Staging home</h1><p>{_BODY}</p><a href='/about'>About</a>"
             f"<img src='{third_origin}/pixel.gif'><link rel='stylesheet' href='/style.css'>",
        "/about": f"<h1>About</h1><p>{_BODY} {_BODY}</p>",
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="staging"')
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><title>401 Unauthorized</title></html>")
                return
            body = pages.get(self.path)
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/css" if self.path.endswith(".css") else "text/html")
            self.end_headers()
            html = f"<html lang='en'><head><title>{self.path}</title></head><body>{body or ''}</body></html>"
            self.wfile.write(html.encode())

        def log_message(self, *args):
            pass

    server, origin = _serve(Handler)
    yield origin, requested
    server.shutdown()


@pytest.fixture(scope="module")
def open_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>open</body></html>")

        def log_message(self, *args):
            pass

    server, origin = _serve(Handler)
    yield origin
    server.shutdown()


# ------------------------------------------------------------ where it's sent

class TestSiteAuth:
    auth = SiteAuth.for_site("https://stg.example.com/start", USER, PASSWORD)

    def test_only_the_exact_host(self):
        assert self.auth.applies_to("https://stg.example.com/about")
        assert not self.auth.applies_to("https://www.example.com/")
        assert not self.auth.applies_to("https://auth.stg.example.com/")
        assert not self.auth.applies_to("https://fonts.googleapis.com/css")
        assert not self.auth.applies_to("https://stg.example.com:8443/")

    def test_never_downgraded_to_plain_http(self):
        assert not self.auth.applies_to("http://stg.example.com/")
        plain = SiteAuth.for_site("http://stg.example.com/", USER, PASSWORD)
        assert plain.applies_to("https://stg.example.com/")

    def test_the_password_stays_out_of_reprs(self):
        assert PASSWORD not in repr(self.auth)

    def test_an_empty_form_means_no_auth(self):
        assert from_form("https://x.com", "", None) is None
        assert from_form("https://x.com", "  ", "") is None


class TestProbe:
    def test_answers(self, staging, open_site):
        origin, _ = staging
        assert asyncio.run(probe(origin + "/")) == "needs_auth"
        assert asyncio.run(probe(origin + "/", SiteAuth.for_site(origin, USER, "wrong"))) == "rejected"
        assert asyncio.run(probe(origin + "/", SiteAuth.for_site(origin, USER, PASSWORD))) == "ok"
        assert asyncio.run(probe(open_site + "/")) == "ok"

    def test_an_unreachable_site_never_blocks_a_crawl(self):
        assert asyncio.run(probe("http://127.0.0.1:9/")) == "ok"


# ------------------------------------------------------------------ the route

@pytest.fixture()
def no_crawl(app_env, monkeypatch):
    started = []

    async def fake_run_crawl(*args, **kwargs):
        started.append(args)

    monkeypatch.setattr(app_env.main, "run_crawl", fake_run_crawl)
    return started


def test_a_protected_site_asks_for_the_password(app_env, no_crawl, staging):
    origin, _ = staging
    res = app_env.client.post("/crawl", json={"url": origin + "/"})
    assert res.status_code == 400 and res.json() == {"code": "site_password_needed"}

    res = app_env.client.post("/crawl", json={"url": origin + "/", "site_username": USER, "site_password": "nope"})
    assert res.json() == {"code": "site_password_rejected"}
    assert not no_crawl


def test_the_right_password_starts_the_crawl_and_stays_in_memory(app_env, no_crawl, staging):
    origin, _ = staging
    res = app_env.client.post("/crawl", json={"url": origin + "/", "site_username": USER, "site_password": PASSWORD})
    assert res.status_code == 200, res.text
    job = app_env.main.get_job(res.json()["run_id"])
    assert job.password_protected
    assert job.site_auth.password == PASSWORD
    assert PASSWORD not in repr(job)


def test_a_site_that_does_not_ask_never_receives_it(app_env, no_crawl, open_site):
    res = app_env.client.post("/crawl", json={"url": open_site + "/", "site_username": USER, "site_password": PASSWORD})
    job = app_env.main.get_job(res.json()["run_id"])
    assert job.site_auth is None and not job.password_protected


def test_nobody_else_is_handed_a_protected_report_from_the_cache(app_env, no_crawl, staging):
    origin, _ = staging
    source = app_env.db.normalize_url(origin + "/")
    run(app_env.db.save_run("prot", source, app_env.owner.id, "completed", 100, [], False, password_protected=True))

    app_env.current["user"] = app_env.owner
    assert app_env.client.post("/crawl", json={"url": origin + "/"}).json() == {"cached": True, "run_id": "prot"}

    app_env.current["user"] = app_env.other
    assert app_env.client.post("/crawl", json={"url": origin + "/"}).json() == {"code": "site_password_needed"}


def test_the_report_says_it_was_password_protected(app_env, staging):
    origin, _ = staging
    run(app_env.db.save_run("prot", origin + "/", app_env.owner.id, "completed", 100, [], False, password_protected=True))
    assert "Password-protected" in app_env.client.get("/crawl/prot").text


def test_a_paused_crawl_whose_password_was_lost_cannot_resume(app_env):
    job = app_env.main.create_job(source_url="https://stg.example.com", user_id=1, max_pages=10)
    job.status, job.password_protected = "paused", True
    res = app_env.client.post(f"/crawl/{job.id}/resume", json={})
    assert res.status_code == 409 and res.json() == {"code": "site_password_gone"}


# ------------------------------------------------------------- a real crawl

def test_a_real_crawl_counts_the_site_and_keeps_the_password_to_itself(app_env, staging, third_party, tmp_path, monkeypatch):
    from app import crawler
    from app.job_store import create_job

    async def no_email(*args, **kwargs):
        pass

    monkeypatch.setattr(crawler, "send_crawl_notification", no_email)

    # crawl4ai's DomainFilter can't match a host with a port, so no local
    # server ever passes it. Scope has its own tests; this one is about auth.
    from crawl4ai.deep_crawling.filters import URLFilter

    class AllowAll(URLFilter):
        def __init__(self, *args, **kwargs):
            super().__init__(name="AllowAll")

        def apply(self, url):
            return True

    monkeypatch.setattr(crawler, "DomainFilter", AllowAll)

    origin, requested = staging
    _, third_party_saw = third_party
    job = create_job(source_url=origin + "/", user_id=1, max_pages=float("inf"))
    job.site_auth = SiteAuth.for_site(origin, USER, PASSWORD)
    job.password_protected = True
    job.concurrency = 2

    try:
        run(crawler.run_crawl(job.id, origin + "/", float("inf")))
    except Exception as exc:  # no Chromium on this machine
        pytest.skip(f"browser unavailable: {exc}")

    counted = {url.rstrip("/").rsplit("/", 1)[-1] for url, page in job.pages.items() if page.success}
    assert job.status == "completed", job.error
    assert {"about"} <= counted and len(counted) == 2, job.pages
    assert not job.login_blocked

    # Reached the third party, but without the password.
    assert third_party_saw and not any(third_party_saw)
    # crawl4ai's own routes still ran after ours: the stylesheet was blocked.
    assert "/style.css" not in requested

    # Finished with it, and never wrote it down.
    assert job.site_auth is None
    saved = run(app_env.db.get_run(job.id))
    assert saved.password_protected
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert PASSWORD.encode() not in path.read_bytes(), path
