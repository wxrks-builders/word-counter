# Translation catalogues

Ten languages, the same ones wxrks.com publishes: **ar, de, es, fr, it, ja, ko,
pt, sv, zh**. English is the source and lives at the root of the site, so it has
no catalogue of its own.

## The loop with wxrks

    python scripts/translations.py export        # -> dist/wordcounter-translations-<date>.zip
    #   upload to wxrks, translate the ten languages, export .po back
    python scripts/translations.py import <zip>  # validates, writes, compiles, reports coverage
    #   run pytest, commit the .po diffs, push — the deploy compiles them

The export refuses a stale catalogue; the import refuses a batch with broken
placeholders or half-filled plural forms before writing anything. Under the
hood it is still plain gettext:

    pybabel extract -F babel.cfg -o locales/messages.pot --no-location --sort-output --add-comments=Translators: .
    pybabel update  --no-fuzzy-matching -i locales/messages.pot -d locales
    python scripts/translations.py compile

The compile is ours, not `pybabel compile`, which fills an untranslated plural
with the English pair: Arabic then shows "1 pages" and Japanese "5 page". Ours
leaves untranslated messages out, so they fall back to English by English rules.

`--add-comments` carries the `# Translators:` notes into the catalogues as `#.`
lines, which is the context a translator sees. `--no-fuzzy-matching` stops
pybabel guessing a translation for a reworded string, which wxrks would then
flag for review with nothing in it.

A language is published — listed in hreflang and the sitemap, offered on the
English pages, its own canonical — once 90% of its catalogue is translated
(`PUBLISH_AT` in `app/i18n.py`). Until then its pages work and point search
engines at the English ones.

`.po` files are committed. `.mo` files are **not** — they are build output, and
`scripts/translations.py compile` runs in the Dockerfile.

Admin pages are deliberately not extracted; see the comment in `babel.cfg`.

## Translating

These are gettext `.po` files, which every translation system reads. Send them
through wxrks like any other content: `msgid` is the English source, `msgstr` is
where the translation goes, and an empty `msgstr` falls back to English rather
than rendering blank — so a half-translated language is safe to deploy.

Things worth telling a translator (the export's TRANSLATING.md says them):

- **`%(name)s` placeholders must survive.** They are substituted at runtime; a
  translation that drops one, or renames it, raises instead of rendering. A
  plural form may say its number in words (Arabic "one page") and leave it out.
- **Plurals have one `msgstr[i]` per form the language has**: six in Arabic,
  two in most, one in Japanese, Korean and Chinese.
- **The `#.` notes** say where a string appears and what its placeholders hold.
- **Length matters here.** Several strings sit in a chip or a single-line
  banner. German and French run long; the layout wraps, but a 50-character
  label in a pill will look wrong.

`pt` is Brazilian Portuguese (hreflang pt-BR), and its catalogue says so
(`Language: pt_BR`, with pt-BR's plural rule, where 0 is singular).

## Adding a language

Add it to `LANGUAGES` and `HREFLANG` in `app/i18n.py`, add its own-language name
to `LANGUAGE_NAMES`, then:

    pybabel init -i locales/messages.pot -d locales -l xx

Init it with the locale the app serves, not just the language (`-l pt_BR`, not
`-l pt`): the plural rule in the header comes from it, and app.js picks plural
forms by the hreflang's rules. tests/test_i18n.py checks the two agree.

A language whose catalogue does not exist yet renders English rather than
failing, so the code change can land before the translations do.

## Finding strings that were never extracted

    PSEUDO_LOCALE=1 uvicorn app.main:app

Then browse `/zz/`. Every translated string comes back wrapped in brackets;
anything still bare is hard-coded English that needs marking up. This finds the
strings you did not think to look for, which grepping does not.
