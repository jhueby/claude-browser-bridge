# Security model

This extension gives a local AI agent your browser, including your logged-in sessions. Treat that like handing someone your unlocked laptop. This file covers what the design defends against and what it doesn't.

## Defended

| Threat | Mitigation |
|---|---|
| A web page you visit talks to the bridge at `127.0.0.1` | The server rejects any request with a non-extension `Origin`, rejects every CORS preflight, and requires a custom `X-Bridge-Token` header, which a page can't send cross-origin without a preflight. Tested in `test/test_security.py`. |
| DNS rebinding (`evil.example` resolving to 127.0.0.1) | The `Host` header must be `127.0.0.1`, `localhost`, or `::1`. |
| Another machine on the LAN | The server binds `127.0.0.1` only. |
| Token guessing / timing | 256-bit token from `secrets`, compared with `hmac.compare_digest`. |
| Page scripts tampering with element refs | Refs live in the extension's isolated world (a `Map` of `WeakRef`s); the page DOM is never annotated. |
| Silent updates by an upstream author | No `update_url`. The code you load is the code you read. |
| Dependency supply chain | The server is stdlib-only; the extension has no third-party code. |
| The agent typing your passwords | `type` refuses `input[type=password]`. Snapshots show `<hidden>` for password values. |
| The agent on sites you care about | Blocked-sites list (hosts, `*` wildcards, subdomains included), checked before any script runs and before navigation. |
| Wanting to watch, not act | Read-only mode allows only observation tools. |
| Wanting it to stop now | Pause disconnects every session immediately; in-flight commands are refused. |
| After-the-fact review | `~/.claude-browser-bridge/audit.log` records every call with timestamp, PID, and arguments (typed text redacted); the popup shows the last 30 actions. |

## Not defended

- **Prompt injection.** A page can contain text addressed to Claude. The bridge can't tell good instructions from bad ones; that's Claude's job and yours. Use the blocklist for sensitive sites and read-only mode when you only need to look.
- **Malware running as your user.** It can read the token file and drive the bridge, or just read your Firefox profile directly. Same-user isolation is out of scope.
- **`evaluate`.** This runs arbitrary JS in the page, by design. Disable the tool in Claude Code's permissions if you don't want it.
- **Port squatting.** If another local process binds 8777-8786 before the bridge, the extension would send it your token and accept commands from it. This needs local code execution, which is covered above.
