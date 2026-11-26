"""Is this page in the language the crawl is counting?

The URL can't be trusted to say: LanguageFilter (app/crawler.py) reads the
first folder as a language code, which catches /de/ and /zh/ but not /cn/ —
China's country code, where Chinese is "zh" — nor /jp/, /kr/, language
subdomains or ?lang= parameters. So each fetched page is asked directly:

1. Its declared language, <html lang="zh-CN">, when it has one.
2. Failing that — or when a site declares the same lang on every page, which
   many do — the script its text is written in. A page that is mostly Chinese
   characters isn't English, whatever it declares.

Two pages in the same script (English and German) can only be told apart by
the declaration; that's accepted rather than guessed at.
"""

from __future__ import annotations

import re

_HTML_LANG_RE = re.compile(r'<html[^>]+\blang=["\']([a-zA-Z]{2,3})(?:[-_][a-zA-Z0-9]+)*["\']', re.IGNORECASE)

# Codepoint ranges per script. Letters outside all of them are ignored.
_RANGES = (
    ("latin", 0x0041, 0x024F),
    ("greek", 0x0370, 0x03FF),
    ("cyrillic", 0x0400, 0x052F),
    ("armenian", 0x0530, 0x058F),
    ("hebrew", 0x0590, 0x05FF),
    ("arabic", 0x0600, 0x06FF),
    ("devanagari", 0x0900, 0x097F),
    ("bengali", 0x0980, 0x09FF),
    ("thai", 0x0E00, 0x0E7F),
    ("georgian", 0x10A0, 0x10FF),
    ("hangul", 0x1100, 0x11FF),
    ("cjk", 0x3040, 0x30FF),       # hiragana, katakana
    ("hangul", 0x3130, 0x318F),
    ("cjk", 0x3400, 0x4DBF),
    ("cjk", 0x4E00, 0x9FFF),
    ("hangul", 0xAC00, 0xD7AF),
    ("arabic", 0xFB50, 0xFDFF),
)

# The scripts each language is written in; a language missing here is never
# judged by script.
_LATIN = {
    "af", "az", "ca", "cs", "cy", "da", "de", "en", "eo", "es", "et", "eu", "fi", "fr", "ga", "gl",
    "hr", "hu", "id", "is", "it", "lt", "lv", "ms", "mt", "nb", "nl", "nn", "no", "pl", "pt", "ro",
    "sk", "sl", "sq", "sv", "sw", "tl", "tr", "uz", "vi",
}
_SCRIPTS: dict[str, set[str]] = {code: {"latin"} for code in _LATIN}
_SCRIPTS.update({
    "ru": {"cyrillic"}, "uk": {"cyrillic"}, "bg": {"cyrillic"}, "mk": {"cyrillic"}, "be": {"cyrillic"},
    "kk": {"cyrillic"}, "mn": {"cyrillic"}, "sr": {"cyrillic", "latin"},
    "el": {"greek"}, "he": {"hebrew"}, "yi": {"hebrew"},
    "ar": {"arabic"}, "fa": {"arabic"}, "ur": {"arabic"},
    "hi": {"devanagari"}, "mr": {"devanagari"}, "ne": {"devanagari"}, "bn": {"bengali"},
    "th": {"thai"}, "ka": {"georgian"}, "hy": {"armenian"},
    "zh": {"cjk"}, "ja": {"cjk"}, "ko": {"hangul", "cjk"},
})

# Below this many letters a page is too thin to judge by script.
MIN_LETTERS = 120
# The share of letters one script needs before it's the page's script.
DOMINANT_SHARE = 0.6


def declared_language(html: str | None) -> str | None:
    match = _HTML_LANG_RE.search((html or "")[:4000])
    return match.group(1).lower() if match else None


def _script(ch: str) -> str | None:
    cp = ord(ch)
    for name, low, high in _RANGES:
        if low <= cp <= high:
            return name
    return None


def dominant_script(text: str | None) -> str | None:
    counts: dict[str, int] = {}
    total = 0
    for ch in (text or "")[:20_000]:
        if not ch.isalpha():
            continue
        name = _script(ch)
        if name is None:
            continue
        counts[name] = counts.get(name, 0) + 1
        total += 1
    if total < MIN_LETTERS:
        return None
    name, n = max(counts.items(), key=lambda kv: kv[1])
    return name if n / total >= DOMINANT_SHARE else None


def scripts_for(languages: list[str]) -> set[str]:
    """Every script the kept languages are written in; empty when any of them
    is unknown, so an unmapped language is never second-guessed."""
    scripts: set[str] = set()
    for code in languages:
        known = _SCRIPTS.get(code.lower())
        if not known:
            return set()
        scripts |= known
    return scripts


def other_language(html: str | None, text: str | None, languages: list[str], scripts: set[str]) -> str | None:
    """The language (or "script:<name>") a page is in when it isn't one the
    crawl is counting; None when it is, or when it can't tell."""
    keep = {code.lower() for code in languages}
    declared = declared_language(html)
    if keep and declared and declared not in keep:
        return declared
    if scripts:
        found = dominant_script(text)
        if found and found not in scripts:
            return f"script:{found}"
    return None
