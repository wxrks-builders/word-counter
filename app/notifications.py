from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from markupsafe import Markup

from app import i18n, report, surfaces
from app.i18n import N_
from app.templates import _install_language, templates

logger = logging.getLogger(__name__)

MAILGUN_API_KEY = os.environ.get("MAILGUN_API_KEY")
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN")
MAILGUN_FROM = os.environ.get("MAILGUN_FROM") or (
    f"Word Counter <noreply@{MAILGUN_DOMAIN}>" if MAILGUN_DOMAIN else None
)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# Where emails should point when PUBLIC_BASE_URL isn't configured. A link in an
# email has no page to be relative to, so "/share/abc" is simply a dead link —
# previously that's what recipients got. The app serves behind --proxy-headers,
# so request.base_url is the real external origin; remembering the most recent
# one lets background jobs (which have no request) still build a working link.
_observed_origin = ""


def remember_origin(origin: str) -> None:
    global _observed_origin
    origin = (origin or "").rstrip("/")
    if origin and origin != _observed_origin:
        _observed_origin = origin
        if not PUBLIC_BASE_URL:
            logger.warning(
                "PUBLIC_BASE_URL is not set — falling back to the observed origin %s for email links. "
                "Set PUBLIC_BASE_URL so links don't depend on which host was hit most recently.",
                origin,
            )


def base_url() -> str:
    return PUBLIC_BASE_URL or _observed_origin


def absolute_url(path: str, surface=None) -> str | None:
    """An absolute link, or None if we genuinely can't build one — callers omit
    the button rather than render a relative href that goes nowhere.

    Pass a surface to link back to the front door the reader actually used. Both
    products are one app on two hostnames, so without this someone who signed up
    on the Markdown domain would get email pointing at the word counter.
    """
    base = surface.origin if surface is not None else base_url()
    if not base:
        logger.error("Cannot build an absolute URL for %s: no PUBLIC_BASE_URL and no request seen yet", path)
        return None
    return f"{base}/{path.lstrip('/')}"


# Marked where they are written, translated at send time — see N_ in app/i18n.py.
_STATUS_SUBJECTS = {
    # Translators: the heading of the email sent when a crawl ends, and the
    # start of its subject line.
    "completed": N_("Crawl finished"),
    "failed": N_("Crawl failed"),
    "cancelled": N_("Crawl stopped"),
}

# The estimate's confidence, as the crawler records it ("high", ...). Spelled
# out here rather than built with _(confidence.capitalize()), which pybabel
# can't see and so would never reach a translator.
_CONFIDENCE = {
    # Translators: how sure the word-count estimate is, shown in the email as
    # "Estimate confidence: High".
    "high": N_("High"),
    "medium": N_("Medium"),
    "low": N_("Low"),
}

# Scripts without capitals, and Arabic, whose letters join: spacing letters
# apart breaks the joins, and uppercasing does nothing. The email's small
# labels are only tracked and capitalised in the others.
_NO_TRACKING = frozenset({"ar", "ja", "ko", "zh"})


def _num(value: int, lang: str) -> str:
    """Grouped for the reader, not the developer — the same rule the pages use."""
    return i18n.format_number(value or 0, lang)


def _fmt(n: int) -> str:
    """For the admin emails only, which are English by design."""
    return f"{n:,}"


def _link(path: str, lang: str, surface=None) -> str | None:
    """An absolute link to a page, in the reader's language."""
    return absolute_url(i18n.localized_path(path, lang), surface)


def _counts(words: int, pages: int, lang: str) -> dict[str, str]:
    """"1,234 words" and "56 pages", each in the plural form its number takes,
    for a sentence that names both. gettext pluralises on one number per
    message, so each noun phrase is its own message and the sentence around
    them is another, which keeps the word order the translator's."""
    ngettext = i18n.ngettext_for(lang)
    return {
        # Translators: a word count, as part of a longer sentence.
        "words": ngettext("%(n)s word", "%(n)s words", words or 0) % {"n": _num(words, lang)},
        # Translators: a page count, as part of a longer sentence.
        "pages": ngettext("%(n)s page", "%(n)s pages", pages or 0) % {"n": _num(pages, lang)},
    }


def _absolute_promo_logo(promo: dict | None, surface) -> dict | None:
    """A mail client has no origin to resolve a relative path against, so the
    logo has to be absolute by the time the template sees it."""
    if not promo or not promo.get("logo_email_url"):
        return promo
    return {**promo, "logo_email_url": absolute_url(promo["logo_email_url"], surface)}


def email_configured() -> bool:
    return bool(MAILGUN_API_KEY and MAILGUN_DOMAIN)


async def _send_email(to_email: str, subject: str, surface=None, lang: str = i18n.DEFAULT_LANG, **context) -> None:
    if not email_configured():
        # Logged rather than returning in silence: "the email never arrived" is
        # otherwise indistinguishable from "the email was never attempted", and
        # the second one is a five-second fix nobody can see.
        logger.info(
            "Email not sent to %s (%s) — MAILGUN_API_KEY/MAILGUN_DOMAIN not set",
            to_email, subject,
        )
        return

    surface = surface or surfaces.DEFAULT
    _ = i18n.gettext_for(lang)
    rtl = lang in i18n.RTL_LANGUAGES
    # One Jinja environment serves every language, so the catalogue is bound
    # immediately before rendering rather than once at import. `lang` also
    # reaches the template, where the duration filter reads it.
    _install_language(lang)
    html = templates.env.get_template("email_notification.html").render(
        logo_url=absolute_url("/static/brand/logo-email.png", surface),
        product_name=surface.name,
        lang=lang,
        html_lang=i18n.HREFLANG.get(lang, lang),
        dir="rtl" if rtl else "ltr",
        # Mail clients ignore logical CSS, so the two sides are named per
        # direction: start is where a line begins.
        start="right" if rtl else "left",
        end="left" if rtl else "right",
        arrow=Markup("&larr;" if rtl else "&rarr;"),
        tracking=lang not in _NO_TRACKING,
        num=lambda value: _num(value, lang),
        **context,
    )

    # Plain-text alternative — some clients show it, and spam filters like
    # seeing one that actually matches the HTML.
    # Translators: one line of the plain-text email: a label and its value
    # ("Total words: 1,234"). Keep both placeholders.
    pair = _("%(label)s: %(value)s")
    lines: list[str] = [context["heading"]]
    if context.get("subheading"):
        lines.append(context["subheading"])
    if context.get("intro_text"):
        lines.append(context["intro_text"])
    for value, label in context.get("hero_stats") or []:
        lines.append(pair % {"label": label, "value": value})
    for label, value in context.get("stats") or []:
        lines.append(pair % {"label": label, "value": value})
    if context.get("notice"):
        lines.append(context["notice"])
    if context.get("cta_url"):
        lines.append(pair % {"label": context.get("cta_label") or _("View report"), "value": context["cta_url"]})
    text = "\n\n".join(lines)

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages",
                auth=("api", MAILGUN_API_KEY),
                data={"from": MAILGUN_FROM, "to": to_email, "subject": subject, "text": text, "html": html},
                timeout=10,
            )
    except Exception:
        # Best-effort — a notification failure should never affect the crawl
        # itself, which has already finished and saved its results by now.
        logger.exception("Failed to send %r to %s", subject, to_email)


async def send_crawl_notification(
    to_email: str,
    source_url: str,
    status: str,
    total_words: int,
    page_count: int,
    run_id: str,
    error: str | None = None,
    detected_cms: str | None = None,
    confidence: str | None = None,
    surface=None,
    lang: str = i18n.DEFAULT_LANG,
    duration_seconds: int = 0,
    crawl_concurrency: int = 0,
    is_pro: bool = False,
) -> None:
    from app.billing import billing_enabled
    from app.promos import email_promo

    surface = surface or surfaces.DEFAULT
    _ = i18n.gettext_for(lang)
    heading = _(_STATUS_SUBJECTS.get(status, N_("Crawl update")))
    completed = status == "completed"

    # At most one, and only when it's true of this crawl — see app/promos.py.
    # Imported inside the function because app.promos reaches app.surfaces and
    # app.plans, and this module is imported by the crawler at startup.
    promo = email_promo(
        source_url=source_url,
        total_words=total_words,
        status=status,
        surface=surface,
        duration_seconds=duration_seconds,
        crawl_concurrency=crawl_concurrency,
        billing_enabled=billing_enabled(),
        is_pro=is_pro,
        detected_cms=detected_cms,
    )

    hero_stats: list[tuple[str, str]] = []
    stats: list[tuple[str, str]] = []
    notice = None

    if completed:
        intro_text = _("Your crawl finished. Here's what it counted.")
        hero_stats = [(_num(total_words, lang), _("Total words")), (_num(page_count, lang), _("Pages counted"))]
        if detected_cms:
            stats.append((_("Detected platform"), detected_cms))
        if confidence in _CONFIDENCE:
            stats.append((_("Estimate confidence"), _(_CONFIDENCE[confidence])))
    elif status == "cancelled":
        intro_text = _("This crawl was stopped before it finished, so the report shows partial results.")
        hero_stats = [(_num(total_words, lang), _("Words so far")), (_num(page_count, lang), _("Pages so far"))]
        if error:
            # Stored in English by the crawler, which has no reader; the fixed
            # sentences translate here (report.stored_text).
            notice = report.stored_text(error, lang)
    else:
        intro_text = _("This crawl couldn't be completed.")
        notice = report.stored_text(error, lang) if error else _("No further detail was recorded.")

    await _send_email(
        to_email,
        # Translators: the crawl email's subject line. %(status)s is "Crawl
        # finished", "Crawl failed" or "Crawl stopped"; %(site)s is the address
        # that was crawled.
        subject=_("%(status)s: %(site)s") % {"status": heading, "site": source_url},
        preheader=(
            # Translators: the inbox preview of the crawl-finished email.
            # %(words)s is "1,234 words" and %(pages)s is "56 pages", each
            # already in the plural form its number needs.
            _("%(words)s across %(pages)s") % _counts(total_words, page_count, lang)
            if completed
            else intro_text
        ),
        heading=heading,
        subheading=source_url,
        intro_text=intro_text,
        hero_stats=hero_stats,
        stats=stats,
        notice=notice,
        notice_tone="danger" if status == "failed" else "warn",
        promo=_absolute_promo_logo(promo, surface),
        pricing_url=_link("/pricing", lang, surface),
        cta_url=_link(f"/crawl/{run_id}", lang, surface),
        cta_label=_("View full report"),
        # Translators: %(product)s is the product's name, such as "Word Counter".
        footer_note=_("You're getting this because you started this crawl on %(product)s.") % {"product": _(surface.name)},
        surface=surface,
        lang=lang,
    )


async def send_share_notification(
    to_email: str,
    shared_by: str,
    source_url: str,
    share_url: str,
    total_words: int | None = None,
    page_count: int | None = None,
    surface=None,
    detected_cms: str | None = None,
    lang: str = i18n.DEFAULT_LANG,
) -> None:
    from app.promos import share_email_promo

    _ = i18n.gettext_for(lang)
    surface = surface or surfaces.DEFAULT
    hero_stats = []
    if total_words is not None and page_count is not None:
        hero_stats = [(_num(total_words, lang), _("Total words")), (_num(page_count, lang), _("Pages counted"))]

    await _send_email(
        to_email,
        # Translators: the subject of the email inviting someone to a report.
        # %(person)s is the sender's name, or their email address.
        subject=_("%(person)s shared a word-count report with you") % {"person": shared_by},
        # Translators: the inbox preview of that email. %(site)s is a web address.
        preheader=_("A word count for %(site)s") % {"site": source_url},
        heading=_("%(person)s shared a report with you") % {"person": shared_by},
        subheading=source_url,
        intro_text=_("They ran a word count on this site and gave you access to the full report."),
        hero_stats=hero_stats,
        stats=[],
        # The recipient isn't a user, which is exactly who this pitch is for.
        promo=_absolute_promo_logo(
            share_email_promo(source_url, total_words, surface, detected_cms), surface),
        pricing_url=_link("/pricing", lang, surface),
        cta_url=share_url,
        cta_label=_("View report"),
        # Translators: %(product)s is the product's name, such as "Word Counter".
        footer_note=_("You're getting this because someone shared a %(product)s report with you.") % {"product": _(surface.name)},
        surface=surface,
        lang=lang,
    )


def admin_emails() -> list[str]:
    """Who gets server alerts. Same list that gates the admin pages, so there is
    one place to add someone rather than two that can disagree."""
    raw = os.environ.get("ADMIN_EMAILS", "")
    return [e.strip() for e in raw.split(",") if e.strip()]


async def send_server_alert(heading: str, intro_text: str, label: str, value: str) -> None:
    """Tells the admins something about the server, not about a crawl.

    Best effort throughout: an alert that raises would take down the periodic
    task that produces every future alert, which is the worst possible failure
    for a thing whose job is to notice failures.
    """
    recipients = admin_emails()
    if not recipients:
        logger.info("Server alert not sent (no ADMIN_EMAILS): %s — %s", heading, value)
        return
    for email in recipients:
        try:
            await _send_email(
                to_email=email,
                subject=f"[{surfaces.DEFAULT.name}] {heading}",
                heading=heading,
                intro_text=intro_text,
                # As a stats row: the template has no slot for a bare
                # label/value, so passed that way the number never showed.
                stats=[(label, value)],
                cta_label="Open the health page",
                cta_url=absolute_url("/admin/health"),
                footer_note="You are receiving this because your address is in ADMIN_EMAILS.",
            )
        except Exception:
            logger.exception("Could not send a server alert to %s", email)


async def send_translation_report(check: dict) -> None:
    """What a translation check found, to the admins: what changed since the
    last check, per language, with a link to the report. English only — it's
    an admin email, like the server alerts."""
    from app.auth import translation_viewers

    # The admins, and the people fixing the translations.
    recipients = list(dict.fromkeys([*admin_emails(), *sorted(translation_viewers())]))
    if not recipients:
        logger.info("Translation check email not sent (no ADMIN_EMAILS) for check %s", check["id"])
        return
    host = (urlsplit(check["site_url"]).hostname or check["site_url"]).removeprefix("www.")
    summary = json.loads(check.get("summary_json") or "{}")
    diff = json.loads(check.get("diff_json") or "{}")
    pct = summary.get("translated_pct")
    pct_text = "—" if pct is None else f"{pct:g}%"
    compared = bool(diff.get("previous_id")) and not diff.get("rules_changed")
    notice, notice_tone = None, None

    if check["status"] == "completed":
        if compared:
            subject = f"{host} translation check: {_fmt(diff['new'])} new, {_fmt(diff['fixed'])} fixed"
            intro = (f"Compared with the previous check: {_fmt(diff['new'])} newly untranslated, "
                     f"{_fmt(diff['fixed'])} fixed. {_fmt(summary.get('strings', 0))} strings and "
                     f"{_fmt(summary.get('missing', 0))} missing pages are open in all.")
            hero = [(_fmt(diff["new"]), "New"), (_fmt(diff["fixed"]), "Fixed"), (pct_text, "Translated")]
        else:
            subject = f"{host} translation check: {_fmt(summary.get('strings', 0))} untranslated strings"
            intro = ("The rules changed since the last check, so this one isn't compared with it."
                     if diff.get("rules_changed") else
                     "The first check of this site. From the next one, this email says what changed.")
            hero = [(_fmt(summary.get("strings", 0)), "Strings"), (_fmt(summary.get("missing", 0)), "Missing pages"),
                    (pct_text, "Translated")]
        stats = []
        for locale in summary.get("locales", []):
            value = "—" if locale.get("translated_pct") is None else f"{locale['translated_pct']:g}% translated"
            if locale.get("missing"):
                value += f" · {_fmt(locale['missing'])} pages missing"
            if compared and (locale.get("new") or locale.get("fixed")):
                value += f" · +{_fmt(locale.get('new', 0))} / −{_fmt(locale.get('fixed', 0))}"
            stats.append((locale.get("name") or locale["code"], value))
        if diff.get("new_top"):
            names = {item["code"]: item.get("name") or item["code"] for item in summary.get("locales", [])}
            items = [f"“{item['text']}” ({names.get(item['locale'], item['locale'])}, {_fmt(item['pages'])} "
                     f"page{'' if item['pages'] == 1 else 's'})" for item in diff["new_top"][:3]]
            notice, notice_tone = "Newly untranslated: " + "; ".join(items) + ".", "warn"
        if summary.get("capped"):
            note = "This site has more pages than one check covers (TRANSLATION_CHECK_MAX_PAGES); the rest weren't checked."
            notice = f"{notice} {note}" if notice else note
            notice_tone = notice_tone or "warn"
    else:
        subject = f"{host} translation check didn't finish"
        intro = check.get("error") or "The check stopped before it finished."
        hero = None
        stats = None

    started = check.get("started_at")
    when = datetime.fromisoformat(started).strftime("%-d %b %Y") if started else ""
    for email in recipients:
        try:
            await _send_email(
                to_email=email,
                subject=subject,
                preheader=intro,
                heading=f"Translation check · {host}",
                subheading=f"{check['site_url']} · {when}",
                intro_text=intro,
                hero_stats=hero,
                stats=stats,
                notice=notice,
                notice_tone=notice_tone,
                cta_label="Open the report",
                cta_url=absolute_url(f"/admin/translations/{check['id']}"),
                footer_note="You are receiving this because your address is in ADMIN_EMAILS "
                            "or TRANSLATION_CHECK_VIEWERS.",
            )
        except Exception:
            logger.exception("Could not send the translation check email to %s", email)
