const $ = (id) => document.getElementById(id);
const save = (patch) => browser.runtime.sendMessage({ type: "save", patch });

function li(text, cls) {
  const el = document.createElement("li");
  el.textContent = text;
  if (cls) el.className = cls;
  return el;
}

const ago = (t) => {
  if (!t) return "never";
  const s = Math.round((Date.now() - t) / 1000);
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`;
};

function button(label, title, onclick) {
  const b = document.createElement("button");
  b.textContent = label;
  b.title = title;
  b.onclick = onclick;
  return b;
}

let sessionKey = "";
function renderSessions(settings, sessions) {
  const ul = $("sessions");
  const rows = Object.entries(sessions).filter(([, s]) => s.up || s.error);
  // Rebuilding every second would eat clicks mid-press; only rebuild when something changed.
  const key = JSON.stringify([settings.paused, rows, Math.floor(Date.now() / 10000)]);
  if (key === sessionKey) return;
  sessionKey = key;
  ul.replaceChildren();
  if (!rows.length) { ul.append(li(settings.paused ? "paused" : "no Claude Code sessions running the bridge", "muted")); return; }
  for (const [port, s] of rows) {
    const row = document.createElement("li");
    row.className = "session";
    const head = document.createElement("div");
    if (!s.up) {
      head.textContent = `✕ :${port}  ${s.error}`;
      head.className = "bad";
      row.append(head);
      ul.append(row);
      continue;
    }
    head.textContent = `${s.disconnected ? "◌" : "●"} ${s.cwd || "(unknown folder)"}`;
    head.className = s.disconnected ? "muted" : "ok";
    const meta = document.createElement("div");
    meta.className = "muted";
    meta.textContent = `port ${port} · pid ${s.pid ?? "?"} · ${s.count || 0} cmds · last ${s.lastTool || "—"} ${ago(s.lastAt)}` +
      (s.disconnected ? " · DISCONNECTED" : "");
    const actions = document.createElement("div");
    actions.append(
      s.disconnected
        ? button("Reconnect", "Let this session use Firefox again", () => act("reconnect", port))
        : button("Disconnect", "Refuse every command from this session until you reconnect it", () => act("disconnect", port)),
      killButton(port),
    );
    row.append(head, meta, actions);
    ul.append(row);
  }
}

// Firefox popups can't use confirm(), so Kill asks for a second click within 4 seconds.
const armedUntil = {}; // port -> time; kept outside the DOM so it survives re-renders

function killButton(port) {
  const armed = (armedUntil[port] || 0) > Date.now();
  const b = button(armed ? "Click again to kill" : "Kill",
    "Shut down this session's bridge process; its browser tools stop until that Claude session reconnects MCP servers", () => {
      if ((armedUntil[port] || 0) > Date.now()) { delete armedUntil[port]; return act("kill", port); }
      armedUntil[port] = Date.now() + 4000;
      sessionKey = "";
      render();
      setTimeout(() => { sessionKey = ""; render(); }, 4100);
    });
  if (armed) b.className = "bad";
  return b;
}

async function act(type, port) {
  const r = await browser.runtime.sendMessage({ type, port });
  $("sessionMsg").textContent = r?.ok ? "" : `${type} failed: ${r?.error || "unknown error"}`;
  sessionKey = "";
  render();
}

async function render(first) {
  const { settings, sessions, activity, nativeError } = await browser.runtime.sendMessage({ type: "state" });
  $("nativeError").textContent = nativeError && settings.tokenSource !== "manual"
    ? `Automatic pairing failed: ${nativeError}. Run python native/install.py, then click Pair automatically.` : "";
  if (first) {
    $("paused").checked = settings.paused;
    $("readOnly").checked = settings.readOnly;
    $("allowEvaluate").checked = !!settings.allowEvaluate;
    $("blocked").value = (settings.blocked || []).join("\n");
    $("allowed").value = (settings.allowed || []).join("\n");
  }
  $("tokenState").textContent = !settings.token ? "not paired"
    : settings.tokenSource === "native" ? "paired automatically (native host)" : "paired";
  const allowOn = (settings.allowed || []).some((p) => p.trim() && !p.trim().startsWith("#"));
  $("allowState").textContent = allowOn ? "— ON (restricted)" : "— off";
  $("allowState").className = allowOn ? "ok" : "muted";
  $("tokenState").className = settings.token ? "ok" : "bad";

  renderSessions(settings, sessions);

  const act = $("activity");
  act.replaceChildren();
  if (!activity.length) act.append(li("nothing yet", "muted"));
  for (const a of activity) {
    const t = new Date(a.t).toLocaleTimeString();
    const status = a.ok === null ? "…" : a.ok ? "✓" : "✕";
    act.append(li(`${t} ${status} ${a.tool}  ${a.url || ""}${a.error ? "  — " + a.error : ""}`, a.ok === false ? "bad" : ""));
  }
}

$("paused").onchange = (e) => save({ paused: e.target.checked });
$("readOnly").onchange = (e) => save({ readOnly: e.target.checked });
$("allowEvaluate").onchange = (e) => save({ allowEvaluate: e.target.checked });
$("pairNow").onclick = async () => { await browser.runtime.sendMessage({ type: "pairNow" }); render(); };
$("saveToken").onclick = async () => { await save({ token: $("token").value.trim(), tokenSource: "manual" }); $("token").value = ""; render(); };
const lines = (id) => $(id).value.split("\n").map((s) => s.trim()).filter(Boolean);
$("saveBlocked").onclick = () => save({ blocked: lines("blocked") }).then(() => render());
$("saveAllowed").onclick = () => save({ allowed: lines("allowed") }).then(() => render());

render(true);
setInterval(render, 1000);
