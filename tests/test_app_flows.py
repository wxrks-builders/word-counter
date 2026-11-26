"""End-to-end coverage of the routes, over a throwaway database.

These started life as ad-hoc scripts in a scratch directory and were lost twice
when it was cleaned, so they live here now. They exercise the paths that are
easy to break from a distance: saving a run, the report view, sharing, the
Markdown export and its access rules, deleting, and the two hostnames.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))


@pytest.fixture()
def app_env(monkeypatch, tmp_path):
    """A fresh app with its own database and Markdown store."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("MARKDOWN_DIR", str(tmp_path / "markdown"))
    monkeypatch.setenv("SAMPLE_RUN_URL", "https://sample.example")
    monkeypatch.setenv("COUNTER_DEMO_RUN_ID", "demo")
    monkeypatch.setenv("MARKDOWN_DEMO_RUN_ID", "demo")

    import importlib

    import app.markdown_store as markdown_store
    import app.surfaces as surfaces
    importlib.reload(markdown_store)
    importlib.reload(surfaces)

    import app.db as db
    import app.main as main
    importlib.reload(db)
    importlib.reload(main)

    from fastapi.testclient import TestClient

    from app.auth import get_current_user, require_user, require_user_api

    from app.models import User

    # Real User models rather than ad-hoc stubs: the routes and templates read
    # whatever the User model exposes (plan state, is_pro), and a stub that
    # drifts from it fails as a template error rather than as a useful test.
    owner = User(id=1, google_sub="a", email="o@x.c", name="Owner")
    other = User(id=2, google_sub="b", email="e@x.c", name="Other")
    pro = User(id=1, google_sub="a", email="o@x.c", name="Owner", plan="pro", plan_status="active")
    current = {"user": owner}

    main.app.dependency_overrides[require_user] = lambda: current["user"]
    main.app.dependency_overrides[require_user_api] = lambda: current["user"]
    main.app.dependency_overrides[get_current_user] = lambda: current["user"]

    with TestClient(main.app) as client:
        run(db.get_or_create_user("a", "o@x.c", "Owner", None))
        run(db.get_or_create_user("b", "e@x.c", "Other", None))
        yield type("Env", (), {
            "client": client, "db": db, "main": main, "surfaces": surfaces,
            "store": markdown_store, "owner": owner, "other": other, "pro": pro,
            "current": current,
        })()
    main.app.dependency_overrides.clear()


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def pages(n=8):
    from app.models import PageResult

    return [
        PageResult(url=f"https://x.com/f{i % 3}/p{i}", title=f"Title {i}", word_count=100 + i)
        for i in range(n)
    ]


def save(env, run_id="r", **kw):
    body = dict(
        run_id=run_id, source_url="https://x.com", user_id=1, status="completed",
        total_words=840, pages=pages(), limit_reached=False,
    )
    body.update(kw)
    run(env.db.save_run(**body))
    return run_id


# ----------------------------------------------------------------- the report

def test_a_saved_run_renders_its_report(app_env):
    save(app_env)
    body = app_env.client.get("/crawl/r").text
    assert "initialPages" in body, "the report should be served from the database"
    assert '"total_pages": 8' in body or "summary" in body


def test_listing_pages_render(app_env):
    for i in range(12):
        save(app_env, run_id=f"r{i}")
    assert app_env.client.get("/").status_code == 200
    assert app_env.client.get("/runs").status_code == 200
    assert "See all" in app_env.client.get("/").text


def test_csv_export_covers_every_page(app_env):
    save(app_env)
    body = app_env.client.get("/crawl/r/export.csv").text
    assert body.count("\n") == 9, "header plus one row per page"
    assert "URL,Title,Words,Repeated words,Status,Error" in body


# ------------------------------------------------------------------- sharing

def test_share_link_grants_and_revokes_access(app_env):
    save(app_env)
    app_env.current["user"] = app_env.other
    assert app_env.client.get("/share/r").status_code == 404

    app_env.current["user"] = app_env.owner
    app_env.client.post("/crawl/r/share", json={"is_public": True})
    del app_env.main.app.dependency_overrides[
        __import__("app.auth", fromlist=["get_current_user"]).get_current_user
    ]
    assert app_env.client.get("/share/r").status_code == 200
    assert app_env.client.get("/crawl/r/export.csv").status_code == 200


def test_a_private_run_is_not_readable_by_others(app_env):
    save(app_env)
    app_env.current["user"] = app_env.other
    assert app_env.client.get("/crawl/r/pages").status_code == 404
    assert app_env.client.get("/crawl/r/export.csv").status_code == 404
    assert app_env.client.get("/crawl/r/markdown.zip").status_code == 404
    assert app_env.client.request("DELETE", "/crawl/r").status_code == 404
    assert app_env.client.post("/crawl/r/share", json={"is_public": True}).status_code == 404


@pytest.mark.xfail(
    reason="Known gap: GET /crawl/{id} has no ownership check, so any signed-in user "
           "can open another user's report if they have the id. Same for /resume, "
           "/cancel and /events. Ids are unguessable UUIDs, so it isn't trivially "
           "exploitable, but it is not enforced. Reported and left as-is pending a "
           "decision, since restricting it changes behaviour people may rely on. "
           "This test starts passing the day it's fixed.",
    strict=True,
)
def test_the_report_page_should_also_be_owner_only(app_env):
    save(app_env)
    app_env.current["user"] = app_env.other
    assert app_env.client.get("/crawl/r").status_code == 404


# ------------------------------------------------------------------ Markdown

def test_markdown_zip_round_trips(app_env):
    for page in pages():
        app_env.store.write("r", page.url, f"# {page.title}\n\nbody of {page.title}")
    save(app_env, capture_markdown=True, markdown_pages=8, markdown_bytes=999,
         markdown_state="capturing")

    res = app_env.client.get("/crawl/r/markdown.zip")
    assert res.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(res.content))
    assert archive.testzip() is None
    assert len(archive.namelist()) == 9, "one per page plus the README"
    assert "body of Title 0" in archive.read(
        app_env.store.entry_name("https://x.com/f0/p0")).decode()


def test_markdown_is_not_offered_when_none_was_saved(app_env):
    save(app_env)
    assert app_env.client.get("/crawl/r/markdown.zip").status_code == 404


def test_deleting_a_run_removes_its_markdown(app_env):
    app_env.store.write("r", "https://x.com/f0/p0", "body")
    save(app_env, capture_markdown=True, markdown_pages=1, markdown_bytes=99)
    assert app_env.store.run_bytes("r") > 0

    assert app_env.client.request("DELETE", "/crawl/r").status_code == 200
    assert app_env.store.run_bytes("r") == 0
    assert run(app_env.db.get_run("r")) is None


def test_the_sample_run_copy_carries_no_markdown(app_env):
    """Every new account gets a copy of the sample run. Inheriting the Markdown
    counters would give it a download button pointing at another run's files."""
    save(app_env, run_id="tmpl", capture_markdown=True, markdown_pages=5, markdown_bytes=500,
         markdown_state="capturing")
    template = run(app_env.db.get_run("tmpl"))
    run(app_env.db.copy_run_to_user(template, 2, "copy", as_sample=True))

    copy = run(app_env.db.get_run("copy"))
    assert copy.markdown_pages == 0
    assert copy.markdown_bytes == 0
    assert copy.markdown_state == "off"
    assert copy.is_sample


# ------------------------------------------------------------------ surfaces

COUNTER_H1 = "Know how much content you’re dealing with."


def counter_host(env):
    return {"host": env.surfaces.COUNTER.host}


def markdown_host(env):
    return {"host": env.surfaces.MARKDOWN.host}


def test_the_front_door_is_public(app_env):
    app_env.current["user"] = None
    res = app_env.client.get("/", headers=counter_host(app_env))
    assert res.status_code == 200, "anonymous visitors get a landing page, not a redirect"
    assert COUNTER_H1 in res.text
    assert app_env.client.get("/login").status_code == 200, "the health check depends on this"


def test_each_hostname_gets_its_own_identity(app_env):
    app_env.current["user"] = None
    counter = app_env.client.get("/", headers=counter_host(app_env)).text
    markdown = app_env.client.get("/", headers=markdown_host(app_env)).text

    assert app_env.surfaces.MARKDOWN.headline in markdown
    assert COUNTER_H1 in counter and COUNTER_H1 not in markdown
    assert "og-markdown.png" in markdown and "og-counter.png" not in markdown
    assert "og-counter.png" in counter and "og-markdown.png" not in counter
    for body, surface in ((counter, app_env.surfaces.COUNTER), (markdown, app_env.surfaces.MARKDOWN)):
        assert f'rel="canonical" href="https://{surface.host}/"' in body
        assert surface.description in body


@pytest.mark.parametrize("host", ["x-abc.onrender.com", "localhost:8000", "", "unknown.example"])
def test_unknown_hosts_fall_back_rather_than_error(app_env, host):
    app_env.current["user"] = None
    res = app_env.client.get("/", headers={"host": host} if host else {})
    assert res.status_code == 200
    assert COUNTER_H1 in res.text


def test_old_hostnames_are_still_served_as_their_product(app_env):
    """Before the move to wxrks.com is finished, the wxrks.app names keep working
    as they did — same product, no redirect to a hostname that may not resolve."""
    app_env.current["user"] = None
    assert not app_env.surfaces.REDIRECT_OLD_HOSTS, "off unless switched on"
    for old, surface in (("markdown.wxrks.app", app_env.surfaces.MARKDOWN),
                         ("wordcount.wxrks.app", app_env.surfaces.COUNTER),
                         ("wordcounter.wxrks.app", app_env.surfaces.COUNTER)):
        res = app_env.client.get("/", headers={"host": old}, follow_redirects=False)
        assert res.status_code == 200, old
        assert surface.headline in res.text or (surface is app_env.surfaces.COUNTER and COUNTER_H1 in res.text)
        assert f'rel="canonical" href="https://{surface.host}/"' in res.text, "search engines are told the new name"


def test_old_hostnames_redirect_once_switched_on(app_env, monkeypatch):
    monkeypatch.setattr(app_env.surfaces, "REDIRECT_OLD_HOSTS", True)
    counter, markdown = app_env.surfaces.COUNTER.host, app_env.surfaces.MARKDOWN.host

    res = app_env.client.get("/de/articles?ref=email", headers={"host": "wordcount.wxrks.app"}, follow_redirects=False)
    assert res.status_code == 301
    assert res.headers["location"] == f"https://{counter}/de/articles?ref=email", "same path, language and query"
    res = app_env.client.head("/share/r", headers={"host": "markdown.wxrks.app"}, follow_redirects=False)
    assert (res.status_code, res.headers["location"]) == (301, f"https://{markdown}/share/r")

    # What can't follow a redirect is answered where it arrives.
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    res = app_env.client.post("/mcp", json=rpc, headers={"host": "wordcount.wxrks.app"}, follow_redirects=False)
    assert res.status_code == 200 and "count_words" in res.text, "assistants connected to the old URL keep working"
    res = app_env.client.get("/mcp", headers={"host": "wordcount.wxrks.app", "accept": "text/event-stream"},
                             follow_redirects=False)
    assert res.status_code == 405, "an MCP client opening a stream is told no, not sent elsewhere"

    assert app_env.client.get("/", headers=counter_host(app_env), follow_redirects=False).status_code == 200


def test_a_current_hostname_is_never_redirected_as_old(monkeypatch):
    """While the deploy still sets COUNTER_HOST to a wxrks.app name, that name is
    the product's address — even with redirects on and it in the old list."""
    import importlib

    import app.surfaces as surfaces
    monkeypatch.setenv("COUNTER_HOST", "wordcount.wxrks.app")
    monkeypatch.setenv("REDIRECT_OLD_HOSTS", "true")
    try:
        importlib.reload(surfaces)
        assert surfaces.moved_from("wordcount.wxrks.app") is None
        assert surfaces.for_host("wordcount.wxrks.app") is surfaces.COUNTER
        assert surfaces.moved_from("wordcounter.wxrks.app") is surfaces.COUNTER
        assert surfaces.moved_from("markdown.wxrks.app") is surfaces.MARKDOWN
        assert surfaces.moved_from("unknown.example") is None
    finally:
        monkeypatch.undo()
        importlib.reload(surfaces)


def test_the_markdown_door_pre_ticks_the_checkbox(app_env):
    assert 'id="capture-markdown" checked' in app_env.client.get(
        "/", headers=markdown_host(app_env)).text
    assert 'id="capture-markdown">' in app_env.client.get(
        "/", headers=counter_host(app_env)).text


def test_signed_in_users_still_get_the_app(app_env):
    body = app_env.client.get("/", headers=counter_host(app_env)).text
    assert 'id="crawl-form"' in body
    assert app_env.surfaces.COUNTER.headline not in body, "landing copy must not leak into the app"


def test_reports_are_kept_out_of_search_results(app_env):
    """A shared report is somebody's data on a public URL."""
    save(app_env)
    assert 'content="noindex' in app_env.client.get("/crawl/r").text
    run(app_env.db.set_run_public("r", True))
    assert 'content="noindex' in app_env.client.get("/share/r").text


def test_the_demo_link_only_appears_when_it_works(app_env):
    app_env.current["user"] = None
    save(app_env, run_id="demo")
    run(app_env.db.set_run_public("demo", True))
    assert "/share/demo" in app_env.client.get("/", headers=counter_host(app_env)).text

    run(app_env.db.set_run_public("demo", False))
    assert "/share/demo" not in app_env.client.get("/", headers=counter_host(app_env)).text


def test_the_surface_is_recorded_on_the_run(app_env):
    """The finished-crawl email is sent from a background task with no request,
    so the front door has to be persisted with the run."""
    save(app_env, surface="markdown")
    assert run(app_env.db.get_run("r")).surface == "markdown"
    assert run(app_env.db.get_run("r")).surface != app_env.surfaces.COUNTER.key


# ------------------------------------------------------------------- billing

def test_billing_routes_do_not_exist_when_stripe_is_unconfigured(app_env):
    """With no key the whole feature is off, and 404 rather than 503 because
    the UI never links to it either — same shape as email with no Mailgun key."""
    assert app_env.client.get("/pricing").status_code == 404
    assert app_env.client.post("/billing/checkout").status_code == 404
    assert app_env.client.post("/stripe/webhook", content=b"{}").status_code == 404


def test_the_upgrade_prompt_only_shows_when_there_is_something_to_buy(app_env, monkeypatch):
    from app import billing

    assert 'href="/pricing"' not in app_env.client.get("/").text

    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")
    assert 'href="/pricing"' in app_env.client.get("/").text


def test_a_pro_account_is_offered_billing_not_an_upgrade(app_env, monkeypatch):
    from app import billing

    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")
    app_env.current["user"] = app_env.pro
    body = app_env.client.get("/").text
    assert "plan-pill" in body, "a paying account should be marked as such"
    assert 'href="/pricing"' not in body, "and not asked to upgrade again"


def test_the_pricing_page_never_promises_more_than_was_measured(app_env, monkeypatch):
    """Guards the number itself. Pro fetches 4x the pages but measured 2.8x, and
    the page must quote the smaller one — see plans.SCALING_EFFICIENCY."""
    from app import billing, plans

    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")
    body = app_env.client.get("/pricing").text
    raw = round(plans.CONCURRENCY_PRO / plans.CONCURRENCY_FREE)
    assert f"{plans.advertised_speedup()}× faster" in body
    assert plans.advertised_speedup() < raw


def test_a_webhook_without_a_valid_signature_is_rejected(app_env, monkeypatch):
    """The plan is only ever written from a signature-verified event — a browser
    can reach success_url without having paid."""
    from app import billing

    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(billing, "STRIPE_WEBHOOK_SECRET", "whsec_test")
    res = app_env.client.post(
        "/stripe/webhook",
        content=b'{"type":"customer.subscription.updated"}',
        headers={"stripe-signature": "t=1,v1=forged"},
    )
    assert res.status_code == 400


def test_the_report_carries_the_exclusions_it_was_run_with(app_env):
    """"Settings applied" has to be able to state what was left out. Without the
    value reaching the page there is no way to tell, after the fact, whether a
    missing subdomain was excluded on purpose or simply never found."""
    save(app_env, exclusions="web-staging, /careers")
    body = app_env.client.get("/crawl/r").text
    assert 'exclusions: "web-staging, /careers"' in body
    assert 'id="exclusions-pill"' in body
    assert 'id="exclusions-pill" style="display:none;"' not in body, (
        "the pill is always rendered — 'Excluded: none' is a different "
        "statement from the pill being absent"
    )


def test_a_run_with_no_exclusions_still_reports_that(app_env):
    save(app_env)
    body = app_env.client.get("/crawl/r").text
    assert "exclusions: null" in body
    assert 'id="exclusions-pill"' in body


# -------------------------------------------------------------------- promos

def test_both_the_shared_report_and_the_owners_pitch_wxrks(app_env):
    """The shared link reaches people who aren't users at all, which is the more
    valuable half — but the owner sees it too, or the one person checking this
    app daily never lays eyes on it."""
    save(app_env, total_words=662_000)
    run(app_env.db.set_run_public("r", True))

    for url in ("/share/r", "/crawl/r"):
        body = app_env.client.get(url, headers=counter_host(app_env)).text
        assert "wxrks.com" in body, url
        assert "words to translate" in body, url
    # Whether each is dismissible differs by audience — see the test below.


def test_the_markdown_surface_is_not_pitched_translation(app_env):
    save(app_env, total_words=662_000, surface="markdown")
    run(app_env.db.set_run_public("r", True))
    body = app_env.client.get("/share/r", headers=markdown_host(app_env)).text
    assert "words to translate" not in body


def test_the_pro_banner_needs_a_crawl_worth_complaining_about(app_env, monkeypatch):
    from app import billing

    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")

    save(app_env, duration_seconds=90, crawl_concurrency=4)
    assert "On Pro it would have taken" not in app_env.client.get("/crawl/r").text

    save(app_env, duration_seconds=3 * 3600, crawl_concurrency=4)
    body = app_env.client.get("/crawl/r").text
    assert "This crawl took ~3h" in body
    assert "/pricing" in body


def test_no_pro_banner_when_billing_is_off(app_env):
    save(app_env, duration_seconds=3 * 3600, crawl_concurrency=4)
    assert "On Pro it would have taken" not in app_env.client.get("/crawl/r").text


def test_the_pricing_page_is_readable_without_an_account(app_env, monkeypatch):
    """It exists to convince people to sign up; requiring them to sign up first
    made every link to it from outside the app a dead end."""
    from app import billing

    app_env.current["user"] = None
    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")
    res = app_env.client.get("/pricing")
    assert res.status_code == 200
    assert "Sign in to upgrade" in res.text


# -------------------------------------------------------------------- health

def test_the_health_page_is_admin_only(app_env, monkeypatch):
    """404 rather than 403, matching require_admin — a non-admin shouldn't be
    able to tell the page exists."""
    monkeypatch.setenv("ADMIN_EMAILS", "nobody@x.c")
    assert app_env.client.get("/admin/health").status_code == 404
    assert app_env.client.get("/admin/health.json").status_code == 404


def test_the_health_page_renders_for_an_admin(app_env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    res = app_env.client.get("/admin/health")
    assert res.status_code == 200
    for panel in ("Right now", "Disk", "Recent stops", "Disk by user"):
        assert panel in res.text


def test_the_health_page_survives_an_empty_database(app_env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    assert app_env.client.get("/admin/health").status_code == 200


def test_health_json_reports_the_real_limits(app_env, monkeypatch):
    from app import plans
    from app.crawler import MAX_CONCURRENT_CRAWLS

    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    body = app_env.client.get("/admin/health.json").json()
    assert body["live"]["max_concurrent_crawls"] == MAX_CONCURRENT_CRAWLS
    assert body["live"]["page_budget"] == plans.PAGE_BUDGET


def test_a_memory_kill_is_visible_afterwards(app_env, monkeypatch):
    """The whole point: before stop_kind, this was indistinguishable from
    somebody clicking Cancel."""
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    save(app_env, status="cancelled", stop_kind="memory",
         stopped_reason="Stopped automatically — too much memory.")
    body = app_env.client.get("/admin/health").text
    assert "memory" in body
    assert "too much memory" in body


# ----------------------------------------------------------------- user menu

ADMIN_MENU_ITEMS = ("Server health", "Running crawls", "All runs", "Estimate accuracy", "Translation check")


def test_the_menu_offers_admins_every_admin_page(app_env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    body = app_env.client.get("/").text
    for item in ADMIN_MENU_ITEMS:
        assert f">{item}<" in body


def test_the_menu_hides_the_admin_pages_from_everyone_else(app_env, monkeypatch):
    """The routes 404 for a non-admin anyway; the menu must not advertise that
    they exist, which is the same reason require_admin returns 404 not 403."""
    monkeypatch.setenv("ADMIN_EMAILS", "someone-else@x.c")
    body = app_env.client.get("/").text
    for item in ADMIN_MENU_ITEMS:
        assert f">{item}<" not in body


def test_the_menu_lists_only_pages_that_actually_open(app_env, monkeypatch):
    """It's built from the same is_admin() require_admin uses, so a listed page
    can never 404 — a menu that lies is worse than no menu."""
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    assert ">Server health<" in app_env.client.get("/").text
    for url in ("/admin/health", "/admin/jobs", "/admin/runs", "/admin/estimates", "/admin/translations"):
        assert app_env.client.get(url).status_code == 200


def test_every_admin_page_carries_the_menu_back_out(app_env, monkeypatch):
    """Admin pages had no chip at all, so they were a dead end."""
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    for url in ("/admin/health", "/admin/jobs", "/admin/runs", "/admin/estimates", "/admin/translations"):
        assert 'id="user-menu-list"' in app_env.client.get(url).text, url


def test_the_menu_follows_the_billing_state(app_env, monkeypatch):
    from app import billing

    assert ">Upgrade to Pro<" not in app_env.client.get("/").text, "nothing to sell yet"

    monkeypatch.setattr(billing, "STRIPE_SECRET_KEY", "sk_test_x")
    assert ">Upgrade to Pro<" in app_env.client.get("/").text

    app_env.current["user"] = app_env.pro
    body = app_env.client.get("/").text
    assert ">Manage billing<" in body
    assert ">Upgrade to Pro<" not in body


def test_preview_is_admin_only(app_env, monkeypatch):
    """Force-rendering banners is a diagnostic, not something a user can turn on
    for themselves."""
    monkeypatch.setenv("ADMIN_EMAILS", "someone-else@x.c")
    save(app_env, total_words=200, duration_seconds=0)
    body = app_env.client.get("/crawl/r?preview=promos").text
    assert "promo-preview" not in body
    assert "promo-pro" not in body


def test_preview_shows_an_admin_both_banners_with_stripe_off(app_env, monkeypatch):
    """The Pro banner is otherwise unviewable until billing is switched on."""
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    save(app_env, total_words=200, duration_seconds=0, crawl_concurrency=0)
    body = app_env.client.get("/crawl/r?preview=promos", headers=counter_host(app_env)).text
    assert "promo-pro" in body
    assert "promo-wxrks" in body
    assert body.count("promo-preview") == 2, "both must be labelled as previews"


def test_a_shared_report_cannot_be_previewed_into(app_env, monkeypatch):
    """No user on that route means is_admin is False, so the parameter is inert
    — worth pinning, because a leak there would be public."""
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    save(app_env, total_words=200, duration_seconds=0)
    run(app_env.db.set_run_public("r", True))
    assert "promo-preview" not in app_env.client.get("/share/r?preview=promos").text


def test_the_home_page_pitches_the_last_crawl_worth_translating(app_env):
    save(app_env, total_words=662_000)
    body = app_env.client.get("/", headers=counter_host(app_env)).text
    assert "wxrks.com" in body
    assert "Your last crawl of" in body


def test_the_home_page_says_nothing_before_a_first_crawl(app_env):
    body = app_env.client.get("/", headers=counter_host(app_env)).text
    # The app itself lives on a wxrks.com subdomain; the pitch links wxrks.com.
    assert "//wxrks.com" not in body and "Your last crawl of" not in body


def test_the_offer_cannot_be_dismissed(app_env):
    """Decided: the offer stays on every report, for the owner and for anyone
    they share it with. Nothing client-side hides it, which also means the one
    person checking whether it works can't accidentally silence it."""
    save(app_env, total_words=662_000)
    run(app_env.db.set_run_public("r", True))

    for url in ("/crawl/r", "/share/r", "/"):
        body = app_env.client.get(url, headers=counter_host(app_env)).text
        assert "promo-wxrks" in body, url
        assert "promo-dismiss" not in body, url
        assert "data-promo-dismiss" not in body, url


# ------------------------------------------------------------- report header

def test_the_report_header_carries_the_topbar(app_env):
    """The report page never had one, which is why the language menu and the
    account chip vanished the moment a crawl was opened."""
    save(app_env)
    html = app_env.client.get("/crawl/r", headers=counter_host(app_env)).text
    assert 'class="lang-menu"' in html
    assert "user-chip" in html


def test_exports_fold_into_one_menu_with_their_old_ids(app_env):
    """Option A: the ids inside are unchanged, so the existing JS handlers bind
    without knowing the markup moved."""
    save(app_env)
    html = app_env.client.get("/crawl/r", headers=counter_host(app_env)).text
    for el_id in ("export-csv-btn", "markdown-btn", "print-btn"):
        assert html.count(f'id="{el_id}"') == 1, el_id
    assert "action-menu-list" in html


def test_delete_and_recrawl_live_behind_the_dots(app_env):
    save(app_env)
    html = app_env.client.get("/crawl/r", headers=counter_host(app_env)).text
    assert html.count('id="delete-btn"') == 1, "a duplicate id binds the handler to the wrong one"
    assert 'id="recrawl-open-btn"' in html
    assert "run-disclosure-headless" in html, "the form stays full-width below, opened from the menu"


def test_a_shared_report_gets_no_owner_actions_but_keeps_its_exports(app_env):
    save(app_env)
    run(app_env.db.set_run_public("r", True))
    app_env.current["user"] = None
    del app_env.main.app.dependency_overrides[
        __import__("app.auth", fromlist=["get_current_user"]).get_current_user
    ]
    html = app_env.client.get("/share/r", headers=counter_host(app_env)).text
    assert "action-dots" not in html
    assert "recrawl-disclosure" not in html, "an anonymous re-crawl has no account to belong to"
    assert "export-csv-btn" in html, "exports are part of the report being shared"
    assert 'class="lang-menu"' in html


def test_the_menu_bindings_run_before_the_finished_report_returns(app_env):
    """initCrawlPage returns early for a finished report — and Re-crawl is
    precisely a finished-report action. Binding it after that return is how the
    menu item shipped doing nothing; this pins the order structurally."""
    js = (Path(__file__).resolve().parents[1] / "app/static/app.js").read_text()
    branch = js.index('if (opts.mode === "past" || opts.mode === "shared")')
    assert js.index('openFromMenu("recrawl-open-btn"') < branch
    assert js.index('openFromMenu("page-issues-open-btn"') < branch


def test_the_blocked_list_lives_behind_the_dots_too(app_env):
    save(app_env)
    html = app_env.client.get("/crawl/r", headers=counter_host(app_env)).text
    assert 'id="page-issues-open-btn"' in html
    assert html.count("run-disclosure-headless") == 2, "issues and re-crawl both open from the menu"


# A real 32-character id, not "r": a one-character id once let a string be
# passed where a list of ids was expected and still match — the string was
# iterated into ['r'] — while every real run id became 32 one-letter ids.
RID = "624d7441a225418887b709b1445c8795"


def test_an_admin_can_correct_a_runs_platform(app_env, monkeypatch):
    """Detection reads only the pages a crawl reached, so it can miss — and a
    run behind a password can't be re-probed. The banner follows the fix."""
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    save(app_env, run_id=RID, total_words=115_194)  # big enough to be pitched at all
    assert "Drupal connector" not in app_env.client.get(f"/crawl/{RID}").text

    res = app_env.client.post(
        f"/admin/runs/{RID}/platform", data={"cms": "Drupal", "back": "/admin/runs?q=x.com"}, follow_redirects=False
    )
    assert res.status_code == 303 and res.headers["location"] == "/admin/runs?q=x.com"
    assert run(app_env.db.get_run(RID)).detected_cms == "Drupal"
    assert "Drupal connector" in app_env.client.get(f"/crawl/{RID}").text

    app_env.client.post(f"/admin/runs/{RID}/platform", data={"cms": ""})
    assert run(app_env.db.get_run(RID)).detected_cms is None


def test_the_platform_override_is_admin_only_and_checked(app_env, monkeypatch):
    save(app_env, run_id=RID)
    monkeypatch.setenv("ADMIN_EMAILS", "nobody@x.c")
    assert app_env.client.post(f"/admin/runs/{RID}/platform", data={"cms": "Drupal"}).status_code == 404

    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    assert app_env.client.post(f"/admin/runs/{RID}/platform", data={"cms": "Joomla"}).status_code == 400
    res = app_env.client.post(
        f"/admin/runs/{RID}/platform", data={"cms": "Drupal", "back": "https://evil.example/"}, follow_redirects=False
    )
    assert res.headers["location"] == "/admin/runs"


# ----------------------------------------------------------- the front page

# The session cookie is Secure, as it is in production, so anything that needs
# it to survive between two requests has to be made over https.
HTTPS = "https://testserver"

@pytest.fixture()
def no_network(app_env, monkeypatch):
    """The front page's crawl handoff, without a real crawl or a real probe."""
    started = []

    async def fake_run_crawl(*args, **kwargs):
        started.append(args)

    async def open_site(url, auth=None):
        return "ok"

    monkeypatch.setattr(app_env.main, "run_crawl", fake_run_crawl)
    monkeypatch.setattr(app_env.main.site_auth, "probe", open_site)
    return started


def test_the_front_page_leads_with_the_form(app_env):
    app_env.current["user"] = None
    body = app_env.client.get("/", headers=counter_host(app_env)).text
    assert 'action="/start"' in body
    assert body.index('action="/start"') < body.index("A website has 200 pages"), \
        "the explanation comes after the form, not before it"
    assert "Before you count" in body, "the limits are stated, not just the features"
    assert "Google" in body


def test_a_signed_out_visitor_starts_crawling_straight_away(app_env, no_network):
    """No sign-in before the crawl: the guest's crawl runs to the estimate."""
    app_env.current["user"] = None
    res = app_env.client.post(HTTPS + "/start", data={"url": "wxrks.com"}, follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"].startswith("/crawl/")
    job = app_env.main.get_job(res.headers["location"].rsplit("/", 1)[-1])
    assert job.guest and job.source_url == app_env.db.normalize_url("https://wxrks.com")
    assert no_network, "the crawl started"

    # The guest can watch their own crawl...
    assert app_env.client.get(HTTPS + res.headers["location"]).status_code == 200
    # ...and a different browser can't.
    other = app_env.client.__class__(app_env.main.app)
    assert other.get(HTTPS + res.headers["location"], follow_redirects=False).headers["location"] == "/login"
    assert other.get(HTTPS + f"/events/{job.id}").status_code == 401

    # One at a time: starting again goes back to the running crawl.
    again = app_env.client.post(HTTPS + "/start", data={"url": "anthropic.com"}, follow_redirects=False)
    assert again.headers["location"] == res.headers["location"]


def test_signing_in_hands_the_guest_crawl_over(app_env, no_network):
    app_env.current["user"] = None
    res = app_env.client.post(HTTPS + "/start", data={"url": "wxrks.com"}, follow_redirects=False)
    job = app_env.main.get_job(res.headers["location"].rsplit("/", 1)[-1])
    guest_id = job.user_id
    run(app_env.db.save_run(job.id, job.source_url, guest_id, "paused", 50_000, [], False))

    from app.auth import claim_guest_runs
    run(claim_guest_runs(guest_id, app_env.owner.id))
    assert job.user_id == app_env.owner.id and not job.guest
    assert run(app_env.db.get_run(job.id)).user_id == app_env.owner.id

    # "Sign in with Google to continue" lands here and picks the crawl up.
    job.status = "paused"
    app_env.current["user"] = app_env.owner
    started_before = len(no_network)
    res = app_env.client.get(HTTPS + f"/crawl/{job.id}/continue", follow_redirects=False)
    assert res.headers["location"] == f"/crawl/{job.id}"
    assert len(no_network) == started_before + 1, "the paused crawl resumed"


def test_a_guest_cannot_resume_past_the_estimate_without_signing_in(app_env, no_network):
    app_env.current["user"] = None
    res = app_env.client.post(HTTPS + "/start", data={"url": "wxrks.com"}, follow_redirects=False)
    job = app_env.main.get_job(res.headers["location"].rsplit("/", 1)[-1])
    job.status = "paused"
    page = app_env.client.get(HTTPS + f"/crawl/{job.id}").text
    assert "Sign in with Google to continue" in page
    assert "%2Fcontinue" in page, "the button resumes this crawl after sign-in"
    started_before = len(no_network)
    # 401 from the sign-in requirement in production; 403 from the guest check
    # behind it when a test lets the request through. Either way, refused.
    assert app_env.client.post(HTTPS + f"/crawl/{job.id}/resume", json={}).status_code in (401, 403)
    assert len(no_network) == started_before


def test_guests_are_limited_and_then_asked_to_sign_in(app_env, no_network, monkeypatch):
    monkeypatch.setattr(app_env.main, "GUEST_CRAWLS_PER_DAY", 1)
    app_env.current["user"] = None
    first = app_env.client.post(HTTPS + "/start", data={"url": "wxrks.com"}, follow_redirects=False)
    app_env.main.get_job(first.headers["location"].rsplit("/", 1)[-1]).status = "completed"
    second = app_env.client.post(HTTPS + "/start", data={"url": "anthropic.com"}, follow_redirects=False)
    assert second.headers["location"] == "/auth/login"

    # Signed back in, the crawl they asked for starts.
    app_env.current["user"] = app_env.owner
    res = app_env.client.get(HTTPS + "/start", follow_redirects=False)
    assert res.headers["location"].startswith("/crawl/")
    assert app_env.main.get_job(res.headers["location"].rsplit("/", 1)[-1]).source_url.startswith("https://anthropic.com")


def test_a_public_report_is_shown_instead_of_crawling_again(app_env, no_network):
    save(app_env, run_id=RID, source_url=app_env.db.normalize_url("https://wxrks.com"))
    run(app_env.db.set_run_public(RID, True))
    app_env.current["user"] = None
    res = app_env.client.post(HTTPS + "/start", data={"url": "wxrks.com"}, follow_redirects=False)
    assert res.headers["location"] == f"/share/{RID}" and not no_network


def test_a_front_page_crawl_that_cannot_start_hands_over_to_the_full_form(app_env, no_network, monkeypatch):
    async def protected(url, auth=None):
        return "needs_auth"

    monkeypatch.setattr(app_env.main.site_auth, "probe", protected)
    app_env.current["user"] = None
    res = app_env.client.post(HTTPS + "/start", data={"url": "https://stg.example.com"}, follow_redirects=False)
    assert res.headers["location"] == "/auth/login", "a protected site needs an account"
    app_env.current["user"] = app_env.owner
    location = app_env.client.get(HTTPS + "/start", follow_redirects=False).headers["location"]
    assert location == "/?url=https%3A%2F%2Fstg.example.com&code=site_password_needed"


def test_unclaimed_guest_crawls_are_swept(app_env, no_network, monkeypatch):
    app_env.current["user"] = None
    res = app_env.client.post(HTTPS + "/start", data={"url": "wxrks.com"}, follow_redirects=False)
    job = app_env.main.get_job(res.headers["location"].rsplit("/", 1)[-1])
    job.status = "completed"
    run(app_env.db.save_run(job.id, job.source_url, job.user_id, "completed", 900, [], False))
    monkeypatch.setattr(app_env.main, "GUEST_RETENTION_DAYS", -1)  # everything counts as old
    run(app_env.main._sweep_guest_runs())
    assert run(app_env.db.get_run(job.id)) is None
    assert run(app_env.db.get_user(job.user_id)) is None, "the empty guest goes too"


def test_a_bad_front_page_url_comes_back_with_an_error(app_env):
    app_env.current["user"] = None
    res = app_env.client.post("/start", data={"url": "not a site"}, follow_redirects=False)
    assert res.status_code == 303 and "error=url" in res.headers["location"]
    assert "Please enter a valid http(s) URL" in app_env.client.get(res.headers["location"]).text


def test_after_sign_in_only_goes_to_internal_paths():
    from app.auth import after_login_path

    assert after_login_path("/start") == "/start"
    assert after_login_path("/de/start") == "/de/start"
    for hostile in ("//evil.example", "https://evil.example", "/\\evil.example", ""):
        assert after_login_path(hostile) == "/", hostile


def test_a_featured_run_is_an_example_on_the_front_page(app_env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    save(app_env, run_id=RID, total_words=412_000)
    app_env.client.post(f"/admin/runs/{RID}/example", data={"on": "1"})
    assert run(app_env.db.get_run(RID)).is_public, "the front page links to its public report"

    app_env.current["user"] = None
    body = app_env.client.get("/", headers=counter_host(app_env)).text
    assert f"/share/{RID}" in body and "412,000 words" in body

    app_env.current["user"] = app_env.owner
    app_env.client.post(f"/admin/runs/{RID}/example", data={"on": "0"})
    app_env.current["user"] = None
    assert f"/share/{RID}" not in app_env.client.get("/", headers=counter_host(app_env)).text


def test_an_account_with_only_the_sample_run_still_gets_the_explanation(app_env):
    save(app_env, run_id="template", source_url="https://wxrks.com", user_id=2)
    template = run(app_env.db.get_run("template"))
    run(app_env.db.copy_run_to_user(template, app_env.owner.id, "sample1", as_sample=True))
    intro = "A website has 200 pages"
    assert intro in app_env.client.get("/", headers=counter_host(app_env)).text

    save(app_env, run_id="mine")
    assert intro not in app_env.client.get("/", headers=counter_host(app_env)).text


def test_the_hero_scene_is_optional_and_served_compressed(app_env):
    """The WebGL bundle is ~570 KB; it must arrive gzipped, and the page must
    stand on its own without it (it only loads after first paint)."""
    app_env.current["user"] = None
    body = app_env.client.get("/", headers=counter_host(app_env)).text
    assert 'id="home-webgl"' in body and "hero-webgl.js" in body
    assert body.index('action="/start"') < body.index('id="home-webgl"'), "the form doesn't wait for the scene"

    res = app_env.client.get("/static/hero-webgl.js", headers={"accept-encoding": "gzip"})
    assert res.status_code == 200 and res.headers.get("content-encoding") == "gzip"
    assert "SPDX-License-Identifier: MIT" in res.text, "three.js's licence travels with it"


def test_the_front_page_answers_speed_and_bot_protection(app_env):
    app_env.current["user"] = None
    body = app_env.client.get("/", headers=counter_host(app_env)).text
    assert "Waits for each page to finish loading" in body
    assert "Why does a crawl take a while?" in body and "What if the site blocks bots?" in body


def test_reports_say_why_a_crawl_took_as_long_as_it_did(app_env):
    save(app_env, run_id="timed", duration_seconds=840)
    body = app_env.client.get("/crawl/timed").text
    assert "Took ~14 min" in body and "Why it took this long" in body

    save(app_env, run_id="untimed")  # older runs recorded no duration
    assert "Why it took this long" not in app_env.client.get("/crawl/untimed").text

    job = app_env.main.create_job(source_url="https://x.com", user_id=app_env.owner.id, max_pages=10)
    job.status = "crawling"
    assert "Seems slow? Here’s why" in app_env.client.get(f"/crawl/{job.id}").text


def test_static_assets_cache_only_under_their_own_version(app_env):
    """A deploy's old container can be asked for the new version: that answer
    must never be cached, or the page keeps last deploy's styles."""
    from app.templates import ASSET_VERSION

    current = app_env.client.get(f"/static/style.css?v={ASSET_VERSION}")
    assert current.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert app_env.client.get("/static/style.css?v=someothervers").headers["cache-control"] == "no-store"
    assert "cache-control" not in app_env.client.get("/static/brand/logo.svg").headers


def test_the_front_page_names_the_platforms_it_works_with(app_env):
    app_env.current["user"] = None
    body = app_env.client.get("/", headers=counter_host(app_env)).text
    assert "Works with any website" in body
    for name in ("WordPress", "Webflow", "Next.js", "Replit"):
        assert name in body, name
