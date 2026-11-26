// Translated strings and the locale, rendered into the page by base.html.
// Falls back to the key rather than throwing: a string added to app.js before
// it is added to app/js_strings.py should look wrong, not break the page.
const I18N = window.I18N || {};
const LOCALE = window.LOCALE || "en";

function t(key, vars) {
  let text = I18N[key] || key;
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.split(`%(${name})s`).join(value);
    }
  }
  return text;
}

// Every number in this app is the product, so it formats for the reader:
// 662,431 in English, 662.431 in German, 662 431 in French.
function n(value) {
  return Number(value || 0).toLocaleString(LOCALE);
}

// Common two-letter language codes, used only to *suggest* a value for the
// language filter from whatever URL is pasted in — not exhaustive (the
// backend's ISO 639-1 set is authoritative for actually filtering), just
// needs to avoid suggesting something wrong.
const COMMON_LANGUAGE_CODES = new Set([
  "en", "es", "fr", "de", "it", "pt", "nl", "ru", "ja", "zh", "ko", "ar", "hi", "tr", "pl",
  "sv", "da", "no", "fi", "cs", "el", "he", "th", "vi", "id", "ms", "ro", "hu", "uk", "bg",
  "sk", "sl", "hr", "sr", "lt", "lv", "et", "ca", "eu", "gl", "is", "ga", "cy", "sq", "mk",
  "az", "ka", "hy", "kk", "uz", "mn", "fa", "ur", "bn", "ta", "te", "ml", "kn", "mr", "gu",
  "pa", "si", "km", "lo", "my", "ne", "am",
]);

function suggestLanguageFromUrl(url) {
  try {
    const segments = new URL(url).pathname.split("/").filter(Boolean);
    if (!segments.length) return "";
    const code = segments[0].toLowerCase().split("-")[0].split("_")[0];
    return code.length === 2 && COMMON_LANGUAGE_CODES.has(code) ? code : "";
  } catch (e) {
    return "";
  }
}

// After a sign-in error, the field it's about — under Advanced.
function focusSignInField(code) {
  if (!code || !(code.startsWith("site_login_") || code.startsWith("site_session_"))) return;
  document.querySelector(".crawl-advanced").open = true;
  if (code.startsWith("site_session_")) {
    document.getElementById("site-session").open = true;
    document.getElementById("login-session").focus();
  } else if (["site_login_form_not_found", "site_login_url_invalid", "site_login_unreachable"].includes(code)) {
    document.getElementById("login-url").focus();
  } else {
    const email = document.getElementById("login-email");
    (email.value ? document.getElementById("login-password") : email).focus();
  }
}

function initHomeForm() {
  const form = document.getElementById("crawl-form");
  const errorEl = document.getElementById("form-error");
  const submitBtn = document.getElementById("submit-btn");
  const urlInput = document.getElementById("url");
  const domainScopeSelect = document.getElementById("domain_scope");
  const languageInput = document.getElementById("language");
  const exclusionsTags = initTagInput(document.getElementById("exclusions"), "");

  // Handed over from the front page when its crawl couldn't start on its own —
  // most often a password-protected site, which needs the Advanced fields.
  const handoff = new URLSearchParams(window.location.search);
  if (handoff.get("url")) {
    urlInput.value = handoff.get("url");
    const code = handoff.get("code");
    if (code) {
      errorEl.textContent = t(code);
      errorEl.style.display = "block";
      if (code === "site_password_needed" || code === "site_password_rejected") {
        document.querySelector(".crawl-advanced").open = true;
        document.getElementById("site-username").focus();
      }
    }
    history.replaceState(null, "", window.location.pathname);
  }

  // Project, under Advanced: "New project…" reveals a name field.
  const projectSelect = document.getElementById("project-select");
  const projectNewInput = document.getElementById("project-new-input");
  if (projectSelect && projectNewInput) {
    projectSelect.addEventListener("change", () => {
      projectNewInput.hidden = projectSelect.value !== "__new";
      if (!projectNewInput.hidden) projectNewInput.focus();
    });
  }
  const projectPayload = () => {
    if (!projectSelect) return {};
    if (projectSelect.value === "__new") {
      const name = projectNewInput.value.trim();
      return name ? { project_name: name } : {};
    }
    return projectSelect.value ? { project_ids: [projectSelect.value] } : {};
  };

  urlInput.addEventListener("blur", () => {
    if (!languageInput.value) {
      const suggestion = suggestLanguageFromUrl(urlInput.value);
      if (suggestion) languageInput.value = suggestion;
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.style.display = "none";
    submitBtn.disabled = true;
    submitBtn.textContent = "Starting…";

    const url = urlInput.value;
    const domainScope = domainScopeSelect.value;
    const language = languageInput.value.trim() || null;
    const exclusions = exclusionsTags.value() || null;
    const siteUsername = document.getElementById("site-username");
    const sitePassword = document.getElementById("site-password");

    try {
      const res = await fetch("/crawl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url, domain_scope: domainScope, language, exclusions,
          capture_markdown: document.getElementById("capture-markdown").checked,
          site_username: siteUsername.value.trim() || null,
          site_password: sitePassword.value || null,
          ...projectPayload(),
          login_url: document.getElementById("login-url").value.trim() || null,
          login_email: document.getElementById("login-email").value.trim() || null,
          login_password: document.getElementById("login-password").value || null,
          login_session: document.getElementById("login-session").value.trim() || null,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        if (data.code === "site_password_needed" || data.code === "site_password_rejected") {
          // The fields live under Advanced; a site asking for them is the
          // moment to bring them out rather than leave the person hunting.
          document.querySelector(".crawl-advanced").open = true;
          (siteUsername.value ? sitePassword : siteUsername).focus();
        }
        focusSignInField(data.code);
        throw new Error(data.code ? t(data.code) : data.detail || t("something_went_wrong"));
      }
      window.location.href = "/crawl/" + data.run_id;
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.style.display = "block";
      submitBtn.disabled = false;
      submitBtn.textContent = "Crawl";
    }
  });
}

// The front page's illustrations. Each animates once, when it scrolls into
// view; with reduced motion requested (or no IntersectionObserver) nothing is
// hidden and nothing moves — the CSS only hides a start state under .js-motion.
function initHomeMotion() {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // "Count my website's words" at the bottom goes back to the form, focused.
  document.querySelectorAll("[data-focus-form]").forEach((link) => {
    link.addEventListener("click", (event) => {
      const input = document.getElementById("site-url") || document.getElementById("url");
      if (!input) return;
      event.preventDefault();
      input.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "center" });
      input.focus({ preventScroll: true });
    });
  });

  const visuals = document.querySelectorAll(".vis");
  if (!visuals.length || reduce || !("IntersectionObserver" in window)) return;
  document.documentElement.classList.add("js-motion");

  const format = new Intl.NumberFormat(LOCALE);
  const countUp = (el) => {
    const target = Number(el.dataset.countup) || 0;
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / 1100);
      el.textContent = format.format(Math.round(target * (1 - Math.pow(1 - t, 3))));
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };

  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      entry.target.classList.add("is-visible");
      entry.target.querySelectorAll("[data-countup]").forEach(countUp);
      observer.unobserve(entry.target);
    }
  }, { threshold: 0.3 });
  visuals.forEach((el) => observer.observe(el));
}

// Sends the browser to a Stripe-hosted page. Both endpoints answer with a URL
// rather than redirecting, because they're called from fetch() — a 303 here
// would be followed by the fetch and never by the window.
async function goToStripe(path, errorEl) {
  try {
    const res = await fetch(path, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Could not reach billing");
    if (data.already_pro) {
      // Nothing to buy — send them to manage what they already have.
      return goToStripe("/billing/portal", errorEl);
    }
    window.location.href = data.url;
  } catch (err) {
    if (errorEl) {
      errorEl.textContent = err.message;
      errorEl.style.display = "block";
    }
  }
}

// Menus in the header — the account chip and the language picker. Driven by
// data attributes rather than an id per menu, so a third one needs markup and
// nothing else, and so opening one always closes the other.
function initUserMenu() {
  const menus = [...document.querySelectorAll("[data-menu]")].map((root) => ({
    root,
    button: root.querySelector("[data-menu-button]"),
    panel: root.querySelector("[data-menu-panel]"),
  })).filter((m) => m.button && m.panel);

  const setOpen = (menu, open) => {
    menu.panel.hidden = !open;
    menu.button.setAttribute("aria-expanded", open ? "true" : "false");
    menu.root.classList.toggle("is-open", open);
  };
  const closeAll = () => menus.forEach((m) => setOpen(m, false));

  for (const menu of menus) {
    menu.button.addEventListener("click", (e) => {
      e.stopPropagation();
      const wasClosed = menu.panel.hidden;
      // Two panels open at once in a header this size overlap each other.
      closeAll();
      if (wasClosed) setOpen(menu, true);
    });
  }
  // Clicking anywhere else closes them — a menu that only closes via its own
  // button is a menu people leave open by accident.
  document.addEventListener("click", (e) => {
    if (!menus.some((m) => m.root.contains(e.target))) closeAll();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeAll();
  });
}

function initBilling() {
  const errorEl = document.getElementById("billing-error");
  const upgradeBtn = document.getElementById("upgrade-btn");
  if (upgradeBtn) {
    upgradeBtn.addEventListener("click", () => {
      upgradeBtn.disabled = true;
      upgradeBtn.textContent = "Opening checkout…";
      goToStripe("/billing/checkout", errorEl);
    });
  }
  // The topbar "Billing" link appears on every signed-in page, so this is bound
  // wherever app.js loads rather than only on the pricing page.
  for (const el of document.querySelectorAll("[data-billing-portal]")) {
    el.addEventListener("click", (e) => {
      e.preventDefault();
      goToStripe("/billing/portal", errorEl);
    });
  }
}

function badgeClass(status) {
  if (status === "completed") return "status-badge status-completed";
  if (status === "failed") return "status-badge status-failed";
  if (status === "cancelled") return "status-badge status-cancelled";
  if (status === "paused") return "status-badge status-paused";
  if (status === "queued") return "status-badge status-queued";
  return "status-badge status-crawling";
}

function statusLabel(status) {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

const TERMINAL_STATUSES = ["completed", "failed", "cancelled", "paused"];
// A summary email goes out on each of these (see send_crawl_notification in
// crawler.py). "paused" is deliberately absent — that run still has to resume
// and finish before anything is sent.
const EMAILED_STATUSES = ["completed", "failed", "cancelled"];

const DOMAIN_SCOPE_LABELS = {
  all: "Whole domain (incl. subdomains)",
  subdomain_only: "This subdomain only",
  top_domain_only: "Top-level domain only",
};

function parseLanguageList(text) {
  if (!text) return [];
  return text.split(",").map((s) => s.trim()).filter(Boolean);
}

// "folder" or "subdomain", by the same leading-"/" rule the crawler applies —
// mirrors exclusion_kind() in app/url_policy.py.
function exclusionKind(entry) {
  return entry.trim().startsWith("/") ? "folder" : "subdomain";
}

function describeExclusions(text) {
  return parseLanguageList(text).map((e) => `${e} (${exclusionKind(e)})`);
}

// A comma-separated box gives no feedback until the crawl is already running:
// you can't tell whether "web-staging" registered as one entry, or whether a
// stray comma made two. Each entry becomes a chip the moment it's committed,
// labelled with how it will actually be treated.
function initTagInput(root, initialValue) {
  if (!root) return { value: () => "" };
  const list = root.querySelector("[data-tag-list]");
  const input = root.querySelector("input");
  let tags = parseLanguageList(initialValue);

  const render = () => {
    list.textContent = "";
    tags.forEach((tag, i) => {
      const chip = document.createElement("span");
      chip.className = "tag";
      // textContent throughout: these are free text somebody typed, and a
      // shared report shows them to whoever the link was sent to.
      chip.appendChild(document.createTextNode(tag));
      const kind = document.createElement("em");
      kind.textContent = exclusionKind(tag);
      chip.appendChild(kind);
      const x = document.createElement("button");
      x.type = "button";
      x.className = "tag-remove";
      x.textContent = "×";
      x.title = `Remove ${tag}`;
      x.addEventListener("click", () => { tags.splice(i, 1); render(); input.focus(); });
      chip.appendChild(x);
      list.appendChild(chip);
    });
    input.placeholder = tags.length ? "Add another…" : "staging, web-staging, /careers";
  };

  const commit = () => {
    const parts = parseLanguageList(input.value);
    for (const part of parts) if (!tags.includes(part)) tags.push(part);
    input.value = "";
    render();
  };

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") {
      // Enter must not submit the form it sits in — here it means "commit
      // this entry", which is the only thing anyone expects it to mean.
      e.preventDefault();
      commit();
    } else if (e.key === "Backspace" && !input.value && tags.length) {
      tags.pop();
      render();
    }
  });
  // Typing an entry and clicking straight to the button should keep it, not
  // silently drop what's still sitting in the box.
  input.addEventListener("blur", commit);
  root.addEventListener("click", (e) => { if (e.target === root || e.target === list) input.focus(); });

  render();
  return { value: () => { commit(); return tags.join(", "); } };
}

function renderSettingsPills(domainScope, languages, autoDetected, exclusions) {
  reportScope = domainScope || "all";
  const domainPill = document.getElementById("domain-scope-pill");
  const languagePill = document.getElementById("language-pill");
  const exclusionsPill = document.getElementById("exclusions-pill");
  if (domainPill) domainPill.textContent = DOMAIN_SCOPE_LABELS[domainScope] || domainScope;
  if (exclusionsPill) {
    // parseLanguageList is just a comma splitter, reused here.
    const entries = parseLanguageList(exclusions);
    // Always shown, like the language pill above it says "Languages: All"
    // rather than disappearing. Hiding it made "nothing was excluded"
    // indistinguishable from "the exclusions weren't recorded", which is the
    // one question this pill exists to answer.
    exclusionsPill.style.display = "";
    // textContent, not innerHTML: unlike the scope and language pills this is
    // free text somebody typed, and a shared report renders whatever its owner
    // entered to everyone they shared it with.
    exclusionsPill.textContent = entries.length
      ? `Excluded: ${describeExclusions(exclusions).join(", ")}`
      : "Excluded: none";
  }
  if (!languagePill) return;
  if (!languages.length) {
    languagePill.textContent = "Languages: All";
  } else if (languages.length === 1) {
    languagePill.textContent = autoDetected ? `Language: ${languages[0]} (auto)` : `Language: ${languages[0]}`;
    languagePill.innerHTML = autoDetected
      ? `Language: <strong>${languages[0]}</strong> (auto)`
      : `Language: <strong>${languages[0]}</strong>`;
  } else {
    const [primary, ...rest] = languages;
    languagePill.innerHTML = `Languages: <strong>${primary}</strong> + ${rest.join(", ")}`;
  }
}

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const datePart = d.toLocaleDateString(LOCALE, { month: "long", day: "numeric", year: "numeric" });
  const timePart = d.toLocaleTimeString(LOCALE, { hour: "numeric", minute: "2-digit" });
  return `${datePart} · ${timePart}`;
}

// Run timestamps are stored as UTC and rendered server-side as a raw ISO
// string, so the browser is what turns them into the reader's local time.
function formatRunDates() {
  for (const el of document.querySelectorAll(".run-date-iso")) {
    el.textContent = formatDate(el.dataset.iso);
  }
}

function formatBytes(bytes) {
  if (!bytes) return "0 MB";
  const mb = bytes / 1048576;
  if (mb < 1) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  if (mb < 1024) return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
}

function formatDuration(totalSeconds) {
  if (!totalSeconds || totalSeconds < 1) return "less than a minute";
  const totalMinutes = Math.round(totalSeconds / 60);
  if (totalMinutes < 1) return "less than a minute";
  if (totalMinutes < 60) return `~${totalMinutes} min`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return minutes ? `~${hours}h ${minutes}m` : `~${hours}h`;
}

function concurrencyLabel(count) {
  if (count <= 1) return "Easy";
  if (count === 2) return "Moderate";
  return "Busy";
}

function renderPageRow(tbody, page, append) {
  const tr = document.createElement("tr");
  if (page.blocked_by_host) {
    tr.className = "page-blocked-host";
    tr.title = page.error || "Blocked by the host's bot detection";
  } else if (!page.success) {
    tr.className = "page-failed";
    tr.title = page.error || "Failed to fetch page";
  }
  const urlTd = document.createElement("td");
  urlTd.className = "url-cell";
  urlTd.title = page.url;
  urlTd.textContent = page.url;
  const titleTd = document.createElement("td");
  titleTd.textContent = page.title || "";
  const wordsTd = document.createElement("td");
  wordsTd.className = "words-cell";
  wordsTd.textContent = page.blocked_by_host ? "blocked" : page.success ? page.word_count.toLocaleString(LOCALE) : "failed";
  const openTd = document.createElement("td");
  openTd.className = "open-cell";
  const openLink = document.createElement("a");
  openLink.href = page.url;
  openLink.target = "_blank";
  openLink.rel = "noopener noreferrer";
  openLink.className = "open-link";
  openLink.textContent = "Open ↗";
  openTd.appendChild(openLink);
  tr.append(urlTd, titleTd, wordsTd, openTd);
  // Live mode prepends so the newest page appears at the top as it streams in;
  // a saved report appends so paged-in rows continue the list in order.
  if (append) tbody.appendChild(tr);
  else tbody.prepend(tr);
}

// A saved report never puts more than this many rows in the DOM. Past roughly
// this point the table stops being something anyone reads and starts being the
// reason the tab hangs — 164k rows is a million DOM nodes. The CSV export is
// the complete list.
const PAGE_LIST_MAX = 5000;

function folderForUrl(url) {
  try {
    const segments = new URL(url).pathname.split("/").filter(Boolean);
    return segments.length ? "/" + segments[0] : "(root)";
  } catch (e) {
    return "(root)";
  }
}

const SUMMARY_ROW_LIMIT = 10;

function renderExpandableTableRows(tbody, rows, colSpan, renderRowFn) {
  const fill = (items) => {
    tbody.innerHTML = "";
    for (const item of items) tbody.appendChild(renderRowFn(item));
  };
  fill(rows.slice(0, SUMMARY_ROW_LIMIT));
  if (rows.length <= SUMMARY_ROW_LIMIT) return;
  const tr = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = colSpan;
  td.className = "show-all-row";
  const link = document.createElement("a");
  link.href = "#";
  link.textContent = `Show all ${rows.length.toLocaleString(LOCALE)}`;
  link.addEventListener("click", (e) => {
    e.preventDefault();
    fill(rows);
  });
  td.appendChild(link);
  tr.appendChild(td);
  tbody.appendChild(tr);
}

function renderFolderRow(group, maxWords) {
  // Live mode still groups in the browser and passes [name, {count, words}];
  // a saved report gets {name, pages, words} already grouped by the server.
  const name = Array.isArray(group) ? group[0] : group.name;
  const words = Array.isArray(group) ? group[1].words : group.words;
  const count = Array.isArray(group) ? group[1].count : group.pages;
  const tr = document.createElement("tr");

  const folderTd = document.createElement("td");
  const bar = document.createElement("div");
  bar.className = "folder-bar";
  bar.style.width = Math.max(1, Math.round((words / maxWords) * 100)) + "%";
  const folderSpan = document.createElement("span");
  folderSpan.textContent = name;
  folderTd.append(bar, folderSpan);

  const countTd = document.createElement("td");
  countTd.className = "folder-pages";
  countTd.innerHTML = `<span>${count.toLocaleString(LOCALE)}</span>`;

  const wordsTd = document.createElement("td");
  wordsTd.className = "folder-words";
  wordsTd.innerHTML = `<span>${words.toLocaleString(LOCALE)}</span>`;

  tr.append(folderTd, countTd, wordsTd);
  return tr;
}

function renderFolderGroups(pages) {
  const groups = {};
  for (const p of pages) {
    const folder = folderForUrl(p.url);
    if (!groups[folder]) groups[folder] = { words: 0, count: 0 };
    groups[folder].words += p.word_count || 0;
    groups[folder].count += 1;
  }
  const rows = Object.entries(groups).sort((a, b) => b[1].words - a[1].words);
  const maxWords = rows.length ? rows[0][1].words : 1;
  const tbody = document.getElementById("folder-tbody");
  renderExpandableTableRows(tbody, rows, 3, (row) => renderFolderRow(row, maxWords));
  renderFoldersLead(rows.map(([name, g]) => ({ name, pages: g.count, words: g.words })));
}

// Same ISO 639-1 set and first-path-segment heuristic as the backend's
// LanguageFilter (app/crawler.py) — kept in sync manually, purely for
// grouping the results table by language, not for filtering anything.
const ISO_639_1_CODES = new Set([
  "aa", "ab", "ae", "af", "ak", "am", "an", "ar", "as", "av", "ay", "az",
  "ba", "be", "bg", "bh", "bi", "bm", "bn", "bo", "br", "bs",
  "ca", "ce", "ch", "co", "cr", "cs", "cu", "cv", "cy",
  "da", "de", "dv", "dz",
  "ee", "el", "en", "eo", "es", "et", "eu",
  "fa", "ff", "fi", "fj", "fo", "fr", "fy",
  "ga", "gd", "gl", "gn", "gu", "gv",
  "ha", "he", "hi", "ho", "hr", "ht", "hu", "hy", "hz",
  "ia", "id", "ie", "ig", "ii", "ik", "io", "is", "it", "iu",
  "ja", "jv",
  "ka", "kg", "ki", "kj", "kk", "kl", "km", "kn", "ko", "kr", "ks", "ku", "kv", "kw", "ky",
  "la", "lb", "lg", "li", "ln", "lo", "lt", "lu", "lv",
  "mg", "mh", "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my",
  "na", "nb", "nd", "ne", "ng", "nl", "nn", "no", "nr", "nv", "ny",
  "oc", "oj", "om", "or", "os",
  "pa", "pi", "pl", "ps", "pt",
  "qu",
  "rm", "rn", "ro", "ru", "rw",
  "sa", "sc", "sd", "se", "sg", "si", "sk", "sl", "sm", "sn", "so", "sq", "sr", "ss", "st", "su", "sv", "sw",
  "ta", "te", "tg", "th", "ti", "tk", "tl", "tn", "to", "tr", "ts", "tt", "tw", "ty",
  "ug", "uk", "ur", "uz",
  "ve", "vi", "vo",
  "wa", "wo",
  "xh",
  "yi", "yo",
  "za", "zh", "zu",
]);

function languageForUrl(url) {
  try {
    const segments = new URL(url).pathname.split("/").filter(Boolean);
    if (!segments.length) return "Default";
    const code = segments[0].toLowerCase().split("-")[0].split("_")[0];
    return code.length === 2 && ISO_639_1_CODES.has(code) ? code : "Default";
  } catch (e) {
    return "Default";
  }
}

function renderLanguageGroups(pages) {
  const groups = {};
  for (const p of pages) {
    const lang = languageForUrl(p.url);
    if (!groups[lang]) groups[lang] = { words: 0, count: 0 };
    groups[lang].words += p.word_count || 0;
    groups[lang].count += 1;
  }
  const section = document.getElementById("languages");
  const rows = Object.entries(groups).sort((a, b) => b[1].words - a[1].words);
  // A single group (e.g. every crawl with no multi-language filter applied)
  // isn't worth a whole extra table — only show this when it's informative.
  if (rows.length < 2) {
    section.style.display = "none";
    return;
  }
  const maxWords = rows[0][1].words || 1;
  const tbody = document.getElementById("language-tbody");
  renderExpandableTableRows(tbody, rows, 3, (row) => renderFolderRow(row, maxWords));
  section.style.display = "";
}

function renderPageListItem(page, i) {
  const li = document.createElement("li");
  const rank = document.createElement("span");
  rank.className = "rank";
  rank.textContent = i + 1;
  const path = document.createElement("span");
  path.className = "path";
  path.title = page.url;
  path.textContent = page.url;
  const words = document.createElement("span");
  words.className = "words";
  words.textContent = page.word_count.toLocaleString(LOCALE);
  li.append(rank, path, words);
  return li;
}

// Shows the first SUMMARY_ROW_LIMIT rows with a link that reveals the rest.
// `label` overrides the link text when the list itself is already a cut down
// version of a much longer one.
function fillExpandableList(list, rows, total, label) {
  const fill = (items) => {
    list.innerHTML = "";
    for (const row of items) list.appendChild(row);
  };
  fill(rows.slice(0, SUMMARY_ROW_LIMIT));
  if (rows.length <= SUMMARY_ROW_LIMIT) return;

  const li = document.createElement("li");
  li.className = "show-all-row";
  const link = document.createElement("a");
  link.href = "#";
  link.textContent = label || `Show all ${total.toLocaleString(LOCALE)}`;
  link.addEventListener("click", (e) => {
    e.preventDefault();
    fill(rows);
  });
  li.appendChild(link);
  list.appendChild(li);
}

function renderTopPages(pages) {
  const top = pages.filter((p) => p.success).sort((a, b) => b.word_count - a.word_count);
  fillExpandableList(
    document.getElementById("top-pages-list"),
    top.map((p, i) => renderPageListItem(p, i)),
    top.length,
    null,
  );
}

function pagesToCsv(pages) {
  const escapeCell = (value) => {
    const s = String(value);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  // Same columns as the server's export (app/main.py, export_csv).
  const rows = [["URL", "Title", "Words", "Repeated words", "Status", "Error"]];
  for (const p of pages) {
    const status = p.blocked_by_host ? "blocked" : p.login_required ? "login_required" : p.success ? "ok" : "failed";
    const repeated = p.success && p.repeated_words != null ? p.repeated_words : "";
    rows.push([p.url, p.title || "", p.success ? p.word_count : "", repeated, status, p.error || ""]);
  }
  return rows.map((row) => row.map(escapeCell).join(",")).join("\r\n");
}

function downloadCsv(filename, csvText) {
  const blob = new Blob([csvText], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function hostForUrl(url) {
  try {
    return new URL(url).hostname.toLowerCase().replace(/^www\./, "");
  } catch (e) {
    return "";
  }
}

// The run's scope, for the subdomain note; set with the settings pills.
let reportScope = "all";

function renderHostNote(crawledCount, leftOut, censusRecorded) {
  const note = document.getElementById("host-note");
  let text = "";
  if (reportScope !== "all") {
    text = t("hosts_scope_limited", { scope: DOMAIN_SCOPE_LABELS[reportScope] || reportScope });
  } else if (crawledCount <= 1 && !leftOut.length && censusRecorded) {
    text = t("hosts_only_one");
  }
  note.textContent = text;
  note.hidden = !text;
}

function renderLeftOut(listId, blockId, rows, label) {
  const block = document.getElementById(blockId);
  const list = document.getElementById(listId);
  list.innerHTML = "";
  for (const row of rows) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = label(row.name);
    const count = document.createElement("span");
    count.className = "muted";
    count.textContent = row.linked_from === 1
      ? t("linked_from_one")
      : t("linked_from_many", { n: row.linked_from.toLocaleString(LOCALE) });
    li.append(name, count);
    list.appendChild(li);
  }
  block.hidden = !rows.length;
}

// The repetition estimate (app/repetition.py): new / repeated / near words.
// `runTotal` is the report's own total: the estimate covers all of it unless a
// restart cut the tally short, and the summary band shows the difference.
function renderRepetition(rep, duplicatePages, runTotal) {
  const section = document.getElementById("repetition");
  if (!rep || !rep.words_total) {
    section.hidden = true;
    return;
  }
  const total = rep.words_total;
  const near = rep.words_near || 0;
  const repeated = rep.words_repeated || 0;
  const fresh = Math.max(0, total - repeated - near);
  const pct = new Intl.NumberFormat(LOCALE, { style: "percent", maximumFractionDigits: 0 });
  const fill = (key, words) => {
    document.getElementById(`rep-bar-${key}`).style.width = `${(words / total) * 100}%`;
    const figure = document.getElementById(`rep-${key}`);
    figure.querySelector(".rep-pct").textContent = pct.format(words / total);
    figure.querySelector(".rep-words").textContent = t("n_words", { n: n(words) });
  };
  fill("new", fresh);
  fill("repeated", repeated);
  fill("near", near);

  const extra = [];
  if (rep.partial) extra.push(t("rep_partial", { words: n(total) }));
  if (duplicatePages === 1) extra.push(t("rep_duplicates_one"));
  else if (duplicatePages > 1) extra.push(t("rep_duplicates_many", { n: n(duplicatePages) }));
  document.getElementById("rep-extra").textContent = extra.join(" ");

  const whole = Math.max(runTotal || 0, total);
  const unchecked = whole - total;
  const pill = document.getElementById("rep-coverage-pill");
  pill.hidden = !unchecked;
  document.getElementById("rep-coverage-text").textContent =
    t("rep_covers", { checked: n(total), total: n(whole) });

  // Words each = the segment's own length: a repeat adds it once per time seen
  // after the first.
  const rows = (rep.top || []).map((row) => ({
    text: row.text,
    seen: row.count,
    each: Math.round(row.words / Math.max(1, row.count - 1)),
    words: row.words,
  }));
  renderExpandableTableRows(document.getElementById("rep-top"), rows, 4, (row) => {
    const tr = document.createElement("tr");
    const text = document.createElement("td");
    text.className = "rep-snippet";
    const clamp = document.createElement("div");
    clamp.className = "clamp-2";
    clamp.textContent = row.text;
    text.title = row.text;
    text.appendChild(clamp);
    const cells = [text];
    for (const [value, cls] of [[`${n(row.seen)}×`, "muted"], [n(row.each), "muted"], [n(row.words), "strong"]]) {
      const td = document.createElement("td");
      td.className = `col-num ${cls}`;
      td.textContent = value;
      cells.push(td);
    }
    tr.append(...cells);
    return tr;
  });
  document.getElementById("rep-top-block").hidden = !rows.length;
  section.hidden = false;

  // The summary band: the repeated share next to the total, and the total as
  // one bar — with the part a restart left unchecked, if any.
  document.getElementById("hero-repeated").hidden = false;
  document.getElementById("hero-repeated-pct").textContent = pct.format(repeated / total);
  document.getElementById("hero-repeated-sub").textContent = unchecked
    ? t("hero_rep_partial", { n: n(total) })
    : t("n_words", { n: n(repeated) });
  const share = (words) => `${(words / whole) * 100}%`;
  document.getElementById("cov-new").style.width = share(fresh);
  document.getElementById("cov-repeated").style.width = share(repeated);
  document.getElementById("cov-near").style.width = share(near);
  document.getElementById("cov-unchecked").style.width = share(unchecked);
  document.getElementById("cov-new-n").textContent = n(fresh);
  document.getElementById("cov-repeated-n").textContent = n(repeated);
  document.getElementById("cov-near-n").textContent = n(near);
  document.getElementById("cov-unchecked-n").textContent = n(unchecked);
  document.getElementById("cov-unchecked-item").hidden = !unchecked;
  const rerun = document.getElementById("cov-rerun");
  if (rerun) rerun.hidden = !unchecked;
  document.getElementById("coverage").hidden = false;
}

// "64% of the words are in /blog" — worth saying once there is more than one
// folder to compare. Rows are {name, pages, words}, largest first.
function renderFoldersLead(rows) {
  const lead = document.getElementById("folders-lead");
  const total = rows.reduce((sum, row) => sum + row.words, 0);
  if (rows.length < 2 || !total) {
    lead.hidden = true;
    return;
  }
  const top = rows[0];
  const pct = new Intl.NumberFormat(LOCALE, { style: "percent", maximumFractionDigits: 0 });
  lead.textContent = t(top.pages === 1 ? "folders_lead_one" : "folders_lead", {
    pct: pct.format(top.words / total),
    folder: top.name,
    words: n(top.words),
    pages: n(top.pages),
  });
  lead.hidden = false;
}

// What the crawl found but didn't add to the total: the summary band's "Not
// counted", the cards in the Left out section, and its link in the nav.
const leftOutCounts = { duplicates: 0, login: 0, blocked: 0, failed: 0 };

function setLeftOut(changes) {
  Object.assign(leftOutCounts, changes);
  const c = leftOutCounts;
  const total = c.duplicates + c.login + c.blocked + c.failed;
  const totalEl = document.getElementById("not-counted");
  totalEl.textContent = n(total);
  totalEl.classList.toggle("zero", total === 0);
  const parts = [];
  if (c.duplicates) parts.push(t(c.duplicates === 1 ? "nc_duplicates_one" : "nc_duplicates", { n: n(c.duplicates) }));
  if (c.login) parts.push(t("nc_login", { n: n(c.login) }));
  if (c.blocked) parts.push(t("nc_blocked", { n: n(c.blocked) }));
  if (c.failed) parts.push(t("nc_failed", { n: n(c.failed) }));
  document.getElementById("not-counted-sub").textContent = parts.length ? parts.join(" · ") : t("nc_none");
  for (const [key, id] of [["duplicates", "lo-duplicates"], ["login", "lo-login"], ["blocked", "lo-blocked"], ["failed", "lo-failed"]]) {
    document.getElementById(id).hidden = !c[key];
    document.getElementById(`${id}-n`).textContent = n(c[key]);
  }
  document.getElementById("left-out").hidden = total === 0;
  document.getElementById("nav-left-out-count").textContent = n(total);
  syncReportNav();
}

// The section links: one per section the report actually shows.
function syncReportNav() {
  for (const link of document.querySelectorAll("#report-nav .report-nav-links a")) {
    const target = document.getElementById(link.hash.slice(1));
    link.hidden = !target || target.hidden || target.getClientRects().length === 0;
  }
  if (window.updateReportNav) window.updateReportNav();
}

// Underlines the section being read, and shows the run's total in the bar once
// it has stuck to the top of the window and the summary band is out of sight.
function initReportNav() {
  const nav = document.getElementById("report-nav");
  if (!nav) return;
  const links = [...nav.querySelectorAll(".report-nav-links a")];
  let queued = false;
  const update = () => {
    queued = false;
    const offset = nav.offsetHeight + 24;
    const visible = links.filter((link) => !link.hidden);
    let current = visible[0];
    for (const link of visible) {
      const target = document.getElementById(link.hash.slice(1));
      if (target && target.getBoundingClientRect().top - offset <= 0) current = link;
    }
    const scrollable = document.documentElement.scrollHeight > window.innerHeight + 8;
    if (scrollable && window.scrollY > 0
        && window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) {
      current = visible[visible.length - 1];
    }
    for (const link of links) {
      if (link === current) link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    }
    nav.classList.toggle("is-stuck", window.scrollY > 0 && nav.getBoundingClientRect().top <= 1);
  };
  const schedule = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(update);
  };
  window.addEventListener("scroll", schedule, { passive: true });
  window.addEventListener("resize", schedule);
  window.updateReportNav = schedule;
  update();
}

// Names for the scripts app/language_check.py tells apart.
const SCRIPT_NAMES = {
  latin: "Latin", greek: "Greek", cyrillic: "Cyrillic", armenian: "Armenian", hebrew: "Hebrew",
  arabic: "Arabic", devanagari: "Devanagari", bengali: "Bengali", thai: "Thai", georgian: "Georgian",
  hangul: "Korean", cjk: "Chinese/Japanese",
};

function renderSetAside(rows) {
  const block = document.getElementById("language-set-aside");
  const list = document.getElementById("language-set-aside-list");
  list.innerHTML = "";
  for (const row of rows) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    const label = row.name.startsWith("script:")
      ? t("written_in", { script: SCRIPT_NAMES[row.name.slice(7)] || row.name.slice(7) })
      : row.name;
    let path = "";
    try { path = new URL(row.example).pathname; } catch (e) { /* no example */ }
    name.textContent = path ? `${label} · ${t("example_page", { path })}` : label;
    const counts = document.createElement("span");
    counts.className = "muted";
    const parts = [t("set_aside_read", { n: row.pages.toLocaleString(LOCALE) })];
    if (row.skipped) parts.push(t("set_aside_skipped", { n: row.skipped.toLocaleString(LOCALE) }));
    counts.textContent = parts.join(" · ");
    li.append(name, counts);
    list.appendChild(li);
  }
  block.hidden = !rows.length;
}

function renderHostGroups(pages) {
  const groups = {};
  for (const p of pages) {
    const host = hostForUrl(p.url);
    if (!groups[host]) groups[host] = { words: 0, count: 0 };
    groups[host].words += p.word_count || 0;
    groups[host].count += 1;
  }
  const rows = Object.entries(groups).sort((a, b) => b[1].words - a[1].words);
  const maxWords = rows.length ? rows[0][1].words : 1;
  renderExpandableTableRows(document.getElementById("host-tbody"), rows, 3, (row) => renderFolderRow(row, maxWords));
  renderHostNote(rows.length, [], false);
}

function renderSummary(pages) {
  renderTopPages(pages);
  renderFolderGroups(pages);
  renderHostGroups(pages);
  renderLanguageGroups(pages);
  document.getElementById("summary").style.display = "";
  syncReportNav();
}

// Saved reports get their totals already grouped by the server, so the browser
// never has to hold every crawled page just to add up folders. See app/report.py.
function renderServerSummary(summary, runTotal) {
  const topList = document.getElementById("top-pages-list");
  const topRows = summary.top_pages.map((p, i) => renderPageListItem(p, i));
  fillExpandableList(topList, topRows, summary.top_pages.length,
    summary.top_pages_capped ? `Show top ${summary.top_pages.length.toLocaleString(LOCALE)}` : null);

  const renderGroups = (rows, tbodyId) => {
    const max = rows.length ? rows[0].words : 1;
    renderExpandableTableRows(document.getElementById(tbodyId), rows, 3, (row) => renderFolderRow(row, max));
  };
  renderGroups(summary.folders, "folder-tbody");
  renderFoldersLead(summary.folders);
  renderRepetition(summary.repetition, summary.duplicate_pages || 0, runTotal);

  if (summary.scope) reportScope = summary.scope;
  renderGroups(summary.hosts || [], "host-tbody");
  const skippedHosts = summary.skipped_hosts || [];
  renderHostNote((summary.hosts || []).length, skippedHosts, !!summary.census_recorded);
  document.getElementById("host-left-out-note").textContent = t("hosts_left_out");
  renderLeftOut("host-left-out-list", "host-left-out", skippedHosts, (name) => name);

  // Shown when it says something: two or more languages counted, or pages and
  // folders left out as other languages — "ignored or counted?" answered.
  const section = document.getElementById("languages");
  const skippedLanguages = summary.skipped_languages || [];
  const setAside = summary.set_aside_languages || [];
  const multiple = summary.languages.length >= 2;
  document.getElementById("language-table").hidden = !multiple;
  if (multiple) renderGroups(summary.languages, "language-tbody");
  renderSetAside(setAside);
  renderLeftOut("language-left-out-list", "language-left-out", skippedLanguages, (code) => `/${code}/`);
  document.getElementById("language-note").hidden = !(setAside.length || skippedLanguages.length);
  section.style.display = multiple || skippedLanguages.length || setAside.length ? "" : "none";

  document.getElementById("summary").style.display = "";
  syncReportNav();
}

function initCrawlPage(opts) {
  const statusBadge = document.getElementById("status-badge");
  const statusDot = document.getElementById("status-dot");
  const statusText = document.getElementById("status-text");
  const totalWordsEl = document.getElementById("total-words");
  const pagesHeadingCountEl = document.getElementById("pages-heading-count");
  const limitNote = document.getElementById("limit-note");
  const cancelNote = document.getElementById("cancel-note");
  const pausedPastNote = document.getElementById("paused-past-note");
  const errorBox = document.getElementById("error-box");
  const tbody = document.getElementById("page-tbody");
  const cancelBtn = document.getElementById("cancel-btn");
  const actionRow = document.getElementById("action-row");
  const recrawlForm = document.getElementById("recrawl-form");
  const recrawlBtn = document.getElementById("recrawl-btn");
  const recrawlDomainScopeSelect = document.getElementById("recrawl-domain-scope");
  const recrawlExclusionsInput = document.getElementById("recrawl-exclusions");
  // Prefilled once, never on later status events — otherwise a re-render
  // mid-crawl would wipe out whatever is being typed into it.
  let recrawlExclusionsPrefilled = false;
  // The exclusions this crawl is running with, kept current from the status
  // stream so the estimate panel can seed itself whenever it opens.
  let currentExclusions = opts.exclusions || "";
  const prefillRecrawlExclusions = (exclusions) => {
    if (recrawlExclusionsPrefilled || !recrawlExclusionsInput) return;
    recrawlExclusionsPrefilled = true;
    if (exclusions) recrawlExclusionsInput.value = exclusions;
  };
  const recrawlLanguageInput = document.getElementById("recrawl-language");
  const printBtn = document.getElementById("print-btn");
  const exportCsvBtn = document.getElementById("export-csv-btn");
  const runDateEl = document.getElementById("run-date");
  const estimatePanel = document.getElementById("estimate-panel");
  const estimatePagesFetchedEl = document.getElementById("estimate-pages-fetched");
  const estimateTotalPagesEl = document.getElementById("estimate-total-pages");
  const estimateAvgWordsEl = document.getElementById("estimate-avg-words");
  const estimateTotalWordsEl = document.getElementById("estimate-total-words");
  const estimateMessageEl = document.getElementById("estimate-message");
  const estimateConfidenceBadge = document.getElementById("estimate-confidence-badge");
  const estimateCmsPill = document.getElementById("estimate-cms-pill");
  const estimateConcurrencyPill = document.getElementById("estimate-concurrency-pill");
  const estimateDurationEl = document.getElementById("estimate-duration");
  const estimateSpeedEl = document.getElementById("estimate-speed");
  const queuedPanel = document.getElementById("queued-panel");
  const queuedMessageEl = document.getElementById("queued-message");
  const emailNotice = document.getElementById("email-notice");
  const proceedBtn = document.getElementById("proceed-btn");
  const adjustBtn = document.getElementById("adjust-btn");

  const shareBtn = document.getElementById("share-btn");
  const shareModal = document.getElementById("share-modal");
  const shareModalClose = document.getElementById("share-modal-close");
  const sharePublicToggle = document.getElementById("share-public-toggle");
  const shareLinkRow = document.getElementById("share-link-row");
  const shareLinkInput = document.getElementById("share-link-input");
  const shareCopyBtn = document.getElementById("share-copy-btn");
  const shareEmailForm = document.getElementById("share-email-form");
  const shareEmailInput = document.getElementById("share-email-input");
  const shareEmailStatus = document.getElementById("share-email-status");
  const peopleList = document.getElementById("people-list");
  const emailNoticeLabel = document.getElementById("email-notice-label");

  const pageMore = document.getElementById("page-more");
  const markdownStat = document.getElementById("markdown-stat");
  const markdownSizeEl = document.getElementById("markdown-size");
  const markdownPagesEl = document.getElementById("markdown-pages");
  const markdownBtn = document.getElementById("markdown-btn");
  const markdownNote = document.getElementById("markdown-note");
  const pageIssuesNote = document.getElementById("page-issues-note");
  const pageIssuesDetails = document.getElementById("page-issues-details");
  const pageIssuesList = document.getElementById("page-issues-list");
  const tryDifferentPageNote = document.getElementById("try-different-page-note");

  const setEmailNotice = (status) => {
    // Absent on a shared report — there's no owner in that view to email.
    if (!emailNotice) return;
    emailNoticeLabel.textContent = EMAILED_STATUSES.includes(status)
      ? "Summary emailed to"
      : "Summary will be emailed to";
    emailNotice.style.display = "inline";
  };

  const showTryDifferentPageNote = (totalWords, pageCount) => {
    // Only meaningful once a crawl has actually finished — total_words is
    // naturally 0 for the first moment of any crawl, not just a failed one.
    tryDifferentPageNote.style.display = (pageCount > 0 && totalWords === 0) ? "block" : "none";
  };

  const renderIssuesList = (issues) => {
    pageIssuesList.innerHTML = "";
    for (const p of issues) {
      const li = document.createElement("li");
      const urlSpan = document.createElement("span");
      urlSpan.className = "issue-url";
      urlSpan.textContent = p.url;
      const reasonSpan = document.createElement("span");
      reasonSpan.className = "issue-reason";
      reasonSpan.textContent = (p.blocked_by_host ? "Blocked: " : "Failed: ") + (p.error || "Unknown reason");
      li.append(urlSpan, reasonSpan);
      pageIssuesList.appendChild(li);
    }
  };

  const updateBlockedHostCount = (pages) => {
    const blocked = pages.filter((p) => p.blocked_by_host);
    const otherFailed = pages.filter((p) => !p.success && !p.blocked_by_host);
    setLeftOut({ blocked: blocked.length, failed: otherFailed.length });

    // Deliberately no quoted error text here — some of those messages are
    // raw crawl4ai/Playwright internals (file paths, line numbers) that are
    // meaningless to a user. Full detail per page is one click away inside the
    // chip, where technical text is fine because it's clearly a diagnostic
    // list rather than the headline.
    //
    // A chip has room for a count, not a sentence: "1 blocked · 2 failed", with
    // the full wording carried by the title attribute for anyone who hovers.
    const parts = [];
    const spoken = [];
    if (blocked.length) {
      parts.push(`${blocked.length.toLocaleString(LOCALE)} blocked`);
      spoken.push(blocked.length === 1
        ? "1 page was blocked by this site's own bot detection."
        : `${blocked.length.toLocaleString(LOCALE)} pages were blocked by this site's own bot detection.`);
    }
    if (otherFailed.length) {
      parts.push(`${otherFailed.length.toLocaleString(LOCALE)} failed`);
      spoken.push(otherFailed.length === 1
        ? "1 page failed to load."
        : `${otherFailed.length.toLocaleString(LOCALE)} pages failed to load.`);
    }

    if (!parts.length) {
      pageIssuesDetails.style.display = "none";
      return;
    }
    pageIssuesNote.textContent = parts.join(" · ");
    showIssuesMenuItem(parts);
    renderIssuesList([...blocked, ...otherFailed]);
    pageIssuesDetails.style.display = "";
  };

  // True while the crawl is running: the band says "so far" and shows the
  // status strip with Cancel.
  let running = false;
  let pageCountNow = 0;
  const updatePageCount = (count) => {
    pageCountNow = count;
    const key = (count === 1 ? "hero_pages_one" : "hero_pages") + (running ? "_so_far" : "");
    const parts = [t(key, { n: n(count) })];
    if (opts.detectedCms) parts.push(t("built_with", { cms: opts.detectedCms }));
    document.getElementById("hero-sub").textContent = parts.join(" · ");
    pagesHeadingCountEl.textContent = n(count);
    pagesHeadingCountEl.hidden = !count;
    const navCount = document.getElementById("nav-pages-count");
    navCount.textContent = n(count);
    navCount.hidden = !count;
  };

  const host = hostForUrl(opts.displayUrl || opts.sourceUrl);
  const setTotalWords = (count) => {
    totalWordsEl.textContent = n(count);
    document.getElementById("nav-total").textContent = `${host} · ${t("n_words", { n: n(count) })}`;
  };

  const updateSettingsPills = (domainScope, languageSetting, detectedLanguage, exclusions) => {
    const languages = languageSetting
      ? parseLanguageList(languageSetting)
      : detectedLanguage
        ? [detectedLanguage]
        : [];
    renderSettingsPills(domainScope, languages, !languageSetting && !!detectedLanguage, exclusions);
  };

  // Built the first time the panel is shown, seeded with the exclusions the
  // crawl is already running with. An empty box here would read as "none set",
  // and proceeding would look like it silently dropped them.
  let estimateTags = null;

  const CONFIDENCE_LABELS = { high: "High confidence", medium: "Medium confidence", low: "Low confidence" };

  const showEstimatePanel = (result) => {
    if (!result) return;
    estimatePagesFetchedEl.textContent = result.pages_fetched.toLocaleString(LOCALE);
    estimateTotalPagesEl.textContent = result.total_pages_estimate.toLocaleString(LOCALE);
    estimateAvgWordsEl.textContent = result.avg_words_per_page.toLocaleString(LOCALE);
    estimateTotalWordsEl.textContent = result.estimated_total_words.toLocaleString(LOCALE);

    estimateConfidenceBadge.textContent = CONFIDENCE_LABELS[result.confidence] || "";
    estimateConfidenceBadge.className = "pill confidence-badge confidence-" + (result.confidence || "low");

    if (result.detected_cms) {
      estimateCmsPill.textContent = result.detected_cms === "Contentful"
        ? "Detected platform: Contentful (headless — sitemap conventions vary)"
        : `Detected platform: ${result.detected_cms}`;
      estimateCmsPill.style.display = "";
    } else {
      estimateCmsPill.style.display = "none";
    }

    const pagesText = result.total_pages_estimate.toLocaleString(LOCALE);
    estimateMessageEl.textContent = result.sitemap_found
      ? `Found a sitemap — this site has approximately ${pagesText} pages. Crawling all of them may take a while.`
      : `No sitemap found — this estimate is based only on pages discovered so far (approximately ${pagesText}), so it may be less accurate. Crawling all of them may take a while.`;

    estimateDurationEl.textContent = formatDuration(result.estimated_duration_seconds);
    // The upsell sits on the estimate because this is the one screen where
    // somebody is actively weighing up how long they're willing to wait.
    const proEl = document.getElementById("estimate-pro-note");
    if (proEl) {
      const proSeconds = result.estimated_duration_seconds_pro;
      const show = opts.billingEnabled && !opts.isPro && proSeconds;
      proEl.style.display = show ? "" : "none";
      if (show) {
        proEl.innerHTML =
          `about <strong>${formatDuration(proSeconds).replace(/^~/, "")}</strong> on Pro · ` +
          `<a href="/pricing">See what that costs →</a>`;
      }
    }
    estimateSpeedEl.textContent =
      `${result.words_per_minute.toLocaleString(LOCALE)} words/min` +
      ` · ${result.pages_per_minute.toLocaleString(LOCALE)} pages/min`;
    estimateConcurrencyPill.textContent = concurrencyLabel(result.concurrent_crawls) + " server load";

    if (!estimateTags) {
      estimateTags = initTagInput(document.getElementById("estimate-exclusions"), currentExclusions);
    }
    actionRow.style.display = "none";
    estimatePanel.style.display = "block";
  };

  const suggestedLanguage = suggestLanguageFromUrl(opts.sourceUrl);
  if (suggestedLanguage) recrawlLanguageInput.value = suggestedLanguage;

  printBtn.addEventListener("click", () => window.print());

  const deleteBtn = document.getElementById("delete-btn");
  if (deleteBtn) {
    // Only offered once there's a saved run to delete — mid-crawl the server
    // refuses anyway, since the crawler would re-save the row on finishing.
    if (opts.mode !== "live") deleteBtn.style.display = "";
    deleteBtn.addEventListener("click", async () => {
      if (!confirm(`Delete this crawl of ${opts.displayUrl || opts.sourceUrl}? This can't be undone.`)) return;
      deleteBtn.disabled = true;
      deleteBtn.textContent = "Deleting…";
      const res = await fetch("/crawl/" + opts.runId, { method: "DELETE" });
      if (res.ok) {
        window.location.href = "/";
        return;
      }
      const data = await res.json().catch(() => ({}));
      deleteBtn.disabled = false;
      deleteBtn.textContent = "Delete run";
      alert(data.detail || "Could not delete this crawl.");
    });
  }

  let currentPages = opts.initialPages || [];
  exportCsvBtn.addEventListener("click", () => {
    const host = (() => {
      try {
        return new URL(opts.sourceUrl).hostname;
      } catch (e) {
        return "crawl";
      }
    })();
    // A saved report only holds the first slice of rows, so the file is built
    // and streamed by the server. A live crawl isn't saved yet, so that one is
    // still assembled here from what has streamed in so far.
    if (opts.mode === "live") {
      downloadCsv(`${host}-word-count.csv`, pagesToCsv(currentPages));
    } else {
      window.location.href = `/crawl/${opts.runId}/export.csv`;
    }
  });

  // Same chip as updateBlockedHostCount(), but driven by the server's counts
  // instead of counting a pages array the saved report no longer holds.
  const applyIssueSummary = (summary) => {
    setLeftOut({ blocked: summary.blocked_count || 0, failed: summary.failed_count || 0 });
    const parts = [];
    const spoken = [];
    if (summary.blocked_count) {
      parts.push(`${summary.blocked_count.toLocaleString(LOCALE)} blocked`);
      spoken.push(summary.blocked_count === 1
        ? "1 page was blocked by this site's own bot detection."
        : `${summary.blocked_count.toLocaleString(LOCALE)} pages were blocked by this site's own bot detection.`);
    }
    if (summary.failed_count) {
      parts.push(`${summary.failed_count.toLocaleString(LOCALE)} failed`);
      spoken.push(summary.failed_count === 1
        ? "1 page failed to load."
        : `${summary.failed_count.toLocaleString(LOCALE)} pages failed to load.`);
    }
    if (!parts.length) {
      pageIssuesDetails.style.display = "none";
      return;
    }
    pageIssuesNote.textContent = parts.join(" · ");
    showIssuesMenuItem(parts);
    renderIssuesList(summary.issues);
    if (summary.issues_capped) {
      const li = document.createElement("li");
      li.className = "muted";
      li.textContent = `Showing the first ${summary.issues.length.toLocaleString(LOCALE)} — export the CSV for the rest.`;
      pageIssuesList.appendChild(li);
    }
    pageIssuesDetails.style.display = "";
  };

  const MARKDOWN_STOP_REASONS = {
    stopped_run_cap: "the per-crawl size limit was reached",
    stopped_disk: "the server was running low on disk space",
    stopped_global_cap: "the server's total Markdown limit was reached",
    error: "of an error while saving",
  };

  // `terminal` gates the download: mid-crawl the archive would be a moving
  // target, and the button carries the size so the weight is known before the
  // click rather than after it starts.
  const applyMarkdown = (md, terminal) => {
    if (!md || !md.enabled) return;
    // "0 MB · 0 pages" says nothing; the figure appears with the first page saved.
    markdownStat.style.display = md.pages ? "" : "none";
    markdownSizeEl.textContent = formatBytes(md.bytes);
    markdownPagesEl.textContent = t(md.pages === 1 ? "n_pages_one" : "n_pages", { n: n(md.pages) });

    if (markdownBtn) {
      markdownBtn.style.display = md.pages ? "" : "none";
      markdownBtn.disabled = !terminal;
      markdownBtn.textContent = terminal
        ? `Download Markdown · ${formatBytes(md.bytes)}`
        : "Download Markdown";
      markdownBtn.title = terminal ? "" : "Available when the crawl finishes";
    }

    const reason = MARKDOWN_STOP_REASONS[md.state];
    if (reason && terminal) {
      markdownNote.textContent =
        `Markdown was saved for the first ${md.pages.toLocaleString(LOCALE)} pages, then stopped because ` +
        `${reason}. The word count below is complete either way.`;
      markdownNote.style.display = "block";
    }
  };

  if (markdownBtn) {
    markdownBtn.addEventListener("click", () => {
      window.location.href = `/crawl/${opts.runId}/markdown.zip`;
    });
  }

  const initPagePaging = (total, shown) => {
    let loaded = shown;
    const note = document.getElementById("page-more-note");
    const btn = document.getElementById("page-more-btn");

    const update = () => {
      if (loaded >= total) {
        pageMore.style.display = total > PAGE_LIST_MAX ? "flex" : "none";
        btn.style.display = "none";
        return;
      }
      pageMore.style.display = "flex";
      if (loaded >= PAGE_LIST_MAX) {
        btn.style.display = "none";
        note.textContent =
          `Showing the first ${loaded.toLocaleString(LOCALE)} of ${total.toLocaleString(LOCALE)} pages.` +
          " Export the CSV for the complete list.";
        return;
      }
      btn.style.display = "";
      btn.disabled = false;
      btn.textContent = "Show more";
      note.textContent = `${loaded.toLocaleString(LOCALE)} of ${total.toLocaleString(LOCALE)}`;
    };

    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Loading…";
      const limit = Math.min(500, PAGE_LIST_MAX - loaded);
      try {
        const res = await fetch(`/crawl/${opts.runId}/pages?offset=${loaded}&limit=${limit}`);
        const data = await res.json();
        for (const page of data.pages) renderPageRow(tbody, page, true);
        loaded += data.pages.length;
      } catch (e) {
        note.textContent = "Couldn't load more pages.";
      }
      update();
    });

    update();
  };

  const liveStrip = document.getElementById("live-strip");
  const heroLabel = document.querySelector(".hero-label");
  const setStatus = (status) => {
    statusBadge.className = badgeClass(status);
    statusText.textContent = statusLabel(status);
    statusDot.innerHTML = status === "crawling" ? '<span class="pulse-dot"></span>' : "";
    running = ["starting", "crawling", "queued"].includes(status);
    liveStrip.hidden = !running;
    (running ? document.getElementById("live-strip-status") : heroLabel).appendChild(statusBadge);
    document.getElementById("live-strip-note").textContent = t(status === "queued" ? "strip_queued" : "strip_crawling");
    updatePageCount(pageCountNow);
  };

  const showLimitNote = () => {
    limitNote.textContent = "The configured page limit was reached — the crawl may be incomplete.";
    limitNote.style.display = "block";
  };

  const showError = (message) => {
    errorBox.textContent = message || "The crawl failed.";
    errorBox.style.display = "block";
  };

  cancelBtn.addEventListener("click", async () => {
    cancelBtn.disabled = true;
    cancelBtn.textContent = "Cancelling…";
    await fetch("/crawl/" + opts.runId + "/cancel", { method: "POST" });
    // The actual status change arrives via the SSE "status" event below.
  });

  // Absent for anyone signed out — a re-crawl needs an account to belong to —
  // and an unguarded bind here throws before a shared report can render.
  if (recrawlForm) recrawlForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const recrawlError = document.getElementById("recrawl-error");
    const recrawlAuth = document.getElementById("recrawl-auth");
    const siteUsername = document.getElementById("recrawl-site-username");
    const sitePassword = document.getElementById("recrawl-site-password");
    recrawlError.hidden = true;
    recrawlBtn.disabled = true;
    recrawlBtn.textContent = t("starting");
    const res = await fetch("/crawl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: opts.sourceUrl,
        domain_scope: recrawlDomainScopeSelect.value,
        language: recrawlLanguageInput.value.trim() || null,
        exclusions: recrawlExclusionsInput.value.trim() || null,
        force_recrawl: true,
        capture_markdown: document.getElementById("recrawl-markdown").checked,
        site_username: siteUsername.value.trim() || null,
        site_password: sitePassword.value || null,
        login_url: document.getElementById("recrawl-login-url").value || null,
        login_email: document.getElementById("recrawl-login-email").value.trim() || null,
        login_password: document.getElementById("recrawl-login-password").value || null,
        login_session: document.getElementById("recrawl-login-session").value.trim() || null,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      if (data.code === "site_password_needed" || data.code === "site_password_rejected") {
        recrawlAuth.hidden = false;
        (siteUsername.value ? sitePassword : siteUsername).focus();
      }
      if (data.code && (data.code.startsWith("site_login_") || data.code.startsWith("site_session_"))) {
        document.getElementById("recrawl-login").hidden = false;
        document.getElementById(data.code.startsWith("site_session_") ? "recrawl-login-session" : "recrawl-login-email").focus();
      }
      recrawlError.textContent = data.code ? t(data.code) : data.detail || t("something_went_wrong");
      recrawlError.hidden = false;
      recrawlBtn.disabled = false;
      recrawlBtn.textContent = t("start_recrawl");
      return;
    }
    window.location.href = "/crawl/" + data.run_id;
  });

  if (shareBtn) {
    const shareUrl = window.location.origin + "/share/" + opts.runId;
    let isPublic = !!opts.initialIsPublic;

    const renderPublicState = (pub) => {
      isPublic = pub;
      sharePublicToggle.setAttribute("aria-checked", pub ? "true" : "false");
      shareLinkInput.value = shareUrl;
      shareLinkRow.style.display = pub ? "flex" : "none";
    };

    const renderPeople = (recipients) => {
      // The owner row is server-rendered; everything after it is an invite.
      while (peopleList.children.length > 1) peopleList.lastElementChild.remove();
      for (const person of recipients || []) {
        const li = document.createElement("li");
        li.className = "person-row";

        const avatar = document.createElement("span");
        avatar.className = "avatar";
        avatar.textContent = (person.email[0] || "?").toUpperCase();

        const id = document.createElement("div");
        id.className = "person-id";
        const name = document.createElement("span");
        name.className = "person-name";
        name.textContent = person.email;
        const sub = document.createElement("span");
        sub.className = "person-email";
        sub.textContent = person.created_at ? "Invited " + formatDate(person.created_at) : "Invited";
        id.append(name, sub);

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "person-remove";
        remove.textContent = "×";
        remove.title = "Remove " + person.email;
        remove.setAttribute("aria-label", "Remove " + person.email);
        remove.addEventListener("click", async () => {
          remove.disabled = true;
          const res = await fetch(
            "/crawl/" + opts.runId + "/share/recipients?email=" + encodeURIComponent(person.email),
            { method: "DELETE" },
          );
          if (res.ok) {
            const data = await res.json();
            renderPeople(data.recipients);
          } else {
            remove.disabled = false;
          }
        });

        li.append(avatar, id, remove);
        peopleList.appendChild(li);
      }
    };

    const openShareModal = () => {
      shareModal.hidden = false;
      shareEmailStatus.textContent = "";
      shareEmailInput.focus();
    };
    const closeShareModal = () => {
      shareModal.hidden = true;
      shareBtn.focus();
    };

    shareBtn.addEventListener("click", openShareModal);
    shareModalClose.addEventListener("click", closeShareModal);
    shareModal.addEventListener("click", (evt) => {
      if (evt.target === shareModal) closeShareModal();
    });
    document.addEventListener("keydown", (evt) => {
      if (evt.key === "Escape" && !shareModal.hidden) closeShareModal();
    });

    sharePublicToggle.addEventListener("click", async () => {
      sharePublicToggle.disabled = true;
      try {
        const res = await fetch("/crawl/" + opts.runId + "/share", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ is_public: !isPublic }),
        });
        const data = await res.json();
        renderPublicState(data.is_public);
      } finally {
        sharePublicToggle.disabled = false;
      }
    });

    shareCopyBtn.addEventListener("click", async () => {
      shareLinkInput.select();
      await navigator.clipboard.writeText(shareLinkInput.value);
      shareCopyBtn.textContent = "Copied!";
      setTimeout(() => { shareCopyBtn.textContent = "Copy link"; }, 1500);
    });

    shareEmailForm.addEventListener("submit", async (evt) => {
      evt.preventDefault();
      const sendBtn = shareEmailForm.querySelector("button");
      sendBtn.disabled = true;
      shareEmailStatus.textContent = "Sending…";
      try {
        const res = await fetch("/crawl/" + opts.runId + "/share/email", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email: shareEmailInput.value.trim() }),
        });
        if (res.ok) {
          const data = await res.json();
          // Inviting someone turns the link on server-side — reflect that here
          // rather than leaving the switch showing a stale "private".
          renderPublicState(data.is_public);
          renderPeople(data.recipients);
          shareEmailInput.value = "";
          shareEmailStatus.textContent = "Sent!";
        } else {
          shareEmailStatus.textContent = "Couldn't send — try again.";
        }
      } finally {
        sendBtn.disabled = false;
      }
    });

    renderPublicState(isPublic);
    renderPeople(opts.initialShareRecipients);
    // Sharing a half-finished report would hand out a link to partial results,
    // so the button waits for the crawl to stop rather than sitting greyed out.
    if (opts.mode === "live") shareBtn.hidden = true;
  }

  // The ⋯ menu drives two panels that live down in the run bar, where they
  // keep their full width. Bound here, before the mode branch: a finished
  // report returns early below, and Re-crawl is precisely a finished-report
  // action — binding it after the return is how it shipped doing nothing.
  const openFromMenu = (buttonId, detailsId) => {
    const item = document.getElementById(buttonId);
    const panel = document.getElementById(detailsId);
    if (!item || !panel) return;
    item.addEventListener("click", () => {
      panel.open = !panel.open;
      if (panel.open) panel.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  };
  openFromMenu("recrawl-open-btn", "recrawl-disclosure");
  openFromMenu("page-issues-open-btn", "page-issues-details");

  // The address the report shows — the owner's to change, within the site that
  // was crawled. A re-crawl still starts from opts.sourceUrl.
  const editUrlBtn = document.getElementById("edit-url-btn");
  const editUrlForm = document.getElementById("edit-url-form");
  if (editUrlBtn && editUrlForm) {
    const heading = document.getElementById("report-url");
    const input = document.getElementById("edit-url-input");
    const error = document.getElementById("edit-url-error");
    const showForm = (show) => {
      editUrlForm.hidden = !show;
      heading.hidden = show;
      editUrlBtn.hidden = show;
      error.hidden = true;
    };
    editUrlBtn.addEventListener("click", () => {
      showForm(true);
      input.focus();
      input.select();
    });
    document.getElementById("edit-url-cancel").addEventListener("click", () => {
      showForm(false);
      editUrlBtn.focus();
    });
    editUrlForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const save = editUrlForm.querySelector('button[type="submit"]');
      save.disabled = true;
      const res = await fetch(`/crawl/${opts.runId}/display-url`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: input.value.trim() || null }),
      });
      if (res.ok) {
        // The title, the tab, the share dialog and the CSV name all follow it.
        window.location.reload();
        return;
      }
      const data = await res.json().catch(() => ({}));
      error.textContent = t(data.code || "something_went_wrong");
      error.hidden = false;
      save.disabled = false;
    });
  }

  // The same panels, from the report itself.
  const openPanel = (panel) => {
    if (!panel) return;
    panel.open = true;
    panel.scrollIntoView({ behavior: "smooth", block: "center" });
  };
  for (const btn of document.querySelectorAll("[data-open-issues]")) {
    btn.addEventListener("click", () => openPanel(pageIssuesDetails));
  }
  const coverageRerun = document.getElementById("cov-rerun");
  if (coverageRerun) {
    coverageRerun.addEventListener("click", () => openPanel(document.getElementById("recrawl-disclosure")));
  }
  initReportNav();

  // The issues item only appears once there is something to look at.
  const issuesOpenBtn = document.getElementById("page-issues-open-btn");
  const showIssuesMenuItem = (parts) => {
    if (!issuesOpenBtn) return;
    issuesOpenBtn.textContent = `${t("view_issues")} · ${parts.join(" · ")}`;
    issuesOpenBtn.style.display = "";
  };

  if (opts.mode === "past" || opts.mode === "shared") {
    if (opts.mode === "shared") {
      actionRow.style.display = "none";
    }
    runDateEl.textContent = "Started: " + formatDate(opts.createdAt);
    setEmailNotice(opts.initialStatus);
    setStatus(opts.initialStatus);
    setTotalWords(opts.initialTotalWords);
    updatePageCount(opts.initialPageCount);
    setLeftOut({ login: opts.initialLoginBlockedCount || 0, duplicates: opts.initialDuplicateCount || 0 });
    renderSettingsPills(opts.domainScope, parseLanguageList(opts.languageSetting), opts.languageAutoDetected, opts.exclusions);
    prefillRecrawlExclusions(opts.exclusions);
    if (opts.initialLimitReached) showLimitNote();
    if (opts.initialStatus === "failed") showError();
    if (opts.initialStatus === "cancelled") cancelNote.style.display = "block";
    if (opts.initialStatus === "paused") pausedPastNote.style.display = "block";
    showTryDifferentPageNote(opts.initialTotalWords, opts.initialPageCount);

    // A saved report is handed totals the server already grouped plus the first
    // slice of rows — never every page. See app/report.py for why.
    const initialPages = opts.initialPages || [];
    for (const page of initialPages) renderPageRow(tbody, page, true);
    applyIssueSummary(opts.summary);
    applyMarkdown(opts.initialMarkdown, true);
    renderServerSummary(opts.summary, opts.initialTotalWords);
    initPagePaging(opts.summary.total_pages, initialPages.length);
    return;
  }

  // live mode
  runDateEl.textContent = "Started: " + formatDate(opts.startedAt);
  setLeftOut({});
  setTotalWords(0);
  updatePageCount(0);
  const seenUrls = new Set();
  const pages = [];
  currentPages = pages; // same array reference, kept in sync as pages.push() happens below

  const applyStatus = (data) => {
    setStatus(data.status);
    setEmailNotice(data.status);
    setTotalWords(data.total_words);
    updatePageCount(data.page_count);
    setLeftOut({ login: data.login_blocked_count || 0, duplicates: data.duplicate_count || 0 });
    updateSettingsPills(data.domain_scope, data.language_setting, data.detected_language, data.exclusions);
    currentExclusions = data.exclusions || "";
    prefillRecrawlExclusions(data.exclusions);
    if (data.limit_reached) showLimitNote();
    if (data.status === "failed") showError(data.error);
    if (data.status === "cancelled") {
      cancelNote.textContent = data.stopped_reason || "This crawl was cancelled — showing partial results.";
      cancelNote.style.display = "block";
    }
    applyMarkdown(data.markdown, TERMINAL_STATUSES.includes(data.status));
    if (TERMINAL_STATUSES.includes(data.status)) {
      cancelBtn.style.display = "none";
      queuedPanel.style.display = "none";
      if (shareBtn) shareBtn.hidden = false;
      showTryDifferentPageNote(data.total_words, data.page_count);
      if (data.status === "paused") {
        showEstimatePanel(data.estimate_result);
      }
    } else {
      cancelBtn.style.display = "";
      if (data.status === "queued") {
        queuedMessageEl.textContent = data.queue_position
          ? `You're #${data.queue_position} in line — this server is busy right now. This will start automatically, and we'll email you when it's done.`
          : "This will start automatically, and we'll email you when it's done.";
        queuedPanel.style.display = "block";
      } else {
        queuedPanel.style.display = "none";
      }
    }
  };

  // Render the job's REAL current status immediately, before ever
  // connecting to SSE — previously this always hardcoded "starting"
  // regardless of the actual status, so reloading a page that had already
  // paused (or finished, or failed) misleadingly showed "Crawling" until
  // the SSE replay caught up moments later.
  if (opts.initialStatusPayload) {
    applyStatus(opts.initialStatusPayload);
  } else {
    setStatus("starting");
    setEmailNotice("starting");
    cancelBtn.style.display = "";
  }

  proceedBtn.addEventListener("click", async () => {
    proceedBtn.disabled = true;
    proceedBtn.textContent = "Resuming…";
    const res = await fetch("/crawl/" + opts.runId + "/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // null, not "", so an untouched field means "keep what the crawl already
      // had" rather than "clear the exclusions".
      body: JSON.stringify({ exclusions: estimateTags ? estimateTags.value() || null : null }),
    });
    if (res.status === 409) {
      const data = await res.json().catch(() => ({}));
      showError(t(data.code || "something_went_wrong"));
      // Left disabled: this crawl can't go on, only a re-crawl can.
      proceedBtn.textContent = t("proceed_with_crawl");
      return;
    }
    estimatePanel.style.display = "none";
    actionRow.style.display = "";
    cancelBtn.style.display = "";
    setStatus("crawling");
    connectEvents();
  });

  adjustBtn.addEventListener("click", () => {
    window.location.href = "/";
  });

  function connectEvents() {
    const source = new EventSource("/events/" + opts.runId);

    source.addEventListener("page", (evt) => {
      const data = JSON.parse(evt.data);
      // A reconnect after "Proceed with crawl" replays every page already
      // known before streaming new ones — skip anything already counted.
      if (seenUrls.has(data.page.url)) return;
      seenUrls.add(data.page.url);
      pages.push(data.page);
      // Stop growing the DOM past the cap — the counts and the summary keep
      // using every page, but a long crawl shouldn't end up with a row per
      // page in the table while you're watching it.
      if (pages.length <= PAGE_LIST_MAX) {
        renderPageRow(tbody, data.page);
      } else if (pages.length === PAGE_LIST_MAX + 1) {
        pageMore.style.display = "flex";
        document.getElementById("page-more-btn").style.display = "none";
        document.getElementById("page-more-note").textContent =
          `Showing the most recent ${PAGE_LIST_MAX.toLocaleString(LOCALE)} pages while this crawl runs.`;
      }
      setTotalWords(data.total_words);
      updatePageCount(pages.length);
      updateBlockedHostCount(pages);
      if (data.markdown_bytes !== undefined) {
        applyMarkdown({ enabled: true, bytes: data.markdown_bytes, pages: data.markdown_pages, state: "capturing" }, false);
      }
    });

    source.addEventListener("login_blocked", (evt) => {
      const data = JSON.parse(evt.data);
      // Intentionally not rendered in the page list — just a running count of
      // pages that turned out to be login walls rather than real content.
      setLeftOut({ login: data.login_blocked_count || 0 });
    });

    source.addEventListener("duplicate", (evt) => {
      const data = JSON.parse(evt.data);
      // Same reasoning as login_blocked: the page was fetched but is the same
      // content as one already counted, so it belongs in neither the list nor
      // the word total — only in this counter.
      setLeftOut({ duplicates: data.duplicate_count || 0 });
    });

    source.addEventListener("status", (evt) => {
      const data = JSON.parse(evt.data);
      applyStatus(data);
      if (TERMINAL_STATUSES.includes(data.status)) {
        renderSummary(pages);
        source.close();
      }
    });

    source.onerror = () => {
      // EventSource auto-retries; if the job is already gone server-side this
      // will just keep failing quietly, which is fine for a local dev tool.
    };

    return source;
  }

  connectEvents();
}


// ------------------------------------------------------------------ projects

async function postJson(url, body, method = "POST") {
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* an empty body */ }
  return { ok: res.ok, data };
}

// A modal-overlay: open, close on its × / Cancel, the backdrop and Escape,
// and give focus back to what opened it.
function bindModal(overlay, closers) {
  let opener = null;
  const open = (from) => {
    opener = from || document.activeElement;
    overlay.hidden = false;
    const first = overlay.querySelector("input:not([type=hidden]):not([type=checkbox]), button[type=submit]");
    if (first) first.focus();
  };
  const close = () => {
    overlay.hidden = true;
    if (opener && opener.focus) opener.focus();
  };
  closers.filter(Boolean).forEach((el) => el.addEventListener("click", close));
  overlay.addEventListener("click", (evt) => { if (evt.target === overlay) close(); });
  document.addEventListener("keydown", (evt) => { if (evt.key === "Escape" && !overlay.hidden) close(); });
  return { open, close };
}

// "Add to project": any button with data-project-run opens it for that run,
// ticked as data-project-ids says. Saving sets the run's projects to the ticks.
function initProjectDialog() {
  const overlay = document.getElementById("project-modal");
  if (!overlay) return;
  const form = document.getElementById("project-form");
  const nameInput = document.getElementById("project-new-name");
  const error = document.getElementById("project-error");
  const modal = bindModal(overlay, [document.getElementById("project-modal-close"), document.getElementById("project-cancel")]);
  let runId = null;

  document.querySelectorAll("[data-project-run]").forEach((btn) => {
    btn.addEventListener("click", () => {
      runId = btn.dataset.projectRun;
      let ticked = [];
      try { ticked = JSON.parse(btn.dataset.projectIds || "[]"); } catch (e) { ticked = []; }
      form.querySelectorAll("input[name=project]").forEach((box) => { box.checked = ticked.includes(box.value); });
      nameInput.value = "";
      error.hidden = true;
      // The ⋯ menu it was picked from closes behind it.
      document.querySelectorAll("[data-menu-panel]").forEach((panel) => { panel.hidden = true; });
      document.querySelectorAll("[data-menu-button]").forEach((b) => b.setAttribute("aria-expanded", "false"));
      modal.open(btn);
    });
  });

  form.addEventListener("submit", async (evt) => {
    evt.preventDefault();
    const save = form.querySelector("button[type=submit]");
    save.disabled = true;
    const projectIds = [...form.querySelectorAll("input[name=project]:checked")].map((box) => box.value);
    const { ok, data } = await postJson("/crawl/" + runId + "/projects",
      { project_ids: projectIds, new_name: nameInput.value.trim() || null });
    save.disabled = false;
    if (!ok) {
      error.textContent = t(data.code || "something_went_wrong");
      error.hidden = false;
      return;
    }
    window.location.reload();
  });
}

// The Projects page: make one.
function initProjectsHome() {
  const form = document.getElementById("project-create-form");
  if (!form) return;
  const error = document.getElementById("project-create-error");
  form.addEventListener("submit", async (evt) => {
    evt.preventDefault();
    const input = document.getElementById("project-create-name");
    const { ok, data } = await postJson("/projects", { name: input.value.trim() });
    if (!ok) {
      error.textContent = t(data.code || "something_went_wrong");
      error.hidden = false;
      return;
    }
    window.location.href = data.url;
  });
}

// A project: rename, share, add and remove runs, delete.
// "What overlapped": Pages | Sentences. Without this script both panels show,
// one under the other.
function initOverlapTabs() {
  const tabs = [...document.querySelectorAll(".overlap-tabs [role=tab]")];
  if (!tabs.length) return;
  const select = (tab, focus) => {
    tabs.forEach((other) => {
      const on = other === tab;
      other.setAttribute("aria-selected", on ? "true" : "false");
      other.tabIndex = on ? 0 : -1;
      const panel = document.getElementById(other.getAttribute("aria-controls"));
      if (panel) panel.hidden = !on;
    });
    if (focus) tab.focus();
  };
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => select(tab));
    tab.addEventListener("keydown", (evt) => {
      if (evt.key !== "ArrowRight" && evt.key !== "ArrowLeft") return;
      evt.preventDefault();
      select(tabs[(i + (evt.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length], true);
    });
  });
  select(tabs.find((tab) => tab.getAttribute("aria-selected") === "true") || tabs[0]);
}

// The Consolidate button, and the progress of a consolidation in the
// background: polls its state and reloads once a newer result is in.
function initConsolidate(base, opts) {
  const btn = document.getElementById("consolidate-btn");
  if (!btn) return;
  const box = document.getElementById("consolidate-progress");
  const bar = document.getElementById("consolidate-bar");
  const step = document.getElementById("consolidate-step");
  const error = document.getElementById("consolidate-error");
  const track = box ? box.querySelector("[role=progressbar]") : null;
  const steps = opts.steps || {};
  const job = opts.job || {};
  const startedGen = job.computed_gen;
  let timer = null;
  let since = 0;

  const show = (state) => {
    const busy = state.status === "queued" || state.status === "running";
    btn.disabled = busy;
    btn.textContent = busy ? t("consolidating") : t("consolidate_again");
    if (box) box.hidden = !busy;
    if (bar) bar.style.width = (state.percent || 0) + "%";
    if (track) track.setAttribute("aria-valuenow", String(state.percent || 0));
    if (step) step.textContent = steps[state.status === "queued" ? "queued" : (state.step || "queued")] || "";
    if (error) {
      error.hidden = state.status !== "failed";
      if (state.status === "failed") error.textContent = t("consolidate_failed", { error: state.error || "" });
    }
    return busy;
  };

  const poll = async () => {
    timer = null;
    if (document.hidden) { schedule(); return; }
    let state;
    try {
      const res = await fetch(base + "/consolidation", { headers: { Accept: "application/json" } });
      if (res.status === 404) return;
      state = await res.json();
    } catch (e) { schedule(); return; }
    const busy = show(state);
    if (state.computed_gen !== startedGen && state.status !== "failed") {
      window.location.reload();
      return;
    }
    if (busy) schedule();
  };
  const schedule = () => {
    if (timer) return;
    since += 1;
    timer = setTimeout(poll, since > 30 ? 5000 : 2000);
  };

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const { ok, data } = await postJson(base + "/consolidate");
    if (!ok) {
      btn.disabled = false;
      if (error) { error.hidden = false; error.textContent = t(data.code || "something_went_wrong"); }
      return;
    }
    since = 0;
    if (show(data)) schedule();
    else if (data.computed_gen !== startedGen) window.location.reload();
  });

  if (job.status === "queued" || job.status === "running") schedule();
}

function initProjectPage(opts) {
  const base = "/projects/" + opts.projectId;

  const renameBtn = document.getElementById("rename-btn");
  const renameForm = document.getElementById("rename-form");
  const title = document.getElementById("project-name");
  if (renameBtn && renameForm) {
    const show = (on) => {
      renameForm.hidden = !on;
      title.hidden = on;
      renameBtn.hidden = on;
      if (on) document.getElementById("rename-input").select();
    };
    renameBtn.addEventListener("click", () => show(true));
    document.getElementById("rename-cancel").addEventListener("click", () => show(false));
    renameForm.addEventListener("submit", async (evt) => {
      evt.preventDefault();
      const name = document.getElementById("rename-input").value.trim();
      const { ok, data } = await postJson(base + "/rename", { name });
      if (!ok) {
        const err = document.getElementById("rename-error");
        err.textContent = t(data.code || "something_went_wrong");
        err.hidden = false;
        return;
      }
      title.textContent = data.name;
      document.title = document.title.replace(/^[^—]*—/, data.name + " —");
      show(false);
    });
  }

  const addOverlay = document.getElementById("add-runs-modal");
  if (addOverlay) {
    const addModal = bindModal(addOverlay, [document.getElementById("add-runs-close"), document.getElementById("add-runs-cancel")]);
    ["add-runs-open", "add-runs-open-inline", "add-runs-open-empty"].forEach((id) => {
      const btn = document.getElementById(id);
      if (btn) btn.addEventListener("click", () => addModal.open(btn));
    });
    const search = document.getElementById("add-runs-search");
    if (search) {
      search.addEventListener("input", () => {
        const q = search.value.trim().toLowerCase();
        addOverlay.querySelectorAll("[data-search]").forEach((row) => { row.hidden = q && !row.dataset.search.includes(q); });
      });
    }
    document.getElementById("add-runs-form").addEventListener("submit", async (evt) => {
      evt.preventDefault();
      const runIds = [...addOverlay.querySelectorAll("input[name=run]:checked")].map((box) => box.value);
      if (!runIds.length) { addModal.close(); return; }
      const { ok, data } = await postJson(base + "/runs", { run_ids: runIds });
      if (!ok) {
        const err = document.getElementById("add-runs-error");
        err.textContent = t(data.code || "something_went_wrong");
        err.hidden = false;
        return;
      }
      window.location.reload();
    });
  }

  initOverlapTabs();
  initConsolidate(base, opts);

  document.querySelectorAll(".project-remove").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = t("removing");
      const { ok } = await postJson(base + "/runs/" + btn.dataset.run, undefined, "DELETE");
      if (ok) window.location.reload();
      else { btn.disabled = false; btn.textContent = t("remove"); }
    });
  });

  const deleteBtn = document.getElementById("delete-project-btn");
  if (deleteBtn) {
    deleteBtn.addEventListener("click", async () => {
      if (!window.confirm(t("confirm_delete_project"))) return;
      const { ok } = await postJson(base, undefined, "DELETE");
      if (ok) window.location.href = "/projects";
    });
  }

  const shareOverlay = document.getElementById("project-share-modal");
  const shareBtn = document.getElementById("project-share-btn");
  if (shareOverlay && shareBtn) {
    const shareModal = bindModal(shareOverlay, [document.getElementById("project-share-close")]);
    shareBtn.addEventListener("click", () => shareModal.open(shareBtn));
    const toggle = document.getElementById("project-public-toggle");
    const linkRow = document.getElementById("project-link-row");
    const linkInput = document.getElementById("project-link-input");
    let isPublic = opts.isPublic;
    toggle.addEventListener("click", async () => {
      toggle.disabled = true;
      const { ok, data } = await postJson(base + "/share", { is_public: !isPublic });
      toggle.disabled = false;
      if (!ok) return;
      isPublic = data.is_public;
      toggle.setAttribute("aria-checked", String(isPublic));
      linkRow.style.display = isPublic ? "" : "none";
      if (data.share_url) linkInput.value = data.share_url;
    });
    document.getElementById("project-copy-btn").addEventListener("click", async () => {
      linkInput.select();
      try { await navigator.clipboard.writeText(linkInput.value); } catch (e) { /* the text is selected */ }
    });
  }
}


// ---------- The MCP guide and connections (templates/mcp*.html) ----------

// Tabs on the guide: one app's steps at a time. Without the script, every
// app's steps show one under the other.
function initDocTabs() {
  const tabs = [...document.querySelectorAll(".doc-tabs [role=tab]")];
  if (!tabs.length) return;
  const select = (tab, focus) => {
    tabs.forEach((other) => {
      const on = other === tab;
      other.setAttribute("aria-selected", on ? "true" : "false");
      other.tabIndex = on ? 0 : -1;
      const panel = document.getElementById(other.getAttribute("aria-controls"));
      if (panel) panel.hidden = !on;
    });
    if (focus) tab.focus();
  };
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => select(tab));
    tab.addEventListener("keydown", (evt) => {
      if (evt.key !== "ArrowRight" && evt.key !== "ArrowLeft") return;
      evt.preventDefault();
      select(tabs[(i + (evt.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length], true);
    });
  });
  const wanted = tabs.find((tab) => location.hash === "#" + tab.getAttribute("aria-controls"));
  select(wanted || tabs.find((tab) => tab.getAttribute("aria-selected") === "true") || tabs[0]);
}

// "Copy" beside a code block copies the block's text.
function initCopyButtons() {
  document.querySelectorAll("[data-copy]").forEach((btn) => {
    if (btn.dataset.copyBound) return;
    btn.dataset.copyBound = "1";
    btn.addEventListener("click", async () => {
      const code = btn.parentElement.querySelector("code");
      if (!code) return;
      try {
        await navigator.clipboard.writeText(code.textContent);
      } catch (e) {
        const range = document.createRange();
        range.selectNodeContents(code);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        return;
      }
      const label = btn.textContent;
      btn.textContent = t("copied");
      setTimeout(() => { btn.textContent = label; }, 1600);
    });
  });
}

// Making a connection shows its URL, command and key once; revoking one
// removes it from the list.
function initMcpConnect(opts) {
  const form = document.getElementById("mcp-key-form");
  if (!form) return;
  const list = document.getElementById("mcp-keys");
  const empty = document.getElementById("mcp-keys-empty");
  const error = document.getElementById("mcp-key-error");
  const panel = document.getElementById("mcp-key-new");

  const bindRevoke = (btn) => {
    btn.addEventListener("click", async () => {
      if (!window.confirm(t("confirm_revoke_key"))) return;
      btn.disabled = true;
      const { ok } = await postJson("/account/mcp-keys/" + btn.dataset.revoke, undefined, "DELETE");
      if (!ok) { btn.disabled = false; return; }
      btn.closest("li").remove();
      if (empty) empty.hidden = list.children.length > 0;
      document.getElementById("mcp-key-create").disabled = false;
    });
  };
  list.querySelectorAll("[data-revoke]").forEach(bindRevoke);

  form.addEventListener("submit", async (evt) => {
    evt.preventDefault();
    const button = document.getElementById("mcp-key-create");
    button.disabled = true;
    error.hidden = true;
    const name = document.getElementById("mcp-key-name").value.trim();
    const { ok, data } = await postJson("/account/mcp-keys", { name });
    button.disabled = false;
    if (!ok) {
      error.textContent = t(data.code || "something_went_wrong");
      error.hidden = false;
      return;
    }
    document.getElementById("mcp-new-url").textContent = data.url;
    document.getElementById("mcp-new-command").textContent =
      "claude mcp add --transport http word-counter " + data.endpoint +
      " \\\n  --header \"Authorization: Bearer " + data.key + "\"";
    document.getElementById("mcp-new-key").textContent = data.key;
    panel.hidden = false;
    panel.scrollIntoView({ behavior: "smooth", block: "start" });

    const item = document.createElement("li");
    item.dataset.keyId = data.id;
    const label = document.createElement("span");
    label.className = "connect-key-name";
    label.textContent = data.name;
    const meta = document.createElement("span");
    meta.className = "connect-key-meta";
    meta.textContent = "wck_…" + data.hint + " · " + t("just_now");
    const revoke = document.createElement("button");
    revoke.type = "button";
    revoke.className = "link-button connect-revoke";
    revoke.dataset.revoke = data.id;
    revoke.textContent = t("revoke");
    item.append(label, meta, revoke);
    list.prepend(item);
    bindRevoke(revoke);
    if (empty) empty.hidden = true;
    document.getElementById("mcp-key-name").value = "";
  });
}
