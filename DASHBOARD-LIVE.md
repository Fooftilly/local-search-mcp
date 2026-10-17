# Live dashboard updates and optional desktop notifications

The dashboard at `http://127.0.0.1:8791/` refreshes the **pending approval queue**, stored allow/deny decisions, and summary counts approximately every 10 seconds while it is open. The `/history` page automatically refreshes its latest 25 audit rows as well. This uses a small external JavaScript file and a bounded local `GET /live?view=policy&q=...` or `GET /live?view=history` endpoint. It does **not** grant permissions, issue crawler requests, or forward anything to the remote agent. A manual **Refresh requests** link and ordinary server-rendered pages continue to work with JavaScript disabled.

The live endpoint is available only on the dashboard's loopback listener. Exact local Host checks, Origin/Referer validation when supplied, Fetch Metadata restrictions, no-store response headers, and the current CSRF-protected POST approval forms are retained. The CSP explicitly permits only **same-origin** external scripts and network connections. No inline scripts, remote SDKs, WebSocket listeners, extra tunnels, or new deployment ports are needed.

Polling is capped at **one request per tab per 10 seconds** after each response and aborts a stalled request after five seconds. Pending table results remain limited to 200 and decisions to 2,000 per poll; the unfiltered set of at most 512 pending hostnames is used only to detect new requests for optional notifications. A filtered dashboard still detects requests outside the active filter. Failed polls show a nonblocking status and retry later without duplicating concurrent requests. The history view contains at most 25 entries, with HTML-escaped text.

## Opt-in KDE/Wayland desktop notifications

Notifications are **disabled by default**. On the domain dashboard, click **Enable desktop notifications**, then grant permission in your browser. On a KDE Plasma desktop, browsers that integrate their Web Notifications with the desktop generally display these via the notification center. Desktop-session availability, browser support, Notification permission, and browser background throttling can affect delivery. This is a **browser-open** feature, not a system background notification service; keep the dashboard tab open if you want to receive notifications.

The setting is local to the browser/origin, stored as a small nonsecret preference in localStorage. Click **Disable desktop notifications** to turn it off at any time. You can also revoke the site's permission through the browser settings. If your browser does not support notifications or rejects them in your session, the control is disabled or cannot enable notifications.

Only **newly queued domain requests** after the dashboard's first poll trigger a notification. Existing backlog does not generate alerts at startup. Repeated requests to a domain already waiting in the queue do not generate repeat alerts. New distinct domains are **grouped**, with at most one notification per minute; none is generated merely because polling found no new work. Popups contain only a count and generic local-review message, **never** domain names, private URL paths, query strings, search terms, page text, tokens, or credentials. Browser permissions are requested only after you explicitly click the button.

This feature does not replace periodic manual checks when the browser is closed or an operating-system session is unavailable. It deliberately avoids invoking `notify-send` as an uncontrolled child process for every incoming agent request, and never enables notifications without your opt-in.

## Security and manual verification

Open the dashboard with JavaScript disabled to confirm the full pending table, existing POST approval controls, domain search filter and manual refresh are usable. With JavaScript on, queue a previously unseen domain through the normal `web_fetch` tool and verify it appears within the next poll. Open `/history` to verify recent audit entries update. Enable notifications in a KDE browser session, then queue another previously unseen domain; verify at most one grouped desktop notification per minute. Disable notifications and confirm there are no further popups.

Responses are deliberately non-cacheable; script and JSON URLs may not be served from cross-site origins. The mock-only HTTP security tests cover Host/Origin/Referer/Fetch Metadata boundaries, escaping, query filtering, and changed pending/decision/history state. They do **not** prove a particular browser/KDE desktop will deliver system notifications without real UI/session testing.
