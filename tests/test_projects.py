"""Projects: one client's crawls together, each page and each sentence once.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_app_flows import app_env, run, save  # noqa: E402,F401

from app.models import PageResult, RunRecord  # noqa: E402
from app.projects import summarize_project, url_key  # noqa: E402


def page(url: str, words: int, digest: str | None = None, success: bool = True) -> PageResult:
    return PageResult(url=url, word_count=words, content_hash=digest, success=success)


def record(run_id: str, source: str, created: str, pages: list[PageResult], status: str = "completed") -> RunRecord:
    return RunRecord(id=run_id, source_url=source, user_id=1, created_at=created, status=status,
                     total_words=sum(p.word_count for p in pages if p.success), page_count=len(pages),
                     limit_reached=False, pages=pages)


LIVE = record("live", "https://www.resort.test", "2026-09-29T10:00:00", [
    page("https://www.resort.test/", 100, "home"),
    page("https://www.resort.test/rooms", 200, "rooms-v1"),
    page("https://www.resort.test/legal", 50, "legal"),
])
STAGING = record("stg", "https://stg.resort.test", "2026-09-30T10:00:00", [
    page("https://stg.resort.test/", 100, "home"),
    page("https://stg.resort.test/rooms", 210, "rooms-v2"),
    page("https://stg.resort.test/new-offer", 40, "offer"),
])
BRAND = record("brand", "https://brand.test", "2026-10-02T10:00:00", [
    page("https://brand.test/legal", 50, "legal"),
    page("https://brand.test/rooms", 300, "brand-rooms"),
])


# ------------------------------------------------------------------ addresses

@pytest.mark.parametrize("url, key", [
    ("https://x.test/", "/"),
    ("https://x.test", "/"),
    ("https://x.test/rooms/", "/rooms"),
    ("https://x.test/rooms/index.html", "/rooms"),
    ("https://x.test/a?utm_source=news&b=2&a=1", "/a?a=1&b=2"),
    ("https://x.test/de/rooms", "/de/rooms"),
])
def test_an_address_is_its_path_and_query(url, key):
    assert url_key(url) == key


# ------------------------------------------------------------------ the arithmetic

def test_each_page_counts_once_across_the_crawls():
    totals = summarize_project([LIVE, STAGING, BRAND])["totals"]
    # home once, rooms once (the latest text), legal once, the new offer, the brand's own rooms
    assert totals["words"] == 100 + 210 + 50 + 40 + 300
    assert totals["added_up"] == 350 + 350 + 350
    assert totals["overlap_words"] == totals["added_up"] - totals["words"]
    assert totals["shared_pages"] == 3


def test_staging_and_live_match_by_address_and_the_latest_text_counts():
    summary = summarize_project([LIVE, STAGING])
    rooms = next(g for g in summary["groups"] if g["path"] == "/rooms")
    assert rooms["kind"] == "changed_text"
    assert rooms["words"] == 210 and rooms["counted_from"] == "stg"


def test_two_different_sites_only_match_by_text():
    summary = summarize_project([LIVE, BRAND])
    paths = {g["path"]: g for g in summary["groups"]}
    assert paths["/legal"]["kind"] == "same_text", "the same legal text on both sites"
    assert "/rooms" not in paths, "the same path, different pages"


def test_the_same_text_under_another_address_is_the_same_page():
    moved = record("moved", "https://stg.resort.test", "2026-10-05T10:00:00",
                   [page("https://stg.resort.test/stays/rooms", 200, "rooms-v1")])
    summary = summarize_project([LIVE, moved])
    group = summary["groups"][0]
    assert group["moved"] and group["kind"] == "same_text"


def test_only_the_latest_crawl_of_a_site_counts():
    older = record("old", "https://www.resort.test", "2026-09-12T10:00:00",
                   [page("https://www.resort.test/", 90, "home-old")])
    summary = summarize_project([LIVE, older])
    assert summary["states"] == {"live": "counted", "old": "superseded"}
    assert summary["totals"]["words"] == 350


def test_a_crawl_that_didnt_finish_fills_the_gaps_and_one_still_running_waits():
    crawling = record("now", "https://sister-brand.test", "2026-10-14T10:00:00",
                      [page("https://sister-brand.test/", 500, "a")], status="crawling")
    failed = record("bad", "https://cassia.test", "2026-10-13T10:00:00",
                    [page("https://cassia.test/", 70, "c")], status="failed")
    summary = summarize_project([LIVE, crawling, failed])
    assert summary["states"] == {"live": "counted", "now": "in_progress", "bad": "gap_filler"}
    assert summary["walk_order"] == ["live", "bad"], "finished crawls first, whatever their dates"
    assert summary["totals"]["words"] == 350 + 70 and summary["totals"]["in_progress"] == 1


def test_a_cancelled_recrawl_never_replaces_the_completed_crawl_of_its_site():
    retry = record("retry", "https://www.resort.test", "2026-10-20T10:00:00",
                   [page("https://www.resort.test/", 100, "home"), page("https://www.resort.test/new", 30, "new")],
                   status="cancelled")
    older_try = record("first-try", "https://www.resort.test", "2026-09-01T10:00:00",
                       [page("https://www.resort.test/", 90, "home-0")], status="cancelled")
    summary = summarize_project([LIVE, retry, older_try])
    assert summary["states"] == {"live": "counted", "retry": "gap_filler", "first-try": "superseded"}
    assert summary["totals"]["words"] == 350 + 30, "it only adds the page the completed crawl doesn't have"


def test_runs_from_before_fingerprints_match_by_address_only():
    old_live = record("ol", "https://www.resort.test", "2026-08-01T10:00:00", [page("https://www.resort.test/", 100)])
    old_stg = record("os", "https://stg.resort.test", "2026-08-02T10:00:00", [page("https://stg.resort.test/", 100)])
    summary = summarize_project([old_live, old_stg])
    assert summary["groups"][0]["kind"] == "address_only"
    assert summary["totals"]["words"] == 100


def test_failed_pages_count_once_in_the_left_out_numbers():
    a = record("a", "https://www.resort.test", "2026-09-01T10:00:00", [page("https://www.resort.test/x", 0, success=False)])
    b = record("b", "https://stg.resort.test", "2026-09-02T10:00:00", [page("https://stg.resort.test/x", 0, success=False)])
    assert summarize_project([a, b])["report"]["failed_count"] == 1


# ------------------------------------------------------------------ older runs, by their Markdown

def test_a_markdown_fingerprint_ignores_formatting():
    from app.projects import markdown_fingerprint

    a = markdown_fingerprint("# Rooms\n\nSea view, **king** bed. [Book](https://x.test/book) ![](img.png)")
    b = markdown_fingerprint("Rooms\nSea view king bed Book")
    assert a == b and a is not None
    assert markdown_fingerprint("") is None and markdown_fingerprint("![](only-an-image.png)") is None


def test_a_crawl_fingerprints_the_markdown_it_would_save_even_when_it_doesnt():
    from types import SimpleNamespace

    from app.crawler import _markdown_fingerprint
    from app.projects import markdown_fingerprint

    class Md(str):
        fit_markdown = "Main content, **only**."
        raw_markdown = "Menu Main content only. Footer"

    result = SimpleNamespace(url="https://x.test/", markdown=Md("Menu Main content only. Footer"))
    assert _markdown_fingerprint(result) == markdown_fingerprint("Main content only")
    assert _markdown_fingerprint(SimpleNamespace(url="https://x.test/", markdown=None)) is None


def test_two_sites_match_by_their_saved_markdown():
    shared = page("https://www.resort.test/legal", 50)
    shared.markdown_hash = "legal-md"
    other = page("https://brand.test/terms", 50)
    other.markdown_hash = "legal-md"
    a = record("a", "https://www.resort.test", "2026-09-30T10:00:00", [shared, page("https://www.resort.test/", 100)])
    b = record("b", "https://brand.test", "2026-09-30T11:00:00", [other])
    summary = summarize_project([a, b])
    assert summary["totals"]["shared_pages"] == 1 and summary["groups"][0]["kind"] == "same_text"


def test_older_runs_get_their_fingerprints_from_saved_markdown(app_env):
    from app import markdown_store

    legal = "Our terms apply to every stay, booking and gift card. " * 20
    live = [page("https://www.resort.test/", 100), page("https://www.resort.test/legal", 50)]
    brand = [page("https://brand.test/rooms", 300), page("https://brand.test/terms", 50)]
    texts = {"https://www.resort.test/": "Welcome to Acme", "https://www.resort.test/legal": legal,
             "https://brand.test/rooms": "Rooms with a view", "https://brand.test/terms": legal}
    for run_id, src, pages_ in (("live", "https://www.resort.test", live), ("brand", "https://brand.test", brand)):
        for p in pages_:
            markdown_store.write(run_id, p.url, texts[p.url])
        save(app_env, run_id=run_id, source_url=src, pages=pages_, total_words=sum(p.word_count for p in pages_),
             capture_markdown=True, markdown_pages=len(pages_))
    project_id = _project(app_env, run_ids=["live", "brand"])

    body = app_env.client.get(f"/projects/{project_id}").text
    assert "Checked by page only" not in body and "Estimated from saved Markdown" in body
    assert run(app_env.db.get_project(project_id))["cached_overlap_words"] == 50, "the terms, counted once"
    assert all(p.markdown_hash for p in run(app_env.db.get_run("live")).pages), "kept on the run"


def test_crawls_that_share_nothing_say_so(app_env):
    other = record("other", "https://other.test", "2026-10-01T10:00:00", [page("https://other.test/", 70, "other-home")])
    _save(app_env, "live", LIVE)
    _save(app_env, "other", other)
    project_id = _project(app_env, run_ids=["live", "other"])
    assert "No page or sentence is in more than one crawl." in app_env.client.get(f"/projects/{project_id}").text


def test_a_run_without_sentences_or_markdown_is_checked_by_page_only(app_env):
    save(app_env, run_id="old", source_url="https://www.resort.test", pages=[page("https://www.resort.test/", 100)],
         total_words=100)
    save(app_env, run_id="brand", source_url="https://brand.test", pages=[page("https://brand.test/", 80, "b")],
         total_words=80)
    project_id = _project(app_env, run_ids=["old", "brand"])
    body = app_env.client.get(f"/projects/{project_id}").text
    assert "Checked by page only: www.resort.test, brand.test." in body or \
        "Checked by page only: brand.test, www.resort.test." in body
    assert run(app_env.db.get_project(project_id))["cached_new_words"] == 180, "every page counts in full"


# ------------------------------------------------------------------ crawls that didn't finish

STG_SITE = record("stg", "https://stg.resort.test", "2026-09-30T10:00:00", [
    page("https://stg.resort.test/", 100, "s-home"),
    page("https://stg.resort.test/rooms", 200, "s-rooms"),
    page("https://stg.resort.test/offers", 50, "offers"),
])
# Signed in on the account site, then sent back to the staging site: the
# members area is new, the rest it had already crawled.
SIGNED_IN = record("acct", "https://stg-account.resort.test", "2026-10-14T11:24:00", [
    page("https://stg.resort.test/members", 80, "members"),
    page("https://stg.resort.test/rooms", 210, "a-rooms"),
    page("https://stg.resort.test/offers", 50, "offers"),
    page("https://stg.resort.test/broken", 0, success=False),
], status="cancelled")


def test_a_crawl_that_didnt_finish_is_consolidated_and_only_fills_the_gaps(app_env):
    _save(app_env, "stg", STG_SITE)
    _save(app_env, "acct", SIGNED_IN, status="cancelled")
    project_id = _project(app_env, run_ids=["stg", "acct"])
    result = run(app_env.db.get_consolidation(project_id))["result"]
    rows = {r["id"]: r for r in result["runs"]}
    assert result["order"] == ["stg", "acct"], "the completed crawl first, though it's older"
    assert rows["acct"]["state"] == "gap_filler"
    assert rows["acct"]["same_page_words"] == 260 and rows["acct"]["new_words"] == 80, "only the members page"
    assert rows["stg"]["new_words"] == 350
    assert result["totals"]["new_words"] == 430

    body = app_env.client.get(f"/projects/{project_id}").text
    assert "Didn’t finish: adds only the pages and sentences the crawls above don’t have." in body
    assert "Count it anyway" not in body


def test_a_run_with_only_the_counting_fingerprint_gets_the_markdown_one(app_env):
    from app import markdown_store

    stg = [page("https://stg.resort.test/terms", 40)]
    acct = [page("https://stg-account.resort.test/legal", 40, "counting-fingerprint")]
    terms = "These terms apply to every member, stay and voucher. " * 8
    markdown_store.write("stg", stg[0].url, terms)
    markdown_store.write("acct", acct[0].url, terms)
    save(app_env, run_id="stg", source_url="https://stg.resort.test", pages=stg, total_words=40,
         capture_markdown=True, markdown_pages=1)
    save(app_env, run_id="acct", source_url="https://stg-account.resort.test", pages=acct, total_words=40,
         capture_markdown=True, markdown_pages=1)
    project_id = _project(app_env, run_ids=["stg", "acct"])
    assert run(app_env.db.get_project(project_id))["cached_overlap_words"] == 40, "matched by their Markdown"
    assert run(app_env.db.get_run("acct")).pages[0].markdown_hash


# ------------------------------------------------------------------ the pages

def _project(env, name="Acme", run_ids=()):
    res = env.client.post("/projects", json={"name": name, "run_ids": list(run_ids)})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def _save(env, run_id, rec: RunRecord, **kw):
    save(env, run_id=run_id, source_url=rec.source_url, pages=rec.pages, total_words=rec.total_words, **kw)


def test_a_project_page_adds_its_crawls_up(app_env):
    _save(app_env, "live", LIVE)
    _save(app_env, "stg", STAGING)
    project_id = _project(app_env, run_ids=["live", "stg"])

    body = app_env.client.get(f"/projects/{project_id}").text
    assert "Acme" in body and "400" in body, "home 100 + rooms 210 (the latest) + offer 40 + legal 50"
    listing = app_env.client.get("/projects").text
    assert "Acme" in listing and "400" in listing, "the cached total, without parsing the pages"


def test_a_run_can_be_in_several_projects(app_env):
    _save(app_env, "live", LIVE)
    group = _project(app_env, "Acme Group", ["live"])
    brand = _project(app_env, "Acme brand", ["live"])
    names = {p["name"] for p in run(app_env.db.projects_for_runs(["live"]))["live"]}
    assert names == {"Acme Group", "Acme brand"}
    assert group != brand


def test_the_report_dialog_sets_a_runs_projects(app_env):
    _save(app_env, "live", LIVE)
    keep = _project(app_env, "Keep")
    drop = _project(app_env, "Drop", ["live"])
    res = app_env.client.post("/crawl/live/projects", json={"project_ids": [keep], "new_name": "Brand new"})
    assert res.status_code == 200
    assert {p["name"] for p in res.json()["projects"]} == {"Keep", "Brand new"}
    assert drop not in run(app_env.db.project_ids_for_run("live"))

    report = app_env.client.get("/crawl/live").text
    assert 'id="project-open-btn"' in report and "Brand new" in report


def test_nobody_else_sees_or_changes_a_project(app_env):
    _save(app_env, "live", LIVE)
    project_id = _project(app_env, run_ids=["live"])
    app_env.current["user"] = app_env.other
    assert app_env.client.get(f"/projects/{project_id}").status_code == 404
    assert app_env.client.get(f"/projects/{project_id}/export.csv").status_code == 404
    assert app_env.client.post(f"/projects/{project_id}/runs", json={"run_ids": ["live"]}).status_code == 404
    assert app_env.client.delete(f"/projects/{project_id}").status_code == 404
    assert app_env.client.post("/crawl/live/projects", json={"project_ids": []}).status_code == 404
    assert app_env.client.post(f"/projects/{project_id}/consolidate").status_code == 404
    assert app_env.client.get(f"/projects/{project_id}/consolidation").status_code == 404
    assert app_env.client.get(f"/projects/{project_id}/sentences.csv").status_code == 404


def test_only_your_own_runs_go_into_your_project(app_env):
    save(app_env, run_id="theirs", user_id=2)
    _save(app_env, "live", LIVE)
    project_id = _project(app_env, run_ids=["live", "theirs"])
    assert app_env.client.post(f"/projects/{project_id}/runs", json={"run_ids": ["theirs"]}).json() == {"added": []}
    assert [r.id for r in run(app_env.db.get_project_run_records(project_id))] == ["live"]


def test_deleting_a_project_keeps_its_runs_and_deleting_a_run_leaves_the_project(app_env):
    _save(app_env, "live", LIVE)
    _save(app_env, "stg", STAGING)
    project_id = _project(app_env, run_ids=["live", "stg"])
    assert app_env.client.delete("/crawl/stg").json() == {"deleted": True}
    assert run(app_env.db.get_project(project_id))["cached_words"] == 350, "refreshed without the deleted run"

    assert app_env.client.delete(f"/projects/{project_id}").json() == {"deleted": True}
    assert run(app_env.db.get_run("live")) is not None
    assert app_env.client.get(f"/projects/{project_id}").status_code == 404


def test_the_share_link_shows_the_client_view_without_the_owners_controls(app_env):
    _save(app_env, "live", LIVE)
    _save(app_env, "stg", STAGING)
    run(app_env.db.set_run_public("stg", True))
    project_id = _project(app_env, run_ids=["live", "stg"])
    assert app_env.client.get(f"/projects/share/{project_id}").status_code == 404, "private until shared"

    assert app_env.client.post(f"/projects/{project_id}/share", json={"is_public": True}).json()["is_public"]
    app_env.current["user"] = app_env.other
    shared = app_env.client.get(f"/projects/share/{project_id}").text
    assert "400" in shared and "Back to the project" not in shared
    assert "/share/stg" in shared, "a public crawl links to its report"
    assert "/share/live" not in shared and "/crawl/live" not in shared, "a private one doesn't"


def test_the_csv_lists_every_page_with_where_it_counts_and_its_new_words(app_env):
    import csv
    import io

    _save(app_env, "live", LIVE)
    _save(app_env, "stg", STAGING)
    project_id = _project(app_env, run_ids=["live", "stg"])
    rows = list(csv.reader(io.StringIO(app_env.client.get(f"/projects/{project_id}/export.csv").text)))
    assert rows[0] == ["URL", "Title", "Crawl", "Words", "Same page as", "Also in", "Repeated in this crawl",
                       "Repeated from other crawls", "New words", "Checked by"]
    assert len(rows) == 1 + 6, "every page of both crawls"
    by_url = {r[0]: r for r in rows[1:]}
    assert by_url["https://stg.resort.test/rooms"][5] == "https://www.resort.test"
    assert by_url["https://www.resort.test/rooms"][4] == "https://stg.resort.test"
    assert by_url["https://www.resort.test/rooms"][8] == "0", "a copy adds nothing"
    assert sum(int(r[8]) for r in rows[1:]) == 400 == sum(int(r[3]) for r in rows[1:]) - 300

    overlap = list(csv.reader(io.StringIO(app_env.client.get(f"/projects/{project_id}/export.csv?only=overlap").text)))
    assert len(overlap) == 1 + 4, "home and rooms, in both crawls"
    assert app_env.client.get(f"/projects/{project_id}/sentences.csv").text.startswith("Sentence,Words,")


def test_a_crawl_started_in_a_project_joins_it(app_env, monkeypatch):
    started = []

    async def fake_run_crawl(*args, **kwargs):
        started.append(args)

    monkeypatch.setattr(app_env.main, "run_crawl", fake_run_crawl)
    project_id = _project(app_env)
    res = app_env.client.post("/crawl", json={"url": "https://sister-brand.test", "project_ids": [project_id]})
    run_id = res.json()["run_id"]
    assert run(app_env.db.project_ids_for_run(run_id)) == [project_id], "before its row is even written"

    res = app_env.client.post("/crawl", json={"url": "https://cassia.test", "project_name": "Cassia"})
    assert [p["name"] for p in run(app_env.db.projects_for_runs([res.json()["run_id"]]))[res.json()["run_id"]]] == ["Cassia"]


def test_someone_elses_project_or_cached_report_is_never_used(app_env, monkeypatch):
    async def fake_run_crawl(*args, **kwargs):
        pass

    monkeypatch.setattr(app_env.main, "run_crawl", fake_run_crawl)
    save(app_env, run_id="cached", source_url="https://shared.test", user_id=2)
    project_id = _project(app_env)
    res = app_env.client.post("/crawl", json={"url": "https://shared.test", "project_ids": [project_id]})
    assert res.json()["cached"] is False, "crawled afresh for you"
    assert run(app_env.db.project_ids_for_run("cached")) == []

    app_env.current["user"] = app_env.other
    res = app_env.client.post("/crawl", json={"url": "https://x2.test", "project_ids": [project_id]})
    assert res.status_code == 400 and res.json() == {"code": "project_not_found"}


def test_the_crawl_form_offers_the_project_and_the_runs_list_shows_it(app_env):
    _save(app_env, "live", LIVE)
    project_id = _project(app_env, run_ids=["live"])
    home = app_env.client.get(f"/?project={project_id}").text
    assert 'id="project-select"' in home and f'value="{project_id}" selected' in home
    assert 'class="project-chip">Acme<' in app_env.client.get("/runs").text
    assert ">Projects<" in home, "in the account menu"


def test_a_resumed_crawl_remembers_the_text_it_counted(app_env):
    from app.job_store import JOBS, restore_job

    _save(app_env, "resumed", LIVE, status="crawling")
    job = restore_job(run(app_env.db.get_run("resumed")))
    try:
        assert job.content_hashes["rooms-v1"] == "https://www.resort.test/rooms"
    finally:
        # The job registry outlives the test: left there, the run would open
        # as a live crawl, with an event stream that never ends.
        JOBS.pop(job.id, None)


# ------------------------------------------------------------------ in a browser

def _render(env, path: str, act=None) -> list[str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    origin = "http://projects.test"
    errors: list[str] = []

    def answer(route):
        url = route.request.url
        if not url.startswith(origin):
            return route.abort()
        res = env.client.get(url[len(origin):], headers={"host": "projects.test"})
        route.fulfill(status=res.status_code, headers=dict(res.headers), body=res.content)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            tab = browser.new_page()
            tab.set_default_timeout(20_000)
            tab.on("pageerror", lambda exc: errors.append(str(exc)))
            tab.route("**/*", answer)
            tab.goto(origin + path)
            tab.wait_for_load_state("networkidle")
            if act is not None:
                act(tab)
            browser.close()
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "BrowserType.launch" in str(exc):
            pytest.skip(f"browser unavailable: {exc}")
        raise
    return errors


def test_the_pages_run_without_script_errors(app_env):
    _save(app_env, "live", LIVE)
    _save(app_env, "stg", STAGING)
    _save(app_env, "loose", BRAND)
    project_id = _project(app_env, run_ids=["live", "stg"])
    app_env.client.post(f"/projects/{project_id}/share", json={"is_public": True})
    for path in ("/projects", f"/projects/{project_id}", f"/projects/{project_id}/client",
                 f"/projects/share/{project_id}", "/crawl/live", "/"):
        assert _render(app_env, path) == [], path

    def sentences_tab(tab):
        assert tab.locator("#overlap-sentences").is_hidden(), "the script shows one panel at a time"
        tab.click("#tab-overlap-sentences")
        assert tab.locator("#overlap-sentences").is_visible() and tab.locator("#overlap-pages").is_hidden()
        assert tab.get_attribute("#tab-overlap-sentences", "aria-selected") == "true"

    assert _render(app_env, f"/projects/{project_id}", sentences_tab) == []


def test_a_project_being_consolidated_shows_its_progress_then_its_result(app_env, monkeypatch):
    from app import consolidation_job

    _save(app_env, "live", LIVE)
    _save(app_env, "stg", STAGING)
    project_id = _project(app_env, run_ids=["live", "stg"])
    first = run(app_env.db.get_consolidation(project_id))["computed_gen"]
    # From here on the job runs in the background, as on the server: the page
    # finds it queued, polls it, and reloads once it's done.
    monkeypatch.setattr(consolidation_job, "INLINE", False)
    run(app_env.db.request_consolidation(project_id, "button", "something changed"))

    def wait_for_result(tab):
        tab.wait_for_selector("#consolidate-progress", state="hidden", timeout=20_000)
        tab.wait_for_load_state("networkidle")

    assert _render(app_env, f"/projects/{project_id}", wait_for_result) == []
    row = run(app_env.db.get_consolidation(project_id))
    assert row["status"] == "done" and row["computed_gen"] > first


# ------------------------------------------------------------------ a real crawl

def test_two_real_crawls_of_the_same_pages_overlap_entirely(app_env, monkeypatch):
    """A real crawl keeps each page's fingerprint, and a project of two crawls
    of the same pages counts them once."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from crawl4ai.deep_crawling.filters import URLFilter

    from app import crawler
    from app.job_store import JOBS, create_job

    body = " ".join(f"word{i}" for i in range(120))
    pages = {"/": f"<h1>Harbour</h1><p>{body}</p><a href='/rooms'>Rooms</a>",
             "/rooms": f"<h1>Rooms</h1><p>{body} {body}</p>"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            html = pages.get(self.path)
            self.send_response(200 if html else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html lang='en'><head><title>{self.path}</title></head><body>{html or ''}</body></html>".encode())

        def log_message(self, *args):
            pass

    class AllowAll(URLFilter):  # crawl4ai's DomainFilter can't match a host with a port
        def __init__(self, *args, **kwargs):
            super().__init__(name="AllowAll")

        def apply(self, url):
            return True

    async def no_email(*args, **kwargs):
        pass

    monkeypatch.setattr(crawler, "send_crawl_notification", no_email)
    monkeypatch.setattr(crawler, "DomainFilter", AllowAll)
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    run_ids = []
    try:
        for server in servers:
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            job = create_job(source_url=origin + "/", user_id=1, max_pages=float("inf"))
            job.concurrency = 2
            job.capture_markdown, job.markdown_state = True, "capturing"
            try:
                run(crawler.run_crawl(job.id, origin + "/", float("inf")))
            except Exception as exc:
                pytest.skip(f"browser unavailable: {exc}")
            assert job.status == "completed", job.error
            run_ids.append(job.id)
            JOBS.pop(job.id, None)
    finally:
        for server in servers:
            server.shutdown()

    saved = run(app_env.db.get_run(run_ids[0]))
    assert all(p.content_hash for p in saved.pages if p.success), "every counted page keeps its fingerprint"
    from app import markdown_store
    from app.projects import markdown_fingerprint

    for p in saved.pages:
        assert p.markdown_hash and p.markdown_hash == markdown_fingerprint(markdown_store.read_text(saved.id, p.url)), \
            "taken while crawling = rebuilt from the file later"
    # The sentences rebuilt from saved Markdown are, by and large, the ones the
    # crawl kept from the text it counted: older crawls compare with new ones.
    from app import segment_store

    exact = {h for hashes, _ in segment_store.read(saved.id).values() for h in hashes}
    segment_store.build_markdown(saved.id, [p.url for p in saved.pages if p.success], markdown_store.read_text)
    rebuilt = {h for hashes, _ in segment_store.read(saved.id, segment_store.MARKDOWN).values() for h in hashes}
    assert rebuilt and len(rebuilt & exact) >= 0.9 * len(rebuilt)

    project_id = _project(app_env, run_ids=run_ids)
    totals = summarize_project(run(app_env.db.get_project_run_records(project_id)))["totals"]
    assert totals["shared_pages"] == 2 and totals["words"] == saved.total_words
    result = run(app_env.db.get_consolidation(project_id))["result"]
    assert result["totals"]["same_pages"] == saved.total_words, "the second crawl's pages are the first's"
    assert all(r["coverage"]["exact"] == r["words"] - r["same_page_words"] for r in result["runs"]), \
        "every page it counts, checked by the sentences the crawl kept"
