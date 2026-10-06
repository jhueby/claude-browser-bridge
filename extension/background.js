// Claude Browser Bridge — background page.
// Long-polls every bridge server on 127.0.0.1:8777-8786 (one per Claude Code session),
// runs the commands they hand out against tabs, and posts the results back.

const PORTS = Array.from({ length: 10 }, (_, i) => 8777 + i);
const READ_ONLY_OK = new Set(["tabs_list", "tab_focus", "screenshot", "snapshot", "get_text", "find",
  "hover", "scroll", "wait_for", "logs_start", "logs_read"]);
const NO_PAGE = new Set(["tabs_list", "tab_open", "tab_close", "tab_focus", "reload_matching"]);

const settings = { token: "", tokenSource: "", paused: false, readOnly: false, blocked: [], allowed: [] };
const sessions = {};          // port -> { up, error, pid, cwd, since, count, lastTool, lastAt }
const cutPids = new Set();    // bridge PIDs the user disconnected from the popup (until "Reconnect")
const activity = [];          // recent commands, newest first
const shotScale = {};         // tabId -> viewport CSS px per screenshot px

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ------------------------------------------------------------------ settings & badge

async function loadSettings() {
  Object.assign(settings, await browser.storage.local.get(Object.keys(settings)));
  if (!settings.token) {
    // Optional: a locally built copy may ship pairing.json (gitignored) to skip the paste step.
    try {
      const p = await (await fetch(browser.runtime.getURL("pairing.json"))).json();
      const seed = { ...(p.settings || {}), ...(p.token ? { token: p.token, tokenSource: "pairing" } : {}) };
      Object.assign(settings, seed);
      await browser.storage.local.set(seed);
    } catch { /* not present */ }
  }
  if (!settings.token) await tokenFromNativeHost();
  updateBadge();
}

// Native messaging: Firefox launches native/host.py (registered by native/install.py, allowed
// only for this extension's ID), which reads ~/.claude-browser-bridge/token. No pasting needed.
let lastNativeTry = 0;
async function tokenFromNativeHost() {
  if (Date.now() - lastNativeTry < 30000) return false;
  lastNativeTry = Date.now();
  try {
    const r = await browser.runtime.sendNativeMessage("claude_browser_bridge", { type: "token" });
    if (!r?.token || r.token === settings.token) return false;
    settings.token = r.token;
    settings.tokenSource = "native";
    await browser.storage.local.set({ token: r.token, tokenSource: "native" });
    return true;
  } catch {
    return false; // host not installed: fall back to the pasted token
  }
}

browser.storage.onChanged.addListener((changes) => {
  for (const [k, { newValue }] of Object.entries(changes)) if (k in settings) settings[k] = newValue;
  updateBadge();
});

function updateBadge() {
  const up = Object.values(sessions).filter((s) => s.up && !cutPids.has(s.pid)).length;
  let text = "", color = "#6b7280";
  if (!settings.token) { text = "!"; color = "#dc2626"; }
  else if (settings.paused) { text = "II"; }
  else if (up) { text = String(up); color = settings.readOnly ? "#2563eb" : "#d97706"; }
  browser.browserAction.setBadgeText({ text });
  browser.browserAction.setBadgeBackgroundColor({ color });
  browser.browserAction.setTitle({
    title: `Claude Browser Bridge — ${settings.paused ? "paused" : `${up} session(s) connected`}` +
      `${settings.readOnly ? " (read-only)" : ""}${activeRules(settings.allowed) ? " (allowlist on)" : ""}`,
  });
}

// ------------------------------------------------------------------ polling

async function pollLoop(port) {
  const base = `http://127.0.0.1:${port}`;
  for (;;) {
    if (!settings.token || settings.paused) { setSession(port, { up: false }); await sleep(1500); continue; }
    let r;
    try {
      r = await fetch(`${base}/poll`, { headers: { "X-Bridge-Token": settings.token }, cache: "no-store" });
    } catch {
      setSession(port, { up: false, error: null });
      await sleep(3000);
      continue;
    }
    if (r.status === 401) {
      // Token may have been rotated; re-read it through the native host (never for test-seeded tokens).
      if (settings.tokenSource !== "pairing" && await tokenFromNativeHost()) continue;
      setSession(port, { up: false, error: "token rejected — re-pair" });
      await sleep(5000);
      continue;
    }
    if (!r.ok && r.status !== 204) { await sleep(2000); continue; }
    // Learn which process is on this port before serving it, so a disconnected session can't
    // slip a command through (a stale pid fails closed: it stays refused until hello updates it).
    if (!sessions[port]?.up) await hello(base, port);
    setSession(port, { up: true, error: null });
    if (r.status === 204) continue;
    const cmd = await r.json();
    handle(base, cmd); // not awaited: keep polling so the server knows we're alive
  }
}

async function hello(base, port) {
  try {
    const h = await (await fetch(`${base}/hello`, { headers: { "X-Bridge-Token": settings.token } })).json();
    if (h.pid !== sessions[port]?.pid) setSession(port, { pid: h.pid, cwd: h.cwd, since: Date.now(), count: 0, lastTool: null, lastAt: null });
  } catch { /* ignore */ }
}

function setSession(port, patch) {
  const prev = sessions[port] || {};
  sessions[port] = { ...prev, ...patch };
  if (prev.up !== sessions[port].up) updateBadge();
}

async function handle(base, cmd) {
  const port = base.split(":").pop();
  const s = sessions[port] || {};
  const entry = { t: Date.now(), tool: cmd.tool, port, pid: s.pid, ok: null, url: "" };
  activity.unshift(entry);
  activity.length = Math.min(activity.length, 30);
  setSession(port, { count: (s.count || 0) + 1, lastTool: cmd.tool, lastAt: entry.t });
  let res;
  try {
    if (cutPids.has(s.pid))
      throw new Error("The user disconnected this Claude Code session from Firefox (Claude Browser Bridge popup). " +
                      "Stop using the browser tools; ask the user to click Reconnect if they want you back in.");
    const result = await execute(cmd.tool, cmd.args || {}, entry);
    res = { id: cmd.id, ok: true, result };
    entry.ok = true;
  } catch (e) {
    res = { id: cmd.id, ok: false, error: String(e?.message || e) };
    entry.ok = false; entry.error = res.error;
  }
  try {
    await fetch(`${base}/result`, {
      method: "POST",
      headers: { "X-Bridge-Token": settings.token, "Content-Type": "application/json" },
      body: JSON.stringify(res),
    });
  } catch { /* server went away */ }
}

// ------------------------------------------------------------------ guards

// siteDenied/activeRules come from guards.js (loaded first by the manifest).
function assertUrlAllowed(url) {
  const why = url && siteDenied(url, settings);
  if (why) throw new Error(`Not allowed: ${why}. Ask the user to do this step themselves or change the bridge's site settings.`);
}

const tabVisible = (t) => !siteDenied(t.url || "about:blank", settings);
const FRESH_TAB = /^about:(blank|newtab|home)$/;

async function getTabById(id) {
  const t = await browser.tabs.get(id);
  assertUrlAllowed(t.url);
  return t;
}

function assertScriptable(tab) {
  if (!/^(https?|file):/.test(tab.url || "")) throw new Error(`Can't script ${tab.url || "this tab"} (browser-internal page).`);
  if (/^https:\/\/(addons\.mozilla\.org|accounts\.firefox\.com)\//.test(tab.url)) throw new Error("Firefox does not allow extensions to script this site.");
}

// ------------------------------------------------------------------ helpers

async function getTab(args) {
  if (args.tab_id != null) return browser.tabs.get(args.tab_id);
  const [tab] = await browser.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) throw new Error("no active tab");
  return tab;
}

function waitForLoad(tabId, action, timeout = 15000) {
  return new Promise((resolve, reject) => {
    let started = false;
    const done = (note) => { clearTimeout(timer); browser.tabs.onUpdated.removeListener(onUpd); resolve(note); };
    const onUpd = (id, info) => {
      if (id !== tabId) return;
      if (info.status === "loading") started = true;
      if (info.status === "complete" && started) done("loaded");
    };
    const timer = setTimeout(() => done(started ? "still loading" : "no navigation observed"), timeout);
    browser.tabs.onUpdated.addListener(onUpd);
    Promise.resolve().then(action).catch((e) => { clearTimeout(timer); browser.tabs.onUpdated.removeListener(onUpd); reject(e); });
  });
}

async function agent(tab, op, args) {
  assertScriptable(tab);
  assertUrlAllowed(tab.url);
  await browser.scripting.executeScript({ target: { tabId: tab.id }, files: ["agent.js"] });
  const [r] = await browser.scripting.executeScript({
    target: { tabId: tab.id },
    func: (o, a) => window.__cbbAgent(o, a),
    args: [op, args],
  });
  const out = r?.result;
  if (!out) throw new Error(r?.error?.message || "page agent returned nothing (page navigating?)");
  if (!out.ok) throw new Error(out.error);
  return out.value;
}

async function mainWorld(tab, func, args = []) {
  assertScriptable(tab);
  assertUrlAllowed(tab.url);
  const [r] = await browser.scripting.executeScript({ target: { tabId: tab.id }, world: "MAIN", func, args });
  if (r?.error) throw new Error(String(r.error.message || r.error));
  return r?.result;
}

async function afterAction(tabId) {
  await sleep(350);
  const t = await browser.tabs.get(tabId).catch(() => null);
  return t ? { url: t.url, title: t.title, loading: t.status === "loading" } : { closed: true };
}

const tabInfo = (t) => ({ id: t.id, window: t.windowId, active: t.active, title: t.title, url: t.url, status: t.status });

async function blobToBase64(blob) {
  const buf = new Uint8Array(await blob.arrayBuffer());
  let s = "";
  for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  return btoa(s);
}

// ------------------------------------------------------------------ tools

async function execute(tool, args, entry) {
  if (settings.paused) throw new Error("Claude Browser Bridge is paused by the user.");
  if (settings.readOnly && !READ_ONLY_OK.has(tool))
    throw new Error(`Claude Browser Bridge is in read-only mode; "${tool}" is not allowed. Ask the user to switch it off.`);

  const tab = NO_PAGE.has(tool) ? null : await getTab(args);
  if (tab) {
    entry.url = tab.url;
    // The tab itself must be in bounds; only a blank/new tab may be navigated somewhere allowed.
    if (!(tool === "navigate" && FRESH_TAB.test(tab.url || ""))) assertUrlAllowed(tab.url || "about:blank");
  }

  switch (tool) {
    case "tabs_list":
      const all = await browser.tabs.query({});
      const shown = all.filter(tabVisible);
      return { tabs: shown.map(tabInfo), ...(shown.length < all.length ? { hidden_by_site_rules: all.length - shown.length } : {}) };

    case "tab_open": {
      if (args.url) assertUrlAllowed(args.url);
      let t = await browser.tabs.create({ url: args.url || "about:blank", active: args.active !== false });
      if (args.url) {
        for (let i = 0; i < 60 && (await browser.tabs.get(t.id)).status !== "complete"; i++) await sleep(250);
        t = await browser.tabs.get(t.id);
      }
      entry.url = t.url;
      return tabInfo(t);
    }

    case "tab_close":
      await getTabById(args.tab_id);
      await browser.tabs.remove(args.tab_id);
      return { closed: args.tab_id };

    case "tab_focus": {
      await getTabById(args.tab_id);
      const t = await browser.tabs.update(args.tab_id, { active: true });
      await browser.windows.update(t.windowId, { focused: true });
      return tabInfo(t);
    }

    case "navigate": {
      const action = args.action || (args.url ? "goto" : "reload");
      if (action === "goto") {
        if (!args.url) throw new Error("url is required for goto");
        assertUrlAllowed(args.url);
      }
      const note = await waitForLoad(tab.id, () => {
        if (action === "goto") return browser.tabs.update(tab.id, { url: args.url });
        if (action === "back") return browser.tabs.goBack(tab.id);
        if (action === "forward") return browser.tabs.goForward(tab.id);
        return browser.tabs.reload(tab.id, { bypassCache: !!args.hard });
      });
      const t = await browser.tabs.get(tab.id);
      return { ...tabInfo(t), load: note };
    }

    case "reload_matching": {
      const tabs = (await browser.tabs.query({})).filter((t) => (t.url || "").includes(args.url_contains) && !siteDenied(t.url, settings));
      await Promise.all(tabs.map((t) => browser.tabs.reload(t.id, { bypassCache: args.hard !== false })));
      return { reloaded: tabs.map((t) => ({ id: t.id, url: t.url })) };
    }

    case "screenshot": {
      assertUrlAllowed(tab.url);
      if (!tab.active) { await browser.tabs.update(tab.id, { active: true }); await sleep(300); }
      const win = await browser.windows.get(tab.windowId);
      if (win.state === "minimized") throw new Error("the tab's window is minimized; ask the user to restore it");
      const dataUrl = await browser.tabs.captureVisibleTab(tab.windowId, { format: "png" });
      const bmp = await createImageBitmap(await (await fetch(dataUrl)).blob());
      const maxW = args.max_width || 1280;
      const scale = Math.min(1, maxW / bmp.width);
      const w = Math.round(bmp.width * scale), h = Math.round(bmp.height * scale);
      const canvas = new OffscreenCanvas(w, h);
      canvas.getContext("2d").drawImage(bmp, 0, 0, w, h);
      const fmt = args.format === "png" ? "image/png" : "image/jpeg";
      const blob = await canvas.convertToBlob({ type: fmt, quality: 0.8 });
      let vp = null;
      try { vp = await agent(tab, "viewport", {}); shotScale[tab.id] = vp.width / w; } catch { shotScale[tab.id] = 1 / (scale * (self.devicePixelRatio || 1)); }
      return { data: await blobToBase64(blob), mime: fmt, width: w, height: h, tab_id: tab.id, url: tab.url, title: tab.title,
        note: "click x/y refer to pixels of this image" };
    }

    case "click": {
      const a = { ...args };
      if (a.x != null && a.y != null && !a.ref && !a.selector && !a.text) {
        const s = shotScale[tab.id] ?? 1;
        a.x *= s; a.y *= s;
      }
      const r = await agent(tab, "click", a);
      return { ...r, page: await afterAction(tab.id) };
    }

    case "type":
    case "press_key":
    case "select_option": {
      const r = await agent(tab, tool, args);
      return { ...r, page: await afterAction(tab.id) };
    }

    case "snapshot":
    case "get_text":
    case "find":
    case "hover":
    case "scroll":
    case "wait_for":
      return agent(tab, tool, args);

    case "evaluate": {
      const r = await mainWorld(tab, evalInPage, [args.code]);
      if (!r || !r.ok) throw new Error(r?.error || "evaluation failed (page CSP may forbid eval)");
      return { value: r.value };
    }

    case "logs_start":
      return mainWorld(tab, startLogCapture);

    case "logs_read": {
      const r = await mainWorld(tab, readLogs, [args.kind || "all", !!args.errors_only, !!args.clear]);
      if (!r) throw new Error("not capturing in this tab — call logs_start first (capture resets on navigation)");
      return r;
    }

    default:
      throw new Error(`unknown tool ${tool}`);
  }
}

// ------------------------------------------------------------------ main-world functions
// These are serialised and run in the page's own JS context, so they must be self-contained.

function evalInPage(code) {
  const ser = (v) => {
    if (v === undefined) return null;
    try {
      return JSON.parse(JSON.stringify(v, (k, x) =>
        typeof x === "bigint" ? x.toString() : x instanceof Element ? x.outerHTML.slice(0, 500) : x));
    } catch { return String(v); }
  };
  let fn;
  const expr = code.trim().replace(/;+\s*$/, "");
  try { fn = new Function(`return (async () => (${expr}\n))();`); }
  catch { try { fn = new Function(`return (async () => {${code}\n})();`); } catch (e) { return { ok: false, error: String(e) }; } }
  return Promise.resolve().then(fn).then((v) => ({ ok: true, value: ser(v) }), (e) => ({ ok: false, error: String(e?.stack || e) }));
}

function startLogCapture() {
  if (window.__cbbLogs) return { capturing: true, already: true };
  const logs = (window.__cbbLogs = []);
  const push = (e) => { logs.push({ t: new Date().toISOString(), ...e }); if (logs.length > 1000) logs.shift(); };
  const fmt = (x) => {
    if (typeof x === "string") return x;
    if (x instanceof Error) return x.stack || String(x);
    try { return JSON.stringify(x); } catch { return String(x); }
  };
  for (const level of ["log", "info", "warn", "error", "debug"]) {
    const orig = console[level];
    console[level] = function (...a) {
      try { push({ kind: "console", level, msg: a.map(fmt).join(" ").slice(0, 4000) }); } catch {}
      return orig.apply(this, a);
    };
  }
  addEventListener("error", (e) => push({ kind: "console", level: "error", msg: `Uncaught ${e.message} @ ${e.filename}:${e.lineno}` }));
  addEventListener("unhandledrejection", (e) => push({ kind: "console", level: "error", msg: "Unhandled rejection: " + fmt(e.reason) }));
  const origFetch = window.fetch;
  window.fetch = async function (input, init) {
    const t0 = performance.now();
    const method = (init && init.method) || (input && input.method) || "GET";
    const url = String((input && input.url) || input);
    try {
      const r = await origFetch.apply(this, arguments);
      push({ kind: "network", via: "fetch", method, url, status: r.status, ms: Math.round(performance.now() - t0) });
      return r;
    } catch (err) {
      push({ kind: "network", via: "fetch", method, url, status: 0, error: String(err), ms: Math.round(performance.now() - t0) });
      throw err;
    }
  };
  const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__cbb = { method: m, url: String(u) }; return open.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    const t0 = performance.now(), info = this.__cbb || {};
    this.addEventListener("loadend", () => push({ kind: "network", via: "xhr", ...info, status: this.status, ms: Math.round(performance.now() - t0) }));
    return send.apply(this, arguments);
  };
  return { capturing: true };
}

function readLogs(kind, errorsOnly, clear) {
  const logs = window.__cbbLogs;
  if (!logs) return null;
  let out = logs.filter((e) => kind === "all" || e.kind === kind);
  if (errorsOnly) out = out.filter((e) => e.level === "error" || e.level === "warn" || (e.kind === "network" && (e.status === 0 || e.status >= 400)));
  out = JSON.parse(JSON.stringify(out));
  if (clear) logs.length = 0;
  return { count: out.length, entries: out };
}

// ------------------------------------------------------------------ popup messaging

browser.runtime.onMessage.addListener(async (msg) => {
  if (msg.type === "state") {
    const view = {};
    for (const [port, s] of Object.entries(sessions)) view[port] = { ...s, disconnected: cutPids.has(s.pid) };
    return { settings, sessions: view, activity };
  }
  if (msg.type === "disconnect" || msg.type === "reconnect") {
    const pid = sessions[msg.port]?.pid;
    if (pid == null) return { ok: false, error: "unknown session" };
    msg.type === "disconnect" ? cutPids.add(pid) : cutPids.delete(pid);
    updateBadge();
    return { ok: true };
  }
  if (msg.type === "kill") {
    const s = sessions[msg.port];
    if (!s?.up) return { ok: false, error: "session not connected" };
    try {
      const r = await fetch(`http://127.0.0.1:${msg.port}/shutdown`, {
        method: "POST",
        headers: { "X-Bridge-Token": settings.token, "Content-Type": "application/json" },
        body: JSON.stringify({ pid: s.pid }),
      });
      if (!r.ok) return { ok: false, error: `bridge said ${r.status}` };
    } catch (e) { return { ok: false, error: String(e) }; }
    cutPids.delete(s.pid);
    setSession(msg.port, { up: false, error: null, pid: null });
    return { ok: true };
  }
  if (msg.type === "save") { await browser.storage.local.set(msg.patch); return { ok: true }; }
});

loadSettings().then(() => PORTS.forEach(pollLoop));
