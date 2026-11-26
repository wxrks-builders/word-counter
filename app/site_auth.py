"""Crawling a site behind HTTP Basic Auth — the browser pop-up that agencies put
in front of a staging site, one shared password for the whole project.

The password lives on the in-memory Job for the length of the crawl and is
never written anywhere: not the database, the logs, the report, the email or
the share link. The run records only that one was used, so a re-crawl knows
to ask for it again.

It is attached to requests for the site's own host and nothing else. The
obvious route — an Authorization header on crawl4ai's BrowserConfig — would
send it with every request the page makes, to its font host, its CDN and its
analytics included.

A sign-in page with an email and password is a different thing — a person's
account, and a crawler signed in as them follows every link — handled, with
the guards that needs, by app/site_login.py.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

# Same posture as the crawl itself, which fetches through Chromium.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
PROBE_TIMEOUT_SECONDS = 6.0

PASSWORD_GONE = (
    "The server restarted during this crawl, and the site's password is never "
    "kept — re-crawl and enter it again to continue."
)


def _host_port(url: str) -> tuple[str, int | None] | None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.hostname.lower(), parts.port


@dataclass(frozen=True)
class SiteAuth:
    username: str
    password: str = field(repr=False)
    host: str
    port: int | None
    scheme: str

    @classmethod
    def for_site(cls, url: str, username: str, password: str) -> "SiteAuth":
        parts = urlsplit(url)
        return cls(
            username=username,
            password=password,
            host=(parts.hostname or "").lower(),
            port=parts.port,
            scheme=parts.scheme,
        )

    def header(self) -> str:
        token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"

    def applies_to(self, url: str) -> bool:
        """Only the exact host the password was given for — not its other
        subdomains, which are different systems — and never downgraded from
        https to plain http."""
        if _host_port(url) != (self.host, self.port):
            return False
        scheme = urlsplit(url).scheme
        return scheme == self.scheme or scheme == "https"


def from_form(url: str, username: str | None, password: str | None) -> SiteAuth | None:
    username = (username or "").strip()
    if not username and not password:
        return None
    return SiteAuth.for_site(url, username, password or "")


async def probe(url: str, auth: SiteAuth | None = None) -> str:
    """One request, before the crawl starts: "needs_auth" when the site asks
    for a password and none was given, "rejected" when the one given didn't
    work, and "ok" otherwise.

    Anything that isn't a Basic challenge — an anti-bot 403, a timeout — is
    "ok": the crawl has always handled those itself, and this check must never
    be the reason a crawl that used to start no longer does.
    """
    headers = {"User-Agent": _BROWSER_UA, "Accept": "text/html,application/xhtml+xml"}
    if auth is not None:
        headers["Authorization"] = auth.header()
    try:
        # httpx drops Authorization on a redirect to another host, which is the
        # same line applies_to() draws for the browser.
        async with httpx.AsyncClient(follow_redirects=True, timeout=PROBE_TIMEOUT_SECONDS) as client:
            async with client.stream("GET", url, headers=headers) as response:
                challenged = response.status_code == 401 and "basic" in response.headers.get(
                    "www-authenticate", ""
                ).lower()
    except (httpx.HTTPError, ValueError):
        return "ok"
    if not challenged:
        return "ok"
    return "rejected" if auth is not None else "needs_auth"


def browser_hook(auth: SiteAuth):
    """crawl4ai's on_page_context_created hook: route this site's requests
    through a handler that adds the header, and let everything else pass
    untouched."""

    async def add_header(route):
        headers = {**route.request.headers, "authorization": auth.header()}
        # fallback, not continue_: crawl4ai's own routes (the CSS and ad
        # blocking) still get their turn at the request.
        await route.fallback(headers=headers)

    async def on_page_context_created(page, **kwargs):
        if not getattr(page, "_site_auth_routed", False):
            # A plain function, not the bound method: Playwright tags its
            # matcher with an attribute, which a frozen dataclass refuses.
            await page.route(lambda url: auth.applies_to(url), add_header)
            page._site_auth_routed = True
        return page

    return on_page_context_created
