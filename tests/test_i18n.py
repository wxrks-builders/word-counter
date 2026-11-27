"""Tests for the ten languages.

The catalogues themselves are empty until wxrks fills them, so nothing here
asserts a translation. What it asserts is the plumbing: that a language is
reachable, that it doesn't change which route answers, that numbers follow the
reader rather than the developer, and that Arabic mirrors.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))

import pytest  # noqa: E402

from app import i18n, surfaces  # noqa: E402


@pytest.fixture()
def i18n_client(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "i.db"))
    monkeypatch.setenv("MARKDOWN_DIR", str(tmp_path / "markdown"))
    import importlib

    import app.db as db
    import app.main as main
    importlib.reload(db)
    importlib.reload(main)
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        client.headers["host"] = surfaces.COUNTER.host
        yield client


class TestTheLanguagesAreTheOnesWxrksPublishes:
    def test_ten_plus_english(self):
        assert set(i18n.LANGUAGES) == {"en", "ar", "de", "es", "fr", "it", "ja", "ko", "pt", "sv", "zh"}

    def test_every_language_has_a_name_in_its_own_language(self):
        """A picker labelled in English is no use to someone looking for 日本語."""
        for code in i18n.LANGUAGES:
            assert i18n.LANGUAGE_NAMES.get(code), code

    def test_hreflang_carries_the_regional_codes(self):
        """wxrks.com declares de-DE, fr-FR and pt-BR against short paths. A
        search engine told 'de' by one property and 'de-DE' by another treats
        them as different targets, which is what hreflang exists to prevent."""
        assert i18n.HREFLANG["de"] == "de-DE"
        assert i18n.HREFLANG["fr"] == "fr-FR"
        assert i18n.HREFLANG["pt"] == "pt-BR"

    def test_every_language_has_a_catalogue_on_disk(self):
        for code in i18n.LANGUAGES:
            if code == i18n.DEFAULT_LANG:
                continue
            po = i18n.LOCALE_DIR / code / "LC_MESSAGES" / "messages.po"
            assert po.exists(), f"{code} has no catalogue"


class TestPathRouting:
    def test_a_language_prefix_is_stripped(self):
        assert i18n.split_path("/es/runs") == ("es", "/runs")
        assert i18n.split_path("/zh/crawl/abc123") == ("zh", "/crawl/abc123")

    def test_english_lives_at_the_root(self):
        """/en/ is not a path — it would be a second URL for the same page,
        which is the duplicate-content problem hreflang is meant to avoid."""
        assert i18n.split_path("/runs") == ("en", "/runs")
        assert i18n.split_path("/en/runs") == ("en", "/en/runs")

    def test_an_unknown_prefix_is_left_alone(self):
        """So it 404s as a missing page rather than being swallowed as a
        language nobody offers."""
        assert i18n.split_path("/nope/runs") == ("en", "/nope/runs")
        assert i18n.split_path("/crawl/es") == ("en", "/crawl/es")

    def test_building_a_link_round_trips(self):
        for code in i18n.LANGUAGES:
            built = i18n.localized_path("/runs", code)
            assert i18n.split_path(built) == (code, "/runs"), code

    def test_static_assets_are_never_prefixed(self):
        """Each language would otherwise miss the browser cache for a file they
        all share."""
        for code in i18n.LANGUAGES:
            assert i18n.localized_path("/static/app.js", code) == "/static/app.js"


class TestNumbersFollowTheReader:
    def test_the_product_is_a_number_so_it_is_formatted_per_locale(self):
        assert i18n.format_number(662431, "en") == "662,431"
        assert i18n.format_number(662431, "de") == "662.431"
        assert i18n.format_number(662431, "pt") == "662.431"

    def test_french_and_swedish_group_with_spaces(self):
        for code in ("fr", "sv"):
            grouped = i18n.format_number(662431, code)
            assert grouped.replace(" ", " ").replace("\xa0", " ") == "662 431", code

    def test_every_language_formats_without_raising(self):
        for code in i18n.LANGUAGES:
            assert i18n.format_number(1234567, code)


class TestArabicMirrors:
    def test_only_arabic_is_right_to_left(self):
        assert i18n.RTL_LANGUAGES == {"ar"}

    def test_the_stylesheet_has_no_physical_directions_left(self):
        """Logical properties are what let one stylesheet serve both
        directions; a physical one silently doesn't mirror."""
        import re

        css = (Path(__file__).resolve().parents[1] / "app/static/style.css").read_text()
        offenders = re.findall(
            r"(?:margin|padding|border)-(?:left|right)\s*:|text-align:\s*(?:left|right)\b|^\s*(?:left|right)\s*:",
            css, re.M,
        )
        assert offenders == [], f"{len(offenders)} physical properties would not mirror"


class TestTheCatalogueIsComplete:
    def test_the_javascript_strings_are_in_the_same_catalogue(self):
        """A second front-end catalogue would mean two extractions and two
        places to send a translator."""
        from app import js_strings

        pot = (i18n.LOCALE_DIR / "messages.pot").read_text()
        for value in js_strings.catalogue("en").values():
            texts = [value] if isinstance(value, str) else [v for k, v in value.items() if not k.startswith("_")]
            for text in texts:
                first_line = text.split("\n")[0].replace('"', '\\"')
                assert first_line[:40] in pot, f"missing from catalogue: {text[:40]}"

    def test_the_landing_copy_is_extracted(self):
        """It is built at import, before any request has a language, so it is
        marked with N_() where it is written and translated at render."""
        from app import surfaces

        pot = (i18n.LOCALE_DIR / "messages.pot").read_text()
        assert surfaces.COUNTER.headline[:40] in pot
        assert surfaces.COUNTER.points[0][0][:30] in pot


class TestPseudoLocale:
    def test_it_wraps_instead_of_translating(self):
        """Its whole job is to make an unextracted string visible: anything
        still bare on a /zz/ page is hard-coded English."""
        assert i18n.gettext_for(i18n.PSEUDO_LANG)("Total words") == "⟦Total words⟧"

    def test_it_is_off_unless_asked_for(self):
        assert i18n.PSEUDO_LANG not in i18n.LANGUAGES


class TestTheSwitcher:
    """Routing without a picker means typing URLs, which is not a feature."""

    def _links(self, html):
        import re

        return {n.strip(): h for h, _, n in re.findall(
            r'<a[^>]*href="([^"]*)"[^>]*hreflang="([^"]+)"[^>]*>\s*<span[^>]*>([^<]+)</span>', html)}

    def test_it_keeps_you_on_the_page_you_are_on(self, i18n_client):
        """Landing somewhere else for choosing a language loses what you were
        reading, which is a small betrayal at exactly the wrong moment."""
        links = self._links(i18n_client.get("/de/login").text)
        assert links["Français"] == "/fr/login"
        assert links["日本語"] == "/ja/login"
        assert links["English"] == "/login", "English is the root, so it has no prefix"

    def test_every_language_is_offered_and_named_in_itself(self, i18n_client):
        links = self._links(i18n_client.get("/").text)
        assert len(links) == len(i18n.LANGUAGES)
        assert "العربية" in links and "日本語" in links and "Deutsch" in links

    def test_the_current_one_is_marked(self, i18n_client):
        assert 'aria-current="true"' in i18n_client.get("/de/login").text

    def test_signed_out_visitors_get_one(self, i18n_client):
        """The public pages are where translation earns its traffic, and the
        same control serves them — not a second, different one."""
        for path in ("/", "/login"):
            html = i18n_client.get(path).text
            assert 'class="lang-menu"' in html, path
            assert "initUserMenu" in html, f"{path} — nothing would open it"

    def test_it_stands_alone_rather_than_living_in_the_account_menu(self, i18n_client):
        """Its own control, so the current language is legible without opening
        anything — which is the whole reason for separating it."""
        html = i18n_client.get("/de/login").text
        assert ">DE<" in html, "the trigger names the current language"
        assert 'data-menu-button' in html

    def test_the_trigger_names_the_current_language(self, i18n_client):
        import re

        for path, expected in (("/", "EN"), ("/de/login", "DE"), ("/ar/login", "AR")):
            html = i18n_client.get(path).text
            assert re.search(r'lang-code">([^<]+)', html).group(1) == expected, path


class TestEmailsFollowTheirReader:
    def test_the_language_is_remembered_on_the_account(self):
        """Emails are sent from background tasks with no request to read a
        prefix from, so browsing is the only moment the choice is observable."""
        from app.models import User

        assert User(id=1, google_sub="s", email="a@b.c", name="A").lang == "en"

    def test_the_crawl_email_uses_the_owners_language(self):
        import inspect

        from app import crawler

        assert "lang=user.lang," in inspect.getsource(crawler.run_crawl)

    def test_the_share_email_uses_the_senders(self):
        """Whoever receives it has no account to hold a preference, and the
        person choosing to share is the one whose words introduce it."""
        import inspect

        from app import main

        assert "lang = _reader_lang(request, user)" in inspect.getsource(main.email_share)
        assert "lang=lang," in inspect.getsource(main.email_share)

    def test_the_reader_is_the_page_the_script_ran_on(self):
        """app.js sends the page's prefix; an old cached copy sends none, and
        the Referer still names the page."""
        from types import SimpleNamespace

        from app import main

        def request(lang, referer=""):
            return SimpleNamespace(state=SimpleNamespace(lang=lang), headers={"referer": referer},
                                   url=SimpleNamespace(netloc="wordcount.wxrks.com"))

        assert main._reader_lang(request("de")) == "de"
        assert main._reader_lang(request("en", "https://wordcount.wxrks.com/ja/crawl/x")) == "ja"
        assert main._reader_lang(request("en", "https://elsewhere.example/ja/crawl/x")) == "en"

    def test_email_numbers_are_grouped_for_the_reader_too(self):
        from app.notifications import _num

        assert _num(662431, "en") == "662,431"
        assert _num(662431, "de") == "662.431"


class TestNothingLeaksMarkup:
    def test_the_home_pitch_bolds_the_number_rather_than_printing_tags(self, i18n_client, monkeypatch):
        """newstyle gettext escapes interpolated values, so markup has to be
        marked safe on the parameter — |safe on the result arrives too late and
        blesses text that already reads '&lt;strong&gt;'."""
        import asyncio

        import app.db as db
        import app.main as main
        from app.auth import get_current_user, require_user, require_user_api
        from app.models import PageResult, User

        u = User(id=1, google_sub="a", email="o@x.c", name="R")
        for dep in (require_user, require_user_api, get_current_user):
            main.app.dependency_overrides[dep] = lambda: u

        def run(coro):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        run(db.get_or_create_user("a", "o@x.c", "R", None))
        run(db.save_run(run_id="r", source_url="https://wxrks.com", user_id=1,
                        status="completed", total_words=1_076_780,
                        pages=[PageResult(url="https://wxrks.com/a", word_count=1076780)],
                        limit_reached=False))
        html = i18n_client.get("/").text
        main.app.dependency_overrides.clear()
        assert "&lt;strong&gt;" not in html
        assert "<strong>" in html


class TestAssetsBustTheCache:
    def test_static_references_carry_a_version(self, i18n_client):
        """Browsers cache /static and nothing ever told them a deploy happened,
        so every deploy shipped new HTML against week-old CSS and JS — which is
        how a new menu renders as an unstyled button that won't open."""
        html = i18n_client.get("/login").text
        assert "style.css?v=" in html
        assert "app.js?v=" in html

    def test_the_version_changes_when_the_files_do(self, tmp_path, monkeypatch):
        from app import templates as t

        monkeypatch.setattr(t, "_STATIC_DIR", tmp_path)
        (tmp_path / "style.css").write_text("a{}")
        (tmp_path / "app.js").write_text("//1")
        first = t._asset_version()
        (tmp_path / "app.js").write_text("//2")
        assert t._asset_version() != first


class TestCataloguesStaySound:
    """The import script validates a batch; these validate forever — including
    hand-edits that never went through the script."""

    def test_every_translated_string_keeps_its_placeholders(self):
        """A translation that drops or renames a %(name)s raises at RENDER
        time: the page using it 500s, in that language only. This is the
        render-crash class caught at commit instead."""
        import re

        from babel.messages.pofile import read_po

        ph = re.compile(r"%\([a-zA-Z_]+\)s")
        problems = []
        for po in sorted(i18n.LOCALE_DIR.glob("*/LC_MESSAGES/messages.po")):
            with po.open("rb") as handle:
                for message in read_po(handle):
                    if not message.id:
                        continue
                    if isinstance(message.id, str):
                        if message.string and sorted(ph.findall(message.id)) != sorted(ph.findall(message.string)):
                            problems.append(f"{po.parent.parent.name}: {message.id[:60]!r}")
                        continue
                    # A plural form may say its number in words and leave it
                    # out (Arabic "one page"), never add one the source lacks.
                    allowed = set(ph.findall(message.id[-1]))
                    for form in message.string or ():
                        if form and set(ph.findall(form)) - allowed:
                            problems.append(f"{po.parent.parent.name}: {message.id[0][:60]!r}")
        assert problems == [], problems

    def test_the_pot_is_not_stale_against_the_code(self):
        """A stale catalogue is how a translator works on last week's strings
        — the export refuses it, and this keeps the refusal honest in CI."""
        import subprocess
        import sys as _sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import translations

        assert translations._pot_is_stale() is False, (
            "run: pybabel extract -F babel.cfg -o locales/messages.pot --no-location --sort-output "
            "--add-comments=Translators: . && pybabel update -i locales/messages.pot -d locales"
        )


class TestPseudoSweep:
    """Renders the public pages under /zz/ and asserts nothing user-facing is
    bare — a future feature with unmarked strings fails here instead of
    shipping English into ten languages."""

    ALLOWED = {
        # Named in themselves by design (the language picker), product names,
        # and data the page is about.
        "wxrks", "Word Counter", "Site to Markdown", "EN", "ZZ", "Pro", "Admin",
        "English", "Deutsch", "Español", "Français", "Italiano", "Português",
        "Svenska", "العربية", "日本語", "한국어", "中文", "Example", "R", "Rodrigo",
    }

    def _bare(self, html):
        import re

        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
        # Data the page shows rather than says — a hostname, a file name, a
        # format — is marked translate="no", which browser translators honour too.
        text = re.sub(r'<(\w+)\b[^>]*\btranslate="no"[^>]*>.*?</\1>', " ", text, flags=re.S)
        text = re.sub(r"<[^>]+>", "\n", text)
        found = []
        for line in text.splitlines():
            t = line.strip()
            if not t or "⟦" in t or "⟧" in t:
                continue
            if re.fullmatch(r"[\d,.\s·%×—:–\-()/&;+←→▾▸⋯×#]*", t):
                continue
            if re.match(r"https?://|[\w.-]+@[\w.-]+|^/", t):
                continue
            if t in self.ALLOWED or len(t) < 3:
                continue
            found.append(t)
        return sorted(set(found))

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
            client.headers["host"] = surfaces.COUNTER.host
            for path in ("/zz/", "/zz/login"):
                bare = self._bare(client.get(path).text)
                # The <title> line joins name and tagline outside markers.
                bare = [b for b in bare if "Word Counter" not in b]
                assert bare == [], f"{path}: unextracted strings {bare}"


def _install_catalog(monkeypatch, tmp_path, lang, messages):
    """A compiled catalogue for `lang` holding `messages` ({msgid: str} or
    {(singular, plural): (form, …)}), in place of whatever is on disk — so a
    test can see a translation without wxrks having delivered one."""
    import gettext

    from babel.messages.catalog import Catalog
    from babel.messages.mofile import write_mo

    catalog = Catalog(locale=i18n.HREFLANG.get(lang, lang).replace("-", "_"))
    for msgid, string in messages.items():
        catalog.add(msgid, string)
    path = tmp_path / f"{lang}.mo"
    with path.open("wb") as handle:
        write_mo(handle, catalog)
    with path.open("rb") as handle:
        monkeypatch.setitem(i18n._translations, lang, gettext.GNUTranslations(handle))


class TestPlurals:
    """English picks a form with n == 1; Arabic has six, French counts 0 as
    singular, Japanese has one. Only the catalogue's own rule gets it right."""

    PAGE = ("%(n)s page", "%(n)s pages")

    def test_templates_use_the_catalogues_rule(self, monkeypatch, tmp_path):
        from app import templates as t

        _install_catalog(monkeypatch, tmp_path, "fr", {self.PAGE: ("%(n)s page FR", "%(n)s pages FR")})
        try:
            t._install_language("fr")
            render = t.templates.env.from_string("{{ ngettext('%(n)s page', '%(n)s pages', count, n=count) }}").render
            assert render(count=0) == "0 page FR", "French puts 0 with 1"
            assert render(count=2) == "2 pages FR"
        finally:
            t._install_language(i18n.DEFAULT_LANG)

    def test_the_browser_gets_one_form_per_category(self, monkeypatch, tmp_path):
        forms = tuple(f"form{i} %(n)s" for i in range(6))
        _install_catalog(monkeypatch, tmp_path, "ar", {self.PAGE: forms})
        variants = i18n.plural_variants("ar", *self.PAGE)
        assert set(variants) == {"zero", "one", "two", "few", "many", "other"}
        assert len(set(variants.values())) == 6, "each category its own form"

    def test_untranslated_plurals_are_picked_by_english_rules(self):
        """Else a French page would read "1 pages" until wxrks delivers."""
        variants = i18n.plural_variants("fr", *self.PAGE)
        assert variants == {"one": "%(n)s page", "other": "%(n)s pages", "_rules": "en"}

    def test_the_compile_leaves_untranslated_messages_out(self, tmp_path):
        """pybabel compile fills an untranslated plural with the English pair,
        so Arabic, which uses form 1 for n == 1, showed "1 pages", and every
        plural looked translated to plural_variants. The project's compile
        leaves it out, and gettext falls back to English by English rules."""
        import gettext

        from babel.messages.catalog import Catalog
        from babel.messages.pofile import write_po

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import translations

        po = tmp_path / "ar" / "LC_MESSAGES" / "messages.po"
        po.parent.mkdir(parents=True)
        catalog = Catalog(locale="ar")
        catalog.add(self.PAGE, ("",) * 6)
        catalog.add(("%(n)s word", "%(n)s words"), tuple(f"form{i} %(n)s" for i in range(6)))
        catalog.add(("%(n)s site", "%(n)s sites"), ("form0 %(n)s", "", "", "", "", ""))
        catalog.add("Save", "")
        catalog.add("Close", "a guess", flags=["fuzzy"])
        with po.open("wb") as handle:
            write_po(handle, catalog)
        mo = po.with_suffix(".mo")

        assert translations.compile_catalog(po, mo) == (1, 4)
        with mo.open("rb") as handle:
            ar = gettext.GNUTranslations(handle)
        assert ar.ngettext(*self.PAGE, 1) == "%(n)s page", "English by English rules, not 1 pages"
        assert ar.ngettext(*self.PAGE, 2) == "%(n)s pages"
        assert ar.ngettext("%(n)s word", "%(n)s words", 1) == "form1 %(n)s", "translated: Arabic rules"
        assert ar.ngettext("%(n)s site", "%(n)s sites", 1) == "%(n)s site", "half-filled counts as untranslated"
        assert ar.gettext("Close") == "Close", "fuzzy stays out"

    def test_the_samples_agree_with_every_catalogues_plural_forms(self):
        """The browser picks a category with CLDR (Intl.PluralRules); the server
        fills each category from the catalogue's gettext rule at one sample. Any
        number must land on the same form both ways, in every language."""
        import gettext
        import re

        for code in i18n.LANGUAGES:
            if code == i18n.DEFAULT_LANG:
                continue
            header = (i18n.LOCALE_DIR / code / "LC_MESSAGES" / "messages.po").read_text()
            expression = re.search(r"plural=(.*?);?\\n", header.replace('"\n"', "")).group(1)
            rule = gettext.c2py(expression)
            samples = dict(i18n.plural_samples(code))
            locale = i18n.locale_for(code)
            for n in [*range(0, 2000), 1_000_000, 2_000_000]:
                assert rule(n) == rule(samples[locale.plural_form(n)]), f"{code}: {n}"

    def test_no_english_only_plural_logic_is_left(self):
        """`n === 1 ? "1 crawl" : "%(n)s crawls"` is the English rule wearing a
        disguise. (Two different sentences for one site and several are fine.)"""
        import re

        root = Path(__file__).resolve().parents[1] / "app"
        offenders = []
        js = (root / "static/app.js").read_text()
        offenders += [f"app.js: {m}" for m in re.findall(r".*={2,3} ?1 \?.*_one.*", js)]
        for template in (root / "templates").glob("*.html"):
            if template.name.startswith("admin_"):
                continue
            for m in re.findall(r"\{%-? if [^%]*== ?1 -?%\}\{\{ _\(['\"]1 ", template.read_text()):
                offenders.append(f"{template.name}: {m}")
        assert offenders == [], offenders

    def test_every_catalogue_states_the_rule_of_the_language_it_serves(self):
        """pt serves Brazilian Portuguese (pt-BR), where 0 is singular; the
        catalogue had Portugal's rule, so "1 páginas" would have shipped."""
        import re

        header = (i18n.LOCALE_DIR / "pt" / "LC_MESSAGES" / "messages.po").read_text()
        assert re.search(r'"Language: pt_BR\\n"', header)
        assert "plural=(n > 1);" in header


class TestDurations:
    def test_english_reads_as_it_always_has(self):
        assert i18n.format_duration(30, "en") == "less than a minute"
        assert i18n.format_duration(25 * 60, "en") == "~25 min"
        assert i18n.format_duration(68 * 60, "en") == "~1h 8m"
        assert i18n.format_duration(3 * 3600, "en") == "~3h"

    def test_the_numbers_in_it_are_the_readers(self):
        assert i18n.format_duration(1500 * 3600, "de") == "~1.500h"


class TestTheLanguageChoice:
    """What counts as choosing a language — the preference emails are sent in."""

    def _request(self, path_lang="en", accept="text/html", cookie=None, method="GET"):
        from types import SimpleNamespace

        return SimpleNamespace(method=method, headers={"accept": accept}, state=SimpleNamespace(lang=path_lang),
                               cookies={i18n.LANG_COOKIE: cookie} if cookie else {})

    def test_a_page_in_a_language_is_a_choice(self):
        assert i18n.chosen_language(self._request("de")) == "de"

    def test_the_scripts_fetches_are_not(self):
        """app.js calls unprefixed paths; reading those as English reset every
        German account to English the moment a crawl page opened."""
        assert i18n.chosen_language(self._request("en", accept="*/*")) is None
        assert i18n.chosen_language(self._request("en", accept="text/event-stream")) is None
        assert i18n.chosen_language(self._request("de", method="POST")) is None

    def test_the_english_root_is_only_a_choice_when_picked(self):
        assert i18n.chosen_language(self._request("en")) is None
        assert i18n.chosen_language(self._request("en", cookie="en")) == "en"

    def test_the_account_remembers_only_choices(self):
        import inspect

        from app import auth

        assert "i18n.chosen_language(request)" in inspect.getsource(auth.get_current_user)

    def test_accept_language_is_read_by_quality(self):
        pick = i18n.preferred_language
        assert pick("de-AT,de;q=0.9,en;q=0.8") == "de"
        assert pick("en-US,en;q=0.9,de;q=0.5") == "en"
        assert pick("fr;q=0.2,ja;q=0.8") == "ja"
        assert pick("de;q=0, en") == "en"
        assert pick("*") is None and pick("") is None and pick(None) is None
        assert pick("nl, pl") is None

    def test_traditional_chinese_is_not_offered_simplified(self):
        assert i18n.preferred_language("zh-TW") is None
        assert i18n.preferred_language("zh-Hant-HK") is None
        assert i18n.preferred_language("zh-CN") == "zh"


class TestSearchEngines:
    def _canonical(self, html):
        import re

        return re.search(r'rel="canonical" href="([^"]+)"', html).group(1)

    def _hreflangs(self, html):
        import re

        return dict(re.findall(r'<link rel="alternate" hreflang="([^"]+)" href="([^"]+)"', html))

    def test_an_untranslated_language_is_not_advertised(self, i18n_client, monkeypatch):
        """Until wxrks delivers, /de/ is English under another URL: pointing it
        at English keeps ten duplicates out of the index."""
        monkeypatch.setattr(i18n, "published_languages", lambda: ("en",))
        html = i18n_client.get("/de/login").text
        assert self._canonical(html).endswith("/login") and "/de/" not in self._canonical(html)
        assert set(self._hreflangs(html)) == {"en", "x-default"}

    def test_a_published_language_is_its_own_canonical(self, i18n_client, monkeypatch):
        """request.url.path is the stripped path, so the canonical used to send
        every language to English — cancelling its own hreflang."""
        monkeypatch.setattr(i18n, "published_languages", lambda: ("en", "de"))
        html = i18n_client.get("/de/login").text
        assert self._canonical(html).endswith("/de/login")
        links = self._hreflangs(html)
        assert links["de-DE"].endswith("/de/login") and links["en"].endswith("/login")
        assert links["x-default"] == links["en"]

    def test_robots_keeps_every_languages_private_pages_out(self, i18n_client):
        robots = i18n_client.get("/robots.txt").text
        for path in ("/share/", "/de/share/", "/ja/crawl/", "/ar/projects"):
            assert f"Disallow: {path}\n" in robots, path

    def test_the_sitemap_lists_each_published_language(self, i18n_client, monkeypatch):
        monkeypatch.setattr(i18n, "published_languages", lambda: ("en", "de"))
        sitemap = i18n_client.get("/sitemap.xml").text
        assert "/de/mcp</loc>" in sitemap and 'hreflang="x-default"' in sitemap
        assert "/fr/" not in sitemap, "not published, not listed"


class TestTheSuggestion:
    """An English page offers the visitor's own language, in that language."""

    PAGE = {"accept": "text/html", "accept-language": "de-DE,de;q=0.9,en;q=0.5"}

    def test_it_offers_a_published_language_the_browser_prefers(self, i18n_client, monkeypatch):
        monkeypatch.setattr(i18n, "published_languages", lambda: ("en", "de"))
        html = i18n_client.get("/login", headers=self.PAGE).text
        assert 'class="lang-suggest" lang="de-DE"' in html
        assert 'href="/de/login"' in html

    def test_it_never_offers_an_untranslated_one(self, i18n_client, monkeypatch):
        monkeypatch.setattr(i18n, "published_languages", lambda: ("en",))
        assert "lang-suggest" not in i18n_client.get("/login", headers=self.PAGE).text

    def test_staying_in_english_is_remembered(self, i18n_client, monkeypatch):
        monkeypatch.setattr(i18n, "published_languages", lambda: ("en", "de"))
        i18n_client.cookies.set(i18n.LANG_COOKIE, "en")
        assert "lang-suggest" not in i18n_client.get("/login", headers=self.PAGE).text

    def test_it_is_not_shown_on_a_page_already_in_a_language(self, i18n_client, monkeypatch):
        monkeypatch.setattr(i18n, "published_languages", lambda: ("en", "de"))
        assert "lang-suggest" not in i18n_client.get("/ja/login", headers=self.PAGE).text

    def test_opening_a_language_remembers_it(self, i18n_client):
        response = i18n_client.get("/de/login", headers={"accept": "text/html"})
        assert f"{i18n.LANG_COOKIE}=de" in response.headers.get("set-cookie", "")


class TestRedirectsKeepTheLanguage:
    def test_signing_in_is_asked_in_the_readers_language(self, i18n_client):
        response = i18n_client.get("/de/runs", follow_redirects=False)
        assert response.headers["location"] == "/de/login"

    def test_signing_out_lands_in_it_too(self, i18n_client):
        response = i18n_client.get("/ja/logout", follow_redirects=False)
        assert response.headers["location"] == "/ja/login"

    def test_the_sign_in_page_shows_only_its_own_errors(self, i18n_client):
        """The message used to come from the address bar, in English — and
        anyone could put words of their own on the sign-in page."""
        assert "field-error" in i18n_client.get("/login?error=google").text
        assert "field-error" not in i18n_client.get("/login?error=Your+account+is+locked").text

    def test_the_admin_pages_say_they_are_english(self, i18n_client):
        """They stay English by decision; a /de/ prefix must not label them German."""
        from app import templates as t

        class Request:
            url = type("U", (), {"path": "/admin/health", "query": ""})()
            state = type("S", (), {"lang": "de"})()
            method = "GET"
            headers = {}
            cookies = {}

        context = t._globals(Request())
        assert context["hreflang"] == "en" and context["lang_dir"] == "ltr"
