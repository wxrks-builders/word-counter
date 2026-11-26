"""The MCP server: Claude, ChatGPT and other assistants counting words.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_app_flows import app_env, run, save  # noqa: E402,F401

from app.models import PageResult  # noqa: E402


@pytest.fixture(autouse=True)
def _no_crawls_left_over():
    """Crawls live in memory; one left by another test would hold a slot, or
    be found as "the count already running" for its site."""
    from app.job_store import JOBS, QUEUE

    JOBS.clear()
    QUEUE.clear()
    yield
    for job in list(JOBS.values()):
        if job.task is not None and not job.task.done():
            job.task.cancel()
    JOBS.clear()
    QUEUE.clear()


def rpc(env, method, params=None, *, key=None, path="/mcp", msg_id=1, headers=None):
    body = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    sent = {"Accept": "application/json, text/event-stream", **(headers or {})}
    if key:
        sent["Authorization"] = f"Bearer {key}"
    return env.client.post(path, json=body, headers=sent)


def modern(params=None):
    return {**(params or {}), "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                        "io.modelcontextprotocol/clientCapabilities": {},
                                        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"}}}


def make_key(env, name="Claude Code"):
    res = env.client.post("/account/mcp-keys", json={"name": name})
    assert res.status_code == 200, res.text
    return res.json()


def call(env, key, tool, arguments=None, **kw):
    res = rpc(env, "tools/call", {"name": tool, "arguments": arguments or {}}, key=key, **kw)
    assert res.status_code == 200, res.text
    return res.json()["result"]


# ---------------------------------------------------------------- the protocol

def test_an_older_client_opens_with_initialize(app_env):
    res = rpc(app_env, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "claude-code", "version": "2"}})
    body = res.json()["result"]
    assert res.status_code == 200 and body["protocolVersion"] == "2025-06-18"
    assert body["serverInfo"]["name"] == "word-counter" and "tools" in body["capabilities"]
    assert "count_words" in body["instructions"]
    assert rpc(app_env, "initialize", {"protocolVersion": "2024-01-01"}).json()["result"]["protocolVersion"] == "2025-11-25"
    assert rpc(app_env, "notifications/initialized", msg_id=None).status_code == 202


def test_a_newer_client_discovers_the_server_without_a_handshake(app_env):
    res = rpc(app_env, "server/discover", modern(), headers={"MCP-Protocol-Version": "2026-07-28"})
    body = res.json()["result"]
    assert res.status_code == 200 and res.headers["MCP-Protocol-Version"] == "2026-07-28"
    assert "2026-07-28" in body["supportedVersions"] and body["resultType"] == "complete"
    assert body["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "word-counter"

    tools = rpc(app_env, "tools/list", modern()).json()["result"]
    assert [t["name"] for t in tools["tools"]] == ["count_words", "get_word_count", "continue_word_count",
                                                  "list_word_counts", "get_pages"]
    assert tools["ttlMs"] > 0 and tools["cacheScope"] == "public"
    assert all(t["inputSchema"]["type"] == "object" for t in tools["tools"])

    unknown = rpc(app_env, "resources/subscribe", modern())
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == -32601
    old = rpc(app_env, "tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}})
    assert old.status_code == 400 and old.json()["error"]["code"] == -32022
    assert "2026-07-28" in old.json()["error"]["data"]["supported"]


def test_what_isnt_a_request_is_refused_plainly(app_env):
    assert app_env.client.post("/mcp", content=b"{not json").status_code == 400
    batch = app_env.client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert batch.status_code == 400 and batch.json()["error"]["code"] == -32600
    assert app_env.client.post("/mcp", json={"id": 1, "method": "ping"}).status_code == 400
    legacy_unknown = rpc(app_env, "nope/nothing")
    assert legacy_unknown.status_code == 200 and legacy_unknown.json()["error"]["code"] == -32601
    assert rpc(app_env, "ping").json()["result"] == {"resultType": "complete"}


def test_the_endpoint_is_a_guide_for_people_and_never_a_stream(app_env):
    app_env.current["user"] = None
    page = app_env.client.get("/mcp", headers={"Accept": "text/html"})
    assert page.status_code == 200 and "Ask Claude or ChatGPT how many words a website has" in page.text
    assert "claude mcp add --transport http word-counter" in page.text
    stream = app_env.client.get("/mcp", headers={"Accept": "text/event-stream"})
    assert stream.status_code == 405 and stream.headers["allow"] == "POST"
    assert app_env.client.delete("/mcp").status_code == 405
    personal = app_env.client.get("/mcp/wck_secretsecretsecretsecret", follow_redirects=False)
    assert personal.status_code == 303 and personal.headers["location"] == "/mcp", "the key isn't shown back"


def test_listing_tools_needs_no_key_but_counting_does(app_env):
    assert len(rpc(app_env, "tools/list").json()["result"]["tools"]) == 5
    result = call(app_env, None, "count_words", {"url": "https://example.test"})
    assert result["isError"] and "/mcp/connect" in result["content"][0]["text"]
    bad = rpc(app_env, "tools/list", key="wck_notarealkeynotarealkey")
    assert bad.status_code == 401 and "Bearer" in bad.headers["www-authenticate"]


# ---------------------------------------------------------------- keys

def test_a_connection_is_made_shown_once_used_and_revoked(app_env):
    made = make_key(app_env, "Claude Code, laptop")
    key = made["key"]
    assert key.startswith("wck_") and made["url"].endswith("/mcp/" + key) and made["hint"] == key[-4:]
    stored = run(app_env.db.list_api_keys(1))
    assert [k["name"] for k in stored] == ["Claude Code, laptop"] and "token_hash" not in stored[0]

    page = app_env.client.get("/mcp/connect").text
    assert "Claude Code, laptop" in page and key not in page, "never shown again"

    assert call(app_env, key, "list_word_counts")["isError"] is False
    by_path = rpc(app_env, "tools/call", {"name": "list_word_counts", "arguments": {}}, path=f"/mcp/{key}")
    assert by_path.status_code == 200 and by_path.json()["result"]["isError"] is False
    assert run(app_env.db.list_api_keys(1))[0]["last_used_at"] is not None

    app_env.current["user"] = app_env.other
    assert app_env.client.delete(f"/account/mcp-keys/{made['id']}").status_code == 404, "not theirs"
    app_env.current["user"] = app_env.owner
    assert app_env.client.delete(f"/account/mcp-keys/{made['id']}").json() == {"revoked": made["id"]}
    assert rpc(app_env, "tools/list", key=key).status_code == 401
    assert rpc(app_env, "tools/list", path=f"/mcp/{key}").status_code == 401


def test_there_is_a_most_connections_a_user_can_have(app_env, monkeypatch):
    from app import mcp_keys

    monkeypatch.setattr(mcp_keys, "MAX_KEYS", 2)
    make_key(app_env)
    make_key(app_env)
    res = app_env.client.post("/account/mcp-keys", json={"name": "third"})
    assert res.status_code == 400 and res.json() == {"code": "too_many_keys"}


def test_the_connect_page_needs_an_account(app_env):
    app_env.main.app.dependency_overrides.clear()       # the real sign-in checks, nobody signed in
    page = app_env.client.get("/mcp/connect", follow_redirects=False)
    assert page.status_code == 303 and "/login" in page.headers["location"]
    assert app_env.client.post("/account/mcp-keys", json={"name": "x"}).status_code == 401


# ---------------------------------------------------------------- the tools

SITE = [
    ("https://example.test/", "Home", 300, 0),
    ("https://example.test/blog/one", "One", 900, 120),
    ("https://example.test/blog/two", "Two", 700, 120),
    ("https://example.test/de/start", "Start", 400, 0),
]


@pytest.fixture
def fake_crawl(app_env, monkeypatch):
    """A crawl that finishes at once with SITE's pages — or pauses, like a
    large site does at its estimate."""
    from app.job_store import get_job

    state = {"pause": False, "started": [], "resumed": []}

    async def fake_run_crawl(job_id, url, max_pages, *args, **kwargs):
        job = get_job(job_id)
        state["started"].append(url)
        job.status = "crawling"
        await asyncio.sleep(0.05)
        for page_url, title, words, repeated in SITE:
            job.pages[page_url] = PageResult(url=page_url, title=title, word_count=words, success=True,
                                             repeated_words=repeated)
        job.total_words = sum(w for _, _, w, _ in SITE)
        job.repetition.words_total = job.total_words
        job.repetition.words_repeated = 240
        if state["pause"] and kwargs.get("pause_at_words") is not None:
            job.status = "paused"
            job.estimate_result = {"estimated_total_words": 120_000, "total_pages_estimate": 410,
                                   "confidence": "medium", "estimated_duration_seconds": 600}
        else:
            job.status = "completed"
        await app_env.db.save_run(run_id=job.id, source_url=job.source_url, user_id=job.user_id, status=job.status,
                                  total_words=job.total_words, pages=list(job.pages.values()), limit_reached=False,
                                  repetition=job.repetition.summary())

    def fake_resume(job, exclusions=None):
        state["resumed"].append(job.id)
        job.status = "crawling"

    monkeypatch.setattr(app_env.main, "run_crawl", fake_run_crawl)
    monkeypatch.setattr(app_env.main, "_resume_job", fake_resume)
    return state


def test_count_words_counts_a_site_and_says_what_it_found(app_env, fake_crawl):
    key = make_key(app_env)["key"]
    result = call(app_env, key, "count_words", {"url": "example.test", "wait_seconds": 10})
    text = result["content"][0]["text"]
    data = result["structuredContent"]
    assert result["isError"] is False and data["status"] == "completed"
    assert data["words"] == 2300 and data["pages"] == 4
    assert "example.test: 2,300 words on 4 pages — every page counted." in text
    assert data["repetition"]["repeated"] == 240 and data["repetition"]["new"] == 2060
    assert {f["name"]: f["words"] for f in data["folders"]}["/blog"] == 1600
    assert {lang["name"] for lang in data["languages"]} == {"Default", "de"}
    assert data["links"]["report"].endswith(f"/crawl/{data['run_id']}") and data["links"]["report"] in text
    assert fake_crawl["started"] == ["https://example.test"]

    again = call(app_env, key, "count_words", {"url": "https://example.test/"})
    assert again["structuredContent"]["reused"] is True and len(fake_crawl["started"]) == 1, "the earlier count"
    assert "fresh=true" in again["content"][0]["text"]
    call(app_env, key, "count_words", {"url": "https://example.test/", "fresh": True, "wait_seconds": 5})
    assert len(fake_crawl["started"]) == 2

    runs = call(app_env, key, "list_word_counts")
    assert len(runs["structuredContent"]["counts"]) == 2
    blog = call(app_env, key, "get_pages", {"run_id": data["run_id"], "contains": "/blog", "limit": 1})
    assert blog["structuredContent"]["matching_pages"] == 2
    assert blog["structuredContent"]["pages"] == [{"url": "https://example.test/blog/one", "title": "One",
                                                   "words": 900, "repeated_words": 120}]


def test_someone_elses_private_count_is_never_reused(app_env, fake_crawl):
    save(app_env, run_id="theirs", source_url="https://example.test", user_id=2, total_words=99)
    key = make_key(app_env)["key"]
    result = call(app_env, key, "count_words", {"url": "https://example.test", "wait_seconds": 5})
    assert result["structuredContent"]["run_id"] != "theirs" and fake_crawl["started"]
    assert call(app_env, key, "get_word_count", {"run_id": "theirs"})["isError"] is True


def test_a_large_site_pauses_with_an_estimate_and_continues_when_asked(app_env, fake_crawl):
    fake_crawl["pause"] = True
    key = make_key(app_env)["key"]
    result = call(app_env, key, "count_words", {"url": "https://big.test", "wait_seconds": 5})
    data, text = result["structuredContent"], result["content"][0]["text"]
    assert data["status"] == "paused" and data["estimate"]["estimated_total_words"] == 120_000
    assert "about 120,000 words on about 410 pages (medium confidence)" in text
    assert "continue_word_count" in text

    resumed = call(app_env, key, "continue_word_count", {"run_id": data["run_id"]})
    assert resumed["structuredContent"]["status"] == "resuming" and fake_crawl["resumed"] == [data["run_id"]]
    assert "about 10 minutes" in resumed["content"][0]["text"]


def test_a_count_still_running_says_how_far_it_got(app_env, monkeypatch):
    from app.job_store import get_job

    async def slow_crawl(job_id, url, *args, **kwargs):
        job = get_job(job_id)
        job.status = "crawling"
        job.pages["https://slow.test/"] = PageResult(url="https://slow.test/", word_count=120, success=True)
        job.total_words = 120
        await asyncio.sleep(30)

    monkeypatch.setattr(app_env.main, "run_crawl", slow_crawl)
    key = make_key(app_env)["key"]
    result = call(app_env, key, "count_words", {"url": "https://slow.test", "wait_seconds": 1})
    data = result["structuredContent"]
    assert data["status"] == "crawling" and data["words"] == 120
    assert "still counting — 120 words on 1 pages so far" in result["content"][0]["text"]
    assert "get_word_count" in result["content"][0]["text"]
    get_job(data["run_id"]).task.cancel()


def test_tool_mistakes_are_explained(app_env, fake_crawl):
    key = make_key(app_env)["key"]
    assert call(app_env, key, "count_words", {})["isError"] is True
    bad = call(app_env, key, "count_words", {"url": "not a website"})
    assert bad["isError"] is True and "isn't a website address" in bad["content"][0]["text"]
    assert call(app_env, key, "get_word_count", {"run_id": "nope"})["isError"] is True
    res = rpc(app_env, "tools/call", {"name": "delete_everything", "arguments": {}}, key=key)
    assert res.json()["error"]["code"] == -32602


def test_a_paused_count_is_reported_once_its_estimate_is_ready(app_env, monkeypatch):
    """The crawl says "paused" a moment before it has worked out the estimate:
    the answer waits for it rather than saying there is none."""
    from app.job_store import get_job

    async def pausing_crawl(job_id, url, *args, **kwargs):
        job = get_job(job_id)
        job.pages["https://big.test/"] = PageResult(url="https://big.test/", word_count=50_100, success=True)
        job.total_words = 50_100
        job.status = "paused"
        await asyncio.sleep(0.4)          # the estimate: sitemaps, arithmetic
        job.estimate_result = {"estimated_total_words": 400_000, "total_pages_estimate": 900, "confidence": "high"}

    monkeypatch.setattr(app_env.main, "run_crawl", pausing_crawl)
    key = make_key(app_env)["key"]
    data = call(app_env, key, "count_words", {"url": "https://big.test", "wait_seconds": 5})["structuredContent"]
    assert data["status"] == "paused" and data["estimate"]["estimated_total_words"] == 400_000


def test_asking_twice_about_a_site_being_counted_follows_the_same_count(app_env, monkeypatch):
    from app.job_store import get_job

    started = []

    async def slow_crawl(job_id, url, *args, **kwargs):
        started.append(job_id)
        job = get_job(job_id)
        job.status = "crawling"
        await asyncio.sleep(30)

    monkeypatch.setattr(app_env.main, "run_crawl", slow_crawl)
    key = make_key(app_env)["key"]
    first = call(app_env, key, "count_words", {"url": "https://slow.test", "wait_seconds": 0})["structuredContent"]
    second = call(app_env, key, "count_words", {"url": "slow.test/", "wait_seconds": 0})["structuredContent"]
    assert second["run_id"] == first["run_id"] and len(started) == 1
    third = call(app_env, key, "count_words", {"url": "https://slow.test", "fresh": True, "wait_seconds": 0})
    assert third["structuredContent"]["run_id"] != first["run_id"], "unless asked to count again"
    for run_id in (first["run_id"], third["structuredContent"]["run_id"]):
        get_job(run_id).task.cancel()


def test_a_count_can_be_kept_to_one_host(app_env, fake_crawl, monkeypatch):
    from app.job_store import get_job

    scopes = []
    original = app_env.main.run_crawl

    async def remember_scope(job_id, url, max_pages, domain_scope="all", *args, **kwargs):
        scopes.append(domain_scope)
        return await original(job_id, url, max_pages, domain_scope, *args, **kwargs)

    monkeypatch.setattr(app_env.main, "run_crawl", remember_scope)
    key = make_key(app_env)["key"]
    whole = call(app_env, key, "count_words", {"url": "https://example.test", "wait_seconds": 5})["structuredContent"]
    assert whole["scope"] == "whole_domain"
    one = call(app_env, key, "count_words", {"url": "https://example.test", "scope": "this_subdomain",
                                             "wait_seconds": 5})["structuredContent"]
    assert one["scope"] == "this_subdomain" and one["run_id"] != whole["run_id"], "not the whole-domain count"
    assert scopes == ["all", "subdomain_only"]
    bad = call(app_env, key, "count_words", {"url": "https://example.test", "scope": "galaxy"})
    assert bad["isError"] is True
