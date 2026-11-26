"""Translation check: what a multilingual site still hasn't translated.

Every page in the site's own language is fetched together with each of its
language versions (the hreflang alternates in the sitemap, or failing that in
the page's head), and the two are compared:

- a language version that doesn't exist, 404s or redirects away is a missing
  page;
- a block of text, an alt/title/placeholder, or a title/description identical
  to the original's is untranslated — 3+ words with some confidence, 1–2 words
  ("Login", "CEO") as short strings, since many of those are names that should
  stay as they are;
- English that isn't on the original page any more (copy that changed after it
  was translated) is caught by script, or by a short list of strictly English
  words;
- a wrong <html lang>, and links to original-language pages that exist in this
  language, are reported too.

A plain GET, not the browser: Webflow and most CMSs render their language
versions on the server, and ten thousand pages through Chromium would be hours
of machinery for a text comparison. Unlike the word count, nothing is stripped
as boilerplate — the menu, the footer, the forms and the cookie banner are
exactly where untranslated text tends to live.

Runs in the app's own process, a page group at a time, saving as it goes:
every deploy restarts the process, and a weekly check of 10k pages has to
survive that. A check is leased to one process (owner + heartbeat) because
old and new containers overlap during a rolling deploy.
"""

from __future__ import annotations

import asyncio
import collections
import gzip
import hashlib
import io
import json
import logging
import math
import os
import re
import socket
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlsplit

import httpx
from lxml import etree
from lxml import html as lxml_html

from app import db
from app.language_check import _script, scripts_for
from app.url_policy import canonical_key
from app.word_count import count_words

logger = logging.getLogger(__name__)

# Bumped whenever the rules below change what counts as a finding. A diff
# against a check made under other rules would report the rule change as
# "new" and "fixed" strings, so it isn't made.
RULES_VERSION = 1

# Sites checked on a schedule, comma-separated. Empty: only "Check now".
SITES = [s.strip().rstrip("/") for s in os.environ.get("TRANSLATION_CHECK_SITES", "").split(",") if s.strip()]
EVERY_DAYS = int(os.environ.get("TRANSLATION_CHECK_EVERY_DAYS", "7"))
# A night hour, away from deploys and from people's crawls.
HOUR_UTC = int(os.environ.get("TRANSLATION_CHECK_HOUR_UTC", "4"))
# Pages fetched at once. It's somebody's website — ours, today, but still.
CONCURRENCY = max(1, int(os.environ.get("TRANSLATION_CHECK_CONCURRENCY", "4")))
KEEP_RUNS = max(2, int(os.environ.get("TRANSLATION_CHECK_KEEP_RUNS", "12")))
# Translated pages one check will look at, at most.
MAX_PAGES = int(os.environ.get("TRANSLATION_CHECK_MAX_PAGES", "20000"))
# Screenshots of key pages (home, then the main menu) per check, each one a
# language version with its untranslated text outlined. A browser per page,
# so it's capped; 0 turns them off.
SCREENSHOTS = max(0, int(os.environ.get("TRANSLATION_CHECK_SCREENSHOTS", "150")))
# A translated page with at least this share of its words still in the
# original language is listed as "still in English".
ENGLISH_PAGE_SHARE = 0.5
# Below this many words a page is too thin to call "in English".
ENGLISH_PAGE_MIN_WORDS = 30
_KEY_PAGES_LIMIT = 100

# A check whose owner hasn't written for this long is presumed dead.
LEASE_SECONDS = 180
# Taken over this many times and still not finished: something about it kills
# the process, so stop trying.
MAX_RESUMES = 5
# Abort when more than this share of the last FAILURE_WINDOW fetches failed —
# a check the site is blocking would otherwise report everything "fixed".
FAILURE_RATE = 0.2
FAILURE_WINDOW = 100
_FAILURE_MIN_SAMPLE = 30

FETCH_TIMEOUT = 20.0
FETCH_RETRIES = 2
_SITEMAP_LIMIT = 50  # child sitemaps followed from an index
_SITEMAP_MAX_BYTES = 200 * 1024 * 1024

OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

# The kinds a check can report, in the order the report shows them.
TEXT_KINDS = ("text", "short_text", "english_text")
KINDS = ("missing_page", "text", "short_text", "english_text", "meta", "attribute", "wrong_lang", "english_link")

_HEADERS = {
    # The same posture as backfill_detected_cms: a bot-shaped User-Agent gets
    # 429s and error shells from sites that serve a browser fine.
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class LostLease(Exception):
    """Another process owns this check now."""


class CheckAborted(Exception):
    """The check can't go on: too many failures, or nothing to compare."""


class _Cancelled(Exception):
    """Cancel was pressed on the admin page."""


# ------------------------------------------------------------------ the text

# Elements whose text is a flow of their own. Anything else is inline, and its
# text belongs to the block around it — unless it holds a block itself, as
# Webflow's <a class="w-inline-block"><div>…</div></a> cards do.
_BLOCK_TAGS = frozenset("""
    address article aside blockquote body button caption dd details dialog div dl dt fieldset
    figcaption figure footer form h1 h2 h3 h4 h5 h6 header hgroup label legend li main menu nav
    ol optgroup option p section summary table tbody td tfoot th thead tr ul
""".split())
# Never text a visitor reads as the page's copy.
_SKIP_TAGS = frozenset("script style noscript template svg code pre iframe object canvas math head".split())
# Classes that mean "not shown" or "not to be translated": Webflow's hidden
# conditional elements, its JSON data blocks and its own language switcher;
# the usual do-not-translate marker. And in-page developer consoles.
_SKIP_CLASSES = frozenset({"notranslate", "w-condition-invisible", "w-json", "w-locales-list", "w-webflow-badge"})
_SKIP_IDS = frozenset({"__vconsole", "eruda"})
_TEXT_ATTRS = ("alt", "title", "placeholder", "aria-label", "data-wait")
_META_NAMES = {
    "description": "description",
    "og:title": "og:title",
    "og:description": "og:description",
    "og:image:alt": "og:image:alt",
    "twitter:title": "twitter:title",
    "twitter:description": "twitter:description",
}

_INVISIBLE = {ord(c): None for c in "­​‌‍⁠﻿"}
_INVISIBLE[0x00A0] = " "
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'",
                         "“": '"', "”": '"', "„": '"', "‟": '"'})
_EDGES = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)
_SPACES = re.compile(r"\s+")
_URLISH = re.compile(r"^(https?://|www\.)\S+$|^\S+@\S+\.\S+$", re.IGNORECASE)
_LATIN_WORD = re.compile(r"[A-Za-z']+")

# Words that are English and almost never anything else in the languages
# sites translate into — "die", "will", "so", "an" or "is" would fire on
# perfectly good German, Dutch or Swedish.
_ENGLISH_WORDS = frozenset("""
    the and with your you our this that from are how what which have has about would they their
    these those been were should could into through
""".split())


def normalize_key(text: str) -> str:
    """What two strings have to share to count as the same text: case,
    curly quotes, invisible characters, spacing and the punctuation at either
    end ("Learn more →") don't matter."""
    text = unicodedata.normalize("NFC", text).translate(_INVISIBLE).translate(_QUOTES)
    text = _SPACES.sub(" ", text).strip().casefold()
    return _EDGES.sub("", text)


def text_key(text: str) -> str:
    return hashlib.blake2b(normalize_key(text).encode(), digest_size=10).hexdigest()


def _clean(text: str) -> str:
    return _SPACES.sub(" ", unicodedata.normalize("NFC", text).translate(_INVISIBLE)).strip()


def _worth_checking(text: str) -> bool:
    """Prices, years, URLs and two-letter labels are the same in any language."""
    if sum(ch.isalpha() for ch in text) < 3:
        return False
    return not _URLISH.match(text)


def lang_code(value: str | None) -> str:
    return (value or "").strip().lower().replace("_", "-").split("-")[0]


@dataclass
class Unit:
    text: str
    words: int
    where: str = "page"              # page | component | cms | richtext | embed
    component_id: str | None = None
    component_label: str | None = None
    detail: str | None = None        # attribute or meta name


@dataclass
class Extracted:
    lang: str | None = None
    units: dict[str, Unit] = field(default_factory=dict)       # body text, by key
    attrs: dict[str, Unit] = field(default_factory=dict)       # alt, title… by key
    meta: dict[str, Unit] = field(default_factory=dict)        # by field name
    links: list[str] = field(default_factory=list)
    alternates: dict[str, str] = field(default_factory=dict)   # hreflang -> absolute URL
    nav_links: list[tuple[str, bool]] = field(default_factory=list)  # (URL, inside a dropdown)

    @property
    def words(self) -> int:
        return sum(unit.words for unit in self.units.values())


@dataclass
class _Context:
    where: str = "page"
    component_id: str | None = None
    component_label: str | None = None


def _component(el) -> str | None:
    raw = el.get("data-wf-component-context")
    if not raw:
        return None
    try:
        from urllib.parse import unquote

        entries = json.loads(unquote(raw))
        return entries[-1].get("componentId") if entries else None
    except Exception:
        return None


def _label(el) -> str | None:
    classes = (el.get("class") or "").split()
    named = [c for c in classes if not c.startswith("w-")]
    return (named or classes or [el.tag])[0]


def _skipped(el, alternates: set[str], base: str) -> bool:
    if el.tag in _SKIP_TAGS or el.get("translate") == "no":
        return True
    classes = set((el.get("class") or "").split())
    if classes & _SKIP_CLASSES or (el.get("id") or "") in _SKIP_IDS:
        return True
    if el.tag == "a":
        # The language switcher: its labels ("English", "Português") are the
        # same on every version by design.
        if el.get("hreflang"):
            return True
        href = (el.get("href") or "").strip()
        if href and alternates and _url_key(urljoin(base, href)) in alternates:
            return True
    return False


def _url_key(url: str) -> str:
    return canonical_key(url.split("#")[0])


_NOT_PAGES = ("#", "mailto:", "tel:", "javascript:", "data:")
# The main menu: <nav>, <header>, role=navigation, or Webflow's navbar.
_NAV_LINKS = ("//nav//a[@href] | //header//a[@href] | //*[@role='navigation']//a[@href]"
              " | //*[contains(concat(' ', normalize-space(@class), ' '), ' w-nav ')]//a[@href]")


def _is_inline(el) -> bool:
    if not isinstance(el.tag, str) or el.tag in _BLOCK_TAGS:
        return False
    return not any(isinstance(d.tag, str) and d.tag in _BLOCK_TAGS for d in el.iterdescendants())


def extract_page(raw_html: str, page_url: str) -> Extracted:
    """The text, attributes, head fields and links of one page."""
    page = Extracted()
    try:
        tree = lxml_html.fromstring(raw_html)
    except Exception:
        return page
    base = page_url
    for href in tree.xpath("//base/@href"):
        base = urljoin(page_url, href.strip())
        break
    page.lang = next(iter(tree.xpath("/html/@lang")), None) or None

    for link in tree.xpath("//link[@rel='alternate'][@hreflang][@href]"):
        page.alternates[link.get("hreflang").strip()] = urljoin(base, link.get("href").strip())
    alternate_keys = {_url_key(url) for url in page.alternates.values()}

    seen_nav: set[str] = set()
    for anchor in tree.xpath(_NAV_LINKS):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(_NOT_PAGES):
            continue
        url = urljoin(base, href).split("#")[0]
        if _url_key(url) in seen_nav:
            continue
        seen_nav.add(_url_key(url))
        dropdown = any("dropdown" in (el.get("class") or "") for el in anchor.iterancestors())
        page.nav_links.append((url, dropdown))

    for el in tree.xpath("//title"):
        text = _clean(el.text_content())
        if _worth_checking(text):
            page.meta["title"] = Unit(text, count_words(text), detail="title")
        break
    for el in tree.xpath("//meta[@content]"):
        name = (el.get("name") or el.get("property") or "").strip().lower()
        if name in _META_NAMES and _META_NAMES[name] not in page.meta:
            text = _clean(el.get("content"))
            if _worth_checking(text):
                page.meta[_META_NAMES[name]] = Unit(text, count_words(text), detail=_META_NAMES[name])

    body = tree.find("body")
    if body is None:
        return page

    def add(store: dict, text: str, ctx: _Context, detail: str | None = None):
        text = _clean(text)
        if not _worth_checking(text):
            return
        key = text_key(text)
        if key not in store:
            store[key] = Unit(text[:500], count_words(text), ctx.where, ctx.component_id, ctx.component_label, detail)

    def inline_text(el) -> str:
        # An inline element's text, minus anything inside it that's skipped.
        parts = [el.text or ""]
        for child in el:
            if isinstance(child.tag, str) and not _skipped(child, alternate_keys, base):
                parts.append(" " if child.tag == "br" else inline_text(child))
            parts.append(child.tail or "")
        return "".join(parts)

    def collect(el, ctx: _Context):
        # Attribute text and links, wherever the element sits.
        for name in _TEXT_ATTRS:
            value = el.get(name)
            if value:
                add(page.attrs, value, ctx, name)
        if el.tag == "input" and (el.get("type") or "").lower() in ("submit", "button", "reset") and el.get("value"):
            add(page.attrs, el.get("value"), ctx, "value")
        if el.tag == "a" and el.get("href"):
            href = el.get("href").strip()
            if href and not href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
                page.links.append(urljoin(base, href).split("#")[0])

    def walk(el, ctx: _Context):
        component = _component(el)
        if component and component != ctx.component_id:
            ctx = _Context("component", component, _label(el))
        classes = set((el.get("class") or "").split())
        if ctx.where != "component":
            if "w-dyn-item" in classes:
                ctx = _Context("cms")
            elif "w-richtext" in classes and ctx.where == "page":
                ctx = _Context("richtext")
            elif "w-embed" in classes and ctx.where == "page":
                ctx = _Context("embed")

        collect(el, ctx)

        # This element's own flow of text: its text, its inline children's
        # whole text and every child's tail. Block children are their own.
        own = [el.text or ""]
        loose = [el.text or ""]      # text that isn't inside an inline child
        pieces: list[str] = []       # each inline child's text
        for child in el:
            if isinstance(child.tag, str):
                if _skipped(child, alternate_keys, base):
                    pass
                elif _is_inline(child):
                    piece = " " if child.tag == "br" else inline_text(child)
                    own.append(piece)
                    if child.tag != "br":
                        pieces.append(piece)
                    # Inline children can still carry attributes and links.
                    for sub in child.iter():
                        if isinstance(sub.tag, str):
                            collect(sub, ctx)
                else:
                    walk(child, ctx)
            own.append(child.tail or "")
            loose.append(child.tail or "")
        if len([p for p in pieces if p.strip()]) > 1 and not any(ch.isalpha() for ch in "".join(loose)):
            # Links or labels side by side with nothing but spaces between —
            # a menu, not a sentence: each one is its own text.
            for piece in pieces:
                add(page.units, piece, ctx)
        else:
            add(page.units, "".join(own), ctx)

    walk(body, _Context())
    return page


def english_looking(text: str, language: str) -> bool:
    """Clearly English, in a page that should be in `language`."""
    words = [w.lower() for w in _LATIN_WORD.findall(text)]
    english = sum(w in _ENGLISH_WORDS for w in words)
    scripts = scripts_for([lang_code(language)])
    if scripts and "latin" not in scripts:
        letters = [ch for ch in text if ch.isalpha()]
        latin = sum(1 for ch in letters if _script(ch) == "latin")
        return bool(letters) and len(words) >= 3 and latin / len(letters) >= 0.6 and english >= 1
    return len(words) >= 5 and english >= 2


def compare_page(source: Extracted, target: Extracted, language: str, source_language: str,
                 translated_links: dict[str, str], page_url: str) -> tuple[list[list], int]:
    """What a language version kept from the original.

    Returns compact findings — [kind, key, text, words, where, component_id,
    component_label, detail] — and the English words left on the page.
    """
    findings: dict[tuple[str, str], list] = {}
    untranslated = 0

    def add(kind: str, key: str, unit: Unit, detail: str | None = None):
        if (kind, key) not in findings:
            findings[(kind, key)] = [kind, key, unit.text, unit.words, unit.where, unit.component_id,
                                     unit.component_label, detail or unit.detail]

    for key, unit in target.units.items():
        if key in source.units:
            add("text" if unit.words >= 3 else "short_text", key, unit)
            untranslated += unit.words
        elif lang_code(language) != lang_code(source_language) and english_looking(unit.text, language):
            add("english_text", key, unit)
            untranslated += unit.words
    for key, unit in target.attrs.items():
        if key in source.attrs:
            add("attribute", key, unit)
    for name, unit in target.meta.items():
        original = source.meta.get(name)
        if original is not None and text_key(original.text) == text_key(unit.text):
            add("meta", text_key(unit.text), unit, name)
    if lang_code(target.lang) != lang_code(language):
        shown = f'lang="{target.lang}"' if target.lang else "no lang attribute"
        add("wrong_lang", text_key(shown), Unit(shown, 0))
    own = _url_key(page_url)
    for href in target.links:
        key = _url_key(href)
        if key != own and key in translated_links:
            path = urlsplit(href).path or "/"
            add("english_link", text_key(key), Unit(path, 0), translated_links[key])
    return list(findings.values()), untranslated


# ------------------------------------------------------------------ sitemaps

@dataclass
class SitemapEntry:
    url: str
    alternates: dict[str, str]


def parse_sitemap(data: bytes) -> tuple[list[SitemapEntry], list[str]]:
    """Pages (with their hreflang alternates) and child sitemaps.

    iterparse with each element cleared once read: a Webflow sitemap of a
    10-language site is 18 MB, mostly repeated alternates.
    """
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    entries: list[SitemapEntry] = []
    children: list[str] = []
    context = etree.iterparse(io.BytesIO(data), events=("end",), resolve_entities=False,
                              no_network=True, huge_tree=True, recover=True)
    for _, el in context:
        tag = etree.QName(el).localname if isinstance(el.tag, str) else ""
        if tag == "url":
            loc = None
            alternates: dict[str, str] = {}
            for child in el:
                if not isinstance(child.tag, str):
                    continue
                name = etree.QName(child).localname
                if name == "loc" and child.text:
                    loc = child.text.strip()
                elif name == "link" and child.get("hreflang") and child.get("href"):
                    alternates[child.get("hreflang").strip()] = child.get("href").strip()
            if loc:
                entries.append(SitemapEntry(loc, alternates))
            el.clear()
        elif tag == "sitemap":
            for child in el:
                if isinstance(child.tag, str) and etree.QName(child).localname == "loc" and child.text:
                    children.append(child.text.strip())
            el.clear()
    return entries, children


async def _download(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        response = await client.get(url, timeout=60.0)
    except httpx.HTTPError:
        return None
    if response.status_code != 200 or len(response.content) > _SITEMAP_MAX_BYTES:
        return None
    return response.content


async def fetch_sitemap(client: httpx.AsyncClient, site_url: str) -> list[SitemapEntry]:
    """Every page the site's sitemaps list: robots.txt's Sitemap: lines, or
    /sitemap.xml, following one level of sitemap index."""
    roots: list[str] = []
    robots = await _download(client, urljoin(site_url + "/", "/robots.txt"))
    if robots:
        for line in robots.decode("utf-8", "replace").splitlines():
            if line.lower().startswith("sitemap:"):
                roots.append(line.split(":", 1)[1].strip())
    if not roots:
        roots = [urljoin(site_url + "/", "/sitemap.xml")]

    entries: list[SitemapEntry] = []
    seen: set[str] = set()
    queue = list(roots)
    followed = 0
    while queue and followed < _SITEMAP_LIMIT:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        followed += 1
        data = await _download(client, url)
        if not data:
            continue
        try:
            found, children = await asyncio.to_thread(parse_sitemap, data)
        except Exception:
            logger.warning("Could not parse the sitemap at %s", url)
            continue
        entries.extend(found)
        queue.extend(children)
    return entries


@dataclass
class Plan:
    source_language: str
    locales: list[dict]                        # [{code, name, prefix, host}]
    groups: dict[str, dict[str, str | None]]   # source URL -> {locale: URL or None}
    capped: bool = False


def _language_name(code: str) -> str:
    try:
        from babel import Locale

        return Locale.parse(code.replace("_", "-"), sep="-").english_name or code
    except Exception:
        return code


def _common_prefix(urls: list[str]) -> tuple[str | None, str | None]:
    """The first path segment (and host) most of a language's pages share."""
    hosts = collections.Counter(urlsplit(u).netloc.lower() for u in urls)
    segments = collections.Counter(
        (urlsplit(u).path.strip("/").split("/") or [""])[0] for u in urls
    )
    host, host_n = hosts.most_common(1)[0] if hosts else (None, 0)
    segment, segment_n = segments.most_common(1)[0] if segments else ("", 0)
    prefix = f"/{segment}" if segment and segment_n >= 0.8 * len(urls) else None
    return prefix, (host if host_n >= 0.8 * len(urls) else None)


def build_plan(site_url: str, entries: list[SitemapEntry], home: Extracted | None,
               max_pages: int = MAX_PAGES) -> Plan:
    """Which pages to compare, and which language versions each one has."""
    alternates_seen: dict[str, list[str]] = collections.defaultdict(list)
    for entry in entries:
        for hreflang, url in entry.alternates.items():
            alternates_seen[hreflang].append(url)
    if not alternates_seen and home is not None:
        for hreflang, url in home.alternates.items():
            alternates_seen[hreflang].append(url)
    if not alternates_seen:
        raise CheckAborted("This site doesn't list its language versions: no hreflang alternates "
                           "in its sitemap or on its home page.")

    # The original language: the one x-default points at, else English, else
    # what the home page declares.
    source_language = None
    x_default = {_url_key(u) for u in alternates_seen.get("x-default", [])}
    if x_default:
        for hreflang, urls in alternates_seen.items():
            if hreflang != "x-default" and {_url_key(u) for u in urls} & x_default:
                source_language = hreflang
                break
    if source_language is None:
        source_language = next((h for h in alternates_seen if lang_code(h) == "en"), None)
    if source_language is None and home is not None and home.lang:
        source_language = next((h for h in alternates_seen if lang_code(h) == lang_code(home.lang)), None)
    if source_language is None:
        raise CheckAborted("Couldn't tell which language is the original (no x-default or English version).")

    locale_codes = [h for h in alternates_seen if h not in ("x-default", source_language)]
    locales = []
    for code in locale_codes:
        prefix, host = _common_prefix(alternates_seen[code])
        locales.append({"code": code, "name": _language_name(code), "prefix": prefix, "host": host})

    def is_translation(url: str) -> bool:
        parts = urlsplit(url)
        for locale in locales:
            if locale["prefix"] and (parts.path.rstrip("/") + "/").startswith(locale["prefix"] + "/"):
                if not locale["host"] or parts.netloc.lower() == locale["host"]:
                    return True
            elif not locale["prefix"] and locale["host"] and parts.netloc.lower() == locale["host"]:
                return True
        return False

    groups: dict[str, dict[str, str | None]] = {}
    for entry in entries:
        if entry.alternates and source_language in entry.alternates:
            source = entry.alternates[source_language]
        elif not entry.alternates and not is_translation(entry.url):
            source = entry.url
        else:
            continue
        group = groups.setdefault(source, {code: None for code in locale_codes})
        for code in locale_codes:
            if entry.alternates.get(code) and not group.get(code):
                group[code] = entry.alternates[code]
    if not groups:
        groups[site_url.rstrip("/") or site_url] = {code: (home.alternates.get(code) if home else None)
                                                   for code in locale_codes}

    capped = False
    if locale_codes and len(groups) * len(locale_codes) > max_pages:
        keep = max(1, max_pages // len(locale_codes))
        groups = dict(list(groups.items())[:keep])
        capped = True
    return Plan(source_language, locales, groups, capped)


def key_pages(home_url: str, home: Extracted | None, sources) -> list[str]:
    """The pages that matter most: the home page, then what the main menu
    links to — its top-level items (Pricing, Contact) before its dropdowns."""
    by_key = {_url_key(source): source for source in sources}
    ordered: list[str] = []

    def add(url: str) -> None:
        source = by_key.get(_url_key(url))
        if source and source not in ordered:
            ordered.append(source)

    add(home_url)
    if home is not None:
        for url, dropdown in home.nav_links:
            if not dropdown:
                add(url)
        for url, dropdown in home.nav_links:
            if dropdown:
                add(url)
    return ordered[:_KEY_PAGES_LIMIT]


# ------------------------------------------------------------------ fetching

@dataclass
class Fetched:
    status: int            # HTTP status, or 0 when the request itself failed
    final_url: str
    html: str | None = None
    error: str | None = None

    @property
    def failed(self) -> bool:
        """Counts against the site: blocked, erroring or unreachable. A 404 is
        an answer, not a failure."""
        return self.status == 0 or self.status == 429 or self.status == 403 or self.status >= 500


async def fetch(client: httpx.AsyncClient, url: str) -> Fetched:
    for attempt in range(FETCH_RETRIES + 1):
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            if attempt < FETCH_RETRIES:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            return Fetched(0, url, error=type(exc).__name__)
        if response.status_code in (429, 500, 502, 503, 504) and attempt < FETCH_RETRIES:
            wait = 2 * (attempt + 1)
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                wait = min(int(retry_after), 60)
            await asyncio.sleep(wait)
            continue
        body = None
        if response.status_code == 200 and "html" in response.headers.get("content-type", "html"):
            body = response.text
        return Fetched(response.status_code, str(response.url), body)
    return Fetched(0, url, error="retries exhausted")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=FETCH_TIMEOUT,
        headers=_HEADERS,
        limits=httpx.Limits(max_connections=CONCURRENCY + 1),
    )


def _redirected_away(fetched: Fetched, locale: dict, source_url: str) -> bool:
    """A language version that sends the visitor somewhere else isn't one."""
    final = urlsplit(fetched.final_url)
    if _url_key(fetched.final_url) == _url_key(source_url):
        return True
    if locale.get("host") and final.netloc.lower() != locale["host"]:
        return True
    prefix = locale.get("prefix")
    return bool(prefix) and not (final.path.rstrip("/") + "/").startswith(prefix + "/")


# ------------------------------------------------------------------ the run

_task: asyncio.Task | None = None


def is_running_here() -> bool:
    return _task is not None and not _task.done()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def start_check(site_url: str, trigger: str = "manual") -> int:
    """Starts a check of `site_url` in this process. One at a time, server-wide."""
    active = await db.active_translation_check()
    if active is not None or is_running_here():
        raise RuntimeError("A translation check is already running")
    check_id = await db.create_translation_check(site_url, trigger, RULES_VERSION, OWNER, _now().isoformat())
    _launch(check_id)
    return check_id


def _launch(check_id: int) -> None:
    global _task
    _task = asyncio.create_task(_run_guarded(check_id))


async def _run_guarded(check_id: int) -> None:
    try:
        await run_check(check_id)
    except asyncio.CancelledError:
        # Shutting down. Let the lease go so the next process takes over
        # without waiting for it to go stale.
        try:
            await db.release_translation_check(check_id, OWNER)
        except Exception:
            pass
        raise
    except LostLease:
        logger.info("Translation check %s was taken over by another process", check_id)
    except _Cancelled:
        await db.finish_translation_check(check_id, OWNER, "cancelled", _now().isoformat(),
                                          error="Cancelled from the admin page.")
    except CheckAborted as exc:
        await db.finish_translation_check(check_id, OWNER, "failed", _now().isoformat(), error=str(exc))
        await _notify(check_id)
    except Exception as exc:
        logger.exception("Translation check %s failed", check_id)
        await db.finish_translation_check(check_id, OWNER, "failed", _now().isoformat(),
                                          error=f"Stopped by an error: {type(exc).__name__}")
        await _notify(check_id)


async def run_check(check_id: int) -> None:
    check = await db.get_translation_check(check_id)
    if check is None:
        return
    async with _client() as client:
        if check["status"] == "planning":
            await _plan(client, check)
        await _work(client, check_id)
    await _screenshots(check_id)
    await finish(check_id)


async def _plan(client: httpx.AsyncClient, check: dict) -> None:
    site_url = check["site_url"]
    entries = await fetch_sitemap(client, site_url)
    home = None
    home_fetch = await fetch(client, site_url)
    if home_fetch.html:
        home = await asyncio.to_thread(extract_page, home_fetch.html, home_fetch.final_url)
    plan = build_plan(site_url, entries, home)
    rows = []
    for source, versions in plan.groups.items():
        rows.append((source, "", source, "pending"))
        for code, url in versions.items():
            rows.append((source, code, url, "pending" if url else "missing"))
    locales = {"source": plan.source_language, "source_name": _language_name(plan.source_language),
               "locales": plan.locales, "capped": plan.capped,
               "key_pages": key_pages(site_url, home, plan.groups)}
    if not await db.set_translation_plan(check["id"], OWNER, locales, rows, _now().isoformat()):
        raise LostLease()


async def _work(client: httpx.AsyncClient, check_id: int) -> None:
    check = await db.get_translation_check(check_id)
    meta = json.loads(check["locales_json"] or "{}")
    locales = {locale["code"]: locale for locale in meta.get("locales", [])}
    source_language = meta.get("source") or "en"

    rows = await db.translation_check_rows(check_id)
    groups: dict[str, dict[str, dict]] = collections.defaultdict(dict)
    for row in rows:
        groups[row["source_url"]][row["locale"]] = row
    # Original page -> its version, per language: a link from a translated
    # page to an original that has a version in that language is a gap.
    translated_links: dict[str, dict[str, str]] = collections.defaultdict(dict)
    for source, versions in groups.items():
        for code, row in versions.items():
            if code and row["url"]:
                translated_links[code][_url_key(source)] = row["url"]
    pending = [source for source, versions in groups.items() if versions.get("", {}).get("status") == "pending"]
    if not pending:
        return

    queue: asyncio.Queue[str] = asyncio.Queue()
    for source in pending:
        queue.put_nowait(source)
    outcomes: collections.deque[bool] = collections.deque(maxlen=FAILURE_WINDOW)

    def record(fetched: Fetched) -> None:
        outcomes.append(fetched.failed)
        if len(outcomes) >= _FAILURE_MIN_SAMPLE and sum(outcomes) / len(outcomes) > FAILURE_RATE:
            raise CheckAborted(
                f"Stopped: {sum(outcomes)} of the last {len(outcomes)} pages couldn't be fetched "
                "(blocked, erroring or timing out), so the results wouldn't be trustworthy."
            )

    async def worker():
        while True:
            try:
                source = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await _wait_for_memory()
            if await db.translation_cancel_requested(check_id):
                raise _Cancelled()
            updates = await _check_group(client, source, groups[source], locales, source_language,
                                         translated_links, record)
            if not await db.save_translation_group(check_id, OWNER, updates, _now().isoformat()):
                raise LostLease()

    workers = [asyncio.create_task(worker()) for _ in range(min(CONCURRENCY, len(pending)))]
    try:
        await asyncio.gather(*workers)
    except BaseException:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise


async def _wait_for_memory() -> None:
    """Never the thing that pushes a customer's crawl over the memory limit."""
    from app.crawler import _MEMORY_LIMIT_BYTES, _process

    for _ in range(60):
        if _process.memory_info().rss < 0.85 * _MEMORY_LIMIT_BYTES:
            return
        await asyncio.sleep(10)


async def _check_group(client, source_url: str, versions: dict[str, dict], locales: dict[str, dict],
                       source_language: str, translated_links: dict[str, dict[str, str]], record) -> list[tuple]:
    """One original page and its language versions -> row updates:
    (source_url, locale, url, status, http_status, source_words, untranslated_words, findings_json)."""
    updates: list[tuple] = []
    original = await fetch(client, source_url)
    record(original)
    if not original.html:
        status = "missing" if original.status in (404, 410) else "http_error"
        updates.append((source_url, "", source_url, status, original.status, 0, 0, None))
        for code, row in versions.items():
            if code and row["status"] == "pending":
                updates.append((source_url, code, row["url"], "skipped", None, 0, 0, None))
        return updates

    source = await asyncio.to_thread(extract_page, original.html, original.final_url)
    source_words = source.words
    updates.append((source_url, "", source_url, "ok", original.status, source_words, 0, None))

    for code, row in versions.items():
        if not code:
            continue
        url = row["url"]
        if not url:
            # Not in the sitemap; the page's own head may still declare it.
            url = source.alternates.get(code)
            if not url:
                continue
        locale = locales.get(code, {"code": code})
        fetched = await fetch(client, url)
        record(fetched)
        if fetched.status in (404, 410):
            updates.append((source_url, code, url, "missing", fetched.status, source_words, 0, None))
        elif fetched.status != 200 or not fetched.html:
            updates.append((source_url, code, url, "http_error", fetched.status, source_words, 0, None))
        elif _redirected_away(fetched, locale, source_url):
            updates.append((source_url, code, url, "redirected", fetched.status, source_words, 0, None))
        else:
            target = await asyncio.to_thread(extract_page, fetched.html, fetched.final_url)
            findings, untranslated = compare_page(source, target, code, source_language,
                                                  translated_links.get(code, {}), fetched.final_url)
            updates.append((source_url, code, url, "ok", fetched.status, source_words,
                            min(untranslated, source_words) if source_words else untranslated,
                            json.dumps(findings, ensure_ascii=False)))
    return updates


# ------------------------------------------------------------------ screenshots

async def _screenshots(check_id: int) -> None:
    """The key pages in each language, untranslated text outlined in red.

    Planned once — home first, then the menu's top-level pages, then its
    dropdowns, every language of a page together — so a check taken over
    mid-way only takes the ones still missing. A browser that won't start
    leaves the check without screenshots, never without its results.
    """
    if SCREENSHOTS <= 0:
        return
    check = await db.get_translation_check(check_id)
    meta = json.loads(check["locales_json"] or "{}")
    keys = meta.get("key_pages") or []
    if not keys:
        return
    rows = {(row["source_url"], row["locale"]): row for row in await db.translation_key_page_rows(check_id, keys)}
    if not await db.translation_shots(check_id):
        page_order = {source: i for i, source in enumerate(keys)}
        language_order = {locale["code"]: i for i, locale in enumerate(meta.get("locales", []))}
        wanted = [row for row in rows.values() if row["status"] == "ok" and _marks_for(row)]
        wanted.sort(key=lambda row: (page_order.get(row["source_url"], 10_000),
                                     language_order.get(row["locale"], 100)))
        plan = [(row["source_url"], row["locale"], row["url"]) for row in wanted[:SCREENSHOTS]]
        if not plan:
            return
        if not await db.plan_translation_shots(check_id, OWNER, plan, _now().isoformat()):
            raise LostLease()
    jobs = []
    for shot in await db.translation_shots(check_id):
        if shot["status"] != "pending":
            continue
        texts, alts = _marks_for(rows.get((shot["source_url"], shot["locale"]), {}))
        jobs.append({"source_url": shot["source_url"], "locale": shot["locale"], "url": shot["url"],
                     "texts": texts, "alts": alts})
    if not jobs:
        return

    from app import translation_shots

    async def save(job: dict, file: str | None, marks: int, error: str | None) -> None:
        if await db.translation_cancel_requested(check_id):
            raise _Cancelled()
        if not await db.save_translation_shot(check_id, OWNER, job["source_url"], job["locale"], file, marks,
                                              error, _now().isoformat()):
            raise LostLease()

    try:
        await translation_shots.capture(check_id, jobs, save, _wait_for_memory)
    except translation_shots.BrowserUnavailable as exc:
        logger.warning("No screenshots for translation check %s: %s", check_id, exc)
        for job in jobs:
            await save(job, None, 0, "The browser couldn't start")


def _marks_for(row: dict) -> tuple[list[str], list[str]]:
    """What to outline on a page: its untranslated text, and images whose alt
    text wasn't translated."""
    if not row or not row.get("findings_json"):
        return [], []
    findings = json.loads(row["findings_json"])
    texts = [normalize_key(f[2]) for f in findings if f[0] in TEXT_KINDS]
    alts = [normalize_key(f[2]) for f in findings if f[0] == "attribute" and f[7] == "alt"]
    return (texts, alts) if texts or alts else ([], [])


# ------------------------------------------------------------------ results

async def aggregate(check_id: int) -> dict[tuple[str, str, str], dict]:
    """Every page's findings, grouped by (language, kind, text)."""
    groups: dict[tuple[str, str, str], dict] = {}
    async for row in db.iter_translation_page_findings(check_id):
        code = row["locale"]
        if row["status"] in ("missing", "redirected"):
            key = text_key(row["source_url"])
            groups[(code, "missing_page", key)] = {
                "text": row["source_url"], "words": row["source_words"] or 0, "detail": row["status"],
                "where": "page", "component_id": None, "component_label": None,
                "pages": 1, "samples": [row["url"] or row["source_url"]], "source_url": row["source_url"],
            }
            continue
        if not row["findings_json"]:
            continue
        for kind, key, text, words, where, component_id, component_label, detail in json.loads(row["findings_json"]):
            group = groups.get((code, kind, key))
            if group is None:
                groups[(code, kind, key)] = {
                    "text": text, "words": words, "detail": detail, "where": where,
                    "component_id": component_id, "component_label": component_label,
                    "pages": 1, "samples": [row["url"]], "source_url": row["source_url"],
                }
            else:
                group["pages"] += 1
                if len(group["samples"]) < 3:
                    group["samples"].append(row["url"])
                # Inside a component anywhere means "fix it in the component".
                if component_id and not group["component_id"]:
                    group.update(where=where, component_id=component_id, component_label=component_label)
    return groups


async def finish(check_id: int) -> None:
    """Groups the findings, compares them with the last check and reports."""
    check = await db.get_translation_check(check_id)
    if not await db.touch_translation_check(check_id, OWNER, _now().isoformat()):
        raise LostLease()
    groups = await aggregate(check_id)
    ignored = await db.translation_ignore_keys(check["site_url"])

    def is_ignored(code: str, key: str) -> bool:
        return (code, key) in ignored or ("*", key) in ignored

    previous = await db.previous_completed_translation_check(check["site_url"], check_id)
    diff = {"previous_id": previous["id"] if previous else None, "rules_changed": False,
            "new": 0, "fixed": 0, "removed": 0, "per_locale": {}, "new_top": []}
    new_keys: set[tuple[str, str, str]] = set()
    if previous is not None and previous["rules_version"] != RULES_VERSION:
        diff["rules_changed"] = True
    elif previous is not None:
        before = await db.translation_finding_samples(previous["id"])
        checked_ok = await db.translation_ok_urls(check_id)
        for ident, group in groups.items():
            if ident not in before and not is_ignored(ident[0], ident[2]):
                new_keys.add(ident)
                diff["new"] += 1
                diff["per_locale"].setdefault(ident[0], {"new": 0, "fixed": 0})["new"] += 1
        for ident, samples in before.items():
            if ident in groups or is_ignored(ident[0], ident[2]):
                continue
            if any(url in checked_ok for url in samples):
                diff["fixed"] += 1
                diff["per_locale"].setdefault(ident[0], {"new": 0, "fixed": 0})["fixed"] += 1
            else:
                diff["removed"] += 1
        top = sorted(new_keys, key=lambda ident: (-groups[ident]["pages"], -groups[ident]["words"]))
        diff["new_top"] = [
            {"locale": ident[0], "kind": ident[1], "text": groups[ident]["text"][:160], "pages": groups[ident]["pages"]}
            for ident in top if ident[1] != "missing_page"
        ][:5]

    await db.replace_translation_findings(check_id, groups, new_keys)
    summary = await summarize(check, diff)
    if not await db.finish_translation_check(check_id, OWNER, "completed", _now().isoformat(),
                                             summary=summary, diff=diff):
        raise LostLease()
    # Grouped now; the per-page lists were three quarters of the check's size.
    # The key pages keep theirs, for the page-by-page view beside each screenshot.
    meta = json.loads(check["locales_json"] or "{}")
    await db.clear_translation_page_findings(check_id, keep=meta.get("key_pages") or [])
    from app import translation_shots

    for pruned in await db.prune_translation_checks(check["site_url"], KEEP_RUNS):
        translation_shots.delete(pruned)
    await _notify(check_id)


def _pct_translated(source_words: int, untranslated: int) -> float | None:
    if not source_words:
        return None
    # Floored: 99.96% left untranslated words behind, and "100%" would say it didn't.
    return max(0.0, math.floor(1000 * (1 - untranslated / source_words)) / 10)


async def summarize(check: dict, diff: dict | None = None) -> dict:
    """Per language: versions checked, missing, % translated, open strings,
    new and fixed — with "Keep as is" as it stands now."""
    meta = json.loads(check.get("locales_json") or "{}")
    if diff is None:
        diff = json.loads(check.get("diff_json") or "{}")
    per_locale_diff = diff.get("per_locale", {})
    pages = await db.translation_page_stats(check["id"])
    findings = await db.translation_finding_stats(check["id"], check["site_url"])
    in_english = await db.translation_english_page_counts(check["id"], check["site_url"], ENGLISH_PAGE_SHARE,
                                                          ENGLISH_PAGE_MIN_WORDS)
    locales = []
    for locale in meta.get("locales", []):
        code = locale["code"]
        page = pages.get(code, {})
        found = findings.get(code, {})
        source_words = page.get("source_words") or 0
        untranslated = min(found.get("untranslated_words") or 0, source_words) if source_words else 0
        changes = per_locale_diff.get(code, {})
        locales.append({
            "code": code, "name": locale.get("name") or code,
            "checked": page.get("ok") or 0, "missing": found.get("missing") or 0,
            "errors": page.get("errors") or 0, "unchecked": page.get("unchecked") or 0,
            "source_words": source_words, "untranslated_words": untranslated,
            "translated_pct": _pct_translated(source_words, untranslated),
            "strings": found.get("strings") or 0, "in_english": in_english.get(code, 0),
            "new": changes.get("new", 0), "fixed": changes.get("fixed", 0),
        })
    source = pages.get("", {})
    total_source = sum(item["source_words"] for item in locales)
    total_untranslated = sum(item["untranslated_words"] for item in locales)
    return {
        "source_language": meta.get("source"), "source_name": meta.get("source_name"),
        "source_pages": source.get("ok") or 0, "source_errors": source.get("errors") or 0,
        "capped": bool(meta.get("capped")),
        "locales": locales,
        "translated_pct": _pct_translated(total_source, total_untranslated),
        "strings": sum(item["strings"] for item in locales),
        "missing": sum(item["missing"] for item in locales),
        "in_english": sum(item["in_english"] for item in locales),
        "key_pages": len(meta.get("key_pages") or []),
    }


async def _notify(check_id: int) -> None:
    try:
        from app.notifications import send_translation_report

        check = await db.get_translation_check(check_id)
        if check is None or check["emailed_at"]:
            return
        await send_translation_report(check)
        await db.mark_translation_emailed(check_id, _now().isoformat())
    except Exception:
        logger.exception("Could not send the translation check email for %s", check_id)


# ------------------------------------------------------------------ schedule

async def claim_stale_check() -> bool:
    """Picks up a check whose process died (a deploy, a crash)."""
    if is_running_here():
        return False
    stale_before = (_now() - timedelta(seconds=LEASE_SECONDS)).isoformat()
    check = await db.claim_stale_translation_check(OWNER, _now().isoformat(), stale_before)
    if check is None:
        return False
    if check["resume_count"] > MAX_RESUMES:
        await db.finish_translation_check(
            check["id"], OWNER, "failed", _now().isoformat(),
            error=f"Gave up after the server restarted {MAX_RESUMES} times during this check.",
        )
        await _notify(check["id"])
        return False
    logger.info("Resuming translation check %s of %s", check["id"], check["site_url"])
    _launch(check["id"])
    return True


async def maybe_start_scheduled(now: datetime | None = None) -> int | None:
    """Starts the weekly check of a TRANSLATION_CHECK_SITES site when it's due.

    Due once the last completed check is EVERY_DAYS old (less half a day, so
    it lands at the same hour each week), at HOUR_UTC — or at any hour once
    two days overdue, so one missed night doesn't cost a week. Not within 20
    hours of any attempt, so a site that fails isn't retried all night.
    """
    if not SITES or is_running_here():
        return None
    await claim_stale_check()
    if is_running_here() or await db.active_translation_check() is not None:
        return None
    now = now or _now()
    for site in SITES:
        latest = await db.latest_translation_check(site)
        if latest and datetime.fromisoformat(latest["started_at"]) > now - timedelta(hours=20):
            continue
        completed = await db.latest_translation_check(site, status="completed")
        if completed is not None:
            last = datetime.fromisoformat(completed["started_at"])
            if last > now - timedelta(days=EVERY_DAYS, hours=-12):
                continue
            overdue = last <= now - timedelta(days=EVERY_DAYS + 2)
        else:
            overdue = False
        if now.hour != HOUR_UTC and not overdue:
            continue
        return await start_check(site, "schedule")
    return None


async def shutdown() -> None:
    if is_running_here() and _task.get_loop() is asyncio.get_running_loop():
        _task.cancel()
        try:
            await _task
        except BaseException:
            pass


def text_fragment_url(url: str, text: str) -> str:
    """A link that scrolls to and highlights the text, in browsers that
    support text fragments; the plain page elsewhere."""
    words = text.split()[:8]
    if not words:
        return url
    fragment = quote(" ".join(words), safe="").replace("-", "%2D")
    return f"{url}#:~:text={fragment}"


# ------------------------------------------------------------------ the report

KIND_LABELS = {
    "missing_page": "Missing page",
    "text": "Untranslated text",
    "short_text": "Short string",
    "english_text": "English text",
    "meta": "Title or description",
    "attribute": "Image or UI text",
    "wrong_lang": "Wrong language declared",
    "english_link": "Link to the original",
}
WHERE_LABELS = {"page": "Page", "component": "Component", "cms": "CMS item", "richtext": "Rich text",
                "embed": "Custom code"}

# The report's sections, in order of how much one fix clears.
SECTIONS = {
    "english": {
        "title": "Pages still in the original language",
        "lead": "Language versions where at least half the words are still in the original \u2014 usually a page "
                "that was never translated, or one copied over and left as it was.",
        "kinds": (),
    },
    "components": {
        "title": "Fix once: components",
        "lead": "Text inside a shared Webflow component. Fixing it once in the component clears every page it's on.",
        "kinds": ("text", "short_text", "english_text", "attribute"),
    },
    "repeated": {
        "title": "Repeated text",
        "lead": "The same untranslated text on several pages, outside components — usually a CMS field or a block "
                "copied between pages.",
        "kinds": ("text", "english_text"), "component": False, "single_page": False,
    },
    "pages": {
        "title": "Page by page",
        "lead": "Untranslated text found on one page only, by page: open the page (or its CMS item) and go through it.",
        "kinds": ("text", "english_text"), "component": False, "single_page": True,
    },
    "meta": {
        "title": "Titles & descriptions",
        "lead": "Page titles, meta descriptions and social previews still in the original language.",
        "kinds": ("meta",),
    },
    "attributes": {
        "title": "Image & UI text",
        "lead": "Alt text, tooltips, placeholders, form buttons and “please wait” messages.",
        "kinds": ("attribute",), "component": False,
    },
    "short": {
        "title": "Short strings",
        "lead": "One or two words. Many are real gaps (menu labels, badges); some are names that should stay as they "
                "are — mark those Keep as is.",
        "kinds": ("short_text",), "component": False,
    },
    "missing": {
        "title": "Missing pages",
        "lead": "Original pages with no version in a language, or whose version is a 404 or redirects away.",
        "kinds": ("missing_page",),
    },
    "links": {
        "title": "Links to the original language",
        "lead": "Links on a translated page to the original-language page, though a translated one exists.",
        "kinds": ("english_link",),
    },
    "lang": {
        "title": "Wrong language declared",
        "lead": "Translated pages whose <html lang> doesn't name their language.",
        "kinds": ("wrong_lang",),
    },
}
