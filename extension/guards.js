// Site access rules, shared by background.js and test/test_guards.js.
//
// Patterns are hostnames, one per entry: "example.com" also covers its subdomains,
// "*" is a wildcard ("*.corp.example", "dev-*.example.com"), "#" starts a comment.
// The blocklist always wins. A non-empty allowlist means "only these sites".

function hostMatches(host, patterns) {
  for (const raw of patterns || []) {
    const p = String(raw).trim().toLowerCase();
    if (!p || p.startsWith("#")) continue;
    const re = new RegExp("^" + p.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*") + "$");
    if (re.test(host) || host.endsWith("." + p)) return raw.trim();
  }
  return null;
}

const activeRules = (list) => (list || []).some((p) => String(p).trim() && !String(p).trim().startsWith("#"));

// Returns null when the URL may be touched, otherwise a human-readable reason.
function siteDenied(url, { blocked = [], allowed = [] } = {}) {
  let host;
  try { host = new URL(url).hostname.toLowerCase(); } catch { return `unparseable URL ${url}`; }
  const b = host && hostMatches(host, blocked);
  if (b) return `blocked by the user's blocklist (${b})`;
  if (activeRules(allowed) && !(host && hostMatches(host, allowed)))
    return `${host || url} is not on the user's allowlist`;
  return null;
}

if (typeof module !== "undefined") module.exports = { hostMatches, siteDenied, activeRules };
