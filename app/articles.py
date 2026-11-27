"""The articles: long-form pages under /articles, and the figures they quote.

The numbers here were measured, not invented: WXRKS_COUNT is what Word Counter
found on wxrks.com for "Claude alone vs. with Word Counter", and the homepage's
chat mock-up quotes the same figures so the two never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass

from babel.numbers import format_currency

from app import i18n, surfaces

# Word Counter's count of wxrks.com, from the run behind the article: the whole
# domain, 4 November 2026.
WXRKS_COUNT = {"words": 2_696_246, "pages": 2_494, "repeated_pct": 39, "new": 1_604_010,
               # What Claude suggested quoting on: without the API docs and the forum.
               "quote_scope": 2_176_673}

# What "Claude alone vs. with Word Counter" measured. Session figures are Claude
# Code's own (stream-json result: duration, cost, turns, tokens); word counts
# are from the sessions' answers, Claude's own crawl files and Word Counter's
# run. Round 2 is filled in from its session when it ends.
EXPERIMENT = {
    "published_label": "4 November 2026",
    "read_minutes": 14,
    "run_date": "3–4 November 2026",
    "claude_code": "2.1.220",
    "model": "claude-opus-5 (1M context)",
    "sitemap_urls": 10_609,
    "sitemap_mb": 18.7,
    "english_pages": 1_152,
    "translated_pages": 9_457,
    "languages": 10,
    "home_text_start": "Platform The wxrks Platform Unleash translation super-powers High-level overview tools",
    "alone_quick": {
        "seconds": 674.7, "cost": 1.30, "turns": 26, "tool_calls": 25, "tokens": 1_110_521,
        "all_text_words": 2_256_008, "body_words": 1_660_196, "pages": 1_149,
        "chrome_per_page": 519, "chrome_words": 595_812,
    },
    "alone_careful": {
        "seconds": 3126.1, "cost": 5.96, "turns": 80, "tool_calls": 79, "tokens": 6_132_523,
        "pages_fetched": 10_515, "english_pages": 1_152, "gross_words": 2_242_781, "chrome_words": 700_928,
        "content_words": 1_541_853, "new_words": 1_114_098, "repeated_pct": 50.3,
        "img_w": 1600, "img_h": 4378,
    },
    "alone_careful_cell": "52 minutes, $5.96 — its own crawler over all 10,515 pages, in every language, and a CAT-style "
                          "repetition analysis",
    "alone_repetition": "None in the quick answer. Asked for it: 50.3%, by its own sentence matching — menus and "
                        "footers included",
    "alone_repetition_why": "Asked for it, Claude measured repetition too — 50.3% for the English pages, menus and "
                            "footers included, since its script had counted them on every page. Not asked, it never "
                            "mentioned it.",
    "alone_careful_story": [
        "Asked for an accurate count for a quote, Claude went all in. It downloaded the sitemap again, wrote a "
        "crawler and fetched <strong>10,515 pages</strong> — every language, not just English. Then it spent most of "
        "an hour on its own HTML parser: separating the menus and footers from the content, and chasing why some "
        "product copy looked hidden (Webflow’s conditional-visibility classes) before deciding the parser was right.",
        "After <strong>52 minutes, 79 tool calls and $5.96</strong> it delivered a thorough report: 2,242,781 words on "
        "the 1,152 English pages, 700,928 of them menus and footers and 1,541,853 of page content; 1,114,098 new words "
        "after removing repeats, a 50.3% repetition rate; and the same for all ten translations.",
        "That is serious work, and its numbers for the main site are in the same range as Word Counter’s. It also "
        "fetched nine times the pages an English quote needs, took the sitemap for the whole website — so the help "
        "center, the docs and the community never came up — and the next site starts from zero again.",
    ],
    "with_quick": {
        "seconds": 45.3, "cost": 0.19, "turns": 3, "tool_calls": 2, "tokens": 97_311,
        "paused_words": 50_683, "paused_pages": 26, "estimate_words": 2_241_748, "estimate_pages": 1_150,
        "repeated_pct": 25.5,
    },
    "with_full": {
        "seconds": 2105.9, "cost": 1.34, "turns": 41, "tool_calls": 40, "checks": 37, "tokens": 1_654_679,
        "crawl_seconds": 2115, "words": 2_696_246, "pages": 2_494, "repeated": 1_064_373,
        "repeated_pct": 39.5, "new": 1_604_010, "duplicates": 799, "behind_login": 14,
        "main_words": 1_718_712, "main_pages": 1_050, "subdomains": 11,
        "subdomain_words": 977_534, "subdomain_pages": 1_444,
        "img_w": 1600, "img_h": 3020,
    },
    # The 1,021 pages both crawls fetched: Claude's script against Word Counter.
    "common": {"pages": 1_021, "claude": 2_166_342, "word_counter": 1_691_655, "more_pct": 28.1,
               "median_extra": 478},
    "report_imgs": {"summary": (1600, 305), "repetition": (1600, 1524), "folders": (1600, 790),
                    "subdomains": (1600, 879), "left-out": (1600, 363)},
    "logs": [
        {"file": "session-1-alone-quick.json", "label": "Round 1"},
        {"file": "session-2-alone-careful.json", "label": "Round 2"},
        {"file": "session-3-with-quick.json", "label": "Round 3"},
        {"file": "session-4-with-full.json", "label": "Round 4"},
    ],
}


def _minutes(seconds: float) -> str:
    return f"{seconds:.0f} seconds" if seconds < 90 else f"{seconds / 60:.1f} minutes"


def _comparison() -> dict:
    """The three ways to count a website, row by row — the article's comparison.

    Every claim is something the product does today (or doesn't: the MCP server
    can't sign in to a site, and can't leave folders out), and every number is
    from EXPERIMENT. A state is "yes", "partly", "no", or None for a row of
    plain figures."""
    x = EXPERIMENT
    aq, ac, wq, wf, cm = x["alone_quick"], x["alone_careful"], x["with_quick"], x["with_full"], x["common"]
    crawl = f"{wf['crawl_seconds'] / 60:.0f} minutes"   # "a 35-minute count", as the article says

    def row(feature, alone, web, mcp):
        return {"feature": feature, "cells": [dict(zip(("state", "text"), cell)) for cell in (alone, web, mcp)]}

    return {
        "columns": [
            {"name": "An assistant on its own", "sub": "Measured with Claude Code. A chat app without a terminal can do less."},
            {"name": "The Word Counter website", "sub": f"{surfaces.COUNTER.host}, in your browser"},
            {"name": "Your assistant + Word Counter", "sub": "Claude, ChatGPT, Cursor or VS Code, through the MCP server"},
        ],
        "groups": [
            {"name": "What gets counted", "rows": [
                row("Menus and footers",
                    ("partly", f"Counted on every page unless you ask; then it writes a parser to strip them. On "
                               f"wxrks.com its first count had {cm['more_pct']}% more words."),
                    ("yes", "Left out of every page: it counts the main text a visitor reads."),
                    ("yes", "The same count as the website.")),
                row("Text that needs JavaScript",
                    ("no", "curl and web-fetch tools read the HTML the server sends: text a script adds later is "
                           "missed, and hidden text is counted."),
                    ("yes", "Every page is opened in a real browser and counted once it has loaded."),
                    ("yes", "The same browser, on Word Counter’s servers.")),
                row("Pages the sitemap doesn’t list",
                    ("partly", f"Up to the script it writes. On wxrks.com it trusted the sitemap and missed "
                               f"{wf['subdomains']} subdomains: {wf['subdomain_words']:,} words."),
                    ("yes", "It follows links, as a visitor would, so no sitemap is needed. The whole domain or one "
                            "host, each subdomain on its own line."),
                    ("yes", "The same; ask for the whole domain or one host.")),
                row("Repetition",
                    ("partly", f"Only when asked, by a method it writes that day: {ac['repeated_pct']}% on "
                               f"wxrks.com, menus included."),
                    ("yes", "On every count, by sentence: repeated and near-repeated words, and the text that "
                            "repeats most."),
                    ("yes", "The rate in every answer; the sentences in the report.")),
                row("Duplicate pages",
                    ("partly", "Up to the script it writes."),
                    ("yes", f"Counted once and listed: {wf['duplicates']} on wxrks.com."),
                    ("yes", "The same.")),
                row("Translated versions",
                    ("no", f"Crawled with the rest: {ac['pages_fetched']:,} pages on wxrks.com, nine times what "
                           f"an English quote needs."),
                    ("yes", "Detected, set aside and listed. Or choose the languages to count."),
                    ("yes", "Detected, set aside and listed. To choose a language, use the website.")),
                row("The same rules every time",
                    ("no", "A new program for every site and every session; ask twice and the steps differ."),
                    ("yes", "One definition on every site, and the report shows what it left out."),
                    ("yes", "The same.")),
            ]},
            {"name": "Running the count", "rows": [
                row("Runs without your computer",
                    ("no", "Claude Code crawls from your computer, through your connection, while the session "
                           "stays open. A chat app usually can’t run a crawler at all."),
                    ("yes", "On Word Counter’s servers. Close the tab; signed in, you get an email when it’s done."),
                    ("yes", "On Word Counter’s servers. Your assistant checks in, or you ask again later.")),
                row("First answer for wxrks.com",
                    (None, f"{_minutes(aq['seconds'])}, ${aq['cost']:.2f}"),
                    (None, "Under a minute: the same estimate, live on the page. No AI usage."),
                    (None, f"{_minutes(wq['seconds'])}, ${wq['cost']:.2f}")),
                row("Full count of wxrks.com",
                    (None, f"{_minutes(ac['seconds'])}, ${ac['cost']:.2f}: every language, no subdomains"),
                    (None, f"{crawl}, free"),
                    (None, f"{crawl}, ${wf['cost']:.2f} of assistant usage; the crawl is free")),
                row("Large sites",
                    ("partly", "Limited by the session, the context window and your spending cap."),
                    ("yes", "No page limit with a free account. It pauses at an estimate and carries on when you "
                            "say so."),
                    ("yes", "The same; your assistant asks before counting the rest.")),
                row("Sites behind a password or a sign-in",
                    ("partly", "Only if you paste a password or a session cookie into the conversation."),
                    ("yes", "Enter them on the crawl form: sent only to that site, used only for that crawl, never "
                            "stored."),
                    ("no", "Start those counts on the website.")),
                row("Leaving folders or subdomains out",
                    ("yes", "Describe them, and it writes that into its script."),
                    ("yes", "Exclude them before the crawl, or when the estimate appears."),
                    ("partly", "One host or the whole domain; filter the pages by folder afterwards.")),
            ]},
            {"name": "After the count", "rows": [
                row("Something to show your client",
                    ("no", "A chat transcript, and files on your computer."),
                    ("yes", "A report with every page, a share link, and an email sent from the report."),
                    ("yes", "Every count is also a report on the website, with the same share link.")),
                row("Every page, itemised",
                    ("partly", "Whatever files its script left in the work folder."),
                    ("yes", "A CSV of every page, and the text of every page as Markdown if you ask for it."),
                    ("yes", "Pages by folder or address in the chat; the CSV in the report.")),
                row("Several sites for one client",
                    ("partly", "Count each one again and combine them by hand."),
                    ("yes", "Group them in a project: each page and each sentence counted once across the crawls, "
                            "with a view for your client."),
                    ("partly", "The counts are in your account; group them on the website.")),
                row("Next month",
                    ("no", "Gone with the session; the next count starts from zero."),
                    ("yes", "Every count is kept in your account; re-crawl from the report."),
                    ("yes", "Ask for your earlier counts, or a fresh one.")),
                row("Questions about the numbers",
                    ("yes", "Ask anything about its own count."),
                    ("partly", "A report, not a conversation: you read the numbers yourself."),
                    ("yes", "Your assistant reads the report. On wxrks.com it suggested what to leave out of the "
                            "quote.")),
                row("What you need",
                    (None, "An AI subscription or API credit"),
                    (None, "A browser. No account to start; a free one for the full count."),
                    (None, "A free Word Counter account, and your AI app")),
            ]},
        ],
    }


COMPARISON = _comparison()


def usd(amount: float, lang: str) -> str:
    """A dollar amount the way the reader's language writes one. The currency
    is what was paid (dollars), whatever the language."""
    return format_currency(amount, "USD", locale=i18n.locale_for(lang))


def _home_vs() -> dict:
    """The front page's "Why not just ask ChatGPT or Claude?": the same run,
    reduced to the figures its two cards quote."""
    x = EXPERIMENT
    aq, ac, wf = x["alone_quick"], x["alone_careful"], x["with_full"]
    return {
        "alone_words": aq["all_text_words"],
        "more_pct": round(x["common"]["more_pct"]),
        "alone_pages_fetched": ac["pages_fetched"],
        "alone_minutes": round(ac["seconds"] / 60),
        # The front page is translated, so the amount is formatted for its
        # reader when it renders — "$5.96" in English, "5,96 $" in German —
        # with alone_cost_in(lang). alone_cost is the English form.
        "alone_cost": usd(ac["cost"], "en"),
        "alone_cost_usd": ac["cost"],
        "alone_cost_in": lambda lang: usd(ac["cost"], lang),
        "main_words": wf["main_words"],
        "subdomains": wf["subdomains"],
        "subdomain_words": wf["subdomain_words"],
        "repeated_pct": wf["repeated_pct"],
        "translated_pages": x["translated_pages"],
        "minutes": round(wf["crawl_seconds"] / 60),
        "differences": sum(len(group["rows"]) for group in COMPARISON["groups"]),
    }


HOME_VS = _home_vs()


@dataclass(frozen=True)
class Article:
    slug: str
    template: str
    title: str
    description: str
    published: str          # ISO date
    image: str              # under /static/articles/


ARTICLES = [
    Article(
        slug="how-many-words-claude-vs-word-counter",
        template="articles/claude_vs_word_counter.html",
        title="How many words does a website have? Claude alone vs. Claude with Word Counter",
        description=("We asked Claude Code how many words wxrks.com has — on its own, and connected to Word "
                     "Counter's MCP server — measured the answers, the time, the cost and the accuracy, and compared "
                     "all three ways to count a website."),
        published="2026-11-03",
        image="/static/articles/claude-vs-word-counter/cover.png",
    ),
]
BY_SLUG = {article.slug: article for article in ARTICLES}
