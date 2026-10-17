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
  const lastNotificationKey = "local-search-mcp.notification-time.v1";
  const pendingAlertKey = "local-search-mcp.notification-owed.v1";
  const queueFingerprintKey = "local-search-mcp.notification-queue-id.v1";
  const queueCountKey = "local-search-mcp.notification-queue-count.v1";
  const fingerprintSaltKey = "local-search-mcp.notification-salt.v1";
  const notificationLock = "local-search-mcp.notification-lock.v1";
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
  let notifyEnabled = false;
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
    // Web Locks provide atomic cross-tab throttling on the same origin.
    // Disable desktop alerts if atomic coordination or local storage is
    // unavailable; never risk a multi-tab notification flood.
    if (!window.isSecureContext || !("Notification" in window) ||
        !navigator.locks || typeof navigator.locks.request !== "function" ||
        !window.crypto || !window.crypto.subtle ||
        typeof window.crypto.getRandomValues !== "function") return false;
    try {
      const probe = "local-search-mcp.notification-storage-test";
      window.localStorage.setItem(probe, "1");
      window.localStorage.removeItem(probe);
      return true;
    } catch (_) { return false; }
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
        try {
          window.localStorage.setItem(pendingAlertKey, "0");
          window.localStorage.removeItem(queueFingerprintKey);
        } catch (_) { /* Refuse silently if private storage is unavailable. */ }
      } else if (available()) {
        // Permission is requested ONLY in direct response to an operator click.
        let permission = Notification.permission;
        if (permission === "default") {
          try { permission = await Notification.requestPermission(); }
          catch (_) { permission = "denied"; }
        }
        notifyEnabled = permission === "granted";
        storageSet(notifyEnabled);
        if (notifyEnabled) {
          // A new opt-in starts with a clean baseline, not old backlog.
          window.localStorage.setItem(pendingAlertKey, "0");
          window.localStorage.removeItem(queueFingerprintKey);
        }
      }
      renderToggle();
    });
  }

  async function processNotifications(domains) {
    if (!notifyEnabled || !available() ||
        Notification.permission !== "granted") return;
    try {
      // The Web Lock serializes detection, shared pending-alert updates and
      // emission across all tabs on this origin. No domain names are
      // persisted: only a salted digest and bounded counts are stored.
      await navigator.locks.request(notificationLock, async () => {
        if (!notifyEnabled || Notification.permission !== "granted") return;
        let salt = window.localStorage.getItem(fingerprintSaltKey);
        if (!salt) {
          const random = new Uint8Array(16);
          window.crypto.getRandomValues(random);
          salt = Array.from(random, byte =>
            byte.toString(16).padStart(2, "0")).join("");
          window.localStorage.setItem(fingerprintSaltKey, salt);
        }
        const stableDomains = Array.from(new Set(domains)).sort();
        const bytes = new TextEncoder().encode(
          salt + ":" + JSON.stringify(stableDomains));
        const digest = await window.crypto.subtle.digest("SHA-256", bytes);
        const fingerprint = Array.from(new Uint8Array(digest), byte =>
          byte.toString(16).padStart(2, "0")).join("");

        const lastFingerprint = window.localStorage.getItem(queueFingerprintKey);
        const earlierCount = Number(window.localStorage.getItem(queueCountKey) || "0");
        let owed = Number(window.localStorage.getItem(pendingAlertKey) || "0");
        if (!Number.isFinite(owed) || owed < 0) owed = 0;
        if (lastFingerprint === null) {
          // First opening initializes the queue baseline without notifying
          // for an existing backlog.
          window.localStorage.setItem(queueFingerprintKey, fingerprint);
          window.localStorage.setItem(queueCountKey, String(stableDomains.length));
        } else if (lastFingerprint !== fingerprint) {
          const growth = Math.max(0, stableDomains.length -
            (Number.isFinite(earlierCount) ? earlierCount : 0));
          // A changed same-size set can mean one host was replaced. A pure
          // decrease means removals, which should not cause an alert.
          const additions = growth || (stableDomains.length >= earlierCount ? 1 : 0);
          owed = Math.min(512, owed + additions);
          window.localStorage.setItem(queueFingerprintKey, fingerprint);
          window.localStorage.setItem(queueCountKey, String(stableDomains.length));
          if (owed) window.localStorage.setItem(pendingAlertKey, String(owed));
        }

        if (!owed) return;
        const now = Date.now();
        const previous = Number(window.localStorage.getItem(lastNotificationKey) || "0");
        if (!Number.isFinite(previous) || now - previous < minNotificationGap) {
          // Leave the shared alert owed; another ordinary poll will deliver
          // it after the cooldown even when no further requests appear.
          return;
        }
        window.localStorage.setItem(pendingAlertKey, "0");
        window.localStorage.setItem(lastNotificationKey, String(now));
        new Notification("Local Search MCP", {
          body: owed === 1 ?
            "One new domain request awaits local review." :
            `${owed} new domain requests await local review.`,
          tag: "local-search-mcp-pending"
        });
      });
    } catch (_) {
      notifyEnabled = false;
      storageSet(false);
      renderToggle();
    }
  }

  function maybeNotify(domains) {
    // Run every poll, even without fresh arrivals, to flush a previously
    // queued cross-tab alert after the one-minute cooldown.
    if (Array.isArray(domains)) void processNotifications(domains);
  }

  window.addEventListener("storage", (event) => {
    if (event.key === storageKey) {
      notifyEnabled = storageGet() && available() &&
        Notification.permission === "granted";
      renderToggle();
    }
  });

  async function refresh() {
    if (stopped) return;
    const query = new URLSearchParams(window.location.search).get("q") || "";
    const endpoint = isHistory ?
      "/live?view=history" : "/live?view=policy&q=" + encodeURIComponent(query.slice(0, 100));
    try {
      const controller = new AbortController();
      // Abort local requests after 5 seconds; no overlapping polls.
      const abort = window.setTimeout(() => controller.abort(), 5000);
      let data;
      try {
        const response = await fetch(endpoint, {
          credentials: "same-origin",
          cache: "no-store",
          referrerPolicy: "no-referrer",
          headers: { "Accept": "application/json" },
          signal: controller.signal
        });
        if (!response.ok) throw new Error("Live endpoint rejected request");
        // Retain the abort deadline while parsing the entire response body.
        // A stalled JSON body must not halt all future dashboard polling.
        data = await response.json();
      } finally {
        window.clearTimeout(abort);
      }
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
