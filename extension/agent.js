// Page agent. Injected on demand (never as a standing content script) into the
// extension's isolated world of a single tab's top frame. Defines window.__cbbAgent
// once per document; background.js then calls it with (op, args).
//
// Element refs live only in this isolated world (a Map of WeakRefs), so the page's
// own scripts cannot see or tamper with them, and the page DOM is never annotated.
(() => {
  if (window.__cbbAgent) return;

  let counter = 0;
  let PREFIX = "";                  // "f23:" when this agent runs inside iframe 23; set per call
  const byRef = new Map();          // "e12" -> WeakRef(Element)
  const refOfEl = new WeakMap();    // Element -> "e12"

  // Shadow DOM: Firefox gives extension content scripts openOrClosedShadowRoot, so even closed
  // shadow roots (common in web components) are reachable. Page scripts can't do this.
  const shadowOf = (el) => el.openOrClosedShadowRoot || el.shadowRoot || null;

  // All elements matching `selector` in tree order, descending into shadow roots.
  function deepAll(selector, root = document) {
    const out = [];
    const walk = (r) => {
      for (const el of r.querySelectorAll("*")) {
        if (el.matches(selector)) out.push(el);
        const sr = shadowOf(el);
        if (sr) walk(sr);
      }
    };
    walk(root);
    return out;
  }

  function deepQuery(selector) {
    const direct = document.querySelector(selector);
    return direct || deepAll(selector)[0] || null;
  }

  // elementFromPoint stops at shadow hosts; keep drilling into their shadow roots.
  function deepElementFromPoint(x, y) {
    let el = document.elementFromPoint(x, y);
    for (let i = 0; el && i < 20; i++) {
      const sr = shadowOf(el);
      const inner = sr?.elementFromPoint?.(x, y);
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  }

  // Focused element, following focus into shadow roots.
  function deepActive() {
    let el = document.activeElement;
    for (let i = 0; el && i < 20; i++) {
      const inner = shadowOf(el)?.activeElement;
      if (!inner) break;
      el = inner;
    }
    return el;
  }

  const isFrame = (el) => el.tagName === "IFRAME" || el.tagName === "FRAME";
  const frameIdOf = (el) => {
    try { return browser.runtime.getFrameId(el); } catch { return -1; }
  };

  const INTERACTIVE = [
    "a[href]", "button", "input:not([type=hidden])", "select", "textarea", "summary",
    "[role=button]", "[role=link]", "[role=checkbox]", "[role=radio]", "[role=tab]", "[role=menuitem]",
    "[role=option]", "[role=switch]", "[role=combobox]", "[role=textbox]", "[role=searchbox]",
    "[contenteditable='']", "[contenteditable=true]", "[onclick]", "[tabindex]:not([tabindex='-1'])",
  ].join(",");
  const HEADINGS = "h1,h2,h3,h4";

  const clean = (s, n = 80) => {
    s = (s || "").replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  };

  function ref(el) {
    let r = refOfEl.get(el);
    if (!r) {
      r = "e" + ++counter;
      refOfEl.set(el, r);
      byRef.set(r, new WeakRef(el));
    }
    return r;
  }

  function visible(el) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    if (el.checkVisibility) return el.checkVisibility({ opacityProperty: true, visibilityProperty: true });
    const cs = getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none" && cs.opacity !== "0";
  }

  function inViewport(el) {
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
  }

  function role(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "a") return "link";
    if (tag === "button" || tag === "summary") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      const t = (el.type || "text").toLowerCase();
      if (["button", "submit", "reset", "image"].includes(t)) return "button";
      if (t === "checkbox" || t === "radio") return t;
      if (t === "range") return "slider";
      if (t === "search") return "searchbox";
      return "textbox";
    }
    if (el.isContentEditable) return "textbox";
    return tag;
  }

  function name(el) {
    const aria = el.getAttribute("aria-label");
    if (aria) return clean(aria);
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" ");
      if (t.trim()) return clean(t);
    }
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    if (el.tagName === "INPUT" && ["button", "submit", "reset"].includes(el.type)) return clean(el.value);
    const txt = clean(el.innerText);
    if (txt) return txt;
    const img = el.querySelector?.("img[alt]");
    return clean(el.getAttribute("placeholder") || el.getAttribute("title") || img?.alt || el.getAttribute("name") || "");
  }

  function describe(el) {
    if (isFrame(el)) {
      const label = clean(el.getAttribute("title") || el.getAttribute("name") || el.getAttribute("aria-label") || "", 60);
      const src = clean(el.getAttribute("src") || (el.hasAttribute("srcdoc") ? "(srcdoc)" : ""), 80);
      return `[${PREFIX}${ref(el)}] iframe${label ? ` "${label}"` : ""}${src ? ` → ${src}` : ""}${inViewport(el) ? "" : " (offscreen)"}`;
    }
    const r = role(el);
    let line = `[${PREFIX}${ref(el)}] ${r}`;
    if (r === "heading") line += ` h${el.tagName[1]}`;
    const n = name(el);
    if (n) line += ` "${n}"`;
    const bits = [];
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      if (el.type === "password") bits.push(`value=${el.value ? "<hidden>" : '""'}`);
      else if (["checkbox", "radio"].includes(el.type)) bits.push(el.checked ? "checked" : "unchecked");
      else bits.push(`value="${clean(el.value, 60)}"`);
      if (el.placeholder && n !== clean(el.placeholder)) bits.push(`placeholder="${clean(el.placeholder, 40)}"`);
    }
    if (el.tagName === "SELECT") bits.push(`selected="${clean(el.selectedOptions[0]?.text || "", 40)}"`);
    if (el.getAttribute("aria-expanded")) bits.push(`expanded=${el.getAttribute("aria-expanded")}`);
    if (el.getAttribute("aria-checked")) bits.push(`checked=${el.getAttribute("aria-checked")}`);
    if (el.disabled || el.getAttribute("aria-disabled") === "true") bits.push("disabled");
    if (el.tagName === "A") {
      const href = el.getAttribute("href") || "";
      if (href && !href.startsWith("javascript:")) bits.push(`→ ${clean(href, 80)}`);
    }
    if (!inViewport(el)) bits.push("(offscreen)");
    return bits.length ? `${line} ${bits.join(" ")}` : line;
  }

  function fromRef(r) {
    r = String(r).replace(/^f\d+:/, "");
    const el = byRef.get(r)?.deref();
    if (!el || !el.isConnected) throw new Error(`ref ${r} is stale or unknown — take a new snapshot`);
    return el;
  }

  function byText(query, pool) {
    const q = query.trim().toLowerCase();
    const cands = pool || deepAll(INTERACTIVE).filter(visible);
    const exact = cands.filter((el) => name(el).toLowerCase() === q);
    if (exact.length) return exact;
    const partial = cands.filter((el) => name(el).toLowerCase().includes(q));
    if (partial.length || pool) return partial;
    // fall back to the deepest visible element of any kind containing the text
    const all = deepAll("*").filter(
      (el) => !isFrame(el) && el.innerText && el.innerText.toLowerCase().includes(q) && visible(el)
    );
    return all.filter((el) => !all.some((o) => o !== el && el.contains(o)));
  }

  function resolve(a, { optional = false } = {}) {
    if (a.ref) return fromRef(a.ref);
    if (a.selector) {
      const el = deepQuery(a.selector);
      if (!el) throw new Error(`no element matches selector ${a.selector}`);
      return el;
    }
    if (a.text) {
      const hits = byText(a.text);
      if (!hits.length) throw new Error(`no visible element with text "${a.text}"`);
      return hits[0];
    }
    if (optional) return null;
    throw new Error("give a ref, selector, or text");
  }

  function center(el) {
    el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }

  function mouse(el, type, x, y, extra = {}) {
    const Ctor = type.startsWith("pointer") ? PointerEvent : MouseEvent;
    el.dispatchEvent(new Ctor(type, {
      bubbles: !type.endsWith("enter") && !type.endsWith("leave"), cancelable: true, composed: true,
      clientX: x, clientY: y, button: 0, buttons: type.endsWith("down") ? 1 : 0, view: window, ...extra,
    }));
  }

  function click(el, x, y, dbl) {
    mouse(el, "pointerover", x, y); mouse(el, "mouseover", x, y);
    mouse(el, "pointerdown", x, y); mouse(el, "mousedown", x, y);
    if (el.focus) el.focus({ preventScroll: true });
    mouse(el, "pointerup", x, y); mouse(el, "mouseup", x, y);
    el.click ? el.click() : mouse(el, "click", x, y, { detail: 1 });
    if (dbl) {
      el.click ? el.click() : mouse(el, "click", x, y, { detail: 2 });
      mouse(el, "dblclick", x, y, { detail: 2 });
    }
  }

  const KEYCODES = { Enter: 13, Escape: 27, Tab: 9, Backspace: 8, Delete: 46, " ": 32,
    ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Home: 36, End: 35, PageUp: 33, PageDown: 34 };

  function key(el, k) {
    const code = k.length === 1 ? (/[a-z]/i.test(k) ? "Key" + k.toUpperCase() : /\d/.test(k) ? "Digit" + k : "") : k;
    const keyCode = KEYCODES[k] ?? (k.length === 1 ? k.toUpperCase().charCodeAt(0) : 0);
    const init = { key: k, code, keyCode, which: keyCode, bubbles: true, cancelable: true, composed: true };
    const down = el.dispatchEvent(new KeyboardEvent("keydown", init));
    if (down && (k.length === 1 || k === "Enter")) el.dispatchEvent(new KeyboardEvent("keypress", init));
    let effect = null;
    if (down) {
      if (k === "Enter" && el.form && el.tagName === "INPUT") { el.form.requestSubmit(); effect = "submitted form"; }
      else if (k === "Enter" && el.tagName !== "TEXTAREA" && !el.isContentEditable && el.click && role(el) === "button") { el.click(); effect = "activated"; }
      else if (k.length === 1 && editable(el)) { insert(el, k); effect = "inserted"; }
      else if (k === "Tab") { focusNext(el); effect = "moved focus"; }
    }
    el.dispatchEvent(new KeyboardEvent("keyup", init));
    return effect;
  }

  function focusNext(el) {
    const f = [...document.querySelectorAll("a[href],button,input,select,textarea,[tabindex]:not([tabindex='-1']),[contenteditable=true]")]
      .filter((e) => !e.disabled && visible(e));
    const i = f.indexOf(el);
    f[(i + 1) % f.length]?.focus();
  }

  const editable = (el) => el.isContentEditable || (el.tagName === "TEXTAREA") ||
    (el.tagName === "INPUT" && !["checkbox", "radio", "button", "submit", "reset", "file", "image", "range", "color"].includes(el.type));

  function insert(el, text) {
    if (document.execCommand("insertText", false, text)) return;
    if (el.isContentEditable) { el.append(text); }
    else {
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, el.value + text);
    }
    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function scrollableAtCenter() {
    let el = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
    while (el && el !== document.body) {
      const cs = getComputedStyle(el);
      if (/(auto|scroll)/.test(cs.overflowY) && el.scrollHeight > el.clientHeight + 4) return el;
      el = el.parentElement;
    }
    return null;
  }

  const ops = {
    viewport: () => ({ width: innerWidth, height: innerHeight, dpr: devicePixelRatio, scrollY }),

    // Returns this frame's outline plus where each child iframe sits in it; background.js
    // stitches child frames' outlines in under their iframe lines.
    snapshot({ max_items = 300 }) {
      const els = deepAll(`${HEADINGS},${INTERACTIVE},iframe,frame`).filter(visible);
      const lines = [];
      const frames = [];
      for (const el of els) {
        if (lines.length >= max_items) { lines.push(`… ${els.length - max_items} more (raise max_items or use find)`); break; }
        if (isFrame(el)) {
          lines.push(describe(el));
          frames.push({ after: lines.length - 1, frameId: frameIdOf(el), src: el.getAttribute("src") || "" });
          continue;
        }
        // skip elements nested inside another interactive element we already listed (e.g. span[tabindex] in a button)
        const parent = el.parentElement?.closest(INTERACTIVE);
        if (parent && refOfEl.has(parent) && !el.matches("input,select,textarea")) continue;
        lines.push(describe(el));
      }
      const head = `${document.title}\n${location.href}\nviewport ${innerWidth}x${innerHeight}, scrolled ${Math.round(scrollY)}/${Math.max(0, document.documentElement.scrollHeight - innerHeight)}`;
      return { head, lines, frames };
    },

    get_text(a) {
      const el = resolve(a, { optional: true }) || document.body;
      const t = el.innerText || el.textContent || "";
      const max = a.max_chars ?? 20000;
      return { text: t.length > max ? t.slice(0, max) + `\n… [truncated ${t.length - max} chars]` : t };
    },

    find({ query, css = false, limit = 20 }) {
      const hits = css ? deepAll(query) : byText(query);
      const lines = hits.slice(0, limit).map(describe);
      return { lines, more: Math.max(0, hits.length - limit) };
    },

    click(a) {
      if (a.x != null && a.y != null && !a.ref && !a.selector && !a.text) {
        const el = deepElementFromPoint(a.x, a.y);
        if (!el) throw new Error(`nothing at (${a.x}, ${a.y})`);
        if (isFrame(el)) {
          // Hand the click to the frame, in the frame's own viewport coordinates.
          const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
          return { descend: { frameId: frameIdOf(el),
            x: a.x - r.left - el.clientLeft - parseFloat(cs.paddingLeft || 0),
            y: a.y - r.top - el.clientTop - parseFloat(cs.paddingTop || 0) } };
        }
        click(el, a.x, a.y, a.double);
        return { clicked: describe(el.closest(INTERACTIVE) || el), at: [a.x, a.y] };
      }
      const el = resolve(a);
      const { x, y } = center(el);
      click(el, x, y, a.double);
      return { clicked: describe(el) };
    },

    type(a) {
      const el = resolve(a, { optional: true }) || deepActive();
      if (!el || el === document.body) throw new Error("no target and nothing is focused");
      if (isFrame(el) && !a.ref && !a.selector) return { focusIn: frameIdOf(el) };
      if (el.tagName === "INPUT" && el.type === "password")
        throw new Error("Refusing to type into a password field. Ask the user to enter it themselves.");
      if (!editable(el)) throw new Error(`${describe(el)} is not a text field`);
      center(el);
      el.focus();
      if (a.clear !== false) {
        if (el.isContentEditable) {
          const range = document.createRange(); range.selectNodeContents(el);
          const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
        } else el.select?.();
        if (!document.execCommand("delete")) {
          if (el.isContentEditable) el.textContent = "";
          else Object.getOwnPropertyDescriptor(el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, "value").set.call(el, "");
        }
      }
      insert(el, a.text);
      if (!el.isContentEditable) el.dispatchEvent(new Event("change", { bubbles: true }));
      let submitted = null;
      if (a.submit) submitted = key(el, "Enter") || "pressed Enter";
      return { typed_into: describe(el), submitted };
    },

    press_key(a) {
      const el = resolve(a, { optional: true }) || deepActive() || document.body;
      if (isFrame(el) && !a.ref && !a.selector) return { focusIn: frameIdOf(el) };
      if (a.ref || a.selector) el.focus?.();
      return { key: a.key, target: describe(el), effect: key(el, a.key), note: "synthetic event (isTrusted=false)" };
    },

    select_option(a) {
      const el = resolve(a);
      if (el.tagName !== "SELECT") throw new Error(`${describe(el)} is not a <select>`);
      const want = a.option.trim().toLowerCase();
      const opt = [...el.options].find((o) => o.value.toLowerCase() === want) ||
                  [...el.options].find((o) => o.text.trim().toLowerCase() === want) ||
                  [...el.options].find((o) => o.text.toLowerCase().includes(want));
      if (!opt) throw new Error(`no option "${a.option}"; options: ${[...el.options].map((o) => o.text.trim()).join(" | ")}`);
      el.value = opt.value;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { selected: opt.text.trim(), value: opt.value };
    },

    hover(a) {
      const el = resolve(a);
      const { x, y } = center(el);
      for (const t of ["pointerover", "pointerenter", "mouseover", "mouseenter", "pointermove", "mousemove"]) mouse(el, t, x, y);
      return { hovered: describe(el) };
    },

    scroll(a) {
      if (a.ref || a.selector || a.text) { const el = resolve(a); center(el); return { scrolled_to: describe(el) }; }
      const before = scrollY;
      if (a.to === "top") scrollTo(0, 0);
      else if (a.to === "bottom") scrollTo(0, document.documentElement.scrollHeight);
      else window.scrollBy(0, a.dy ?? innerHeight * 0.8);
      if (scrollY === before && a.to == null) {
        const inner = scrollableAtCenter();
        if (inner) { inner.scrollBy(0, a.dy ?? inner.clientHeight * 0.8); return { container: describe(inner), scrollTop: inner.scrollTop }; }
      }
      return { scrollY: Math.round(scrollY), max: Math.max(0, document.documentElement.scrollHeight - innerHeight) };
    },

    async wait_for(a) {
      if (!a.selector && !a.text) throw new Error("give a selector or text");
      const deadline = Date.now() + (a.timeout_ms ?? 10000);
      const check = () => {
        if (a.selector) { const el = deepQuery(a.selector); return el && visible(el) ? el : null; }
        if (document.body.innerText.toLowerCase().includes(a.text.toLowerCase())) return byText(a.text)[0] || document.body;
        return byText(a.text)[0] || null; // text inside shadow roots isn't in body.innerText
      };
      const t0 = Date.now();
      while (Date.now() < deadline) {
        const hit = check();
        if (a.gone ? !hit : hit) return { ok: true, waited_ms: Date.now() - t0, element: hit && hit !== document.body ? describe(hit) : null };
        await sleep(200);
      }
      throw new Error(`timed out after ${a.timeout_ms ?? 10000}ms waiting for ${a.selector || `"${a.text}"`}${a.gone ? " to disappear" : ""}`);
    },
  };

  window.__cbbAgent = async (op, args) => {
    PREFIX = args?.prefix || "";
    try {
      if (!ops[op]) throw new Error(`unknown op ${op}`);
      return { ok: true, value: await ops[op](args || {}) };
    } catch (e) {
      return { ok: false, error: String(e?.message || e) };
    }
  };
})();
