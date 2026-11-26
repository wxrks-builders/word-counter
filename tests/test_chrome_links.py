"""Links that live only in menus and footers must still be crawled.

crawl4ai strips excluded_tags before collecting links, so these used to be
invisible: a site linking its inner pages only from the footer came back as the
homepage plus one page. The fix must change discovery and nothing else — the
end-to-end test below crawls the same local site both ways and checks every
page's word count is identical.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))

from app.crawler import EXCLUDED_TAGS, ChromeLinkBFS, _markdown_text, chrome_links  # noqa: E402
from app.word_count import count_words  # noqa: E402


class TestChromeLinks:
    def test_reads_nav_footer_aside_and_form(self):
        html = """<html><body>
            <nav><a href="/a">A</a></nav>
            <main><a href="/body-link">not ours to find</a></main>
            <aside><a href="/c">C</a></aside>
            <form><a href="/d">D</a></form>
            <footer><div><a href="/b">B</a></div></footer>
        </body></html>"""
        assert sorted(chrome_links(html, "https://site.com/")) == [
            "https://site.com/a", "https://site.com/b", "https://site.com/c", "https://site.com/d",
        ]

    def test_resolves_relative_links_against_the_page_and_base_href(self):
        page = "https://site.com/en/about"
        assert chrome_links('<footer><a href="team">t</a></footer>', page) == ["https://site.com/en/team"]
        with_base = '<head><base href="https://site.com/root/"></head><footer><a href="x">x</a></footer>'
        assert chrome_links(with_base, page) == ["https://site.com/root/x"]

    def test_keeps_subdomains_drops_other_sites(self):
        html = """<footer>
            <a href="https://auth.site.com/sign-up">in scope for the filter chain to judge</a>
            <a href="https://www.facebook.com/site">social</a>
            <a href="https://sister-brand.com/">sister site</a>
        </footer>"""
        assert chrome_links(html, "https://www.site.com/") == ["https://auth.site.com/sign-up"]

    def test_skips_non_page_hrefs_fragments_and_repeats(self):
        html = """<nav>
            <a href="#top">top</a><a href="mailto:x@site.com">m</a><a href="tel:+1">t</a>
            <a href="javascript:void(0)">j</a><a href="/faq#billing">f</a><a href="/faq">f</a>
        </nav>"""
        assert chrome_links(html, "https://site.com/") == ["https://site.com/faq"]

    def test_language_switcher_links_are_left_alone(self):
        # LanguageFilter only understands /xx/ paths; a switcher pointing at a
        # language subdomain would otherwise pull a second language into the count.
        html = """<html lang="en"><head>
            <link rel="alternate" hreflang="de" href="https://de.site.com/">
            <link rel="alternate" hreflang="x-default" href="https://www.site.com/">
            <link rel="alternate" hreflang="en-GB" href="https://www.site.com/uk">
        </head><body>
            <nav><a href="https://de.site.com/">Deutsch</a>
                 <a href="https://fr.site.com/" hreflang="fr">Français</a>
                 <a href="https://es.site.com/" lang="es">Español</a>
                 <a href="https://www.site.com/uk">UK</a>
                 <a href="/pricing">Pricing</a></nav>
        </body></html>"""
        assert chrome_links(html, "https://www.site.com/") == [
            "https://www.site.com/uk", "https://www.site.com/pricing",
        ]

    def test_without_a_page_language_nothing_is_second_guessed(self):
        html = '<nav><a href="https://de.site.com/" hreflang="de">Deutsch</a></nav>'
        assert chrome_links(html, "https://www.site.com/") == ["https://de.site.com/"]

    def test_nothing_to_read(self):
        assert chrome_links(None, "https://site.com/") == []
        assert chrome_links("", "https://site.com/") == []
        assert chrome_links("<main><a href='/x'>x</a></main>", "https://site.com/") == []


# --------------------------------------------------------------- end to end

_BODY = "<p>" + " ".join(f"word{i}" for i in range(60)) + "</p>"
_CHROME = """
<nav><a href="/">Home</a><a href="/benefits">Benefits</a></nav>
<footer><a href="/faq">FAQ</a><a href="/hotels">Hotels</a>
  <a href="/contact">Contact</a><a href="/terms">Terms</a></footer>"""

_PAGES = {
    # Like the site that exposed this: the body links one page, the footer the rest.
    "index.html": f"<h1>Home</h1>{_BODY}<main><a href='/benefits'>See benefits</a></main>",
    "benefits": f"<h1>Benefits</h1>{_BODY}{_BODY}",
    "faq": f"<h1>FAQ</h1>{_BODY}",
    "hotels": f"<h1>Hotels</h1>{_BODY}<p>extra words here for a distinct count</p>",
    "contact": f"<h1>Contact</h1>{_BODY}",
    "terms": f"<h1>Terms</h1>{_BODY}{_BODY}{_BODY}",
}


class _Html(SimpleHTTPRequestHandler):
    def guess_type(self, path):
        return "text/html"

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    for name, body in _PAGES.items():
        (root / name).write_text(f"<html lang='en'><head><title>{name}</title></head><body>{_CHROME}{body}</body></html>")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Html, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def _crawl(seed: str, strategy_cls, **strategy_kwargs) -> dict[str, int]:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, DefaultMarkdownGenerator, PruningContentFilter

    config = CrawlerRunConfig(
        deep_crawl_strategy=strategy_cls(max_depth=5, max_pages=50, include_external=False, **strategy_kwargs),
        stream=True,
        markdown_generator=DefaultMarkdownGenerator(content_filter=PruningContentFilter()),
        excluded_tags=list(EXCLUDED_TAGS),
        word_count_threshold=10,
        verbose=False,
    )
    words: dict[str, int] = {}
    async with AsyncWebCrawler(config=BrowserConfig(verbose=False)) as crawler:
        async for result in await crawler.arun(seed + "/", config=config):
            if result.success:
                words[urlparse(result.url).path.strip("/") or "index"] = count_words(_markdown_text(result))
    return words


def test_footer_pages_are_found_and_counts_do_not_change(site):
    from crawl4ai.deep_crawling import BFSDeepCrawlStrategy

    try:
        before = asyncio.run(_crawl(site, BFSDeepCrawlStrategy))
    except Exception as exc:  # no Chromium on this machine
        pytest.skip(f"browser unavailable: {exc}")
    from app.report import LinkCensus

    linked_hosts: dict[str, int] = {}
    after = asyncio.run(_crawl(site, ChromeLinkBFS, census=LinkCensus("127.0.0.1", linked_hosts, {})))

    # The bug, reproduced: only what the body links is reachable.
    assert set(before) == {"index", "benefits"}
    assert "faq" not in before

    assert {"benefits", "faq", "hotels", "contact", "terms"} <= set(after)
    for page, count in before.items():
        assert after[page] == count, f"{page}: word count changed {count} -> {after[page]}"
    # And the menus still aren't counted: every page carries the same chrome,
    # so the one-body pages must agree exactly.
    assert after["faq"] == after["contact"]
    # The census ran on every page read: each links back to the site itself.
    assert linked_hosts == {"127.0.0.1": len(after)}
