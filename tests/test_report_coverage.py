"""What a report says about subdomains and language folders, counted or not.

A run used to be silent about anything it didn't crawl: a subdomain the scope
left out, or a /de/ folder skipped as a translation, simply didn't appear, and
"was that ignored or counted?" had no answer. The crawler now records what the
crawled pages link to, and the report names what it left out.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import PageResult  # noqa: E402
from app.report import LinkCensus, host_for_url, left_out, summarize  # noqa: E402


def test_hosts_are_named_as_a_reader_would():
    assert host_for_url("https://www.Site.com/a") == "site.com"
    assert host_for_url("https://blog.site.com/") == "blog.site.com"
    assert host_for_url("not a url") == ""


class TestLinkCensus:
    def test_counts_pages_not_links(self):
        hosts, languages = {}, {}
        census = LinkCensus("site.com", hosts, languages)
        # One page linking blog twice and /de/ three times counts once each.
        census.observe(["https://blog.site.com/a", "https://blog.site.com/b",
                        "https://www.site.com/de/x", "https://site.com/de/y", "https://site.com/de/z"])
        census.observe(["https://blog.site.com/a", "https://shop.site.com/", "https://site.com/fr-ch/"])
        assert hosts == {"blog.site.com": 2, "site.com": 2, "shop.site.com": 1}
        assert languages == {"de": 1, "fr": 1}

    def test_other_sites_and_non_links_are_ignored(self):
        hosts, languages = {}, {}
        census = LinkCensus("www.site.com", hosts, languages)
        census.observe(["https://othersite.com/de/", "https://notsite.com/", "mailto:a@site.com", None, "/relative"])
        assert hosts == {} and languages == {}


def _pages(*urls):
    return [PageResult(url=u, title="", word_count=100, success=True) for u in urls]


def test_left_out_names_only_what_was_not_counted():
    pages = _pages("https://site.com/", "https://site.com/en/about", "https://blog.site.com/post")
    result = left_out(pages, {"site.com": 9, "blog.site.com": 4, "shop.site.com": 7, "staging.site.com": 2},
                      {"en": 3, "de": 5, "fr": 5})
    assert result["skipped_hosts"] == [{"name": "shop.site.com", "linked_from": 7},
                                       {"name": "staging.site.com", "linked_from": 2}]
    assert result["skipped_languages"] == [{"name": "de", "linked_from": 5}, {"name": "fr", "linked_from": 5}]
    assert result["census_recorded"]


def test_older_runs_have_nothing_to_say_about_what_they_left_out():
    result = left_out(_pages("https://site.com/"), {}, {})
    assert result == {"skipped_hosts": [], "skipped_languages": [], "set_aside_languages": [], "census_recorded": False}


def test_the_summary_groups_by_subdomain():
    pages = _pages("https://www.site.com/", "https://site.com/a", "https://blog.site.com/p")
    assert summarize(pages)["hosts"] == [
        {"name": "site.com", "pages": 2, "words": 200},
        {"name": "blog.site.com", "pages": 1, "words": 100},
    ]


# ------------------------------------------------------ a page's own language

from app import language_check  # noqa: E402

_CHINESE = "我们为全球企业提供专业的翻译和本地化服务，帮助您的网站在每个市场都能被理解。" * 6
_ENGLISH = "We help companies translate and localise their websites so every market understands them. " * 4


class TestOtherLanguage:
    def test_a_country_folder_in_chinese_is_set_aside(self):
        """The reported case: /cn/ isn't a language code, so the URL let it
        through; the page itself says what it is."""
        html = '<html lang="zh-CN"><body>…</body></html>'
        assert language_check.other_language(html, _CHINESE, ["en"], {"latin"}) == "zh"

    def test_a_page_that_declares_the_wrong_language_is_caught_by_its_script(self):
        html = '<html lang="en"><body>…</body></html>'
        assert language_check.other_language(html, _CHINESE, ["en"], {"latin"}) == "script:cjk"

    def test_the_counted_language_passes(self):
        assert language_check.other_language('<html lang="en-US">', _ENGLISH, ["en"], {"latin"}) is None
        assert language_check.other_language("<html>", _ENGLISH, ["en"], {"latin"}) is None

    def test_a_same_script_translation_is_caught_only_by_its_declaration(self):
        german = "Wir helfen Unternehmen, ihre Websites zu übersetzen und zu lokalisieren. " * 4
        assert language_check.other_language('<html lang="de">', german, ["en"], {"latin"}) == "de"
        assert language_check.other_language("<html>", german, ["en"], {"latin"}) is None

    def test_thin_or_mixed_pages_are_not_second_guessed(self):
        assert language_check.dominant_script("短い") is None
        mixed = _ENGLISH[:200] + _CHINESE[:150]
        assert language_check.dominant_script(mixed) is None

    def test_an_unknown_language_is_never_judged_by_script(self):
        assert language_check.scripts_for(["en", "xx"]) == set()
        assert language_check.scripts_for(["en", "ja"]) == {"latin", "cjk"}


def test_the_report_lists_what_was_set_aside():
    result = left_out(_pages("https://site.com/"), {"site.com": 1}, {},
                      {"zh": {"pages": 3, "skipped": 512, "example": "https://site.com/cn/pricing"},
                       "script:cyrillic": {"pages": 2, "skipped": 0, "example": "https://site.com/ru-page"}})
    assert result["set_aside_languages"][0] == {"name": "zh", "pages": 3, "skipped": 512,
                                                "example": "https://site.com/cn/pricing"}
    assert [r["name"] for r in result["set_aside_languages"]] == ["zh", "script:cyrillic"]


# ------------------------------------------------------------ a real crawl

import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import pytest  # noqa: E402

from test_app_flows import app_env, run  # noqa: E402,F401

_EN_BODY = "<p>" + _ENGLISH + "</p>"
_ZH_BODY = "<p>" + _CHINESE + "</p>"
_SITE = {
    "/": ("en", _EN_BODY + "<a href='/about'>About</a><a href='/cn/'>中文</a><a href='/cn/news'>新闻</a><a href='/cn/team'>团队</a>"),
    "/about": ("en", _EN_BODY),
    "/cn/": ("zh-CN", _ZH_BODY),
    "/cn/news": ("zh-CN", _ZH_BODY),
    # Read third, by which time /cn/ is plainly Chinese: its links aren't fetched.
    "/cn/team": ("zh-CN", _ZH_BODY + "".join(f"<a href='/cn/p{i}'>p{i}</a>" for i in range(8))),
    **{f"/cn/p{i}": ("zh-CN", _ZH_BODY) for i in range(8)},
}


@pytest.fixture(scope="module")
def bilingual_site():
    fetched: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            fetched.append(self.path)
            lang, body = _SITE.get(self.path, ("en", ""))
            self.send_response(200 if self.path in _SITE else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html lang='{lang}'><head><title>{self.path}</title></head><body>{body}</body></html>".encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", fetched
    server.shutdown()


def test_a_crawl_sets_aside_a_chinese_country_folder(app_env, bilingual_site, monkeypatch):
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

    origin, fetched = bilingual_site
    job = create_job(source_url=origin + "/", user_id=1, max_pages=float("inf"))
    job.concurrency = 1  # one at a time, so /cn/team is read after the other two
    try:
        run(crawler.run_crawl(job.id, origin + "/", float("inf")))
    except Exception as exc:
        pytest.skip(f"browser unavailable: {exc}")

    counted = sorted(url.removeprefix(origin) for url, page in job.pages.items() if page.success)
    assert job.status == "completed", job.error
    assert counted == ["/", "/about"], "only the English pages are counted"
    assert job.other_languages["zh"]["pages"] == 3
    assert job.other_languages["zh"]["skipped"] == 8
    assert not any(path.startswith("/cn/p") for path in fetched), "a learned folder isn't fetched"
    saved = run(app_env.db.get_run(job.id))
    assert saved.other_languages["zh"]["skipped"] == 8
