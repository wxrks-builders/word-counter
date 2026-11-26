# Word Counter

Count every word on a website, page by page and folder by folder, before you
quote, translate or rewrite it. The same app also exports a whole site as clean
Markdown (Site to Markdown).

It runs free at **[wordcount.wxrks.com](https://wordcount.wxrks.com)**. This
repository is the code behind it, under the [AGPL-3.0](LICENSE).

## What it counts

- **The text people read.** Every page is opened in a real browser
  ([Crawl4AI](https://github.com/unclecode/crawl4ai) on Playwright's
  Chromium), so text built by JavaScript is counted. Menus, footers and forms
  are left out, so the same navigation isn't counted once per page.
- **Every page, sitemap or not.** It follows links the way a visitor would,
  across subdomains if you ask, and lists each subdomain on its own line.
- **Each page once.** Duplicate pages (the same text under another address)
  are counted once and listed.
- **Translations set aside.** Pages in another language are detected and left
  out of the count, or you choose the languages to count.
- **Repetition.** Every count estimates how much of the text repeats across
  pages, by sentence, the way translation-memory tools discount it.

## What you get

- A report per crawl: totals, folders, subdomains, languages, the most repeated
  text, and every page. Share it with a link or by email, or print it.
- A CSV of every page, and each page's text as Markdown in one ZIP.
- Projects: several crawls of one client counted together, with each page and
  each sentence counted once across them.
- Sites behind a sign-in: crawl with an email and password, or a pasted
  session.
- An [MCP server](https://modelcontextprotocol.io) at `/mcp`, so Claude,
  ChatGPT, Cursor or VS Code can run counts and read the reports.
- The interface in eleven languages.

## Running it locally

You need Python 3.12 and about 1 GB of disk for Chromium.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium
crawl4ai-setup

cp .env.example .env
```

In `.env`, set at least:

- `SESSION_SECRET`: any long random string
  (`python -c "import secrets; print(secrets.token_hex(32))"`).
- `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`: a Google OAuth client with
  `http://localhost:8000/auth/callback` as a redirect URI. With placeholder
  values the app still starts and you can crawl as a guest, up to the point
  where a large site asks you to sign in.
- `SECURE_COOKIES=false`, since localhost is plain http.

Then:

```sh
pybabel compile -d locales   # the translated interface; English works without it
uvicorn app.main:app --reload --workers 1
```

Open <http://localhost:8000>. Every other setting is optional and described in
[`.env.example`](.env.example): email (Mailgun), billing (Stripe), crawl limits,
and the hostnames each product answers on.

### With Docker

```sh
docker build -t word-counter .
docker run -p 8000:8000 --env-file .env -v "$PWD/data:/app/data" word-counter
```

Keep it to one container and one worker: crawl progress lives in process
memory. [DEPLOY.md](DEPLOY.md) explains why, and everything else a production
deploy needs (a persistent volume, memory limits, proxy headers, hostnames).

## Tests

```sh
pytest
```

A few tests crawl a local test server with the real browser, so they need the
Chromium install above.

## Translating the interface

Strings are marked with `_()` in Python and templates and live in `locales/`.
After changing one:

```sh
pybabel extract -F babel.cfg -o locales/messages.pot --no-location --sort-output .
pybabel update -i locales/messages.pot -d locales
```

An empty translation falls back to English, so a partly translated language is
safe to ship.

## Where things are

| Path | What it holds |
| --- | --- |
| `app/main.py` | The web app: routes, pages, the MCP endpoint |
| `app/crawler.py` | The crawl: fetching, the frontier, counting, saving Markdown |
| `app/word_count.py`, `app/repetition.py` | How words and repeated sentences are counted |
| `app/url_policy.py` | Which links are followed, and which addresses are the same page |
| `app/projects.py`, `app/consolidation.py` | Counting several crawls together |
| `app/mcp_server.py` | The tools assistants call |
| `app/surfaces.py` | The two products (Word Counter, Site to Markdown) and their hostnames |
| `app/templates/`, `app/static/` | Pages, styles and scripts |
| `webgl/` | Source of the front page's WebGL hero (built into `app/static/hero-webgl.js`) |
| `tests/` | The test suite |

## Contributing

Issues and pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).
To report a security problem privately, see [SECURITY.md](SECURITY.md).

## License

Word Counter is free software under the
[GNU Affero General Public License v3.0](LICENSE). If you run a modified copy
as a service, the AGPL asks you to offer your users its source: set
`SOURCE_CODE_URL` to your repository and the footer links to it.

The wxrks name and logo are trademarks of wxrks and aren't covered by the
license; a fork that runs publicly should use its own. Third-party software,
fonts and icons keep their own licenses, listed in [NOTICE](NOTICE).
