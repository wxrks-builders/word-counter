"""Consolidating a project: only the new words, and what overlapped.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_app_flows import app_env, run, save  # noqa: E402,F401

from app import consolidation, consolidation_job, segment_store  # noqa: E402
from app.consolidation import EXACT, MARKDOWN, PAGE_ONLY, Page, Run, apportion, consolidate  # noqa: E402
from app.models import PageResult  # noqa: E402
from app.repetition import page_segments  # noqa: E402

CTA = "Book your stay today and save on every night."
TEXTS = {
    "https://stg.x.test/": f"Welcome to the hotel by the sea. {CTA}",
    "https://stg.x.test/rooms": f"Sea view rooms with a balcony. {CTA}",
    "https://www.x.test/": f"Welcome to the hotel by the sea. {CTA}",
    "https://www.x.test/spa": f"Our spa is calm and quiet. {CTA}",
    "https://brand.test/terms": "Sea view rooms with a balcony. Terms apply to every booking made online.",
    "https://old.test/": "An old page nobody kept the sentences of.",
}


def words(url: str) -> int:
    return len(TEXTS[url].split())


def page(url: str, digest: str | None = None) -> Page:
    return Page(url, None, words(url), True, content_hash=digest)


def crawl(run_id, source, created, urls, status="completed") -> Run:
    return Run(id=run_id, source_url=source, shown_url=source, created_at=created, status=status,
               pages=[page(u) for u in urls])


STG = crawl("stg", "https://stg.x.test", "2026-10-02", ["https://stg.x.test/", "https://stg.x.test/rooms"])
WWW = crawl("www", "https://www.x.test", "2026-10-01", ["https://www.x.test/", "https://www.x.test/spa"])
BRAND = crawl("brand", "https://brand.test", "2026-10-03", ["https://brand.test/terms"], status="cancelled")
OLD = crawl("old", "https://old.test", "2026-09-01", ["https://old.test/"])


def loader(sources: dict[str, str]):
    """Sentences for each run from TEXTS, as EXACT unless the run is listed."""
    def load(run, urls):
        source = sources.get(run.id, EXACT)
        if source == PAGE_ONLY:
            return {}
        out = {}
        for url in urls:
            segs = page_segments(TEXTS[url])
            out[url] = ([k for _, k, _ in segs], [n for _, _, n in segs], source)
        return out
    return load


def texts(run, fingerprints):
    found = {}
    for text in TEXTS.values():
        for segment, key, _ in page_segments(text):
            if key in fingerprints:
                found[key] = segment
    return found


def check_sums(result: dict) -> None:
    t = result["totals"]
    assert t["added_up"] - t["same_pages"] - t["repeated_within"] - t["repeated_across"] == t["new_words"]
    assert t["each_page_once"] == t["added_up"] - t["same_pages"]
    counted = [r for r in result["runs"] if r["state"] in ("counted", "gap_filler")]
    for row in counted:
        assert row["words"] == row["same_page_words"] + row["repeated_within"] + row["repeated_across"] + row["new_words"]
    for key in ("repeated_within", "repeated_across", "new_words"):
        assert sum(r[key] for r in counted) == t[key]


def test_every_page_once_and_every_sentence_once():
    done = consolidate([STG, WWW, BRAND], loader({}), texts)
    result = done.result
    check_sums(result)
    rows = {r["id"]: r for r in result["runs"]}
    cta = len(CTA.split())
    assert result["order"] == ["stg", "www", "brand"]
    # stg: its home is new; the CTA again on its rooms page is a repeat within it.
    assert rows["stg"]["repeated_within"] == cta and rows["stg"]["repeated_across"] == 0
    # www: its home is the same page as stg's (same address, same site); its spa
    # page repeats the CTA stg already has.
    assert rows["www"]["same_page_words"] == words("https://www.x.test/")
    assert rows["www"]["same_page_in"] == [{"id": "stg", "pages": 1, "words": words("https://www.x.test/")}]
    assert rows["www"]["repeated_across"] == cta
    # brand didn't finish: it fills gaps, and its first sentence is stg's.
    assert rows["brand"]["state"] == "gap_filler" and rows["brand"]["repeated_across"] == 6
    unique = {s for text in TEXTS.values() for s, _, _ in page_segments(text)} - {"An old page nobody kept the sentences of."}
    assert result["totals"]["new_words"] == sum(len(s.split()) for s in unique), "each distinct sentence once"


def test_what_overlapped_lists_the_pages_and_the_sentences():
    result = consolidate([STG, WWW, BRAND], loader({}), texts).result
    assert result["overlap_pages"]["count"] == 1
    top = result["overlap_pages"]["top"][0]
    assert top["counted_from"] == "stg" and top["copies"][0]["run"] == "www"
    sentences = {s["text"]: s for s in result["sentences"]["top"]}
    assert sentences[CTA]["times"] == 3 and sentences[CTA]["runs"] == ["stg", "www"]
    assert sentences[CTA]["removed"] == 2 * len(CTA.split())
    assert sentences["Sea view rooms with a balcony."]["runs"] == ["stg", "brand"]
    pairs = {(p["a"], p["b"]): p for p in result["pairs"]}
    assert pairs[("stg", "www")]["pages"] == 1 and pairs[("stg", "www")]["sentence_words"] == len(CTA.split())
    assert pairs[("stg", "brand")]["pages"] == 0 and pairs[("stg", "brand")]["sentence_words"] == 6


def test_the_csv_rows_add_up_to_the_same_totals():
    done = consolidate([STG, WWW, BRAND], loader({}), texts)
    t = done.result["totals"]
    assert sum(r[3] for r in done.page_rows) == t["added_up"]
    assert sum(r[8] for r in done.page_rows) == t["new_words"]
    copy = next(r for r in done.page_rows if r[0] == "https://www.x.test/")
    assert copy[4] == "https://stg.x.test" and copy[8] == 0 and copy[9] == "same_page"
    assert done.sentence_rows[0][0] == CTA


def test_markdown_sentences_are_scaled_to_the_counted_words():
    assert apportion(110, MARKDOWN, 100, 30, 20) == (33, 22, 55, MARKDOWN)
    assert apportion(110, MARKDOWN, 0, 0, 0) == (0, 0, 110, PAGE_ONLY), "no sentences: the page counts in full"
    assert apportion(5, EXACT, 8, 6, 2) == (5, 0, 0, EXACT), "never more than the page's own count"


def test_an_exact_crawl_and_a_markdown_one_recognise_the_same_sentence():
    result = consolidate([STG, WWW], loader({"www": MARKDOWN}), texts).result
    check_sums(result)
    rows = {r["id"]: r for r in result["runs"]}
    assert rows["www"]["coverage"][MARKDOWN] > 0 and rows["www"]["repeated_across"] > 0
    assert result["notes"]["markdown_runs"] == ["www"]


def test_a_crawl_with_nothing_kept_is_checked_by_page_only():
    result = consolidate([STG, OLD], loader({"old": PAGE_ONLY}), texts).result
    check_sums(result)
    rows = {r["id"]: r for r in result["runs"]}
    assert rows["old"]["new_words"] == words("https://old.test/") and rows["old"]["coverage"][PAGE_ONLY]
    assert result["notes"]["page_only_runs"] == ["old"]


def test_reruns_and_split_comparisons_give_the_same_result(monkeypatch):
    first = consolidate([STG, WWW, BRAND], loader({}), texts).result
    assert consolidate([STG, WWW, BRAND], loader({}), texts).result == first
    monkeypatch.setattr(consolidation, "PARTITION_SIZE", 1)
    assert consolidate([STG, WWW, BRAND], loader({}), texts).result == first


def test_crawls_still_running_and_replaced_ones_are_listed_but_not_compared():
    running = Run(id="now", source_url="https://new.test", shown_url="https://new.test", created_at="2026-10-09",
                  status="crawling", total_words=999)
    older = crawl("older", "https://stg.x.test", "2026-09-01", ["https://stg.x.test/"])
    result = consolidate([STG, running, older], loader({}), texts).result
    rows = {r["id"]: r for r in result["runs"]}
    assert rows["now"]["state"] == "in_progress" and rows["now"]["new_words"] == 0
    assert rows["older"]["state"] == "superseded"
    assert result["order"] == ["stg"]
    check_sums(result)


def test_pages_mostly_repeated_from_another_crawl_are_named():
    a = Run(id="a", source_url="https://a.test", shown_url="https://a.test", created_at="2026-10-02", status="completed",
            pages=[Page("https://a.test/legal", None, 40, True)])
    b = Run(id="b", source_url="https://b.test", shown_url="https://b.test", created_at="2026-10-01", status="completed",
            pages=[Page("https://b.test/terms", None, 44, True)])
    legal = [f"Clause {n} applies to every booking made on this site." for n in range(4)]
    segs = {"https://a.test/legal": legal, "https://b.test/terms": legal + ["Brand B only."]}

    def load(run, urls):
        return {u: ([page_segments(s)[0][1] for s in segs[u]], [len(s.split()) for s in segs[u]], EXACT) for u in urls}

    result = consolidate([a, b], load, lambda run, fps: {}).result
    mostly = result["overlap_pages"]["mostly_repeated"]
    assert mostly == [{"url": "https://b.test/terms", "run": "b", "words": 44, "across": 40, "from": ["a"]}]
    assert result["sentences"]["top"][0]["text_known"] is False


# ------------------------------------------------------------------ the job

def _project_with(env, *run_ids) -> str:
    res = env.client.post("/projects", json={"name": "Acme", "run_ids": list(run_ids)})
    return res.json()["id"]


def _now():
    return consolidation_job._now()


def test_a_project_is_consolidated_from_the_files_its_crawls_kept(app_env):
    from app import markdown_store

    # stg kept its sentences while crawling; www only saved Markdown.
    stg_pages = [PageResult(url=u, word_count=words(u), success=True) for u in ("https://stg.x.test/", "https://stg.x.test/rooms")]
    writer = segment_store.Writer("stg")
    for p in stg_pages:
        writer.add_page(p.url, page_segments(TEXTS[p.url]), range(len(page_segments(TEXTS[p.url]))))
    writer.flush()
    save(app_env, run_id="stg", source_url="https://stg.x.test", pages=stg_pages, total_words=sum(p.word_count for p in stg_pages))
    www_pages = [PageResult(url="https://www.x.test/spa", word_count=words("https://www.x.test/spa"), success=True)]
    markdown_store.write("www", "https://www.x.test/spa", TEXTS["https://www.x.test/spa"])
    save(app_env, run_id="www", source_url="https://www.x.test", pages=www_pages, total_words=www_pages[0].word_count,
         capture_markdown=True, markdown_pages=1)

    project_id = _project_with(app_env, "stg", "www")
    row = run(app_env.db.get_consolidation(project_id))
    assert row["status"] == "done" and row["computed_gen"] == row["requested_gen"]
    result = row["result"]
    rows = {r["id"]: r for r in result["runs"]}
    assert rows["stg"]["coverage"]["exact"] and rows["www"]["coverage"]["markdown"]
    t = result["totals"]
    assert t["repeated_within"] + t["repeated_across"] == 2 * len(CTA.split()), "the CTA, three times: two repeats"
    assert t["repeated_across"] >= len(CTA.split()), "recognised across the two kinds of crawl"
    assert segment_store.has("www", segment_store.MARKDOWN), "rebuilt once and kept"
    project = run(app_env.db.get_project(project_id))
    assert project["cached_new_words"] == result["totals"]["new_words"]

    body = app_env.client.get(f"/projects/{project_id}").text
    assert "Sentences repeated across crawls" in body and CTA in body
    status = app_env.client.get(f"/projects/{project_id}/consolidation").json()
    assert status["status"] == "done" and status["computed_gen"] == result["generation"]


def test_viewing_an_unchanged_project_doesnt_consolidate_it_again(app_env):
    save(app_env, run_id="a", source_url="https://a.test")
    project_id = _project_with(app_env, "a")
    gen = run(app_env.db.get_consolidation(project_id))["requested_gen"]
    app_env.client.get(f"/projects/{project_id}")
    assert run(app_env.db.get_consolidation(project_id))["requested_gen"] == gen
    assert app_env.client.post(f"/projects/{project_id}/consolidate").json()["computed_gen"] == gen + 1, "the button always does"
    save(app_env, run_id="b", source_url="https://b.test")
    app_env.client.post(f"/projects/{project_id}/runs", json={"run_ids": ["b"]})
    assert run(app_env.db.get_consolidation(project_id))["computed_gen"] == gen + 2, "a crawl added"


def test_a_request_while_running_queues_it_again(app_env):
    save(app_env, run_id="a", source_url="https://a.test")
    project_id = _project_with(app_env, "a")
    db = app_env.db
    run(db.request_consolidation(project_id, "button", "x"))
    claimed = run(db.claim_consolidation("me", _now(), "0", project_id))
    assert claimed is not None
    project_id, gen, _ = claimed
    run(db.request_consolidation(project_id, "again", "x"))
    assert run(db.get_consolidation(project_id))["status"] == "running"
    assert run(db.finish_consolidation(project_id, "me", gen, {"totals": {}}, "x"))
    assert run(db.get_consolidation(project_id))["status"] == "queued", "the newer request still to do"


def test_a_lost_lease_writes_nothing_and_a_stale_one_is_taken_over(app_env):
    save(app_env, run_id="a", source_url="https://a.test")
    project_id = _project_with(app_env, "a")
    db = app_env.db
    before = run(db.get_consolidation(project_id))["result"]
    run(db.request_consolidation(project_id, "again", "y"))
    _, gen, _ = run(db.claim_consolidation("first", _now(), "0", project_id))
    assert run(db.claim_consolidation("second", _now(), "0", project_id)) is None, "its lease is fresh"
    taken = run(db.claim_consolidation("second", _now(), "9999", project_id))
    assert taken is not None and taken[2] == 1, "stale: taken over, and counted as a resume"
    assert run(db.heartbeat_consolidation(project_id, "first", None)) is None
    assert not run(db.finish_consolidation(project_id, "first", gen, {"totals": {}}, "y"))
    assert run(db.get_consolidation(project_id))["result"] == before


def test_a_failure_keeps_the_last_result(app_env, monkeypatch):
    save(app_env, run_id="a", source_url="https://a.test")
    project_id = _project_with(app_env, "a")
    good = run(app_env.db.get_consolidation(project_id))["result"]

    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(consolidation, "consolidate", boom)
    status = app_env.client.post(f"/projects/{project_id}/consolidate").json()
    assert status["status"] == "failed" and "disk on fire" in status["error"]
    row = run(app_env.db.get_consolidation(project_id))
    assert row["result"] == good
    assert "The last consolidation didn’t finish" in app_env.client.get(f"/projects/{project_id}").text
    app_env.client.get(f"/projects/{project_id}")
    assert run(app_env.db.get_consolidation(project_id))["status"] == "failed", "not retried for the same crawls"


def test_too_many_resumes_give_up(app_env):
    save(app_env, run_id="a", source_url="https://a.test")
    project_id = _project_with(app_env, "a")
    run(app_env.db.request_consolidation(project_id, "again", "z"))
    gen = None
    for _ in range(consolidation_job.MAX_RESUMES + 2):
        project_id, gen, resumed = run(app_env.db.claim_consolidation(consolidation_job.OWNER, _now(), "9999", project_id))
    run(consolidation_job._run_guarded(project_id, gen, resumed))
    row = run(app_env.db.get_consolidation(project_id))
    assert row["status"] == "failed" and "restarted" in row["error"]


def test_a_project_deleted_mid_way_is_left_alone(app_env):
    save(app_env, run_id="a", source_url="https://a.test")
    project_id = _project_with(app_env, "a")
    assert consolidation_job.csv_path(project_id, "pages").exists()
    app_env.client.delete(f"/projects/{project_id}")
    assert run(app_env.db.get_consolidation(project_id)) is None
    assert not consolidation_job.csv_path(project_id, "pages").exists()
    run(consolidation_job.run(project_id, 1))       # nothing to do, nothing raised


@pytest.mark.parametrize("too_many", [True])
def test_a_project_too_large_for_the_server_says_so(app_env, monkeypatch, too_many):
    monkeypatch.setattr(consolidation_job, "MAX_PAGES", 0)
    save(app_env, run_id="a", source_url="https://a.test")
    project_id = _project_with(app_env, "a")
    row = run(app_env.db.get_consolidation(project_id))
    assert row["status"] == "failed" and "Too large" in row["error"]
