"""Embedded, fixed first-party JavaScript for the loopback dashboard.

This is a trusted program literal, not an HTTP/file input or dynamically
assembled script. Keep as a separate module to avoid accidental arbitrary-file
reads flowing into the HTTP response writer.
"""

LIVE_SCRIPT = r"""/* Same-origin, opt-in live dashboard updates. No inline scripts or remote calls. */
(() => {
  "use strict";
  const isHistory = document.documentElement.dataset.liveView === "history";
  const pendingBody = document.getElementById("live-pending");
  const decidedBody = document.getElementById("live-decided");
  const historyBody = document.getElementById("live-history");
  const status = document.getElementById("live-status");
  const notice = document.getElementById("notification-toggle");
  const storageKey = "local-search-mcp.desktop-notifications.v1";
  const lastTableHtml = new WeakMap();

  function updateTable(body, html) {
    if (!body || lastTableHtml.get(body) === html) return;
    // Do not destroy a focused Rename input or button. Resume syncing this
    // table on the next poll after the operator moves focus elsewhere.
    if (body.contains(document.activeElement)) return;
    body.innerHTML = html;
    lastTableHtml.set(body, html);
  }
  const interval = 10000;
  const minNotificationGap = 60000;
  let previousDomains = null;
  let notifyEnabled = false;
  let lastNotified = 0;
  let groupedNewRequests = 0;
  let stopped = false;

  function say(value) {
    if (status) status.textContent = value;
  }

  function storageGet() {
    try { return window.localStorage.getItem(storageKey) === "enabled"; }
    catch (_) { return false; }
  }
  function storageSet(value) {
    try { window.localStorage.setItem(storageKey, value ? "enabled" : "disabled"); }
    catch (_) { /* Private browsing may disable storage. */ }
  }

  function available() {
    return window.isSecureContext && "Notification" in window;
  }

  function renderToggle() {
    if (!notice) return;
    if (!available()) {
      notice.disabled = true;
      notice.textContent = "Desktop notifications unavailable";
      return;
    }
    if (notifyEnabled && Notification.permission !== "granted") {
      notifyEnabled = false;
      storageSet(false);
    }
    notice.textContent = notifyEnabled ?
      "Disable desktop notifications" : "Enable desktop notifications";
  }

  if (notice) {
    notifyEnabled = storageGet() && available() &&
      Notification.permission === "granted";
    renderToggle();
    notice.addEventListener("click", async () => {
      if (notifyEnabled) {
        notifyEnabled = false;
        storageSet(false);
      } else if (available()) {
        // Permission is requested ONLY in direct response to an operator click.
        let permission = Notification.permission;
        if (permission === "default") {
          try { permission = await Notification.requestPermission(); }
          catch (_) { permission = "denied"; }
        }
        notifyEnabled = permission === "granted";
        storageSet(notifyEnabled);
      }
      renderToggle();
    });
  }

  function maybeNotify(domains) {
    const current = new Set(domains);
    if (previousDomains === null) {
      // Never alert for a pre-existing backlog after opening the dashboard.
      previousDomains = current;
      return;
    }
    let newlyPending = 0;
    for (const domain of current) {
      if (!previousDomains.has(domain)) newlyPending += 1;
    }
    previousDomains = current;
    if (!notifyEnabled || !available() || Notification.permission !== "granted") {
      groupedNewRequests = 0;
      return;
    }
    groupedNewRequests += newlyPending;
    const now = Date.now();
    if (!groupedNewRequests || now - lastNotified < minNotificationGap) return;
    const total = groupedNewRequests;
    groupedNewRequests = 0;
    lastNotified = now;
    try {
      // No domains, URL paths, search queries, tokens or page content in popups.
      new Notification("Local Search MCP", {
        body: total === 1 ?
          "One new domain request awaits local review." :
          `${total} new domain requests await local review.`,
        tag: "local-search-mcp-pending"
      });
    } catch (_) {
      notifyEnabled = false;
      storageSet(false);
      renderToggle();
    }
  }

  async function refresh() {
    if (stopped) return;
    const query = new URLSearchParams(window.location.search).get("q") || "";
    const endpoint = isHistory ?
      "/live?view=history" : "/live?view=policy&q=" + encodeURIComponent(query.slice(0, 100));
    try {
      const controller = new AbortController();
      // Abort local requests after 5 seconds; no overlapping polls.
      const abort = window.setTimeout(() => controller.abort(), 5000);
      let response;
      try {
        response = await fetch(endpoint, {
          credentials: "same-origin",
          cache: "no-store",
          referrerPolicy: "no-referrer",
          headers: { "Accept": "application/json" },
          signal: controller.signal
        });
      } finally {
        window.clearTimeout(abort);
      }
      if (!response.ok) throw new Error("Live endpoint rejected request");
      const data = await response.json();
      if (isHistory) {
        updateTable(historyBody, data.history_html);
      } else {
        updateTable(pendingBody, data.pending_html);
        updateTable(decidedBody, data.decided_html);
        for (const type of ["pending", "allowed", "denied"]) {
          const count = document.getElementById("live-count-" + type);
          if (count) count.textContent = String(data.counts[type]);
        }
        if (Array.isArray(data.pending_domains)) maybeNotify(data.pending_domains);
      }
      say("Live updates on · every 10 seconds");
    } catch (_) {
      say("Live updates paused · refresh manually if this persists");
    } finally {
      if (!stopped) window.setTimeout(() => { void refresh(); }, interval);
    }
  }

  window.addEventListener("pagehide", () => { stopped = true; });
  void refresh();
})();
"""
