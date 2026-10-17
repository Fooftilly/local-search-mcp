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
class Clock extends Date { static now() { return now; } }
const localStorage = {
  getItem(key) { return storage.has(key) ? storage.get(key) : null; },
  setItem(key, value) { storage.set(key, String(value)); },
  removeItem(key) { storage.delete(key); }
};
function Notification(name, options) {
  notifications.push({ at: now, name, body: options.body });
}
Notification.permission = "granted";
Notification.requestPermission = async () => "granted";

function createTab(initialDomains) {
  const state = { domains: [...initialDomains], timers: [], handlers: {} };
  const targets = new Map();
  function element(id) {
    if (!targets.has(id)) {
      targets.set(id, {
        textContent: "", innerHTML: "server-rendered", disabled: false,
        focusChild: null,
        contains(child) { return child !== null && this.focusChild === child; },
        addEventListener(kind, fn) { this["on" + kind] = fn; }
      });
    }
    return targets.get(id);
  }
  const document = {
    documentElement: { dataset: { liveView: "policy" } },
    activeElement: null,
    getElementById: element
  };
  const window = {
    isSecureContext: true, Notification, crypto: webcrypto, localStorage,
    location: { search: "" },
    addEventListener(kind, fn) { state.handlers[kind] = fn; },
    setTimeout(fn, ms) {
      const timer = { fn, ms, cancelled: false };
      state.timers.push(timer);
      return timer;
    },
    clearTimeout(timer) { timer.cancelled = true; }
  };
  const fetch = async () => {
    const domains = [...state.domains];
    return {
      ok: true,
      json: async () => ({
        pending_domains: domains,
        counts: { pending: domains.length, allowed: 0, denied: 0 },
        pending_html: "pending: " + domains.join(","),
        decided_html: "decided: " + domains.join(",")
      })
    };
  };
  vm.runInNewContext(source, {
    window, document, navigator: { locks }, Notification, fetch,
    TextEncoder, AbortController, URLSearchParams, Date: Clock
  }, { timeout: 5000 });
  return {
    state, document, element,
    close() { if (state.handlers.pagehide) state.handlers.pagehide(); }
  };
}

async function settle() {
  // WebCrypto and promise/microtask chains complete without real timers.
  for (let i = 0; i < 14; i++) {
    await new Promise(resolve => setImmediate(resolve));
  }
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

  tab.state.domains.push("new.example");
  await poll(tab, 110000);
  assert.equal(notifications.length, 1, "new request notifies immediately");
  assert.match(notifications[0].body, /One new domain request/);

  tab.state.domains.push("later.example");
  await poll(tab, 120000);
  assert.equal(notifications.length, 1, "cooldown suppresses immediate popup");
  let owed = JSON.parse(storage.get("local-search-mcp.notification-owed.v1"));
  assert.equal(owed.length, 1, "a delayed alert is retained");

  for (const at of [130000, 140000, 150000, 160000]) {
    await poll(tab, at);
    assert.equal(notifications.length, 1, "no premature duplicate");
  }
  await poll(tab, 170000);
  assert.equal(notifications.length, 2, "owed alert must fire without new arrivals");
  owed = JSON.parse(storage.get("local-search-mcp.notification-owed.v1"));
  assert.equal(owed.length, 0);

  // A newly queued request that gets approved during cooldown must not
  // produce a misleading notification about something no longer pending.
  tab.state.domains.push("approved.example");
  await poll(tab, 180000);
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

  // A still-open hidden tab can be throttled beyond the 45-second reopen
  // threshold; delayed alerts must survive and fire after the cooldown.
  await poll(tab, 330000);
  assert.equal(notifications.length, 4,
               "background-throttled existing tabs must retain owed alerts");

  tab.close();
  second.close();
  now = 420000; // More than 45 seconds without a dashboard poll.
  const reopened = createTab(["existing.example", "closed-session.example"]);
  await settle();
  assert.equal(notifications.length, 4, "reopen must not alert old backlog");
  assert.deepEqual(JSON.parse(storage.get("local-search-mcp.notification-owed.v1")), []);
  reopened.close();
  console.log("dashboard live browser state tests passed");
}
main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
