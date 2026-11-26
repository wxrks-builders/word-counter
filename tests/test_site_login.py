"""Crawling a site behind a sign-in page — an email and password on a form.

The promises this feature makes, each tested here: a wrong password is said
on the form; the email, password and session are never stored; a signed-in
crawl never opens a link that signs out, deletes or cancels; someone without
the sign-in can't be handed the report.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))

from app import site_login  # noqa: E402
from app.site_login import SignInError, parse_session, unsafe_link  # noqa: E402
from test_app_flows import app_env, run  # noqa: E402,F401

EMAIL, PASSWORD = "reader@client.test", "s3cret-sign-in-pw"
TOKEN = "tok-5f1e9a"
_BODY = " ".join(f"word{i}" for i in range(80))

LOGIN_FORM = ("<form method='post' action='/login'><label>Email <input type='email' name='email'></label>"
              "<label>Password <input type='password' name='password'></label>"
              "<button type='submit'>Sign in</button></form>")
EMAIL_FIRST = ("<form method='get' action='/login2/password'><input type='email' name='email' placeholder='Email'>"
               "<button>Next</button></form>")


def _password_step(email: str) -> str:
    return (f"<form method='post' action='/login'><input type='hidden' name='email' value='{email}'>"
            "<input type='password' name='password'><button type='submit'>Sign in</button></form>")


@pytest.fixture(scope="module")
def members():
    """A site that sends everyone to its sign-in page, with a log-out link and
    a cancel link on every page a signed-in crawler must never open."""
    requested: list[str] = []
    signed_in_hits: list[str] = []
    pages = {
        "/": (f"<h1>Members</h1><p>{_BODY}</p><a href='/about'>About</a> <a href='/account/logout'>Log out</a> "
              "<a href='/bookings/7/cancel'>Cancel booking</a> <a href='/blog/how-to-remove-stains'>Stains</a>"),
        "/about": f"<h1>About</h1><p>{_BODY} {_BODY}</p>",
        "/blog/how-to-remove-stains": f"<h1>Stains</h1><p>{_BODY}</p>",
        "/account/logout": "<p>Signed out</p>",
        "/bookings/7/cancel": "<p>Booking cancelled</p>",
    }

    class Handler(BaseHTTPRequestHandler):
        def _signed_in(self) -> bool:
            return f"session={TOKEN}" in (self.headers.get("Cookie") or "")

        def _send(self, status: int, title: str, body: str, headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(f"<html lang='en'><head><title>{title}</title></head><body>{body}</body></html>".encode())

        def do_GET(self):
            path, _, query = self.path.partition("?")
            requested.append(path)
            if path == "/login":
                return self._send(200, "Sign in", LOGIN_FORM)
            if path == "/login2":
                return self._send(200, "Sign in", EMAIL_FIRST)
            if path == "/login2/password":
                email = parse_qs(query).get("email", [""])[0]
                return self._send(200, "Sign in", _password_step(email))
            if path == "/public":
                return self._send(200, "Public", f"<p>{_BODY}</p>")
            if not self._signed_in():
                return self._send(302, "Redirect", "", {"Location": "/login"})
            signed_in_hits.append(path)
            body = pages.get(path)
            if body is None:
                return self._send(404, "Not found", "")
            self._send(200, path, body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode())
            requested.append("POST " + self.path)
            if form.get("email") == [EMAIL] and form.get("password") == [PASSWORD]:
                return self._send(303, "Redirect", "",
                                  {"Location": "/", "Set-Cookie": f"session={TOKEN}; Path=/; HttpOnly"})
            self._send(200, "Sign in", "<p>Wrong email or password</p>" + LOGIN_FORM)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield type("Members", (), {"url": f"http://127.0.0.1:{server.server_address[1]}",
                               "requested": requested, "signed_in_hits": signed_in_hits})()
    server.shutdown()


def sign_in(*args, **kwargs):
    try:
        return asyncio.run(site_login.sign_in(*args, **kwargs))
    except SignInError as exc:
        if exc.code == "site_login_unavailable":
            pytest.skip("no browser here")
        raise


# ------------------------------------------------------------------ links it never opens

@pytest.mark.parametrize("url", [
    "https://x.com/account/logout", "https://x.com/users/sign_out", "https://x.com/auth/log-out?next=/",
    "https://x.com/bookings/7/cancel", "https://x.com/account/delete-account", "https://x.com/cart?action=remove",
    "https://x.com/newsletter/unsubscribe?id=4", "https://x.com/api-keys/9/revoke",
])
def test_links_that_change_the_account_are_never_opened(url):
    assert unsafe_link(url)


@pytest.mark.parametrize("url", [
    "https://x.com/blog/how-to-remove-stains", "https://x.com/pricing", "https://x.com/account/settings",
    "https://x.com/docs/cancellation-policy-explained", "https://x.com/?page=2",
])
def test_ordinary_pages_are(url):
    assert not unsafe_link(url)


# ------------------------------------------------------------------ a pasted session

def test_a_cookie_header_is_read_with_or_without_its_name():
    session = parse_session("Cookie: session=abc; csrftoken=x=y", "https://stg.example.com/start")
    assert session.cookies == (("session", "abc", None), ("csrftoken", "x=y", None))
    assert session.origin == "https://stg.example.com"
    assert parse_session("session=abc", "https://stg.example.com").cookies == (("session", "abc", None),)


def test_a_cookie_extensions_export_is_read_too():
    raw = '[{"name": "session", "value": "abc", "domain": ".example.com"}, {"name": "theme", "value": "dark"}]'
    assert [c[0] for c in parse_session(raw, "https://stg.example.com").cookies] == ["session", "theme"]


def test_what_isnt_a_session_is_said_so():
    assert parse_session("   ", "https://x.com") is None
    for raw in ("just some words", "[not json", "{}"):
        with pytest.raises(SignInError) as caught:
            parse_session(raw, "https://x.com")
        assert caught.value.code == "site_session_unreadable"


def test_the_session_is_host_only_and_stays_out_of_reprs():
    session = parse_session("session=" + TOKEN, "https://stg.example.com/a")
    assert session.playwright_cookies() == [{"name": "session", "value": TOKEN, "url": "https://stg.example.com"}]
    assert TOKEN not in repr(session)


# ------------------------------------------------------------------ signing in

def test_signing_in_on_the_sites_own_form(members):
    session = sign_in(members.url + "/", members.url + "/login", EMAIL, PASSWORD)
    assert ("session", TOKEN) in {(name, value) for name, value, _ in session.cookies}
    assert session.origin == members.url


def test_the_form_is_found_where_the_start_page_sends_you(members):
    session = sign_in(members.url + "/", None, EMAIL, PASSWORD)
    assert any(name == "session" for name, _, _ in session.cookies)


def test_email_first_then_password_on_the_next_screen(members):
    session = sign_in(members.url + "/", members.url + "/login2", EMAIL, PASSWORD)
    assert any(value == TOKEN for _, value, _ in session.cookies)


def test_a_wrong_password_is_said_on_the_form(members):
    with pytest.raises(SignInError) as caught:
        sign_in(members.url + "/", members.url + "/login", EMAIL, "wrong")
    assert caught.value.code == "site_login_rejected"


def test_a_page_without_a_form_asks_for_the_sign_in_page(members):
    with pytest.raises(SignInError) as caught:
        sign_in(members.url + "/public", None, EMAIL, PASSWORD)
    assert caught.value.code == "site_login_form_not_found"


def test_a_pasted_session_is_checked_before_the_crawl(members):
    good = parse_session(f"Cookie: session={TOKEN}; theme=dark", members.url + "/")
    try:
        asyncio.run(site_login.check_session(members.url + "/", good))
    except SignInError as exc:
        if exc.code == "site_login_unavailable":
            pytest.skip("no browser here")
        raise
    with pytest.raises(SignInError) as caught:
        asyncio.run(site_login.check_session(members.url + "/", parse_session("session=expired", members.url)))
    assert caught.value.code == "site_session_rejected"


# ------------------------------------------------------------------ the route

@pytest.fixture()
def no_crawl(app_env, monkeypatch):
    started = []

    async def fake_run_crawl(*args, **kwargs):
        started.append(args)

    monkeypatch.setattr(app_env.main, "run_crawl", fake_run_crawl)
    return started


def test_the_crawl_form_signs_in_and_keeps_only_the_session_in_memory(app_env, no_crawl, members):
    res = app_env.client.post("/crawl", json={"url": members.url + "/", "login_url": members.url + "/login",
                                              "login_email": EMAIL, "login_password": PASSWORD})
    if res.json().get("code") == "site_login_unavailable":
        pytest.skip("no browser here")
    assert res.status_code == 200, res.text
    job = app_env.main.get_job(res.json()["run_id"])
    assert job.signed_in and job.login_url == members.url + "/login"
    assert any(value == TOKEN for _, value, _ in job.site_session.cookies)
    assert PASSWORD not in repr(vars(job)) and TOKEN not in repr(job), "the password isn't kept at all"


def test_a_wrong_password_never_starts_a_crawl(app_env, no_crawl, members):
    res = app_env.client.post("/crawl", json={"url": members.url + "/", "login_url": members.url + "/login",
                                              "login_email": EMAIL, "login_password": "nope"})
    if res.json().get("code") == "site_login_unavailable":
        pytest.skip("no browser here")
    assert res.status_code == 400 and res.json() == {"code": "site_login_rejected"}
    assert not no_crawl


def test_half_a_sign_in_is_said_so(app_env, no_crawl, members):
    res = app_env.client.post("/crawl", json={"url": members.url + "/public", "login_email": EMAIL})
    assert res.json() == {"code": "site_login_incomplete"}


def test_nobody_else_is_handed_a_signed_in_report_from_the_cache(app_env, no_crawl):
    source = app_env.db.normalize_url("https://members.example.com/")
    run(app_env.db.save_run("in", source, app_env.owner.id, "completed", 100, [], False, signed_in=True))
    assert app_env.client.post("/crawl", json={"url": source}).json() == {"cached": True, "run_id": "in"}
    app_env.current["user"] = app_env.other
    assert app_env.client.post("/crawl", json={"url": source}).json().get("cached") is False


def test_the_report_says_it_was_signed_in(app_env):
    run(app_env.db.save_run("in", "https://members.example.com", app_env.owner.id, "completed", 100, [], False,
                            signed_in=True, login_url="https://members.example.com/login"))
    report = app_env.client.get("/crawl/in").text
    assert ">Signed in<" in report
    assert 'id="recrawl-login"' in report and 'value="https://members.example.com/login"' in report


def test_a_paused_crawl_whose_sign_in_was_lost_cannot_resume(app_env):
    job = app_env.main.create_job(source_url="https://members.example.com", user_id=1, max_pages=10)
    job.status, job.signed_in = "paused", True
    res = app_env.client.post(f"/crawl/{job.id}/resume", json={})
    assert res.status_code == 409 and res.json() == {"code": "site_session_gone"}


def test_a_restart_fails_a_signed_in_crawl_and_says_why(app_env):
    from fastapi.testclient import TestClient

    run(app_env.db.save_run("in", "https://members.example.com", app_env.owner.id, "crawling", 0, [], False,
                            signed_in=True))
    with TestClient(app_env.main.app):  # a fresh start of the app
        restarted = run(app_env.db.get_run("in"))
    assert restarted.status == "failed" and restarted.error == site_login.SESSION_GONE


# ------------------------------------------------------------------ a real crawl

def test_a_signed_in_crawl_counts_the_pages_and_never_signs_itself_out(app_env, members, monkeypatch, tmp_path):
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

    session = sign_in(members.url + "/", members.url + "/login", EMAIL, PASSWORD)
    members.signed_in_hits.clear()
    job = create_job(source_url=members.url + "/", user_id=1, max_pages=float("inf"))
    job.site_session, job.signed_in, job.login_url = session, True, members.url + "/login"
    job.concurrency = 2
    run(crawler.run_crawl(job.id, members.url + "/", float("inf")))

    assert job.status == "completed", job.error
    counted = {urlsplit(url).path for url, page in job.pages.items() if page.success}
    assert {"/", "/about", "/blog/how-to-remove-stains"} <= counted, job.pages
    assert not job.login_blocked
    assert "/account/logout" not in members.signed_in_hits, "it never signed itself out"
    assert "/bookings/7/cancel" not in members.signed_in_hits, "nor cancelled a booking"

    saved = run(app_env.db.get_run(job.id))
    assert saved.signed_in and job.site_session is None, "the session goes when the crawl ends"
    database = Path(app_env.db.DB_PATH).read_bytes()
    assert TOKEN.encode() not in database and PASSWORD.encode() not in database
