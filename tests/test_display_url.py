"""The address a report shows, when the crawl had to start from an inner page.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_app_flows import app_env, run, save  # noqa: E402,F401

INNER = "https://www.example-resorts.com/stays/somewhere-quiet"


def _set(env, url, run_id="r"):
    return env.client.post(f"/crawl/{run_id}/display-url", json={"url": url})


def test_the_owner_can_show_the_home_page_instead(app_env):
    save(app_env, source_url=INNER)
    res = _set(app_env, "example-resorts.com")
    assert res.status_code == 200 and res.json()["display_url"] == "https://example-resorts.com"

    report = app_env.client.get("/crawl/r").text
    assert "<h1 id=\"report-url\">https://example-resorts.com</h1>" in report
    assert "<title>https://example-resorts.com" in report
    assert "Crawled from https://www.example-resorts.com/stays/somewhere-quiet" in report, "the owner can still see it"
    assert 'sourceUrl: "https://www.example-resorts.com/stays/somewhere-quiet"' in report, "a re-crawl starts where it worked"

    run(app_env.db.set_run_public("r", True))
    shared = app_env.client.get("/share/r").text
    assert "<h1 id=\"report-url\">https://example-resorts.com</h1>" in shared
    assert "Crawled from" not in shared and "somewhere-quiet" not in shared.split("initialPages")[0]

    assert "example-resorts.com-word-count.csv" in app_env.client.get("/crawl/r/export.csv").headers["content-disposition"]
    assert run(app_env.db.list_user_runs(1))[0]["shown_url"] == "https://example-resorts.com", "and in the runs list"


def test_only_an_address_on_the_crawled_site_is_accepted(app_env):
    save(app_env, source_url=INNER)
    assert _set(app_env, "https://www.example.com").json()["code"] == "display_url_other_site"
    assert _set(app_env, "not a url").json()["code"] == "display_url_invalid"
    assert _set(app_env, "https://stg-v2.example-resorts.com").status_code == 200, "another subdomain of the same site"


def test_an_empty_address_goes_back_to_where_the_crawl_started(app_env):
    save(app_env, source_url=INNER)
    _set(app_env, "https://example-resorts.com")
    assert _set(app_env, "").json()["display_url"] == INNER
    report = app_env.client.get("/crawl/r").text
    assert f"<h1 id=\"report-url\">{INNER}</h1>" in report and "Crawled from" not in report


def test_nobody_else_can_change_it(app_env):
    save(app_env, source_url=INNER)
    app_env.current["user"] = app_env.other
    assert _set(app_env, "https://example-resorts.com").status_code == 404
    assert 'id="edit-url-btn"' not in app_env.client.get("/crawl/r").text
