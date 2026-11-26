from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, SecretStr


class User(BaseModel):
    id: int
    google_sub: str
    email: str
    name: str
    picture: str | None = None
    # Billing. Written only by the Stripe webhook (app/billing.py) — never by a
    # checkout redirect, which the browser can be sent to without ever paying.
    plan: str = "free"
    plan_status: str | None = None
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None
    plan_renews_at: str | None = None
    # The language this account last browsed in — what their emails are written
    # in, since a background task has no request to read a prefix from.
    lang: str = "en"

    @property
    def is_guest(self) -> bool:
        """Someone who started a crawl from the front page without signing in.
        A real users row, so every "who owns this run" check works unchanged,
        but with no Google account and no email behind it — see app/auth.py."""
        return self.google_sub.startswith("guest:")

    @property
    def is_pro(self) -> bool:
        """Both halves matter: a subscription that lapsed keeps plan="pro" until
        Stripe says otherwise, so status is what decides. "trialing" counts —
        a trial that isn't the paid experience isn't a trial."""
        return self.plan == "pro" and self.plan_status in ("active", "trialing")


class PageResult(BaseModel):
    url: str
    title: str = ""
    word_count: int = 0
    success: bool = True
    login_required: bool = False
    blocked_by_host: bool = False
    # The already-counted page this one turned out to be a copy of — set by
    # crawler.py's post-fetch duplicate guard. Kept out of the page list and
    # the word total entirely, same as a login wall.
    duplicate_of: str | None = None
    error: str | None = None
    # The CMS recognised while crawling, if any — decides which connector the
    # offer points at. See app/promos.py.
    detected_cms: str | None = None
    # Words in this page that repeat a segment already seen in the crawl — see
    # app/repetition.py. None on pages from before it existed.
    repeated_words: int | None = None
    # A fingerprint of the counted text (the one the duplicate guard compares),
    # so a project can tell the same page apart in two crawls even under two
    # addresses — see app/projects.py. None on pages from before it existed.
    content_hash: str | None = None
    # The same idea for the page's Markdown (main content only, as
    # markdown_store saves it), taken on every page. Runs from before it
    # existed get it rebuilt from their saved Markdown, so they can still be
    # compared by text.
    markdown_hash: str | None = None


class RunRecord(BaseModel):
    id: str
    # Where the crawl started. display_url, when the owner has set one, is what
    # the report shows instead — see shown_url.
    source_url: str
    display_url: str | None = None
    user_id: int
    created_at: str
    status: str
    total_words: int
    page_count: int
    limit_reached: bool
    login_blocked_count: int = 0
    # Pages fetched but found to be a copy of one already counted — see
    # crawler.py's post-fetch duplicate guard.
    duplicate_count: int = 0
    domain_scope: str = "all"
    # Comma-separated subdomains/folders left out of this crawl, as typed.
    exclusions: str | None = None
    # Pages fetched at once — see app/plans.py. 0 on runs from before this was
    # recorded, which restore_job treats as "resolve it fresh".
    crawl_concurrency: int = 0
    # Wall-clock seconds the crawl took. 0 means unknown — either still running,
    # or saved before this was recorded.
    duration_seconds: int = 0
    # Why the run ended — see app/health.py. None on runs saved before this was
    # recorded, which reads as "unknown" rather than being guessed at.
    stop_kind: str | None = None
    stopped_reason: str | None = None
    error: str | None = None
    # The CMS recognised while crawling, if any — decides which connector the
    # offer points at, and what it's allowed to claim. See app/promos.py.
    detected_cms: str | None = None
    language: str | None = None
    language_auto_detected: bool = False
    resume_state: dict | None = None
    is_public: bool = False
    # A copy of the demo run seeded into a new account, so the first sign-in
    # isn't an empty page. Owned and deletable like any other run.
    is_sample: bool = False
    # Saved page Markdown. Files live on disk keyed by run id, never in
    # pages_json — see app/markdown_store.py.
    capture_markdown: bool = False
    markdown_pages: int = 0
    markdown_bytes: int = 0
    markdown_state: str = "off"
    # Which front door this run came in through — see app/surfaces.py.
    surface: str = "counter"
    # The site sat behind a Basic Auth password. Only the fact is kept — the
    # password never is (app/site_auth.py) — so a re-crawl knows to ask again.
    password_protected: bool = False
    # Crawled signed in on the site's own sign-in page. The fact and the sign-in
    # page are kept; the email, password and session never are (app/site_login.py).
    signed_in: bool = False
    login_url: str | None = None
    # Pages linking to each subdomain and language folder of the site, counted
    # or not — see app/report.py's LinkCensus. Empty on older runs.
    linked_hosts: dict[str, int] = {}
    linked_languages: dict[str, int] = {}
    # Pages read and set aside as another language, and URLs in folders that
    # turned out to be one — see app/language_check.py.
    other_languages: dict[str, dict] = {}
    # The repetition estimate's summary (app/repetition.py); empty on older runs.
    repetition: dict = {}
    pages: list[PageResult]

    @property
    def shown_url(self) -> str:
        return self.display_url or self.source_url


class CrawlRequest(BaseModel):
    url: str
    domain_scope: Literal["all", "subdomain_only", "top_domain_only"] = "all"
    # Comma-separated subdomains and folders to leave out, e.g.
    # "staging, web-staging, /careers" — see app/url_policy.py.
    exclusions: str | None = None
    language: str | None = None
    force_recrawl: bool = False
    capture_markdown: bool = False
    # For a site behind a browser password pop-up (HTTP Basic Auth). SecretStr
    # so the password can't turn up in a repr, a log line or an error message.
    site_username: str | None = None
    site_password: SecretStr | None = None
    # For a site with a sign-in page: the page (optional — the start URL's own
    # form otherwise), an email and password the crawler signs in with, or a
    # session pasted from the browser instead. See app/site_login.py.
    login_url: str | None = None
    login_email: str | None = None
    login_password: SecretStr | None = None
    login_session: SecretStr | None = None
    # Projects to put the new run in, and/or a new one to create for it —
    # optional, under Advanced. See app/projects.py.
    project_ids: list[str] | None = None
    project_name: str | None = None


class ResumeRequest(BaseModel):
    # Offered again at the pause, because the estimate panel is the first
    # moment anyone can see they're about to crawl 2,000 pages of a staging
    # mirror. Omitted means "keep whatever the crawl already had".
    exclusions: str | None = None


class ShareEmailRequest(BaseModel):
    email: str


class ProjectRequest(BaseModel):
    name: str
    # Runs to put in it straight away.
    run_ids: list[str] = []


class ProjectRunsRequest(BaseModel):
    run_ids: list[str]


class McpKeyRequest(BaseModel):
    # What the key is for, so the user can tell keys apart ("Claude Code, laptop").
    name: str = ""


class RunProjectsRequest(BaseModel):
    """The projects a run is in, all of them — the dialog sends its ticks —
    plus an optional new one to create and add it to."""
    project_ids: list[str] = []
    new_name: str | None = None


class DisplayUrlRequest(BaseModel):
    # Empty puts the report back to the address the crawl started from.
    url: str | None = None


class ShareToggleRequest(BaseModel):
    # Omitted means "flip whatever the current state is" — the switch in the
    # share dialog always sends an explicit state so it can't drift out of sync.
    is_public: bool | None = None
