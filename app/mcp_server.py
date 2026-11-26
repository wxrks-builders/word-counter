"""The Word Counter as an MCP server: Claude, ChatGPT, Cursor and any other
assistant that speaks the Model Context Protocol can count the words on a
website, follow the count while it runs, and read the result.

One endpoint, POST /mcp (or /mcp/<key>), JSON-RPC in and plain JSON out — no
sessions and no event stream, since nothing here needs the server to call the
client. Both generations of the protocol are answered:

- up to 2025-11-25, a client opens with `initialize` and its requests carry no
  metadata;
- from 2026-07-28 there is no handshake: every request carries its protocol
  version in `_meta`, and `server/discover` says what the server is.

Listing the tools needs no key, so an app can be connected before anyone signs
in; counting does. The tools reuse the web app's own crawl (main.py passes its
functions in as a `Backend`), so a count started here is a run like any other:
it shows in Your runs, has a report page and a CSV, and follows the same rules —
including the pause at an estimate on large sites, which the assistant can
continue.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from app import db, report, surfaces
from app.job_store import JOBS, QUEUE, get_job
from app.models import CrawlRequest, User

SERVER_NAME = "word-counter"
SERVER_TITLE = "Word Counter by wxrks"
SERVER_VERSION = "1.0.0"
MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED = (MODERN, *LEGACY)
# A tool waits this long for a count, at most, before handing back progress.
# Assistants give a tool call about a minute.
MAX_WAIT = 55
FINISHED = ("completed", "failed", "cancelled", "paused")
# The tool's names for the web form's "Which pages count as part of this site?"
SCOPES = {"whole_domain": "all", "this_subdomain": "subdomain_only", "top_level_domain": "top_domain_only"}
_SCOPE_NAMES = {value: key for key, value in SCOPES.items()}
RUNNING = ("starting", "queued", "crawling", "resuming")

INSTRUCTIONS = (
    "Word Counter crawls every page of a website and counts the words a visitor reads — main text, "
    "not menus, footers or forms; duplicate pages once — with an estimate of how much of it repeats. "
    "Use count_words with the site's address. Small sites finish within the call; for larger ones the "
    "result says the count is still running: call get_word_count with the run_id until it's done. "
    "Very large sites pause at about 50,000 words with an estimate of the total; tell the user the "
    "estimate and call continue_word_count only if they want everything counted. Always give the "
    "report link, so the user can see every page."
)


@dataclass
class Backend:
    """What the tools need from the web app, handed in by main.py."""
    begin_crawl: Callable[[CrawlRequest, User], Awaitable[tuple[dict, int]]]
    resume: Callable[[object], None]
    site_url: Callable[[str], str]


class ToolError(Exception):
    """A problem to explain to the assistant, returned as an error result."""


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


_WAIT = {"type": "integer", "minimum": 0, "maximum": MAX_WAIT,
         "description": f"Seconds to wait for the count to finish before returning progress (0–{MAX_WAIT})."}
_RUN_ID = {"type": "string", "description": "The run_id a previous call returned."}

TOOLS = [
    {
        "name": "count_words",
        "title": "Count the words on a website",
        "description": (
            "Crawls every page of a website and counts its words: the text a visitor reads, without menus, "
            "footers or forms, each duplicate page once. Returns the total, pages, an estimate of repeated "
            "text, and the split by language, folder and subdomain, with a link to the full report. Reuses "
            "a finished count of the same site unless fresh is true. If the count isn't done within "
            "wait_seconds, returns its progress and a run_id for get_word_count."
        ),
        "inputSchema": _schema({
            "url": {"type": "string", "description": "The website, e.g. https://example.com or example.com."},
            "fresh": {"type": "boolean", "default": False,
                      "description": "Count again even if this site was counted before."},
            "scope": {"type": "string", "enum": list(SCOPES), "default": "whole_domain",
                      "description": "whole_domain: the site and all its subdomains (default). this_subdomain: only "
                                     "the host given, e.g. www.example.com. top_level_domain: example.com without "
                                     "its subdomains."},
            "wait_seconds": {**_WAIT, "default": 40},
        }, ["url"]),
        "annotations": {"title": "Count the words on a website", "readOnlyHint": False,
                        "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "get_word_count",
        "title": "Get a word count",
        "description": (
            "The state of a count started with count_words: its progress while it runs, the estimate if it "
            "paused, or the full result once it's finished."
        ),
        "inputSchema": _schema({"run_id": _RUN_ID, "wait_seconds": {**_WAIT, "default": 0}}, ["run_id"]),
        "annotations": {"title": "Get a word count", "readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "continue_word_count",
        "title": "Count the rest of a large site",
        "description": (
            "A large site pauses at about 50,000 words with an estimate of its total. This carries the "
            "count on to the last page. Only call it when the user wants the full count."
        ),
        "inputSchema": _schema({"run_id": _RUN_ID}, ["run_id"]),
        "annotations": {"title": "Count the rest of a large site", "readOnlyHint": False,
                        "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "list_word_counts",
        "title": "List my word counts",
        "description": "The websites you've counted, most recent first, with their totals and run_ids.",
        "inputSchema": _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}),
        "annotations": {"title": "List my word counts", "readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "get_pages",
        "title": "List the pages of a count",
        "description": (
            "The pages of a count with their words, largest first — optionally only those whose address "
            "contains a given text, such as /blog or /de/."
        ),
        "inputSchema": _schema({
            "run_id": _RUN_ID,
            "contains": {"type": "string", "description": "Only pages whose address contains this text."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
        }, ["run_id"]),
        "annotations": {"title": "List the pages of a count", "readOnlyHint": True, "openWorldHint": False},
    },
]
_TOOL_NAMES = {tool["name"] for tool in TOOLS}

# What count_words says when the crawl can't start, by the web app's codes.
_START_ERRORS = {
    "enter_valid_url": "That isn't a website address. Give one like https://example.com.",
    "site_password_needed": "This site asks for a username and password (HTTP authentication). "
                            "Start that count on the website, where you can enter them.",
    "site_password_rejected": "The site rejected the username and password.",
    "project_not_found": "That project doesn't exist.",
}


# ---------------------------------------------------------------- the protocol

class RpcError(Exception):
    def __init__(self, code: int, message: str, data=None, http_status: int = 400):
        super().__init__(message)
        self.code, self.message, self.data, self.http_status = code, message, data, http_status


@dataclass
class Reply:
    body: dict | None          # None: a notification, answered with 202 and no body
    status: int = 200
    version: str | None = None


def _meta_version(message: dict) -> str | None:
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    return meta.get("io.modelcontextprotocol/protocolVersion") if isinstance(meta, dict) else None


async def handle(message, *, user: User | None, backend: Backend, header_version: str | None = None) -> Reply:
    """One JSON-RPC message in, its reply out."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        if isinstance(message, dict) and "method" not in message and ("result" in message or "error" in message):
            return Reply(None, 202)    # a response to a request we never send; nothing to do
        return Reply(_error(None, -32600, "Invalid request: expected one JSON-RPC 2.0 request."), 400)
    method = message["method"]
    msg_id = message.get("id")
    if "id" not in message:            # a notification (initialized, cancelled…)
        return Reply(None, 202)

    meta_version = _meta_version(message)
    version = meta_version or header_version
    modern = version == MODERN
    try:
        if meta_version is not None and meta_version not in SUPPORTED and method != "initialize":
            raise RpcError(-32022, "Unsupported protocol version",
                           {"supported": list(SUPPORTED), "requested": meta_version})
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        result = await _dispatch(method, params, user, backend, version)
    except RpcError as exc:
        status = exc.http_status if modern else (400 if exc.code == -32022 else 200)
        return Reply(_error(msg_id, exc.code, exc.message, exc.data), status, version)
    result.setdefault("resultType", "complete")
    return Reply({"jsonrpc": "2.0", "id": msg_id, "result": result}, 200, version)


def _error(msg_id, code: int, message: str, data=None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def _server_info() -> dict:
    return {"name": SERVER_NAME, "title": SERVER_TITLE, "version": SERVER_VERSION,
            "websiteUrl": surfaces.COUNTER.url("/mcp")}


async def _dispatch(method: str, params: dict, user: User | None, backend: Backend, version: str | None) -> dict:
    if method == "initialize":
        asked = params.get("protocolVersion")
        agreed = asked if asked in LEGACY else LEGACY[0]
        return {"protocolVersion": agreed, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": _server_info(), "instructions": INSTRUCTIONS}
    if method == "server/discover":
        return {"supportedVersions": list(SUPPORTED), "capabilities": {"tools": {}},
                "_meta": {"io.modelcontextprotocol/serverInfo": _server_info()},
                "instructions": INSTRUCTIONS, "ttlMs": 3_600_000, "cacheScope": "public"}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS, "ttlMs": 3_600_000, "cacheScope": "public"}
    if method in ("resources/list", "prompts/list", "resources/templates/list"):
        key = {"resources/list": "resources", "prompts/list": "prompts",
               "resources/templates/list": "resourceTemplates"}[method]
        return {key: []}
    if method == "tools/call":
        name = params.get("name")
        if name not in _TOOL_NAMES:
            raise RpcError(-32602, f"Unknown tool: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise RpcError(-32602, "Tool arguments must be an object.")
        return await call_tool(name, arguments, user, backend)
    raise RpcError(-32601, f"Method not found: {method}", http_status=404)


# ---------------------------------------------------------------- the tools

def _result(text: str, data: dict | None = None, error: bool = False) -> dict:
    result = {"content": [{"type": "text", "text": text}], "isError": error}
    if data is not None:
        result["structuredContent"] = data
    return result


async def call_tool(name: str, arguments: dict, user: User | None, backend: Backend) -> dict:
    if user is None:
        return _result(
            "This Word Counter connection has no key, so it can't count yet. Get a personal connection URL "
            f"(free account) at {backend.site_url('/mcp/connect')} and add it to your app in place of this "
            "one — or, in Claude Code, pass the key as an Authorization: Bearer header.",
            {"error": "no_key", "connect_url": backend.site_url("/mcp/connect")}, error=True)
    try:
        if name == "count_words":
            return await _count_words(arguments, user, backend)
        if name == "get_word_count":
            run_id = _run_id(arguments)
            await _wait(run_id, _seconds(arguments, 0))
            return await _describe(run_id, user, backend)
        if name == "continue_word_count":
            return await _continue(_run_id(arguments), user, backend)
        if name == "list_word_counts":
            return await _list(arguments, user, backend)
        return await _pages(arguments, user, backend)
    except ToolError as exc:
        return _result(str(exc), {"error": str(exc)}, error=True)


def _run_id(arguments: dict) -> str:
    run_id = arguments.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ToolError("Give the run_id that count_words returned.")
    return run_id.strip()


def _seconds(arguments: dict, default: int) -> int:
    try:
        value = int(arguments.get("wait_seconds", default))
    except (TypeError, ValueError):
        value = default
    return max(0, min(MAX_WAIT, value))


async def _count_words(arguments: dict, user: User, backend: Backend) -> dict:
    url = str(arguments.get("url") or "").strip()
    if not url:
        raise ToolError("Give the website to count, e.g. https://example.com.")
    if "://" not in url:
        url = "https://" + url
    fresh = bool(arguments.get("fresh", False))
    scope_name = arguments.get("scope") or "whole_domain"
    if scope_name not in SCOPES:
        raise ToolError("scope must be whole_domain, this_subdomain or top_level_domain.")
    scope = SCOPES[scope_name]
    if not fresh:
        live = _own_live_job(user, url, scope)
        if live is not None:
            await _wait(live.id, _seconds(arguments, 40))
            return await _describe(live.id, user, backend)
    result, status = await backend.begin_crawl(CrawlRequest(url=url, force_recrawl=fresh, domain_scope=scope), user)
    if status != 200:
        code = result.get("code", "")
        raise ToolError(_START_ERRORS.get(code) or result.get("detail") or f"The count couldn't start ({code}).")
    run_id = result["run_id"]
    if result.get("cached"):
        run = await db.get_run(run_id)
        if run is None or not (run.user_id == user.id or run.is_public) or run.domain_scope != scope:
            # Someone else's private count of the same site — never shown
            # here — or one of a different part of it: count afresh.
            result, status = await backend.begin_crawl(CrawlRequest(url=url, force_recrawl=True, domain_scope=scope), user)
            if status != 200:
                raise ToolError(_START_ERRORS.get(result.get("code", "")) or "The count couldn't start.")
            run_id = result["run_id"]
        else:
            described = await _describe(run_id, user, backend)
            described["content"][0]["text"] = (
                f"From an earlier count ({run.created_at[:10]}); call count_words with fresh=true to count again.\n\n"
                + described["content"][0]["text"])
            described["structuredContent"]["reused"] = True
            described["structuredContent"]["counted_at"] = run.created_at
            described["structuredContent"]["note"] = "From an earlier count; pass fresh=true to count again."
            return described
    await _wait(run_id, _seconds(arguments, 40))
    return await _describe(run_id, user, backend)


def _settled(job) -> bool:
    """Stopped, and done stopping: the status flips before the crawl has
    worked out its estimate and saved the run, so the task has to be over too."""
    return job.status in FINISHED and (job.task is None or job.task.done())


async def _wait(run_id: str, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while True:
        job = get_job(run_id)
        if job is None or _settled(job):
            return
        if time.monotonic() >= deadline:
            # Out of time — but a crawl that has just stopped only needs a
            # moment more, so its result isn't reported half-made.
            if job.status in FINISHED:
                for _ in range(20):
                    await asyncio.sleep(0.25)
                    if _settled(job):
                        break
            return
        await asyncio.sleep(0.5)


def _own_live_job(user: User, url: str, scope: str):
    """A count of this site the user already has running, or paused at its
    estimate — asked about twice, a site is counted once."""
    key = db.normalize_url(url)
    for job in list(JOBS.values()):
        if (job.user_id == user.id and job.source_url == key and job.domain_scope == scope
                and (job.status in RUNNING or job.status == "paused")):
            return job
    return None


class _View:
    """A count as the tools read it: the live job while it's in memory — its
    numbers are final the moment it stops, before the run row is saved — and
    the saved run otherwise."""

    def __init__(self, run_id: str, job=None, run=None):
        self.run_id = run_id
        source = job if job is not None else run
        self.status = source.status
        self.source_url = job.source_url if job is not None else run.shown_url
        self.pages = list(job.pages.values()) if job is not None else run.pages
        self.total_words = source.total_words
        self.duplicates = job.duplicate_count if job is not None else run.duplicate_count
        self.login_blocked = (len(job.login_blocked) + job.restored_login_blocked_count) if job is not None \
            else run.login_blocked_count
        self.repetition = (job.repetition.summary(job.repetition_partial) if job is not None else run.repetition) or {}
        self.linked_hosts = (job.linked_hosts if job is not None else run.linked_hosts) or {}
        self.linked_languages = (job.linked_languages if job is not None else run.linked_languages) or {}
        self.other_languages = (job.other_languages if job is not None else run.other_languages) or {}
        self.estimate = job.estimate_result if job is not None else None
        self.error = source.error
        self.stopped_reason = job.stopped_reason if job is not None else run.stopped_reason
        self.queue_position = (QUEUE.index(run_id) + 1) if run_id in QUEUE else None
        self.created_at = job.started_at if job is not None else run.created_at
        self.domain_scope = (job.domain_scope if job is not None else run.domain_scope) or "all"


async def _view(run_id: str, user: User) -> _View:
    job = get_job(run_id)
    if job is not None:
        if job.user_id != user.id:
            run = await db.get_run(run_id)
            if run is None or not run.is_public:
                raise ToolError("No count with that run_id.")
            return _View(run_id, run=run)
        return _View(run_id, job=job)
    run = await db.get_run(run_id)
    if run is None or not (run.user_id == user.id or run.is_public):
        raise ToolError("No count with that run_id.")
    return _View(run_id, run=run)


def _n(value) -> str:
    return f"{int(value or 0):,}"


def _site(url: str) -> str:
    return url.replace("https://", "").replace("http://", "").rstrip("/")


async def _describe(run_id: str, user: User, backend: Backend) -> dict:
    view = await _view(run_id, user)
    counted = [p for p in view.pages if p.success]
    links = {"report": backend.site_url(f"/crawl/{run_id}"), "csv": backend.site_url(f"/crawl/{run_id}/export.csv")}
    data = {"run_id": run_id, "site": view.source_url, "status": view.status, "words": view.total_words,
            "pages": len(counted), "scope": _SCOPE_NAMES.get(view.domain_scope, "whole_domain"), "links": links}

    if view.status in RUNNING:
        data["queue_position"] = view.queue_position
        if view.status == "queued":
            text = (f"{_site(view.source_url)}: waiting for a free slot"
                    f"{f' (number {view.queue_position} in line)' if view.queue_position else ''}.")
        else:
            text = (f"{_site(view.source_url)}: still counting — {_n(view.total_words)} words on "
                    f"{_n(len(counted))} pages so far.")
        data["next_step"] = (f"Call get_word_count with run_id {run_id} and wait_seconds up to {MAX_WAIT} "
                             "until status is completed.")
        text += f"\n{data['next_step']} Live progress: {links['report']}"
        return _result(text, data)

    summary = report.summarize(view.pages)
    left_out = report.left_out(view.pages, view.linked_hosts, view.linked_languages, view.other_languages)
    repetition = view.repetition
    words_total = repetition.get("words_total") or 0
    if words_total:
        repeated = repetition.get("words_repeated", 0)
        near = repetition.get("words_near", 0)
        data["repetition"] = {
            "words_checked": words_total, "repeated": repeated, "near_repeated": near,
            "new": max(0, words_total - repeated - near),
            "repeated_percent": round(100 * repeated / words_total, 1),
            "most_repeated": [{"text": t["text"], "times": t["count"], "words": t["words"]}
                              for t in (repetition.get("top") or [])[:5]],
        }
    data["languages"] = summary["languages"][:10]
    data["folders"] = summary["folders"][:10]
    data["subdomains"] = summary["hosts"][:10]
    data["largest_pages"] = summary["top_pages"][:10]
    data["left_out"] = {
        "duplicate_pages": view.duplicates,
        "pages_behind_sign_in": view.login_blocked,
        "blocked_or_failed_pages": summary["blocked_count"] + summary["failed_count"],
        "linked_subdomains_not_counted": [h["name"] for h in left_out["skipped_hosts"][:10]],
        "linked_languages_not_counted": [lang["name"] for lang in left_out["skipped_languages"][:10]],
    }

    site = _site(view.source_url)
    if view.status == "completed":
        lines = [f"{site}: {_n(view.total_words)} words on {_n(len(counted))} pages — every page counted."]
    elif view.status == "paused":
        estimate = view.estimate or {}
        data["estimate"] = {k: estimate.get(k) for k in (
            "estimated_total_words", "total_pages_estimate", "confidence", "estimated_duration_seconds",
            "sitemap_count")} if estimate else None
        lines = [f"{site}: a large site. Paused at {_n(view.total_words)} words on {_n(len(counted))} pages."]
        if estimate.get("estimated_total_words"):
            minutes = round((estimate.get("estimated_duration_seconds") or 0) / 60)
            lines.append(
                f"Estimated total: about {_n(estimate['estimated_total_words'])} words on about "
                f"{_n(estimate.get('total_pages_estimate'))} pages ({estimate.get('confidence', 'low')} confidence)."
                + (f" Counting everything would take about {minutes} minutes." if minutes else ""))
        data["next_step"] = (f"The total above is an estimate. If the user wants every page counted, call "
                             f"continue_word_count with run_id {run_id}, then get_word_count until it completes.")
        lines.append(f"To count every page, call continue_word_count with run_id {run_id}.")
    else:
        why = {"cancelled": "stopped before the end", "failed": "failed"}[view.status]
        lines = [f"{site}: the count {why} after {_n(view.total_words)} words on {_n(len(counted))} pages."]
        if view.stopped_reason or view.error:
            lines.append(f"Reason: {view.stopped_reason or view.error}")

    if words_total:
        rep = data["repetition"]
        lines.append(
            f"Repetition (estimate, by sentence): {_n(rep['repeated'])} words ({rep['repeated_percent']}%) repeat "
            f"text seen on another page; {_n(rep['new'])} are new.")
    if len(summary["languages"]) > 1:
        lines.append("Languages: " + "; ".join(
            f"{lang['name']} {_n(lang['words'])} words ({_n(lang['pages'])} pages)" for lang in summary["languages"][:6]))
    if summary["folders"]:
        lines.append("Largest sections: " + "; ".join(
            f"{f['name']} {_n(f['words'])} ({_n(f['pages'])} pages)" for f in summary["folders"][:6]))
    if len(summary["hosts"]) > 1:
        lines.append("Subdomains: " + "; ".join(
            f"{h['name']} {_n(h['words'])}" for h in summary["hosts"][:6]))
    left = data["left_out"]
    not_counted = ["menus, footers and forms"]
    if left["duplicate_pages"]:
        not_counted.append(f"{_n(left['duplicate_pages'])} duplicate pages")
    if left["pages_behind_sign_in"]:
        not_counted.append(f"{_n(left['pages_behind_sign_in'])} pages behind a sign-in")
    if left["blocked_or_failed_pages"]:
        not_counted.append(f"{_n(left['blocked_or_failed_pages'])} pages that failed or were blocked")
    lines.append("Not counted: " + ", ".join(not_counted) + ".")
    if left["linked_subdomains_not_counted"]:
        lines.append("Linked subdomains not crawled: " + ", ".join(left["linked_subdomains_not_counted"][:6]) + ".")
    lines.append(f"Full report (every page): {links['report']} · CSV: {links['csv']}")
    lines.append(f"run_id: {run_id}")
    return _result("\n".join(lines), data)


async def _continue(run_id: str, user: User, backend: Backend) -> dict:
    job = get_job(run_id)
    if job is None or job.user_id != user.id:
        run = await db.get_run(run_id)
        if run is None or run.user_id != user.id:
            raise ToolError("No count with that run_id.")
        raise ToolError("This count can't be continued any more. Start a new one with count_words and fresh=true.")
    if job.status != "paused":
        return await _describe(run_id, user, backend)
    if (job.password_protected and job.site_auth is None) or (job.signed_in and job.site_session is None):
        raise ToolError("This count needs the site's password or sign-in again — continue it on the website: "
                        + backend.site_url(f"/crawl/{run_id}"))
    backend.resume(job)
    estimate = job.estimate_result or {}
    minutes = round((estimate.get("estimated_duration_seconds") or 0) / 60)
    next_step = f"Call get_word_count with run_id {run_id} and wait_seconds {MAX_WAIT} until status is completed."
    text = (f"Counting the rest of {_site(job.source_url)}"
            + (f" — about {minutes} minutes" if minutes else "")
            + f". {next_step} Progress: {backend.site_url(f'/crawl/{run_id}')}")
    return _result(text, {"run_id": run_id, "status": "resuming", "estimated_minutes": minutes or None,
                          "next_step": next_step})


async def _list(arguments: dict, user: User, backend: Backend) -> dict:
    try:
        limit = max(1, min(50, int(arguments.get("limit", 10))))
    except (TypeError, ValueError):
        limit = 10
    runs = await db.list_user_runs(user.id, limit=limit)
    if not runs:
        return _result("No word counts yet. Use count_words with a website's address.", {"counts": []})
    rows = [{"run_id": r["id"], "site": r["shown_url"], "status": r["status"], "words": r["total_words"],
             "pages": r["page_count"], "counted_at": r["created_at"], "report": backend.site_url(f"/crawl/{r['id']}")}
            for r in runs]
    text = "\n".join(f"{_site(r['site'])} — {_n(r['words'])} words, {_n(r['pages'])} pages, {r['status']}, "
                     f"{r['counted_at'][:10]} (run_id {r['run_id']})" for r in rows)
    return _result(text, {"counts": rows})


async def _pages(arguments: dict, user: User, backend: Backend) -> dict:
    view = await _view(_run_id(arguments), user)
    contains = str(arguments.get("contains") or "").strip()
    try:
        limit = max(1, min(200, int(arguments.get("limit", 25))))
    except (TypeError, ValueError):
        limit = 25
    pages = [p for p in view.pages if p.success and (not contains or contains in p.url)]
    pages.sort(key=lambda p: -p.word_count)
    rows = [{"url": p.url, "title": p.title or "", "words": p.word_count, "repeated_words": p.repeated_words}
            for p in pages[:limit]]
    head = (f"{_n(len(pages))} pages{f' containing {contains!r}' if contains else ''}, "
            f"{_n(sum(p.word_count for p in pages))} words; the largest {len(rows)}:")
    text = head + "\n" + "\n".join(f"{_n(r['words'])} words — {r['url']}" for r in rows)
    return _result(text, {"run_id": view.run_id, "matching_pages": len(pages), "pages": rows})
