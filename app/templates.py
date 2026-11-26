from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app import i18n, js_strings, surfaces
from app.brand_logos import LOGOS, PLATFORM_GROUPS, brand_logo
from app.articles import HOME_VS, WXRKS_COUNT
from app.icons import icon


import hashlib as _hashlib
import os
from pathlib import Path as _Path

_STATIC_DIR = _Path(__file__).resolve().parent / "static"


def _asset_version() -> str:
    """A short fingerprint of the static assets, computed once per process.

    Browsers cache /static aggressively and nothing ever told them a deploy
    happened — so every deploy so far has shipped new HTML against whatever
    CSS and JS the visitor already had. New markup with week-old styles is
    exactly how "the menu renders as an unstyled green button and won't open"
    happens. The query string changes when the files do, which is the whole
    cache key.
    """
    digest = _hashlib.blake2b(digest_size=6)
    for name in ("style.css", "print.css", "app.js", "hero-webgl.js"):
        try:
            digest.update((_STATIC_DIR / name).read_bytes())
        except OSError:
            pass
    return digest.hexdigest()


ASSET_VERSION = _asset_version()

SOURCE_CODE_URL = os.environ.get("SOURCE_CODE_URL", "https://github.com/wxrks-builders/word-counter")


def _globals(request: Request) -> dict:
    """Puts the request's surface, and whether billing exists at all, into every
    template.

    A context processor rather than an argument at each call site: there is no
    shared context here — every route builds its own dict — so adding this by
    hand would mean touching nine TemplateResponse calls and remembering it on
    the tenth. The middleware in main.py resolves the surface; the getattr
    fallback covers templates rendered outside a request, such as the email body.

    billing_enabled is imported lazily to keep the import graph one-way:
    app.billing already imports this module for `templates`.
    """
    from app.auth import can_view_translations, is_admin
    from app.billing import billing_enabled

    lang = getattr(request.state, "lang", i18n.DEFAULT_LANG)
    # The path with its language prefix already stripped by the middleware, so
    # the alternates below can be built for every language from one place.
    bare_path = i18n.split_path(request.url.path)[1]

    _install_language(lang)
    return {
        "surface": getattr(request.state, "surface", surfaces.DEFAULT),
        # Pages that talk about the counter by its address, whichever host
        # they're read on.
        "counter_surface": surfaces.COUNTER,
        # AGPL-3.0: whoever runs this as a service offers its source. A fork
        # points this at its own repository.
        "source_code_url": SOURCE_CODE_URL,
        "billing_enabled": billing_enabled(),
        # A predicate rather than a flag: the context processor has the request
        # but not whichever user the route resolved, so the template applies it
        # to the user it already has.
        "is_admin": is_admin,
        "can_view_translations": can_view_translations,
        "lang": lang,
        "lang_dir": "rtl" if lang in i18n.RTL_LANGUAGES else "ltr",
        "hreflang": i18n.HREFLANG.get(lang, lang),
        "languages": i18n.selectable_languages(),
        "language_names": i18n.LANGUAGE_NAMES,
        # Every internal href goes through this, so a link written once works in
        # all eleven. Static assets pass through unprefixed — they aren't
        # translated, and prefixing them would make each language miss the
        # browser cache for a file they all share.
        "url": lambda path: i18n.localized_path(path, lang),
        "alternates": [
            (i18n.HREFLANG.get(code, code), i18n.localized_path(bare_path, code))
            for code in i18n.LANGUAGES
        ],
        # The switcher. Each entry points at the page you are already on, in
        # that language — landing somewhere else for choosing a language is a
        # small betrayal, and it loses whatever you were reading.
        "language_links": [
            {
                "code": code,
                "name": i18n.LANGUAGE_NAMES.get(code, code),
                "href": i18n.localized_path(bare_path, code),
                "current": code == lang,
            }
            for code in i18n.selectable_languages()
        ],
        "num": lambda value: i18n.format_number(value or 0, lang),
        "icon": icon,
        "brand_logo": brand_logo,
        "platforms": {group: [(slug, LOGOS[slug][0]) for slug in slugs] for group, slugs in PLATFORM_GROUPS.items()},
        "js_strings": js_strings.catalogue(lang),
        "asset_v": ASSET_VERSION,
        # The figures the chat mock-up quotes (templates/_chat_mock.html).
        "chat_mock": WXRKS_COUNT,
        # ...and the front page's comparison with an assistant on its own.
        "vs_ai": HOME_VS,
    }


def format_duration(total_seconds) -> str:
    """"~1h 8m" / "~25 min". Mirrors formatDuration() in app/static/app.js so a
    duration reads the same whether the page rendered it or the browser did."""
    seconds = int(total_seconds or 0)
    if seconds < 60:
        return "less than a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"~{minutes} min"
    hours, remainder = divmod(minutes, 60)
    return f"~{hours}h {remainder}m" if remainder else f"~{hours}h"


templates = Jinja2Templates(directory="app/templates", context_processors=[_globals])
templates.env.filters["duration"] = format_duration
# Jinja's own i18n extension, so {% trans %} and _() work in templates and
# pybabel can extract from them without a custom parser.
templates.env.add_extension("jinja2.ext.i18n")


def _install_language(lang: str) -> None:
    templates.env.install_gettext_callables(
        gettext=i18n.gettext_for(lang), ngettext=lambda s, p, n: i18n.gettext_for(lang)(s if n == 1 else p), newstyle=True
    )


# Re-bound per request in the context processor below, because one Jinja
# environment is shared by every language.
_install_language(i18n.DEFAULT_LANG)
