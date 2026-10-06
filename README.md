# claude-browser-bridge

Let [Claude Code](https://claude.com/claude-code) drive your **real Firefox**, including your logged-in sessions, through an MCP server that only listens on loopback.

It's a small clean-room rewrite of the idea behind `nanogenomic/ClaudeCodeBrowser`. No code was copied. The goal is a version you can read end to end in one sitting:

| Part | Design |
|---|---|
| Server | one Python file, **stdlib only** (no `pip install`) |
| Extension | ~800 lines of plain JS, no build step, no bundled libraries |
| Network | `127.0.0.1` only; no remote endpoints, telemetry, or auto-update URL |
| Page access | scripts are injected **on demand** into the one tab being driven, not into every frame of every page |
| Guard rails | pause switch, read-only mode, blocked-sites list, refuses password fields, local audit log |

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

Requirements: Python 3.9+ and Firefox 142+.

**1. Register the MCP server with Claude Code** (user scope, so every project gets it):

```bash
claude mcp add --scope user browser-bridge -- python /path/to/claude-browser-bridge/server/bridge.py
```

**2. Load the extension.** Pick one:

- *Quick (until Firefox restarts):* open `about:debugging#/runtime/this-firefox`, choose **Load Temporary Add-on…**, and pick `extension/manifest.json`.
- *Permanent:* Firefox release builds only install signed add-ons. Sign it **unlisted** with your own free AMO account, which gives a private `.xpi` that is never published:
  ```bash
  npx web-ext sign --source-dir extension --channel unlisted --api-key $AMO_JWT_ISSUER --api-secret $AMO_JWT_SECRET
  ```
  Then drag the `.xpi` from `web-ext-artifacts/` into Firefox. (Developer Edition, Nightly, or ESR with `xpinstall.signatures.required=false` can install it unsigned instead.)

**3. Pair:**

```bash
python server/bridge.py --token
```

Paste the output into the extension popup and click **Save**. The badge then shows the number of connected Claude sessions: orange for full control, blue for read-only, `II` when paused, and red `!` while unpaired.

## Using it

Ask Claude something like *"open localhost:3000 in Firefox, log in with the seed user, and screenshot the dashboard"*. A good loop is `snapshot`, then act by ref, then `snapshot` or `screenshot` to check the result.

The popup lists the connected sessions (with each one's working directory) and the last 30 actions. **Pause** disconnects everything immediately.

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
extension/background.js  polling, tab control, tool dispatch, guards
extension/agent.js       on-demand page agent (snapshot, refs, click/type/...)
extension/popup.*        status, pause, read-only, token, blocklist, activity
test/e2e.py              real Firefox (throwaway profile) end-to-end test
test/test_security.py    endpoint hardening checks
```

Run tests with `python test/test_security.py` and `python test/e2e.py`. The e2e test needs Node for `npx web-ext`; it uses a temporary profile and token and leaves your real setup alone.

~/.claude-browser-bridge/ holds `token`, `audit.log` (JSONL of every call, with typed text redacted), and `screenshots/`.
