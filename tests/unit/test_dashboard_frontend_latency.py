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
"""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
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
        # The selects keep the immediate load (nothing to coalesce).
        assert (
            "[fInstrument, fTf, fDirection, fRankBy].forEach((el) => { el.onchange = load; });"
            in html
        )


# ---------------------------------------------------------------------------
# Review MAJOR fix — the fetch_windows/completeness tick fetches are BOUNDED
# with the abort timer armed THROUGH the body read, honoring the timeoutMs
# BXPoll passes to the custom fetchJson seam (pre-fix the ticks kept bare
# un-timed `fetch`, so under BXPoll's one-in-flight scheduling a
# headers-then-stalled-body response hung the tick promise forever and the
# poller never rescheduled — the frozen-tab class the migration exists to
# prevent).
# ---------------------------------------------------------------------------


class TestBoundedTickFetch:
    @pytest.mark.parametrize("name", ["fetch_windows.html", "completeness.html"])
    def test_tick_fetch_is_bounded_through_the_body_read(self, name: str) -> None:
        html = _tpl(name)
        # The bounded-fetch token: timer armed through the BODY read.
        assert "function fetchTextBounded(url, timeoutMs) {" in html
        assert "/* timer deliberately still armed through the body read */" in html
        assert "await fetchTextBounded(" in html
        # The custom seam threads BXPoll's own timeout bound into the tick.
        assert "fetchJson: function (_url, timeoutMs) {" in html
        assert "return poll(timeoutMs).then(function (ok) {" in html
        # The legacy no-BXPoll arm defaults to the sibling pages' 12 s bound.
        assert '(typeof timeoutMs === "number" && timeoutMs > 0) ? timeoutMs : 12000;' in html

    def test_bare_untimed_tick_fetches_are_gone(self) -> None:
        assert 'await fetch("/api/v1/completeness/scoreboard")' not in _tpl("completeness.html")
        assert 'await fetch("/api/v1/coverage/windows?"' not in _tpl("fetch_windows.html")


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
    "v29.html",
]

# ONE robust block matcher (whitespace-tolerant `</script\s*>` end tag,
# case-insensitive to mirror the open tag — the CodeQL js/bad-tag-filter
# shape); the src-less filter runs in Python over the captured attrs
# instead of a negative lookahead inside the tag match. This is a
# TEST-side parser of our own trusted templates, never a sanitizer.
_SCRIPT_BLOCK = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_SRC_ATTR = re.compile(r"\bsrc\s*=", re.IGNORECASE)


def _inline_script_bodies(html: str) -> list[str]:
    """Every non-empty src-less inline ``<script>`` body in ``html``."""
    return [
        body
        for attrs, body in _SCRIPT_BLOCK.findall(html)
        if not _SRC_ATTR.search(attrs) and body.strip()
    ]


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
