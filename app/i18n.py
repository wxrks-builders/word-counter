"""Language selection, catalogues, and the formatting that goes with them.

The ten languages are exactly the ones wxrks.com publishes, taken from its own
hreflang tags. It addresses them by short path prefix — /es, /de, /pt — while
its hreflang carries the regional code, and this app matches both halves: get
one of them wrong and the two properties disagree about what a language is,
which is precisely what hreflang exists to prevent.

Translations are not written here. Catalogues ship with English filled and the
rest empty, and come back translated as .po files with no code change — see
locales/README.md.
"""

from __future__ import annotations

import gettext
import os
from functools import lru_cache
from pathlib import Path

from babel import Locale
from babel.numbers import format_decimal

# English is the root, so it has no prefix. The order is the order they appear
# in a language picker.
DEFAULT_LANG = "en"
LANGUAGES: tuple[str, ...] = ("en", "ar", "de", "es", "fr", "it", "ja", "ko", "pt", "sv", "zh")

# What each one is called in its own language — the only sensible way to label a
# language picker, since somebody looking for Japanese is not reading English.
LANGUAGE_NAMES = {
    "en": "English", "ar": "العربية", "de": "Deutsch", "es": "Español",
    "fr": "Français", "it": "Italiano", "ja": "日本語", "ko": "한국어",
    "pt": "Português", "sv": "Svenska", "zh": "中文",
}

# The path is short; the hreflang is regional, matching what wxrks.com declares.
# A search engine told "de" and a sibling site told "de-DE" will treat them as
# different targets, so these have to agree across both properties.
HREFLANG = {
    "en": "en", "ar": "ar", "de": "de-DE", "es": "es", "fr": "fr-FR",
    "it": "it", "ja": "ja", "ko": "ko", "pt": "pt-BR", "sv": "sv", "zh": "zh",
}

# Written right to left, so the whole layout mirrors rather than just the text.
RTL_LANGUAGES = frozenset({"ar"})

# The locale the browser's Intl formats with, where it differs from hreflang.
# Arabic numbers are written in Western digits on the page and in the script
# alike: Babel defaults to them and Intl defaults to Arabic-Indic ones, and a
# total the server rendered as 662,431 next to one the script rendered as
# ٦٦٢٬٤٣١ is the same number looking like two.
INTL_LOCALES = {"ar": "ar-u-nu-latn"}

# A language goes into hreflang, the sitemap and the "also in" suggestion once
# this share of its catalogue is translated. Below it the pages still work (an
# empty msgstr falls back to English) but are English under another URL, and
# advertising them would tell search engines ten duplicates are translations.
PUBLISH_AT = 0.9

# A pseudo-locale for finding strings that were never extracted. Every
# translated string comes back wrapped, so anything still bare on the page is
# hard-coded English somebody missed. Enabled only when PSEUDO_LOCALE is set,
# because it must never be reachable in production.
PSEUDO_LANG = "zz"
PSEUDO_ENABLED = os.environ.get("PSEUDO_LOCALE", "").lower() in ("1", "true", "yes")

LOCALE_DIR = Path(__file__).resolve().parent.parent / "locales"

_translations: dict[str, gettext.NullTranslations] = {}


def N_(message: str) -> str:
    """Marks a string for extraction without translating it.

    Module-level text — the landing copy in app/surfaces.py, the plan features —
    is built at import, long before a request exists to have a language. This
    puts it in the catalogue where it is written, and the template calls _() on
    it at render time, when there is a language to translate into.
    """
    return message


def selectable_languages() -> tuple[str, ...]:
    return LANGUAGES + (PSEUDO_LANG,) if PSEUDO_ENABLED else LANGUAGES


def is_language(code: str) -> bool:
    return code in selectable_languages()


def translations_for(lang: str) -> gettext.NullTranslations:
    """The catalogue for a language, loaded once.

    Falls back to untranslated English rather than raising: a language whose .mo
    hasn't been compiled yet should show English, not a 500. That is also what
    makes it safe to add a language to LANGUAGES before its catalogue exists.
    """
    if lang not in _translations:
        try:
            _translations[lang] = gettext.translation(
                "messages", localedir=str(LOCALE_DIR), languages=[lang]
            )
        except OSError:
            _translations[lang] = gettext.NullTranslations()
    return _translations[lang]


def gettext_for(lang: str):
    """The `_` a template or route uses. The pseudo-locale wraps instead of
    translating, so untranslated text is visible rather than merely absent."""
    if lang == PSEUDO_LANG:
        return lambda message: f"⟦{message}⟧"
    return translations_for(lang).gettext


def ngettext_for(lang: str):
    """Plurals from the catalogue's own Plural-Forms rule.

    English has two forms and picks one with n == 1; Arabic has six, French
    puts 0 with 1, Japanese has one. Only the catalogue knows which form a
    number takes, so anything with a count goes through this and never
    through `singular if n == 1 else plural`.

    The English singular carries the number too ("%(n)s page", not "1 page"):
    in other languages the form used for 1 is also used for 21, or for 0.
    """
    if lang == PSEUDO_LANG:
        return lambda singular, plural, n: f"⟦{singular if n == 1 else plural}⟧"
    return translations_for(lang).ngettext


def pgettext_for(lang: str):
    """For a word with more than one meaning: the context keeps "Crawl" the
    button apart from "Crawl" the noun, which other languages spell apart."""
    if lang == PSEUDO_LANG:
        return lambda context, message: f"⟦{message}⟧"
    return translations_for(lang).pgettext


def npgettext_for(lang: str):
    if lang == PSEUDO_LANG:
        return lambda context, singular, plural, n: f"⟦{singular if n == 1 else plural}⟧"
    return translations_for(lang).npgettext


@lru_cache(maxsize=None)
def plural_samples(lang: str) -> tuple[tuple[str, int], ...]:
    """One number for each CLDR plural category the language uses:
    (("one", 1), ("other", 0)) in English, six pairs in Arabic.

    The browser picks a category with Intl.PluralRules; the server fills each
    category with the form the catalogue gives that category's sample. Both
    come from CLDR, so the form the script shows for 11 is the form the page
    would have rendered for 11 (tests/test_i18n.py checks every language).
    """
    locale = locale_for(lang)
    samples: dict[str, int] = {}
    for n in [*range(0, 1001), 1_000_000]:
        samples.setdefault(locale.plural_form(n), n)
    return tuple(samples.items())


def plural_variants(lang: str, singular: str, plural: str) -> dict[str, str]:
    """A plural message as the browser needs it: {"one": ..., "other": ...},
    keyed by CLDR category. See plural_samples.

    Untranslated, it is still English, and English has to be picked by English
    rules: French puts 0 and 1 in "one", which would otherwise show "1 pages".
    gettext falls back the same way on the server, so the page and the script
    agree. Such an entry says so with "_rules": "en"."""
    if lang != PSEUDO_LANG and not _has_plural(lang, singular):
        return {"one": singular, "other": plural, "_rules": DEFAULT_LANG}
    ngettext = ngettext_for(lang)
    return {category: ngettext(singular, plural, n) for category, n in plural_samples(lang)}


def _has_plural(lang: str, singular: str) -> bool:
    """Whether the compiled catalogue translates this plural message.
    scripts/translations.py compile leaves untranslated messages out of the .mo,
    so presence is the test. (pybabel compile would not: it fills them with
    English, so every plural would look translated.)"""
    catalog = getattr(translations_for(lang), "_catalog", None)
    return bool(catalog and catalog.get((singular, 0)))


def intl_locale(lang: str) -> str:
    """The tag app.js hands to Intl. The pseudo-locale formats as English."""
    if lang == PSEUDO_LANG:
        return "en"
    return INTL_LOCALES.get(lang, HREFLANG.get(lang, lang))


def duration_text(total_seconds, lang: str) -> str:
    """"1h 8m" / "25 min" / "less than a minute": a crawl's length, rounded to
    the minute. Messages rather than CLDR units so each language picks its own
    abbreviations, and so app.js (formatDuration) reads the very same ones."""
    # Bound to these names so pybabel finds the calls (and the notes above them).
    _ = gettext_for(lang)
    ngettext = ngettext_for(lang)
    seconds = int(total_seconds or 0)
    if seconds < 60:
        return _("less than a minute")
    minutes = round(seconds / 60)
    if minutes < 60:
        # Translators: a duration in minutes, abbreviated ("25 min").
        return ngettext("%(n)s min", "%(n)s min", minutes) % {"n": format_number(minutes, lang)}
    hours, remainder = divmod(minutes, 60)
    if remainder:
        # Translators: a duration, abbreviated: h is hours, m is minutes ("1h 8m").
        return _("%(h)sh %(m)sm") % {"h": format_number(hours, lang), "m": format_number(remainder, lang)}
    # Translators: a duration in hours, abbreviated ("3h").
    return _("%(h)sh") % {"h": format_number(hours, lang)}


def format_duration(total_seconds, lang: str) -> str:
    """"~1h 8m" / "~25 min": duration_text, marked as approximate."""
    if int(total_seconds or 0) < 60:
        return duration_text(total_seconds, lang)
    _ = gettext_for(lang)
    # Translators: marks a duration as approximate ("~25 min"). %(duration)s is
    # a duration such as "25 min" or "1h 8m".
    return _("~%(duration)s") % {"duration": duration_text(total_seconds, lang)}


@lru_cache(maxsize=None)
def coverage(lang: str) -> float:
    """Share of the catalogue that is translated and compiled, 0.0 to 1.0.

    Read from the .po (the source of truth committed to the repository), but
    only counted once a compiled .mo exists: an uncompiled catalogue serves
    English whatever the .po says."""
    if lang == DEFAULT_LANG:
        return 1.0
    po = LOCALE_DIR / lang / "LC_MESSAGES" / "messages.po"
    if not isinstance(translations_for(lang), gettext.GNUTranslations):
        return 0.0
    try:
        from babel.messages.pofile import read_po

        with po.open("rb") as handle:
            catalog = read_po(handle)
    except Exception:
        return 0.0
    messages = [m for m in catalog if m.id]
    if not messages:
        return 0.0
    done = sum(1 for m in messages if (m.string if isinstance(m.string, str) else all(m.string)))
    return done / len(messages)


def published_languages() -> tuple[str, ...]:
    """The languages offered to search engines and suggested to visitors: the
    source and every one translated past PUBLISH_AT. The language menu still
    lists all of them, so a language can be reviewed before it is published."""
    return tuple(code for code in LANGUAGES if coverage(code) >= PUBLISH_AT)


def preferred_language(accept_language: str | None, choices=None) -> str | None:
    """The best of `choices` for an Accept-Language header, or None.

    Matches on the primary subtag (de-AT reads de), except Chinese: the
    catalogue is Simplified, so zh-TW, zh-HK and zh-Hant are not offered it.
    """
    choices = tuple(choices if choices is not None else LANGUAGES)
    ranked: list[tuple[float, int, str]] = []
    for index, part in enumerate((accept_language or "").split(",")):
        tag, _, params = part.strip().partition(";")
        tag = tag.strip().lower().replace("_", "-")
        if not tag or tag == "*":
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.strip().partition("=")
            if key == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if quality <= 0:
            continue
        primary = tag.split("-")[0]
        if primary == "zh" and any(sub in tag.split("-")[1:] for sub in ("tw", "hk", "mo", "hant")):
            continue
        if primary in choices:
            ranked.append((-quality, index, primary))
    return min(ranked)[2] if ranked else None


def split_path(path: str) -> tuple[str, str]:
    """("/es/runs") -> ("es", "/runs"). Anything else keeps its path and gets
    the default, so an unknown first segment stays a normal 404 rather than
    being swallowed as a language nobody offers."""
    parts = path.split("/", 2)
    if len(parts) > 1 and is_language(parts[1]) and parts[1] != DEFAULT_LANG:
        return parts[1], "/" + (parts[2] if len(parts) > 2 else "")
    return DEFAULT_LANG, path


def localized_path(path: str, lang: str) -> str:
    """The prefixed form of an internal path.

    English is the root, so it never gains a prefix. Static files never do
    either — they aren't translated and prefixing them would make every language
    miss the browser cache for the same asset.
    """
    if lang == DEFAULT_LANG or not path.startswith("/") or path.startswith("/static/"):
        return path
    return f"/{lang}{path}"


def locale_for(lang: str) -> Locale:
    """Babel's locale for formatting. The pseudo-locale formats as English —
    it exists to find missing strings, not to invent a number format."""
    try:
        return Locale.parse(HREFLANG.get(lang, lang), sep="-")
    except Exception:
        return Locale.parse("en")


def format_number(value: int | float, lang: str) -> str:
    """662431 -> "662,431" in English, "662.431" in German, "662 431" in French.

    This app's whole output is a number, so leaving it in US grouping for every
    language would be the product getting it visibly wrong in ten places at once.
    """
    return format_decimal(value, locale=locale_for(lang))


def format_money(amount: int | float, currency: str, lang: str, whole: bool = False) -> str:
    """£0 in English, 0 £ in German, 0 £GB in French: the currency is the
    price's, never the reader's — a German reading the pricing page still pays
    in pounds — and only its writing follows the language. `whole` drops the
    minor units, for a round price shown as "£0" rather than "£0.00"."""
    import copy

    locale = locale_for(lang)
    pattern = copy.copy(locale.currency_formats["standard"])
    if whole:
        pattern.frac_prec = (0, 0)
    return pattern.apply(amount, locale, currency=currency, currency_digits=not whole)


# Set by app.js when someone picks a language in the menu or answers the
# "also in" suggestion, so the choice outlives the URL they happened to open.
LANG_COOKIE = "lang"


def is_page_view(request) -> bool:
    """A browser opening a page, as opposed to app.js fetching data. The API,
    the event stream and the downloads live at unprefixed paths, so they would
    otherwise all read as English."""
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


def chosen_language(request) -> str | None:
    """The language this request shows the visitor chose, or None when it says
    nothing.

    A prefixed page is a choice: the URL is it. An unprefixed one is only a
    choice of English when the visitor picked English in the menu (the cookie
    says so). Otherwise it's simply where a link happened to land, and must not
    overwrite a German speaker's preference with English.
    """
    if not is_page_view(request):
        return None
    lang = getattr(request.state, "lang", DEFAULT_LANG)
    if lang == PSEUDO_LANG:
        return None
    if lang != DEFAULT_LANG:
        return lang
    return DEFAULT_LANG if request.cookies.get(LANG_COOKIE) == DEFAULT_LANG else None


def suggested_language(request) -> str | None:
    """On an English page, the published language this visitor would rather
    read, if any: the one they picked before, or else their browser's.

    Suggested, never redirected to: an automatic redirect on every visit makes
    the English page unreachable for anyone whose browser says otherwise, and
    hides it from crawlers that send no Accept-Language at all.
    """
    if getattr(request.state, "lang", DEFAULT_LANG) != DEFAULT_LANG or not is_page_view(request):
        return None
    published = published_languages()
    remembered = request.cookies.get(LANG_COOKIE)
    if remembered:
        return remembered if remembered in published and remembered != DEFAULT_LANG else None
    match = preferred_language(request.headers.get("accept-language"), published)
    return match if match and match != DEFAULT_LANG else None
