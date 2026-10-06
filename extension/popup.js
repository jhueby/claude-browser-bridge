const $ = (id) => document.getElementById(id);
const save = (patch) => browser.runtime.sendMessage({ type: "save", patch });

function li(text, cls) {
  const el = document.createElement("li");
  el.textContent = text;
  if (cls) el.className = cls;
  return el;
}

async function render(first) {
  const { settings, sessions, activity } = await browser.runtime.sendMessage({ type: "state" });
  if (first) {
    $("paused").checked = settings.paused;
    $("readOnly").checked = settings.readOnly;
    $("blocked").value = (settings.blocked || []).join("\n");
  }
  $("tokenState").textContent = settings.token ? "paired" : "not paired";
  $("tokenState").className = settings.token ? "ok" : "bad";

  const ul = $("sessions");
  ul.replaceChildren();
  const ports = Object.entries(sessions).filter(([, s]) => s.up || s.error);
  if (!ports.length) ul.append(li(settings.paused ? "paused" : "no Claude Code sessions running the bridge", "muted"));
  for (const [port, s] of ports) {
    ul.append(li(s.up ? `● :${port}  ${s.cwd || ""}` : `✕ :${port}  ${s.error}`, s.up ? "ok" : "bad"));
  }

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
$("saveToken").onclick = async () => { await save({ token: $("token").value.trim() }); $("token").value = ""; render(); };
$("saveBlocked").onclick = () => save({ blocked: $("blocked").value.split("\n").map((s) => s.trim()).filter(Boolean) });

render(true);
setInterval(render, 1000);
