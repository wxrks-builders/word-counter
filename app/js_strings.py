"""Strings that app.js shows, kept here so there is one catalogue.

The alternative — a second JSON catalogue for the front end — means two
extractions, two review passes, and two places a translator has to be sent. A
Python dict costs one render of a small JSON blob per page and keeps every
string this product says in a single .po file.

Keys are stable identifiers; only the English changes when copy is edited, and
pybabel treats a changed msgid as a new string needing translation, which is
correct — reworded copy does need re-translating.
"""

from __future__ import annotations

from app.i18n import gettext_for


def catalogue(lang: str) -> dict[str, str]:
    _ = gettext_for(lang)
    return {
        # Status and progress
        "starting": _("Starting…"),
        "crawling": _("Crawling"),
        "completed": _("Completed"),
        "failed": _("Failed"),
        "cancelled": _("Cancelled"),
        "paused": _("Paused"),
        "queued": _("Queued"),
        "crawl": _("Crawl"),
        "resuming": _("Resuming…"),
        "start_recrawl": _("Start re-crawl"),
        # Report: subdomains and languages the run did or didn't count
        "hosts_scope_limited": _("This run was set to “%(scope)s”, so other subdomains weren’t crawled."),
        "hosts_only_one": _("No other subdomains were linked from the pages crawled."),
        "hosts_left_out": _(
            "Linked from the pages crawled but not counted — outside this run’s settings, excluded, "
            "or not reached before it stopped."
        ),
        "linked_from_one": _("linked from 1 page"),
        "linked_from_many": _("linked from %(n)s pages"),
        "rep_covers": _("Covers %(checked)s of %(total)s words"),
        "rep_partial": _("The server restarted during this crawl, so the estimate covers %(words)s of its words."),
        "rep_duplicates_one": _("1 duplicate page isn’t in the total at all."),
        "rep_duplicates_many": _("%(n)s duplicate pages aren’t in the total at all."),
        "set_aside_read": _("%(n)s read and set aside"),
        "set_aside_skipped": _("%(n)s more not fetched"),
        "example_page": _("e.g. %(path)s"),
        "written_in": _("written in %(script)s"),
        "proceed_with_crawl": _("Proceed with crawl"),
        "cancelling": _("Cancelling…"),
        "deleting": _("Deleting…"),

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
        "project_not_found": _("That project isn't yours, or it no longer exists."),
        "project_name_required": _("Give the project a name."),
        "confirm_delete_project": _("Delete this project? Its crawls stay, in your runs and in any other project."),
        "confirm_revoke_key": _("Revoke this connection? Any app using it stops counting until you give it a new one."),
        "too_many_keys": _("You have as many connections as there can be. Revoke one to make another."),
        "just_now": _("made just now"),
        "revoke": _("Revoke"),
        "consolidating": _("Consolidating…"),
        "consolidate_again": _("Consolidate again"),
        "consolidate_failed": _("The last consolidation didn’t finish: %(error)s"),
        "removing": _("Removing…"),
        "remove": _("Remove"),
        "add_another": _("Add another…"),
        "exclusions_placeholder": _("staging, web-staging, /careers"),
        "remove_entry": _("Remove %(entry)s"),
        "folder": _("folder"),
        "subdomain": _("subdomain"),

        # Settings pills
        "languages_all": _("Languages: All"),
        "language_one": _("Language: %(lang)s"),
        "language_auto": _("Language: %(lang)s (auto)"),
        "languages_many": _("Languages: %(primary)s + %(rest)s"),
        "excluded_none": _("Excluded: none"),
        "excluded_some": _("Excluded: %(entries)s"),

        # Report tables and totals
        "n_words": _("%(n)s words"),
        "n_pages": _("%(n)s pages"),
        "n_pages_one": _("1 page"),
        "hero_pages": _("on %(n)s pages"),
        "hero_pages_one": _("on 1 page"),
        "hero_pages_so_far": _("on %(n)s pages so far"),
        "hero_pages_one_so_far": _("on 1 page so far"),
        "strip_crawling": _("Pages appear below as they’re read."),
        "strip_queued": _("Waiting for a free slot. It starts on its own."),
        "built_with": _("built with %(cms)s"),
        "hero_rep_partial": _("of the %(n)s words checked"),
        "folders_lead": _("%(pct)s of the words are in %(folder)s: %(words)s on %(pages)s pages."),
        "folders_lead_one": _("%(pct)s of the words are in %(folder)s: %(words)s on 1 page."),
        "nc_duplicates": _("%(n)s duplicates"),
        "nc_duplicates_one": _("1 duplicate"),
        "nc_login": _("%(n)s behind a login"),
        "nc_blocked": _("%(n)s blocked"),
        "nc_failed": _("%(n)s failed"),
        "nc_none": _("Nothing left out"),
        "show_all": _("Show all"),
        "root_folder": _("(root)"),
        "no_pages_yet": _("No pages counted yet."),
        "view_issues": _("View blocked/failed pages"),

        # Estimate panel
        "high_confidence": _("High confidence"),
        "medium_confidence": _("Medium confidence"),
        "low_confidence": _("Low confidence"),
        "detected_platform": _("Detected platform: %(cms)s"),
        "detected_contentful": _("Detected platform: Contentful (headless — sitemap conventions vary)"),
        "server_load_easy": _("Easy server load"),
        "server_load_moderate": _("Moderate server load"),
        "server_load_busy": _("Busy server load"),
        "sitemap_found": _("Found a sitemap — this site has approximately %(pages)s pages. Crawling all of them may take a while."),
        "sitemap_missing": _("No sitemap found — this estimate is based only on pages discovered so far (approximately %(pages)s), so it may be less accurate. Crawling all of them may take a while."),
        "words_per_min": _("%(words)s words/min · %(pages)s pages/min"),
        "on_pro": _("about %(time)s on Pro"),
        "see_what_costs": _("See what that costs →"),

        # Durations
        "less_than_a_minute": _("less than a minute"),
        "about_minutes": _("~%(n)s min"),
        "about_hours": _("~%(h)sh"),
        "about_hours_minutes": _("~%(h)sh %(m)sm"),

        # Markdown
        "markdown_download": _("Download Markdown (%(size)s)"),
        "markdown_preparing": _("Preparing…"),

        # Sharing and deleting
        "copy_link": _("Copy link"),
        "copied": _("Copied"),
        "share_sent": _("Sent to %(email)s"),
        "confirm_delete": _("Delete this run and everything saved with it? This cannot be undone."),
        "confirm_cancel_all": _("Cancel every active crawl on the server? This cannot be undone."),

        # Queue
        "queue_position": _("You're number %(n)s in the queue. It'll start on its own."),

        # Billing
        "opening_checkout": _("Opening checkout…"),
        "billing_unreachable": _("Could not reach billing"),
    }
