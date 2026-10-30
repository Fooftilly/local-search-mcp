/* Same-origin, opt-in live dashboard updates. No inline scripts or remote calls. */
(() => {
  "use strict";
  const isHistory = document.documentElement.dataset.liveView === "history";
  const pendingBody = document.getElementById("live-pending");
  const decidedBody = document.getElementById("live-decided");
  const auditBody = document.getElementById("live-audit");
  const historyBody = document.getElementById("live-history");
  const activityBody = document.getElementById("live-activity");
  const serviceBody = document.getElementById("live-services");
  const historySummaryBody = document.getElementById("live-history-summary");
  const status = document.getElementById("live-status");
  const notice = document.getElementById("notification-toggle");
  const storageKey = "local-search-mcp.desktop-notifications.v1";
  const lastNotificationKey = "local-search-mcp.notification-time.v1";
  const pendingAlertKey = "local-search-mcp.notification-owed.v1";
  const seenDomainIdsKey = "local-search-mcp.notification-seen-ids.v1";
  const latestSnapshotKey = "local-search-mcp.notification-snapshot-us.v1";
  const snapshotRunKey = "local-search-mcp.notification-snapshot-run.v1";
  const retiredRunKey = "local-search-mcp.notification-retired-runs.v1";
  const tabPresencePrefix = "local-search-mcp.notification-tab.v1.";
  const fingerprintSaltKey = "local-search-mcp.notification-salt.v1";
  const notificationLock = "local-search-mcp.notification-lock.v1";
  const lastTableHtml = new WeakMap();

  function updateTable(body, html) {
    if (!body || lastTableHtml.get(body) === html) return;
    // Do not destroy a focused Rename input or button. Resume syncing this
    // table on the next poll after the operator moves focus elsewhere.
    if (body.contains(document.activeElement)) return;
    // Preserve deliberate bulk selections across 10-second live polls. Once
    // unchecked, the next poll resumes updates; server-side request IDs still
    // reject any stale selections after another tab has acted.
    if (body === pendingBody && typeof body.querySelector === "function" &&
        body.querySelector('input[name="selected"]:checked')) return;
    body.innerHTML = html;
    lastTableHtml.set(body, html);
  }
  const interval = 10000;
  const minNotificationGap = 60000;
  let notifyEnabled = false;
  let stopped = false;
  let tabPresenceKey = null;
  let pendingTimer = null;
  let activeController = null;
  let refreshGeneration = 0;
  let refreshQueue = Promise.resolve();

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

  function activeTabKeys() {
    const keys = [];
    try {
      for (let i = 0; i < window.localStorage.length; i++) {
        const key = window.localStorage.key(i);
        if (key && key.startsWith(tabPresencePrefix)) keys.push(key);
      }
    } catch (_) { /* Conservatively keep shared state on storage failure. */ }
    return keys;
  }

  async function registerTab() {
    // A disable in another tab can remove our key before its storage event
    // reaches this tab. Repair the stale local pointer before short-circuiting.
    if (tabPresenceKey && window.localStorage.getItem(tabPresenceKey) !== "open") {
      tabPresenceKey = null;
    }
    if (stopped || !notifyEnabled || !storageGet() ||
        !available() || tabPresenceKey) return;
    try {
      // Register under the same lock as notification updates. Every live tab
      // has a unique key until pagehide, even when its polling is throttled.
      await navigator.locks.request(notificationLock, async () => {
        if (stopped || !notifyEnabled || !storageGet() || tabPresenceKey) return;
        const peers = activeTabKeys();
        const id = new Uint8Array(16);
        window.crypto.getRandomValues(id);
        tabPresenceKey = tabPresencePrefix + Array.from(id, byte =>
          byte.toString(16).padStart(2, "0")).join("");
        const newSessionBaseline = peers.length === 0;
        window.localStorage.setItem(tabPresenceKey, "open");
        if (newSessionBaseline) {
          // All previous tabs closed normally; start from the new backlog.
          window.localStorage.removeItem(seenDomainIdsKey);
          window.localStorage.setItem(pendingAlertKey, "[]");
          window.localStorage.removeItem(latestSnapshotKey);
        window.localStorage.removeItem(snapshotRunKey);
        window.localStorage.removeItem(retiredRunKey);
        }
      });
    } catch (_) {
      notifyEnabled = false;
      storageSet(false);
      renderToggle();
    }
  }

  function unregisterTab() {
    if (!tabPresenceKey) return;
    try {
      window.localStorage.removeItem(tabPresenceKey);
      tabPresenceKey = null;
      if (!activeTabKeys().length) {
        // Never erase another live tab's owed work. Only an orderly final
        // close clears the shared baseline for a later fresh session.
        window.localStorage.removeItem(seenDomainIdsKey);
        window.localStorage.setItem(pendingAlertKey, "[]");
        window.localStorage.removeItem(latestSnapshotKey);
        window.localStorage.removeItem(snapshotRunKey);
        window.localStorage.removeItem(retiredRunKey);
      }
    } catch (_) { tabPresenceKey = null; }
  }

  async function resetDisabledNotifications() {
    // A crashed tab may leave a presence key even if pagehide never fired.
    // Explicit disable is an operator recovery action: clear *all* presence
    // registrations, not only this tab, and discard old pending identifiers.
    const clear = () => {
      try {
        for (const key of activeTabKeys()) window.localStorage.removeItem(key);
        tabPresenceKey = null;
        window.localStorage.removeItem(seenDomainIdsKey);
        window.localStorage.setItem(pendingAlertKey, "[]");
        window.localStorage.removeItem(latestSnapshotKey);
        window.localStorage.removeItem(snapshotRunKey);
        window.localStorage.removeItem(retiredRunKey);
      } catch (_) { /* The UI stays disabled if storage is unavailable. */ }
    };
    try { await navigator.locks.request(notificationLock, clear); }
    catch (_) { clear(); }
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
    if (notifyEnabled) void registerTab();
    renderToggle();
    notice.addEventListener("click", async () => {
      if (notifyEnabled) {
        notifyEnabled = false;
        storageSet(false);
        await resetDisabledNotifications();
      } else if (available()) {
        // Permission is requested ONLY in direct response to an operator click.
        let permission = Notification.permission;
        if (permission === "default") {
          try { permission = await Notification.requestPermission(); }
          catch (_) { permission = "denied"; }
        }
        notifyEnabled = permission === "granted";
        storageSet(notifyEnabled);
        if (notifyEnabled) await registerTab();
      }
      renderToggle();
    });
  }

  function readHashedIds(key) {
    try {
      const value = window.localStorage.getItem(key);
      if (value === null) return null;
      const ids = JSON.parse(value);
      if (!Array.isArray(ids) || ids.length > 512 ||
          !ids.every(id => typeof id === "string" && /^[a-f0-9]{64}$/.test(id))) {
        return null;
      }
      return ids;
    } catch (_) { return null; }
  }

  async function processNotifications(domains, snapshotUs, snapshotRun) {
    if (!notifyEnabled || !available() ||
        Notification.permission !== "granted") return;
    try {
      if (!tabPresenceKey) await registerTab();
      if (!tabPresenceKey || !notifyEnabled) return;
      // A single Web Lock serializes state across tabs. Only salted SHA-256
      // identifiers (not domain names, URLs or query text) enter storage.
      await navigator.locks.request(notificationLock, async () => {
        if (!notifyEnabled || !storageGet() ||
            Notification.permission !== "granted") return;
        // An earlier HTTP response can arrive after a newer tab's snapshot.
        // Compare server-issued microsecond sequence under the same Web Lock
        // before changing any shared queue or owed-alert state.
        if (!/^[a-f0-9]{32}$/.test(snapshotRun) ||
            !Number.isSafeInteger(snapshotUs) || snapshotUs <= 0) return;
        const currentRun = window.localStorage.getItem(snapshotRunKey);
        if (currentRun !== snapshotRun) {
          // An updated dashboard process may begin again at a smaller
          // timestamp after a clock rollback. Never mistake it for a stale
          // response; but do not accept late responses from retired runs.
          let retired = [];
          try {
            const saved = JSON.parse(window.localStorage.getItem(retiredRunKey) || "[]");
            if (Array.isArray(saved)) retired = saved.filter(
              id => typeof id === "string" && /^[a-f0-9]{32}$/.test(id)).slice(-8);
          } catch (_) { /* Corrupted optional metadata means no retired IDs. */ }
          if (retired.includes(snapshotRun)) return;
          if (currentRun && /^[a-f0-9]{32}$/.test(currentRun)) retired.push(currentRun);
          window.localStorage.setItem(retiredRunKey,
                                      JSON.stringify([...new Set(retired)].slice(-8)));
          window.localStorage.setItem(snapshotRunKey, snapshotRun);
          window.localStorage.removeItem(latestSnapshotKey);
        }
        const latest = Number(window.localStorage.getItem(latestSnapshotKey) || "0");
        // Corrupted persisted timestamps (including Infinity) must not freeze
        // notifications indefinitely. A valid server snapshot replaces them.
        if (Number.isSafeInteger(latest) && latest >= 0 &&
            snapshotUs <= latest) return;
        window.localStorage.setItem(latestSnapshotKey, String(snapshotUs));
        let salt = window.localStorage.getItem(fingerprintSaltKey);
        if (!salt) {
          const random = new Uint8Array(16);
          window.crypto.getRandomValues(random);
          salt = Array.from(random, byte =>
            byte.toString(16).padStart(2, "0")).join("");
          window.localStorage.setItem(fingerprintSaltKey, salt);
        }
        const stableDomains = Array.from(new Set(domains)).slice(0, 512).sort();
        const domainIds = await Promise.all(stableDomains.map(async domain => {
          const bytes = new TextEncoder().encode(salt + ":" + domain);
          const digest = await window.crypto.subtle.digest("SHA-256", bytes);
          return Array.from(new Uint8Array(digest), byte =>
            byte.toString(16).padStart(2, "0")).join("");
        }));
        const current = new Set(domainIds);
        const previousIds = readHashedIds(seenDomainIdsKey);
        const now = Date.now();
        if (previousIds === null) {
          // A newly opted-in tab or a new browser session starts with the
          // current queue as baseline, without alerting for old backlog.
          window.localStorage.setItem(seenDomainIdsKey, JSON.stringify(domainIds));
          window.localStorage.setItem(pendingAlertKey, "[]");
          return;
        }

        const previous = new Set(previousIds);
        const owedIds = readHashedIds(pendingAlertKey) || [];
        // Resolved/denied hosts are removed from alerts still owed.
        const owed = new Set(owedIds.filter(id => current.has(id)));
        for (const id of domainIds) {
          if (!previous.has(id)) owed.add(id);
        }
        window.localStorage.setItem(seenDomainIdsKey, JSON.stringify(domainIds));
        window.localStorage.setItem(pendingAlertKey, JSON.stringify([...owed]));

        if (!owed.size) return;
        const lastNotification = Number(window.localStorage.getItem(lastNotificationKey) || "0");
        if (!Number.isFinite(lastNotification) ||
            now - lastNotification < minNotificationGap) {
          // Keep only still-pending alerts owed. A normal next poll will
          // deliver them after the cooldown without another new request.
          return;
        }
        window.localStorage.setItem(pendingAlertKey, "[]");
        window.localStorage.setItem(lastNotificationKey, String(now));
        new Notification("Local Search MCP", {
          body: owed.size === 1 ?
            "One new domain request awaits local review." :
            `${owed.size} new domain requests await local review.`,
          tag: "local-search-mcp-pending"
        });
      });
    } catch (_) {
      notifyEnabled = false;
      storageSet(false);
      renderToggle();
    }
  }

  function maybeNotify(domains, snapshotUs, snapshotRun) {
    // Run every poll, even without fresh arrivals, to flush a previously
    // queued cross-tab alert after the one-minute cooldown.
    if (Array.isArray(domains)) {
      void processNotifications(domains, snapshotUs, snapshotRun);
    }
  }

  window.addEventListener("storage", (event) => {
    if (event.key === storageKey) {
      const enabled = storageGet() && available() &&
        Notification.permission === "granted";
      // The event itself can represent a disable even when a rapid
      // disable/re-enable makes storageGet() already return enabled.
      if (event.newValue !== "enabled" || !enabled) unregisterTab();
      if (tabPresenceKey &&
          window.localStorage.getItem(tabPresenceKey) !== "open") {
        tabPresenceKey = null;
      }
      notifyEnabled = enabled;
      if (enabled) void registerTab();
      renderToggle();
    }
  });

  function startRefresh() {
    // Serialize refresh attempts even across a pagehide/pageshow boundary;
    // an old aborted request must finish before a restored tab starts another.
    const generation = refreshGeneration;
    const next = refreshQueue.then(async () => {
      if (!stopped && generation === refreshGeneration) await refresh(generation);
    });
    refreshQueue = next.catch(() => {});
  }

  async function refresh(generation) {
    if (stopped || generation !== refreshGeneration) return;
    const query = new URLSearchParams(window.location.search).get("q") || "";
    const endpoint = isHistory ?
      "/live?view=history&" + new URLSearchParams(window.location.search).toString() :
      "/live?view=policy&q=" + encodeURIComponent(query.slice(0, 100));
    try {
      const controller = new AbortController();
      activeController = controller;
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
      // A response from before pagehide must not update a restored tab.
      if (stopped || generation !== refreshGeneration) return;
      if (isHistory) {
        updateTable(historyBody, data.history_html);
        updateTable(historySummaryBody, data.history_summary_html);
      } else {
        updateTable(pendingBody, data.pending_html);
        updateTable(decidedBody, data.decided_html);
        updateTable(auditBody, data.audit_html);
        updateTable(activityBody, data.activity_html);
        updateTable(serviceBody, data.service_rows_html);
        for (const type of ["pending", "allowed", "denied"]) {
          const count = document.getElementById("live-count-" + type);
          if (count) count.textContent = String(data.counts[type]);
        }
        if (Array.isArray(data.pending_domains)) {
          maybeNotify(data.pending_domains, data.snapshot_us, data.snapshot_run);
        }
      }
      say("Live updates on · every 10 seconds");
    } catch (_) {
      if (!stopped && generation === refreshGeneration) {
        say("Live updates paused · refresh manually if this persists");
      }
    } finally {
      activeController = null;
      if (!stopped && generation === refreshGeneration) {
        pendingTimer = window.setTimeout(() => {
          pendingTimer = null;
          startRefresh();
        }, interval);
      }
    }
  }

  window.addEventListener("pagehide", () => {
    stopped = true;
    refreshGeneration += 1;
    if (pendingTimer !== null) {
      window.clearTimeout(pendingTimer);
      pendingTimer = null;
    }
    if (activeController !== null) activeController.abort();
    unregisterTab();
  });
  window.addEventListener("pageshow", event => {
    if (event.persisted) {
      stopped = false;
      refreshGeneration += 1;
      if (notifyEnabled) void registerTab();
      startRefresh();
    }
  });
  startRefresh();
})();
