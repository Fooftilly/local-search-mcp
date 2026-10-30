"use strict";
// Zero-dependency browser-behavior regression for the embedded dashboard JS.
// The corresponding Python unittest feeds LIVE_SCRIPT to this test on stdin.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");
const { TextEncoder } = require("node:util");

const source = fs.readFileSync(0, "utf8");
const notifications = [];
const storage = new Map([["local-search-mcp.desktop-notifications.v1", "enabled"]]);
const lockQueues = new Map();
const locks = {
  request(name, callback) {
    const prior = lockQueues.get(name) || Promise.resolve();
    const current = prior.then(() => callback());
    lockQueues.set(name, current.catch(() => {}));
    return current;
  }
};
let now = 100000;
let serverSnapshot = 0;
let serverRun = "a".repeat(32);
class Clock extends Date { static now() { return now; } }
const localStorage = {
  get length() { return storage.size; },
  key(index) { return [...storage.keys()][index] ?? null; },
  getItem(key) { return storage.has(key) ? storage.get(key) : null; },
  setItem(key, value) { storage.set(key, String(value)); },
  removeItem(key) { storage.delete(key); }
};
function Notification(name, options) {
  notifications.push({ at: now, name, body: options.body });
}
Notification.permission = "granted";
Notification.requestPermission = async () => "granted";

function createTab(initialDomains, view = "policy", query = "", activeFilter,
                   activePage) {
  const state = { domains: [...initialDomains], timers: [], handlers: {},
                  nextSnapshotUs: null, nextSnapshotRun: null, pendingOverride: null,
                  historyRows: "history: before", historyBars: "bars: before",
                  activityRows: "activity: before", serviceRows: "services: before",
                  pendingPages: "pending pagination: original",
                  operationsRows: "operations: before", summaryRows: "summary: before",
                  skipOverview: false,
                  bulkBoxes: [], nextBulkBoxes: null, lastEndpoint: null };
  const targets = new Map();
  function element(id) {
    if (!targets.has(id)) {
      targets.set(id, {
        textContent: "", _html: "server-rendered", disabled: false,
        get innerHTML() { return this._html; },
        set innerHTML(value) {
          this._html = value;
          // Model replacement of checkbox DOM nodes, unlike a plain string
          // mock that retained old node state across table re-renders.
          if (id === "live-pending" && state.nextBulkBoxes !== null) {
            state.bulkBoxes = state.nextBulkBoxes;
            state.nextBulkBoxes = null;
          }
        },
        dataset: id === "bulk-selection-tools" ? { max: "25" } : {},
        hidden: id === "bulk-selection-tools",
        focusChild: null, bulkSelected: false,
        contains(child) { return child !== null && this.focusChild === child; },
        querySelector(selector) {
          return selector === 'input[name="selected"]:checked' &&
            (this.bulkSelected || state.bulkBoxes.some(box => box.checked)) ? {} : null;
        },
        querySelectorAll(selector) {
          return selector === 'input[name="selected"][form="bulk-form"]' &&
            id === "live-pending" ? state.bulkBoxes : [];
        },
        addEventListener(kind, fn) { this["on" + kind] = fn; }
      });
    }
    return targets.get(id);
  }
  const document = {
    documentElement: { dataset: {
      liveView: view,
      ...(activeFilter === undefined ? {} : { reviewQuery: activeFilter }),
      ...(activePage === undefined ? {} : { reviewPage: activePage })
    } },
    activeElement: null,
    getElementById: element
  };
  const window = {
    isSecureContext: true, Notification, crypto: webcrypto, localStorage,
    location: { search: query },
    addEventListener(kind, fn) { state.handlers[kind] = fn; },
    setTimeout(fn, ms) {
      const timer = { fn, ms, cancelled: false };
      state.timers.push(timer);
      return timer;
    },
    clearTimeout(timer) { timer.cancelled = true; }
  };
  const fetch = async (endpoint) => {
    state.lastEndpoint = endpoint;
    const domains = [...state.domains];
    const snapshot = state.nextSnapshotUs ?? ++serverSnapshot;
    const run = state.nextSnapshotRun ?? serverRun;
    state.nextSnapshotUs = null;
    state.nextSnapshotRun = null;
    return {
      ok: true,
      json: async () => ({
        snapshot_us: snapshot,
        snapshot_run: run,
        pending_domains: domains,
        counts: { pending: domains.length, allowed: 0, denied: 0 },
        pending_html: state.pendingOverride ?? "pending: " + domains.join(","),
        decided_html: "decided: " + domains.join(","),
        history_html: state.historyRows,
        history_summary_html: state.historyBars,
        activity_html: state.activityRows,
        service_rows_html: state.serviceRows,
        pending_pages_html: state.pendingPages,
        ...(endpoint.includes("&overview=1") && !state.skipOverview ? {
          operations_html: state.operationsRows,
          activity_summary_html: state.summaryRows
        } : {})
      })
    };
  };
  // First-party LIVE_SCRIPT arrives solely from the checked-in Python module;
  // no user-supplied code, network input, or path is evaluated in this test.
  vm.runInNewContext(source, { // NOSONAR: trusted embedded test fixture only
    window, document, navigator: { locks }, Notification, fetch,
    TextEncoder, AbortController, URLSearchParams, Date: Clock
  }, { timeout: 5000 });
  return {
    state, document, element,
    close() { if (state.handlers.pagehide) state.handlers.pagehide(); }
  };
}

async function settle() {
  // WebCrypto runs on Node's worker pool and may outlive multiple event-loop
  // ticks. Wait for the actual in-flight Web Lock callback, not an arbitrary
  // number of microtasks, before advancing the fake clock or queue snapshot.
  for (let i = 0; i < 14; i++) {
    await new Promise(resolve => setImmediate(resolve));
  }
  await Promise.all([...lockQueues.values()]);
  await new Promise(resolve => setImmediate(resolve));
}

async function poll(tab, at) {
  now = at;
  const timer = tab.state.timers.find(x => x.ms === 10000 && !x.cancelled);
  assert.ok(timer, "a next-poll timer must be scheduled");
  timer.cancelled = true;
  timer.fn();
  await settle();
}

async function main() {
  const tab = createTab(["existing.example"]);
  await settle();
  assert.equal(notifications.length, 0, "existing backlog must not alert");
  assert.equal(tab.element("live-activity").innerHTML, "activity: before");
  assert.equal(tab.element("live-services").innerHTML, "services: before");
  assert.equal(tab.element("live-operations").innerHTML, "server-rendered");
  assert.equal(tab.element("live-activity-summary").innerHTML, "server-rendered");
  // The optional selection helpers must not submit or authorize anything.
  // They preserve the existing server-validated bulk form and its 25 cap.
  const select = tab.element("bulk-select-visible");
  const clear = tab.element("bulk-clear-selection");
  const tools = tab.element("bulk-selection-tools");
  const summary = tab.element("bulk-selection-count");
  const bulkPendingBody = tab.element("live-pending");
  assert.equal(tools.hidden, false, "JS progressively enables selection tools");
  tab.state.bulkBoxes = Array.from({ length: 28 }, () =>
    ({ checked: false, disabled: false }));
  select.onclick();
  assert.equal(tab.state.bulkBoxes.filter(x => x.checked).length, 25);
  assert.equal(tab.state.bulkBoxes.filter(x => x.disabled).length, 3);
  assert.match(summary.textContent, /25 selected/);
  assert.equal(select.disabled, true, "cap disables Select all visible");
  clear.onclick();
  assert.equal(tab.state.bulkBoxes.filter(x => x.checked).length, 0);
  assert.equal(tab.state.bulkBoxes.filter(x => x.disabled).length, 0);
  assert.equal(clear.disabled, true);
  tab.state.bulkBoxes[0].checked = true;
  bulkPendingBody.onchange({ target: tab.state.bulkBoxes[0] });
  assert.match(summary.textContent, /1 selected/);
  select.onclick();
  assert.equal(tab.state.bulkBoxes.filter(x => x.checked).length, 25);
  // Unexpected over-selection (for example a synthetic change) is reverted.
  tab.state.bulkBoxes[25].checked = true;
  bulkPendingBody.onchange({ target: tab.state.bulkBoxes[25] });
  assert.equal(tab.state.bulkBoxes[25].checked, false);
  assert.match(summary.textContent, /Selection limit: 25/);
  const beforeSelectedPoll = bulkPendingBody.innerHTML;
  tab.state.pendingOverride = "pending: changed while 25 selected";
  await poll(tab, 105000);
  assert.equal(bulkPendingBody.innerHTML, beforeSelectedPoll,
               "poll cannot overwrite deliberate checkbox selections");
  assert.equal(tab.state.bulkBoxes.filter(x => x.checked).length, 25);
  assert.match(summary.textContent, /25 selected/);
  clear.onclick();
  // On the next successful snapshot, model a new set of DOM checkboxes.
  tab.state.nextBulkBoxes = Array.from({ length: 3 }, () =>
    ({ checked: false, disabled: false }));
  await poll(tab, 106000);
  assert.equal(bulkPendingBody.innerHTML, "pending: changed while 25 selected");
  assert.equal(tab.state.bulkBoxes.length, 3);
  assert.equal(clear.disabled, true);
  assert.equal(select.disabled, false);
  assert.match(summary.textContent, /0 selected/);
  tab.state.pendingOverride = null;
  tab.state.activityRows = "activity: refreshed";
  tab.state.serviceRows = "services: refreshed";

  tab.state.domains.push("new.example");
  await poll(tab, 110000);
  assert.equal(tab.element("live-activity").innerHTML, "activity: refreshed");
  assert.equal(tab.element("live-services").innerHTML, "services: refreshed");
  assert.equal(notifications.length, 1, "new request notifies immediately; " +
    JSON.stringify({ storage: [...storage.entries()],
                     status: tab.element("live-status").textContent }));
  assert.match(notifications[0].body, /One new domain request/);

  tab.state.domains.push("later.example");
  await poll(tab, 120000);
  assert.equal(notifications.length, 1, "cooldown suppresses immediate popup");
  let owed = JSON.parse(storage.get("local-search-mcp.notification-owed.v1"));
  assert.equal(owed.length, 1, "a delayed alert is retained");

  for (const at of [130000, 140000, 150000]) {
    await poll(tab, at);
    assert.equal(notifications.length, 1, "no premature duplicate");
    assert.doesNotMatch(tab.state.lastEndpoint, /&overview=1$/);
  }
  tab.state.operationsRows = "operations: new 24h counts";
  tab.state.summaryRows = "summary: new outcome breakdown";
  await poll(tab, 160000);
  assert.equal(notifications.length, 1, "no premature duplicate");
  assert.match(tab.state.lastEndpoint, /&overview=1$/,
               "24h summary requested on a slower minute cadence");
  await poll(tab, 170000);
  assert.doesNotMatch(tab.state.lastEndpoint, /&overview=1$/,
                      "successful overview refresh restarts the minute window");
  assert.equal(tab.element("live-operations").innerHTML,
               "operations: new 24h counts");
  assert.equal(tab.element("live-activity-summary").innerHTML,
               "summary: new outcome breakdown");
  assert.equal(notifications.length, 2, "owed alert must fire without new arrivals");
  owed = JSON.parse(storage.get("local-search-mcp.notification-owed.v1"));
  assert.equal(owed.length, 0);

  // A newly queued request that gets approved during cooldown must not
  // produce a misleading notification about something no longer pending.
  tab.state.domains.push("approved.example");
  await poll(tab, 180000);
  assert.doesNotMatch(tab.state.lastEndpoint, /&overview=1$/,
                      "ten-second requests omit expensive history grouping");
  assert.equal(JSON.parse(storage.get("local-search-mcp.notification-owed.v1")).length, 1);
  tab.state.domains = tab.state.domains.filter(x => x !== "approved.example");
  await poll(tab, 190000);
  assert.equal(JSON.parse(storage.get("local-search-mcp.notification-owed.v1")).length, 0);
  for (const at of [200000, 210000, 220000, 230000]) await poll(tab, at);
  assert.equal(notifications.length, 2, "decided requests must not alert later");

  // A second tab sees the same queue snapshot but cannot duplicate an alert.
  const second = createTab(tab.state.domains);
  await settle();
  second.state.domains.push("multi-tab.example");
  tab.state.domains.push("multi-tab.example");
  await poll(tab, 240000);
  await poll(second, 240000);
  assert.equal(notifications.length, 3, "only one cross-tab popup per new domain");

  // A delayed old HTTP response must not regress the shared queue state and
  // rediscover an already notified domain as new on the next fresh poll.
  second.state.domains = second.state.domains.filter(x => x !== "multi-tab.example");
  second.state.nextSnapshotUs = 1; // Deliberately older than the last accepted poll.
  await poll(second, 241000);
  second.state.domains.push("multi-tab.example");
  await poll(second, 242000);
  assert.equal(JSON.parse(storage.get("local-search-mcp.notification-owed.v1")).length,
               0, "stale cross-tab responses must not queue duplicate alerts");

  // A dashboard restart with a rolled-back clock must reset sequencing,
  // without allowing a delayed response from the retired process to undo it.
  const retiredRun = serverRun;
  serverRun = "b".repeat(32);
  serverSnapshot = 0;
  await poll(tab, 243000);
  assert.equal(storage.get("local-search-mcp.notification-snapshot-run.v1"), serverRun);
  assert.equal(storage.get("local-search-mcp.notification-snapshot-us.v1"), "1");
  second.state.nextSnapshotRun = retiredRun;
  second.state.nextSnapshotUs = 999999999999;
  second.state.domains = [];
  await poll(second, 244000);
  assert.equal(storage.get("local-search-mcp.notification-snapshot-run.v1"), serverRun,
               "a late response from an old server instance must be rejected");
  second.state.domains = [...tab.state.domains];
  await poll(tab, 245000);
  assert.equal(notifications.length, 3,
               "clock rollback and retired replies must not create duplicate alerts");

  // Corrupted or non-finite saved snapshots must not suppress future valid
  // server responses, even when the current server run has not changed.
  const snapshotKey = "local-search-mcp.notification-snapshot-us.v1";
  for (const invalid of ["Infinity", "NaN", "9007199254740992"]) {
    storage.set(snapshotKey, invalid);
    await poll(tab, 246000);
    assert.equal(storage.get(snapshotKey), String(serverSnapshot),
                 "invalid saved snapshot must recover: " + invalid);
  }
  assert.equal(notifications.length, 3,
               "recovering corrupt snapshot state must not duplicate alerts");

  // Rename field state and focus survive a changed poll response.
  const decided = tab.element("live-decided");
  const editing = { value: "unfinished-rename" };
  decided.focusChild = editing;
  tab.document.activeElement = editing;
  const original = decided.innerHTML;
  tab.state.domains.push("more.example");
  await poll(tab, 250000);
  assert.equal(decided.innerHTML, original, "focused table must not be replaced");
  assert.equal(editing.value, "unfinished-rename");
  decided.focusChild = null;
  tab.document.activeElement = null;
  await poll(tab, 260000);
  assert.notEqual(decided.innerHTML, original, "updates resume after blur");

  // Checked bulk selections must survive live updates until deselected.
  const pending = tab.element("live-pending");
  const beforeSelection = pending.innerHTML;
  pending.bulkSelected = true;
  tab.state.pendingOverride = "pending: changed while selecting";
  await poll(tab, 265000);
  assert.equal(pending.innerHTML, beforeSelection,
               "checked bulk selections must not be discarded by polling");
  pending.bulkSelected = false;
  await poll(tab, 266000);
  assert.equal(pending.innerHTML, "pending: changed while selecting",
               "pending updates must resume after bulk selection clears");
  tab.state.pendingOverride = null;

  // Opening a new tab during a >45-second background throttle must not erase
  // the still-open first tab's owed alerts.
  now = 325000;
  const another = createTab(tab.state.domains);
  await settle();
  assert.equal(notifications.length, 4,
               "new tabs must not reset still-active background tab alerts");
  // A still-open hidden tab can be throttled beyond 45 seconds without
  // resetting the session or re-alerting.
  await poll(tab, 330000);
  assert.equal(notifications.length, 4,
               "background-throttled existing tabs must retain owed alerts");

  // bfcache restores must cancel the suspended old poll timer instead of
  // creating a second ten-second loop on pageshow.
  const activeTimers = () => tab.state.timers.filter(
    timer => timer.ms === 10000 && !timer.cancelled);
  assert.equal(activeTimers().length, 1);
  tab.state.handlers.pagehide();
  assert.equal(activeTimers().length, 0, "pagehide must cancel scheduled polls");
  tab.state.handlers.pageshow({ persisted: true });
  await settle();
  assert.equal(activeTimers().length, 1,
               "pageshow must create exactly one replacement poll timer");
  await poll(tab, 335000);
  assert.equal(activeTimers().length, 1,
               "restored poll timer must not duplicate on subsequent polls");

  // A queued disabled storage event may arrive only after a rapid re-enable.
  // The tab must repair its vanished registration even when the stored
  // preference is currently "enabled" again.
  for (const key of [...storage.keys()]) {
    if (key.startsWith("local-search-mcp.notification-tab.v1.")) {
      storage.delete(key);
    }
  }
  tab.state.handlers.storage({
    key: "local-search-mcp.desktop-notifications.v1", newValue: "disabled"
  });
  await settle();
  assert.ok([...storage.keys()].some(key =>
    key.startsWith("local-search-mcp.notification-tab.v1.")),
    "delayed disable event must re-register an enabled tab");

  tab.close();
  second.close();
  another.close();
  now = 420000; // More than 45 seconds without a dashboard poll.
  const reopened = createTab(["existing.example", "closed-session.example"]);
  await settle();
  assert.equal(notifications.length, 4, "reopen must not alert old backlog");
  assert.deepEqual(JSON.parse(storage.get("local-search-mcp.notification-owed.v1")), []);
  reopened.close();

  // A POST response returns /domain without a ?q= in the address bar.
  // Its server-rendered reviewQuery must survive subsequent /live polling.
  const postResult = createTab([], "policy", "", "related.example.net");
  await settle();
  assert.equal(postResult.state.lastEndpoint,
               "/live?view=policy&q=related.example.net",
               "post-result filter survives subsequent live refresh");
  await poll(postResult, 425000);
  assert.equal(postResult.state.lastEndpoint,
               "/live?view=policy&q=related.example.net");
  postResult.close();

  // Page 3 of the review queue must be retained after filtered POSTs,
  // and its navigation controls must update with the live queue.
  const pageThree = createTab([], "policy", "", "example.net", "3");
  await settle();
  assert.equal(pageThree.state.lastEndpoint,
               "/live?view=policy&q=example.net&page=3");
  assert.equal(pageThree.element("live-pending-pages").innerHTML,
               "pending pagination: original");
  pageThree.state.pendingPages = "pending pagination: after decision";
  await poll(pageThree, 426000);
  assert.equal(pageThree.state.lastEndpoint,
               "/live?view=policy&q=example.net&page=3");
  assert.equal(pageThree.element("live-pending-pages").innerHTML,
               "pending pagination: after decision");
  assert.equal(pageThree.element("live-pending-pages-bottom").innerHTML,
               "pending pagination: after decision");
  pageThree.close();

  // Simulate an unclean browser exit: a tab never receives pagehide and
  // leaves an orphaned presence key behind. Explicitly disabling from a
  // different live tab must clear all registrations before opt-in resumes.
  const orphan = createTab(["orphaned.example"]);
  await settle();
  const operator = createTab(["orphaned.example"]);
  await settle();
  assert.ok([...storage.keys()].some(key =>
    key.startsWith("local-search-mcp.notification-tab.v1.")));
  await operator.element("notification-toggle").onclick();
  await settle();
  assert.equal([...storage.keys()].filter(key =>
    key.startsWith("local-search-mcp.notification-tab.v1.")).length, 0,
    "explicit disable must purge registration keys from crashed tabs");
  assert.equal(storage.has("local-search-mcp.notification-seen-ids.v1"), false);
  assert.deepEqual(JSON.parse(storage.get("local-search-mcp.notification-owed.v1")), []);
  await operator.element("notification-toggle").onclick();
  await settle();
  operator.close();
  now = 510000;
  const afterRecovery = createTab(["orphaned.example", "since-closed.example"]);
  await settle();
  assert.equal(notifications.length, 4,
               "a fresh session after disable must not alert existing backlog");
  afterRecovery.close();
  // Intentionally do not call orphan.close(): this simulates an unclean exit.

  // The UI and persistent browser metadata must never contain input domains,
  // URL tokens, or private request details; only salted hashes and counts.
  const persisted = JSON.stringify([...storage.entries()]);
  const popupText = JSON.stringify(notifications);
  for (const secret of ["existing.example", "new.example", "later.example",
                        "multi-tab.example", "approved.example",
                        "closed-session.example", "orphaned.example",
                        "since-closed.example", "PRIVATE_URL_TOKEN"]) {
    assert.ok(!persisted.includes(secret), "private domain leaked to storage: " + secret);
    assert.ok(!popupText.includes(secret), "private domain leaked to popup: " + secret);
  }
  // The history tab must refresh both filtered records and summary bars
  // with the same URL parameters; no policy notification data is involved.
  const historyTab = createTab([], "history", "?kind=fetch&hours=24&domain=example.org");
  await settle();
  assert.match(historyTab.state.lastEndpoint,
               /^\/live\?view=history&kind=fetch&hours=24&domain=example\.org$/);
  assert.equal(historyTab.element("live-history").innerHTML, "history: before");
  assert.equal(historyTab.element("live-history-summary").innerHTML, "bars: before");
  historyTab.state.historyRows = "history: updated";
  historyTab.state.historyBars = "bars: updated";
  await poll(historyTab, 550000);
  assert.equal(historyTab.element("live-history").innerHTML, "history: updated");
  assert.equal(historyTab.element("live-history-summary").innerHTML, "bars: updated");
  historyTab.close();

  console.log("dashboard live browser state tests passed");
}
main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
