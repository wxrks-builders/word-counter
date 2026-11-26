"""Screenshots for the translation check: a page in one language, with the
text that wasn't translated outlined in red.

Playwright's own Chromium, the one crawl4ai installs, driven directly: this
needs a page's pixels and a little script, none of crawl4ai's crawling. Files
live beside the database, one folder per check, and go when the check's
detail is pruned.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import shutil
from pathlib import Path

from app import db

logger = logging.getLogger(__name__)

SHOTS_DIR = Path(os.environ.get("TRANSLATION_SHOTS_DIR") or (db.DB_PATH.parent / "translation-shots"))
WIDTH = 1280
# Long pages are cut here: the top of a page is where a reader decides.
MAX_HEIGHT = 9000
# Browser pages at once. Each is a real page load with its scripts.
AT_ONCE = 2
FILE_NAME = re.compile(r"^[0-9a-f]{20}\.jpg$")

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Scrolls the page through once: Webflow and most site builders reveal
# sections as they scroll into view, and load images lazily.
_SCROLL_JS = """async () => {
  const end = Math.min(document.documentElement.scrollHeight, %d);
  for (let y = 0; y < end; y += Math.round(innerHeight * 0.8)) {
    scrollTo(0, y);
    await new Promise(r => setTimeout(r, 120));
  }
  scrollTo(0, 0);
}""" % MAX_HEIGHT

# Outlines every element whose text is one of the untranslated texts, and
# images whose alt text wasn't translated. The keys come normalised the way
# translation_check.normalize_key does it; this mirrors that. Returns how many
# outlined elements are visible.
_MARK_JS = r"""({texts, alts}) => {
  const norm = s => s.normalize('NFC')
    .replace(/[­​-‍⁠﻿]/g, '').replace(/ /g, ' ')
    .replace(/[‘’‚‛]/g, "'").replace(/[“”„‟]/g, '"')
    .replace(/\s+/g, ' ').trim().toLowerCase()
    .replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, '');
  const wanted = new Set(texts), wantedAlts = new Set(alts);
  const skip = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'svg', 'SVG', 'HEAD']);
  const found = [];
  for (const el of document.body.querySelectorAll('*')) {
    if (skip.has(el.tagName)) continue;
    const text = el.innerText;
    if (!text || text.length > 3000) continue;
    if (wanted.has(norm(text))) found.push(el);
  }
  // The innermost element carrying the text, not every wrapper around it.
  const marked = found.filter(el => !found.some(other => other !== el && el.contains(other)));
  for (const img of document.querySelectorAll('img[alt]')) {
    if (wantedAlts.has(norm(img.alt))) marked.push(img);
  }
  let visible = 0;
  for (const el of marked) {
    el.style.setProperty('outline', '3px solid #E5484D', 'important');
    el.style.setProperty('outline-offset', '2px', 'important');
    el.style.setProperty('box-shadow', 'inset 0 0 0 9999px rgba(229,72,77,0.10)', 'important');
    const box = el.getBoundingClientRect();
    if (box.width > 0 && box.height > 0) visible += 1;
  }
  return visible;
}"""


class BrowserUnavailable(Exception):
    pass


def file_for(check_id: int, source_url: str, locale: str) -> str:
    return hashlib.blake2b(f"{check_id}|{source_url}|{locale}".encode(), digest_size=10).hexdigest() + ".jpg"


def path_for(check_id: int, name: str) -> Path | None:
    if not FILE_NAME.match(name):
        return None
    return SHOTS_DIR / str(check_id) / name


def delete(check_id: int) -> None:
    shutil.rmtree(SHOTS_DIR / str(check_id), ignore_errors=True)


# Hidden after the first screen, so a fixed header or chat button isn't
# repeated down the stitched image.
_HIDE_FIXED_JS = """() => {
  for (const el of document.querySelectorAll('body *')) {
    if (getComputedStyle(el).position === 'fixed') el.style.setProperty('visibility', 'hidden', 'important');
  }
}"""
VIEWPORT_HEIGHT = 900


async def _shoot(browser, job: dict, path: Path) -> int:
    """One screen at a time, scrolling down, stitched together.

    A single full-page capture never scrolls, and sites that reveal sections
    as they scroll into view (Webflow does, by default) come out black below
    the first screen. Each screen gets a moment for its section to appear.
    """
    from PIL import Image

    context = await browser.new_context(viewport={"width": WIDTH, "height": VIEWPORT_HEIGHT},
                                        user_agent=_USER_AGENT)
    try:
        page = await context.new_page()
        await page.goto(job["url"], wait_until="domcontentloaded", timeout=45_000)
        try:
            # Pricing pages with a video embed never fire "load" in time;
            # what's on the page is there by now regardless.
            await page.wait_for_load_state("load", timeout=15_000)
        except Exception:
            pass
        await page.evaluate(_SCROLL_JS)
        await page.wait_for_timeout(500)
        marks = await page.evaluate(_MARK_JS, {"texts": job["texts"], "alts": job["alts"]})
        height = await page.evaluate(f"Math.min(document.documentElement.scrollHeight, {MAX_HEIGHT})")
        height = max(VIEWPORT_HEIGHT, int(height))

        canvas = Image.new("RGB", (WIDTH, height), "white")
        y = 0
        while y < height:
            top = min(y, height - VIEWPORT_HEIGHT)
            await page.evaluate(f"window.scrollTo(0, {top})")
            await page.wait_for_timeout(450)
            shot = await page.screenshot(type="png")
            tile = Image.open(io.BytesIO(shot)).convert("RGB")
            canvas.paste(tile, (0, top))
            if y == 0:
                await page.evaluate(_HIDE_FIXED_JS)
            y += VIEWPORT_HEIGHT
        canvas.save(path, "JPEG", quality=60, optimize=True)
        return marks
    finally:
        await context.close()


async def capture(check_id: int, jobs: list[dict], save, wait_for_memory) -> None:
    """Takes each job's screenshot and hands the result to `save(job, file,
    marks, error)`. An exception from `save` (cancelled, lease lost) stops
    them all and propagates; one page failing to load is just recorded."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserUnavailable("Playwright isn't installed") from exc

    folder = SHOTS_DIR / str(check_id)
    folder.mkdir(parents=True, exist_ok=True)
    queue: asyncio.Queue[dict] = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        except Exception as exc:
            raise BrowserUnavailable(str(exc).splitlines()[0][:200]) from exc

        async def worker():
            while True:
                try:
                    job = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await wait_for_memory()
                name = file_for(check_id, job["source_url"], job["locale"])
                try:
                    marks = await _shoot(browser, job, folder / name)
                except Exception as exc:
                    logger.info("Screenshot of %s failed: %s", job["url"], exc)
                    await save(job, None, 0, f"Couldn't load the page ({type(exc).__name__})")
                    continue
                await save(job, name, marks, None)

        workers = [asyncio.create_task(worker()) for _ in range(min(AT_ONCE, len(jobs)))]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        finally:
            await browser.close()
