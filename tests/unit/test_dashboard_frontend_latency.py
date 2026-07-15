"""Frontend latency-hardening pins (DEC-FRONTEND-LATENCY-HARDENING-001, MIGRATION.md #1720).

The pack consolidates the v29 operator page's DUPLICATE pollers onto one
BXPoll-disciplined consumer per endpoint (measured BEFORE on origin/main
@ a268bd4c, real headless Chromium, idle operator page, 120 s visible:
336 requests — sweep-progress 99 = the 2 s inline poller + bx-oneclick's 3 s
poller SUMMED, pull/rate 63 = the 5 s heartbeat poller + bx-rate-monitor's
3 s widget SUMMED, liveproof 36 = the 10 s heartbeat poller + bx-liveproof's
5 s widget SUMMED), migrates the six fixed-interval template pollers onto the
ONE shared ``BXPoll.createPoller`` core, debounces the /screener + /results
inputs, adopts the in-place tbody row-patch idiom on the two remaining
full-table innerHTML rebuild pages, and makes /reports' ~750 chips build ONCE
with class-toggle selection.

HONEST envelope (Golden Rule #10):

* SOURCE-STRING pins + ``node --check`` syntax gates — the repo has no
  headless JS runner in CI, so template/widget behaviour is asserted by
  structure, never executed here (the house #1529/#1561 envelope). The
  BEFORE numbers above were measured OUT of CI by the coordinator's
  browser-measurement harness; the AFTER run happens on the pushed SHA.
* The hidden-tab state was ALREADY 0 req/s on the idle operator page
  (measured) — this pack claims NO measured hidden-tab win. The one
  source-asserted hidden-guard gap it closes is ``pollRunSweep`` (armed only
  while a sweep runs, so unmeasured), which had no ``document.hidden`` guard,
  no backoff and a bare un-timed fetch; it is now a subscriber on the shared
  guarded poller.
* Fix 4 (defer on bx-format.js / bx-movers-time.js) was deliberately
  SKIPPED: those includes are end-of-BODY (not render-blocking head
  scripts), movers.html has genuine parse-time consumers
  (``fillTimes()`` → ``BXMoversTime`` at inline-execution time; ``load()``'s
  fetch callback can race a deferred bx-format.js), and the non-defer choice
  is an explicit standing pin
  (test_dashboard_halfup_format.py::test_template_loads_shared_formatter_before_inline_script)
  whose intent stands.

DEC-O1-GUARANTEE-HARDEN-001 additions (this suite also hosts the widened
bounds): the #1720 body-armed tick bound is extended from 2 to ALL FIVE
sibling poller templates through the NEW shared ``BXUI.fetchTextBounded``
(node-EXECUTED stalled-body golden — not just a source pin); the bx-oneclick
timeline gains a skip-render token + a NAMED newest-500 render cap + a 200 ms
search debounce (pure primitives node-EXECUTED); universe.html's windows line
+ six integrity loaders ride the gbFetchJson bound with a per-loader
latest-claims-the-paint generation guard.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from html.parser import HTMLParser
from pathlib import Path

import pytest

_DASHBOARD = Path(__file__).resolve().parents[2] / "brutex" / "dashboard"
_TEMPLATES = _DASHBOARD / "templates"
_STATIC = _DASHBOARD / "static"

_NODE = shutil.which("node")


def _tpl(name: str) -> str:
    text = (_TEMPLATES / name).read_text(encoding="utf-8")
    assert text, f"expected a non-empty template at {name}"
    return text


def _js(name: str) -> str:
    text = (_STATIC / name).read_text(encoding="utf-8")
    assert text, f"expected a non-empty static asset at {name}"
    return text


# ---------------------------------------------------------------------------
# node --check syntax gate: every shipped static JS file must parse.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_NODE is None, reason="node not installed")
@pytest.mark.parametrize("name", sorted(p.name for p in _STATIC.glob("*.js")))
def test_static_js_parses_under_node_check(name: str) -> None:
    """``node --check`` on every static JS file (the touched three included)."""
    proc = subprocess.run(  # nosec B603 — fixed argv, test-only
        [str(_NODE), "--check", str(_STATIC / name)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"{name} failed node --check: {proc.stderr}"


# ---------------------------------------------------------------------------
# Fix 1 — v29 duplicate pollers consolidated (ONE poller per endpoint).
# ---------------------------------------------------------------------------


class TestV29PollerConsolidation:
    """The killed-token pins: the three duplicate/triplicate pollers are GONE."""

    def test_killed_duplicate_poller_tokens_are_absent(self) -> None:
        html = _tpl("v29.html")
        for killed in (
            "setInterval(pollSweepProgress, 2000)",  # duplicate of bx-oneclick's 3 s poller
            "sweepPoll = setInterval(pollRunSweep, 1200)",  # the THIRD sweep-progress poller
            "setInterval(pollPull, 5000)",  # duplicate of bx-rate-monitor's 3 s widget
            "setInterval(pollProof, 10000)",  # duplicate of bx-liveproof's 5 s widget
            "setInterval(pollState, 2000)",  # bare interval → BXPoll (single endpoint)
            "async function pollSweepProgress()",
            "async function pollRunSweep()",
            "async function pollPull()",
            "async function pollProof()",
        ):
            assert killed not in html, f"v29.html: killed poller token resurfaced: {killed!r}"

    def test_data_sweep_panel_subscribes_to_the_shared_poller(self) -> None:
        html = _tpl("v29.html")
        assert (
            "(window.__bxSweepProgressSubs = window.__bxSweepProgressSubs || []).push(renderSweepProgress);"
            in html
        )
        # The heartbeat error hook rides the failure fan-out.
        assert "window.__bxSweepProgressFailSubs" in html

    def test_run_controls_subscribe_and_use_the_shared_pollnow(self) -> None:
        """The Run click flips `watching` + fires ONE shared pollNow — no timer."""
        html = _tpl("v29.html")
        assert "let watching = false;" in html
        assert (
            "(window.__bxSweepProgressSubs = window.__bxSweepProgressSubs || []).push(onRunSweepProgress);"
            in html
        )
        assert "if (window.__bxSweepProgressPollNow) window.__bxSweepProgressPollNow();" in html
        # The terminal snapshot still re-enables the button + reports survivors.
        assert "watching = false; sweepBtn.disabled = false;" in html
        assert "'✅ Sweep ' + p.state + ' — ' + (p.survivors || 0)" in html

    def test_heartbeat_slots_subscribe_to_the_widget_polls(self) -> None:
        """The heartbeat slots subscribe to the widgets' own polls.

        Semantics note (review NIT-3, ledgered in MIGRATION.md #1720): the
        ``__bxSweepProgressFailSubs`` fan-out passes the consecutive-failure
        COUNT (bx-oneclick's ``failures`` counter), not an HTTP status/error
        like the old inline hook — the heartbeat error message is static, so
        the argument is informational only. Recorded here + in the ledger
        instead of a v29.html comment: v29.html deliberately stays
        byte-identical to the measured AFTER SHA (31e9022c).
        """
        html = _tpl("v29.html")
        assert (
            "(window.__bxPullRateSubs = window.__bxPullRateSubs || []).push(renderHbPull);" in html
        )
        assert (
            "(window.__bxLiveproofSubs = window.__bxLiveproofSubs || []).push(renderHbProof);"
            in html
        )
        # The honest "unreachable — retrying" copy survives on the fail arms.
        assert "rate feed unreachable — retrying" in html
        assert "liveproof feed unreachable — retrying" in html

    def test_state_poller_rides_bxpoll_with_the_2s_cadence(self) -> None:
        html = _tpl("v29.html")
        assert "url: '/api/v1/state'," in html
        assert "pollMs: 2000," in html
        assert "onData: renderStateSnap" in html


class TestSharedPollerFanOuts:
    """The additive fan-out hooks in the three shared JS files."""

    def test_bx_oneclick_fans_progress_out_to_page_subscribers(self) -> None:
        js = _js("bx-oneclick.js")
        assert 'notifyPageSubs("__bxSweepProgressSubs", data);' in js
        assert 'notifyPageSubs("__bxSweepProgressFailSubs", failures);' in js
        assert "window.__bxSweepProgressPollNow = function () {" in js
        assert "progressPoller.pollNow();" in js

    def test_bx_rate_monitor_fans_pull_rate_out_to_page_subscribers(self) -> None:
        js = _js("bx-rate-monitor.js")
        assert 'notifyPageSubs("__bxPullRateSubs", data);' in js
        assert 'notifyPageSubs("__bxPullRateFailSubs", pollFailures);' in js

    def test_bx_liveproof_fans_liveproof_out_to_page_subscribers(self) -> None:
        js = _js("bx-liveproof.js")
        assert 'notifyPageSubs("__bxLiveproofSubs", data);' in js
        assert 'notifyPageSubs("__bxLiveproofFailSubs", pollFailures);' in js

    @pytest.mark.parametrize("name", ["bx-oneclick.js", "bx-rate-monitor.js", "bx-liveproof.js"])
    def test_fan_out_isolates_a_throwing_subscriber(self, name: str) -> None:
        """One bad subscriber can never break the widget's poll chain."""
        js = _js(name)
        assert "subscriber isolation" in js
        assert "try {" in js

    @pytest.mark.skipif(_NODE is None, reason="node not installed")
    def test_notify_page_subs_isolation_golden(self) -> None:
        """Node-executed golden for the fan-out core: call order + isolation.

        The helper is IIFE-local in all three files (bx-rate-monitor /
        bx-liveproof are deliberately dependency-free), so the golden executes
        the extracted canonical body against a fake window: a THROWING first
        subscriber must not stop the second, and a missing/empty list is a
        no-op.
        """
        script = r"""
        const window = {};
        function notifyPageSubs(listName, arg) {
          var subs = window[listName];
          if (!subs || !subs.length) { return; }
          for (var i = 0; i < subs.length; i++) {
            try { subs[i](arg); } catch (e) { /* subscriber isolation */ }
          }
        }
        const calls = [];
        notifyPageSubs("__missing", 1);  // no list → no-op, no throw
        window.__t = [];
        notifyPageSubs("__t", 2);  // empty list → no-op
        window.__t.push(function () { calls.push("boom"); throw new Error("boom"); });
        window.__t.push(function (v) { calls.push("ok:" + v); });
        notifyPageSubs("__t", 42);
        console.log(JSON.stringify(calls));
        """
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            [str(_NODE), "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == '["boom","ok:42"]'


# ---------------------------------------------------------------------------
# Fix 2 — launch.html (the sixth migrated poller; the uniform five are pinned
# in test_dashboard_poll_hygiene.py).
# ---------------------------------------------------------------------------


class TestLaunchPoller:
    def test_launch_rides_bxpoll_with_the_2500ms_cadence(self) -> None:
        html = _tpl("launch.html")
        assert '<script defer src="/static/bx-poll.js"></script>' in html
        assert "window.BXPoll.createPoller({" in html
        assert "var POLL_MS = 2500;" in html  # cadence value unchanged
        assert "pollMs: POLL_MS," in html
        # The killed bare-interval scheduling.
        assert (
            "window.setInterval(function () { if (!document.hidden) { poll(); } }, POLL_MS);"
            not in html
        )

    def test_launch_pollnote_semantics_survive(self) -> None:
        html = _tpl("launch.html")
        assert "retrying; state may be STALE" in html
        assert "function onPollFail(count)" in html
        # The post-launch refresh goes through the shared poller.
        assert "launching = false; repoll();" in html


# ---------------------------------------------------------------------------
# Fix 3 — debounced inputs (screener + results).
# ---------------------------------------------------------------------------


class TestDebouncedInputs:
    def test_screener_filter_inputs_are_debounced(self) -> None:
        html = _tpl("screener.html")
        assert "function renderDebounced()" in html
        assert "setTimeout(render, 200)" in html
        assert "byId(id).addEventListener('input', renderDebounced);" in html
        # The per-keystroke direct bind is gone.
        assert "byId(id).addEventListener('input', render);" not in html
        # The sort/th-click path stays synchronous (a single discrete action).
        assert "render();" in html

    def test_results_top_n_input_is_debounced(self) -> None:
        html = _tpl("results.html")
        assert "const FTOP_DEBOUNCE_MS = 200;" in html
        assert "fTopDebounceTimer = setTimeout(load, FTOP_DEBOUNCE_MS);" in html
        assert "fTop.onchange = load;" not in html
        # The selects keep the immediate load (nothing to coalesce) — the
        # array grew fGate with DEC-SWEEP-SERVING-GATE-PROVENANCE-001.
        assert (
            "[fInstrument, fTf, fDirection, fGate, fRankBy]"
            ".forEach((el) => { el.onchange = load; });" in html
        )


# ---------------------------------------------------------------------------
# Review MAJOR fix (#1720), widened by DEC-O1-GUARANTEE-HARDEN-001 to ALL FIVE
# sibling poller templates — every tick fetch is BOUNDED with the abort timer
# armed THROUGH the body read, honoring the timeoutMs BXPoll passes to the
# custom fetchJson seam (pre-fix the ticks kept a headers-only bound /
# unbounded `await r.text()`, so under BXPoll's one-in-flight scheduling a
# headers-then-stalled-body response hung the tick promise forever and the
# poller never rescheduled — the frozen-tab class; the #1720 fix covered only
# fetch_windows/completeness, and THIS parametrize list's former 2-template
# narrowness was the fingerprint of the gap).
# ---------------------------------------------------------------------------

#: The two #1720 templates keep their SELF-CONTAINED local fetchTextBounded —
#: they deliberately do NOT load bx-ui.js, so migrating them onto the shared
#: BXUI helper would add a new script dependency for zero behavioral win
#: (the documented judgment call, DEC-O1-GUARANTEE-HARDEN-001).
_LOCAL_BOUND_TEMPLATES = ["fetch_windows.html", "completeness.html"]
#: The three gap templates already depended on BXUI for their fetch helper
#: (the former headers-only doFetch → BXUI.fetchWithTimeout), so they now
#: delegate to the ONE shared BXUI.fetchTextBounded (§11 COMMON).
_SHARED_BOUND_TEMPLATES = ["coverage.html", "spot_coverage.html", "expired_options.html"]


class TestBoundedTickFetch:
    @pytest.mark.parametrize("name", _LOCAL_BOUND_TEMPLATES + _SHARED_BOUND_TEMPLATES)
    def test_tick_fetch_is_bounded_through_the_body_read(self, name: str) -> None:
        html = _tpl(name)
        # The bounded-fetch helper + its tick consumption.
        assert "function fetchTextBounded(url, timeoutMs) {" in html
        assert "await fetchTextBounded(" in html
        # The custom seam threads BXPoll's own timeout bound into the tick.
        assert "fetchJson: function (_url, timeoutMs) {" in html
        assert "return poll(timeoutMs).then(function (ok) {" in html

    @pytest.mark.parametrize("name", _LOCAL_BOUND_TEMPLATES)
    def test_local_copy_keeps_the_armed_through_body_token(self, name: str) -> None:
        html = _tpl(name)
        # The bounded-fetch token: timer armed through the BODY read.
        assert "/* timer deliberately still armed through the body read */" in html
        # The legacy no-BXPoll arm defaults to the sibling pages' 12 s bound.
        assert '(typeof timeoutMs === "number" && timeoutMs > 0) ? timeoutMs : 12000;' in html

    @pytest.mark.parametrize("name", _SHARED_BOUND_TEMPLATES)
    def test_gap_templates_delegate_to_the_shared_bxui_bound(self, name: str) -> None:
        """The three gap templates ride the ONE shared BXUI.fetchTextBounded."""
        html = _tpl(name)
        assert "return BXUI.fetchTextBounded(url, timeoutMs);" in html
        # KILLED tokens: the headers-only bound + the unbounded body reads.
        assert "fetchWithTimeout" not in html, (
            f"{name}: the headers-only fetchWithTimeout bound resurfaced"
        )
        assert "function doFetch(url)" not in html
        assert "await r.text()" not in html, (
            f"{name}: an unbounded post-headers body read resurfaced"
        )
        # The tick consumes the bounded {ok, status, text} shape.
        assert "const txt = res.text;" in html

    def test_expired_options_second_read_is_bounded_too(self) -> None:
        """The best-effort pull-progress read rides the SAME bounded helper."""
        html = _tpl("expired_options.html")
        assert "await fetchTextBounded('/api/v1/universe/pull-progress', timeoutMs)" in html
        assert "await pr.json()" not in html

    def test_bare_untimed_tick_fetches_are_gone(self) -> None:
        assert 'await fetch("/api/v1/completeness/scoreboard")' not in _tpl("completeness.html")
        assert 'await fetch("/api/v1/coverage/windows?"' not in _tpl("fetch_windows.html")
        for name in _SHARED_BOUND_TEMPLATES:
            assert "doFetch(" not in _tpl(name)


# ---------------------------------------------------------------------------
# DEC-O1-GUARANTEE-HARDEN-001 — the ONE shared body-armed bound on BXUI
# (bx-ui.js), source-pinned AND node-EXECUTED (a real stalled-body abort).
# ---------------------------------------------------------------------------


class TestSharedFetchTextBounded:
    def test_bx_ui_ships_the_body_armed_helper(self) -> None:
        js = _js("bx-ui.js")
        assert "function fetchTextBounded(url, ms) {" in js
        assert "/* timer deliberately still armed through the body read */" in js
        # Exported on the BXUI namespace for the template delegators.
        assert "fetchTextBounded: fetchTextBounded," in js
        # The 12 s default (the sibling pages' legacy bound) lives HERE now.
        assert '(typeof ms === "number" && ms > 0) ? ms : 12000;' in js
        # Timeout keeps the fetchWithTimeout error shape (coverage.html's
        # failMsg + spot_coverage's timed-out verdict read e.timeout).
        assert 'e.name = "TimeoutError";' in js
        assert "e.timeout = true;" in js

    @pytest.mark.skipif(_NODE is None, reason="node not installed")
    def test_fetch_text_bounded_aborts_a_stalled_body_read(self) -> None:
        """EXECUTED golden over the REAL shipped bx-ui.js: the happy path
        resolves {ok, status, text}, and a headers-then-STALLED-BODY response
        rejects with the TimeoutError{timeout:true} shape within the bound —
        the exact wedge class fetchWithTimeout (timer cleared at the headers)
        could never catch."""
        script = (
            "const fs = require('fs');\n"
            f"const src = fs.readFileSync({json.dumps(str(_STATIC / 'bx-ui.js'))}, 'utf8');\n"
            "global.window = {};\n"
            "class FakeAbortController {\n"
            "  constructor() { this.signal = { aborted: false, onabort: null }; }\n"
            "  abort() { this.signal.aborted = true; if (this.signal.onabort) this.signal.onabort(); }\n"
            "}\n"
            "global.AbortController = FakeAbortController;\n"
            "let mode = 'ok';\n"
            "global.fetch = function (url, init) {\n"
            "  if (mode === 'ok') {\n"
            "    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('BODY:' + url) });\n"
            "  }\n"
            "  // headers arrive fine; the BODY stalls forever unless aborted\n"
            "  return Promise.resolve({ ok: true, status: 200, text: () => new Promise((resolve, reject) => {\n"
            "    init.signal.onabort = () => reject(new Error('aborted'));\n"
            "  }) });\n"
            "};\n"
            "eval(src);\n"
            "const B = global.window.BXUI;\n"
            "(async () => {\n"
            "  const okRes = await B.fetchTextBounded('/x', 50);\n"
            "  if (!(okRes.ok === true && okRes.status === 200 && okRes.text === 'BODY:/x')) {\n"
            "    throw new Error('happy-path shape wrong: ' + JSON.stringify(okRes));\n"
            "  }\n"
            "  mode = 'stall';\n"
            "  const t0 = Date.now();\n"
            "  let caught = null;\n"
            "  try {\n"
            "    await B.fetchTextBounded('/y', 40);\n"
            "  } catch (e) { caught = e; }\n"
            "  if (!caught) { throw new Error('stalled body did NOT abort'); }\n"
            "  if (!(caught.timeout === true && caught.name === 'TimeoutError')) {\n"
            "    throw new Error('wrong error shape: ' + caught);\n"
            "  }\n"
            "  if (Date.now() - t0 > 5000) { throw new Error('abort exceeded the bound'); }\n"
            "  console.log('BOUNDED_OK');\n"
            "})().catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });\n"
        )
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            [str(_NODE), "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "BOUNDED_OK"


# ---------------------------------------------------------------------------
# Review NIT-1 fix — the "every touched template's inline <script> parses
# under node" claim gets executable teeth: extract the inline (src-less)
# script bodies and `node --check` each.
# ---------------------------------------------------------------------------

_TOUCHED_TEMPLATES = [
    "completeness.html",
    "coverage.html",
    "expired_options.html",
    "fetch_windows.html",
    "launch.html",
    "reports.html",
    "results.html",
    "screener.html",
    "spot_coverage.html",
    # DEC-PULL-SCOPED-ALL-ONECLICK-001 refuter-round-4 H NIT-1: the branch
    # added inline JS (the pull-all-spot button wiring) with no syntax gate.
    # DEC-O1-GUARANTEE-HARDEN-001 — bounded latest-wins loaders.
    "universe.html",
    "v29.html",
]

# NO tag regex here (CodeQL js/bad-tag-filter whack-a-mole: `</script\s*>`
# still missed `</script\t\n bar>` variants — HTML allows junk between the
# tag name and `>`): the stdlib ``html.parser.HTMLParser`` does the tag
# grammar natively, including the script-element CDATA content mode (script
# bodies arrive verbatim via ``handle_data``). This is a TEST-side reader of
# our own trusted templates, never a sanitizer.


class _InlineScriptExtractor(HTMLParser):
    """Collect every src-less inline ``<script>`` body."""

    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[str] = []
        self._in_inline_script = False
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._in_inline_script = all(name != "src" for name, _value in attrs)
            self._chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_inline_script:
            self.bodies.append("".join(self._chunks))
            self._in_inline_script = False
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._in_inline_script:
            self._chunks.append(data)


def _inline_script_bodies(html: str) -> list[str]:
    """Every non-empty src-less inline ``<script>`` body in ``html``."""
    parser = _InlineScriptExtractor()
    parser.feed(html)
    parser.close()
    return [body for body in parser.bodies if body.strip()]


@pytest.mark.skipif(_NODE is None, reason="node not installed")
@pytest.mark.parametrize("name", _TOUCHED_TEMPLATES)
def test_touched_template_inline_scripts_parse_under_node_check(name: str, tmp_path: Path) -> None:
    """Every src-less inline ``<script>`` block of a touched template parses."""
    blocks = _inline_script_bodies(_tpl(name))
    assert blocks, f"{name}: expected at least one inline script block"
    for i, body in enumerate(blocks):
        target = tmp_path / f"{name}.{i}.js"
        target.write_text(body, encoding="utf-8")
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            [str(_NODE), "--check", str(target)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, (
            f"{name} inline script block #{i} failed node --check: {proc.stderr}"
        )


# ---------------------------------------------------------------------------
# Fix 5 — in-place tbody row patch (completeness + spot_coverage).
# ---------------------------------------------------------------------------


class TestInPlaceTbodyPatch:
    def test_completeness_ships_the_shared_row_builders_and_patches(self) -> None:
        html = _tpl("completeness.html")
        # ONE esc()'d row builder each, shared by full rebuild AND patch.
        assert "const spotRowsHtml = (rs) => rs.map((r) =>" in html
        assert "const optRowsHtml = (rs) => rs.map((r) => {" in html
        assert 'spotPanel.querySelector("table > tbody")' in html
        assert 'optPanel.querySelector("table > tbody")' in html
        assert "spotTbody.innerHTML = spotRowsHtml(rows);" in html
        assert "optTbody.innerHTML = optRowsHtml(rows);" in html
        # The full-rebuild path writes the SAME builder strings.
        assert '"</tr></thead><tbody>" + spotRowsHtml(rows) + "</tbody></table>";' in html
        assert '"</tr></thead><tbody>" + optRowsHtml(rows) + "</tbody></table>";' in html
        # esc() discipline intact on payload values in the builders.
        assert "esc(r.symbol)" in html
        assert "esc(r.underlying)" in html

    def test_spot_coverage_ships_the_shared_row_builder_and_patch(self) -> None:
        html = _tpl("spot_coverage.html")
        assert "function spotRowsHtml(rows)" in html
        assert 'panel.querySelector("table > tbody")' in html
        assert "tbody.innerHTML = spotRowsHtml(rows);" in html
        assert '"</tr></thead><tbody>" + spotRowsHtml(rows) + "</tbody></table>";' in html
        assert "esc(r.symbol)" in html


# ---------------------------------------------------------------------------
# Fix 6 — /reports chips build ONCE; selection toggles classes in place.
# ---------------------------------------------------------------------------


class TestReportsChipsBuildOnce:
    def test_chips_are_memoised_by_symbol_set_key(self) -> None:
        html = _tpl("reports.html")
        assert "let chipBySymbol = new Map();" in html
        assert "let chipSetKey = null;" in html
        # The set key folds symbol + exchange + option_pullable.
        assert 's.symbol + "\\u001f" + s.exchange + "\\u001f" + (s.option_pullable ? 1 : 0)' in html
        assert "if (setKey === chipSetKey && chipBySymbol.size) {" in html
        assert "applySelection();" in html

    def test_apply_selection_is_a_class_sweep_not_a_rebuild(self) -> None:
        html = _tpl("reports.html")
        assert "function applySelection() {" in html
        assert 'chip.classList.toggle("on", selected.has(sym));' in html
        # Review MINOR fix: the chip map keys symbol + exchange (a dual-listed
        # NSE+BSE symbol keeps BOTH chips — a symbol-only key kept just the
        # LAST one, leaving the other chip's highlight stale on bulk-select
        # clicks); the `selected` set stays symbol-keyed (wire unchanged).
        assert 'chipBySymbol.set(s.symbol + "\\u001f" + s.exchange, chip);' in html
        assert 'const sym = key.split("\\u001f")[0];' in html
        assert "chipBySymbol.set(s.symbol, chip);" not in html
        # The XSS discipline of the build path is untouched (also pinned by
        # test_dashboard_reports_template_xss.py against the rendered page).
        assert "nameEl.textContent = s.symbol;" in html
        assert "chip.innerHTML" not in html


# ---------------------------------------------------------------------------
# Fix 4 — deliberate SKIP pins: the sync bx-format/bx-movers-time includes
# stay (see the module docstring for the reasons); a silent "optimisation"
# flipping them to defer must trip THIS test and re-derive the race analysis.
# ---------------------------------------------------------------------------


class TestDeferSkipIsDeliberate:
    @pytest.mark.parametrize("name", ["movers.html", "db.html"])
    def test_bx_format_stays_synchronous(self, name: str) -> None:
        html = _tpl(name)
        assert '<script src="/static/bx-format.js"></script>' in html
        assert '<script defer src="/static/bx-format.js"></script>' not in html

    def test_bx_movers_time_stays_synchronous(self) -> None:
        html = _tpl("movers.html")
        assert '<script src="/static/bx-movers-time.js"></script>' in html
        assert '<script defer src="/static/bx-movers-time.js"></script>' not in html
        # The parse-time consumer that makes defer unsafe.
        assert "fillTimes(); renderPills(); load();" in html


# ---------------------------------------------------------------------------
# DEC-O1-GUARANTEE-HARDEN-001 — the bx-oneclick timeline render bounds:
# (a) a skip-render token so an UNCHANGED (ring, filter) window never
#     rebuilds the DOM (pre-fix, a live sweep's events tick tore down +
#     rebuilt up to 5,000 text nodes every ~3 s even for dup-only pages);
# (b) the DOM render is capped at the NEWEST 500 rows with the truncation
#     NAMED on a visible count line (the BXScreener bxRenderCount/bxCapNote
#     anti-illusion idiom);
# (c) the timeline search input is 200 ms debounced (the #1720 house idiom —
#     it was the ONE filter input on the estate exempt from the law).
# The pure primitives are node-EXECUTED below; the DOM wiring is
# source-pinned (the house #1557 envelope — no browser runner in CI).
# ---------------------------------------------------------------------------


class TestTimelineRenderBounds:
    def test_render_cap_constant_and_capped_walk(self) -> None:
        js = _js("bx-oneclick.js")
        assert "var BX_TIMELINE_RENDER_CAP = 500;" in js
        assert (
            "var visible = bxTimelineRenderRows(rows, BX_TIMELINE_RENDER_CAP, state.renderOffset);"
            in js
        )
        # The truncation is NAMED on a visible line (textContent via BXUI.el).
        assert "bxTimelineCapNote(visible.length, rows.length, state.renderOffset)" in js
        assert 'window.BXUI.el("div", "oc-event-cap", capNote)' in js

    def test_skip_render_token_gates_the_rebuild(self) -> None:
        js = _js("bx-oneclick.js")
        # The token folds the viewport offset (review round 1 MAJOR-A) so a
        # "load earlier" viewport move genuinely re-renders.
        assert (
            "var token = bxTimelineRenderToken(state.ringMutations, q, state.renderOffset);" in js
        )
        assert "if (token === state.lastTimelineToken) {" in js
        # Only ADMITTED records move the counter — a dup-only events page
        # leaves the window unchanged so the next render skips.
        assert "var admitted = bxRingAdd(state.ring, state.ringSeen, records," in js
        assert "state.ringMutations += admitted;" in js
        # loadEarlier's prepends move it too; selectRun forces the next render.
        assert "state.ringMutations += take.length;" in js
        assert "state.lastTimelineToken = null; // ring content reset" in js

    def test_viewport_pager_wiring_source_pins(self) -> None:
        """Review round 1 MAJOR-A — the slidable-viewport DOM wiring.

        The newest-500 render cap previously DEFEATED the wired "load
        earlier" pager: prepended older records were never rendered once the
        filtered rows exceeded the cap. The wiring pins: (a) a loadEarlier
        click slides the viewport onto the OLDEST window; (b) a live events
        tick grows a parked viewport's tail distance by the admissions
        (never yanked back to newest); (c) a run change AND a filter change
        reset the viewport to the live newest window.
        """
        js = _js("bx-oneclick.js")
        # (a) the click slides onto the oldest window (front-anchored offset).
        assert "state.renderOffset = bxTimelineEarlierOffset(" in js
        # (b) a live tick content-anchors a parked viewport.
        assert "state.renderOffset = bxTimelineTickOffset(state.renderOffset, admitted);" in js
        # (c) run + filter changes re-anchor on the newest window.
        assert "state.renderOffset = 0; // a run change re-anchors on the newest window" in js
        assert "state.renderOffset = 0; // a filter change re-anchors on the newest window" in js
        # The rendered window clamps to the renderable range before the
        # stored token folds the EFFECTIVE offset.
        assert (
            "state.renderOffset = bxTimelineClampOffset(rows.length, BX_TIMELINE_RENDER_CAP, "
            "state.renderOffset);" in js
        )

    def test_search_input_is_debounced(self) -> None:
        js = _js("bx-oneclick.js")
        assert "setTimeout(renderTimeline, 200)" in js
        # The killed per-keystroke direct bind.
        assert 'searchBox.addEventListener("input", renderTimeline);' not in js

    @pytest.mark.skipif(_NODE is None, reason="node not installed")
    def test_timeline_pure_primitives_node_golden(self) -> None:
        """EXECUTED golden over the REAL shipped bx-oneclick.js exports."""
        script = (
            f"const O = require({json.dumps(str(_STATIC / 'bx-oneclick.js'))});\n"
            "const rows = [];\n"
            "for (let i = 0; i < 1200; i++) { rows.push({ n: i }); }\n"
            "const capped = O.bxTimelineRenderRows(rows, O.BX_TIMELINE_RENDER_CAP);\n"
            "const small = [{ n: 1 }, { n: 2 }];\n"
            "const out = {\n"
            "  cap: O.BX_TIMELINE_RENDER_CAP,\n"
            "  cappedLen: capped.length,\n"
            "  first: capped[0].n,\n"
            "  last: capped[capped.length - 1].n,\n"
            "  withinCapIsSameArray: O.bxTimelineRenderRows(small, 500) === small,\n"
            "  nonArrayIsEmpty: O.bxTimelineRenderRows(null, 500).length === 0,\n"
            "  note: O.bxTimelineCapNote(capped.length, rows.length),\n"
            "  noteWhenUncut: O.bxTimelineCapNote(200, 200),\n"
            "  tokenStable: O.bxTimelineRenderToken(3, 'x') === O.bxTimelineRenderToken(3, 'x'),\n"
            "  tokenMovesOnMutation: O.bxTimelineRenderToken(3, 'x') !== O.bxTimelineRenderToken(4, 'x'),\n"
            "  tokenMovesOnFilter: O.bxTimelineRenderToken(3, 'x') !== O.bxTimelineRenderToken(3, 'y'),\n"
            "  // the separator tooth: a digit-leading filter can never collide\n"
            "  noJoinCollision: O.bxTimelineRenderToken(1, '2x') !== O.bxTimelineRenderToken(12, 'x'),\n"
            "  nullFilterIsEmpty: O.bxTimelineRenderToken(0, null) === O.bxTimelineRenderToken(0, ''),\n"
            "};\n"
            "console.log(JSON.stringify(out));\n"
        )
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            [str(_NODE), "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out == {
            "cap": 500,
            "cappedLen": 500,
            "first": 700,  # the OLDEST 700 rows are the cut — the tail survives
            "last": 1199,  # the newest row always renders
            "withinCapIsSameArray": True,
            "nonArrayIsEmpty": True,
            "note": "showing newest 500 of 1200 events (render cap)",
            "noteWhenUncut": "",
            "tokenStable": True,
            "tokenMovesOnMutation": True,
            "tokenMovesOnFilter": True,
            "noJoinCollision": True,
            "nullFilterIsEmpty": True,
        }

    @pytest.mark.skipif(_NODE is None, reason="node not installed")
    def test_timeline_viewport_pager_node_golden(self) -> None:
        """Review round 1 MAJOR-A repro, EXECUTED over the REAL shipped file.

        Pre-fix, the newest-500 render slice silently DEFEATED the wired
        "load earlier" pager: with >500 filtered rows, every prepended older
        record was invisible. The golden drives the exact viewport
        composition the widget wires (bxRingAdd admissions →
        bxTimelineTickOffset · loadEarlier prepend → bxTimelineEarlierOffset
        · clamp → bxTimelineRenderRows/bxTimelineCapNote) and pins:
        (a) a ring grown past 500 by three loadEarlier clicks renders the
            OLDEST loaded event;
        (b) a live events tick while the viewport is parked does NOT
            re-anchor it back to newest (same window content; token moves);
        (c) a filter-change reset (offset 0) re-anchors on the newest row.
        """
        script = (
            f"const O = require({json.dumps(str(_STATIC / 'bx-oneclick.js'))});\n"
            "const cap = O.BX_TIMELINE_RENDER_CAP;\n"
            "function ev(n) { return { ts_utc: 't' + n, kind: 'k', payload: { n: n } }; }\n"
            "let ring = []; const seen = {}; let offset = 0;\n"
            "// the initial tail pages: events 700..999 (the newest 300)\n"
            "let recs = []; for (let n = 700; n < 1000; n++) { recs.push(ev(n)); }\n"
            "offset = O.bxTimelineTickOffset(offset, O.bxRingAdd(ring, seen, recs, 5000));\n"
            "function win() {\n"
            "  const rows = O.bxRingFilter(ring, '', '');\n"
            "  offset = O.bxTimelineClampOffset(rows.length, cap, offset);\n"
            "  return { rows: rows, vis: O.bxTimelineRenderRows(rows, cap, offset) };\n"
            "}\n"
            "function earlier(lo, hi) { // the widget's prepend + slide, verbatim shape\n"
            "  const take = [];\n"
            "  for (let n = lo; n < hi; n++) {\n"
            "    const r = ev(n); const k = O.bxEventLineText(r);\n"
            "    if (!seen[k]) { seen[k] = true; take.push(r); }\n"
            "  }\n"
            "  ring = take.concat(ring);\n"
            "  offset = O.bxTimelineEarlierOffset(O.bxRingFilter(ring, '', '').length, cap);\n"
            "}\n"
            "earlier(500, 700); earlier(300, 500); earlier(100, 300); // three clicks\n"
            "const w3 = win();\n"
            "const tokParked = O.bxTimelineRenderToken(9, '', offset);\n"
            "// a live tick while parked: 3 new events admitted at the tail\n"
            "offset = O.bxTimelineTickOffset(\n"
            "  offset, O.bxRingAdd(ring, seen, [ev(1000), ev(1001), ev(1002)], 5000));\n"
            "const w4 = win();\n"
            "const out = {\n"
            "  ringLen: ring.length,\n"
            "  oldestLoadedRendered: w3.vis[0].payload.n,\n"
            "  parkedFirst: w4.vis[0].payload.n,\n"
            "  parkedLast: w4.vis[w4.vis.length - 1].payload.n,\n"
            "  parkedNote: O.bxTimelineCapNote(w4.vis.length, w4.rows.length, offset),\n"
            "  // the token moves on a viewport slide (the skip-render fold)\n"
            "  tokenMovesOnOffset: tokParked !== O.bxTimelineRenderToken(9, '', 0),\n"
            "  // (c) the filter-change reset re-anchors on the newest row\n"
            "  resetNewest: O.bxTimelineRenderRows(w4.rows, cap, 0)[cap - 1].payload.n,\n"
            "  resetNote: O.bxTimelineCapNote(cap, w4.rows.length, 0),\n"
            "};\n"
            "console.log(JSON.stringify(out));\n"
        )
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            [str(_NODE), "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out == {
            "ringLen": 903,
            # (a) the OLDEST loaded event (100) IS rendered after 3 clicks —
            # pre-fix the newest-500 slice rendered 500..999 and hid it.
            "oldestLoadedRendered": 100,
            # (b) the parked window content is UNCHANGED across the tick
            # (100..599) — never yanked back to the newest window.
            "parkedFirst": 100,
            "parkedLast": 599,
            "parkedNote": "showing events 1–500 of 903 (render cap 500)",
            "tokenMovesOnOffset": True,
            "resetNewest": 1002,  # (c) offset 0 renders the live tail again
            "resetNote": "showing newest 500 of 903 events (render cap)",
        }


# ---------------------------------------------------------------------------
# DEC-O1-GUARANTEE-HARDEN-001 — universe.html: the 5 s windows line + the six
# 15 s integrity loaders ride the SAME gbFetchJson body-armed abort bound
# through ONE latest-claims-the-paint helper (boundedLatestJson — the
# gbPollGen idiom per loader), so a hung socket can no longer accumulate per
# tick and an out-of-order slow response can never repaint over a newer one.
# Cadence + rendered output are pinned unchanged by the existing
# test_dashboard_poll_hygiene.py universe pins (guard strings, setInterval
# registrations, catch-up calls) — this class pins only the fetch mechanics.
# ---------------------------------------------------------------------------

_UNIVERSE_LOADER_CALLS = [
    'boundedLatestJson("completeness", "/api/v1/universe/completeness")',
    'boundedLatestJson("mapping", "/api/v1/universe/mapping-integrity")',
    'boundedLatestJson("master", "/api/v1/universe/master-integrity")',
    'boundedLatestJson("economics", "/api/v1/universe/economics-integrity")',
    'boundedLatestJson("parseskips", "/api/v1/universe/parse-skips")',
    'boundedLatestJson("lakeunique", "/api/v1/universe/lake-uniqueness")',
    "boundedLatestJson('windowsline', '/api/v1/coverage/windows?limit=0')",
]


class TestUniverseBoundedLoaders:
    def test_loaders_ride_the_one_bounded_latest_wins_helper(self) -> None:
        html = _tpl("universe.html")
        assert "async function boundedLatestJson(key, url) {" in html
        # Delegation to the SAME gbFetchJson bound (§11 COMMON — no eighth
        # hand-rolled AbortController copy).
        assert "const res = await gbFetchJson(url);" in html
        # The latest-claims-the-paint ordering guard (the gbPollGen idiom).
        assert (
            "if (gen !== loaderGen[key]) return null;   "
            "// superseded — never repaint over a newer response" in html
        )
        for call in _UNIVERSE_LOADER_CALLS:
            assert call in html, f"universe.html: bounded loader call missing: {call!r}"

    def test_bare_untimed_loader_fetches_are_gone(self) -> None:
        html = _tpl("universe.html")
        for killed in (
            'await fetch("/api/v1/universe/completeness")',
            'await fetch("/api/v1/universe/mapping-integrity")',
            'await fetch("/api/v1/universe/master-integrity")',
            'await fetch("/api/v1/universe/economics-integrity")',
            'await fetch("/api/v1/universe/parse-skips")',
            'await fetch("/api/v1/universe/lake-uniqueness")',
            "await fetch('/api/v1/coverage/windows?limit=0')",
        ):
            assert killed not in html, f"universe.html: bare loader fetch resurfaced: {killed!r}"

    def test_loaders_consume_the_parsed_body_shape(self) -> None:
        """gbFetchJson returns {ok, status, body} — the loaders read .body and
        keep their non-ok early-return degrade (a superseded response reads
        null → the same silent keep-last-shown arm)."""
        html = _tpl("universe.html")
        assert html.count("if (!r || !r.ok) return;") == 7, (
            "all 7 migrated loaders must keep the non-ok/superseded early return"
        )
        assert "const map = r.body;" in html
        assert "const counts = (r.body || {}).counts || {};" in html
