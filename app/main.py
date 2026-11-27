from __future__ import annotations

import asyncio
import csv
import gzip
import io
import json
from html import escape
import logging
import math
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote_plus, urlencode, urlsplit

from dotenv import load_dotenv

load_dotenv()

from babel.dates import format_datetime
from crawl4ai.utils import get_base_domain
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import (articles, auth, billing, consolidation_job, db, i18n, markdown_store, mcp_keys, mcp_server, projects, repetition,
                 report, segment_store, site_auth, site_login, surfaces, translation_check)
from app.auth import (get_current_user, get_guest, get_viewer, guest_for, is_admin, require_admin,
                      require_translation_viewer, require_user, require_user_api)
from app.crawler import (KNOWN_CMS, MAX_CONCURRENT_CRAWLS, PAUSE_AT_WORDS, backfill_detected_cms,
                         estimate_result_from_snapshot, run_crawl)
from app.job_store import create_job, enqueue, get_job, list_active_jobs, list_queued_jobs, restore_job
from app.models import (CrawlRequest, DisplayUrlRequest, McpKeyRequest, ProjectRequest, ProjectRunsRequest,
                        ResumeRequest, RunProjectsRequest, ShareEmailRequest, ShareToggleRequest, User)
from app.notifications import absolute_url, remember_origin, send_server_alert, send_share_notification
from app.plans import active_page_load, resolve_concurrency
from app.health import pending_alerts, snapshot as health_snapshot
from app.i18n import N_, gettext_for
from app.promos import home_pitch, pro_upsell, rank_page_promos, wxrks_pitch
from app.templates import ASSET_VERSION, templates
from app.url_policy import parse_exclusions

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = ("completed", "failed", "cancelled", "paused")
# Terminal *and* not resumable — these have nothing left to stream, so the
# crawl page reads them back from the DB instead of using the live view.
_FINISHED_STATUSES = ("completed", "failed", "cancelled")


# Disk can fill with nothing running, so this can't hang off crawl completion.
ALERT_CHECK_SECONDS = int(os.environ.get("ALERT_CHECK_SECONDS", "600"))

# Crawling without signing in. Every guest crawl stops at the PAUSE_AT_WORDS
# estimate and needs a sign-in to go further, so these only bound the cheap
# part — but anonymous crawling is still somebody else's server doing work for
# anyone, and signed-in users must never queue behind it.
GUEST_CRAWLS_PER_DAY = int(os.environ.get("GUEST_CRAWLS_PER_DAY", "5"))
GUEST_MAX_ACTIVE = int(os.environ.get("GUEST_MAX_ACTIVE", "2"))
# Unclaimed guest crawls are deleted after this long.
GUEST_RETENTION_DAYS = int(os.environ.get("GUEST_RETENTION_DAYS", "7"))
_guest_starts: dict[str, list[float]] = {}


async def _alert_loop() -> None:
    """Checks the server's own health on a timer and mails ADMIN_EMAILS.

    Every failure is swallowed: this task must outlive whatever it finds, or a
    transient error permanently disables the thing whose job is noticing errors.
    """
    while True:
        await asyncio.sleep(ALERT_CHECK_SECONDS)
        try:
            await _sweep_guest_runs()
        except Exception:
            logger.exception("The guest-run sweep failed; will try again next cycle")
        try:
            for alert in await pending_alerts():
                await send_server_alert(
                    heading=alert["heading"],
                    intro_text=alert["intro_text"],
                    label=alert["label"],
                    value=alert["value"],
                )
                await db.mark_alert_sent(alert["kind"], datetime.now(timezone.utc).isoformat())
                logger.warning("Server alert: %s — %s", alert["heading"], alert["value"])
        except Exception:
            logger.exception("The health check failed; will try again next cycle")
        try:
            # Picks up a check a deploy interrupted, and starts the weekly one.
            await translation_check.maybe_start_scheduled()
            await translation_check.claim_stale_check()
        except Exception:
            logger.exception("The translation check schedule failed; will try again next cycle")
        # Takes over a consolidation a deploy interrupted, once its lease is out.
        consolidation_job.kick()


async def _sweep_guest_runs() -> None:
    """Guest crawls nobody signed in to keep, and the guests left with none."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=GUEST_RETENTION_DAYS)).isoformat()
    for run_id in await db.stale_guest_run_ids(cutoff):
        if get_job(run_id) is not None and get_job(run_id).status in ("starting", "crawling"):
            continue
        await db.delete_run(run_id)
        markdown_store.delete(run_id)
    await db.delete_idle_guests(cutoff)
    day_ago = time.time() - 86_400
    for ip in [ip for ip, starts in _guest_starts.items() if not any(t > day_ago for t in starts)]:
        del _guest_starts[ip]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    # Any run still marked "crawling" here was interrupted by a crash/restart
    # (JOBS is always empty on a fresh process) — pick each one back up from
    # its last checkpoint rather than leaving it stuck forever.
    for run in await db.get_crawling_runs():
        if not await db.claim_crawling_run(run.id):
            # Another process/instance already claimed this one — skip it.
            continue
        if run.password_protected:
            # Its password went with the old process. Carrying on without it
            # would fill the report with login walls, so say what happened.
            await db.fail_run(run.id, site_auth.PASSWORD_GONE)
            continue
        if run.signed_in:
            # And so did its sign-in, for the same reason.
            await db.fail_run(run.id, site_login.SESSION_GONE)
            continue
        job = restore_job(run)
        language = job.language_setting or job.detected_language
        job.task = asyncio.create_task(
            run_crawl(job.id, job.source_url, job.max_pages, job.domain_scope, language, resume_state=job.resume_state)
        )

    # A "paused" run is a legitimate stop, not a crash — but its in-memory
    # Job (and estimate_result) is gone all the same, so without this the
    # crawl page would fall back to the DB-only "past" view, which never
    # shows the estimate panel or lets "Proceed with crawl" work again. No
    # task is started here — just restoring enough state for the page and
    # the existing /resume endpoint to work exactly as they did before the
    # restart.
    for run in await db.get_paused_runs():
        snapshot = await db.get_estimate_snapshot(run.id)
        estimate_result = estimate_result_from_snapshot(snapshot) if snapshot else None
        restore_job(run, estimate_result=estimate_result)

    # Saved Markdown outlives its run if a delete didn't get to unlink it (a
    # crash mid-delete, or a database restored from a backup). Left alone it
    # would sit on the same volume as the database forever.
    try:
        for run_id in markdown_store.existing_run_ids():
            if await db.get_run_exists(run_id):
                continue
            logger.info("Removing saved Markdown for run %s, which no longer exists", run_id)
            markdown_store.delete(run_id)
    except Exception:
        logger.exception("Markdown orphan sweep failed")

    # Sentence fingerprints are kept for as long as their run exists.
    try:
        for run_id in segment_store.stored_run_ids():
            if not await db.get_run_exists(run_id):
                logger.info("Removing sentence fingerprints for run %s, which no longer exists", run_id)
                segment_store.delete(run_id)
    except Exception:
        logger.exception("Sentence fingerprint sweep failed")

    # A repetition tally is only kept while its run can still continue; one
    # left behind by a crash at the finish, or by a deleted run, goes here.
    try:
        for run_id in repetition.stored_run_ids():
            run = await db.get_run(run_id)
            if run is None or run.status not in ("crawling", "paused"):
                repetition.delete(run_id)
    except Exception:
        logger.exception("Repetition tally sweep failed")

    # A translation check left by the process this one replaces. During a
    # rolling deploy that process may still be running it — the lease only
    # lets go once its heartbeat is stale, and _alert_loop tries again.
    try:
        await translation_check.claim_stale_check()
    except Exception:
        logger.exception("Could not resume the translation check")

    # Consolidated projects' CSVs outlive a project deleted mid-way.
    try:
        for project_id in consolidation_job.stored_project_ids():
            if await db.get_project(project_id) is None:
                consolidation_job.delete_files(project_id)
    except Exception:
        logger.exception("Consolidation file sweep failed")
    try:
        await consolidation_job.resume_after_restart()
    except Exception:
        logger.exception("Could not resume project consolidations")

    alert_task = asyncio.create_task(_alert_loop())

    yield

    alert_task.cancel()
    # Before the database closes: a cancelled check hands its lease over.
    await translation_check.shutdown()
    await consolidation_job.shutdown()
    await db.close_db()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(StarletteHTTPException)
async def _not_found_page(request: Request, exc: StarletteHTTPException):
    """A person opening a missing report gets a page in their language rather
    than {"detail": "Crawl not found"} in English. Everything else — app.js,
    assistants, redirects raised as exceptions — gets FastAPI's usual answer."""
    if exc.status_code == 404 and i18n.is_page_view(request) and hasattr(request.state, "surface"):
        user = await get_current_user(request)
        return templates.TemplateResponse(request, "not_found.html", {"user": user}, status_code=404)
    return await http_exception_handler(request, exc)

# Secure by default, opt out for local http:// development.
#
# This used to read RENDER, which the host set automatically — so the moment
# the app moved off Render the flag silently went false and the session cookie
# stopped being marked Secure in production. A missing variable must fail
# closed, not open, so the default is now "on" and only an explicit
# SECURE_COOKIES=false turns it off.
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "true").strip().lower() not in ("0", "false", "no", "off")
if not SECURE_COOKIES:
    logger.warning("SECURE_COOKIES is off — the session cookie will be sent over plain http. Local development only.")

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["SESSION_SECRET"],
    https_only=SECURE_COOKIES,
)


class _VersionedStatic:
    """Cache headers from the ?v= fingerprint the templates put on assets.

    Matching this process's ASSET_VERSION, the file can never change under that
    URL: cache it for a year. Any other version is a request this process can't
    honestly answer — during a rolling deploy the new page's
    style.css?v=<new> can reach the old container, which would serve the old
    file under the new URL and have it cached there, so the page renders with
    last deploy's styles until a hard reload. That answer is sent no-store.
    Unversioned requests (images, fonts) are left as they were.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        version = parse_qs(scope.get("query_string", b"").decode()).get("v", [None])[0]
        if version is None:
            return await self.app(scope, receive, send)
        cache = "public, max-age=31536000, immutable" if version == ASSET_VERSION else "no-store"

        async def send_with_cache(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = cache
            await send(message)

        await self.app(scope, receive, send_with_cache)


# Compressed here and only here: the stylesheet, scripts and the hero's WebGL
# bundle (570 KB, 145 KB gzipped). Not app-wide, where it would also gzip the
# Markdown ZIP downloads — already compressed, and hundreds of MB.
app.mount(
    "/static",
    _VersionedStatic(GZipMiddleware(StaticFiles(directory="app/static"), minimum_size=1024)),
    name="static",
)
app.include_router(auth.router)
app.include_router(billing.router)


@app.middleware("http")
async def _record_origin(request: Request, call_next):
    """Emails are sent from background tasks that have no request to build a
    link from. Recording the origin real traffic arrives on means a missing
    PUBLIC_BASE_URL degrades to a working link instead of a broken one.

    Also resolves which product this request belongs to — both hostnames are
    served by this one app (see app/surfaces.py), and redirects the old ones
    when that's switched on. Templates read the surface through the context
    processor in app/templates.py.
    """
    # An old hostname (wxrks.app, before the move to wxrks.com) sends people to
    # the new one, same path and query. Only GET and HEAD: a POST can't follow a
    # 301 with its body, and assistants connected to the old /mcp URL, Stripe's
    # webhook and a sign-in in flight keep being served where they are.
    moved = surfaces.moved_from(request.headers.get("host"))
    if moved and request.method in ("GET", "HEAD") and not _wants_event_stream(request):
        target = moved.url(request.url.path)
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=301)

    request.state.surface = surfaces.for_host(request.headers.get("host"))
    remember_origin(str(request.base_url))

    # Language comes off the front of the path, the way wxrks.com addresses its
    # own translations. The prefix is then removed from the scope so all 31
    # routes match exactly the patterns they already declare — the alternative
    # is declaring every route twice, which is a second place to forget one.
    lang, rest = i18n.split_path(request.url.path)
    request.state.lang = lang
    if rest != request.url.path:
        request.scope["path"] = rest
        request.scope["raw_path"] = rest.encode()

    response = await call_next(request)
    # So a shared or bookmarked URL keeps its language, and so caches don't
    # serve one language's page under another's URL.
    response.headers.setdefault("Content-Language", i18n.HREFLANG.get(lang, lang))
    if i18n.is_page_view(request):
        if lang == i18n.DEFAULT_LANG:
            # The English page offers the visitor's own language (base.html),
            # so it differs by what their browser asks for.
            response.headers.append("Vary", "Accept-Language")
        elif lang in i18n.LANGUAGES and request.cookies.get(i18n.LANG_COOKIE) != lang:
            # Opening a page in a language is choosing it: remembered, so the
            # English root offers it back next time instead of guessing.
            response.set_cookie(i18n.LANG_COOKIE, lang, max_age=365 * 86_400, samesite="lax",
                                secure=SECURE_COOKIES)
    return response


def _valid_url(url: str) -> bool:
    url = url.strip()
    if not url or any(ch.isspace() for ch in url):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        parts.port  # raises on a malformed port
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and ("." in host or host == "localhost")


def _sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


HOME_RUNS = 10
RUNS_PER_PAGE = 25


def _page_count(total: int, per_page: int) -> int:
    return max(1, math.ceil(total / per_page))


@app.get("/")
async def index(request: Request, user: User | None = Depends(get_current_user)):
    """Signed in, this is the app. Signed out, it's the front door.

    It used to redirect anonymous visitors straight to /login, which meant
    anyone arriving from a link saw a Google button and one sentence about a
    product they'd never heard of.
    """
    surface = request.state.surface
    if user is None:
        examples = await _example_runs(surface)
        return templates.TemplateResponse(
            request,
            "landing.html",
            {
                "examples": examples,
                "preview": await _report_preview(examples),
                "pause_at_words": PAUSE_AT_WORDS,
                "other_surfaces": [s for s in surfaces.SURFACES if s.key != surface.key],
                "start_error": request.query_params.get("error") == "url",
                "prefill_url": request.query_params.get("url", ""),
            },
        )

    recent_runs = await db.list_user_runs(user.id, limit=HOME_RUNS)
    # The copied demo run every new account starts with doesn't count: someone
    # who has only ever seen that still hasn't crawled anything, and still
    # needs the page to explain itself.
    show_intro = not any(not run["is_sample"] for run in recent_runs)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "user": user,
            "recent_runs": recent_runs,
            "show_intro": show_intro,
            "examples": (examples := await _example_runs(surface) if show_intro else []),
            "preview": await _report_preview(examples),
            "pause_at_words": PAUSE_AT_WORDS,
            # Drawn from their most recent crawl big enough to be a translation
            # project — see app/promos.py. Nothing to say if they haven't run
            # one yet, which is the common case on a first visit.
            "wxrks_pitch": home_pitch(recent_runs, request.state.surface),
            "total_runs": await db.count_user_runs(user.id),
            **await _project_choices(user, [run["id"] for run in recent_runs]),
            "preselect_project": request.query_params.get("project", ""),
        },
    )


_PREVIEW_CACHE: dict[str, dict] = {}


async def _report_preview(examples: list[dict]) -> dict | None:
    """The top of a real report, drawn from the first example — the front page
    shows the product instead of describing it. A finished run never changes
    (a re-crawl is a new run id), so it's computed once per run."""
    if not examples:
        return None
    run_id = examples[0]["id"]
    if run_id not in _PREVIEW_CACHE:
        run = await db.get_run(run_id)
        if run is None:
            return None
        folders = report.summarize(run.pages)["folders"][:5]
        _PREVIEW_CACHE.clear()  # only ever the current example
        _PREVIEW_CACHE[run_id] = {
            "id": run.id,
            "host": (urlsplit(run.shown_url).hostname or run.shown_url).removeprefix("www."),
            "total_words": run.total_words,
            "page_count": run.page_count,
            "folders": folders,
            "max_words": max((f["words"] for f in folders), default=0) or 1,
        }
    return _PREVIEW_CACHE[run_id]


async def _example_runs(surface) -> list[dict]:
    """Finished public reports the front page links to, so a visitor can see
    what they'd get before signing in. Featured from the admin runs list; the
    surface's demo run stands in until something is."""
    examples = await db.list_example_runs()
    if examples:
        return examples
    demo_id = await _public_demo_run_id(surface)
    demo = await db.get_run(demo_id) if demo_id else None
    if demo is None:
        return []
    return [{"id": demo.id, "source_url": demo.shown_url, "total_words": demo.total_words,
             "page_count": demo.page_count}]


def _with_scheme(url: str) -> str:
    """People type "tesla.com", not "https://tesla.com"."""
    url = url.strip()
    return url if "://" in url or not url else f"https://{url}"


@app.post("/start")
async def start_from_front_page(
    request: Request,
    url: str = Form(""),
    markdown: str = Form(""),
    user: User | None = Depends(get_current_user),
):
    """The front page's form. Signed out, the crawl starts right away as a
    guest and runs to the PAUSE_AT_WORDS estimate; a site smaller than that
    finishes without anyone signing in, and a bigger one asks for Google
    sign-in to count the rest. Past a guest limit, sign-in comes first and the
    crawl starts the moment they're back."""
    lang = request.state.lang
    url = _with_scheme(url)
    if not _valid_url(url):
        return RedirectResponse(
            i18n.localized_path("/", lang) + "?" + urlencode({"error": "url", "url": url}), status_code=303
        )
    capture_markdown = markdown == "1"
    request.session["pending_markdown"] = capture_markdown
    if user is not None:
        request.session["pending_crawl"] = url
        return RedirectResponse(i18n.localized_path("/start", lang), status_code=303)

    guest = await get_guest(request)
    running = [j for j in list_active_jobs() + list_queued_jobs() if guest is not None and j.user_id == guest.id]
    if running:
        # One at a time: back to the crawl that's still going.
        return RedirectResponse(i18n.localized_path(f"/crawl/{running[0].id}", lang), status_code=303)

    # Somebody already published a report of this site: show it instead of
    # crawling it again.
    cached = await db.get_latest_run(db.normalize_url(url))
    if cached is not None and cached.is_public and not cached.password_protected and not cached.signed_in:
        return RedirectResponse(i18n.localized_path(f"/share/{cached.id}", lang), status_code=303)

    if not _guest_may_start(_client_ip(request)):
        return _sign_in_then_start(request, url)

    surface = request.state.surface
    result, status = await _begin_crawl(
        CrawlRequest(url=url, force_recrawl=True, capture_markdown=capture_markdown),
        await guest_for(request),
        surface.key,
    )
    if status != 200:
        # Most likely a password-protected site, which needs the full form's
        # Advanced fields — and an account, since its report is private.
        return _sign_in_then_start(request, url)
    _guest_starts.setdefault(_client_ip(request), []).append(time.time())
    return RedirectResponse(i18n.localized_path(f"/crawl/{result['run_id']}", lang), status_code=303)


def _sign_in_then_start(request: Request, url: str) -> RedirectResponse:
    request.session["pending_crawl"] = url
    request.session["after_login"] = i18n.localized_path("/start", request.state.lang)
    return RedirectResponse("/auth/login", status_code=303)


def _client_ip(request: Request) -> str:
    # --proxy-headers makes this the visitor, not the proxy in front of us.
    return request.client.host if request.client else "unknown"


def _guest_may_start(ip: str) -> bool:
    day_ago = time.time() - 86_400
    recent = [t for t in _guest_starts.get(ip, []) if t > day_ago]
    _guest_starts[ip] = recent
    guests_running = sum(1 for j in list_active_jobs() + list_queued_jobs() if j.guest)
    return len(recent) < GUEST_CRAWLS_PER_DAY and guests_running < GUEST_MAX_ACTIVE


@app.get("/start")
async def start_pending_crawl(request: Request, user: User | None = Depends(get_current_user)):
    lang = request.state.lang
    if user is None:
        return RedirectResponse(i18n.localized_path("/login", lang), status_code=303)
    url = request.session.pop("pending_crawl", None)
    home = i18n.localized_path("/", lang)
    if not url:
        return RedirectResponse(home, status_code=303)
    surface = request.state.surface
    capture_markdown = request.session.pop("pending_markdown", surface.default_capture_markdown)
    result, status = await _begin_crawl(CrawlRequest(url=url, capture_markdown=capture_markdown), user, surface.key)
    if status != 200:
        # Back to the full form, filled in, with the reason — a password-
        # protected site, most likely, which needs the Advanced fields.
        return RedirectResponse(
            home + "?" + urlencode({"url": url, "code": result.get("code", "")}), status_code=303
        )
    return RedirectResponse(i18n.localized_path(f"/crawl/{result['run_id']}", lang), status_code=303)


async def _public_demo_run_id(surface) -> str:
    run_id = surface.demo_run_id
    if not run_id:
        return ""
    run = await db.get_run(run_id)
    return run_id if run is not None and run.is_public else ""


@app.get("/runs")
async def all_runs(request: Request, page: int = 1, user: User = Depends(require_user)):
    page = max(1, page)
    total = await db.count_user_runs(user.id)
    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "user": user,
            "runs": (runs := await db.list_user_runs(user.id, limit=RUNS_PER_PAGE, offset=(page - 1) * RUNS_PER_PAGE)),
            **await _project_choices(user, [run["id"] for run in runs]),
            "page": page,
            "page_count": _page_count(total, RUNS_PER_PAGE),
            "total": total,
            "base_url": "/runs?",
        },
    )


@app.post("/crawl")
async def start_crawl(payload: CrawlRequest, request: Request, user: User = Depends(require_user_api)):
    result, status = await _begin_crawl(payload, user, request.state.surface.key)
    return JSONResponse(result, status_code=status)


async def _begin_crawl(payload: CrawlRequest, user: User, surface_key: str) -> tuple[dict, int]:
    """Starts, queues or reuses a crawl, and puts it in the projects the form
    asked for. Shared by the form on the report pages and the front page's,
    which only gets here after a Google sign-in."""
    wanted = None
    new_name = (payload.project_name or "").strip()
    if (payload.project_ids or new_name) and not user.is_guest:
        wanted = []
        for project_id in payload.project_ids or []:
            if await _owned_project(user, project_id) is None:
                return {"code": "project_not_found"}, 400
            wanted.append(project_id)
    result, status = await _start_or_reuse_crawl(payload, user, surface_key)
    if status == 200 and wanted is not None:
        if result.get("cached"):
            # Somebody else's report of the site can't go in your project:
            # crawl it afresh for you instead.
            cached = await db.get_run(result["run_id"])
            if cached is None or cached.user_id != user.id:
                result, status = await _start_or_reuse_crawl(
                    payload.model_copy(update={"force_recrawl": True}), user, surface_key
                )
        if status == 200:
            await _put_in_projects(user, result["run_id"], wanted, new_name)
    return result, status


async def _start_or_reuse_crawl(payload: CrawlRequest, user: User, surface_key: str) -> tuple[dict, int]:
    url = payload.url.strip()
    if not _valid_url(url):
        return {"code": "enter_valid_url", "detail": "Please enter a valid http(s) URL"}, 400

    max_pages = float("inf")

    source_url = db.normalize_url(url)

    if not payload.force_recrawl:
        cached = await db.get_latest_run(source_url)
        # Reusing a cached run is only right if it can answer what was asked.
        # Someone who ticked "save Markdown" must not be handed an older run
        # that has none — that would look like the setting silently did nothing.
        # Exclusions are matched for the same reason: handing back a run that
        # still contains the staging mirror you just excluded would read as the
        # field having done nothing.
        reusable = (
            cached is not None
            and (not payload.capture_markdown or cached.markdown_pages > 0)
            and parse_exclusions(cached.exclusions) == parse_exclusions(payload.exclusions)
            # Otherwise typing a staging URL would hand anyone the report of a
            # site they don't have the password, or the sign-in, for.
            and (not (cached.password_protected or cached.signed_in) or cached.user_id == user.id)
        )
        if reusable:
            return {"cached": True, "run_id": cached.id}, 200

    # Asked before a password is ever sent: a site that doesn't challenge for
    # one never receives it, even if the form had one filled in.
    auth = site_auth.from_form(
        source_url,
        payload.site_username,
        payload.site_password.get_secret_value() if payload.site_password else None,
    )
    if await site_auth.probe(source_url) == "needs_auth":
        if auth is None:
            return {"code": "site_password_needed"}, 400
        if await site_auth.probe(source_url, auth) == "rejected":
            return {"code": "site_password_rejected"}, 400
    else:
        auth = None

    # Signing in on the site's own page, when asked to: done now, so a wrong
    # password is said on the form, not discovered in an empty report. Only
    # the session it gets is kept, and only for this crawl.
    try:
        session = await site_login.from_form(
            source_url, payload.login_url, payload.login_email,
            payload.login_password.get_secret_value() if payload.login_password else None,
            payload.login_session.get_secret_value() if payload.login_session else None,
            auth,
        )
    except site_login.SignInError as exc:
        return {"code": exc.code}, 400

    # Checked before create_job() below adds itself to JOBS — otherwise the
    # new job would always count toward its own admission check, queueing
    # every request one slot too early (e.g. the 3rd of 3 allowed slots).
    at_capacity = len(list_active_jobs()) >= MAX_CONCURRENT_CRAWLS

    job = create_job(source_url=source_url, user_id=user.id, max_pages=max_pages)
    # Set now (rather than waiting for run_crawl's own copy of this, which
    # only runs once actually started) so a queued job shows correct
    # settings immediately, and _maybe_start_next_queued has the right
    # values to launch with later.
    job.domain_scope = payload.domain_scope
    job.exclusions = payload.exclusions
    # How fast this crawl runs. Resolved here rather than inside run_crawl so a
    # queued job already knows what it was promised, and so the value is fixed
    # against the box as it is right now (see app/plans.py).
    job.concurrency = resolve_concurrency(user, active_page_load(list_active_jobs()))
    job.language_setting = payload.language
    # Set here rather than only passed to run_crawl below, because a queued job
    # is started later by _maybe_start_next_queued, which has no payload — the
    # other resume paths read it off the job for the same reason.
    job.capture_markdown = payload.capture_markdown
    # Recorded from the request, not the payload, so the front door someone
    # actually used is what their finished-crawl email links back to.
    job.surface = surface_key
    job.site_auth = auth
    job.password_protected = auth is not None
    job.site_session = session
    job.signed_in = session is not None
    job.login_url = (payload.login_url or "").strip() or None if session is not None else None
    job.guest = user.is_guest

    if at_capacity:
        job.status = "queued"
        # Paid crawls jump the waiting free ones. When the box is full this is
        # most of what "faster" means — page concurrency does nothing for a
        # crawl that hasn't started yet.
        position = enqueue(job.id, front=user.is_pro)
        return {"cached": False, "run_id": job.id, "queued": True, "position": position}, 200

    job.task = asyncio.create_task(
        run_crawl(
            job.id, source_url, max_pages, payload.domain_scope, payload.language,
            pause_at_words=PAUSE_AT_WORDS, capture_markdown=payload.capture_markdown,
            exclusions=payload.exclusions,
        )
    )
    return {"cached": False, "run_id": job.id}, 200


@app.post("/crawl/{job_id}/resume")
async def resume_crawl(
    job_id: str,
    # Optional so a browser still running a cached copy of the old app.js —
    # which posts no body at all — keeps working instead of 422ing on Proceed.
    payload: ResumeRequest | None = None,
    user: User = Depends(require_user_api),
):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "paused":
        return JSONResponse({"status": job.status})
    if job.password_protected and job.site_auth is None:
        # Paused, then the server restarted: the password didn't survive it.
        return JSONResponse({"code": "site_password_gone"}, status_code=409)
    if job.signed_in and job.site_session is None:
        # Nor did the sign-in.
        return JSONResponse({"code": "site_session_gone"}, status_code=409)

    if job.guest:
        return JSONResponse({"code": "sign_in_to_continue"}, status_code=403)
    # The estimate panel is the first place anyone can see what the crawl is
    # about to spend itself on, so exclusions can still be tightened here. None
    # means "keep what the crawl already had".
    _resume_job(job, payload.exclusions if payload else None)
    return JSONResponse({"status": "resuming"})


def _resume_job(job, exclusions: str | None = None) -> None:
    language = job.language_setting or job.detected_language
    job.estimate_result = None
    job.task = asyncio.create_task(
        run_crawl(
            job.id, job.source_url, float("inf"), job.domain_scope, language,
            resume_state=job.resume_state, exclusions=exclusions,
        )
    )


@app.get("/crawl/{run_id}/continue")
async def continue_after_sign_in(run_id: str, request: Request, user: User | None = Depends(get_current_user)):
    """Where "Sign in with Google to continue" lands: a guest's crawl paused at
    the estimate, now theirs (the sign-in handed it over), carries on."""
    target = i18n.localized_path(f"/crawl/{run_id}", request.state.lang)
    if user is None:
        return RedirectResponse(
            "/auth/login?" + urlencode({"next": i18n.localized_path(f"/crawl/{run_id}/continue", request.state.lang)}),
            status_code=303,
        )
    job = get_job(run_id)
    if job is not None and job.user_id == user.id and job.status == "paused" and not job.guest:
        if not (job.password_protected and job.site_auth is None) and not (job.signed_in and job.site_session is None):
            _resume_job(job)
    return RedirectResponse(target, status_code=303)


@app.get("/crawl/{run_id}")
async def crawl_page(
    run_id: str,
    request: Request,
    preview: str = "",
    viewer: User | None = Depends(get_viewer),
):
    login = i18n.localized_path("/login", request.state.lang)
    if viewer is None:
        raise HTTPException(status_code=303, headers={"Location": login})
    job = get_job(run_id)
    guest = viewer.is_guest
    if guest and not await _guest_owns(viewer, run_id):
        raise HTTPException(status_code=303, headers={"Location": login})
    # A guest gets the page without anything that needs an account: no share,
    # no re-crawl, no "emailed to" — and a way to sign in to keep it.
    user = None if guest else viewer
    guest_context = {
        "guest": guest,
        "pause_at_words": PAUSE_AT_WORDS,
        "sign_in_url": "/auth/login?" + urlencode({"next": i18n.localized_path(f"/crawl/{run_id}", request.state.lang)}),
        "continue_url": "/auth/login?" + urlencode(
            {"next": i18n.localized_path(f"/crawl/{run_id}/continue", request.state.lang)}
        ),
        "retention_days": GUEST_RETENTION_DAYS,
    }

    # The projects dialog, for the crawl's owner while it runs.
    live_choices = (await _project_choices(user, [run_id])
                    if user is not None and job is not None and job.user_id == user.id else {})

    def live_view():
        return templates.TemplateResponse(
            request,
            "crawl.html",
            {
                "mode": "live",
                "run_id": run_id,
                "password_protected": job.password_protected,
                "signed_in": job.signed_in,
                "login_url": job.login_url,
                "source_url": job.source_url,
                "display_url": job.source_url,
                "started_at": job.started_at,
                "initial_status_payload": _localized_event(job.status_payload(), request.state.lang),
                "user": user,
                "share_recipients": share_recipients,
                **live_choices,
                **guest_context,
            },
        )

    share_recipients = await db.list_run_shares(run_id)

    # A finished run gets the saved view, even while its job is still in
    # memory. The live view builds its summary from the SSE replay, so on a
    # very large crawl a replay that doesn't make it through leaves the page
    # showing a "Completed" header with no folder or top-pages tables. There
    # is nothing to stream once a run is over, so read it back from the DB
    # and let the page render itself server-side.
    # "paused" is not a finished state here: it's resumable, and the live view
    # is what carries its estimate panel and the Proceed button.
    if job is not None and job.status not in _FINISHED_STATUSES:
        return live_view()

    show_preview = preview == "promos" and is_admin(user)

    run = await db.get_run(run_id)
    if run is None:
        # Terminal in memory but not written yet — save_run() runs moments
        # after the status flips. The live view still resolves through SSE.
        if job is not None:
            return live_view()
        raise HTTPException(status_code=404, detail="Crawl not found")

    # At most one — see rank_page_promos for why the Ink treatment can't be
    # used twice on the same page.
    page_pro, page_wxrks = rank_page_promos(
        pro_upsell(run, user, billing.billing_enabled(), preview=show_preview),
        wxrks_pitch(run, request.state.surface, "past", preview=show_preview),
        preview=show_preview,
    )
    return templates.TemplateResponse(
        request,
        "crawl.html",
        {
            "mode": "past",
            "password_protected": run.password_protected,
            "signed_in": run.signed_in,
            "login_url": run.login_url,
            "run_id": run_id,
            "source_url": run.source_url,
            "display_url": run.shown_url,
            "is_owner": user is not None and run.user_id == user.id,
            **(await _project_choices(user, [run_id]) if user is not None and run.user_id == user.id else {}),
            # For the printed report's last line, when there is a link to give.
            "share_link": _share_url(run_id, request.state.surface, request.state.lang) if run.is_public else None,
            "run": run,
            "summary": _report_summary(run, request.state.lang),
            "initial_pages": [report.page_row(p, request.state.lang) for p in run.pages[: report.PAGE_ROWS]],
            "user": user,
            "share_recipients": share_recipients,
            # Both resolved here rather than in the template — see app/promos.py
            # for why "should this banner appear" is a tested function.
            # ?preview=promos force-renders them for an admin, so the wording can
            # be checked without contriving a run that satisfies every condition
            # — and before Stripe is switched on, which is otherwise the one
            # state in which the Pro banner can never be looked at.
            "pro_upsell": page_pro,
            "wxrks_pitch": page_wxrks,
            **guest_context,
        },
    )


def _report_summary(run, lang: str = i18n.DEFAULT_LANG) -> dict:
    """The saved report's totals, plus what the crawled pages link to that the
    run didn't count — so a subdomain or /de/ folder left out by the settings
    is named instead of silently missing."""
    return {
        **report.summarize(run.pages, lang),
        **report.left_out(run.pages, run.linked_hosts, run.linked_languages, run.other_languages),
        "scope": run.domain_scope,
        "repetition": run.repetition or None,
        "duplicate_pages": run.duplicate_count,
    }


async def _guest_owns(guest: User, run_id: str) -> bool:
    job = get_job(run_id)
    if job is not None:
        return job.user_id == guest.id
    run = await db.get_run(run_id)
    return run is not None and run.user_id == guest.id


@app.get("/share/{run_id}")
async def shared_crawl_page(run_id: str, request: Request):
    run = await db.get_run(run_id)
    if run is None or not run.is_public:
        raise HTTPException(status_code=404, detail="Crawl not found")

    return templates.TemplateResponse(
        request,
        "crawl.html",
        {
            "mode": "shared",
            "password_protected": run.password_protected,
            "signed_in": run.signed_in,
            "run_id": run_id,
            "source_url": run.source_url,
            "display_url": run.shown_url,
            "share_link": _share_url(run_id, request.state.surface, request.state.lang),
            "run": run,
            "summary": _report_summary(run, request.state.lang),
            "initial_pages": [report.page_row(p, request.state.lang) for p in run.pages[: report.PAGE_ROWS]],
            "user": None,
            "share_recipients": [],
            # Nobody is signed in here, so there is no Pro to sell — but this is
            # the one page that reaches people who aren't users at all.
            "pro_upsell": None,
            "wxrks_pitch": wxrks_pitch(run, request.state.surface, "shared"),
        },
    )


async def _readable_run(run_id: str, user: User | None):
    """A run the caller may read: their own, or any run with its public link on.
    404 rather than 403 so a private run isn't distinguishable from a missing one."""
    run = await db.get_run(run_id)
    if run is None or not (run.is_public or (user is not None and run.user_id == user.id)):
        raise HTTPException(status_code=404, detail="Crawl not found")
    return run


@app.delete("/crawl/{run_id}")
async def delete_crawl(run_id: str, user: User = Depends(require_user_api)):
    run = await db.get_run(run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Crawl not found")

    # A crawl still working would carry on writing to a row that no longer
    # exists, and would re-save itself on finishing. Make the caller stop it.
    job = get_job(run_id)
    if job is not None and job.status not in _FINISHED_STATUSES:
        # A code, like the crawl form's errors, so app.js says it in the
        # reader's language; detail stays for anything reading the English.
        return JSONResponse({"code": "stop_before_delete", "detail": "Stop this crawl before deleting it"},
                            status_code=409)

    in_projects = await db.project_ids_for_run(run_id)
    await db.delete_run(run_id)
    for project_id in in_projects:
        await consolidation_job.refresh(project_id, "crawl deleted")
    return JSONResponse({"deleted": True})


@app.get("/crawl/{run_id}/pages")
async def crawl_pages(request: Request, run_id: str, offset: int = 0, limit: int = report.PAGE_ROWS,
                      user: User | None = Depends(get_viewer)):
    """Rows for the pages table, fetched as the reader asks for more. Embedding
    all of them is what made a large report unopenable."""
    run = await _readable_run(run_id, user)
    offset = max(0, offset)
    limit = max(1, min(limit, 1000))
    rows = run.pages[offset : offset + limit]
    lang = _reader_lang(request, None if user is None or user.is_guest else user)
    return JSONResponse({
        "pages": [report.page_row(p, lang) for p in rows],
        "offset": offset,
        "total": len(run.pages),
        "has_more": offset + len(rows) < len(run.pages),
    })


@app.get("/crawl/{run_id}/export.csv")
async def export_csv(run_id: str, user: User | None = Depends(get_viewer)):
    """Built and streamed here rather than in the browser — the CSV for a large
    run is bigger than the page should ever hold in memory."""
    run = await _readable_run(run_id, user)
    host = (urlsplit(run.shown_url).hostname or "crawl").replace("www.", "")

    def rows():
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def flush():
            value = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return value

        # "Repeated words": the page's words that repeat a segment already seen
        # in the crawl (app/repetition.py) — blank on runs from before it.
        writer.writerow(["URL", "Title", "Words", "Repeated words", "Status", "Error"])
        # Chunked rather than yielded per row: every yield is its own ASGI
        # message, and 164k of them cost ~20s in overhead alone.
        for i, page in enumerate(run.pages, 1):
            status = ("blocked" if page.blocked_by_host
                      else "login_required" if page.login_required
                      else "ok" if page.success else "failed")
            writer.writerow([
                page.url, page.title or "", page.word_count if page.success else "",
                page.repeated_words if page.success and page.repeated_words is not None else "",
                status, page.error or "",
            ])
            if i % 2000 == 0:
                yield flush()
        yield flush()

    return StreamingResponse(
        rows(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{host}-word-count.csv"'},
    )


# Why saving Markdown stopped early, by the state the crawler records
# (job_store.Job.markdown_state) — for the archive's README.
_MARKDOWN_STOPPED = {
    # Translators: in the README inside a Markdown download, after "Capture
    # stopped before the end of the crawl": the reason it stopped.
    "stopped_disk": N_("the server was running out of disk space"),
    "stopped_run_cap": N_("this crawl reached the most Markdown one crawl may save"),
    "stopped_global_cap": N_("the server reached the most Markdown it keeps"),
}


def _markdown_readme(run, lang: str) -> str:
    """The README.txt at the top of a Markdown download, in the reader's language."""
    _ = gettext_for(lang)
    ngettext = i18n.ngettext_for(lang)
    try:
        crawled = format_datetime(datetime.fromisoformat(run.created_at), "long",
                                  tzinfo=timezone.utc, locale=i18n.locale_for(lang))
    except (TypeError, ValueError):
        crawled = run.created_at
    lines = [
        # Translators: the first line of the README inside a Markdown download.
        # %(product)s is the product's name; %(site)s is the address crawled.
        _("%(product)s — saved Markdown for %(site)s") % {"product": _(surfaces.COUNTER.name), "site": run.shown_url},
        # Translators: %(date)s is the date and time of the crawl.
        _("Crawled: %(date)s") % {"date": crawled},
        # Translators: %(saved)s is how many pages are in the download and
        # %(total)s how many the crawl counted.
        ngettext("Pages in this archive: %(saved)s of %(total)s crawled",
                 "Pages in this archive: %(saved)s of %(total)s crawled", run.page_count)
        % {"saved": i18n.format_number(run.markdown_pages, lang), "total": i18n.format_number(run.page_count, lang)},
        "",
        _("Main page content only — site navigation, footers and other repeated chrome are stripped. "
          "Each file starts with the URL it came from."),
    ]
    readme = "\n".join(lines)
    if run.markdown_state.startswith("stopped"):
        reason = _MARKDOWN_STOPPED.get(run.markdown_state)
        # Translators: %(reason)s says why, such as "the server was running out
        # of disk space".
        readme += "\n\n" + _(
            "Capture stopped before the end of the crawl because %(reason)s, so this archive covers only "
            "the pages listed above. The word count itself is complete."
        ) % {"reason": _(reason) if reason else run.markdown_state}
    return readme


@app.get("/crawl/{run_id}/markdown.zip")
async def export_markdown(request: Request, run_id: str, user: User | None = Depends(get_viewer)):
    """Readable by the owner or through a live public link, matching Export CSV
    and the report itself — a shared report that withholds half its exports
    isn't really shared. Worth knowing this is the app's one large download, so
    a public link is also a standing egress cost until it's switched off."""
    run = await _readable_run(run_id, user)
    if not run.markdown_pages:
        raise HTTPException(status_code=404, detail="No Markdown was saved for this crawl")

    host = (urlsplit(run.shown_url).hostname or "crawl").replace("www.", "")
    readme = _markdown_readme(run, _reader_lang(request, None if user is None or user.is_guest else user))

    return StreamingResponse(
        markdown_store.iter_zip(run_id, [p.url for p in run.pages], readme=readme),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{host}-markdown.zip"'},
    )


def _share_url(run_id: str, surface=None, lang: str = i18n.DEFAULT_LANG) -> str | None:
    """Built from the surface the sharer is looking at, so a link copied on the
    Markdown domain doesn't hand the recipient the other brand — and in the
    sharer's language, the one the invitation email is written in."""
    return absolute_url(i18n.localized_path(f"/share/{run_id}", lang), surface)


def _reader_lang(request: Request, user: User | None = None) -> str:
    """The language of the page a request came from.

    app.js sends its fetches, the event stream and the downloads with the
    page's prefix (localPath), so request.state.lang is usually it. A browser
    still running the previous app.js sends them unprefixed, and reads English;
    for that one the page is in the Referer, from the same site, and failing
    that the account's saved language."""
    if request.state.lang != i18n.DEFAULT_LANG:
        return request.state.lang
    referer = urlsplit(request.headers.get("referer") or "")
    if referer.path and referer.netloc in ("", request.url.netloc):
        return i18n.split_path(referer.path)[0]
    if user is not None and not user.is_guest and user.lang in i18n.LANGUAGES:
        return user.lang
    return i18n.DEFAULT_LANG


def _localized_event(event: dict, lang: str) -> dict:
    """A crawl event with the crawler's stored English sentences (why it
    stopped, why a page failed) in the reader's language. A copy: the same
    event goes to every subscriber, each in their own language."""
    if lang == i18n.DEFAULT_LANG:
        return event
    event = dict(event)
    for key in ("stopped_reason", "error"):
        if event.get(key):
            event[key] = report.stored_text(event[key], lang)
    if isinstance(event.get("page"), dict) and event["page"].get("error"):
        event["page"] = {**event["page"], "error": report.stored_text(event["page"]["error"], lang)}
    return event


@app.post("/crawl/{run_id}/share")
async def toggle_share(
    run_id: str, request: Request, payload: ShareToggleRequest | None = None,
    user: User = Depends(require_user_api),
):
    run = await db.get_run(run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Run not found")

    new_state = payload.is_public if payload is not None and payload.is_public is not None else not run.is_public
    await db.set_run_public(run_id, new_state)
    return JSONResponse({"is_public": new_state,
                         "share_url": _share_url(run_id, request.state.surface, _reader_lang(request, user))})


@app.post("/crawl/{run_id}/display-url")
async def set_display_url(run_id: str, payload: DisplayUrlRequest, user: User = Depends(require_user_api)):
    """The address a report shows. Kept to the site that was crawled — a report
    can be labelled with its home page, never with somebody else's."""
    run = await db.get_run(run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Run not found")
    text = (payload.url or "").strip()
    if not text:
        await db.set_run_display_url(run_id, None)
        return JSONResponse({"display_url": run.source_url})
    url = db.normalize_url(text if "://" in text else "https://" + text)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        return JSONResponse({"code": "display_url_invalid"}, status_code=422)
    if get_base_domain(url) != get_base_domain(run.source_url):
        return JSONResponse({"code": "display_url_other_site"}, status_code=422)
    await db.set_run_display_url(run_id, None if url == run.source_url else url)
    return JSONResponse({"display_url": url})


@app.post("/crawl/{run_id}/share/email")
async def email_share(
    run_id: str, payload: ShareEmailRequest, request: Request, user: User = Depends(require_user_api)
):
    run = await db.get_run(run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Run not found")

    email = payload.email.strip()
    if not email:
        return JSONResponse({"code": "share_email_required", "detail": "An email address is required"},
                            status_code=400)

    # The invite carries the public link, so it can't work while the report is
    # private — inviting someone is itself the intent to make the link work.
    if not run.is_public:
        await db.set_run_public(run_id, True)

    await db.add_run_share(run_id, email)
    # The sender's language: whoever receives this has no account to have a
    # preference on, and the person choosing to share is the one whose words
    # introduce it. Read from the page they shared from (see _reader_lang).
    lang = _reader_lang(request, user)
    await send_share_notification(
        to_email=email,
        # The name reads better than a raw address in "X shared a report with
        # you", but not everyone's Google profile has one worth showing.
        shared_by=user.name or user.email,
        source_url=run.shown_url,
        share_url=_share_url(run_id, request.state.surface, lang),
        total_words=run.total_words,
        page_count=run.page_count,
        surface=request.state.surface,
        detected_cms=run.detected_cms,
        lang=lang,
    )
    return JSONResponse(
        {"sent": True, "is_public": True, "recipients": await db.list_run_shares(run_id)}
    )


@app.delete("/crawl/{run_id}/share/recipients")
async def remove_share_recipient(run_id: str, email: str, user: User = Depends(require_user_api)):
    run = await db.get_run(run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="Run not found")

    await db.remove_run_share(run_id, email.strip())
    return JSONResponse({"recipients": await db.list_run_shares(run_id)})


async def _cancel_job(job_id: str) -> str:
    """Cancels a job regardless of which process is actually running it
    (JOBS isn't shared across processes) — sets the in-memory flag if we
    happen to have it locally, but always also persists to the DB so
    whichever process is really running it picks this up on its next poll
    (see crawler.py's _should_cancel). Raises 404 only if the job is
    entirely unknown, both here and in the DB; otherwise returns its
    resulting status (a no-op "as-is" status for an already-terminal job)."""
    job = get_job(job_id)
    if job is not None:
        if job.status in _TERMINAL_STATUSES:
            return job.status
        job.request_cancel()
    else:
        run = await db.get_run(job_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if run.status in _TERMINAL_STATUSES:
            return run.status

    await db.request_cancel(job_id)
    return "cancelling"


@app.post("/crawl/{job_id}/cancel")
async def cancel_crawl(job_id: str, user: User | None = Depends(get_viewer)):
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if user.is_guest and not await _guest_owns(user, job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    status = await _cancel_job(job_id)
    return JSONResponse({"status": status})


@app.get("/events/{job_id}")
async def crawl_events(job_id: str, request: Request, user: User | None = Depends(get_viewer)):
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    job = get_job(job_id)
    if job is None or (user.is_guest and job.user_id != user.id):
        raise HTTPException(status_code=404, detail="Job not found")

    lang = _reader_lang(request, None if user.is_guest else user)

    async def event_stream():
        for page in job.pages.values():
            yield _sse("page", _localized_event(
                {"type": "page", "page": page.model_dump(), "total_words": job.total_words}, lang))
        yield _sse("status", _localized_event(job.status_payload(), lang))

        if job.status in _TERMINAL_STATUSES:
            return

        queue: asyncio.Queue = asyncio.Queue()
        job.subscribers.append(queue)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event["type"], _localized_event(event, lang))
                if event["type"] == "status" and event.get("status") in _TERMINAL_STATUSES:
                    break
        finally:
            if queue in job.subscribers:
                job.subscribers.remove(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _error_pct(estimated: int, actual: int | None) -> float | None:
    if not actual:
        return None
    return round((estimated - actual) / actual * 100, 1)


def _aggregate_estimate_errors(rows: list[dict], group_key: str) -> dict[str, dict]:
    groups: dict[str, list[float]] = {}
    for row in rows:
        if row["word_error_pct"] is None:
            continue
        key = row[group_key] or "(none)"
        groups.setdefault(key, []).append(row["word_error_pct"])
    return {
        key: {
            "count": len(errors),
            "avg_signed_pct": round(sum(errors) / len(errors), 1),
            "avg_abs_pct": round(sum(abs(e) for e in errors) / len(errors), 1),
        }
        for key, errors in groups.items()
    }


def _aggregate_speed_by_concurrency(rows: list[dict]) -> dict[str, dict]:
    groups: dict[str, list[float]] = {}
    for row in rows:
        if row["words_per_minute"] is None or row["concurrent_crawls"] is None:
            continue
        key = str(row["concurrent_crawls"])
        groups.setdefault(key, []).append(row["words_per_minute"])
    return {
        key: {"count": len(speeds), "avg_words_per_minute": round(sum(speeds) / len(speeds))}
        for key, speeds in sorted(groups.items())
    }


@app.get("/admin/estimates")
async def admin_estimates(request: Request, admin: User = Depends(require_admin)):
    rows = await db.list_estimate_history()
    for row in rows:
        row["word_error_pct"] = _error_pct(row["estimated_total_words"], row["actual_total_words"])
        row["page_error_pct"] = _error_pct(row["estimated_total_pages"], row["actual_total_pages"])

    return templates.TemplateResponse(
        request,
        "admin_estimates.html",
        {
            "rows": rows,
            "by_confidence": _aggregate_estimate_errors(rows, "confidence"),
            "by_cms": _aggregate_estimate_errors(rows, "detected_cms"),
            "by_concurrency": _aggregate_speed_by_concurrency(rows),
            "user": admin,
        },
    )


async def _job_summary(job) -> dict:
    owner = await db.get_user(job.user_id)
    return {
        "id": job.id,
        "source_url": job.source_url,
        "status": job.status,
        "owner_email": owner.email if owner else "(unknown)",
        "started_at": job.started_at,
        "page_count": len(job.pages),
        "total_words": job.total_words,
    }


@app.get("/admin/health")
async def admin_health(request: Request, admin: User = Depends(require_admin)):
    return templates.TemplateResponse(
        request, "admin_health.html", {"health": await health_snapshot(), "user": admin}
    )


@app.get("/admin/health.json")
async def admin_health_json(admin: User = Depends(require_admin)):
    """The same numbers, for anything that polls — Coolify, an uptime checker,
    a terminal. Behind require_admin like the page it mirrors."""
    return JSONResponse(await health_snapshot())


@app.post("/admin/backfill/detected-cms")
async def admin_backfill_detected_cms(admin: User = Depends(require_admin)):
    """Recover the platform for runs crawled before it was recorded, so their
    reports point at the right connector. Safe to run more than once — it only
    looks at runs that still have none."""
    return JSONResponse(await backfill_detected_cms())


@app.post("/admin/runs/{run_id}/platform")
async def admin_set_platform(
    run_id: str, cms: str = Form(""), back: str = Form("/admin/runs"), admin: User = Depends(require_admin)
):
    """Correct the platform a run was detected as, which picks the connector
    its banners pitch. Detection reads the pages a crawl happened to reach, so
    it can miss — a headless front end hides its CMS, and a run behind a
    password can't be re-probed by the backfill."""
    if cms and cms not in KNOWN_CMS:
        raise HTTPException(status_code=400, detail="Unknown platform")
    if not await db.set_detected_cms([run_id], cms or None):
        raise HTTPException(status_code=404, detail="Crawl not found")
    return _back_to_admin_runs(back)


@app.post("/admin/runs/{run_id}/example")
async def admin_set_example(
    run_id: str, on: str = Form(""), back: str = Form("/admin/runs"), admin: User = Depends(require_admin)
):
    """Feature a finished run on the front page, which also turns its public
    link on — the front page links to it."""
    if not await db.set_run_example(run_id, on == "1"):
        raise HTTPException(status_code=404, detail="Crawl not found")
    return _back_to_admin_runs(back)


def _back_to_admin_runs(back: str) -> RedirectResponse:
    # Only ever back to the admin list it came from, never an arbitrary URL.
    return RedirectResponse(back if back.startswith("/admin/runs") else "/admin/runs", status_code=303)


@app.get("/admin/jobs")
async def admin_jobs(request: Request, admin: User = Depends(require_admin)):
    jobs = [await _job_summary(job) for job in list_active_jobs()]
    queued_jobs = [await _job_summary(job) for job in list_queued_jobs()]
    return templates.TemplateResponse(
        request, "admin_jobs.html", {"jobs": jobs, "queued_jobs": queued_jobs, "user": admin}
    )


@app.get("/admin/runs")
async def admin_runs(request: Request, page: int = 1, q: str = "", admin: User = Depends(require_admin)):
    page = max(1, page)
    query = q.strip()
    total = await db.count_all_runs(query)
    return templates.TemplateResponse(
        request,
        "admin_runs.html",
        {
            "runs": await db.list_all_runs(limit=RUNS_PER_PAGE, offset=(page - 1) * RUNS_PER_PAGE, query=query),
            "page": page,
            "page_count": _page_count(total, RUNS_PER_PAGE),
            "total": total,
            "query": query,
            "base_url": f"/admin/runs?q={quote_plus(query)}&",
            "back": str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
            "platforms": KNOWN_CMS,
            "user": admin,
        },
    )


@app.post("/admin/jobs/{job_id}/cancel")
async def admin_cancel_job(job_id: str, admin: User = Depends(require_admin)):
    status = await _cancel_job(job_id)
    return JSONResponse({"status": status})


@app.post("/admin/jobs/cancel-all")
async def admin_cancel_all(admin: User = Depends(require_admin)):
    cancelled = [job.id for job in list_active_jobs() + list_queued_jobs()]
    for job_id in cancelled:
        await _cancel_job(job_id)
    return JSONResponse({"cancelled": cancelled})


# ------------------------------------------------------------ translation check

TRANSLATION_ROWS = 100
TRANSLATION_PREVIEW_ROWS = 8


def _decode_check(check: dict) -> dict:
    check["summary"] = json.loads(check.get("summary_json") or "{}")
    check["diff"] = json.loads(check.get("diff_json") or "{}")
    check["host"] = (urlsplit(check["site_url"]).hostname or check["site_url"]).removeprefix("www.")
    return check


async def _translation_progress(check: dict) -> dict:
    """Original pages done (each with all its language versions) of all."""
    source = (await db.translation_page_stats(check["id"])).get("", {})
    total = sum(source.get(k) or 0 for k in ("ok", "missing", "errors", "unchecked"))
    return {"done": total - (source.get("unchecked") or 0), "total": total}


async def _translation_check_or_404(check_id: int) -> dict:
    check = await db.get_translation_check(check_id)
    if check is None:
        raise HTTPException(status_code=404, detail="Check not found")
    return _decode_check(check)


def _shape_translation_rows(rows: list[dict]) -> list[dict]:
    for row in rows:
        row["locales"] = sorted({code for code in (row.get("locales") or "").split(",") if code})
        samples = json.loads(row.get("samples") or "[]")
        row["example"] = samples[0] if samples else None
        if row["example"] and row.get("kind") in ("text", "short_text", "english_text"):
            row["example_link"] = translation_check.text_fragment_url(row["example"], row["text"])
        else:
            row["example_link"] = row["example"]
    return rows


def _english_versions(raw: str | None) -> list[dict]:
    versions = []
    for item in (raw or "").split("|"):
        code, _, rest = item.partition("=")
        share, _, url = rest.partition("=")
        if code:
            versions.append({"code": code, "share": int(share or 0), "url": url})
    return sorted(versions, key=lambda v: v["code"])


async def _translation_section(check: dict, key: str, lang: str | None, page: int, limit: int,
                               component: str = "", source: str = "", source_name: str | None = None) -> dict:
    spec = translation_check.SECTIONS[key]
    offset = (page - 1) * limit
    mode = "strings"
    if key == "english":
        rows, total = await db.translation_english_pages(
            check["id"], check["site_url"], translation_check.ENGLISH_PAGE_SHARE,
            translation_check.ENGLISH_PAGE_MIN_WORDS, lang=lang, limit=limit, offset=offset)
        for row in rows:
            row["versions"] = _english_versions(row["versions"])
            row["text_key"] = "page:" + row["source_url"]
            row["text"] = row["source_url"]
        title = f"Pages still in {source_name}" if source_name else spec["title"]
        return {"key": key, **spec, "title": title, "rows": rows, "total": total, "mode": "english_pages"}
    if key == "components" and not component:
        groups = await db.translation_component_groups(check["id"], check["site_url"], spec["kinds"], lang)
        for row in groups:
            row["locales"] = sorted({c for c in (row.get("locales") or "").split(",") if c})
        rows, total, mode = groups[offset:offset + limit], len(groups), "components"
    elif key == "pages" and not source:
        rows, total = await db.translation_source_groups(check["id"], check["site_url"], spec["kinds"],
                                                         lang=lang, limit=limit, offset=offset)
        for row in rows:
            row["locales"] = sorted({c for c in (row.get("locales") or "").split(",") if c})
        mode = "sources"
    else:
        rows, total = await db.translation_finding_groups(
            check["id"], check["site_url"], spec["kinds"], lang=lang,
            component=component or spec.get("component"), source_url=source or None,
            single_page=spec.get("single_page"), limit=limit, offset=offset,
        )
        rows = _shape_translation_rows(rows)
    return {"key": key, **spec, "rows": rows, "total": total, "mode": mode}


def _page_state(row: dict | None) -> dict:
    """One language version of a key page, for the grid."""
    if row is None or row["status"] in ("missing", "redirected"):
        return {"state": "missing"}
    if row["status"] != "ok":
        return {"state": "unchecked"}
    pct = translation_check._pct_translated(row["source_words"], min(row["untranslated_words"], row["source_words"]))
    state = "good" if pct is None or pct >= 95 else "warn" if pct >= 75 else "bad"
    return {"state": state, "pct": pct, "url": row["url"]}


async def _key_pages(check: dict, summary: dict) -> list[dict]:
    """Home and the main menu's pages, each language's version: how much of
    it is translated, and whether there's a screenshot."""
    keys = json.loads(check.get("locales_json") or "{}").get("key_pages") or []
    if not keys:
        return []
    rows = {(r["source_url"], r["locale"]): r for r in await db.translation_key_page_rows(check["id"], keys)}
    shots = {(s["source_url"], s["locale"]): s for s in await db.translation_shots(check["id"])}
    grid = []
    for source in keys:
        cells = []
        for locale in summary.get("locales", []):
            cell = _page_state(rows.get((source, locale["code"])))
            cell["code"] = locale["code"]
            shot = shots.get((source, locale["code"]))
            cell["shot"] = shot["status"] if shot else None
            cells.append(cell)
        grid.append({"source_url": source, "path": urlsplit(source).path or "/", "cells": cells})
    return grid


async def _key_page_view(check: dict, summary: dict, source: str, lang: str) -> dict | None:
    """One key page in one language: its screenshot and what's untranslated."""
    keys = json.loads(check.get("locales_json") or "{}").get("key_pages") or []
    if source not in keys:
        return None
    rows = {r["locale"]: r for r in await db.translation_key_page_rows(check["id"], [source])}
    codes = [locale["code"] for locale in summary.get("locales", [])]
    lang = lang if lang in codes else (codes[0] if codes else "")
    row = rows.get(lang)
    shots = {(s["source_url"], s["locale"]): s for s in await db.translation_shots(check["id"])}
    ignored = await db.translation_ignore_keys(check["site_url"])
    findings = []
    for kind, key, text, words, where, component_id, label, detail in json.loads((row or {}).get("findings_json") or "[]"):
        if (lang, key) in ignored or ("*", key) in ignored:
            continue
        findings.append({"kind": kind, "text_key": key, "text": text, "words": words, "where_hint": where,
                         "component_label": label, "detail": detail,
                         "example_link": translation_check.text_fragment_url(row["url"], text)
                         if kind in translation_check.TEXT_KINDS else row["url"]})
    order = {kind: i for i, kind in enumerate(translation_check.KINDS)}
    findings.sort(key=lambda f: (order.get(f["kind"], 99), -f["words"]))
    tabs = []
    for code in codes:
        tab = _page_state(rows.get(code))
        tab["code"] = code
        tabs.append(tab)
    return {"source_url": source, "path": urlsplit(source).path or "/", "lang": lang,
            "page": _page_state(row), "row": row, "shot": shots.get((source, lang)), "findings": findings,
            "tabs": tabs}


@app.get("/admin/translations")
async def admin_translations(request: Request, admin: User = Depends(require_translation_viewer)):
    checks = [_decode_check(check) for check in await db.list_translation_checks()]
    active = next((check for check in checks if check["status"] in ("planning", "running")), None)
    return templates.TemplateResponse(
        request,
        "admin_translations.html",
        {
            "checks": checks,
            "active": active,
            "progress": await _translation_progress(active) if active else None,
            "scheduled": translation_check.SITES,
            "every_days": translation_check.EVERY_DAYS,
            "hour_utc": translation_check.HOUR_UTC,
            "busy": request.query_params.get("busy") == "1",
            "user": admin,
        },
    )


@app.post("/admin/translations/start")
async def admin_translation_start(url: str = Form(...), admin: User = Depends(require_admin)):
    raw = url.strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        raise HTTPException(status_code=400, detail="Enter a site address, like https://wxrks.com")
    try:
        check_id = await translation_check.start_check(f"{parts.scheme}://{parts.netloc}", "manual")
    except RuntimeError:
        return RedirectResponse("/admin/translations?busy=1", status_code=303)
    return RedirectResponse(f"/admin/translations/{check_id}", status_code=303)


@app.get("/admin/translations/{check_id}")
async def admin_translation_report(
    request: Request, check_id: int, lang: str = "", section: str = "", page: int = 1,
    component: str = "", source: str = "", admin: User = Depends(require_translation_viewer),
):
    check = await _translation_check_or_404(check_id)
    # Older checks keep only their summary (prune_translation_checks).
    has_detail = bool(await db.translation_page_stats(check["id"]))
    if has_detail and check["locales_json"]:
        summary = await translation_check.summarize(check)
    else:
        summary = check["summary"]
    codes = {locale["code"] for locale in summary.get("locales", [])}
    lang = lang if lang in codes else ""
    page = max(1, page)
    sections: list[dict] = []
    folders: list[tuple[str, int]] = []
    key_grid: list[dict] = []
    key_view = None
    source_name = summary.get("source_name")
    if not has_detail:
        pass
    elif section == "key":
        key_view = await _key_page_view(check, summary, source, request.query_params.get("lang", ""))
        if key_view is None:
            raise HTTPException(status_code=404, detail="Not a key page of this check")
        lang = ""
    elif section in translation_check.SECTIONS:
        sections = [await _translation_section(check, section, lang or None, page, TRANSLATION_ROWS,
                                               component, source, source_name)]
    elif summary:
        key_grid = await _key_pages(check, summary)
        for key in translation_check.SECTIONS:
            found = await _translation_section(check, key, lang or None, 1, TRANSLATION_PREVIEW_ROWS,
                                               source_name=source_name)
            if found["total"]:
                sections.append(found)
    if has_detail and summary and section in ("", "missing"):
        missing, _ = await db.translation_finding_groups(check["id"], check["site_url"], ("missing_page",),
                                                         lang=lang or None, limit=100_000)
        counts: dict[str, int] = {}
        for row in missing:
            folder = report.folder_for_url(row["text"])
            counts[folder] = counts.get(folder, 0) + 1
        folders = sorted(counts.items(), key=lambda kv: -kv[1])[:12]
    query = {k: v for k, v in (("lang", lang), ("section", section), ("component", component),
                                ("source", source)) if v}
    total = sections[0]["total"] if section and sections else 0
    return templates.TemplateResponse(
        request,
        "admin_translation_check.html",
        {
            "check": check,
            "summary": summary,
            "lang": lang,
            "section": section,
            "component": component,
            "source": source,
            "sections": sections,
            "folders": folders,
            "key_grid": key_grid,
            "key_view": key_view,
            "progress": await _translation_progress(check) if check["status"] in ("planning", "running") else None,
            "has_detail": has_detail,
            "ignores": await db.list_translation_ignores(check["site_url"]),
            "kind_labels": translation_check.KIND_LABELS,
            "where_labels": translation_check.WHERE_LABELS,
            "locale_names": {item["code"]: item["name"] for item in summary.get("locales", [])},
            "page": page,
            "page_count": _page_count(total, TRANSLATION_ROWS),
            "base_url": f"/admin/translations/{check_id}?{urlencode(query)}&" if query else f"/admin/translations/{check_id}?",
            "back": str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
            "user": admin,
        },
    )


@app.get("/admin/translations/{check_id}/shots/{name}")
async def admin_translation_shot(check_id: int, name: str, viewer: User = Depends(require_translation_viewer)):
    from app import translation_shots

    path = translation_shots.path_for(check_id, name)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.post("/admin/translations/{check_id}/cancel")
async def admin_translation_cancel(check_id: int, admin: User = Depends(require_admin)):
    await _translation_check_or_404(check_id)
    await db.request_translation_cancel(check_id)
    return RedirectResponse(f"/admin/translations/{check_id}", status_code=303)


@app.post("/admin/translations/{check_id}/ignore")
async def admin_translation_ignore(
    check_id: int, key: str = Form(...), text: str = Form(""), locale: str = Form("*"),
    undo: str = Form(""), back: str = Form(""), admin: User = Depends(require_translation_viewer),
):
    """Keep as is: a name, a brand, a quote that stays in the original. Per
    language, or '*' for all of them."""
    check = await _translation_check_or_404(check_id)
    if undo:
        await db.delete_translation_ignore(check["site_url"], locale, key)
    else:
        await db.add_translation_ignore(check["site_url"], locale or "*", key, text, admin.email,
                                        datetime.now(timezone.utc).isoformat())
    target = back if back.startswith(f"/admin/translations/{check_id}") else f"/admin/translations/{check_id}"
    return RedirectResponse(target, status_code=303)


def _csv_stream(header: list[str], rows_source):
    async def stream():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(header)
        n = 0
        async for row in rows_source:
            writer.writerow(row)
            n += 1
            if n % 2000 == 0:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
        yield buffer.getvalue()

    return stream()


@app.get("/admin/translations/{check_id}/findings.csv")
async def admin_translation_findings_csv(check_id: int, lang: str = "",
                                         admin: User = Depends(require_translation_viewer)):
    """One row per finding and language — the list to send to translation."""
    check = await _translation_check_or_404(check_id)
    kinds, wheres = translation_check.KIND_LABELS, translation_check.WHERE_LABELS

    async def rows():
        async for f in db.iter_translation_findings(check_id, check["site_url"], lang or None):
            samples = json.loads(f["sample_urls_json"] or "[]")
            yield [f["locale"], kinds.get(f["kind"], f["kind"]), f["detail"] or "", f["text"], f["page_count"],
                   f["words"], wheres.get(f["where_hint"], f["where_hint"] or ""), f["component_label"] or "",
                   samples[0] if samples else "", f["source_url"] or "", "yes" if f["is_new"] else ""]

    suffix = f"-{lang}" if lang else ""
    return StreamingResponse(
        _csv_stream(["Language", "Kind", "Field", "Text", "Pages", "Words", "Where", "Component",
                     "Example page", "Original page", "New"], rows()),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{check["host"]}-translation-check{suffix}.csv"'},
    )


@app.get("/admin/translations/{check_id}/pages.csv")
async def admin_translation_pages_csv(check_id: int, lang: str = "",
                                      admin: User = Depends(require_translation_viewer)):
    check = await _translation_check_or_404(check_id)

    async def rows():
        async for p in db.iter_translation_pages(check_id, lang or None):
            left = (round(100 * p["untranslated_words"] / p["source_words"], 1)
                    if p["status"] == "ok" and p["source_words"] else "")
            yield [p["locale"], p["source_url"], p["url"] or "", p["status"], p["http_status"] or "",
                   p["source_words"] or "", p["untranslated_words"] if p["status"] == "ok" else "", left]

    suffix = f"-{lang}" if lang else ""
    return StreamingResponse(
        _csv_stream(["Language", "Original page", "Translated page", "Status", "HTTP status", "Original words",
                     "Words left untranslated", "% left"], rows()),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{check["host"]}-translation-pages{suffix}.csv"'},
    )


# ------------------------------------------------------------------ projects
#
# A project groups one person's runs and counts each page once across them —
# app/projects.py has the arithmetic. Only ever its owner's: anyone else gets
# a 404, as for a report, and guests have none.

async def _owned_project(user: User | None, project_id: str) -> dict | None:
    if user is None or user.is_guest:
        return None
    project = await db.get_project(project_id)
    return project if project is not None and project["user_id"] == user.id else None


async def _project_or_404(user: User, project_id: str) -> dict:
    project = await _owned_project(user, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def _project_choices(user: User | None, run_ids: list[str]) -> dict:
    """What a page needs to offer projects: the user's list, and the projects
    each of the runs on the page is in."""
    if user is None or user.is_guest:
        return {"projects": [], "run_projects": {}}
    return {
        "projects": await db.list_user_projects(user.id),
        "run_projects": await db.projects_for_runs(run_ids),
    }


async def _new_project(user: User, name: str) -> dict:
    return await db.create_project(user.id, name.strip()[:120], projects.new_id())


async def _put_in_projects(user: User, run_id: str, project_ids: list[str], new_name: str = "") -> None:
    if new_name:
        project_ids = [*project_ids, (await _new_project(user, new_name))["id"]]
    for project_id in project_ids:
        await db.add_runs_to_project(project_id, [run_id])
        await consolidation_job.refresh(project_id, "crawl added")


async def _owned_runs(user: User, run_ids: list[str]) -> list[str]:
    """Only runs that are the user's go into their projects."""
    owned = []
    for run_id in dict.fromkeys(run_ids):
        job = get_job(run_id)
        run = await db.get_run(run_id) if job is None else None
        owner = job.user_id if job is not None else (run.user_id if run is not None else None)
        if owner == user.id:
            owned.append(run_id)
    return owned


def _project_share_url(project_id: str, surface=None, lang: str = i18n.DEFAULT_LANG) -> str | None:
    """In the sharer's language, like _share_url."""
    return absolute_url(i18n.localized_path(f"/projects/share/{project_id}", lang), surface)


# ---------- Articles, robots.txt and the sitemap ----------

@app.get("/articles")
async def articles_index(request: Request, user: User | None = Depends(get_current_user)):
    return templates.TemplateResponse(request, "articles/index.html", {"user": user, "articles": articles.ARTICLES})


@app.get("/articles/{slug}")
async def article_page(slug: str, request: Request, user: User | None = Depends(get_current_user)):
    article = articles.BY_SLUG.get(slug)
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return templates.TemplateResponse(request, article.template, {"user": user, "article": article,
                                                                  "experiment": articles.EXPERIMENT,
                                                                  "comparison": articles.COMPARISON})


@app.get("/robots.txt")
async def robots_txt(request: Request):
    """Reports, projects and accounts stay out of search; the front door, the
    MCP guide and the articles are in the sitemap."""
    private = ["/crawl/", "/share/", "/projects", "/runs", "/admin", "/account", "/mcp/"]
    # Robots rules match by prefix, so /share/ alone leaves /de/share/ open:
    # every language's copy of a private path is listed too.
    lines = ["User-agent: *"]
    for code in i18n.LANGUAGES:
        lines += [f"Disallow: {i18n.localized_path(path, code)}" for path in private]
    lines += ["Allow: /", f"Sitemap: {request.state.surface.url('/sitemap.xml')}"]
    return Response("\n".join(lines) + "\n", media_type="text/plain")


@app.get("/sitemap.xml")
async def sitemap_xml(request: Request):
    """Each published language's version is its own <url>, and each lists the
    full set (itself and x-default included), which is what hreflang in a
    sitemap requires. Articles are English only, so they have no alternates."""
    surface = request.state.surface
    published = i18n.published_languages()
    paths = ["/", "/mcp", "/articles"] + [f"/articles/{a.slug}" for a in articles.ARTICLES]
    entries = []
    for path in paths:
        if path.startswith("/articles/"):
            entries.append(f"<url><loc>{escape(surface.url(path))}</loc></url>")
            continue
        alternates = "".join(
            f'<xhtml:link rel="alternate" hreflang="{i18n.HREFLANG.get(code, code)}" '
            f'href="{escape(surface.url(i18n.localized_path(path, code)))}"/>'
            for code in published
        ) + f'<xhtml:link rel="alternate" hreflang="x-default" href="{escape(surface.url(path))}"/>'
        for code in published:
            loc = surface.url(i18n.localized_path(path, code))
            entries.append(f"<url><loc>{escape(loc)}</loc>{alternates}</url>")
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">'
            + "".join(entries) + "</urlset>\n")
    return Response(body, media_type="application/xml")


# ---------- The MCP server: Claude, ChatGPT and other assistants (app/mcp_server.py) ----------

def _wants_event_stream(request: Request) -> bool:
    """An MCP client opening a stream, not a person opening the page."""
    accept = request.headers.get("accept", "")
    return "text/event-stream" in accept and "text/html" not in accept


def _mcp_backend(request: Request) -> mcp_server.Backend:
    surface = request.state.surface
    return mcp_server.Backend(
        begin_crawl=lambda payload, user: _begin_crawl(payload, user, surface.key),
        resume=_resume_job,
        site_url=surface.url,
    )


async def _mcp_post(request: Request, token: str | None) -> Response:
    """The endpoint. The key comes in the URL or as a Bearer header; with
    neither, the server still describes itself and its tools, and a tool call
    explains how to connect. Session cookies are never read here, so a page in
    someone's browser can't act on their account through it."""
    header = request.headers.get("authorization", "")
    bearer = header[7:].strip() if header.lower().startswith("bearer ") else None
    supplied = token or bearer
    user = await mcp_keys.user_for(supplied) if supplied else None
    if supplied and user is None:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {
                "code": -32001,
                "message": "This Word Counter key isn't valid (revoked or mistyped). Make a new connection at "
                           + request.state.surface.url("/mcp/connect")}},
            status_code=401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
    try:
        message = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                            status_code=400)
    if isinstance(message, list):
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {
            "code": -32600, "message": "Send one JSON-RPC request per POST; batches aren't supported."}},
            status_code=400)
    reply = await mcp_server.handle(message, user=user, backend=_mcp_backend(request),
                                    header_version=request.headers.get("mcp-protocol-version"))
    if reply.body is None:
        return Response(status_code=202)
    headers = {"Cache-Control": "no-store"}
    if reply.version in mcp_server.SUPPORTED:
        headers["MCP-Protocol-Version"] = reply.version
    return JSONResponse(reply.body, status_code=reply.status, headers=headers)


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    return await _mcp_post(request, None)


@app.get("/mcp")
async def mcp_page(request: Request, user: User | None = Depends(get_current_user)):
    """People get the guide; an MCP client asking for a stream gets 405 — this
    server answers every request directly and never streams."""
    if _wants_event_stream(request):
        return Response(status_code=405, headers={"Allow": "POST"})
    return templates.TemplateResponse(request, "mcp.html", {
        "user": user,
        "endpoint": request.state.surface.url("/mcp"),
        "tools": mcp_server.TOOLS,
        # The page quotes the pause threshold as a number, formatted there for
        # the reader rather than written into the copy.
        "pause_at_words": PAUSE_AT_WORDS,
    })


@app.delete("/mcp")
async def mcp_no_sessions():
    return Response(status_code=405, headers={"Allow": "POST"})


@app.get("/mcp/connect")
async def mcp_connect_page(request: Request, user: User = Depends(require_user)):
    """Where a user gets their personal connection URL, and revokes old ones."""
    return templates.TemplateResponse(request, "mcp_connect.html", {
        "user": user,
        "keys": await db.list_api_keys(user.id),
        "max_keys": mcp_keys.MAX_KEYS,
        "endpoint": request.state.surface.url("/mcp"),
    })


@app.post("/account/mcp-keys")
async def create_mcp_key(request: Request, payload: McpKeyRequest, user: User = Depends(require_user_api)):
    if user.is_guest:
        raise HTTPException(status_code=403, detail="Sign in to connect an assistant")
    if len(await db.list_api_keys(user.id)) >= mcp_keys.MAX_KEYS:
        return JSONResponse({"code": "too_many_keys"}, status_code=400)
    token, row = await mcp_keys.create(user, payload.name)
    endpoint = request.state.surface.url("/mcp")
    return JSONResponse({**row, "key": token, "endpoint": endpoint, "url": f"{endpoint}/{token}"})


@app.delete("/account/mcp-keys/{key_id}")
async def revoke_mcp_key(key_id: str, user: User = Depends(require_user_api)):
    if not await db.revoke_api_key(user.id, key_id):
        raise HTTPException(status_code=404, detail="Key not found")
    return JSONResponse({"revoked": key_id})


@app.post("/mcp/{token}")
async def mcp_endpoint_with_key(token: str, request: Request):
    return await _mcp_post(request, token)


@app.get("/mcp/{token}")
async def mcp_personal_url(token: str, request: Request):
    """The personal URL opened in a browser: the guide, without the key in it."""
    if _wants_event_stream(request):
        return Response(status_code=405, headers={"Allow": "POST"})
    return RedirectResponse(i18n.localized_path("/mcp", request.state.lang), status_code=303)


@app.delete("/mcp/{token}")
async def mcp_no_sessions_with_key(token: str):
    return Response(status_code=405, headers={"Allow": "POST"})


@app.get("/projects")
async def projects_home(request: Request, user: User = Depends(require_user)):
    if user.is_guest:
        return RedirectResponse(
            "/auth/login?" + urlencode({"next": i18n.localized_path("/projects", request.state.lang)}),
            status_code=303)
    loose = await db.list_runs_not_in_project(user.id, limit=20)
    return templates.TemplateResponse(
        request,
        "projects.html",
        {
            "user": user,
            "projects": await db.list_user_projects(user.id),
            "project_sites": await db.list_project_sites(user.id),
            "loose_runs": loose,
            "run_projects": {},
        },
    )


@app.post("/projects")
async def create_project(payload: ProjectRequest, request: Request, user: User = Depends(require_user_api)):
    if user.is_guest:
        raise HTTPException(status_code=403, detail="Sign in to make projects")
    if not payload.name.strip():
        return JSONResponse({"code": "project_name_required"}, status_code=400)
    project = await _new_project(user, payload.name)
    run_ids = await _owned_runs(user, payload.run_ids)
    if run_ids:
        await db.add_runs_to_project(project["id"], run_ids[: projects.MAX_RUNS])
    await consolidation_job.refresh(project["id"], "created")
    # app.js goes straight there, so it opens in the language of the page
    # the project was made from.
    url = i18n.localized_path(f"/projects/{project['id']}", _reader_lang(request, user))
    return JSONResponse({"id": project["id"], "url": url})


@app.get("/projects/share/{project_id}")
async def shared_project_page(project_id: str, request: Request):
    """The client's view, through a public link."""
    project = await db.get_project(project_id)
    if project is None or not project["is_public"]:
        raise HTTPException(status_code=404, detail="Project not found")
    return await _project_client_view(request, project, None)


async def _project_view(project_id: str, lang: str = i18n.DEFAULT_LANG) -> dict:
    """What a project page shows: the last consolidation, the job's state, and
    a row per crawl — the consolidated ones in the order they were read, then
    any it doesn't have yet (still crawling, or added since)."""
    summaries = await db.list_project_run_summaries(project_id)
    await consolidation_job.refresh(project_id, "viewed")
    row = await db.get_consolidation(project_id)
    result = row["result"] if row else None
    live = {s["id"]: s for s in summaries}
    rows = []
    if result:
        for run_row in result["runs"]:
            if run_row["id"] in live:
                rows.append({**run_row, "shown_url": live[run_row["id"]]["shown_url"],
                             "is_public": live[run_row["id"]]["is_public"], "consolidated": True})
    known = {r["id"] for r in rows}
    for summary in summaries:
        if summary["id"] not in known:
            rows.append({**summary, "state": "waiting", "consolidated": False,
                         "own_words": summary["total_words"], "own_pages": summary["page_count"]})
    names = {s["id"]: s["shown_url"] for s in summaries}
    if result:
        names = {**result.get("names", {}), **names}
    return {
        "c": result,
        "job": report.consolidation_status(await consolidation_job.status(project_id), lang),
        "rows": rows,
        "names": names,
        "run_count": len(summaries),
        "stale": bool(row and row["status"] in ("queued", "running")),
    }


async def _project_client_view(request: Request, project: dict, user: User | None):
    view = await _project_view(project["id"], request.state.lang)
    return templates.TemplateResponse(
        request,
        "project_client.html",
        {
            "user": user,
            "project": project,
            **view,
            "is_owner": user is not None,
            "share_link": (_project_share_url(project["id"], request.state.surface, request.state.lang)
                           if project["is_public"] else None),
        },
    )


@app.get("/projects/{project_id}/client")
async def project_client_preview(project_id: str, request: Request, user: User = Depends(require_user)):
    """The owner looking at what the share link shows — and printing it."""
    return await _project_client_view(request, await _project_or_404(user, project_id), user)


@app.get("/projects/{project_id}")
async def project_page(project_id: str, request: Request, user: User = Depends(require_user)):
    project = await _project_or_404(user, project_id)
    view = await _project_view(project_id, request.state.lang)
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "user": user,
            "project": project,
            **view,
            "addable_runs": await db.list_runs_not_in_project(user.id, project_id, limit=100),
            "max_runs": projects.MAX_RUNS,
            "share_link": _project_share_url(project_id, request.state.surface, request.state.lang),
        },
    )


@app.post("/projects/{project_id}/rename")
async def rename_project(project_id: str, payload: ProjectRequest, user: User = Depends(require_user_api)):
    await _project_or_404(user, project_id)
    if not payload.name.strip():
        return JSONResponse({"code": "project_name_required"}, status_code=400)
    await db.rename_project(project_id, payload.name.strip()[:120])
    return JSONResponse({"name": payload.name.strip()[:120]})


@app.post("/projects/{project_id}/share")
async def share_project(project_id: str, request: Request, payload: ShareToggleRequest | None = None,
                        user: User = Depends(require_user_api)):
    project = await _project_or_404(user, project_id)
    state = payload.is_public if payload is not None and payload.is_public is not None else not project["is_public"]
    await db.set_project_public(project_id, state)
    return JSONResponse({"is_public": state, "share_url": _project_share_url(
        project_id, request.state.surface, _reader_lang(request, user))})


@app.delete("/projects/{project_id}")
async def delete_project(project_id: str, user: User = Depends(require_user_api)):
    """The project goes; its runs stay."""
    await _project_or_404(user, project_id)
    await db.delete_project(project_id)
    consolidation_job.delete_files(project_id)
    return JSONResponse({"deleted": True})


@app.post("/projects/{project_id}/runs")
async def add_project_runs(project_id: str, payload: ProjectRunsRequest, user: User = Depends(require_user_api)):
    await _project_or_404(user, project_id)
    present = len(await db.list_project_run_summaries(project_id))
    run_ids = (await _owned_runs(user, payload.run_ids))[: max(0, projects.MAX_RUNS - present)]
    if run_ids:
        await db.add_runs_to_project(project_id, run_ids)
        await consolidation_job.refresh(project_id, "crawls added")
    return JSONResponse({"added": run_ids})


@app.delete("/projects/{project_id}/runs/{run_id}")
async def remove_project_run(project_id: str, run_id: str, user: User = Depends(require_user_api)):
    await _project_or_404(user, project_id)
    await db.remove_run_from_project(project_id, run_id)
    await consolidation_job.refresh(project_id, "crawl removed")
    return JSONResponse({"removed": run_id})


@app.post("/projects/{project_id}/consolidate")
async def consolidate_project(project_id: str, request: Request, user: User = Depends(require_user_api)):
    """The Consolidate button: goes through every crawl in the project again."""
    await _project_or_404(user, project_id)
    await consolidation_job.request(project_id, "button")
    return JSONResponse(report.consolidation_status(await consolidation_job.status(project_id), request.state.lang))


@app.get("/projects/{project_id}/consolidation")
async def project_consolidation_status(project_id: str, request: Request, user: User = Depends(require_user_api)):
    """Polled by the project page while a consolidation runs."""
    await _project_or_404(user, project_id)
    return JSONResponse(report.consolidation_status(await consolidation_job.status(project_id), request.state.lang))


@app.post("/crawl/{run_id}/projects")
async def set_run_projects(run_id: str, payload: RunProjectsRequest, user: User = Depends(require_user_api)):
    """The projects a run is in, as the dialog's ticks say, plus a new one if
    named. Works on a crawl still running: membership doesn't need its row."""
    if user.is_guest or not await _owned_runs(user, [run_id]):
        raise HTTPException(status_code=404, detail="Run not found")
    wanted = []
    for project_id in dict.fromkeys(payload.project_ids):
        if await _owned_project(user, project_id) is None:
            return JSONResponse({"code": "project_not_found"}, status_code=400)
        wanted.append(project_id)
    current = await db.project_ids_for_run(run_id)
    for project_id in current:
        if project_id not in wanted:
            await db.remove_run_from_project(project_id, run_id)
            await consolidation_job.refresh(project_id, "crawl removed")
    await _put_in_projects(user, run_id, [p for p in wanted if p not in current], (payload.new_name or "").strip())
    return JSONResponse({"projects": (await db.projects_for_runs([run_id])).get(run_id, [])})


def _consolidated_csv(path, only_overlap: bool = False):
    """Streams a consolidation's CSV; with only_overlap, just the pages found
    in more than one crawl (a "Same page as" or an "Also in")."""
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        if not only_overlap:
            while chunk := handle.read(256 * 1024):
                yield chunk
            return
        reader = csv.reader(handle)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(next(reader, []))
        for i, row in enumerate(reader, 1):
            if len(row) > 5 and (row[4] or row[5]):
                writer.writerow(row)
            if i % 2000 == 0:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
        yield buffer.getvalue()


def _project_slug(project: dict) -> str:
    return re.sub(r"[^a-z0-9]+", "-", project["name"].lower()).strip("-") or "project"


@app.get("/projects/{project_id}/export.csv")
async def export_project_csv(project_id: str, only: str = "", user: User = Depends(require_user)):
    """Every page of every crawl in the project: where it's counted, where else
    it is, and its new words. ?only=overlap keeps the pages in more than one."""
    project = await _project_or_404(user, project_id)
    path = consolidation_job.csv_path(project_id, "pages")
    if not path.exists():
        raise HTTPException(status_code=404, detail="This project hasn't been consolidated yet")
    overlap = only == "overlap"
    name = f"{_project_slug(project)}-{'overlapping-pages' if overlap else 'pages'}.csv"
    return StreamingResponse(_consolidated_csv(path, overlap), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/projects/{project_id}/sentences.csv")
async def export_project_sentences(project_id: str, user: User = Depends(require_user)):
    """The sentences found more than once in the project, most words removed first."""
    project = await _project_or_404(user, project_id)
    path = consolidation_job.csv_path(project_id, "sentences")
    if not path.exists():
        raise HTTPException(status_code=404, detail="This project hasn't been consolidated yet")
    return StreamingResponse(_consolidated_csv(path), media_type="text/csv",
                             headers={"Content-Disposition":
                                      f'attachment; filename="{_project_slug(project)}-repeated-sentences.csv"'})
