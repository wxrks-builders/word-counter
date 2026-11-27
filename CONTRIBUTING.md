# Contributing

Thanks for helping. Bug reports, fixes and ideas are all welcome.

## Reporting a problem

Open an issue with the address you crawled (if it's public), what you expected
the count to be, and what the report showed. Counting questions are easiest to
answer with the page addresses whose numbers look wrong.

Please don't post passwords, session cookies or private reports in an issue.
For a security problem, follow [SECURITY.md](SECURITY.md) instead.

## Making a change

1. Set up the app as in the [README](README.md#running-it-locally).
2. Make your change, with a test that fails without it. Tests live in `tests/`
   and run with `pytest`.
3. If you added or changed text people read, wrap it in `_()` — `ngettext()` if
   it has a number in it, never `if n == 1` — add a `Translators:` note when the
   context isn't obvious, and regenerate the catalogues (README, "Translating
   the interface"). [LOCALIZATION.md](LOCALIZATION.md) has the rules. New
   strings show in English until they're translated.
4. Open a pull request saying what changed and why.

A few things this code relies on, worth knowing before changing them:

- **One process.** Crawl progress, the queue and live updates live in memory
  (`app/job_store.py`). Anything that assumes several workers or instances will
  break; see [DEPLOY.md](DEPLOY.md).
- **Counting is deterministic.** The same pages give the same count every time.
  Changes to `app/word_count.py`, `app/repetition.py` or `app/url_policy.py`
  change everyone's numbers, so they need a test showing the before and after.
- **Right-to-left works.** The interface runs in Arabic too: use logical CSS
  properties (`margin-inline`, `inset-inline`), not `left`/`right`. A test checks.

## License of contributions

Word Counter is licensed under the AGPL-3.0. By opening a pull request you
agree that your contribution is licensed under the same terms.
