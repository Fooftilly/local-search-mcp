# Live dashboard updates and optional desktop notifications

**Pending triage order:** The dashboard's domain-filter form also offers
**Recently updated** (unchanged row-update-time default),
**Oldest waiting** (creation time, oldest first), and **Most requests**
(descending request count, then actual last-request time; legacy rows without
that timestamp fall back to row-update time). Ordering affects only the visible pending queue, not persisted
permissions, explicit rule order, saved history, or alert baselines. Every mode
uses a deterministic hostname tie-breaker across pages. The chosen order is
preserved through filtered page navigation, single/bulk/once POST feedback,
manual Refresh, and live snapshots; changing the order uses the normal GET
form and begins at page 1. Only three fixed sort modes are accepted and SQL
ORDER BY clauses are server-owned constants, never user interpolations.
Incoming requests may reorder a live queue; stale IDs still fail closed.

**Pending review pagination:** The dashboard now displays 200 pending domains per
page, with accessible Previous 200 / Next 200 links at both ends of the
review table. The full 512-domain policy-limited queue can be reviewed without
guessing a search term. A page is bounded to a maximum of 20; the database
returns deterministic timestamp/domain order and a separately counted number
of filtered pending rows. Filters and current page are retained through
domain/one-use/bulk decisions, including stale POST error feedback, and
through the existing live polling path. Changing pages does **not** transfer
selected checkboxes or submit any authorization; batch selections remain
page-local and capped at 25 by the server. If the operator
approves the final request on the final page, the server and live dashboard
automatically move to the last valid page rather than rendering an empty
nonexistent one. During simultaneous incoming requests or reviews, items may
shift between pages; stale request IDs still fail closed. The usual 200-per-page table remains the default without
JavaScript.

The home page also shows **24-hour operational totals** from the existing
retention-controlled history index: all operations, searches, fetches, and
non-success outcomes (which includes denied, pending and errors). These are
counts across all recorded statuses, not rankings or uptime percentages; the
existing detailed history table remains available behind its disclosure
control. The first page load displays a snapshot, and an existing same-origin
policy polling request adds `overview=1` at most once per minute per open
tab. The server does **not** recompute the grouped history summary on normal
ten-second polls. Failed summary refreshes retry with the next regular poll.
If retained history is unavailable, the cards show an explicit unavailable
state rather than misleading zeros. Summary output never includes query text,
URLs, hosts, paths, credentials, or raw history records.

This UI intentionally does **not** display process-lifetime cache metrics:
the dashboard process is separate from the MCP bridge, so its local cache
counters would be misleading. The read-only MCP `web_metrics` tool continues
to report bridge-process cache counters and retention-bounded outcomes.

The dashboard at `http://127.0.0.1:8791/` refreshes the **pending approval queue**, stored allow/deny decisions, and summary counts approximately every 10 seconds while it is open. The `/history` page automatically refreshes its latest 25 audit rows as well. This uses a small same-origin external JavaScript response (bundled as a fixed trusted program literal) and a bounded local `GET /live?view=policy&q=...` or `GET /live?view=history` endpoint. It does **not** grant permissions, issue crawler requests, or forward anything to the remote agent. A manual **Refresh requests** link and ordinary server-rendered pages continue to work with JavaScript disabled.

Live policy snapshots carry a per-process random 128-bit run identifier and an increasing server-issued microsecond sequence. Tabs ignore older responses from the same run and late replies from retired server processes, but accept a new run's lower sequence even after a system-clock rollback. Corrupt, non-finite or out-of-safe-integer-range stored snapshot numbers are replaced by the next valid snapshot instead of blocking future updates. This prevents a delayed HTTP response from restoring obsolete pending state or re-alerting an already noticed request. The live endpoint is available only on the dashboard's loopback listener. Exact local Host checks, Origin/Referer validation when supplied, Fetch Metadata restrictions, no-store response headers, and the current CSRF-protected POST approval forms are retained. The CSP explicitly permits only **same-origin** external scripts and network connections. No inline scripts, remote SDKs, WebSocket listeners, extra tunnels, or new deployment ports are needed.

Polling is capped at **one request per tab per 10 seconds** after each response and aborts a stalled request (including a stalled JSON response body) after five seconds. Unchanged result tables are not rebuilt, and a table with a focused Rename input or action button is left untouched until the operator moves focus away; this prevents live refresh from discarding in-progress edits. Pending table results remain limited to 200 and decisions to 2,000 per poll; the unfiltered set of at most 512 pending hostnames is used only to detect new requests for optional notifications. A filtered dashboard still detects requests outside the active filter. Failed polls show a nonblocking status and retry later without duplicating concurrent requests. Pagehide cancels a pending poll timer and aborts any active request; restoring a page from the browser's back/forward cache serializes the new poll after cleanup of the old one, so it does not start a second polling loop. The history view contains at most 25 entries, with HTML-escaped text.

## Opt-in KDE/Wayland desktop notifications

Notifications are **disabled by default**. On the domain dashboard, click **Enable desktop notifications**, then grant permission in your browser. On a KDE Plasma desktop, browsers that integrate their Web Notifications with the desktop generally display these via the notification center. Desktop-session availability, browser support, Notification permission, and browser background throttling can affect delivery. This is a **browser-open** feature, not a system background notification service; keep the dashboard tab open if you want to receive notifications.

The setting is local to the browser/origin, stored as a small nonsecret preference in localStorage. Browsers must support secure-context Web Notifications, localStorage, and the Web Locks API for atomic cross-tab coordination; without these features the notification control is unavailable, while live tables and manual refresh still work. Enabling/disabling the preference syncs across open same-origin tabs. A delayed browser storage event from a rapid disable/re-enable cycle forces registration repair; an enabled tab does not retain an orphaned in-memory pointer. Click **Disable desktop notifications** to turn it off at any time. You can also revoke the site's permission through the browser settings. If your browser does not support notifications or rejects them in your session, the control is disabled or cannot enable notifications.

Only **newly queued domain requests** after the dashboard's first poll trigger a notification. Existing backlog does not generate alerts at startup. Repeated requests to a domain already waiting in the queue do not generate repeat alerts. New distinct domains are **grouped within each poll**, with at most one notification per minute **across all open dashboard tabs in the same browser profile**. An exclusive Web Lock coordinates all tabs. Only **salted SHA-256 identifiers for at most 512 pending domain names**, the random 128-bit salt used to create them, a bounded list of identifiers for alerts owed, the newest accepted server snapshot number, the current 128-bit server-run ID and up to eight retired-run IDs, a random registration key for each active tab, and the last delivery time are stored in the browser; raw domain names, URLs and queries are never persisted by this feature. Newly added identifiers during the one-minute cooldown remain owed, but an identifier is discarded **on the next successful poll** after that request is resolved or denied. Subsequent ordinary polls deliver only still-pending alerts once the cooldown expires, even without further requests. Multiple tabs observing the same request cannot queue duplicates. The first poll initializes a baseline without alerting for existing requests. Each opted-in dashboard tab registers a short random tab identifier in localStorage and clears its registration on page exit. Shared alert state is reset **only when the last registered tab closes** (or a new session finds no registered tabs), never merely because another still-open tab has been background-throttled. This is a best-effort browser lifecycle boundary: an abrupt crash may leave a stale tab registration in localStorage. Explicitly clicking **Disable desktop notifications** removes all registration keys and clears stale shared alert state, including keys left by crashed tabs, before the operator can enable alerts again; clearing this origin's site data also resets it. Normal close and restore retain the existing session protections. Browser throttling can delay alert delivery without losing queued alerts. No notification is generated merely because polling found no new work and no alert is owed. Popups contain only a count and generic local-review message, **never** domain names, private URL paths, query strings, search terms, page text, tokens, or credentials. Browser permissions are requested only after you explicitly click the button.

This feature does not replace periodic manual checks when the browser is closed or an operating-system session is unavailable. It deliberately avoids invoking `notify-send` as an uncontrolled child process for every incoming agent request, and never enables notifications without your opt-in.

## Security and manual verification

Open the dashboard with JavaScript disabled to confirm the full pending table, existing POST approval controls, domain search filter and manual refresh are usable. With JavaScript on, queue a previously unseen domain through the normal `web_fetch` tool and verify it appears within the next poll. Open `/history` to verify recent audit entries update. Enable notifications in a KDE browser session, then queue another previously unseen domain; verify at most one grouped desktop notification per minute. Disable notifications and confirm there are no further popups.

Responses are deliberately non-cacheable; script and JSON URLs may not be served from cross-site origins. The mock-only HTTP security tests cover Host/Origin/Referer/Fetch Metadata boundaries, escaping, query filtering, and changed pending/decision/history state. They do **not** prove a particular browser/KDE desktop will deliver system notifications without real UI/session testing.
