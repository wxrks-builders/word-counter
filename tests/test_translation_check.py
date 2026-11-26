"""The translation check: what a multilingual site still hasn't translated.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import gzip
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_app_flows import app_env, run  # noqa: E402,F401

from app import translation_check as tc  # noqa: E402


@pytest.fixture(autouse=True)
def no_task_left_over():
    """The running check is module state; a task from another test's event
    loop must never look like one running here."""
    tc._task = None
    yield
    tc._task = None


def page(body: str, head: str = "", url: str = "https://site.test/pt/page", lang: str = "pt-BR"):
    return tc.extract_page(f"<html lang='{lang}'><head>{head}</head><body>{body}</body></html>", url)


def texts(extracted) -> list[str]:
    return sorted(unit.text for unit in extracted.units.values())


# ------------------------------------------------------------------ extraction

def test_inline_markup_stays_one_block_of_text():
    assert texts(page("<p>Click <b>here</b> to <a href='/x'>start</a> today.</p>")) == ["Click here to start today."]


def test_a_div_with_text_around_a_block_keeps_its_own_text():
    assert texts(page("<div>Before the card <div>Inside the card</div> after it</div>")) == [
        "Before the card after it", "Inside the card"]


def test_the_desktop_and_mobile_menus_count_once():
    nav = "<nav><a href='/a'>Pricing for teams</a></nav>"
    assert texts(page(nav + nav.replace("<nav>", "<nav class='mobile'>"))) == ["Pricing for teams"]


def test_links_side_by_side_are_each_their_own_text():
    """A menu written as bare links: one untranslated item must still match."""
    assert texts(page("<nav><a href='/'>Home</a> <a href='/about'>About us</a> <a href='/blog'>Blog</a></nav>")) == [
        "About us", "Blog", "Home"]


def test_what_isnt_copy_is_left_out():
    body = ("<div class='w-locales-list'><a href='/'>English</a><a href='/pt'>Português</a></div>"
            "<div class='w-condition-invisible'>Hidden by a condition</div>"
            "<script>var label = 'not text';</script><pre>code sample here</pre>"
            "<div id='__vconsole'>All Log Info Warn Error</div>"
            "<p translate='no'>Brand name here</p><p class='notranslate'>Another brand name</p>"
            "<p>Visible words here</p>")
    assert texts(page(body)) == ["Visible words here"]


def test_the_language_switcher_is_found_by_the_pages_own_alternates():
    head = ("<link rel='alternate' hreflang='en' href='https://site.test/page'>"
            "<link rel='alternate' hreflang='pt-BR' href='https://site.test/pt/page'>")
    body = ("<ul><li><a href='/page'>English</a></li><li><a href='/pt/page'>Português</a></li></ul>"
            "<p>Real words to check</p>")
    extracted = page(body, head)
    assert texts(extracted) == ["Real words to check"]
    assert extracted.alternates == {"en": "https://site.test/page", "pt-BR": "https://site.test/pt/page"}


def test_form_messages_buttons_and_image_text_are_checked():
    body = ("<form><input type='email' placeholder='Your work email'>"
            "<input type='submit' value='Book a demo' data-wait='Please wait...'></form>"
            "<div class='w-form-done'><div>Thank you! Your submission has been received!</div></div>"
            "<img src='a.png' alt='Dashboard with all projects'>")
    extracted = page(body)
    attrs = {(unit.detail, unit.text) for unit in extracted.attrs.values()}
    assert {("placeholder", "Your work email"), ("value", "Book a demo"), ("data-wait", "Please wait..."),
            ("alt", "Dashboard with all projects")} <= attrs
    assert "Thank you! Your submission has been received!" in texts(extracted)


def test_where_to_fix_it_comes_from_the_webflow_markup():
    context = quote(json.dumps([{"componentId": "nav-123", "instanceId": "i1"}]))
    body = (f"<div class='navbar w-nav' data-wf-component-context='{context}'>"
            f"<div data-wf-component-context='{context}'>Menu label text</div></div>"
            "<div class='w-dyn-list'><div class='w-dyn-item'><p>From a CMS item</p></div></div>"
            "<div class='w-richtext'><p>Inside rich text</p></div><p>Plain page text</p>")
    by_text = {unit.text: unit for unit in page(body).units.values()}
    menu = by_text["Menu label text"]
    assert (menu.where, menu.component_id, menu.component_label) == ("component", "nav-123", "navbar")
    assert by_text["From a CMS item"].where == "cms"
    assert by_text["Inside rich text"].where == "richtext"
    assert by_text["Plain page text"].where == "page"


def test_prices_years_addresses_and_tiny_labels_arent_text():
    body = "<p>$49</p><p>2026</p><p>https://wxrks.com/pricing</p><p>OK</p><p>hello@wxrks.com</p><p>Sous-Chef</p>"
    assert texts(page(body)) == ["Sous-Chef"]


def test_keys_ignore_case_quotes_spacing_and_end_punctuation():
    assert tc.normalize_key("Learn more →") == tc.normalize_key("learn  more")
    assert tc.text_key("It’s “ready”") == tc.text_key("it's \"ready\"")
    assert tc.text_key("Sign up­") == tc.text_key("Sign up")
    assert tc.text_key("Plan 1") != tc.text_key("Plan 2")


# ------------------------------------------------------------------ English left behind

@pytest.mark.parametrize("text, language", [
    ("A plataforma reúne tradução automática, revisão e entrega num só lugar", "pt-BR"),
    ("Die Plattform vereint maschinelle Übersetzung, Prüfung und Auslieferung an einem Ort", "de-DE"),
    ("Was die Kunden an der Plattform schätzen, die so viel Zeit spart, will man nicht missen", "de-DE"),
    ("Plattformen samlar maskinöversättning, granskning och leverans på ett ställe", "sv"),
    ("La piattaforma riunisce traduzione automatica, revisione e consegna in un unico posto", "it"),
    ("La plataforma reúne la traducción automática, la revisión y la entrega en un solo lugar", "es"),
    ("wxrks Sous-Chef Portal", "ja"),
])
def test_ordinary_translated_text_isnt_taken_for_english(text, language):
    assert not tc.english_looking(text, language)


@pytest.mark.parametrize("text, language", [
    ("Our September update is here and it brings the new editor to your team", "pt-BR"),
    ("Translate your website with the new Portal", "ja"),
    ("What our customers say about the platform", "ko"),
])
def test_english_is_recognised(text, language):
    assert tc.english_looking(text, language)


def test_a_page_comparison_finds_each_kind():
    from app.url_policy import canonical_key

    source = page("<p>Translate your whole website</p><p>Book a demo</p><p>Login</p>"
                  "<img alt='The team at work'>",
                  "<title>wxrks: Translation OS</title><meta name='description' content='The modern TMS for teams'>",
                  url="https://site.test/page", lang="en")
    target = page("<p>Traduza todo o seu site</p><p>Book a demo</p><p>Login</p><img alt='The team at work'>"
                  "<p>Our new editor is here and it changes how you work</p><a href='/pricing'>Preços</a>",
                  "<title>wxrks: Translation OS</title><meta name='description' content='O TMS moderno'>",
                  lang="en")
    findings, words = tc.compare_page(source, target, "pt-BR", "en",
                                      {canonical_key("https://site.test/pricing"): "https://site.test/pt/pricing"},
                                      "https://site.test/pt/page")
    found = {(f[0], f[2]) for f in findings}
    assert ("text", "Book a demo") in found
    assert ("short_text", "Login") in found
    assert ("english_text", "Our new editor is here and it changes how you work") in found
    assert ("attribute", "The team at work") in found
    assert ("meta", "wxrks: Translation OS") in found
    assert not any(kind == "meta" and text == "O TMS moderno" for kind, text in found)
    assert ("wrong_lang", 'lang="en"') in found
    assert ("english_link", "/pricing") in found
    assert words == 3 + 1 + 11


# ------------------------------------------------------------------ the sitemap

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">
  <url><loc>https://site.test/</loc>
    <xhtml:link rel="alternate" hreflang="pt-BR" href="https://site.test/pt"/>
    <xhtml:link rel="alternate" hreflang="en" href="https://site.test/"/>
    <xhtml:link rel="alternate" hreflang="x-default" href="https://site.test/"/></url>
  <url><loc>https://site.test/pt</loc>
    <xhtml:link rel="alternate" hreflang="pt-BR" href="https://site.test/pt"/>
    <xhtml:link rel="alternate" hreflang="en" href="https://site.test/"/>
    <xhtml:link rel="alternate" hreflang="x-default" href="https://site.test/"/></url>
  <url><loc>https://site.test/about</loc>
    <xhtml:link rel="alternate" hreflang="en" href="https://site.test/about"/>
    <xhtml:link rel="alternate" hreflang="x-default" href="https://site.test/about"/></url>
  <url><loc>https://site.test/pt/orphan</loc></url>
</urlset>"""


def test_the_sitemap_pairs_each_page_with_its_versions():
    entries, children = tc.parse_sitemap(SITEMAP.encode())
    assert children == []
    plan = tc.build_plan("https://site.test", entries, None)
    assert plan.source_language == "en"
    assert [(loc["code"], loc["prefix"]) for loc in plan.locales] == [("pt-BR", "/pt")]
    assert plan.groups == {"https://site.test/": {"pt-BR": "https://site.test/pt"},
                           "https://site.test/about": {"pt-BR": None}}


def test_a_gzipped_sitemap_index_is_followed():
    index = ("<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
             "<sitemap><loc>https://site.test/pages.xml</loc></sitemap></sitemapindex>")
    entries, children = tc.parse_sitemap(gzip.compress(index.encode()))
    assert (entries, children) == ([], ["https://site.test/pages.xml"])


def test_a_huge_site_is_capped_and_says_so():
    entries, _ = tc.parse_sitemap(SITEMAP.encode())
    plan = tc.build_plan("https://site.test", entries, None, max_pages=1)
    assert plan.capped and len(plan.groups) == 1


def test_a_site_without_language_versions_is_reported_as_such():
    entries, _ = tc.parse_sitemap(b"<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                                  b"<url><loc>https://site.test/</loc></url></urlset>")
    with pytest.raises(tc.CheckAborted):
        tc.build_plan("https://site.test", entries, None)


# ------------------------------------------------------------------ a whole check

NAV_CONTEXT = quote(json.dumps([{"componentId": "nav-1", "instanceId": "a"}]))


def _nav(about_href: str, about_text: str, demo: str) -> str:
    return (f"<nav class='navbar' data-wf-component-context='{NAV_CONTEXT}'>"
            f"<a href='{about_href}' data-wf-component-context='{NAV_CONTEXT}'>{about_text}</a>"
            f"<span data-wf-component-context='{NAV_CONTEXT}'>{demo}</span></nav>")


def _site_pages() -> dict:
    """path -> (lang, title, description, body), or an int status, or ("redirect", to)."""
    en_nav = _nav("/about", "About us and our story", "Book a demo now")
    pt_nav = _nav("/pt/about", "Sobre nós e a nossa história", "Book a demo now")
    return {
        "/": ("en", "Harbour TMS", "Translation for teams", en_nav +
              "<h1>Translate every page you publish</h1><p>Every page, every language, in one place.</p>"),
        "/pt": ("pt-BR", "Harbour TMS", "Tradução para equipes", pt_nav +
                "<h1>Traduza cada página que publica</h1><p>Cada página, cada idioma, num só lugar.</p>"
                "<p><a href='/about'>Saiba mais</a></p>"),
        "/about": ("en", "About Harbour", "Who we are and why we do it", en_nav +
                   "<h1>About us</h1><p>We started in a small harbour office.</p>"
                   "<img src='/t.png' alt='The team at the harbour office'>"),
        "/pt/about": ("pt-BR", "Sobre a Harbour", "Who we are and why we do it", pt_nav +
                      "<h1>Sobre nós</h1><p>Começamos num pequeno escritório no porto.</p>"
                      "<img src='/t.png' alt='The team at the harbour office'>"),
        "/blog/one": ("en", "First post", "Our first post", en_nav + "<p>The first post on this blog.</p>"),
        "/pt/blog/one": 404,
        "/contact": ("en", "Contact", "Write to us", en_nav + "<p>Write to us at any time.</p>"),
        "/pt/contact": ("redirect", "/contact"),
        "/team": ("en", "Team", "The team", en_nav + TEAM),
        "/pt/team": ("pt-BR", "Equipe", "A equipe", pt_nav + TEAM),
    }


# Long enough to count as a page, and never translated.
TEAM = ("<p>Our team is spread over four time zones and meets every Monday morning to plan the week ahead.</p>"
        "<p>Each of us has worked in localization for years, on websites, apps, games and documentation.</p>")

PAIRS = {"/": "/pt", "/about": "/pt/about", "/blog/one": "/pt/blog/one", "/contact": "/pt/contact",
         "/team": "/pt/team"}


@pytest.fixture()
def site():
    pages = _site_pages()
    hits: dict[str, int] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            base = f"http://{self.headers['Host']}"
            path = self.path.split("?")[0]
            hits[path] = hits.get(path, 0) + 1
            if path == "/sitemap.xml":
                body = ["<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9' "
                        "xmlns:xhtml='http://www.w3.org/1999/xhtml'>"]
                for en, pt in PAIRS.items():
                    for loc in (en, pt):
                        body.append(f"<url><loc>{base}{loc}</loc>"
                                    f"<xhtml:link rel='alternate' hreflang='en' href='{base}{en}'/>"
                                    f"<xhtml:link rel='alternate' hreflang='x-default' href='{base}{en}'/>"
                                    f"<xhtml:link rel='alternate' hreflang='pt-BR' href='{base}{pt}'/></url>")
                body.append("</urlset>")
                return self._send(200, "".join(body), "application/xml")
            entry = pages.get(path)
            if entry is None or isinstance(entry, int):
                return self._send(entry or 404, "not found", "text/plain")
            if entry[0] == "redirect":
                self.send_response(301)
                self.send_header("Location", entry[1])
                self.end_headers()
                return
            lang, title, description, body = entry
            self._send(200, f"<html lang='{lang}'><head><title>{title}</title>"
                            f"<meta name='description' content='{description}'></head><body>{body}</body></html>",
                       "text/html; charset=utf-8")

        def _send(self, status, text, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(text.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield type("Site", (), {"url": f"http://127.0.0.1:{server.server_address[1]}", "pages": pages, "hits": hits})()
    server.shutdown()


@pytest.fixture()
def quiet(monkeypatch):
    """No emails, and one page at a time so the order is predictable."""
    sent = []

    async def record(check_id):
        sent.append(check_id)

    monkeypatch.setattr(tc, "_notify", record)
    monkeypatch.setattr(tc, "CONCURRENCY", 1)
    monkeypatch.setattr(tc, "SCREENSHOTS", 0)
    return sent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_site(env, url: str) -> int:
    check_id = run(env.db.create_translation_check(url, "manual", tc.RULES_VERSION, tc.OWNER, _now()))
    run(tc.run_check(check_id))
    return check_id


def findings(env, check_id: int) -> set[tuple[str, str, str]]:
    async def all_rows():
        return [row async for row in env.db.iter_translation_findings(check_id, (await env.db.get_translation_check(check_id))["site_url"])]
    return {(row["locale"], row["kind"], row["text"]) for row in run(all_rows())}


def test_a_check_finds_what_wasnt_translated(app_env, site, quiet):
    check_id = check_site(app_env, site.url)
    check = run(app_env.db.get_translation_check(check_id))
    assert check["status"] == "completed", check["error"]
    assert quiet == [check_id], "the admins are told"

    found = findings(app_env, check_id)
    assert ("pt-BR", "text", "Book a demo now") in found
    assert ("pt-BR", "meta", "Who we are and why we do it") in found
    assert ("pt-BR", "attribute", "The team at the harbour office") in found
    assert ("pt-BR", "english_link", "/about") in found
    assert ("pt-BR", "missing_page", f"{site.url}/blog/one") in found, "a 404"
    assert ("pt-BR", "missing_page", f"{site.url}/contact") in found, "redirects back to the original"
    assert not any(text.startswith("Sobre") or text.startswith("Traduza") for _, _, text in found)

    summary = json.loads(check["summary_json"])
    (pt,) = summary["locales"]
    assert (pt["code"], pt["checked"], pt["missing"]) == ("pt-BR", 3, 2)
    assert pt["in_english"] == 1, "/pt/team was never translated"
    assert 0 < pt["translated_pct"] < 100
    groups = run(app_env.db.translation_component_groups(check_id, site.url, tc.SECTIONS["components"]["kinds"]))
    assert [(g["label"], g["strings"], g["pages"]) for g in groups] == [("navbar", 1, 3)], "one fix, every page"


def test_the_next_check_says_what_was_fixed_and_what_is_new(app_env, site, quiet):
    first = check_site(app_env, site.url)
    for path in ("/pt", "/pt/about", "/pt/team"):
        lang, title, description, body = site.pages[path]
        body = body.replace("Book a demo now", "Agende uma demonstração")
        if path == "/pt/about":
            body += "<p>This paragraph was never translated, and you can see it on the page</p>"
        site.pages[path] = (lang, title, description, body)

    second = check_site(app_env, site.url)
    diff = json.loads(run(app_env.db.get_translation_check(second))["diff_json"])
    assert diff["previous_id"] == first
    assert (diff["new"], diff["fixed"]) == (1, 1)
    assert diff["new_top"][0]["text"] == "This paragraph was never translated, and you can see it on the page"


def test_keep_as_is_applies_per_language_or_everywhere(app_env, site, quiet, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    check_id = check_site(app_env, site.url)
    key = tc.text_key("Book a demo now")
    before = run(tc.summarize(run(app_env.db.get_translation_check(check_id))))["strings"]

    res = app_env.client.post(f"/admin/translations/{check_id}/ignore",
                              data={"key": key, "text": "Book a demo now", "locale": "es"}, follow_redirects=False)
    assert res.status_code == 303
    assert run(tc.summarize(run(app_env.db.get_translation_check(check_id))))["strings"] == before, "Spanish only"

    app_env.client.post(f"/admin/translations/{check_id}/ignore",
                        data={"key": key, "text": "Book a demo now", "locale": "pt-BR"})
    assert run(tc.summarize(run(app_env.db.get_translation_check(check_id))))["strings"] == before - 1
    report = app_env.client.get(f"/admin/translations/{check_id}?lang=pt-BR").text
    assert "navbar" not in report.split("Kept as is")[0], "the component has nothing else left"
    assert "Undo" in app_env.client.get(f"/admin/translations/{check_id}").text

    app_env.client.post(f"/admin/translations/{check_id}/ignore",
                        data={"key": key, "locale": "pt-BR", "undo": "1"})
    assert run(tc.summarize(run(app_env.db.get_translation_check(check_id))))["strings"] == before


def test_an_interrupted_check_carries_on_where_it_stopped(app_env, site, quiet, monkeypatch):
    check_id = run(app_env.db.create_translation_check(site.url, "manual", tc.RULES_VERSION, tc.OWNER, _now()))
    real = tc._check_group
    calls = {"n": 0}

    async def dies_on_the_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("the deploy restarted the process")
        return await real(*args, **kwargs)

    monkeypatch.setattr(tc, "_check_group", dies_on_the_second)
    with pytest.raises(RuntimeError):
        run(tc.run_check(check_id))
    rows = run(app_env.db.translation_check_rows(check_id))
    assert sum(1 for row in rows if row["locale"] == "" and row["status"] == "ok") == 1
    first_source = next(row["source_url"] for row in rows if row["locale"] == "" and row["status"] == "ok")
    first_path = urlsplit(first_source).path or "/"
    fetched_before = site.hits.get(first_path, 0)

    # A new process: the old one's lease has run out, and it takes over.
    monkeypatch.setattr(tc, "_check_group", real)
    run(app_env.db.release_translation_check(check_id, tc.OWNER))
    monkeypatch.setattr(tc, "OWNER", "the-next-process")
    claimed = run(app_env.db.claim_stale_translation_check(tc.OWNER, _now(), _now()))
    assert claimed["id"] == check_id and claimed["resume_count"] == 1
    run(tc.run_check(check_id))

    check = run(app_env.db.get_translation_check(check_id))
    assert check["status"] == "completed"
    assert site.hits.get(first_path, 0) == fetched_before, "a page already checked isn't fetched again"
    assert ("pt-BR", "text", "Book a demo now") in findings(app_env, check_id)


def test_a_check_another_process_is_running_isnt_taken_over(app_env):
    check_id = run(app_env.db.create_translation_check("https://site.test", "manual", 1, "process-a", _now()))
    stale_before = (datetime.now(timezone.utc) - timedelta(seconds=tc.LEASE_SECONDS)).isoformat()
    assert run(app_env.db.claim_stale_translation_check("process-b", _now(), stale_before)) is None
    assert not run(app_env.db.set_translation_plan(check_id, "process-b", {}, [], _now())), "nor written to"


def test_a_check_that_keeps_dying_is_given_up(app_env, quiet):
    check_id = run(app_env.db.create_translation_check("https://site.test", "manual", 1, "gone", _now()))

    async def exhaust():
        conn = app_env.db._conn()
        await conn.execute("UPDATE translation_checks SET resume_count = ?, heartbeat_at = '' WHERE id = ?",
                           (tc.MAX_RESUMES, check_id))
        await conn.commit()
        return await tc.claim_stale_check()

    assert run(exhaust()) is False
    check = run(app_env.db.get_translation_check(check_id))
    assert check["status"] == "failed" and "restarted" in check["error"]


def test_a_site_that_blocks_the_check_fails_it_rather_than_report_everything_fixed(app_env, site, quiet, monkeypatch):
    for path in list(site.pages):
        site.pages[path] = 503
    monkeypatch.setattr(tc, "FETCH_RETRIES", 0)
    monkeypatch.setattr(tc, "_FAILURE_MIN_SAMPLE", 3)
    check_id = run(app_env.db.create_translation_check(site.url, "manual", 1, tc.OWNER, _now()))
    run(tc._run_guarded(check_id))
    check = run(app_env.db.get_translation_check(check_id))
    assert check["status"] == "failed"
    assert "couldn't be fetched" in check["error"]


def test_cancel_stops_the_check(app_env, site, quiet):
    check_id = run(app_env.db.create_translation_check(site.url, "manual", 1, tc.OWNER, _now()))
    run(app_env.db.request_translation_cancel(check_id))
    run(tc._run_guarded(check_id))
    assert run(app_env.db.get_translation_check(check_id))["status"] == "cancelled"
    assert quiet == [], "no email for a cancel"


# ------------------------------------------------------------------ the weekly schedule

@pytest.fixture()
def schedule(monkeypatch):
    started = []

    async def fake_start(site_url, trigger="manual"):
        started.append((site_url, trigger))
        return 1

    monkeypatch.setattr(tc, "SITES", ["https://site.test"])
    monkeypatch.setattr(tc, "start_check", fake_start)
    return started


def _at(hour: int) -> datetime:
    return datetime.now(timezone.utc).replace(hour=hour, minute=5, second=0, microsecond=0)


NIGHT = _at(tc.HOUR_UTC)
DAY = _at((tc.HOUR_UTC + 6) % 24)


def _past_check(env, status: str, when: datetime) -> None:
    check_id = run(env.db.create_translation_check("https://site.test", "schedule", 1, tc.OWNER, when.isoformat()))
    if status != "running":
        run(env.db.finish_translation_check(check_id, tc.OWNER, status, when.isoformat()))


def test_the_first_check_waits_for_the_night_hour(app_env, schedule):
    run(tc.maybe_start_scheduled(DAY))
    assert schedule == []
    run(tc.maybe_start_scheduled(NIGHT))
    assert schedule == [("https://site.test", "schedule")]


def test_a_check_is_due_a_week_after_the_last(app_env, schedule):
    _past_check(app_env, "completed", NIGHT - timedelta(days=3))
    run(tc.maybe_start_scheduled(NIGHT))
    assert schedule == []


def test_a_week_old_check_is_repeated(app_env, schedule):
    _past_check(app_env, "completed", NIGHT - timedelta(days=7))
    run(tc.maybe_start_scheduled(NIGHT))
    assert schedule == [("https://site.test", "schedule")]


def test_a_failed_attempt_isnt_retried_the_same_night(app_env, schedule):
    _past_check(app_env, "completed", NIGHT - timedelta(days=8))
    _past_check(app_env, "failed", NIGHT - timedelta(hours=2))
    run(tc.maybe_start_scheduled(NIGHT))
    assert schedule == []


def test_an_overdue_check_doesnt_wait_for_the_hour(app_env, schedule):
    _past_check(app_env, "completed", DAY - timedelta(days=10))
    run(tc.maybe_start_scheduled(DAY))
    assert schedule == [("https://site.test", "schedule")]


def test_never_two_at_once(app_env, schedule):
    _past_check(app_env, "running", datetime.now(timezone.utc))
    run(tc.maybe_start_scheduled(NIGHT))
    assert schedule == []


# ------------------------------------------------------------------ the email

def test_the_email_says_what_changed_per_language(monkeypatch):
    from app import notifications

    captured = {}

    async def fake_send(to_email, subject, **context):
        captured.update(subject=subject, **context)

    monkeypatch.setenv("ADMIN_EMAILS", "a@b.c")
    monkeypatch.setattr(notifications, "_send_email", fake_send)
    check = {
        "id": 7, "site_url": "https://wxrks.com", "status": "completed", "started_at": _now(),
        "summary_json": json.dumps({"translated_pct": 81.5, "strings": 312, "missing": 121, "locales": [
            {"code": "pt-BR", "name": "Portuguese (Brazil)", "translated_pct": 79.2, "missing": 121, "new": 3, "fixed": 9},
        ]}),
        "diff_json": json.dumps({"previous_id": 6, "rules_changed": False, "new": 3, "fixed": 9,
                                 "new_top": [{"locale": "pt-BR", "kind": "text", "text": "For Creative Teams",
                                              "pages": 1069}]}),
    }
    run(notifications.send_translation_report(check))
    assert captured["subject"] == "wxrks.com translation check: 3 new, 9 fixed"
    assert ("3", "New") in captured["hero_stats"] and ("81.5%", "Translated") in captured["hero_stats"]
    assert captured["stats"] == [("Portuguese (Brazil)", "79.2% translated · 121 pages missing · +3 / −9")]
    assert "For Creative Teams" in captured["notice"]
    assert captured["cta_url"].endswith("/admin/translations/7") or captured["cta_url"] is None


# ------------------------------------------------------------------ the admin pages

def test_the_pages_are_admin_only(app_env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "someone-else@x.c")
    assert app_env.client.get("/admin/translations").status_code == 404
    assert app_env.client.get("/admin/translations/1").status_code == 404
    assert app_env.client.post("/admin/translations/start", data={"url": "https://x.com"}).status_code == 404


def test_the_report_and_its_exports(app_env, site, quiet, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    check_id = check_site(app_env, site.url)

    listing = app_env.client.get("/admin/translations").text
    assert f"/admin/translations/{check_id}" in listing

    report = app_env.client.get(f"/admin/translations/{check_id}").text
    for heading in ("By language", "Fix once: components", "Titles &amp; descriptions", "Image &amp; UI text",
                    "Missing pages", "Links to the original language"):
        assert heading in report, heading
    assert "navbar" in report and "Portuguese (Brazil)" in report

    drill = app_env.client.get(f"/admin/translations/{check_id}?section=components&component=nav-1").text
    assert "Book a demo now" in drill

    csv = app_env.client.get(f"/admin/translations/{check_id}/findings.csv?lang=pt-BR").text
    assert csv.startswith("Language,Kind,Field,Text,Pages,Words,Where,Component,Example page,Original page,New")
    assert "Book a demo now" in csv
    pages_csv = app_env.client.get(f"/admin/translations/{check_id}/pages.csv").text
    assert pages_csv.count("\n") == 1 + 5, "a row per language version"


def test_check_now_rejects_what_isnt_a_site(app_env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    assert app_env.client.post("/admin/translations/start", data={"url": "not a site"}).status_code == 400


def test_check_now_while_one_runs_says_so(app_env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)

    async def busy(*args, **kwargs):
        raise RuntimeError("A translation check is already running")

    monkeypatch.setattr(tc, "start_check", busy)
    res = app_env.client.post("/admin/translations/start", data={"url": "wxrks.com"}, follow_redirects=False)
    assert res.headers["location"] == "/admin/translations?busy=1"
    assert "already running" in app_env.client.get("/admin/translations?busy=1").text


# ------------------------------------------------------------------ key pages

def test_key_pages_are_home_then_the_menus_top_level_then_its_dropdowns():
    home = page("<nav class='w-nav'><a href='/'>Home</a>"
                "<div class='w-dropdown'><nav class='w-dropdown-list'><a href='/platform/editor'>Editor</a></nav></div>"
                "<a href='/pricing'>Pricing</a><a href='/contact'>Contact</a><a href='https://elsewhere.test/x'>Out</a>"
                "</nav><main><a href='/blog'>Blog</a></main>", url="https://site.test/", lang="en")
    sources = ["https://site.test/", "https://site.test/blog", "https://site.test/contact",
               "https://site.test/platform/editor", "https://site.test/pricing"]
    assert tc.key_pages("https://site.test", home, sources) == [
        "https://site.test/", "https://site.test/pricing", "https://site.test/contact",
        "https://site.test/platform/editor"], "not /blog: it isn't in the menu"


def test_a_page_never_translated_is_listed_and_can_be_kept(app_env, site, quiet, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    check_id = check_site(app_env, site.url)
    report = app_env.client.get(f"/admin/translations/{check_id}").text
    assert "Pages still in English" in report
    assert f"{site.url}/team".replace("http://", "") in report.split("Pages still in English")[1]

    app_env.client.post(f"/admin/translations/{check_id}/ignore",
                        data={"key": f"page:{site.url}/team", "text": f"{site.url}/team", "locale": "pt-BR"})
    summary = run(tc.summarize(run(app_env.db.get_translation_check(check_id))))
    assert summary["in_english"] == 0
    assert "Pages still in English" not in app_env.client.get(f"/admin/translations/{check_id}").text


def test_the_key_pages_grid_and_a_screenshot_with_the_gaps_outlined(app_env, site, quiet, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    monkeypatch.setattr(tc, "SCREENSHOTS", 5)
    check_id = check_site(app_env, site.url)
    shots = run(app_env.db.translation_shots(check_id))
    if shots and all(shot["error"] == "The browser couldn't start" for shot in shots):
        pytest.skip("no browser here")
    by_path = {urlsplit(shot["source_url"]).path or "/": shot for shot in shots}
    assert set(by_path) == {"/", "/about"}, "home, then what the menu links to"
    home = by_path["/"]
    assert home["status"] == "ok" and home["marks"] >= 1, home

    report = app_env.client.get(f"/admin/translations/{check_id}").text
    assert "Key pages" in report and "tc-shot-dot" in report
    view = app_env.client.get(f"/admin/translations/{check_id}",
                              params={"section": "key", "source": home["source_url"], "lang": "pt-BR"}).text
    assert f"/admin/translations/{check_id}/shots/{home['file']}" in view
    assert "Book a demo now" in view, "the page's own list, beside the screenshot"
    image = app_env.client.get(f"/admin/translations/{check_id}/shots/{home['file']}")
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    assert image.content[:2] == b"\xff\xd8"


def test_screenshot_names_are_checked(app_env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    assert app_env.client.get("/admin/translations/1/shots/..%2F..%2Ft.db").status_code == 404
    assert app_env.client.get("/admin/translations/1/shots/notes.txt").status_code == 404


# ------------------------------------------------------------------ viewers

def test_a_viewer_sees_the_reports_but_not_the_rest_of_admin(app_env, site, quiet, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    check_id = check_site(app_env, site.url)
    monkeypatch.setenv("TRANSLATION_CHECK_VIEWERS", f"someone@x.c, {app_env.other.email.upper()}")
    app_env.current["user"] = app_env.other

    menu = app_env.client.get("/").text
    assert ">Translation check<" in menu and ">Server health<" not in menu
    assert app_env.client.get("/admin/translations").status_code == 200
    assert "Check now" not in app_env.client.get("/admin/translations").text
    assert app_env.client.get(f"/admin/translations/{check_id}").status_code == 200
    assert app_env.client.get(f"/admin/translations/{check_id}/findings.csv").status_code == 200
    assert app_env.client.post(f"/admin/translations/{check_id}/ignore",
                               data={"key": "k", "text": "x", "locale": "pt-BR"},
                               follow_redirects=False).status_code == 303, "they can mark Keep as is"
    assert app_env.client.post("/admin/translations/start", data={"url": "https://x.com"}).status_code == 404
    assert app_env.client.post(f"/admin/translations/{check_id}/cancel").status_code == 404
    assert app_env.client.get("/admin/runs").status_code == 404


def test_nobody_else_sees_them(app_env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", app_env.owner.email)
    monkeypatch.setenv("TRANSLATION_CHECK_VIEWERS", "someone@x.c")
    app_env.current["user"] = app_env.other
    assert ">Translation check<" not in app_env.client.get("/").text
    assert app_env.client.get("/admin/translations").status_code == 404


def test_viewers_get_the_email_too(monkeypatch):
    from app import notifications

    sent = []

    async def fake_send(to_email, subject, **context):
        sent.append(to_email)

    monkeypatch.setenv("ADMIN_EMAILS", "admin@x.c")
    monkeypatch.setenv("TRANSLATION_CHECK_VIEWERS", "fixer@x.c, admin@x.c")
    monkeypatch.setattr(notifications, "_send_email", fake_send)
    run(notifications.send_translation_report({"id": 1, "site_url": "https://wxrks.com", "status": "failed",
                                               "started_at": _now(), "error": "Stopped"}))
    assert sent == ["admin@x.c", "fixer@x.c"], "once each"
