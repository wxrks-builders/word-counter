# LOCALIZATION.md

How Word Counter handles languages. Read this before writing text a person
will see, and update it when a decision changes.

## Languages

- Source language: en (no prefix: `/`, `/runs`)
- Target languages: ar, de, es, fr, it, ja, ko, pt, sv, zh — the ten
  wxrks.com publishes, addressed by short prefix (`/de/runs`). Their hreflang
  is regional where wxrks.com's is: de-DE, fr-FR, pt-BR (`HREFLANG` in
  `app/i18n.py`). zh is Simplified Chinese.
- Fallback: a message with no translation shows in English (gettext). An
  untranslated plural is picked by English rules, on the page and in app.js.
- First visit: the URL decides. An English page offers the visitor's own
  language in a banner written in that language — the one they chose before
  (`lang` cookie), else the best `Accept-Language` match — and remembers the
  answer. Never a redirect, never by IP.
- Right-to-left languages in scope: ar (`RTL_LANGUAGES`).
- Published: a language goes into hreflang, the sitemap and the banner, and
  becomes its own canonical, once 90% of its catalogue is translated
  (`PUBLISH_AT`). Below that its pages work but point their canonical at
  English, so ten untranslated copies aren't indexed as translations.

## Where the text lives

- Messages: `locales/<lang>/LC_MESSAGES/messages.po`, extracted to
  `locales/messages.pot`. `.mo` files are build output (the Dockerfile
  compiles them with `scripts/translations.py compile`, never `pybabel
  compile`, which fills untranslated plurals with English).
- Format: gettext, through Babel and Jinja's i18n extension. `_()` for text,
  `ngettext()` for anything with a count, `pgettext()` for a word with two
  meanings ("Crawl" the button, "Crawl" the noun), `N_()` for module-level text
  translated at render.
- Plurals: always `ngettext('%(n)s page', '%(n)s pages', count, n=num(count))` —
  the raw count picks the form, the formatted number fills it, and the English
  singular carries `%(n)s` too. Never `if n == 1`.
- app.js text: `app/js_strings.py`, in the same catalogue, rendered into each
  page. Plural entries reach the browser as one string per CLDR category and
  are picked with `Intl.PluralRules`; `t(key, {n: n(x), count: x})`.
- Context notes for translators: `# Translators:` (Python) and
  `{# Translators: … #}` (templates), on the line before the call; they
  arrive in the `.po` as `#.` lines. Extract with
  `--add-comments=Translators:` (see `scripts/translations.py`).
- Approval status: kept in wxrks, the translation platform.
- Key style: the English text is the key (gettext). Rewording a string makes
  it a new one that needs translating, which is correct.
- Content from the database: none translated. Crawled pages, titles and
  project names are the user's data and are shown as they are.
- Images with text: `app/static/brand/og-counter.png` and `og-markdown.png`
  (the share images) carry English text and US-formatted numbers. One image
  per language is still to be made; until then every language shares them.

## Formatting

- Numbers: Babel on the server (`num()`, `i18n.format_number`), `Intl` in
  app.js, both from the page's locale. Arabic uses Western digits in both
  (`INTL_LOCALES`: ar-u-nu-latn), so the two never disagree on a page.
- Dates: stored in UTC, rendered by app.js with `Intl.DateTimeFormat` in the
  reader's language and time zone (`data-iso` + `formatRunDates()`).
- Durations: messages (`i18n.duration_text`, mirrored by `durationText()` in
  app.js) so each language abbreviates its own units.
- Money: `money(amount, currency)` / `i18n.format_money`. The currency is the
  price's (pounds), never the reader's; only how it is written follows the
  language.
- Language and script names: `Intl.DisplayNames` in app.js; the language menu
  names each language in itself (`LANGUAGE_NAMES`).
- Separators: `·` between independent items is layout and stays as is. A list
  inside a sentence uses `Intl.ListFormat` (app.js) or the
  `pgettext('list separator', ', ')` message (templates).

## Not translated

These stay in English on purpose. The checker skips them.

- app/templates/admin_*.html
- app/templates/articles/*.html
- app/static/articles/**/*
- app/mcp_server.py

Also English by decision, inside files that are otherwise translated:

- The admin links in the account menu (marked `lang="en"`) and the admin
  emails (`send_server_alert`, `send_translation_report`).
- What the MCP server returns: assistants read it, not people.
- CSV column headers and status values: data files that spreadsheets and
  scripts read; the project CSVs are cached and shared by every language.
- The Crawl4AI credit in the footer: its license asks for that sentence as
  written (`translate="no" lang="en"`).
- Technical reasons a page failed (a DNS or TLS error from the browser or the
  remote server). The crawler's own fixed sentences are translated where they
  are shown (`report.stored_text`).

## Never translate

- wxrks
- Crawl4AI, Playwright, Chromium
- MCP, Claude, Claude Code, ChatGPT, Cursor, VS Code
- Markdown, CSV, ZIP, JSON, HTML
- Platform names the crawler detects (WordPress, Webflow, Contentful, …)
- The brand lockup in the header (`translate="no"`)

Word Counter, Site to Markdown and Pro are names: translate around them, not
through them, unless a language's convention says otherwise. Decide that per
language with wxrks and record it here.

## Glossary

Terms that must be translated the same way everywhere. Fill a row the first
time a term is decided.

| Term | Notes | ar | de | es | fr | it | ja | ko | pt-BR | sv | zh |
|---|---|---|---|---|---|---|---|---|---|---|---|
| crawl (noun) | one run over a site; "Your crawls" | | | | | | | | | | |
| crawl (verb) | the button that starts one | | | | | | | | | | |
| word count | the product's number | | | | | | | | | | |
| repeated / repetition | sentences seen on another page, as translation-memory tools count them | | | | | | | | | | |
| near repeat | a sentence almost the same as another | | | | | | | | | | |
| duplicate page | the same text under another address, counted once | | | | | | | | | | |
| project | several crawls of one client counted together | | | | | | | | | | |
| consolidate | count a project's crawls together, each page and sentence once | | | | | | | | | | |
| sitemap | the site's list of its pages | | | | | | | | | | |
| subdomain / folder | the two kinds of exclusion | | | | | | | | | | |
| estimate | the projected count at the pause | | | | | | | | | | |
| connection | a personal MCP URL for an assistant | | | | | | | | | | |

## Voice per language

The English is direct and plain, second person ("your crawl"). Each language
addresses the reader the way wxrks.com does, so the two sites sound alike:

- es: tú, never usted
- pt-BR: você
- fr: vous
- de: Sie
- it: tu
- sv: du
- ja: です/ます
- ko: 합니다 style
- zh: 您
- ar: Modern Standard Arabic

## Markets

| Market | Locale | Currency | Notes |
|---|---|---|---|
| Everywhere | any of the eleven | GBP | One Stripe price; Stripe Checkout picks its own language from the browser. |

## Review

- Translations are made and reviewed in wxrks, then imported with
  `python scripts/translations.py import <zip>`, which refuses broken
  placeholders and half-filled plural forms.
- Always reviewed by a person who speaks the language before going live:
  pricing and plan copy, sign-in and account messages, the emails.

## Decisions

- 2026-11-27: the voice per language matches wxrks.com (see Voice per language).
- 2026-11-27: plurals through the catalogue (`ngettext`), never `n == 1`;
  app.js gets one string per CLDR category.
- 2026-11-27: a language is published (hreflang, sitemap, canonical, banner)
  at 90% translated; untranslated languages point their canonical at English.
- 2026-11-27: Arabic numbers in Western digits, page and script alike.
- 2026-11-27: admin pages, articles, MCP output and CSV headers stay English.
- 2026-11-27: the account's language (used for its emails) is updated only by
  pages the user opens in a language, never by app.js's requests.
