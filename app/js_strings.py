"""Strings that app.js shows, kept here so there is one catalogue.

The alternative — a second JSON catalogue for the front end — means two
extractions, two review passes, and two places a translator has to be sent. A
Python dict costs one render of a small JSON blob per page and keeps every
string this product says in a single .po file.

Keys are stable identifiers; only the English changes when copy is edited, and
pybabel treats a changed msgid as a new string needing translation, which is
correct — reworded copy does need re-translating.

Plurals: an entry made with `ngettext(singular, plural)` arrives in the
browser as {"one": …, "other": …} — one string per CLDR plural category the
language has (six in Arabic, one in Japanese), each filled from the
catalogue's own plural rule. app.js picks the category with
Intl.PluralRules and the `count` it is given; see t() there. The English
singular carries the number too ("%(n)s page"), since other languages use
that form for 21 or for 0 as well.

Every placeholder is a formatted value (n(…), a percent, a name): the raw
number only ever travels as `count`.
"""

from __future__ import annotations

from app import i18n


def catalogue(lang: str) -> dict[str, str | dict[str, str]]:
    _ = i18n.gettext_for(lang)
    pgettext = i18n.pgettext_for(lang)

    # Named ngettext so pybabel extracts the pair as one plural message.
    def ngettext(singular: str, plural: str) -> dict[str, str]:
        return i18n.plural_variants(lang, singular, plural)

    return {
        # Status and progress
        # Translators: the label of a crawl's status badge, and of the form's
        # button while a crawl starts.
        "starting": _("Starting…"),
        # Translators: status badge: the crawl is reading pages right now.
        "crawling": _("Crawling"),
        "completed": _("Completed"),
        "failed": _("Failed"),
        "cancelled": _("Cancelled"),
        "paused": _("Paused"),
        "queued": _("Queued"),
        # Translators: the button that starts a crawl of the address typed in (a verb).
        "crawl": pgettext("button", "Crawl"),
        "resuming": _("Resuming…"),
        "start_recrawl": _("Start re-crawl"),
        # Report: subdomains and languages the run did or didn't count
        # Translators: %(scope)s is one of "Whole domain (incl. subdomains)",
        # "This subdomain only" or "Top-level domain only".
        "hosts_scope_limited": _("This run was set to “%(scope)s”, so other subdomains weren’t crawled."),
        "hosts_only_one": _("No other subdomains were linked from the pages crawled."),
        "hosts_left_out": _(
            "Linked from the pages crawled but not counted — outside this run’s settings, excluded, "
            "or not reached before it stopped."
        ),
        # Translators: beside a subdomain or language that wasn't counted: how
        # many of the crawled pages link to it.
        "linked_from": ngettext("linked from %(n)s page", "linked from %(n)s pages"),
        # Translators: %(checked)s and %(total)s are word counts; the sentence
        # says how much of the total the repetition estimate covers.
        "rep_covers": ngettext("Covers %(checked)s of %(total)s word", "Covers %(checked)s of %(total)s words"),
        "rep_partial": ngettext(
            "The server restarted during this crawl, so the estimate covers %(n)s of its words.",
            "The server restarted during this crawl, so the estimate covers %(n)s of its words.",
        ),
        "rep_duplicates": ngettext(
            "%(n)s duplicate page isn’t in the total at all.",
            "%(n)s duplicate pages aren’t in the total at all.",
        ),
        # Translators: pages in another language that were fetched, then left
        # out of the count.
        "set_aside_read": ngettext("%(n)s read and set aside", "%(n)s read and set aside"),
        # Translators: more pages in that language that weren't fetched at all.
        "set_aside_skipped": ngettext("%(n)s more not fetched", "%(n)s more not fetched"),
        # Translators: %(path)s is the address of one page, such as /fr/pricing.
        "example_page": _("e.g. %(path)s"),
        # Translators: a group of pages told apart by their writing system.
        # %(script)s is a script's name in your language (Latin, Cyrillic,
        # Arabic…), as the browser names it; phrase it so any name fits.
        "written_in": _("written in %(script)s"),
        # Translators: the name used for text in Chinese or Japanese characters,
        # which the language check can't tell apart.
        "script_cjk": _("Chinese or Japanese characters"),
        "proceed_with_crawl": _("Proceed with crawl"),
        "cancelling": _("Cancelling…"),
        "deleting": _("Deleting…"),
        "delete_run": _("Delete run"),
        # Translators: %(site)s is the address of the site that was crawled.
        "confirm_delete_crawl": _("Delete this crawl of %(site)s? This can’t be undone."),
        "delete_failed": _("Couldn’t delete this crawl. Try again."),
        "stop_before_delete": _("Stop this crawl before deleting it."),

        # The crawl form
        "something_went_wrong": _("Something went wrong"),
        "enter_valid_url": _("Please enter a valid http(s) URL"),
        "display_url_invalid": _("Enter a web address, such as https://www.example.com."),
        "display_url_other_site": _("Use an address on the site that was crawled."),
        "site_password_needed": _(
            "This site is password-protected. Enter the username and password its owner gave you."
        ),
        "site_password_rejected": _("The site didn't accept that username and password."),
        "site_password_gone": _(
            "The server restarted while this crawl was paused, and the site's password is never "
            "kept. Start a re-crawl and enter it again."
        ),
        "site_login_form_not_found": _(
            "Couldn't find a sign-in form there. Enter the address of the site's sign-in page."
        ),
        "site_login_rejected": _(
            "The site didn't let the crawler in with that email and password, or it asked for more, like a "
            "code. For a code or a Google or Microsoft sign-in, paste a session instead."
        ),
        "site_login_incomplete": _("Enter both the email (or username) and the password."),
        "site_login_url_invalid": _("Enter the sign-in page as a web address, like https://example.com/login."),
        "site_login_unreachable": _("The sign-in page didn't load. Check its address and try again."),
        "site_login_unavailable": _("Signing in isn't available right now. Try again in a minute, or paste a session."),
        "site_session_unreadable": _(
            "That doesn't look like a session. Copy the value of the “cookie” line from the browser's "
            "developer tools."
        ),
        "site_session_rejected": _(
            "That session didn't get past the sign-in page; it may have ended. Sign in again in your browser "
            "and copy a fresh one."
        ),
        "site_session_gone": _(
            "The server restarted while this crawl was paused, and the site's sign-in is never kept. "
            "Start a re-crawl and sign in again."
        ),
        "sign_in_to_continue": _("Sign in with Google to count the rest of this site."),
        "project_not_found": _("That project isn't yours, or it no longer exists."),
        "project_name_required": _("Give the project a name."),
        "confirm_delete_project": _("Delete this project? Its crawls stay, in your runs and in any other project."),
        "confirm_revoke_key": _("Revoke this connection? Any app using it stops counting until you give it a new one."),
        "too_many_keys": _("You have as many connections as there can be. Revoke one to make another."),
        # Translators: after a new connection's key hint ("wck_…a1b2 · made just now").
        "just_now": _("made just now"),
        "revoke": _("Revoke"),
        "consolidating": _("Consolidating…"),
        "consolidate_again": _("Consolidate again"),
        # Translators: %(error)s is a technical reason, often in English.
        "consolidate_failed": _("The last consolidation didn’t finish: %(error)s"),
        "removing": _("Removing…"),
        "remove": _("Remove"),
        # Translators: placeholder in the exclusions box once it holds an entry.
        "add_another": _("Add another…"),
        "exclusions_placeholder": _("staging, web-staging, /careers"),
        # Translators: the × on an exclusion chip; %(entry)s is what was typed, such as /careers.
        "remove_entry": _("Remove %(entry)s"),
        # Translators: on an exclusion chip: the entry is a folder (it starts with /).
        "folder": _("folder"),
        # Translators: on an exclusion chip: the entry is a subdomain.
        "subdomain": _("subdomain"),
        # Translators: one exclusion in the settings pill: %(entry)s is what was
        # typed, %(kind)s is "folder" or "subdomain".
        "exclusion_entry": _("%(entry)s (%(kind)s)"),
        "scope_all": _("Whole domain (incl. subdomains)"),
        "scope_subdomain_only": _("This subdomain only"),
        "scope_top_domain_only": _("Top-level domain only"),

        # Settings pills
        # Translators: a small pill on the report; keep it short.
        "languages_all": _("Languages: All"),
        # Translators: a small pill; %(lang)s is a language's name, shown in bold.
        "language_one": _("Language: %(lang)s"),
        # Translators: a small pill; %(lang)s is the language the crawler detected, in bold.
        "language_auto": _("Language: %(lang)s (auto)"),
        # Translators: a small pill; %(primary)s is the first language (bold),
        # %(rest)s a list of the others.
        "languages_many": _("Languages: %(primary)s + %(rest)s"),
        "excluded_none": _("Excluded: none"),
        # Translators: a pill; %(entries)s is a list such as "staging (subdomain), /careers (folder)".
        "excluded_some": _("Excluded: %(entries)s"),

        # Report tables and totals
        "n_words": ngettext("%(n)s word", "%(n)s words"),
        "n_pages": ngettext("%(n)s page", "%(n)s pages"),
        # Translators: under the total word count: "on 1,204 pages".
        "hero_pages": ngettext("on %(n)s page", "on %(n)s pages"),
        # Translators: under the total word count while the crawl still runs.
        "hero_pages_so_far": ngettext("on %(n)s page so far", "on %(n)s pages so far"),
        "strip_crawling": _("Pages appear below as they’re read."),
        "strip_queued": _("Waiting for a free slot. It starts on its own."),
        # Translators: %(cms)s is a platform's name, such as WordPress or Webflow.
        "built_with": _("built with %(cms)s"),
        "hero_rep_partial": ngettext("of the %(n)s word checked", "of the %(n)s words checked"),
        # Translators: %(pct)s is a percentage (64%%), %(folder)s a folder such
        # as /blog, %(words)s a number of words; the plural follows the pages.
        "folders_lead": ngettext(
            "%(pct)s of the words are in %(folder)s: %(words)s on %(n)s page.",
            "%(pct)s of the words are in %(folder)s: %(words)s on %(n)s pages.",
        ),
        # Translators: the next four are parts of a short line under "Not
        # counted": "3 duplicates · 2 blocked". Keep them short.
        "nc_duplicates": ngettext("%(n)s duplicate", "%(n)s duplicates"),
        # Translators: part of the "Not counted" line; %(n)s counts pages
        # that asked for a sign-in. Agree with "pages" where your language must.
        "nc_login": ngettext("%(n)s behind a login", "%(n)s behind a login"),
        # Translators: part of the "Not counted" line; %(n)s counts pages the
        # site's bot protection refused. Agree with "pages" where your language must.
        "nc_blocked": ngettext("%(n)s blocked", "%(n)s blocked"),
        # Translators: part of the "Not counted" line; %(n)s counts pages that
        # didn't load. Agree with "pages" where your language must.
        "nc_failed": ngettext("%(n)s failed", "%(n)s failed"),
        "nc_none": _("Nothing left out"),
        "pages_blocked": ngettext(
            "%(n)s page was blocked by this site's own bot detection.",
            "%(n)s pages were blocked by this site's own bot detection.",
        ),
        "pages_failed": ngettext("%(n)s page failed to load.", "%(n)s pages failed to load."),
        "show_all": _("Show all"),
        # Translators: a link under a table that shows every row; %(n)s is how many.
        "show_all_n": _("Show all %(n)s"),
        # Translators: a link under the top pages, when the list is itself the top %(n)s of more.
        "show_top_n": _("Show top %(n)s"),
        "root_folder": _("(root)"),
        # Translators: the language row for pages with no language in their address.
        "language_default": _("Default"),
        "no_pages_yet": _("No pages counted yet."),
        "view_issues": _("View blocked/failed pages"),
        # Translators: a page row's tooltip: the site refused the crawler.
        "blocked_by_host": _("Blocked by the host's bot detection"),
        "fetch_failed": _("Failed to fetch page"),
        # Translators: in the Words column instead of a number; keep it short and lower-case if your language does.
        "word_cell_blocked": _("blocked"),
        # Translators: in the Words column instead of a number; keep it short.
        "word_cell_failed": _("failed"),
        # Translators: a link that opens the crawled page in a new tab.
        "open_page": _("Open ↗"),
        # Translators: %(reason)s is the technical reason a page was blocked, often in English.
        "issue_blocked": _("Blocked: %(reason)s"),
        # Translators: %(reason)s is the technical reason a page failed, often in English.
        "issue_failed": _("Failed: %(reason)s"),
        "unknown_reason": _("Unknown reason"),
        "issues_capped": _("Showing the first %(n)s — export the CSV for the rest."),
        "pages_capped": ngettext(
            "Showing the first %(loaded)s of %(n)s page. Export the CSV for the complete list.",
            "Showing the first %(loaded)s of %(n)s pages. Export the CSV for the complete list.",
        ),
        "pages_live_capped": ngettext(
            "Showing the most recent %(n)s page while this crawl runs.",
            "Showing the most recent %(n)s pages while this crawl runs.",
        ),
        # Translators: beside "Show more": %(loaded)s of %(total)s pages shown.
        "loaded_of_total": _("%(loaded)s of %(total)s"),
        "show_more": _("Show more"),
        "loading": _("Loading…"),
        "load_more_failed": _("Couldn't load more pages."),
        "limit_reached": _("The configured page limit was reached — the crawl may be incomplete."),
        "crawl_failed": _("The crawl failed."),
        "cancelled_partial": _("This crawl was cancelled — showing partial results."),
        # Translators: %(date)s is a date and time, such as "27 November 2026, 15:08".
        "started_at": _("Started: %(date)s"),
        # Translators: %(email)s is the account's email address, shown in bold.
        "summary_emailed": _("Summary emailed to %(email)s"),
        # Translators: %(email)s is the account's email address, shown in bold.
        "summary_will_email": _("Summary will be emailed to %(email)s"),

        # Estimate panel
        "high_confidence": _("High confidence"),
        "medium_confidence": _("Medium confidence"),
        "low_confidence": _("Low confidence"),
        # Translators: %(cms)s is a platform's name, such as WordPress.
        "detected_platform": _("Detected platform: %(cms)s"),
        "detected_contentful": _("Detected platform: Contentful (headless — sitemap conventions vary)"),
        "server_load_easy": _("Easy server load"),
        "server_load_moderate": _("Moderate server load"),
        "server_load_busy": _("Busy server load"),
        "sitemap_found": ngettext(
            "Found a sitemap — this site has approximately %(n)s page. Crawling all of them may take a while.",
            "Found a sitemap — this site has approximately %(n)s pages. Crawling all of them may take a while.",
        ),
        "sitemap_missing": ngettext(
            "No sitemap found — this estimate is based only on pages discovered so far (approximately %(n)s), "
            "so it may be less accurate. Crawling all of them may take a while.",
            "No sitemap found — this estimate is based only on pages discovered so far (approximately %(n)s), "
            "so it may be less accurate. Crawling all of them may take a while.",
        ),
        # Translators: crawl speed, abbreviated; %(words)s and %(pages)s are numbers.
        "words_per_min": _("%(words)s words/min · %(pages)s pages/min"),
        # Translators: %(time)s is a duration such as "25 min", shown in bold.
        "on_pro": _("about %(time)s on Pro"),
        "see_what_costs": _("See what that costs →"),

        # Durations — the same messages as i18n.duration_text, so the page and
        # the script say a duration the same way.
        "less_than_a_minute": _("less than a minute"),
        "duration_minutes": ngettext("%(n)s min", "%(n)s min"),
        "duration_hours": _("%(h)sh"),
        "duration_hours_minutes": _("%(h)sh %(m)sm"),
        "approximately": _("~%(duration)s"),

        # Markdown
        # Translators: a button; %(size)s is a file size such as "12 MB".
        "markdown_download": _("Download Markdown · %(size)s"),
        "markdown_download_plain": _("Download Markdown"),
        "markdown_after_finish": _("Available when the crawl finishes"),
        "markdown_preparing": _("Preparing…"),
        "markdown_stopped_run_cap": ngettext(
            "Markdown was saved for the first %(n)s page, then stopped because the per-crawl size limit was "
            "reached. The word count below is complete either way.",
            "Markdown was saved for the first %(n)s pages, then stopped because the per-crawl size limit was "
            "reached. The word count below is complete either way.",
        ),
        "markdown_stopped_disk": ngettext(
            "Markdown was saved for the first %(n)s page, then stopped because the server was running low on "
            "disk space. The word count below is complete either way.",
            "Markdown was saved for the first %(n)s pages, then stopped because the server was running low on "
            "disk space. The word count below is complete either way.",
        ),
        "markdown_stopped_global_cap": ngettext(
            "Markdown was saved for the first %(n)s page, then stopped because the server's total Markdown "
            "limit was reached. The word count below is complete either way.",
            "Markdown was saved for the first %(n)s pages, then stopped because the server's total Markdown "
            "limit was reached. The word count below is complete either way.",
        ),
        "markdown_stopped_error": ngettext(
            "Markdown was saved for the first %(n)s page, then stopped because of an error while saving. "
            "The word count below is complete either way.",
            "Markdown was saved for the first %(n)s pages, then stopped because of an error while saving. "
            "The word count below is complete either way.",
        ),

        # Sharing and deleting
        "copy_link": _("Copy link"),
        "copied": _("Copied"),
        "sending": _("Sending…"),
        # Translators: %(email)s is the address the report was just sent to.
        "share_sent": _("Sent to %(email)s"),
        "share_failed": _("Couldn't send — try again."),
        "share_email_required": _("Enter an email address to send the report to."),
        # Translators: under an invited person's address; %(date)s is when.
        "invited_on": _("Invited %(date)s"),
        "invited": _("Invited"),
        # Translators: the × beside an invited person; %(email)s is their address.
        "remove_person": _("Remove %(email)s"),
        "confirm_delete": _("Delete this run and everything saved with it? This cannot be undone."),
        "confirm_cancel_all": _("Cancel every active crawl on the server? This cannot be undone."),

        # Queue
        # Translators: %(n)s is a position in the queue (1, 2, 3…).
        "queue_position": _(
            "You're #%(n)s in line — this server is busy right now. This will start automatically, and "
            "we'll email you when it's done."
        ),
        "queue_start_email": _("This will start automatically, and we'll email you when it's done."),

        # Billing
        "opening_checkout": _("Opening checkout…"),
        "billing_unreachable": _("Could not reach billing"),
    }
