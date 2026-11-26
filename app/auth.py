from __future__ import annotations

import logging
import os
import uuid

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import db
from app.job_store import reassign_jobs
from app.models import User
from app.templates import templates

logger = logging.getLogger(__name__)

router = APIRouter()

# The run copied into every new account so the first sign-in isn't an empty
# page. Matched by URL rather than run id so re-crawling the site refreshes
# what new users see, with no config change and no id to look up.
SAMPLE_RUN_URL = os.environ.get("SAMPLE_RUN_URL", "https://wxrks.com")


async def seed_sample_run(user_id: int) -> None:
    """Best effort: a new account signing in must never fail because the demo
    run is missing or the copy went wrong."""
    if not SAMPLE_RUN_URL:
        return
    try:
        template = await db.get_latest_run(db.normalize_url(SAMPLE_RUN_URL))
        if template is None:
            logger.info("No completed run for SAMPLE_RUN_URL=%s — new account starts empty", SAMPLE_RUN_URL)
            return
        await db.copy_run_to_user(template, user_id, uuid.uuid4().hex, as_sample=True)
    except Exception:
        logger.exception("Could not seed the sample run for user %s", user_id)

oauth = OAuth()
oauth.register(
    name="google",
    client_id=os.environ["GOOGLE_CLIENT_ID"],
    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


async def get_current_user(request: Request) -> User | None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    if not hasattr(request.state, "user_cache"):
        user = await db.get_user(user_id)
        # Remember the language they are browsing in. Emails are sent later from
        # background tasks that have no request to read a prefix from, so this
        # is the only moment the preference is observable.
        lang = getattr(request.state, "lang", None)
        if user is not None and lang and lang != user.lang:
            try:
                await db.set_user_lang(user.id, lang)
                user = user.model_copy(update={"lang": lang})
            except Exception:
                logger.exception("Could not remember the language for user %s", user.id)
        request.state.user_cache = user
    return request.state.user_cache


async def get_guest(request: Request) -> User | None:
    """The anonymous account this browser started crawls under, if any."""
    guest_id = request.session.get("guest_user_id")
    if guest_id is None:
        return None
    guest = await db.get_user(guest_id)
    return guest if guest is not None and guest.is_guest else None


async def guest_for(request: Request) -> User:
    """Created on the first crawl started without signing in, and kept in the
    session — so the crawl has an owner every existing access check
    understands, and signing in can hand everything over in one step."""
    guest = await get_guest(request)
    if guest is None:
        guest, _ = await db.get_or_create_user(f"guest:{uuid.uuid4().hex}", "", "Guest", None)
        request.session["guest_user_id"] = guest.id
    return guest


async def get_viewer(request: Request, user: User | None = Depends(get_current_user)) -> User | None:
    """For the pages a crawl lives on: the signed-in user, or else the guest
    who started it. Guests only ever reach their own crawls — each route
    checks that, since a signed-in account's access is wider."""
    return user if user is not None else await get_guest(request)


async def claim_guest_runs(guest_id: int, user_id: int) -> int:
    """On sign-in, everything the browser crawled as a guest becomes theirs —
    finished runs in the database and anything still running in memory."""
    reassign_jobs(guest_id, user_id)
    return await db.reassign_runs(guest_id, user_id)


async def require_user(request: Request) -> User:
    """Use for HTML pages: bounces an anonymous browser to /login."""
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


async def require_user_api(request: Request) -> User:
    """Use for JSON/SSE endpoints: a redirect isn't meaningful there."""
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def is_admin(user: User | None) -> bool:
    """Read at call time, not import time, so adding an address to ADMIN_EMAILS
    takes effect on restart rather than needing a rebuild. Shared with the
    templates so the menu offers exactly the pages the routes will allow —
    a menu that lists a page you then get 404 from is worse than no menu."""
    if user is None:
        return False
    admins = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
    return user.email.lower() in admins


def require_admin(user: User = Depends(require_user)) -> User:
    """404, not 403 — a non-admin shouldn't be able to tell this route
    exists at all, not just that they're forbidden from it."""
    if not is_admin(user):
        raise HTTPException(status_code=404)
    return user


def translation_viewers() -> set[str]:
    """Who can open the translation check reports besides the admins: the
    people fixing the translations, who shouldn't need the rest of admin."""
    return {e.strip().lower() for e in os.environ.get("TRANSLATION_CHECK_VIEWERS", "").split(",") if e.strip()}


def can_view_translations(user: User | None) -> bool:
    if user is None:
        return False
    return is_admin(user) or (user.email or "").lower() in translation_viewers()


def require_translation_viewer(user: User = Depends(require_user)) -> User:
    """404 to everyone else, like require_admin."""
    if not can_view_translations(user):
        raise HTTPException(status_code=404)
    return user


@router.get("/login")
async def login_page(request: Request):
    error = request.query_params.get("error")
    return templates.TemplateResponse(request, "login.html", {"error": error})


@router.get("/auth/login")
async def auth_login(request: Request):
    # Where to land afterwards, for links like "Sign in to continue this crawl".
    if request.query_params.get("next"):
        request.session["after_login"] = after_login_path(request.query_params["next"])
    redirect_uri = request.url_for("auth_callback")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        return RedirectResponse(url="/login?error=Google+sign-in+failed", status_code=302)

    userinfo = token.get("userinfo")
    if userinfo is None:
        return RedirectResponse(url="/login?error=Could+not+read+Google+profile", status_code=302)

    user, created = await db.get_or_create_user(
        google_sub=userinfo["sub"],
        email=userinfo.get("email", ""),
        name=userinfo.get("name") or userinfo.get("email", "User"),
        picture=userinfo.get("picture"),
    )
    if created:
        await seed_sample_run(user.id)
    guest = await get_guest(request)
    if guest is not None:
        await claim_guest_runs(guest.id, user.id)
        request.session.pop("guest_user_id", None)
    request.session["user_id"] = user.id
    return RedirectResponse(url=after_login_path(request.session.pop("after_login", "/")), status_code=302)


def after_login_path(path: str) -> str:
    """Where to go once signed in — an internal path only. A session value
    shouldn't be trusted to be one, and "//evil.example" is a URL."""
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
