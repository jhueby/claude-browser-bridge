# claude-browser-bridge

Let [Claude Code](https://claude.com/claude-code) drive your **real Firefox**, including your logged-in sessions, through an MCP server that only listens on loopback.

It's a small clean-room rewrite of the idea behind `nanogenomic/ClaudeCodeBrowser`. No code was copied. The goal is a version you can read end to end in one sitting:

| Part | Design |
|---|---|
| Server | one Python file, **stdlib only** (no `pip install`) |
| Extension | ~800 lines of plain JS, no build step, no bundled libraries |
| Network | `127.0.0.1` only; no remote endpoints, telemetry, or auto-update URL |
| Page access | scripts are injected **on demand** into the one tab being driven, not into every frame of every page |
| Guard rails | pause switch, read-only mode, site allowlist + blocklist, per-session disconnect/kill, refuses password fields, local audit log |

See [SECURITY.md](SECURITY.md) for the threat model.

## How it works

```
Claude Code ──stdio (MCP)──> server/bridge.py ──127.0.0.1:8777 long-poll──> Firefox extension ──> tab
```

Each Claude Code session spawns its own `bridge.py`, which binds the first free port in `8777-8786`. The extension long-polls all ten ports, so several sessions can share one browser. Every request has to carry the pairing token. Requests from web origins and requests with a non-loopback `Host` header are rejected.

## Tools

| Tool | What it does |
|---|---|
| `tabs_list`, `tab_open`, `tab_close`, `tab_focus` | tab management |
| `navigate` | goto / back / forward / reload (`hard` bypasses cache) |
| `reload_matching` | reload every tab whose URL contains a string (restart a dev server, refresh its tabs) |
| `screenshot` | visible area as an image (downscaled JPEG by default), also saved to disk |
| `snapshot` | page outline: headings plus every visible interactive element with a ref like `[e7] button "Save"` |
| `find`, `get_text` | locate elements by text or CSS; read visible text |
| `click`, `type`, `press_key`, `select_option`, `hover`, `scroll` | act on a ref, selector, text, or screenshot coordinate |
| `wait_for` | wait for a selector or text to appear or disappear |
| `evaluate` | run JS in the page's main world (expression or function body, `await` allowed) |
| `logs_start`, `logs_read` | capture console messages and fetch/XHR traffic in a tab |
| `bridge_status` | connection state and file locations |

## Install

Requirements: Python 3.9+, Firefox 142+, and Node (only to sign the extension).

**1. Register the MCP server with Claude Code** (user scope, so every project gets it):

```bash
claude mcp add --scope user browser-bridge -- python /path/to/claude-browser-bridge/server/bridge.py
```

**2. Install the native pairing host** (once, per user, no admin rights):

```bash
python native/install.py
```

Firefox launches `native/host.py` through its standard native-messaging mechanism, and only for this extension's ID. The host reads `~/.claude-browser-bridge/token`, so the extension pairs itself: no token to paste, and if the token is ever rotated, it re-fetches it. On Windows this writes one key, `HKCU\Software\Mozilla\NativeMessagingHosts\claude_browser_bridge`. `python native/install.py --remove` undoes it.

Run it from your own terminal. Sandboxed agent shells, including Claude Code's tool sandbox on Windows, can virtualize registry writes: the install appears to succeed, but your real Firefox then reports "No such native application claude_browser_bridge".

**3. Install the extension permanently.** Firefox release builds only keep signed add-ons, so sign it **unlisted** with your own free AMO account. This produces a private `.xpi` that Mozilla signs and never publishes:

1. Create API keys at <https://addons.mozilla.org/developers/addon/api/key/>.
2. Save them to `~/.claude-browser-bridge/amo.json` as `{"issuer": "user:…", "secret": "…"}`, or set `AMO_JWT_ISSUER` / `AMO_JWT_SECRET`.
3. Run:
   ```bash
   python scripts/sign.py --install
   ```
   Firefox shows its normal add-on prompt; click **Add**. It now survives restarts, like any other extension.

When you change the extension later, run `python scripts/sign.py --bump --install`. AMO never signs the same version twice.

*Quick alternative (until Firefox restarts):* go to `about:debugging#/runtime/this-firefox`, choose **Load Temporary Add-on…**, and pick `extension/manifest.json`.

*No native host?* Pair by hand instead: run `python server/bridge.py --token` and paste the output into the popup.

The badge shows the number of connected Claude sessions: orange for full control, blue for read-only, `II` when paused, and red `!` while unpaired.

## Using it

Ask Claude something like *"open localhost:3000 in Firefox, log in with the seed user, and screenshot the dashboard"*. A good loop is `snapshot`, then act by ref, then `snapshot` or `screenshot` to check the result.

### The popup

- **Pause** disconnects every session at once. **Read-only** allows only observation tools.
- **Sessions** lists every Claude Code session using the bridge: its project folder, port, PID, number of commands, and last action. Each session has two controls:
  - **Disconnect** refuses that session's commands, and it gets a clear "the user disconnected you" error. **Reconnect** undoes it.
  - **Kill** (click twice to confirm) shuts down that session's bridge process. Its browser tools stop until the session reconnects its MCP servers (`/mcp`) or restarts. The Claude session itself keeps running.
- **Allowed sites:** if any are listed, Claude can *only* touch tabs on those hosts. Every other tab is hidden from `tabs_list`, and navigating a tab off-list locks Claude out of that tab. A blank or new tab may still be navigated to an allowed site. Leave the list empty to turn it off.
- **Blocked sites** always win, even over the allowlist.
- **Recent actions** shows the last 30 commands.

Site patterns are hostnames, one per line: `example.com` also covers its subdomains, `*` is a wildcard (`dev-*.corp.test`), and `#` starts a comment. Ports and schemes are ignored, and `file://` pages count as having no host, so they're refused whenever the allowlist is on.

## Limitations

- Input events are synthetic (`isTrusted=false`). Most sites accept them; a few anti-bot checks and some drag-and-drop UIs won't.
- Only the top frame is scripted. Content inside iframes, including cross-origin embeds, is out of reach.
- `evaluate` follows the page's Content-Security-Policy, so it fails on sites that forbid `unsafe-eval`.
- Console and network capture is in-page (it patches `console`, `fetch`, and `XHR`) and resets on navigation.
- Screenshots cover the visible area only, and the tab is brought to the front to take one.
- Firefox only. On Chrome, use Anthropic's Claude in Chrome extension.

## Files

```
server/bridge.py         MCP stdio server + loopback HTTP endpoint
extension/background.js  polling, tab control, tool dispatch, session controls
extension/guards.js      allowlist/blocklist matching (shared with the unit test)
extension/agent.js       on-demand page agent (snapshot, refs, click/type/...)
extension/popup.*        status, pause, read-only, sessions, pairing, site lists, activity
native/host.py           native-messaging host: hands the token to the extension
native/install.py        registers/removes the native host (per user)
scripts/sign.py          unlisted AMO signing -> permanent .xpi
test/e2e.py              real Firefox (throwaway profile) end-to-end test; --allowlist for the allowlist suite;
                         drives the real popup over Marionette to test Disconnect/Reconnect/Kill
test/test_security.py    endpoint hardening checks
test/test_guards.js      site-rule unit tests (node)
test/test_native.py      native host protocol test
```

Run tests with `node test/test_guards.js`, `python test/test_security.py`, `python test/e2e.py`, `python test/e2e.py --allowlist`, `python test/test_native.py`, and `python test/e2e.py --native` (checks auto-pairing; the test browser stays paused because it receives your real token). Set `BRIDGE_DEBUG=1` to log every HTTP request the bridge receives to stderr. The e2e test needs Node for `npx web-ext`; it uses a temporary profile and token and leaves your real setup alone.

~/.claude-browser-bridge/ holds `token`, `audit.log` (JSONL of every call, with typed text redacted), and `screenshots/`.
