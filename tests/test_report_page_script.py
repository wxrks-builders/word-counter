"""The report page's script, run in a real browser.

The report is drawn by app.js, and one unguarded handler for an element a given
viewer doesn't have stops everything after it: shared links rendered as an empty
"Starting…" page for weeks because the re-crawl form, hidden from anyone signed
out, was bound before the finished-report branch. The server-side tests can't
see that — the HTML is fine — so this loads the page in Chromium, for each kind
of viewer, and fails on any script error.

Requests are answered by the app's TestClient through Playwright routing, so no
server or port is involved.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_app_flows import app_env, run, save  # noqa: E402,F401

ORIGIN = "http://report.test"


def _render(env, path: str) -> tuple[list[str], dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")

    errors: list[str] = []

    def answer(route):
        url = route.request.url
        if not url.startswith(ORIGIN):
            return route.abort()
        res = env.client.get(url[len(ORIGIN):], headers={"host": "report.test"})
        route.fulfill(status=res.status_code, headers=dict(res.headers), body=res.content)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.route("**/*", answer)
            page.goto(ORIGIN + path)
            page.wait_for_load_state("networkidle")
            shown = {
                "status": page.inner_text("#status-text"),
                "words": page.inner_text("#total-words"),
                "scope": page.inner_text("#domain-scope-pill"),
            }
            browser.close()
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "BrowserType.launch" in str(exc):
            pytest.skip(f"browser unavailable: {exc}")
        raise
    return errors, shown


def test_a_shared_link_renders_for_someone_signed_out(app_env):
    save(app_env, run_id="r")
    run(app_env.db.set_run_public("r", True))
    app_env.current["user"] = None

    errors, shown = _render(app_env, "/share/r")
    assert errors == []
    assert shown["words"] == "840" and shown["status"] == "Completed" and shown["scope"]


def test_the_owners_report_renders(app_env):
    save(app_env, run_id="r")

    errors, shown = _render(app_env, "/crawl/r")
    assert errors == []
    assert shown["words"] == "840" and shown["status"] == "Completed" and shown["scope"]


# ------------------------------------------------------------ guests

_ESTIMATE = {
    "pages_fetched": 38, "total_pages_estimate": 420, "avg_words_per_page": 1310,
    "estimated_total_words": 550_000, "confidence": "high", "detected_cms": "Webflow",
    "estimated_duration_seconds": 900, "estimated_duration_seconds_pro": 320,
    "pages_per_minute": 30, "words_per_minute": 39_000, "concurrent_crawls": 1,
    "sitemap_found": True, "concurrency": 4,
}


def _guest_job(env, status: str):
    """A crawl a signed-out visitor started, as the front page makes one."""
    from app.auth import guest_for  # noqa: F401  (the route below creates the guest)

    async def fake_run_crawl(*args, **kwargs):
        pass

    async def open_site(url, auth=None):
        return "ok"

    env.main.run_crawl, env.main.site_auth.probe = fake_run_crawl, open_site
    env.current["user"] = None
    res = env.client.post("https://testserver/start", data={"url": "wxrks.com"}, follow_redirects=False)
    job = env.main.get_job(res.headers["location"].rsplit("/", 1)[-1])
    job.status = status
    if status == "paused":
        job.estimate_result = _ESTIMATE
    return job


def _render_as_guest(env, path: str):
    # The guest is identified by the session cookie the start request set.
    cookies = [{"name": c.name, "value": c.value, "url": ORIGIN} for c in env.client.cookies.jar]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    errors: list[str] = []

    def answer(route):
        url = route.request.url
        if not url.startswith(ORIGIN) or "/events/" in url:
            return route.abort()
        res = env.client.get("https://testserver" + url[len(ORIGIN):], headers={"host": "report.test"})
        route.fulfill(status=res.status_code, headers=dict(res.headers), body=res.content)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()
        context.add_cookies(cookies)
        page = context.new_page()
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.route("**/*", answer)
        page.goto(ORIGIN + path)
        page.wait_for_timeout(500)
        gate_visible = page.locator(".guest-gate").is_visible() if page.locator(".guest-gate").count() else False
        proceed_visible = page.locator("#proceed-btn").is_visible()
        browser.close()
    return errors, gate_visible, proceed_visible


def test_a_guest_can_watch_their_crawl(app_env, monkeypatch):
    monkeypatch.setattr(app_env.main, "run_crawl", app_env.main.run_crawl)  # restored after the test
    monkeypatch.setattr(app_env.main.site_auth, "probe", app_env.main.site_auth.probe)
    job = _guest_job(app_env, "crawling")
    errors, _, _ = _render_as_guest(app_env, f"/crawl/{job.id}")
    assert errors == []


def test_a_guest_at_the_estimate_is_asked_to_sign_in_not_to_proceed(app_env, monkeypatch):
    monkeypatch.setattr(app_env.main, "run_crawl", app_env.main.run_crawl)
    monkeypatch.setattr(app_env.main.site_auth, "probe", app_env.main.site_auth.probe)
    job = _guest_job(app_env, "paused")
    errors, gate_visible, proceed_visible = _render_as_guest(app_env, f"/crawl/{job.id}")
    assert errors == []
    assert gate_visible and not proceed_visible


def test_a_report_says_what_its_settings_left_out(app_env):
    """A subdomain-only run still gets a subdomain section: it says why the
    others weren't crawled and names the ones the pages link to."""
    save(app_env, run_id="scoped", domain_scope="subdomain_only",
         linked_hosts={"x.com": 8, "blog.x.com": 5}, linked_languages={"de": 3})
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    errors: list[str] = []

    def answer(route):
        url = route.request.url
        if not url.startswith(ORIGIN):
            return route.abort()
        res = app_env.client.get(url[len(ORIGIN):], headers={"host": "report.test"})
        route.fulfill(status=res.status_code, headers=dict(res.headers), body=res.content)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.route("**/*", answer)
        page.goto(ORIGIN + "/crawl/scoped")
        page.wait_for_load_state("networkidle")
        note = page.inner_text("#host-note")
        hosts = page.inner_text("#host-tbody")
        host_left_out = page.inner_text("#host-left-out-list")
        language_visible = page.locator("#languages").is_visible()
        language_left_out = page.inner_text("#language-left-out-list")
        browser.close()

    assert errors == []
    assert "This subdomain only" in note
    assert "x.com" in hosts
    assert "blog.x.com" in host_left_out and "linked from 5 pages" in host_left_out
    assert language_visible and "/de/" in language_left_out


def test_the_report_is_one_page_with_section_links(app_env):
    """Every section stays on the page; the links jump to the ones this run
    has, the bar sticks to the top, and "Not counted" adds up what's left out."""
    save(app_env, run_id="nav", total_words=840, login_blocked_count=2, duplicate_count=5,
         repetition={"words_total": 840, "words_repeated": 210, "words_near": 0, "segments_total": 90,
                     "segments_unique": 60, "top": [], "partial": False, "capped": False})
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    errors: list[str] = []

    def answer(route):
        url = route.request.url
        if not url.startswith(ORIGIN):
            return route.abort()
        res = app_env.client.get(url[len(ORIGIN):], headers={"host": "report.test"})
        route.fulfill(status=res.status_code, headers=dict(res.headers), body=res.content)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 700})
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.route("**/*", answer)
        page.goto(ORIGIN + "/crawl/nav")
        page.wait_for_load_state("networkidle")
        links = page.eval_on_selector_all(
            "#report-nav .report-nav-links a", "els => els.filter(a => !a.hidden).map(a => a.hash)")
        not_counted = page.inner_text("#not-counted")
        not_counted_sub = page.inner_text("#not-counted-sub")
        left_out_visible = page.locator("#left-out").is_visible()
        hero_repeated = page.inner_text("#hero-repeated-pct")
        page.click("#report-nav a[href='#pages']")
        page.wait_for_timeout(900)
        nav_top = page.eval_on_selector("#report-nav", "el => el.getBoundingClientRect().top")
        stuck = page.eval_on_selector("#report-nav", "el => el.classList.contains('is-stuck')")
        current = page.eval_on_selector("#report-nav a[aria-current='location']", "el => el.hash")
        browser.close()

    assert errors == []
    assert links == ["#overview", "#repetition", "#folders", "#subdomains", "#pages", "#left-out"], links
    assert not_counted == "7" and "5 duplicates" in not_counted_sub and "2 behind a login" in not_counted_sub
    assert left_out_visible
    assert hero_repeated == "25%"
    assert nav_top == 0 and stuck, "the links stay at the top of the window"
    assert current in ("#pages", "#left-out")


def test_a_running_crawl_puts_its_status_and_cancel_in_the_summary_band(app_env):
    """While it runs: the status and Cancel share a strip at the top of the
    band, Share waits for the end, and the total says "so far". Once it has
    stopped, the strip goes and the status sits beside the total."""
    from app.job_store import JOBS, create_job

    job = create_job(source_url="https://x.com", user_id=1, max_pages=float("inf"))
    job.status, job.total_words = "crawling", 50_604
    save(app_env, run_id="done")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    errors: list[str] = []

    def answer(route):
        url = route.request.url
        if not url.startswith(ORIGIN) or "/events/" in url:
            return route.abort()
        res = app_env.client.get(url[len(ORIGIN):], headers={"host": "report.test"})
        route.fulfill(status=res.status_code, headers=dict(res.headers), body=res.content)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.route("**/*", answer)
            page.goto(ORIGIN + f"/crawl/{job.id}")
            page.wait_for_timeout(400)
            live = page.evaluate("""() => ({
                strip: !document.getElementById('live-strip').hidden,
                badgeInStrip: !!document.querySelector('#live-strip #status-badge'),
                cancelInStrip: !!document.querySelector('#live-strip #cancel-btn'),
                cancelShown: document.getElementById('cancel-btn').offsetParent !== null,
                shareShown: document.getElementById('share-btn').offsetParent !== null,
                sub: document.getElementById('hero-sub').textContent,
                note: document.getElementById('live-strip-note').textContent,
                csvButtons: [...document.querySelectorAll('#pages button')].filter(b => b.textContent.includes('CSV')).length,
            })""")
            page.goto(ORIGIN + "/crawl/done")
            page.wait_for_load_state("networkidle")
            done = page.evaluate("""() => ({
                strip: !document.getElementById('live-strip').hidden,
                badgeByTotal: !!document.querySelector('.hero-label #status-badge'),
                shareShown: document.getElementById('share-btn').offsetParent !== null,
            })""")
            browser.close()
    finally:
        JOBS.pop(job.id, None)

    assert errors == []
    assert live["strip"] and live["badgeInStrip"] and live["cancelInStrip"] and live["cancelShown"]
    assert not live["shareShown"], "nothing to share until it finishes"
    assert "so far" in live["sub"] and live["note"]
    assert live["csvButtons"] == 0, "the CSV is in Export, not repeated in Pages"
    assert not done["strip"] and done["badgeByTotal"] and done["shareShown"]


def test_the_printed_report_leaves_out_the_page_list_and_the_controls(app_env):
    """On paper: the summary, the sections and a closing line on where the full
    page list is — not two thousand rows, and nothing to click."""
    save(app_env, run_id="paper")
    run(app_env.db.set_run_public("paper", True))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")

    def answer(route):
        url = route.request.url
        if not url.startswith(ORIGIN):
            return route.abort()
        res = app_env.client.get(url[len(ORIGIN):], headers={"host": "report.test"})
        route.fulfill(status=res.status_code, headers=dict(res.headers), body=res.content)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route("**/*", answer)
        page.goto(ORIGIN + "/crawl/paper")
        page.wait_for_load_state("networkidle")
        page.emulate_media(media="print")
        shown = {sel: page.locator(sel).first.is_visible() for sel in (
            "#overview", "#folders", "#pages", "#report-nav", ".header-actions", ".print-footer")}
        footer = page.inner_text(".print-footer")
        font = page.evaluate("getComputedStyle(document.body).fontFamily")
        browser.close()

    assert shown["#overview"] and shown["#folders"] and shown[".print-footer"]
    assert not shown["#pages"] and not shown["#report-nav"] and not shown[".header-actions"]
    assert "The full list of 8 pages" in footer and "/share/paper" in footer
    assert "Urbanist" in font, "the report's own typeface, not a serif fallback"
