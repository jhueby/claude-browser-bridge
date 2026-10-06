// Unit tests for extension/guards.js.   node test/test_guards.js
const assert = require("assert");
const { siteDenied } = require("../extension/guards.js");

const cases = [
  // [url, settings, allowed?]
  ["https://example.com/", {}, true],
  ["https://mybank.com/x", { blocked: ["mybank.com"] }, false],
  ["https://login.mybank.com/x", { blocked: ["mybank.com"] }, false],
  ["https://notmybank.com/", { blocked: ["mybank.com"] }, true],
  ["https://a.paypal.com/", { blocked: ["*.paypal.com"] }, false],
  ["https://localhost:3000/", { allowed: ["localhost"] }, true],
  ["http://127.0.0.1:8080/", { allowed: ["localhost"] }, false],
  ["https://tryhackme.com/room", { allowed: ["tryhackme.com"] }, true],
  ["https://www.tryhackme.com/room", { allowed: ["tryhackme.com"] }, true],
  ["https://tryhackme.com.evil.io/", { allowed: ["tryhackme.com"] }, false],
  ["https://eviltryhackme.com/", { allowed: ["tryhackme.com"] }, false],
  ["https://dev-api.corp.test/", { allowed: ["dev-*.corp.test"] }, true],
  ["https://prod-api.corp.test/", { allowed: ["dev-*.corp.test"] }, false],
  ["https://mail.google.com/", { allowed: ["google.com"], blocked: ["mail.google.com"] }, false], // block wins
  ["https://docs.google.com/", { allowed: ["google.com"], blocked: ["mail.google.com"] }, true],
  ["about:blank", { allowed: ["localhost"] }, false],
  ["about:blank", { blocked: ["x.com"] }, true],
  ["file:///C:/x.html", { allowed: ["localhost"] }, false],
  ["https://anything.com/", { allowed: ["# just a comment", "  "] }, true], // comments/blank = allowlist off
  ["https://ex.com/", { allowed: ["EX.COM"] }, true], // case-insensitive
];

let fails = 0;
for (const [url, settings, want] of cases) {
  const got = siteDenied(url, settings) === null;
  const ok = got === want;
  if (!ok) fails++;
  console.log(`${ok ? "PASS" : "FAIL"} ${want ? "allow" : "deny "} ${url} ${JSON.stringify(settings)}`);
}
assert.strictEqual(fails, 0, `${fails} guard case(s) failed`);
console.log("ALL PASSED");
