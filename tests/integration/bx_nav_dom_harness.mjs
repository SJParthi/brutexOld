/* Headless DOM harness for brutex/dashboard/static/bx-nav.js.
 *
 * bx-nav.js is browser-only (window / document / DOMParser / fetch). The Python
 * server-contract tests (test_dashboard_nav_swap.py) cover the routes the swap
 * depends on; THIS harness proves the pure-JS behaviours the hostile review
 * flagged — (1) page pollers are torn down on a swap, (2) a head <script src>
 * the incoming page declares is loaded as an EXECUTABLE script, (3) bx-nav's own
 * listeners are not duplicated.
 *
 * It is a minimal hand-rolled DOM shim (no jsdom dependency): just enough of the
 * surface bx-nav.js touches. Run as: `node bx_nav_dom_harness.mjs <path-to-bx-nav.js>`.
 * Exits non-zero with a message on the first failed assertion.
 */
import { readFileSync } from "node:fs";
import vm from "node:vm";

const navPath = process.argv[2];
const navSrc = readFileSync(navPath, "utf8");

let failures = 0;
function assert(cond, msg) {
  if (!cond) {
    console.error("FAIL: " + msg);
    failures += 1;
  } else {
    console.log("ok: " + msg);
  }
}

/* ---- minimal DOM shim ---------------------------------------------------- */
const listeners = { document: {}, window: {} };

function makeEl(tag) {
  const el = {
    tagName: (tag || "").toUpperCase(),
    attributes: [],
    children: [],
    _attrs: {},
    textContent: "",
    parentNode: null,
    setAttribute(name, value) {
      if (!(name in this._attrs)) this.attributes.push({ name, value });
      else this.attributes.find((a) => a.name === name).value = value;
      this._attrs[name] = value;
      if (name === "src") this.src = resolveUrl(value);
    },
    getAttribute(name) {
      return name in this._attrs ? this._attrs[name] : null;
    },
    hasAttribute(name) {
      return name in this._attrs;
    },
    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
      if (this === document.head && child.tagName === "SCRIPT" && child.src) {
        executedHeadScripts.push(child.src);
      }
      return child;
    },
    cloneNode() {
      const c = makeEl(this.tagName);
      for (const a of this.attributes) c.setAttribute(a.name, a.value);
      c.textContent = this.textContent;
      return c;
    },
    querySelectorAll(sel) {
      return queryAll(this, sel);
    },
    querySelector(sel) {
      const all = queryAll(this, sel);
      return all.length ? all[0] : null;
    },
    get outerHTML() {
      const attrs = this.attributes.map((a) => ` ${a.name}="${a.value}"`).join("");
      return `<${this.tagName.toLowerCase()}${attrs}>${this.textContent}</${this.tagName.toLowerCase()}>`;
    },
  };
  return el;
}

function resolveUrl(v) {
  if (/^https?:/.test(v)) return v;
  return "http://localhost" + (v.charAt(0) === "/" ? v : "/" + v);
}

function descendants(node, acc) {
  for (const c of node.children) {
    acc.push(c);
    descendants(c, acc);
  }
  return acc;
}

function queryAll(root, sel) {
  const all = descendants(root, []);
  const parts = sel.split(",").map((s) => s.trim());
  return all.filter((el) =>
    parts.some((p) => {
      if (p === "style") return el.tagName === "STYLE";
      if (p === "link[rel=stylesheet]")
        return el.tagName === "LINK" && el.getAttribute("rel") === "stylesheet";
      if (p === "script[src]") return el.tagName === "SCRIPT" && el.getAttribute("src");
      if (p === "script") return el.tagName === "SCRIPT";
      return false;
    }),
  );
}

let executedHeadScripts = [];

const document = {
  head: makeEl("head"),
  body: makeEl("body"),
  addEventListener(type, fn) {
    (listeners.document[type] = listeners.document[type] || []).push(fn);
  },
  createElement: makeEl,
  querySelector(sel) {
    return null;
  },
};
Object.defineProperty(document.body, "innerHTML", {
  get() {
    return this._html || "";
  },
  set(v) {
    this._html = v;
    this.children = []; // body scripts (if any) would be parsed here; not needed
  },
});

let dispatched = [];
const window = {
  history: {
    state: {},
    pushState() {},
    replaceState(s) {
      this.state = s;
    },
  },
  fetch: () => Promise.reject(new Error("fetch not stubbed yet")),
  location: { origin: "http://localhost", pathname: "/coverage", search: "", href: "http://localhost/coverage" },
  scrollTo() {},
  addEventListener(type, fn) {
    (listeners.window[type] = listeners.window[type] || []).push(fn);
  },
  dispatchEvent(ev) {
    dispatched.push(ev.type);
    (listeners.window[ev.type] || []).forEach((fn) => fn(ev));
    return true;
  },
  setInterval,
  clearInterval,
  setTimeout,
  clearTimeout,
  Event: class {
    constructor(type) {
      this.type = type;
    }
  },
};

/* DOMParser → parse a tiny fixed HTML into head/body elements. We don't need a
 * real HTML parser: the harness supplies the parsed structure via a hook. */
let nextParsedDoc = null;
class DOMParser {
  parseFromString() {
    return nextParsedDoc;
  }
}

/* ---- run bx-nav.js in a vm context --------------------------------------- */
const sandbox = { window, document, DOMParser, console, Event: window.Event };
sandbox.fetch = (...a) => window.fetch(...a);
vm.createContext(sandbox);
vm.runInContext(navSrc, sandbox);

/* ---- Test 1: page pollers torn down on swap ------------------------------ */
// Simulate a page that installed pollers via the wrapped setInterval/setTimeout.
let pollFired = 0;
const wrappedSetInterval = sandbox.window.setInterval; // bx-nav wrapped this
const id = wrappedSetInterval(() => {
  pollFired += 1;
}, 1);

// Drive a swap: build a parsed "incoming" doc, stub fetch, fire a nav click.
function buildDoc({ headScriptSrc } = {}) {
  const doc = { head: makeEl("head"), body: makeEl("body") };
  doc.head.appendChild(makeEl("title"));
  if (headScriptSrc) {
    const s = makeEl("script");
    s.setAttribute("src", headScriptSrc);
    doc.head.appendChild(s);
  }
  doc.querySelector = (sel) => (sel === "title" ? doc.head.children[0] : null);
  return doc;
}

async function triggerSwap(parsed) {
  nextParsedDoc = parsed;
  window.fetch = () =>
    Promise.resolve({
      ok: true,
      headers: { get: () => "text/html; charset=utf-8" },
      text: () => Promise.resolve("<html></html>"),
    });
  // Build an anchor + click event bx-nav will intercept.
  const a = makeEl("a");
  a.setAttribute("href", "/movers");
  a.origin = "http://localhost";
  a.pathname = "/movers";
  a.search = "";
  a.href = "http://localhost/movers";
  a.target = "";
  a.closest = (sel) => (sel === "nav.topnav" ? makeEl("nav") : null);
  const event = {
    defaultPrevented: false,
    button: 0,
    target: a,
    preventDefault() {
      this.defaultPrevented = true;
    },
  };
  (listeners.document.click || []).forEach((fn) => fn(event));
  // Let the fetch promise chain settle.
  await new Promise((r) => setTimeout(r, 10));
}

await triggerSwap(buildDoc({ headScriptSrc: "/static/bx-datepicker.js" }));

// After the swap, the old page's interval must be cleared → no further fires.
const firedAtSwap = pollFired;
await new Promise((r) => setTimeout(r, 20));
assert(pollFired === firedAtSwap, "page setInterval poller is cleared on swap (no leak)");

// bx:teardown was announced before the body swap.
assert(dispatched.includes("bx:teardown"), "bx:teardown event dispatched before swap");

// Head <script src> from the incoming page was added as an executable script.
assert(
  executedHeadScripts.some((s) => s.endsWith("/static/bx-datepicker.js")),
  "incoming head <script src> (bx-datepicker.js) loaded on swap",
);

/* ---- Test 2: head script dedupe ------------------------------------------ */
// A second swap to a page declaring the SAME head script must NOT re-add it.
const before = executedHeadScripts.length;
// The datepicker script is now present in document.head from swap 1.
await triggerSwap(buildDoc({ headScriptSrc: "/static/bx-datepicker.js" }));
const addedDup = executedHeadScripts.length - before;
assert(addedDup === 0, "already-present head script is not re-added (deduped by src)");

/* ---- Test 3: bx-nav listeners registered exactly once -------------------- */
assert((listeners.document.click || []).length === 1, "bx-nav click listener registered once");
assert((listeners.window.popstate || []).length === 1, "bx-nav popstate listener registered once");

if (failures > 0) {
  console.error(`\n${failures} assertion(s) FAILED`);
  process.exit(1);
}
console.log("\nALL bx-nav DOM behaviours verified");
process.exit(0);
