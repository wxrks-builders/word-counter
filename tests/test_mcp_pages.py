"""The public pages around the MCP server: the guide, the homepage section,
the articles, robots.txt and the sitemap.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_app_flows import app_env, counter_host, markdown_host  # noqa: E402,F401
from test_i18n import TestPseudoSweep  # noqa: E402

from app import articles, i18n, mcp_server  # noqa: E402


def test_the_homepage_points_assistants_to_the_guide(app_env):
    app_env.current["user"] = None
    home = app_env.client.get("/", headers=counter_host(app_env)).text
    assert "Ask Claude or ChatGPT. Get the real count." in home
    assert 'href="/mcp"' in home and 'href="/articles/how-many-words-claude-vs-word-counter"' in home
    assert 'class="footer-links"' in home and 'href="/articles"' in home


def test_the_menu_links_to_the_connections(app_env):
    assert 'href="/mcp/connect"' in app_env.client.get("/runs").text


def test_the_guide_works_in_every_language(app_env):
    app_env.current["user"] = None
    page = app_env.client.get("/pt/mcp", headers=counter_host(app_env))
    assert page.status_code == 200 and 'href="/pt/mcp/connect"' in page.text
    assert f"https://{app_env.surfaces.COUNTER.host}/mcp" in page.text, "the server URL, for this host"


def test_the_articles_are_listed_and_unknown_ones_are_missing(app_env):
    app_env.current["user"] = None
    index = app_env.client.get("/articles", headers=counter_host(app_env))
    assert index.status_code == 200
    for article in articles.ARTICLES:
        assert f'href="/articles/{article.slug}"' in index.text
        page = app_env.client.get(f"/articles/{article.slug}", headers=counter_host(app_env))
        assert page.status_code == 200 and article.title.split("?")[0] in page.text
    assert app_env.client.get("/articles/nope").status_code == 404


def test_robots_and_the_sitemap_list_the_public_pages(app_env):
    robots = app_env.client.get("/robots.txt", headers=counter_host(app_env))
    assert robots.status_code == 200 and "Disallow: /crawl/" in robots.text and "Disallow: /mcp/" in robots.text
    host = app_env.surfaces.COUNTER.host
    assert f"Sitemap: https://{host}/sitemap.xml" in robots.text
    sitemap = app_env.client.get("/sitemap.xml", headers=counter_host(app_env))
    assert sitemap.headers["content-type"].startswith("application/xml")
    for path in ("/", "/mcp", "/articles", "/articles/how-many-words-claude-vs-word-counter"):
        assert f"<loc>https://{host}{path if path != '/' else ''}" in sitemap.text, path
    # A language is listed once it's translated (i18n.PUBLISH_AT); until then
    # its pages are English under another address and stay out.
    from app import i18n

    for code in i18n.LANGUAGES:
        tag = i18n.HREFLANG[code]
        listed = f'hreflang="{tag}" href="https://{host}{i18n.localized_path("/mcp", code)}"' in sitemap.text
        assert listed == (code in i18n.published_languages()), code


class TestTheGuideIsTranslatable(TestPseudoSweep):
    """The same sweep as the front door's, over the guide."""

    def test_public_pages_have_no_unextracted_strings(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PSEUDO_LOCALE", "1")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "z.db"))
        monkeypatch.setenv("MARKDOWN_DIR", str(tmp_path / "markdown"))
        monkeypatch.setattr(i18n, "PSEUDO_ENABLED", True)
        import importlib

        import app.db as db
        import app.main as main
        importlib.reload(db)
        importlib.reload(main)
        from fastapi.testclient import TestClient

        with TestClient(main.app) as client:
            from app import surfaces
            client.headers["host"] = surfaces.COUNTER.host
            for path in ("/zz/mcp",):
                bare = self._bare(client.get(path).text)
                bare = [b for b in bare if "Word Counter" not in b]
                assert bare == [], f"{path}: unextracted strings {bare}"


def test_the_article_quotes_the_measured_figures_and_says_it_is_english(app_env):
    app_env.current["user"] = None
    x = articles.EXPERIMENT
    page = app_env.client.get("/articles/how-many-words-claude-vs-word-counter", headers=counter_host(app_env)).text
    assert f"{x['with_full']['words']:,} words on {x['with_full']['pages']:,}" in page
    assert f"{x['alone_quick']['all_text_words']:,}" in page
    assert '<main class="article" lang="en">' in page
    assert page.count('rel="alternate" hreflang=') == 1, "no translations claimed"
    static = Path(__file__).resolve().parents[1] / "app/static/articles/claude-vs-word-counter"
    for name in ("cover.png", "alone-quick.webp", "alone-careful.webp", "with-quick.webp", "with-full.webp",
                 "report-summary.webp", "guide-connect.webp", "connect.webp"):
        assert (static / name).is_file(), name
        assert f"/static/articles/claude-vs-word-counter/{name}" in page, name
    for log in x["logs"]:
        text = (static / log["file"]).read_text()
        assert "wck_" not in text and "/Users/" not in text and "claude-501" not in text, log["file"]


def test_the_homepage_mock_quotes_the_same_count(app_env):
    app_env.current["user"] = None
    home = app_env.client.get("/", headers=counter_host(app_env)).text
    assert f"{articles.WXRKS_COUNT['words']:,} words on {articles.WXRKS_COUNT['pages']:,} pages" in home
    assert articles.WXRKS_COUNT["words"] == articles.EXPERIMENT["with_full"]["words"]


def test_the_article_compares_all_three_ways_to_count(app_env):
    app_env.current["user"] = None
    page = app_env.client.get("/articles/how-many-words-claude-vs-word-counter", headers=counter_host(app_env)).text
    assert 'id="compare"' in page and 'href="#compare"' in page
    columns = [col["name"] for col in articles.COMPARISON["columns"]]
    assert len(columns) == 3
    rows = [row for group in articles.COMPARISON["groups"] for row in group["rows"]]
    assert page.count('<th scope="row">') >= len(rows)
    for row in rows:
        assert len(row["cells"]) == 3, row["feature"]
        assert all(cell["state"] in ("yes", "partly", "no", None) for cell in row["cells"]), row["feature"]
        assert row["feature"] in page, row["feature"]
    # Every answer is labelled with its column, for the one-card-per-feature phone layout.
    for name in columns:
        assert page.count(f'data-col="{name}"') == len(rows), name
    # The MCP column is honest about what only the website does.
    by_feature = {row["feature"]: row for row in rows}
    assert by_feature["Sites behind a password or a sign-in"]["cells"][2]["state"] == "no"
    assert "site_password_needed" in mcp_server._START_ERRORS, "the server sends those counts to the website"
    assert f"{articles.EXPERIMENT['with_full']['subdomain_words']:,}" in page


def test_the_homepage_says_why_not_just_ask_an_assistant(app_env):
    app_env.current["user"] = None
    home = app_env.client.get("/", headers=counter_host(app_env)).text
    v = articles.HOME_VS
    assert "Why not just ask ChatGPT or Claude?" in home
    # Both cards quote the article's measured figures, row against row.
    for figure in (v["alone_words"], v["main_words"], v["subdomain_words"], v["alone_pages_fetched"], v["translated_pages"]):
        assert f"{figure:,}" in home, figure
    assert home.count('class="vs-row"') == 8
    assert f'href="/articles/how-many-words-claude-vs-word-counter#compare"' in home
    assert f"All {v['differences']} differences" in home
    assert v["differences"] == sum(len(g["rows"]) for g in articles.COMPARISON["groups"])
    # It comes before the MCP section: why not ask an assistant, then how to.
    assert home.index('class="home-section home-vs"') < home.index("home-mcp")


def test_the_homepage_mcp_section_shows_the_steps_and_a_follow_up(app_env):
    app_env.current["user"] = None
    home = app_env.client.get("/", headers=counter_host(app_env)).text
    for step in ("Get your connection URL", "Add it to your app", "Ask about any website"):
        assert step in home
    assert "What should I quote on?" in home and f"{articles.WXRKS_COUNT['quote_scope']:,}" in home
    assert 'class="chat-mock vis vis-chat"' in home, "the chat plays out once on the front page"
    guide = app_env.client.get("/mcp", headers=counter_host(app_env)).text
    assert "What should I quote on?" not in guide and "vis-chat" not in guide, "the guide keeps the short mock-up"


def test_site_to_markdown_doesnt_argue_about_word_counts(app_env):
    app_env.current["user"] = None
    home = app_env.client.get("/", headers=markdown_host(app_env)).text
    assert "Why not just ask ChatGPT or Claude?" not in home
