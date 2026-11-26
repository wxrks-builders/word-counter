from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiosqlite

from app import markdown_store, repetition, segment_store
from app.models import PageResult, RunRecord, User

DB_PATH = Path(os.environ.get("DB_PATH", "data/wordcount.db"))

_connection: aiosqlite.Connection | None = None


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[: -len(":80")]
    if scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[: -len(":443")]
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


async def init_db() -> None:
    global _connection
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _connection = await aiosqlite.connect(DB_PATH)
    _connection.row_factory = aiosqlite.Row
    await _connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            google_sub TEXT UNIQUE NOT NULL,
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            picture TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            total_words INTEGER NOT NULL,
            page_count INTEGER NOT NULL,
            limit_reached INTEGER NOT NULL,
            pages_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_runs_source_url ON runs(source_url);
        CREATE INDEX IF NOT EXISTS idx_runs_user_id ON runs(user_id);

        CREATE TABLE IF NOT EXISTS server_alerts (
            kind TEXT PRIMARY KEY,
            last_sent_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS run_shares (
            run_id TEXT NOT NULL,
            email TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_id, email)
        );

        -- Projects: a user's runs grouped by client, with each page counted
        -- once across them — see app/projects.py. A run can be in several.
        -- cached_* are the totals as last computed, so listing projects never
        -- parses pages_json.
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            is_public INTEGER NOT NULL DEFAULT 0,
            cached_words INTEGER,
            cached_pages INTEGER,
            cached_overlap_words INTEGER,
            cached_runs INTEGER,
            cached_at TEXT,
            cached_new_words INTEGER,
            cached_repeated_words INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_projects_user_id ON projects(user_id);

        -- Personal keys for the MCP server (app/mcp_server.py): what lets Claude
        -- or ChatGPT count words as a user. Only a hash of each key is kept;
        -- the key itself is shown once, when it's made.
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            hint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            revoked_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_api_keys_user_id ON api_keys(user_id);

        -- A project's consolidation (app/consolidation_job.py): one row per
        -- project, a background job's state and its last result. A request
        -- bumps requested_gen; a job remembers the generation it claimed, so a
        -- request made while it runs queues it again when it finishes.
        CREATE TABLE IF NOT EXISTS project_consolidations (
            project_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            requested_gen INTEGER NOT NULL DEFAULT 1,
            claimed_gen INTEGER,
            computed_gen INTEGER,
            reason TEXT,
            owner TEXT,
            heartbeat_at TEXT,
            resume_count INTEGER NOT NULL DEFAULT 0,
            progress_json TEXT,
            wanted_inputs TEXT,
            inputs TEXT,
            result_json TEXT,
            requested_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_project_consolidations_status ON project_consolidations(status);

        -- No foreign keys: a crawl can join a project before its run row is
        -- first written, at the first checkpoint.
        CREATE TABLE IF NOT EXISTS project_runs (
            project_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            added_at TEXT NOT NULL,
            count_unfinished INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (project_id, run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_project_runs_run_id ON project_runs(run_id);

        CREATE TABLE IF NOT EXISTS estimate_history (
            run_id TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            created_at TEXT NOT NULL,
            pages_fetched INTEGER NOT NULL,
            discovered_total INTEGER NOT NULL,
            sitemap_count INTEGER,
            sitemap_found INTEGER NOT NULL,
            detected_cms TEXT,
            confidence TEXT NOT NULL,
            estimated_total_pages INTEGER NOT NULL,
            estimated_total_words INTEGER NOT NULL,
            actual_total_pages INTEGER,
            actual_total_words INTEGER,
            completed_at TEXT,
            elapsed_seconds INTEGER,
            words_per_minute INTEGER,
            pages_per_minute REAL,
            estimated_duration_seconds INTEGER,
            concurrent_crawls INTEGER
        );

        -- Translation check (app/translation_check.py). A check, one row per
        -- original page and per language version, and the findings grouped
        -- once the check is done.
        CREATE TABLE IF NOT EXISTS translation_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_url TEXT NOT NULL,
            status TEXT NOT NULL,
            trigger TEXT NOT NULL,
            rules_version INTEGER NOT NULL,
            locales_json TEXT,
            owner TEXT,
            heartbeat_at TEXT,
            resume_count INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            summary_json TEXT,
            diff_json TEXT,
            emailed_at TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_translation_checks_site ON translation_checks(site_url, id);

        -- locale '' is the original page itself.
        CREATE TABLE IF NOT EXISTS translation_pages (
            check_id INTEGER NOT NULL,
            source_url TEXT NOT NULL,
            locale TEXT NOT NULL,
            url TEXT,
            status TEXT NOT NULL,
            http_status INTEGER,
            source_words INTEGER NOT NULL DEFAULT 0,
            untranslated_words INTEGER NOT NULL DEFAULT 0,
            findings_json TEXT,
            PRIMARY KEY (check_id, source_url, locale)
        );
        CREATE INDEX IF NOT EXISTS idx_translation_pages_status ON translation_pages(check_id, status);

        CREATE TABLE IF NOT EXISTS translation_findings (
            check_id INTEGER NOT NULL,
            locale TEXT NOT NULL,
            kind TEXT NOT NULL,
            text_key TEXT NOT NULL,
            text TEXT NOT NULL,
            detail TEXT,
            words INTEGER NOT NULL DEFAULT 0,
            page_count INTEGER NOT NULL DEFAULT 0,
            where_hint TEXT,
            component_id TEXT,
            component_label TEXT,
            sample_urls_json TEXT,
            source_url TEXT,
            is_new INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (check_id, locale, kind, text_key)
        );
        CREATE INDEX IF NOT EXISTS idx_translation_findings_kind ON translation_findings(check_id, kind);

        -- Screenshots of key pages, one per language version.
        CREATE TABLE IF NOT EXISTS translation_shots (
            check_id INTEGER NOT NULL,
            source_url TEXT NOT NULL,
            locale TEXT NOT NULL,
            url TEXT NOT NULL,
            status TEXT NOT NULL,
            file TEXT,
            marks INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            taken_at TEXT,
            PRIMARY KEY (check_id, source_url, locale)
        );

        -- "Keep as is": per language ('*' = every language), since "Marketing"
        -- is right in French and a gap in Portuguese. A whole page kept in the
        -- original is keyed 'page:' + its original URL.
        CREATE TABLE IF NOT EXISTS translation_ignores (
            site_url TEXT NOT NULL,
            locale TEXT NOT NULL,
            text_key TEXT NOT NULL,
            text TEXT NOT NULL,
            created_by TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (site_url, locale, text_key)
        );
        """
    )
    await _connection.commit()
    await _ensure_columns()


async def _ensure_columns() -> None:
    """Lightweight migration so existing local databases pick up new columns
    without wiping previously-saved runs."""
    conn = _conn()
    cur = await conn.execute("PRAGMA table_info(runs)")
    existing = {row["name"] for row in await cur.fetchall()}
    if "login_blocked_count" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN login_blocked_count INTEGER NOT NULL DEFAULT 0")
        await conn.commit()
    if "domain_scope" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN domain_scope TEXT NOT NULL DEFAULT 'all'")
        await conn.commit()
    if "language" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN language TEXT")
        await conn.commit()
    if "language_auto_detected" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN language_auto_detected INTEGER NOT NULL DEFAULT 0")
        await conn.commit()
    if "resume_state_json" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN resume_state_json TEXT")
        await conn.commit()
    if "cancel_requested" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
        await conn.commit()
    if "is_public" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN is_public INTEGER NOT NULL DEFAULT 0")
        await conn.commit()
    if "is_sample" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN is_sample INTEGER NOT NULL DEFAULT 0")
        await conn.commit()
    for column, ddl in [
        # Whether this run was asked to save page Markdown, persisted so a
        # crash-resumed crawl carries on capturing instead of silently stopping.
        ("capture_markdown", "ALTER TABLE runs ADD COLUMN capture_markdown INTEGER NOT NULL DEFAULT 0"),
        ("markdown_pages", "ALTER TABLE runs ADD COLUMN markdown_pages INTEGER NOT NULL DEFAULT 0"),
        ("markdown_bytes", "ALTER TABLE runs ADD COLUMN markdown_bytes INTEGER NOT NULL DEFAULT 0"),
        ("markdown_state", "ALTER TABLE runs ADD COLUMN markdown_state TEXT NOT NULL DEFAULT 'off'"),
        # Which front door this run was started from, so the finished-crawl
        # email links back to the same brand. That email is sent from a
        # background task, which has no request to read a Host header from.
        ("surface", "ALTER TABLE runs ADD COLUMN surface TEXT NOT NULL DEFAULT 'counter'"),
        # Pages fetched and then found to be a copy of one already counted, and
        # the subdomains/folders this run was told to leave out — see
        # app/url_policy.py.
        ("duplicate_count", "ALTER TABLE runs ADD COLUMN duplicate_count INTEGER NOT NULL DEFAULT 0"),
        ("exclusions", "ALTER TABLE runs ADD COLUMN exclusions TEXT"),
        # How many pages this run crawled at once — see app/plans.py. Persisted
        # so a resumed crawl keeps the speed it was started with, rather than
        # silently dropping to whatever the resuming caller resolves.
        ("crawl_concurrency", "ALTER TABLE runs ADD COLUMN crawl_concurrency INTEGER NOT NULL DEFAULT 0"),
        # Wall-clock seconds this run took. created_at alone can't answer "how
        # long did this take", and app/promos.py needs the real number before it
        # will claim anything about it. Runs saved before this default to 0,
        # which reads as "unknown" rather than "instant".
        ("duration_seconds", "ALTER TABLE runs ADD COLUMN duration_seconds INTEGER NOT NULL DEFAULT 0"),
        # Why the run ended. Until these existed, a memory kill, a stall and a
        # user clicking Cancel all persisted as status='cancelled' and nothing
        # else, so "are crawls dying because too many run at once" had no
        # answer anywhere on the server. NULL on older runs means "unknown",
        # which the health page says rather than guessing.
        ("stop_kind", "ALTER TABLE runs ADD COLUMN stop_kind TEXT"),
        ("stopped_reason", "ALTER TABLE runs ADD COLUMN stopped_reason TEXT"),
        ("error", "ALTER TABLE runs ADD COLUMN error TEXT"),
        # Which CMS the crawler recognised while fetching. It was only ever
        # stored on estimate_history, which gets a row only when a crawl pauses
        # for an estimate — so a run that finished without pausing had no
        # platform recorded anywhere. app/promos.py needs it on every run.
        ("detected_cms", "ALTER TABLE runs ADD COLUMN detected_cms TEXT"),
        # Whether the site was behind a Basic Auth password. The password itself
        # is never stored — see app/site_auth.py.
        ("password_protected", "ALTER TABLE runs ADD COLUMN password_protected INTEGER NOT NULL DEFAULT 0"),
        # Crawled signed in on the site's sign-in page, and which page that was.
        # The email, password and session are never stored — see app/site_login.py.
        ("signed_in", "ALTER TABLE runs ADD COLUMN signed_in INTEGER NOT NULL DEFAULT 0"),
        ("login_url", "ALTER TABLE runs ADD COLUMN login_url TEXT"),
        # Featured on the front page as an example report — set from the admin
        # runs list, only ever alongside a live public link.
        ("is_example", "ALTER TABLE runs ADD COLUMN is_example INTEGER NOT NULL DEFAULT 0"),
        # What the crawled pages link to, counted or not — see report.LinkCensus.
        ("linked_hosts_json", "ALTER TABLE runs ADD COLUMN linked_hosts_json TEXT"),
        ("linked_languages_json", "ALTER TABLE runs ADD COLUMN linked_languages_json TEXT"),
        ("other_languages_json", "ALTER TABLE runs ADD COLUMN other_languages_json TEXT"),
        # The repetition estimate's summary — see app/repetition.py.
        ("repetition_json", "ALTER TABLE runs ADD COLUMN repetition_json TEXT"),
        # The address the owner wants the report to show — the site's home page,
        # say, when the crawl had to start from an inner page. The crawl, and any
        # re-crawl, still use source_url.
        ("display_url", "ALTER TABLE runs ADD COLUMN display_url TEXT"),
    ]:
        if column not in existing:
            await conn.execute(ddl)
            await conn.commit()

    # The users table has never been migrated before — everything above is the
    # runs table. Billing state lives here because it belongs to the account,
    # not to any one crawl.
    cur = await conn.execute("PRAGMA table_info(users)")
    existing_user_cols = {row["name"] for row in await cur.fetchall()}
    for column, ddl in [
        ("plan", "ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'"),
        ("plan_status", "ALTER TABLE users ADD COLUMN plan_status TEXT"),
        ("stripe_customer_id", "ALTER TABLE users ADD COLUMN stripe_customer_id TEXT"),
        ("stripe_subscription_id", "ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT"),
        ("plan_renews_at", "ALTER TABLE users ADD COLUMN plan_renews_at TEXT"),
        # Which language to write to this person in. Emails are sent from
        # background tasks with no request to read a prefix from, so the last
        # one they browsed under is remembered here.
        ("lang", "ALTER TABLE users ADD COLUMN lang TEXT NOT NULL DEFAULT 'en'"),
    ]:
        if column not in existing_user_cols:
            await conn.execute(ddl)
            await conn.commit()
    if "stripe_customer_id" not in existing_user_cols:
        # A Stripe webhook arrives knowing only the customer id, so that lookup
        # has to be as cheap as the one by primary key.
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_stripe_customer ON users(stripe_customer_id)"
        )
        await conn.commit()

    cur = await conn.execute("PRAGMA table_info(estimate_history)")
    existing_estimate_cols = {row["name"] for row in await cur.fetchall()}
    for column, ddl in [
        ("elapsed_seconds", "ALTER TABLE estimate_history ADD COLUMN elapsed_seconds INTEGER"),
        ("words_per_minute", "ALTER TABLE estimate_history ADD COLUMN words_per_minute INTEGER"),
        ("pages_per_minute", "ALTER TABLE estimate_history ADD COLUMN pages_per_minute REAL"),
        ("estimated_duration_seconds", "ALTER TABLE estimate_history ADD COLUMN estimated_duration_seconds INTEGER"),
        ("concurrent_crawls", "ALTER TABLE estimate_history ADD COLUMN concurrent_crawls INTEGER"),
    ]:
        if column not in existing_estimate_cols:
            await conn.execute(ddl)
            await conn.commit()

    # A project's totals as its last consolidation found them.
    cur = await conn.execute("PRAGMA table_info(projects)")
    existing_project_cols = {row["name"] for row in await cur.fetchall()}
    for column in ("cached_new_words", "cached_repeated_words"):
        if column not in existing_project_cols:
            await conn.execute(f"ALTER TABLE projects ADD COLUMN {column} INTEGER")
            await conn.commit()

    # Unused since projects consolidate every crawl in them; kept so older
    # databases and this one stay alike.
    cur = await conn.execute("PRAGMA table_info(project_runs)")
    if "count_unfinished" not in {row["name"] for row in await cur.fetchall()}:
        await conn.execute("ALTER TABLE project_runs ADD COLUMN count_unfinished INTEGER NOT NULL DEFAULT 0")
        await conn.commit()


async def close_db() -> None:
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None


def _conn() -> aiosqlite.Connection:
    if _connection is None:
        raise RuntimeError("Database not initialized — call init_db() at startup")
    return _connection


def _row_to_user(row: aiosqlite.Row) -> User:
    return User(
        id=row["id"],
        google_sub=row["google_sub"],
        email=row["email"],
        name=row["name"],
        picture=row["picture"],
        plan=row["plan"],
        plan_status=row["plan_status"],
        stripe_customer_id=row["stripe_customer_id"],
        stripe_subscription_id=row["stripe_subscription_id"],
        plan_renews_at=row["plan_renews_at"],
        lang=row["lang"],
    )


async def get_or_create_user(google_sub: str, email: str, name: str, picture: str | None) -> tuple[User, bool]:
    """Returns the user and whether this call is what created them — the caller
    seeds the sample run on a genuinely new account, and only once."""
    conn = _conn()
    async with conn.execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,)) as cur:
        row = await cur.fetchone()
    if row is not None:
        return _row_to_user(row), False

    now = datetime.now(timezone.utc).isoformat()
    cur = await conn.execute(
        "INSERT INTO users (google_sub, email, name, picture, created_at) VALUES (?, ?, ?, ?, ?)",
        (google_sub, email, name, picture, now),
    )
    await conn.commit()
    return await get_user(cur.lastrowid), True


async def get_user(user_id: int) -> User | None:
    conn = _conn()
    async with conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_user(row) if row else None


# ---------- Personal keys for the MCP server (app/mcp_keys.py) ----------

async def create_api_key(user_id: int, key_id: str, name: str, token_hash: str, hint: str) -> dict:
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "INSERT INTO api_keys (id, user_id, name, token_hash, hint, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (key_id, user_id, name, token_hash, hint, now),
    )
    await conn.commit()
    return {"id": key_id, "name": name, "hint": hint, "created_at": now, "last_used_at": None}


async def list_api_keys(user_id: int) -> list[dict]:
    """A user's keys that still work, newest first."""
    conn = _conn()
    async with conn.execute(
        "SELECT id, name, hint, created_at, last_used_at FROM api_keys"
        " WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at DESC",
        (user_id,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def revoke_api_key(user_id: int, key_id: str) -> bool:
    conn = _conn()
    cur = await conn.execute(
        "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
        (datetime.now(timezone.utc).isoformat(), key_id, user_id),
    )
    await conn.commit()
    return cur.rowcount > 0


async def user_for_api_key(token_hash: str) -> User | None:
    """The user a key belongs to, if it's live. Notes when it was last used —
    at most once a minute, so a busy assistant doesn't write on every call."""
    conn = _conn()
    async with conn.execute(
        "SELECT k.id AS key_id, k.last_used_at AS key_last_used, u.* FROM api_keys k JOIN users u ON u.id = k.user_id"
        " WHERE k.token_hash = ? AND k.revoked_at IS NULL",
        (token_hash,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    last = row["key_last_used"]
    if last is None or (now - datetime.fromisoformat(last)).total_seconds() > 60:
        await conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now.isoformat(), row["key_id"]))
        await conn.commit()
    return _row_to_user(row)


async def set_user_lang(user_id: int, lang: str) -> None:
    conn = _conn()
    await conn.execute("UPDATE users SET lang = ? WHERE id = ?", (lang, user_id))
    await conn.commit()


async def get_user_by_stripe_customer(customer_id: str) -> User | None:
    """How a Stripe webhook finds the account: the event carries a customer id
    and nothing else that identifies us."""
    conn = _conn()
    async with conn.execute("SELECT * FROM users WHERE stripe_customer_id = ?", (customer_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_user(row) if row else None


async def set_stripe_customer(user_id: int, customer_id: str) -> None:
    conn = _conn()
    await conn.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?", (customer_id, user_id))
    await conn.commit()


async def save_subscription_state(
    user_id: int,
    plan: str,
    plan_status: str | None,
    stripe_subscription_id: str | None,
    plan_renews_at: str | None,
) -> None:
    """Writes plan state outright rather than adjusting it, which is what makes
    a redelivered Stripe webhook a no-op instead of a double-apply."""
    conn = _conn()
    await conn.execute(
        """
        UPDATE users
           SET plan = ?, plan_status = ?, stripe_subscription_id = ?, plan_renews_at = ?
         WHERE id = ?
        """,
        (plan, plan_status, stripe_subscription_id, plan_renews_at, user_id),
    )
    await conn.commit()


def _row_to_run(row: aiosqlite.Row) -> RunRecord:
    pages = [PageResult(**p) for p in json.loads(row["pages_json"])]
    resume_state_json = row["resume_state_json"]
    return RunRecord(
        id=row["id"],
        source_url=row["source_url"],
        user_id=row["user_id"],
        created_at=row["created_at"],
        status=row["status"],
        total_words=row["total_words"],
        page_count=row["page_count"],
        limit_reached=bool(row["limit_reached"]),
        login_blocked_count=row["login_blocked_count"],
        duplicate_count=row["duplicate_count"],
        domain_scope=row["domain_scope"],
        exclusions=row["exclusions"],
        crawl_concurrency=row["crawl_concurrency"],
        duration_seconds=row["duration_seconds"],
        stop_kind=row["stop_kind"],
        stopped_reason=row["stopped_reason"],
        error=row["error"],
        detected_cms=row["detected_cms"],
        language=row["language"],
        language_auto_detected=bool(row["language_auto_detected"]),
        resume_state=json.loads(resume_state_json) if resume_state_json else None,
        is_public=bool(row["is_public"]),
        is_sample=bool(row["is_sample"]),
        capture_markdown=bool(row["capture_markdown"]),
        markdown_pages=row["markdown_pages"],
        markdown_bytes=row["markdown_bytes"],
        markdown_state=row["markdown_state"],
        surface=row["surface"],
        password_protected=bool(row["password_protected"]),
        signed_in=bool(row["signed_in"]),
        login_url=row["login_url"],
        linked_hosts=json.loads(row["linked_hosts_json"]) if row["linked_hosts_json"] else {},
        linked_languages=json.loads(row["linked_languages_json"]) if row["linked_languages_json"] else {},
        other_languages=json.loads(row["other_languages_json"]) if row["other_languages_json"] else {},
        repetition=json.loads(row["repetition_json"]) if row["repetition_json"] else {},
        display_url=row["display_url"],
        pages=pages,
    )


async def get_latest_run(source_url: str) -> RunRecord | None:
    conn = _conn()
    async with conn.execute(
        "SELECT * FROM runs WHERE source_url = ? AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
        (source_url,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_run(row) if row else None


async def get_run(run_id: str) -> RunRecord | None:
    conn = _conn()
    async with conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_run(row) if row else None


async def set_run_public(run_id: str, is_public: bool) -> bool:
    """Returns False if the run has no saved row at all (e.g. still live and
    never reached a terminal/paused state) — nothing to toggle yet."""
    conn = _conn()
    cur = await conn.execute("UPDATE runs SET is_public = ? WHERE id = ?", (int(is_public), run_id))
    await conn.commit()
    return cur.rowcount == 1


async def add_run_share(run_id: str, email: str) -> None:
    """Records that the report's link was emailed to someone. This is a record
    of who it was sent to, not an access grant — access comes from is_public."""
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    # DO NOTHING, not REPLACE: re-sending to someone already on the list keeps
    # their original invite date, so the list doesn't reshuffle under them.
    await conn.execute(
        "INSERT INTO run_shares (run_id, email, created_at) VALUES (?, ?, ?)"
        " ON CONFLICT(run_id, email) DO NOTHING",
        (run_id, email, now),
    )
    await conn.commit()


async def list_run_shares(run_id: str) -> list[dict]:
    conn = _conn()
    async with conn.execute(
        "SELECT email, created_at FROM run_shares WHERE run_id = ? ORDER BY created_at", (run_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [{"email": row["email"], "created_at": row["created_at"]} for row in rows]


async def remove_run_share(run_id: str, email: str) -> None:
    conn = _conn()
    await conn.execute("DELETE FROM run_shares WHERE run_id = ? AND email = ?", (run_id, email))
    await conn.commit()


async def save_run(
    run_id: str,
    source_url: str,
    user_id: int,
    status: str,
    total_words: int,
    pages: list[PageResult],
    limit_reached: bool,
    login_blocked_count: int = 0,
    duplicate_count: int = 0,
    domain_scope: str = "all",
    exclusions: str | None = None,
    crawl_concurrency: int = 0,
    duration_seconds: int = 0,
    stop_kind: str | None = None,
    stopped_reason: str | None = None,
    error: str | None = None,
    detected_cms: str | None = None,
    language: str | None = None,
    language_auto_detected: bool = False,
    resume_state: dict | None = None,
    capture_markdown: bool = False,
    markdown_pages: int = 0,
    markdown_bytes: int = 0,
    markdown_state: str = "off",
    surface: str = "counter",
    password_protected: bool = False,
    signed_in: bool = False,
    login_url: str | None = None,
    linked_hosts: dict[str, int] | None = None,
    linked_languages: dict[str, int] | None = None,
    other_languages: dict[str, dict] | None = None,
    repetition: dict | None = None,
) -> None:
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    pages_json = json.dumps([p.model_dump() for p in pages])
    resume_state_json = json.dumps(resume_state) if resume_state is not None else None
    await conn.execute(
        """
        INSERT INTO runs
            (id, source_url, user_id, created_at, status, total_words, page_count, limit_reached,
             login_blocked_count, duplicate_count, domain_scope, exclusions, crawl_concurrency,
             duration_seconds, stop_kind, stopped_reason, error, detected_cms,
             language, language_auto_detected, resume_state_json, pages_json,
             capture_markdown, markdown_pages, markdown_bytes, markdown_state, surface,
             password_protected, linked_hosts_json, linked_languages_json, other_languages_json,
             repetition_json, signed_in, login_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            status=excluded.status,
            total_words=excluded.total_words,
            page_count=excluded.page_count,
            limit_reached=excluded.limit_reached,
            login_blocked_count=excluded.login_blocked_count,
            duplicate_count=excluded.duplicate_count,
            domain_scope=excluded.domain_scope,
            exclusions=excluded.exclusions,
            crawl_concurrency=excluded.crawl_concurrency,
            duration_seconds=excluded.duration_seconds,
            stop_kind=excluded.stop_kind,
            stopped_reason=excluded.stopped_reason,
            error=excluded.error,
            detected_cms=excluded.detected_cms,
            language=excluded.language,
            language_auto_detected=excluded.language_auto_detected,
            resume_state_json=excluded.resume_state_json,
            pages_json=excluded.pages_json,
            capture_markdown=excluded.capture_markdown,
            markdown_pages=excluded.markdown_pages,
            markdown_bytes=excluded.markdown_bytes,
            markdown_state=excluded.markdown_state,
            surface=excluded.surface,
            password_protected=excluded.password_protected,
            linked_hosts_json=excluded.linked_hosts_json,
            linked_languages_json=excluded.linked_languages_json,
            other_languages_json=excluded.other_languages_json,
            repetition_json=excluded.repetition_json,
            signed_in=excluded.signed_in,
            login_url=excluded.login_url
        """,
        (
            # created_at is only ever set on first insert (see ON CONFLICT above) —
            # periodic checkpointing during a crawl must not keep bumping it forward.
            run_id, source_url, user_id, now, status, total_words, len(pages), int(limit_reached),
            login_blocked_count, duplicate_count, domain_scope, exclusions, crawl_concurrency,
            duration_seconds, stop_kind, stopped_reason, (error or "")[:500] or None, detected_cms,
            language, int(language_auto_detected), resume_state_json, pages_json,
            int(capture_markdown), markdown_pages, markdown_bytes, markdown_state, surface,
            int(password_protected),
            json.dumps(linked_hosts) if linked_hosts else None,
            json.dumps(linked_languages) if linked_languages else None,
            json.dumps(other_languages) if other_languages else None,
            json.dumps(repetition) if repetition and repetition.get("words_total") else None,
            int(signed_in), login_url,
        ),
    )
    await conn.commit()


async def get_crawling_runs() -> list[RunRecord]:
    """Runs still marked "crawling" at startup are, by definition, orphaned —
    JOBS is always empty on a fresh process, so nothing else could still be
    running one. Used to auto-resume crawls interrupted by a crash/restart.

    First resets any rows stuck at "resuming" (claimed by claim_crawling_run,
    then crashed again before reaching a checkpoint) back to "crawling" so
    they're eligible again — safe to run redundantly if multiple processes
    boot at once, since the end state is identical either way."""
    conn = _conn()
    await conn.execute("UPDATE runs SET status = 'crawling' WHERE status = 'resuming'")
    await conn.commit()
    async with conn.execute("SELECT * FROM runs WHERE status = 'crawling'") as cur:
        rows = await cur.fetchall()
    return [_row_to_run(row) for row in rows]


async def get_paused_runs() -> list[RunRecord]:
    """Runs still marked "paused" at startup — JOBS is always empty on a
    fresh process, so their in-memory Job (and its estimate_result) is gone
    even though the pause itself is a legitimate, not-crashed state. Used to
    rebuild each one back into JOBS (display-only, no task relaunched — see
    app.main's lifespan) so the estimate panel and "Proceed with crawl" keep
    working across a restart instead of quietly breaking."""
    conn = _conn()
    async with conn.execute("SELECT * FROM runs WHERE status = 'paused'") as cur:
        rows = await cur.fetchall()
    return [_row_to_run(row) for row in rows]


async def get_estimate_snapshot(run_id: str) -> dict | None:
    conn = _conn()
    async with conn.execute("SELECT * FROM estimate_history WHERE run_id = ?", (run_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def reassign_runs(from_user_id: int, to_user_id: int) -> int:
    conn = _conn()
    cur = await conn.execute("UPDATE runs SET user_id = ? WHERE user_id = ?", (to_user_id, from_user_id))
    await conn.commit()
    return cur.rowcount


async def stale_guest_run_ids(before_iso: str) -> list[str]:
    """Crawls started without signing in and never claimed by a sign-in."""
    conn = _conn()
    async with conn.execute(
        "SELECT r.id FROM runs r JOIN users u ON u.id = r.user_id"
        " WHERE u.google_sub LIKE 'guest:%' AND r.created_at < ?",
        (before_iso,),
    ) as cur:
        return [row["id"] for row in await cur.fetchall()]


async def delete_idle_guests(before_iso: str) -> int:
    conn = _conn()
    cur = await conn.execute(
        "DELETE FROM users WHERE google_sub LIKE 'guest:%' AND created_at < ?"
        " AND id NOT IN (SELECT user_id FROM runs)",
        (before_iso,),
    )
    await conn.commit()
    return cur.rowcount


async def set_run_display_url(run_id: str, url: str | None) -> bool:
    """None puts the report back to showing the address the crawl started from."""
    conn = _conn()
    cur = await conn.execute("UPDATE runs SET display_url = ? WHERE id = ?", (url, run_id))
    await conn.commit()
    return cur.rowcount > 0


async def set_run_example(run_id: str, is_example: bool) -> bool:
    """Featuring a run also switches its public link on: the front page links
    to /share/<id>, which 404s for a private run. Un-featuring leaves the link
    as it is — it may have been shared on purpose."""
    conn = _conn()
    if is_example:
        cur = await conn.execute("UPDATE runs SET is_example = 1, is_public = 1 WHERE id = ?", (run_id,))
    else:
        cur = await conn.execute("UPDATE runs SET is_example = 0 WHERE id = ?", (run_id,))
    await conn.commit()
    return cur.rowcount == 1


async def list_example_runs(limit: int = 3) -> list[dict]:
    """The front page's example reports, smallest first so the list reads as
    a range. A run whose public link was switched off since drops out."""
    conn = _conn()
    async with conn.execute(
        f"SELECT {_RUN_SUMMARY_COLUMNS} FROM runs"
        " WHERE is_example = 1 AND is_public = 1 AND status = 'completed'"
        " ORDER BY total_words ASC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_run_summary(row) for row in rows]


async def fail_run(run_id: str, error: str) -> None:
    conn = _conn()
    await conn.execute(
        "UPDATE runs SET status = 'failed', stop_kind = 'failed', error = ? WHERE id = ?",
        (error, run_id),
    )
    await conn.commit()


async def claim_crawling_run(run_id: str) -> bool:
    """Atomically claims an orphaned run for auto-resume, so if more than one
    process/instance races to resume the same run on startup, only one wins.
    Must only ever match the exact "crawling" state (not e.g. "resuming" too)
    — SQLite serializes this UPDATE across processes sharing the same DB
    file, so whichever one flips the row first leaves nothing for the other
    to match."""
    conn = _conn()
    cur = await conn.execute(
        "UPDATE runs SET status = 'resuming' WHERE id = ? AND status = 'crawling'",
        (run_id,),
    )
    await conn.commit()
    return cur.rowcount == 1


async def request_cancel(run_id: str) -> None:
    """Persists a cancel request to the shared DB (not just in-memory), so it
    reaches whichever process is actually running the crawl even if the HTTP
    request that triggered it landed on a different one. A no-op if the run
    has no row yet (cancelled before its first checkpoint) — the in-memory
    flag set alongside this call covers that narrow window instead."""
    conn = _conn()
    await conn.execute("UPDATE runs SET cancel_requested = 1 WHERE id = ?", (run_id,))
    await conn.commit()


async def is_cancel_requested(run_id: str) -> bool:
    conn = _conn()
    async with conn.execute("SELECT cancel_requested FROM runs WHERE id = ?", (run_id,)) as cur:
        row = await cur.fetchone()
    return bool(row["cancel_requested"]) if row else False


async def save_estimate_snapshot(run_id: str, source_url: str, estimate_result: dict) -> None:
    """Called exactly once per run, right when it pauses and computes an
    estimate — a run only ever pauses once (resuming never sets
    pause_at_words again), so there's nothing to upsert against here."""
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """
        INSERT INTO estimate_history
            (run_id, source_url, created_at, pages_fetched, discovered_total, sitemap_count,
             sitemap_found, detected_cms, confidence, estimated_total_pages, estimated_total_words,
             elapsed_seconds, words_per_minute, pages_per_minute, estimated_duration_seconds, concurrent_crawls)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, source_url, now,
            estimate_result["pages_fetched"], estimate_result["discovered_total"], estimate_result["sitemap_count"],
            int(estimate_result["sitemap_found"]), estimate_result["detected_cms"], estimate_result["confidence"],
            estimate_result["total_pages_estimate"], estimate_result["estimated_total_words"],
            estimate_result["elapsed_seconds"], estimate_result["words_per_minute"],
            estimate_result["pages_per_minute"], estimate_result["estimated_duration_seconds"],
            estimate_result["concurrent_crawls"],
        ),
    )
    await conn.commit()


async def record_estimate_actual(run_id: str, actual_total_pages: int, actual_total_words: int) -> None:
    """Safe to call unconditionally whenever a run completes — a no-op
    (affects zero rows) for any run that never paused and so never had an
    estimate snapshot saved in the first place."""
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "UPDATE estimate_history SET actual_total_pages = ?, actual_total_words = ?, completed_at = ? WHERE run_id = ?",
        (actual_total_pages, actual_total_words, now, run_id),
    )
    await conn.commit()


async def list_estimate_history() -> list[dict]:
    conn = _conn()
    async with conn.execute("SELECT * FROM estimate_history ORDER BY created_at DESC") as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


# Listing screens show one line per run and never touch the page rows, so they
# deliberately avoid pages_json — parsing it for every run is what would make
# "all runs" expensive on an account (or a server) with a lot of history.
_RUN_SUMMARY_COLUMNS = ("id, source_url, created_at, status, total_words, page_count, is_public, is_sample,"
                        " markdown_pages, markdown_bytes, detected_cms, is_example, display_url")


def _row_to_run_summary(row: aiosqlite.Row) -> dict:
    summary = {
        "id": row["id"],
        "source_url": row["source_url"],
        "shown_url": row["display_url"] or row["source_url"],
        "created_at": row["created_at"],
        "status": row["status"],
        "total_words": row["total_words"],
        "page_count": row["page_count"],
        "is_public": bool(row["is_public"]),
        "is_sample": bool(row["is_sample"]),
        "markdown_pages": row["markdown_pages"],
        "markdown_bytes": row["markdown_bytes"],
        "detected_cms": row["detected_cms"],
        "is_example": bool(row["is_example"]),
    }
    if "owner_email" in row.keys():
        summary["owner_email"] = row["owner_email"]
        summary["owner_name"] = row["owner_name"]
    return summary


async def delete_run(run_id: str) -> None:
    """Removes the run and everything hanging off it. estimate_history is kept
    deliberately — it's the admin accuracy record, and dropping rows would
    quietly bias it toward whichever runs nobody happened to delete."""
    conn = _conn()
    await conn.execute("DELETE FROM run_shares WHERE run_id = ?", (run_id,))
    await conn.execute("DELETE FROM project_runs WHERE run_id = ?", (run_id,))
    await conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    await conn.commit()
    markdown_store.delete(run_id)
    repetition.delete(run_id)
    segment_store.delete(run_id)


async def copy_run_to_user(run: RunRecord, user_id: int, run_id: str, as_sample: bool = False) -> None:
    """Note the markdown_* columns are deliberately absent from the INSERT below,
    so the copy takes their defaults. Stored Markdown is keyed by run id on disk
    and is not copied — inheriting the counts would give the new run a download
    button pointing at another run's files."""
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "INSERT INTO runs (id, source_url, user_id, created_at, status, total_words, page_count,"
        " limit_reached, pages_json, login_blocked_count, domain_scope, language,"
        " language_auto_detected, is_sample)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id, run.source_url, user_id, now, run.status, run.total_words, run.page_count,
            int(run.limit_reached), json.dumps([p.model_dump() for p in run.pages]),
            run.login_blocked_count, run.domain_scope, run.language,
            int(run.language_auto_detected), int(as_sample),
        ),
    )
    await conn.commit()


async def get_run_exists(run_id: str) -> bool:
    """Existence only — deliberately not get_run(), which parses the whole
    pages_json blob just to answer this."""
    conn = _conn()
    async with conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)) as cur:
        return await cur.fetchone() is not None


async def count_user_runs(user_id: int) -> int:
    conn = _conn()
    async with conn.execute("SELECT COUNT(*) AS n FROM runs WHERE user_id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    return row["n"]


async def list_user_runs(user_id: int, limit: int = 10, offset: int = 0) -> list[dict]:
    conn = _conn()
    async with conn.execute(
        f"SELECT {_RUN_SUMMARY_COLUMNS} FROM runs WHERE user_id = ?"
        " ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_run_summary(row) for row in rows]


def _all_runs_filter(query: str) -> tuple[str, list]:
    """Admin search matches the crawled site or the owner's email/name."""
    if not query:
        return "", []
    like = f"%{query}%"
    return " WHERE r.source_url LIKE ? OR u.email LIKE ? OR u.name LIKE ?", [like, like, like]


async def count_all_runs(query: str = "") -> int:
    conn = _conn()
    where, params = _all_runs_filter(query)
    async with conn.execute(
        f"SELECT COUNT(*) AS n FROM runs r JOIN users u ON u.id = r.user_id{where}", params
    ) as cur:
        row = await cur.fetchone()
    return row["n"]


async def disk_usage_by_user(limit: int = 25) -> list[dict]:
    """Who is holding how much saved Markdown, largest first.

    markdown_bytes is what the crawler counted as it wrote. The health page
    shows it next to what is actually on disk rather than picking one, because
    the two can drift — an orphaned run directory survives until the next boot
    sweep, and a crash mid-run leaves bytes nobody counted."""
    conn = _conn()
    async with conn.execute(
        """
        SELECT u.id, u.email, u.name,
               COALESCE(SUM(r.markdown_bytes), 0) AS bytes,
               COUNT(r.id) AS runs,
               SUM(CASE WHEN r.markdown_pages > 0 THEN 1 ELSE 0 END) AS runs_with_markdown
          FROM users u LEFT JOIN runs r ON r.user_id = u.id
      GROUP BY u.id
      ORDER BY bytes DESC, runs DESC
         LIMIT ?
        """,
        (limit,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def stop_kind_counts(since_iso: str) -> dict[str, int]:
    """How runs ended since a given time, by kind. The panel that answers
    "are crawls dying because too many run at once"."""
    conn = _conn()
    async with conn.execute(
        "SELECT COALESCE(stop_kind, 'unknown') AS kind, COUNT(*) AS n"
        " FROM runs WHERE created_at >= ? GROUP BY kind ORDER BY n DESC",
        (since_iso,),
    ) as cur:
        return {row["kind"]: row["n"] for row in await cur.fetchall()}


async def recent_stops(limit: int = 15) -> list[dict]:
    """The most recent runs that didn't simply finish, with their reasons."""
    conn = _conn()
    async with conn.execute(
        """
        SELECT r.id, r.source_url, r.created_at, r.status, r.stop_kind,
               r.stopped_reason, r.error, r.duration_seconds, r.crawl_concurrency,
               u.email AS owner_email
          FROM runs r JOIN users u ON u.id = r.user_id
         WHERE r.stop_kind IS NOT NULL AND r.stop_kind NOT IN ('completed', 'paused')
      ORDER BY r.created_at DESC LIMIT ?
        """,
        (limit,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def count_stops_since(kind: str, since_iso: str) -> int:
    conn = _conn()
    async with conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE stop_kind = ? AND created_at >= ?",
        (kind, since_iso),
    ) as cur:
        return (await cur.fetchone())["n"]


async def runs_missing_detected_cms(limit: int = 200) -> list[dict]:
    """Runs crawled before the platform was recorded. Only completed ones —
    re-probing a site for a crawl that failed halfway isn't worth the request."""
    conn = _conn()
    async with conn.execute(
        "SELECT id, source_url FROM runs"
        " WHERE detected_cms IS NULL AND status = 'completed'"
        " ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def set_detected_cms(run_ids: list[str], cms: str | None) -> int:
    if not run_ids:
        return 0
    conn = _conn()
    placeholders = ",".join("?" for _ in run_ids)
    cur = await conn.execute(
        f"UPDATE runs SET detected_cms = ? WHERE id IN ({placeholders})", (cms, *run_ids)
    )
    await conn.commit()
    return cur.rowcount


async def known_run_ids() -> set[str]:
    """Every run id the database knows about, for spotting orphaned Markdown
    directories between boots — the startup sweep only runs at boot."""
    conn = _conn()
    async with conn.execute("SELECT id FROM runs") as cur:
        return {row["id"] for row in await cur.fetchall()}


async def alert_last_sent(kind: str) -> str | None:
    conn = _conn()
    async with conn.execute("SELECT last_sent_at FROM server_alerts WHERE kind = ?", (kind,)) as cur:
        row = await cur.fetchone()
    return row["last_sent_at"] if row else None


async def mark_alert_sent(kind: str, when_iso: str) -> None:
    """Persisted rather than held in memory on purpose: an in-process cooldown
    resets on restart, and restarts are exactly what happens when the server is
    having the problems these alerts are about."""
    conn = _conn()
    await conn.execute(
        "INSERT INTO server_alerts (kind, last_sent_at) VALUES (?, ?)"
        " ON CONFLICT(kind) DO UPDATE SET last_sent_at = excluded.last_sent_at",
        (kind, when_iso),
    )
    await conn.commit()


async def list_all_runs(limit: int = 50, offset: int = 0, query: str = "") -> list[dict]:
    """Every user's runs, newest first — admin only."""
    conn = _conn()
    where, params = _all_runs_filter(query)
    columns = ", ".join(f"r.{c}" for c in _RUN_SUMMARY_COLUMNS.split(", "))
    async with conn.execute(
        f"SELECT {columns}, u.email AS owner_email, u.name AS owner_name"
        f" FROM runs r JOIN users u ON u.id = r.user_id{where}"
        " ORDER BY r.created_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_run_summary(row) for row in rows]


# ------------------------------------------------------------ translation check
#
# Every write a check makes goes through its lease: the heartbeat is updated
# WHERE owner = this process first, inside the same transaction, so a process
# that has lost the check to another (a rolling deploy runs both for a while)
# finds out before it writes anything.

_TRANSLATION_ACTIVE = ("planning", "running")
# Shared by every report query: a "Keep as is" for this language or for all.
_NOT_IGNORED = (
    " NOT EXISTS (SELECT 1 FROM translation_ignores i WHERE i.site_url = ?"
    " AND i.text_key = f.text_key AND (i.locale = '*' OR i.locale = f.locale))"
)


async def create_translation_check(site_url: str, trigger: str, rules_version: int, owner: str, now: str) -> int:
    conn = _conn()
    cur = await conn.execute(
        "INSERT INTO translation_checks (site_url, status, trigger, rules_version, owner, heartbeat_at, started_at)"
        " VALUES (?, 'planning', ?, ?, ?, ?, ?)",
        (site_url, trigger, rules_version, owner, now, now),
    )
    await conn.commit()
    return cur.lastrowid


async def get_translation_check(check_id: int) -> dict | None:
    conn = _conn()
    async with conn.execute("SELECT * FROM translation_checks WHERE id = ?", (check_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def list_translation_checks(limit: int = 30) -> list[dict]:
    conn = _conn()
    async with conn.execute("SELECT * FROM translation_checks ORDER BY id DESC LIMIT ?", (limit,)) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def active_translation_check() -> dict | None:
    conn = _conn()
    async with conn.execute(
        "SELECT * FROM translation_checks WHERE status IN (?, ?) ORDER BY id DESC LIMIT 1", _TRANSLATION_ACTIVE
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def latest_translation_check(site_url: str, status: str | None = None) -> dict | None:
    conn = _conn()
    sql = "SELECT * FROM translation_checks WHERE site_url = ?"
    params: list = [site_url]
    if status:
        sql += " AND status = ?"
        params.append(status)
    async with conn.execute(sql + " ORDER BY id DESC LIMIT 1", params) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def previous_completed_translation_check(site_url: str, before_id: int) -> dict | None:
    conn = _conn()
    async with conn.execute(
        "SELECT * FROM translation_checks WHERE site_url = ? AND status = 'completed' AND id < ?"
        " ORDER BY id DESC LIMIT 1",
        (site_url, before_id),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def _hold_translation_lease(check_id: int, owner: str, now: str, status: tuple[str, ...]) -> bool:
    conn = _conn()
    marks = ", ".join("?" for _ in status)
    cur = await conn.execute(
        f"UPDATE translation_checks SET heartbeat_at = ? WHERE id = ? AND owner = ? AND status IN ({marks})",
        (now, check_id, owner, *status),
    )
    if cur.rowcount == 0:
        await conn.rollback()
        return False
    return True


async def set_translation_plan(check_id: int, owner: str, locales: dict, rows: list[tuple], now: str) -> bool:
    """rows: (source_url, locale, url, status). Replaces any earlier plan, so a
    check taken over mid-planning simply plans again."""
    conn = _conn()
    if not await _hold_translation_lease(check_id, owner, now, ("planning",)):
        return False
    await conn.execute("DELETE FROM translation_pages WHERE check_id = ?", (check_id,))
    await conn.executemany(
        "INSERT INTO translation_pages (check_id, source_url, locale, url, status) VALUES (?, ?, ?, ?, ?)",
        [(check_id, *row) for row in rows],
    )
    await conn.execute(
        "UPDATE translation_checks SET status = 'running', locales_json = ? WHERE id = ?",
        (json.dumps(locales), check_id),
    )
    await conn.commit()
    return True


async def translation_check_rows(check_id: int) -> list[dict]:
    conn = _conn()
    async with conn.execute(
        "SELECT source_url, locale, url, status FROM translation_pages WHERE check_id = ? ORDER BY rowid",
        (check_id,),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def save_translation_group(check_id: int, owner: str, updates: list[tuple], now: str) -> bool:
    """One original page and its versions, all at once — a group is either
    checked or still pending, never half. updates: (source_url, locale, url,
    status, http_status, source_words, untranslated_words, findings_json)."""
    conn = _conn()
    if not await _hold_translation_lease(check_id, owner, now, ("running",)):
        return False
    await conn.executemany(
        "UPDATE translation_pages SET url = ?, status = ?, http_status = ?, source_words = ?,"
        " untranslated_words = ?, findings_json = ? WHERE check_id = ? AND source_url = ? AND locale = ?",
        [(url, status, http, words, untranslated, findings, check_id, source, locale)
         for source, locale, url, status, http, words, untranslated, findings in updates],
    )
    await conn.commit()
    return True


async def touch_translation_check(check_id: int, owner: str, now: str) -> bool:
    ok = await _hold_translation_lease(check_id, owner, now, _TRANSLATION_ACTIVE)
    if ok:
        await _conn().commit()
    return ok


async def release_translation_check(check_id: int, owner: str) -> None:
    """Hands a check over at shutdown: an empty heartbeat is stale at once."""
    conn = _conn()
    await conn.execute(
        "UPDATE translation_checks SET heartbeat_at = '' WHERE id = ? AND owner = ? AND status IN (?, ?)",
        (check_id, owner, *_TRANSLATION_ACTIVE),
    )
    await conn.commit()


async def claim_stale_translation_check(owner: str, now: str, stale_before: str) -> dict | None:
    conn = _conn()
    async with conn.execute(
        "SELECT id FROM translation_checks WHERE status IN (?, ?)"
        " AND (heartbeat_at IS NULL OR heartbeat_at < ?) ORDER BY id LIMIT 1",
        (*_TRANSLATION_ACTIVE, stale_before),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    cur = await conn.execute(
        "UPDATE translation_checks SET owner = ?, heartbeat_at = ?, resume_count = resume_count + 1"
        " WHERE id = ? AND status IN (?, ?) AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
        (owner, now, row["id"], *_TRANSLATION_ACTIVE, stale_before),
    )
    await conn.commit()
    return await get_translation_check(row["id"]) if cur.rowcount else None


async def finish_translation_check(check_id: int, owner: str, status: str, now: str,
                                   summary: dict | None = None, diff: dict | None = None,
                                   error: str | None = None) -> bool:
    conn = _conn()
    cur = await conn.execute(
        "UPDATE translation_checks SET status = ?, finished_at = ?, heartbeat_at = ?, summary_json = ?,"
        " diff_json = ?, error = ? WHERE id = ? AND owner = ? AND status IN (?, ?)",
        (status, now, now, json.dumps(summary) if summary else None, json.dumps(diff) if diff else None,
         error, check_id, owner, *_TRANSLATION_ACTIVE),
    )
    await conn.commit()
    return cur.rowcount > 0


async def request_translation_cancel(check_id: int) -> bool:
    conn = _conn()
    cur = await conn.execute(
        "UPDATE translation_checks SET cancel_requested = 1 WHERE id = ? AND status IN (?, ?)",
        (check_id, *_TRANSLATION_ACTIVE),
    )
    await conn.commit()
    return cur.rowcount > 0


async def translation_cancel_requested(check_id: int) -> bool:
    conn = _conn()
    async with conn.execute("SELECT cancel_requested FROM translation_checks WHERE id = ?", (check_id,)) as cur:
        row = await cur.fetchone()
    return bool(row and row["cancel_requested"])


async def mark_translation_emailed(check_id: int, now: str) -> None:
    conn = _conn()
    await conn.execute("UPDATE translation_checks SET emailed_at = ? WHERE id = ?", (now, check_id))
    await conn.commit()


async def iter_translation_page_findings(check_id: int, batch: int = 500):
    """The checked language versions, a batch at a time — all of them at once
    would be every finding of ten thousand pages in memory."""
    conn = _conn()
    last = 0
    while True:
        async with conn.execute(
            "SELECT rowid, locale, source_url, url, status, source_words, findings_json FROM translation_pages"
            " WHERE check_id = ? AND locale != '' AND status IN ('ok', 'missing', 'redirected') AND rowid > ?"
            " ORDER BY rowid LIMIT ?",
            (check_id, last, batch),
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            return
        for row in rows:
            yield row
        last = rows[-1]["rowid"]


async def clear_translation_page_findings(check_id: int, keep: list[str] = ()) -> None:
    conn = _conn()
    marks = ", ".join("?" for _ in keep)
    await conn.execute(
        "UPDATE translation_pages SET findings_json = NULL WHERE check_id = ?"
        + (f" AND source_url NOT IN ({marks})" if keep else ""),
        (check_id, *keep),
    )
    await conn.commit()


async def translation_key_page_rows(check_id: int, sources: list[str]) -> list[dict]:
    """Every language version of the key pages, with their findings."""
    if not sources:
        return []
    conn = _conn()
    marks = ", ".join("?" for _ in sources)
    async with conn.execute(
        "SELECT source_url, locale, url, status, source_words, untranslated_words, findings_json"
        f" FROM translation_pages WHERE check_id = ? AND locale != '' AND source_url IN ({marks})",
        (check_id, *sources),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def plan_translation_shots(check_id: int, owner: str, rows: list[tuple], now: str) -> bool:
    conn = _conn()
    if not await _hold_translation_lease(check_id, owner, now, ("running",)):
        return False
    await conn.executemany(
        "INSERT OR IGNORE INTO translation_shots (check_id, source_url, locale, url, status)"
        " VALUES (?, ?, ?, ?, 'pending')",
        [(check_id, *row) for row in rows],
    )
    await conn.commit()
    return True


async def translation_shots(check_id: int) -> list[dict]:
    conn = _conn()
    async with conn.execute(
        "SELECT * FROM translation_shots WHERE check_id = ? ORDER BY rowid", (check_id,)
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def save_translation_shot(check_id: int, owner: str, source_url: str, locale: str, file: str | None,
                                marks: int, error: str | None, now: str) -> bool:
    conn = _conn()
    if not await _hold_translation_lease(check_id, owner, now, ("running",)):
        return False
    await conn.execute(
        "UPDATE translation_shots SET status = ?, file = ?, marks = ?, error = ?, taken_at = ?"
        " WHERE check_id = ? AND source_url = ? AND locale = ?",
        ("ok" if file else "failed", file, marks, error, now, check_id, source_url, locale),
    )
    await conn.commit()
    return True


# A language version kept in the original on purpose, by "Keep as is".
_PAGE_NOT_IGNORED = (
    " NOT EXISTS (SELECT 1 FROM translation_ignores i WHERE i.site_url = ?"
    " AND i.text_key = 'page:' || p.source_url AND (i.locale = '*' OR i.locale = p.locale))"
)


def _english_pages_filter(check_id: int, site_url: str, share: float, min_words: int,
                          lang: str | None) -> tuple[str, list]:
    sql = (" FROM translation_pages p WHERE p.check_id = ? AND p.locale != '' AND p.status = 'ok'"
           " AND p.source_words >= ? AND p.untranslated_words >= p.source_words * ? AND" + _PAGE_NOT_IGNORED)
    params: list = [check_id, min_words, share, site_url]
    if lang:
        sql += " AND p.locale = ?"
        params.append(lang)
    return sql, params


async def translation_english_page_counts(check_id: int, site_url: str, share: float,
                                          min_words: int) -> dict[str, int]:
    conn = _conn()
    where, params = _english_pages_filter(check_id, site_url, share, min_words, None)
    async with conn.execute(f"SELECT p.locale, COUNT(*) AS n{where} GROUP BY p.locale", params) as cur:
        return {row["locale"]: row["n"] for row in await cur.fetchall()}


async def translation_english_pages(check_id: int, site_url: str, share: float, min_words: int, *,
                                    lang: str | None = None, limit: int = 50,
                                    offset: int = 0) -> tuple[list[dict], int]:
    """Original pages whose language versions are still mostly in the
    original language, each with the languages concerned and how much."""
    conn = _conn()
    where, params = _english_pages_filter(check_id, site_url, share, min_words, lang)
    async with conn.execute(f"SELECT COUNT(DISTINCT p.source_url) AS n{where}", params) as cur:
        total = (await cur.fetchone())["n"]
    async with conn.execute(
        "SELECT p.source_url, COUNT(*) AS languages, MAX(p.source_words) AS words,"
        " GROUP_CONCAT(p.locale || '=' || CAST(100 * p.untranslated_words / p.source_words AS INTEGER)"
        "   || '=' || p.url, '|') AS versions,"
        " MAX(1.0 * p.untranslated_words / p.source_words) AS worst"
        f"{where} GROUP BY p.source_url ORDER BY languages DESC, worst DESC, words DESC, p.source_url"
        " LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ) as cur:
        rows = [dict(row) for row in await cur.fetchall()]
    return rows, total


async def translation_ok_urls(check_id: int) -> set[str]:
    conn = _conn()
    async with conn.execute(
        "SELECT url FROM translation_pages WHERE check_id = ? AND locale != '' AND status = 'ok'", (check_id,)
    ) as cur:
        return {row["url"] for row in await cur.fetchall()}


async def translation_finding_samples(check_id: int) -> dict[tuple[str, str, str], list[str]]:
    conn = _conn()
    async with conn.execute(
        "SELECT locale, kind, text_key, sample_urls_json FROM translation_findings WHERE check_id = ?", (check_id,)
    ) as cur:
        return {(row["locale"], row["kind"], row["text_key"]): json.loads(row["sample_urls_json"] or "[]")
                for row in await cur.fetchall()}


async def replace_translation_findings(check_id: int, groups: dict, new_keys: set) -> None:
    conn = _conn()
    await conn.execute("DELETE FROM translation_findings WHERE check_id = ?", (check_id,))
    await conn.executemany(
        "INSERT INTO translation_findings (check_id, locale, kind, text_key, text, detail, words, page_count,"
        " where_hint, component_id, component_label, sample_urls_json, source_url, is_new)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (check_id, locale, kind, key, g["text"], g["detail"], g["words"], g["pages"], g["where"],
             g["component_id"], g["component_label"], json.dumps(g["samples"]), g["source_url"],
             1 if (locale, kind, key) in new_keys else 0)
            for (locale, kind, key), g in groups.items()
        ],
    )
    await conn.commit()


async def translation_page_stats(check_id: int) -> dict[str, dict]:
    """Per language: versions checked, missing, failed, and the original words
    they were compared against."""
    conn = _conn()
    async with conn.execute(
        "SELECT locale, SUM(status = 'ok') AS ok, SUM(status IN ('missing', 'redirected')) AS missing,"
        " SUM(status = 'http_error') AS errors, SUM(status IN ('pending', 'skipped')) AS unchecked,"
        " SUM(CASE WHEN status = 'ok' THEN source_words ELSE 0 END) AS source_words,"
        " SUM(CASE WHEN status = 'ok' THEN untranslated_words ELSE 0 END) AS untranslated_words"
        " FROM translation_pages WHERE check_id = ? GROUP BY locale",
        (check_id,),
    ) as cur:
        return {row["locale"]: dict(row) for row in await cur.fetchall()}


async def translation_finding_stats(check_id: int, site_url: str) -> dict[str, dict]:
    """Per language, with "Keep as is" applied: English words left, open
    strings, missing pages kept as is, and new ones."""
    conn = _conn()
    async with conn.execute(
        "SELECT locale,"
        " SUM(CASE WHEN kind IN ('text', 'short_text', 'english_text') THEN words * page_count ELSE 0 END)"
        "   AS untranslated_words,"
        " SUM(kind != 'missing_page') AS strings, SUM(kind = 'missing_page') AS missing,"
        " SUM(is_new) AS new"
        f" FROM translation_findings f WHERE check_id = ? AND {_NOT_IGNORED} GROUP BY locale",
        (check_id, site_url),
    ) as cur:
        return {row["locale"]: dict(row) for row in await cur.fetchall()}


def _finding_filters(check_id: int, site_url: str, kinds: tuple[str, ...], lang: str | None,
                     component: bool | str | None, source_url: str | None,
                     single_page: bool | None) -> tuple[str, list]:
    sql = f" WHERE f.check_id = ? AND f.kind IN ({', '.join('?' for _ in kinds)}) AND {_NOT_IGNORED}"
    params: list = [check_id, *kinds, site_url]
    if lang:
        sql += " AND f.locale = ?"
        params.append(lang)
    if component is True:
        sql += " AND f.component_id IS NOT NULL"
    elif component is False:
        sql += " AND f.component_id IS NULL"
    elif isinstance(component, str):
        sql += " AND f.component_id = ?"
        params.append(component)
    if source_url:
        sql += " AND f.source_url = ?"
        params.append(source_url)
    if single_page is True:
        sql += " AND f.page_count = 1"
    elif single_page is False:
        sql += " AND f.page_count > 1"
    return sql, params


async def translation_finding_groups(check_id: int, site_url: str, kinds: tuple[str, ...], *,
                                     lang: str | None = None, component: bool | str | None = None,
                                     source_url: str | None = None, single_page: bool | None = None,
                                     limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    """One row per text, with the languages it's untranslated in."""
    conn = _conn()
    where, params = _finding_filters(check_id, site_url, kinds, lang, component, source_url, single_page)
    async with conn.execute(
        f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM translation_findings f{where} GROUP BY f.kind, f.text_key)",
        params,
    ) as cur:
        total = (await cur.fetchone())["n"]
    async with conn.execute(
        "SELECT f.kind, f.text_key, MIN(f.text) AS text, MAX(f.detail) AS detail, MAX(f.words) AS words,"
        " MAX(f.page_count) AS pages, GROUP_CONCAT(f.locale) AS locales, MAX(f.is_new) AS is_new,"
        " MIN(f.sample_urls_json) AS samples, MIN(f.source_url) AS source_url,"
        " MAX(f.component_label) AS component_label, MAX(f.where_hint) AS where_hint"
        f" FROM translation_findings f{where} GROUP BY f.kind, f.text_key"
        " ORDER BY pages DESC, words DESC, text LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ) as cur:
        rows = [dict(row) for row in await cur.fetchall()]
    return rows, total


async def translation_component_groups(check_id: int, site_url: str, kinds: tuple[str, ...],
                                       lang: str | None = None) -> list[dict]:
    """Untranslated text by the Webflow component it lives in: one fix there
    clears every page the component is on."""
    conn = _conn()
    where, params = _finding_filters(check_id, site_url, kinds, lang, True, None, None)
    async with conn.execute(
        "SELECT f.component_id, MAX(f.component_label) AS label, COUNT(DISTINCT f.text_key) AS strings,"
        " GROUP_CONCAT(DISTINCT f.locale) AS locales, MAX(f.page_count) AS pages, MAX(f.is_new) AS is_new"
        f" FROM translation_findings f{where} GROUP BY f.component_id ORDER BY pages DESC, strings DESC",
        params,
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def translation_source_groups(check_id: int, site_url: str, kinds: tuple[str, ...], *,
                                    lang: str | None = None, limit: int = 50,
                                    offset: int = 0) -> tuple[list[dict], int]:
    """Text found on one page only, by page — the way it gets fixed: open
    that page (or its CMS item) and go through its fields."""
    conn = _conn()
    where, params = _finding_filters(check_id, site_url, kinds, lang, False, None, True)
    async with conn.execute(
        f"SELECT COUNT(DISTINCT f.source_url) AS n FROM translation_findings f{where}", params
    ) as cur:
        total = (await cur.fetchone())["n"]
    # Each text once, however many languages left it: summed across
    # languages, a page's words would read ten times what's on it.
    async with conn.execute(
        "SELECT source_url, COUNT(*) AS strings, SUM(words) AS words, GROUP_CONCAT(locales) AS locales,"
        " MAX(is_new) AS is_new FROM (SELECT f.source_url, f.text_key, MAX(f.words) AS words,"
        " GROUP_CONCAT(f.locale) AS locales, MAX(f.is_new) AS is_new"
        f" FROM translation_findings f{where} GROUP BY f.source_url, f.text_key)"
        " GROUP BY source_url ORDER BY words DESC, source_url LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ) as cur:
        rows = [dict(row) for row in await cur.fetchall()]
    return rows, total


async def iter_translation_findings(check_id: int, site_url: str, lang: str | None = None):
    """Every open finding, one row per language, for the CSV."""
    conn = _conn()
    where, params = _finding_filters(check_id, site_url, tuple(_ALL_TRANSLATION_KINDS), lang, None, None, None)
    async with conn.execute(
        f"SELECT f.* FROM translation_findings f{where} ORDER BY f.locale, f.kind, f.page_count DESC, f.text",
        params,
    ) as cur:
        async for row in cur:
            yield dict(row)


_ALL_TRANSLATION_KINDS = ("missing_page", "text", "short_text", "english_text", "meta", "attribute",
                          "wrong_lang", "english_link")


async def iter_translation_pages(check_id: int, lang: str | None = None):
    conn = _conn()
    sql = "SELECT * FROM translation_pages WHERE check_id = ? AND locale != ''"
    params: list = [check_id]
    if lang:
        sql += " AND locale = ?"
        params.append(lang)
    async with conn.execute(sql + " ORDER BY locale, rowid", params) as cur:
        async for row in cur:
            yield dict(row)


async def translation_ignore_keys(site_url: str) -> set[tuple[str, str]]:
    conn = _conn()
    async with conn.execute(
        "SELECT locale, text_key FROM translation_ignores WHERE site_url = ?", (site_url,)
    ) as cur:
        return {(row["locale"], row["text_key"]) for row in await cur.fetchall()}


async def add_translation_ignore(site_url: str, locale: str, key: str, text: str, by: str, now: str) -> None:
    conn = _conn()
    await conn.execute(
        "INSERT OR REPLACE INTO translation_ignores (site_url, locale, text_key, text, created_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (site_url, locale, key, text[:500], by, now),
    )
    await conn.commit()


async def delete_translation_ignore(site_url: str, locale: str, key: str) -> None:
    conn = _conn()
    await conn.execute(
        "DELETE FROM translation_ignores WHERE site_url = ? AND locale = ? AND text_key = ?", (site_url, locale, key)
    )
    await conn.commit()


async def list_translation_ignores(site_url: str) -> list[dict]:
    conn = _conn()
    async with conn.execute(
        "SELECT * FROM translation_ignores WHERE site_url = ? ORDER BY created_at DESC", (site_url,)
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def prune_translation_checks(site_url: str, keep: int) -> list[int]:
    """The last `keep` checks of a site stay listed with their summary; the
    page-by-page detail only for the two latest completed ones (the report
    and what it's compared with) — it runs to tens of MB per check."""
    conn = _conn()
    async with conn.execute(
        "SELECT id, status FROM translation_checks WHERE site_url = ? ORDER BY id DESC", (site_url,)
    ) as cur:
        rows = await cur.fetchall()
    completed = [row["id"] for row in rows if row["status"] == "completed"][:2]
    active = [row["id"] for row in rows if row["status"] in _TRANSLATION_ACTIVE]
    pruned = []
    for index, row in enumerate(rows):
        if row["id"] in completed or row["id"] in active:
            continue
        pruned.append(row["id"])
        for table in ("translation_pages", "translation_findings", "translation_shots"):
            await conn.execute(f"DELETE FROM {table} WHERE check_id = ?", (row["id"],))
        if index >= keep:
            await conn.execute("DELETE FROM translation_checks WHERE id = ?", (row["id"],))
    await conn.commit()
    # The caller removes their screenshot files.
    return pruned


# ------------------------------------------------------------------ projects

_PROJECT_COLUMNS = ("p.id, p.user_id, p.name, p.created_at, p.updated_at, p.is_public, p.cached_words,"
                    " p.cached_pages, p.cached_overlap_words, p.cached_runs, p.cached_at,"
                    " p.cached_new_words, p.cached_repeated_words")


async def create_project(user_id: int, name: str, project_id: str) -> dict:
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "INSERT INTO projects (id, user_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (project_id, user_id, name, now, now),
    )
    await conn.commit()
    return await get_project(project_id)


async def get_project(project_id: str) -> dict | None:
    conn = _conn()
    async with conn.execute(f"SELECT {_PROJECT_COLUMNS} FROM projects p WHERE p.id = ?", (project_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    project = dict(row)
    project["is_public"] = bool(project["is_public"])
    return project


async def list_user_projects(user_id: int) -> list[dict]:
    """Newest activity first, each with how many runs it holds."""
    conn = _conn()
    async with conn.execute(
        f"SELECT {_PROJECT_COLUMNS}, (SELECT COUNT(*) FROM project_runs pr JOIN runs r ON r.id = pr.run_id"
        "  WHERE pr.project_id = p.id) AS run_count"
        " FROM projects p WHERE p.user_id = ? ORDER BY p.updated_at DESC",
        (user_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [{**dict(row), "is_public": bool(row["is_public"])} for row in rows]


async def rename_project(project_id: str, name: str) -> None:
    conn = _conn()
    await conn.execute("UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
                       (name, datetime.now(timezone.utc).isoformat(), project_id))
    await conn.commit()


async def set_project_public(project_id: str, is_public: bool) -> None:
    conn = _conn()
    await conn.execute("UPDATE projects SET is_public = ? WHERE id = ?", (int(is_public), project_id))
    await conn.commit()


async def delete_project(project_id: str) -> None:
    """The project goes; its runs stay, in your runs and any other project."""
    conn = _conn()
    await conn.execute("DELETE FROM project_runs WHERE project_id = ?", (project_id,))
    await conn.execute("DELETE FROM project_consolidations WHERE project_id = ?", (project_id,))
    await conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    await conn.commit()


async def add_runs_to_project(project_id: str, run_ids: list[str]) -> None:
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    await conn.executemany(
        "INSERT OR IGNORE INTO project_runs (project_id, run_id, added_at) VALUES (?, ?, ?)",
        [(project_id, run_id, now) for run_id in run_ids],
    )
    await conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
    await conn.commit()


async def remove_run_from_project(project_id: str, run_id: str) -> None:
    conn = _conn()
    await conn.execute("DELETE FROM project_runs WHERE project_id = ? AND run_id = ?", (project_id, run_id))
    await conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?",
                       (datetime.now(timezone.utc).isoformat(), project_id))
    await conn.commit()


async def project_ids_for_run(run_id: str) -> list[str]:
    conn = _conn()
    async with conn.execute("SELECT project_id FROM project_runs WHERE run_id = ?", (run_id,)) as cur:
        return [row["project_id"] for row in await cur.fetchall()]


async def projects_for_runs(run_ids: list[str]) -> dict[str, list[dict]]:
    """For a list of runs: the projects each is in, as {id, name}."""
    if not run_ids:
        return {}
    conn = _conn()
    marks = ", ".join("?" for _ in run_ids)
    async with conn.execute(
        "SELECT pr.run_id, p.id, p.name FROM project_runs pr JOIN projects p ON p.id = pr.project_id"
        f" WHERE pr.run_id IN ({marks}) ORDER BY p.name",
        run_ids,
    ) as cur:
        rows = await cur.fetchall()
    found: dict[str, list[dict]] = {}
    for row in rows:
        found.setdefault(row["run_id"], []).append({"id": row["id"], "name": row["name"]})
    return found


async def list_project_sites(user_id: int) -> dict[str, list[str]]:
    """Each of a user's projects: the addresses of its runs, newest first."""
    conn = _conn()
    async with conn.execute(
        "SELECT pr.project_id, COALESCE(r.display_url, r.source_url) AS shown_url"
        " FROM project_runs pr JOIN projects p ON p.id = pr.project_id JOIN runs r ON r.id = pr.run_id"
        " WHERE p.user_id = ? ORDER BY r.created_at DESC",
        (user_id,),
    ) as cur:
        rows = await cur.fetchall()
    sites: dict[str, list[str]] = {}
    for row in rows:
        shown = row["shown_url"]
        if shown not in sites.setdefault(row["project_id"], []):
            sites[row["project_id"]].append(shown)
    return sites


async def get_project_run_records(project_id: str) -> list[RunRecord]:
    """Every run in the project, with its pages — for the summary."""
    conn = _conn()
    async with conn.execute(
        "SELECT r.* FROM project_runs pr JOIN runs r ON r.id = pr.run_id WHERE pr.project_id = ?"
        " ORDER BY r.created_at DESC",
        (project_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_run(row) for row in rows]


async def list_runs_not_in_project(user_id: int, project_id: str | None = None, limit: int = 50) -> list[dict]:
    """The user's runs outside this project — or outside every project when
    none is given — newest first, for the pickers."""
    conn = _conn()
    columns = ", ".join(f"r.{c}" for c in _RUN_SUMMARY_COLUMNS.split(", "))
    if project_id is None:
        where = ("NOT EXISTS (SELECT 1 FROM project_runs pr JOIN projects p ON p.id = pr.project_id"
                 " WHERE pr.run_id = r.id AND p.user_id = r.user_id)")
        params: list = [user_id, limit]
    else:
        where = "NOT EXISTS (SELECT 1 FROM project_runs pr WHERE pr.run_id = r.id AND pr.project_id = ?)"
        params = [user_id, project_id, limit]
    async with conn.execute(
        f"SELECT {columns} FROM runs r WHERE r.user_id = ? AND {where} AND r.is_sample = 0"
        " ORDER BY r.created_at DESC LIMIT ?",
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_run_summary(row) for row in rows]


async def update_run_pages(run_id: str, pages: list[PageResult]) -> bool:
    """Rewrites a finished run's pages — the fingerprints a project rebuilt.
    Never a run still crawling, whose next checkpoint would write over it."""
    conn = _conn()
    cur = await conn.execute(
        "UPDATE runs SET pages_json = ? WHERE id = ? AND status IN ('completed', 'failed', 'cancelled')",
        (json.dumps([p.model_dump() for p in pages]), run_id),
    )
    await conn.commit()
    return cur.rowcount > 0


async def update_project_cache(project_id: str, words: int, pages: int, overlap_words: int, runs: int,
                               new_words: int | None = None, repeated_words: int | None = None) -> None:
    conn = _conn()
    await conn.execute(
        "UPDATE projects SET cached_words = ?, cached_pages = ?, cached_overlap_words = ?, cached_runs = ?,"
        " cached_new_words = ?, cached_repeated_words = ?, cached_at = ? WHERE id = ?",
        (words, pages, overlap_words, runs, new_words, repeated_words, datetime.now(timezone.utc).isoformat(),
         project_id),
    )
    await conn.commit()


async def list_project_run_summaries(project_id: str) -> list[dict]:
    """The project's runs without their pages — cheap enough for every view."""
    conn = _conn()
    async with conn.execute(
        "SELECT r.id, r.source_url, r.display_url, r.created_at, r.status, r.total_words, r.page_count,"
        " r.markdown_pages, r.is_public, r.signed_in, r.password_protected, r.detected_cms, r.repetition_json"
        " FROM project_runs pr JOIN runs r ON r.id = pr.run_id WHERE pr.project_id = ? ORDER BY r.created_at DESC",
        (project_id,),
    ) as cur:
        rows = await cur.fetchall()
    out = []
    for row in rows:
        repetition = json.loads(row["repetition_json"]) if row["repetition_json"] else {}
        total = repetition.get("words_total") or 0
        out.append({
            "id": row["id"], "source_url": row["source_url"], "shown_url": row["display_url"] or row["source_url"],
            "created_at": row["created_at"], "status": row["status"], "total_words": row["total_words"],
            "page_count": row["page_count"], "markdown_pages": row["markdown_pages"], "is_public": bool(row["is_public"]),
            "signed_in": bool(row["signed_in"]), "password_protected": bool(row["password_protected"]),
            "detected_cms": row["detected_cms"],
            "repeated_pct": round(100 * repetition["words_repeated"] / total) if total else None,
        })
    return out


# ---------- Project consolidations (app/consolidation_job.py) ----------

def _consolidation_row(row: aiosqlite.Row) -> dict:
    out = dict(row)
    out["result"] = json.loads(row["result_json"]) if row["result_json"] else None
    out["progress"] = json.loads(row["progress_json"]) if row["progress_json"] else None
    out.pop("result_json", None)
    out.pop("progress_json", None)
    return out


async def get_consolidation(project_id: str) -> dict | None:
    conn = _conn()
    async with conn.execute("SELECT * FROM project_consolidations WHERE project_id = ?", (project_id,)) as cur:
        row = await cur.fetchone()
    return _consolidation_row(row) if row else None


async def request_consolidation(project_id: str, reason: str, wanted_inputs: str) -> int:
    """Asks for a (new) consolidation and returns its generation. One already
    running carries on and is queued again when it finishes."""
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "INSERT INTO project_consolidations (project_id, status, requested_gen, reason, wanted_inputs, requested_at)"
        " VALUES (?, 'queued', 1, ?, ?, ?)"
        " ON CONFLICT(project_id) DO UPDATE SET requested_gen = requested_gen + 1, reason = excluded.reason,"
        " wanted_inputs = excluded.wanted_inputs, requested_at = excluded.requested_at,"
        " status = CASE WHEN status = 'running' THEN 'running' ELSE 'queued' END",
        (project_id, reason, wanted_inputs, now),
    )
    await conn.commit()
    async with conn.execute("SELECT requested_gen FROM project_consolidations WHERE project_id = ?",
                            (project_id,)) as cur:
        row = await cur.fetchone()
    return row["requested_gen"]


async def claim_consolidation(owner: str, now: str, stale_before: str,
                              project_id: str | None = None) -> tuple[str, int, int] | None:
    """Takes the oldest queued consolidation, or one whose owner stopped
    beating — (project id, generation, times resumed)."""
    conn = _conn()
    claimable = "(status = 'queued' OR (status = 'running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)))"
    args: tuple = (stale_before,)
    where = claimable
    if project_id is not None:
        where += " AND project_id = ?"
        args += (project_id,)
    async with conn.execute(
        f"SELECT project_id, status, requested_gen, resume_count FROM project_consolidations WHERE {where}"
        " ORDER BY requested_at LIMIT 1", args,
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    resumed = row["resume_count"] + (1 if row["status"] == "running" else 0)
    cur = await conn.execute(
        "UPDATE project_consolidations SET status = 'running', owner = ?, heartbeat_at = ?, started_at = ?,"
        f" claimed_gen = requested_gen, resume_count = ?, progress_json = NULL WHERE project_id = ? AND {claimable}",
        (owner, now, now, resumed, row["project_id"], stale_before),
    )
    await conn.commit()
    if cur.rowcount == 0:
        return None
    return row["project_id"], row["requested_gen"], resumed


async def heartbeat_consolidation(project_id: str, owner: str, progress: dict | None) -> int | None:
    """Keeps the lease; the generation now requested, or None if it's lost."""
    conn = _conn()
    cur = await conn.execute(
        "UPDATE project_consolidations SET heartbeat_at = ?, progress_json = ?"
        " WHERE project_id = ? AND owner = ? AND status = 'running'",
        (datetime.now(timezone.utc).isoformat(), json.dumps(progress) if progress else None, project_id, owner),
    )
    await conn.commit()
    if cur.rowcount == 0:
        return None
    async with conn.execute("SELECT requested_gen FROM project_consolidations WHERE project_id = ?",
                            (project_id,)) as cur2:
        row = await cur2.fetchone()
    return row["requested_gen"] if row else None


async def finish_consolidation(project_id: str, owner: str, gen: int, result: dict, inputs: str) -> bool:
    conn = _conn()
    cur = await conn.execute(
        "UPDATE project_consolidations SET result_json = ?, computed_gen = ?, inputs = ?, finished_at = ?,"
        " error = NULL, owner = NULL, progress_json = NULL, resume_count = 0,"
        " status = CASE WHEN requested_gen > ? THEN 'queued' ELSE 'done' END"
        " WHERE project_id = ? AND owner = ? AND status = 'running'",
        (json.dumps(result), gen, inputs, datetime.now(timezone.utc).isoformat(), gen, project_id, owner),
    )
    await conn.commit()
    return cur.rowcount > 0


async def fail_consolidation(project_id: str, owner: str, gen: int, error: str) -> None:
    """Keeps the last good result. Queued again only if something changed
    since the attempt; otherwise it waits for the next change or the button."""
    conn = _conn()
    await conn.execute(
        "UPDATE project_consolidations SET error = ?, finished_at = ?, owner = NULL, progress_json = NULL,"
        " resume_count = 0, status = CASE WHEN requested_gen > ? THEN 'queued' ELSE 'failed' END"
        " WHERE project_id = ? AND owner = ?",
        (error, datetime.now(timezone.utc).isoformat(), gen, project_id, owner),
    )
    await conn.commit()


async def release_consolidation(project_id: str, owner: str) -> None:
    """Hands an interrupted job back to the queue (shutdown)."""
    conn = _conn()
    await conn.execute(
        "UPDATE project_consolidations SET status = 'queued', owner = NULL, progress_json = NULL"
        " WHERE project_id = ? AND owner = ? AND status = 'running'",
        (project_id, owner),
    )
    await conn.commit()
