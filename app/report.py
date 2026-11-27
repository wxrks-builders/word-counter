"""Server-side aggregation for the report page.

The page used to be handed every crawled page and did all of this in the
browser. On a large crawl that meant tens of MB of embedded JSON and a row in
the DOM for every page — 164k pages came to 46MB of HTML and a million DOM
nodes, which is what made the tab hang. The grouping is cheap; it's the raw
rows that are expensive to ship, so it happens here and only the totals go out.

The folder/language rules mirror folderForUrl() and languageForUrl() in
app.js, which still run in live mode while a crawl is streaming.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from app import i18n
from app.i18n import N_
from app.models import PageResult

# Sentences the crawler stores in English, because it runs with no request and
# so no reader to write for. They are translated where they are shown; this
# list puts the ones written in app/crawler.py without N_() into the catalogue.
# Anything else in a page's error is the browser's or the remote server's own
# words (a DNS failure, a TLS error) and is shown as it came.
_STORED = (
    # Translators: why a page wasn't counted, in the report's list of pages
    # that failed and in its tooltips.
    N_("Requires login"),
    N_("Duplicate of a page already counted"),
    N_("Failed to fetch page"),
    N_("This link points to a downloadable file, not a webpage"),
)
# Stored already formatted ("…for over 10 minutes…"), so the msgid it came
# from (in app/crawler.py) is rebuilt to look it up.
_STALLED = re.compile(r"^This crawl stalled — no page was fetched for over (\d+) minutes — and was stopped automatically\.$")
_STALLED_MSGID = ("This crawl stalled — no page was fetched for over %(minutes)s "
                  "minutes — and was stopped automatically.")


# Why a project's consolidation stopped (app/consolidation_job.py), stored the
# same way. The size one is stored with its numbers in, "{:,}"-grouped.
_TOO_LARGE = re.compile(r"^Too large to consolidate here: ([\d,]+) pages across its crawls "
                        r"\(this server's limit is ([\d,]+)\)\.$")


def stored_text(text: str | None, lang: str) -> str | None:
    """A sentence the crawler or a consolidation stored, in the reader's
    language when it is one of the fixed ones (and in the catalogue), as stored
    otherwise."""
    if not text or lang == i18n.DEFAULT_LANG:
        return text
    _ = i18n.gettext_for(lang)
    stalled = _STALLED.match(text)
    if stalled:
        minutes = int(stalled.group(1))
        return _(_STALLED_MSGID) % {"minutes": i18n.format_number(minutes, lang)}
    too_large = _TOO_LARGE.match(text)
    if too_large:
        pages, limit = (int(group.replace(",", "")) for group in too_large.groups())
        ngettext = i18n.ngettext_for(lang)  # by this name, so pybabel extracts it
        # Translators: why a project couldn't be consolidated. %(limit)s is the
        # server's page limit; the plural follows the pages.
        return ngettext(
            "Too large to consolidate here: %(n)s page across its crawls (this server's limit is %(limit)s).",
            "Too large to consolidate here: %(n)s pages across its crawls (this server's limit is %(limit)s).",
            pages,
        ) % {"n": i18n.format_number(pages, lang), "limit": i18n.format_number(limit, lang)}
    return _(text)


def consolidation_status(state: dict, lang: str) -> dict:
    """consolidation_job.status() with its stored error in the reader's language."""
    if state.get("error"):
        return {**state, "error": stored_text(state["error"], lang)}
    return state

# How much of each list the report page gets up front. The full page list is
# paged in on demand; these two are shown in full behind a "Show all".
TOP_PAGES = 100
MAX_ISSUES = 500
PAGE_ROWS = 100

ISO_639_1_CODES = {
    "aa","ab","ae","af","ak","am","an","ar","as","av","ay","az","ba","be","bg","bh","bi","bm","bn",
    "bo","br","bs","ca","ce","ch","co","cr","cs","cu","cv","cy","da","de","dv","dz","ee","el","en",
    "eo","es","et","eu","fa","ff","fi","fj","fo","fr","fy","ga","gd","gl","gn","gu","gv","ha","he",
    "hi","ho","hr","ht","hu","hy","hz","ia","id","ie","ig","ii","ik","io","is","it","iu","ja","jv",
    "ka","kg","ki","kj","kk","kl","km","kn","ko","kr","ks","ku","kv","kw","ky","la","lb","lg","li",
    "ln","lo","lt","lu","lv","mg","mh","mi","mk","ml","mn","mr","ms","mt","my","na","nb","nd","ne",
    "ng","nl","nn","no","nr","nv","ny","oc","oj","om","or","os","pa","pi","pl","ps","pt","qu","rm",
    "rn","ro","ru","rw","sa","sc","sd","se","sg","si","sk","sl","sm","sn","so","sq","sr","ss","st",
    "su","sv","sw","ta","te","tg","th","ti","tk","tl","tn","to","tr","ts","tt","tw","ty","ug","uk",
    "ur","uz","ve","vi","vo","wa","wo","xh","yi","yo","za","zh","zu",
}


def _first_segment(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    # Matches `new URL(...)` throwing in the browser, which the JS treats as
    # "(root)" — anything without a scheme and host isn't a URL to split.
    if not parts.scheme or not parts.netloc:
        return None
    for segment in parts.path.split("/"):
        if segment:
            return segment
    return None


def folder_for_url(url: str) -> str:
    segment = _first_segment(url)
    return f"/{segment}" if segment else "(root)"


def language_for_url(url: str) -> str:
    segment = _first_segment(url)
    if not segment:
        return "Default"
    code = segment.lower().split("-")[0].split("_")[0]
    return code if len(code) == 2 and code in ISO_639_1_CODES else "Default"


def host_for_url(url: str) -> str:
    """The subdomain a page is on, as the report names it — "www." dropped,
    since www.site.com and site.com are the same site to a reader."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host.removeprefix("www.")


# Longest "linked but not crawled" lists the report shows.
MAX_LEFT_OUT = 50


class LinkCensus:
    """How many crawled pages link to each subdomain and each language folder
    of the site — including the ones the crawl was set to skip, which is the
    point: a skipped subdomain or /de/ folder is otherwise invisible in the
    report, and "was that ignored or counted?" has no answer.

    Counted once per page, not per link, so a footer link on every page reads
    as "linked from 400 pages". Only the site's own registrable domain is
    looked at; links elsewhere are someone else's site.
    """

    def __init__(self, site: str, hosts: dict[str, int], languages: dict[str, int]):
        self.site = site.lower().removeprefix("www.")
        self.hosts = hosts
        self.languages = languages

    def observe(self, hrefs) -> None:
        hosts: set[str] = set()
        languages: set[str] = set()
        for href in hrefs:
            host = host_for_url(href or "")
            if not host or not (host == self.site or host.endswith("." + self.site)):
                continue
            hosts.add(host)
            language = language_for_url(href)
            if language != "Default":
                languages.add(language)
        for host in hosts:
            self.hosts[host] = self.hosts.get(host, 0) + 1
        for language in languages:
            self.languages[language] = self.languages.get(language, 0) + 1


def left_out(pages: list[PageResult], linked_hosts: dict[str, int], linked_languages: dict[str, int],
             other_languages: dict[str, dict] | None = None) -> dict:
    """Subdomains and language folders the crawled pages link to that the run
    didn't count — by its settings, its exclusions, or because it stopped
    first. Empty for runs from before the census existed."""
    crawled_hosts = {host_for_url(p.url) for p in pages}
    crawled_languages = {language_for_url(p.url) for p in pages}

    def ranked(counts: dict[str, int], crawled: set[str]) -> list[dict]:
        rows = [{"name": name, "linked_from": n} for name, n in counts.items() if name not in crawled]
        return sorted(rows, key=lambda r: (-r["linked_from"], r["name"]))[:MAX_LEFT_OUT]

    set_aside = sorted(
        ({"name": code, "pages": v.get("pages", 0), "skipped": v.get("skipped", 0), "example": v.get("example", "")}
         for code, v in (other_languages or {}).items()),
        key=lambda r: (-(r["pages"] + r["skipped"]), r["name"]),
    )
    return {
        "skipped_hosts": ranked(linked_hosts, crawled_hosts),
        "skipped_languages": ranked(linked_languages, crawled_languages),
        # Read, found to be another language, and not counted — see
        # app/language_check.py. "skipped" is what its folder had left unfetched.
        "set_aside_languages": set_aside,
        "census_recorded": bool(linked_hosts),
    }


def _grouped(pages: list[PageResult], key) -> list[dict]:
    groups: dict[str, dict] = {}
    for page in pages:
        name = key(page.url)
        group = groups.get(name)
        if group is None:
            group = groups[name] = {"name": name, "pages": 0, "words": 0}
        group["pages"] += 1
        group["words"] += page.word_count or 0
    return sorted(groups.values(), key=lambda g: g["words"], reverse=True)


def page_row(page: PageResult, lang: str = i18n.DEFAULT_LANG) -> dict:
    """Only the fields the table actually renders — PageResult carries more.
    The error is in the reader's language where it is one of the crawler's
    fixed sentences (see stored_text)."""
    return {
        "url": page.url,
        "title": page.title or "",
        "word_count": page.word_count,
        "success": page.success,
        "blocked_by_host": page.blocked_by_host,
        "error": stored_text(page.error, lang),
    }


def summarize(pages: list[PageResult], lang: str = i18n.DEFAULT_LANG) -> dict:
    blocked = [p for p in pages if p.blocked_by_host]
    failed = [p for p in pages if not p.success and not p.blocked_by_host]
    top = sorted((p for p in pages if p.success), key=lambda p: p.word_count, reverse=True)[:TOP_PAGES]

    return {
        "folders": _grouped(pages, folder_for_url),
        "hosts": _grouped(pages, host_for_url),
        "languages": _grouped(pages, language_for_url),
        "top_pages": [{"url": p.url, "word_count": p.word_count} for p in top],
        "top_pages_capped": len([p for p in pages if p.success]) > TOP_PAGES,
        "blocked_count": len(blocked),
        "failed_count": len(failed),
        "issues": [
            {"url": p.url, "error": stored_text(p.error, lang), "blocked_by_host": p.blocked_by_host}
            for p in (blocked + failed)[:MAX_ISSUES]
        ],
        "issues_capped": len(blocked) + len(failed) > MAX_ISSUES,
        "total_pages": len(pages),
    }
