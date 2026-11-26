"""Crawling a site behind a sign-in page: an email and password on a form, not
the browser's own pop-up (that one is app/site_auth.py).

Two ways in, both under Advanced on the crawl form:

- Sign in for the crawl. The app opens the sign-in page in its own browser,
  fills it in — also when the password is asked for on a second screen —
  submits it, and checks the site really let it in. What it keeps is the
  session the site handed back; the password is dropped the moment sign-in
  is done.
- Paste a session. For a sign-in a password alone can't get through (a code,
  "Sign in with Google"), someone signs in in their own browser and pastes
  its Cookie header.

Either way the session lives on the in-memory Job for that crawl only — never
the database, the logs, the report or the email — and goes to the start URL's
exact host and nothing else: cookies are set host-only, whatever domain the
site gave them. A crawler signed in as someone follows every link it finds,
so a signed-in crawl never visits a link that would sign out, delete, cancel
or unsubscribe (SignedInLinkFilter in app/crawler.py uses unsafe_link).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

from app.site_auth import SiteAuth

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
OPEN_TIMEOUT_MS = 30_000

SESSION_GONE = (
    "The server restarted during this crawl, and the site's sign-in is never "
    "kept — re-crawl and sign in again to continue."
)


class SignInError(Exception):
    """Why the crawl couldn't sign in, as a code the form shows in words."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.lower()}"


@dataclass(frozen=True)
class SiteSession:
    """What a signed-in browser carries: its cookies and, for sites that keep
    the sign-in in the page's storage, that too. Bound to one origin."""

    origin: str
    cookies: tuple[tuple[str, str, float | None], ...] = field(repr=False)  # (name, value, expires)
    local_storage: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def playwright_cookies(self) -> list[dict]:
        # By url, not domain: a cookie set by url is host-only, so a session
        # the site scoped to .example.com still reaches only the host crawled.
        cookies = []
        for name, value, expires in self.cookies:
            cookie = {"name": name, "value": value, "url": self.origin}
            if expires and expires > 0:
                cookie["expires"] = expires
            cookies.append(cookie)
        return cookies


# ------------------------------------------------------------------ links

# Ends the session wherever it appears in a URL.
_SIGN_OUT = re.compile(r"log-?_?out|sign-?_?out|log-?_?off|sign-?_?off", re.IGNORECASE)
# Changes something when it opens a path segment or a query value:
# /bookings/12/cancel, /account/delete-account, ?action=remove. Not when it's
# a word in a slug ("/blog/how-to-remove-stains"), which is content.
_ACTIONS = ("delete", "remove", "destroy", "cancel", "unsubscribe", "deactivate", "revoke", "disconnect",
            "terminate", "close-account", "close_account")


def unsafe_link(url: str) -> bool:
    parts = urlsplit(url)
    if _SIGN_OUT.search(parts.path) or _SIGN_OUT.search(parts.query):
        return True
    pieces = [segment.lower() for segment in parts.path.split("/") if segment]
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        pieces += [name.lower(), value.lower()]
    # The word itself, or followed by - or _: "cancel-subscription" changes
    # something, "cancellation-policy" is a page about it.
    return any(piece == action or piece.startswith((action + "-", action + "_"))
               for piece in pieces for action in _ACTIONS)


# ------------------------------------------------------------------ a pasted session

def parse_session(raw: str | None, start_url: str) -> SiteSession | None:
    """The Cookie header copied from the browser's developer tools ("a=1; b=2",
    with or without "Cookie:"), or a cookie extension's JSON export."""
    text = (raw or "").strip()
    if not text:
        return None
    pairs: list[tuple[str, str, float | None]] = []
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise SignInError("site_session_unreadable") from exc
        items = data if isinstance(data, list) else data.get("cookies", []) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("name") and "value" in item:
                pairs.append((str(item["name"]), str(item["value"]), None))
    else:
        if text.lower().startswith("cookie:"):
            text = text[len("cookie:"):]
        for piece in text.replace("\n", ";").split(";"):
            name, sep, value = piece.strip().partition("=")
            if sep and name.strip() and " " not in name.strip():
                pairs.append((name.strip(), value.strip(), None))
    if not pairs:
        raise SignInError("site_session_unreadable")
    return SiteSession(origin_of(start_url), tuple(pairs))


# ------------------------------------------------------------------ the browser

async def apply(context, session: SiteSession) -> None:
    """Puts the session into a browser context before its first page loads."""
    if session.cookies:
        await context.add_cookies(session.playwright_cookies())
    if session.local_storage:
        items = json.dumps(list(session.local_storage))
        origin = json.dumps(session.origin)
        await context.add_init_script(
            f"(() => {{ if (location.origin !== {origin}) return;"
            f" for (const [k, v] of {items}) {{ try {{ localStorage.setItem(k, v); }} catch (e) {{}} }} }})();"
        )


def browser_hook(session: SiteSession):
    """crawl4ai's on_page_context_created hook: the session in every context
    the crawler opens, once each."""

    async def on_page_context_created(page, context=None, **kwargs):
        context = context or page.context
        if not getattr(context, "_wc_signed_in", False):
            await apply(context, session)
            context._wc_signed_in = True
        return page

    return on_page_context_created


async def _launch(playwright):
    try:
        return await playwright.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
    except Exception as exc:
        raise SignInError("site_login_unavailable") from exc


async def _context(browser, start_url: str, basic: SiteAuth | None):
    options = {"user_agent": _BROWSER_UA, "viewport": {"width": 1280, "height": 900}}
    if basic is not None:
        # The pop-up password too, for a staging site with both — and, like
        # the crawl, for that origin only.
        options["http_credentials"] = {"username": basic.username, "password": basic.password,
                                       "origin": origin_of(start_url)}
    return await browser.new_context(**options)


async def _open(page, url: str) -> None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=OPEN_TIMEOUT_MS)
    except Exception as exc:
        raise SignInError("site_login_unreachable") from exc
    try:
        await page.wait_for_load_state("load", timeout=8_000)
    except Exception:
        pass


async def _settle(page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    await page.wait_for_timeout(500)


async def _first_visible(scope, selectors: tuple[str, ...]):
    for selector in selectors:
        found = scope.locator(selector)
        for i in range(min(await found.count(), 10)):
            candidate = found.nth(i)
            if await candidate.is_visible():
                return candidate
    return None


_PASSWORD = ("input[type=password]",)
_USER_FIELDS = (
    "input[type=email]", "input[autocomplete=username]", "input[autocomplete=email]",
    "input[name*=email i]", "input[id*=email i]", "input[name*=user i]", "input[id*=user i]",
    "input[name*=login i]", "input[id*=login i]", "input[type=text]",
)
_SUBMITS = ("button[type=submit]", "input[type=submit]", "button:not([type])")
_SIGN_IN_PATH = re.compile(r"(^|/)(log-?in|sign-?in|sign_in|signin|auth|sso|session/new|users/sign_in)(/|$)",
                           re.IGNORECASE)


async def _signed_out(page) -> bool:
    """Back at a sign-in page: one by its address, or a password box on show."""
    if _SIGN_IN_PATH.search(urlsplit(page.url).path):
        return True
    return await _first_visible(page, _PASSWORD) is not None


async def sign_in(start_url: str, login_url: str | None, email: str, password: str,
                  basic: SiteAuth | None = None) -> SiteSession:
    """Signs in on the site's own form and returns the session it hands back."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await _launch(playwright)
        try:
            context = await _context(browser, start_url, basic)
            page = await context.new_page()
            await _open(page, login_url or start_url)

            password_field = await _first_visible(page, _PASSWORD)
            if password_field is None:
                # Email first, password on the next screen.
                user_field = await _first_visible(page, _USER_FIELDS)
                if user_field is None:
                    raise SignInError("site_login_form_not_found")
                await user_field.fill(email)
                await user_field.press("Enter")
                await _settle(page)
                password_field = await _first_visible(page, _PASSWORD)
                if password_field is None:
                    raise SignInError("site_login_form_not_found")

            form = password_field.locator("xpath=ancestor::form[1]")
            scope = form if await form.count() else page
            user_field = await _first_visible(scope, _USER_FIELDS)
            if user_field is not None and not (await user_field.input_value()).strip():
                await user_field.fill(email)
            await password_field.fill(password)
            submit = await _first_visible(scope, _SUBMITS)
            if submit is not None:
                await submit.click(no_wait_after=True)
            else:
                await password_field.press("Enter")
            await _settle(page)
            if await _first_visible(page, _PASSWORD) is not None:
                raise SignInError("site_login_rejected")

            # Where the crawl starts has to let us in, not just the form.
            await _open(page, start_url)
            if await _signed_out(page):
                raise SignInError("site_login_rejected")
            state = await context.storage_state()
        finally:
            await browser.close()

    host = (urlsplit(start_url).hostname or "").lower()
    origin = origin_of(start_url)
    cookies = tuple(
        (c["name"], c["value"], c.get("expires"))
        for c in state.get("cookies", [])
        if host == c.get("domain", "").lstrip(".").lower() or host.endswith("." + c.get("domain", "").lstrip(".").lower())
    )
    storage = tuple(
        (item["name"], item["value"])
        for entry in state.get("origins", []) if entry.get("origin") == origin
        for item in entry.get("localStorage", [])
    )
    if not cookies and not storage:
        raise SignInError("site_login_rejected")
    return SiteSession(origin, cookies, storage)


async def check_session(start_url: str, session: SiteSession, basic: SiteAuth | None = None) -> None:
    """A pasted session has to get the crawler past the sign-in page."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await _launch(playwright)
        try:
            context = await _context(browser, start_url, basic)
            await apply(context, session)
            page = await context.new_page()
            await _open(page, start_url)
            if await _signed_out(page):
                raise SignInError("site_session_rejected")
        finally:
            await browser.close()


async def from_form(start_url: str, login_url: str | None, email: str | None, password: str | None,
                    pasted: str | None, basic: SiteAuth | None = None) -> SiteSession | None:
    """The session for a crawl from what the form was given, or None when it
    wasn't asked to sign in. A pasted session wins over email and password."""
    session = parse_session(pasted, start_url)
    if session is not None:
        await check_session(start_url, session, basic)
        return session
    email = (email or "").strip()
    if not email and not password:
        return None
    if not email or not password:
        raise SignInError("site_login_incomplete")
    if login_url:
        parts = urlsplit(login_url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise SignInError("site_login_url_invalid")
    return await sign_in(start_url, (login_url or "").strip() or None, email, password, basic)
