"""/db self-recovering discovery loaders (the G3 client slice of the
pull-contention / feed-resilience pack — the #1529 DEC-PULL-UI-STALENESS-001
posture applied to the /db discovery fetches).

SIX arms ride ONE template-local retry driver: the spot cascade
(``loadSpotCascade``), the three F&O cascade levels (``loadFnoCascade`` /
``dbFetchFnoExpiries`` / ``dbFetchFnoContracts``), the expired-options
datasets arm (``loadOptionDatasets``), and the sidebar TABLES list
(``dbFetchTables`` — the operator-evidenced arm: a mid-pull 12 s timeout
painted the dead "offline — couldn't reach the server" card and never
recovered). A failed fetch schedules a capped-exponential auto-retry
(3 s→30 s, NEVER gives up); a hidden tab fires zero fetches (one
visibilitychange catch-up per label); a generation counter (the universe.html
``gbPollGen`` idiom) supersedes stale chains; a failed refresh KEEPS the
last-good picker list instead of clobbering it with ``[]`` (the fails-before
bug at pre-fix db.html:1260/:1190); and every fetch is BOUNDED through the
BODY read (``dbFetchJsonBounded`` — ``BXUI.fetchWithTimeout`` clears its abort
timer once the HEADERS resolve, so a headers-then-stalled-body response would
have hung ``await r.json()`` forever with no retry scheduled).

HONEST envelope (Golden Rule #10): source-string pins + ``node --check``
syntax gates + node-EXECUTED goldens for the extracted PURE helpers — the
repo has no headless JS runner in CI, so the DOM wiring is asserted by
structure, never executed here (the house #1529/#1561/#1720 envelope).
"""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from html.parser import HTMLParser
from pathlib import Path

import pytest

_TEMPLATES = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "templates"
_STATIC = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"

_NODE = shutil.which("node")


def _db_html() -> str:
    text = (_TEMPLATES / "db.html").read_text(encoding="utf-8")
    assert text, "expected a non-empty db.html template"
    return text


def _extract_function(src: str, name: str) -> str:
    """Extract ``function <name>(...) {...}`` verbatim via brace counting.

    The extracted helpers are brace-free inside string literals, so plain
    counting is exact; the goldens below execute the REAL template code, never
    a hand-copied twin.
    """
    start = src.index(f"function {name}(")
    depth = 0
    for j in range(src.index("{", start), len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _run_node(script: str) -> str:
    proc = subprocess.run(  # nosec B603 — fixed argv, test-only
        [str(_NODE), "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# (a) Bounded fetch THROUGH the body read.
# ---------------------------------------------------------------------------


class TestBoundedDiscoveryFetch:
    def test_bxui_fetch_with_timeout_does_not_bound_the_body_read(self) -> None:
        """Why a per-template helper exists (verified, not assumed).

        ``BXUI.fetchWithTimeout`` clears its abort timer in the HEADERS
        resolve handler, so its deadline never covers ``r.json()`` — reusing
        it could not close the headers-then-stalled-body wedge. Pinned so a
        future bx-ui.js change that DOES bound the body read surfaces here
        and lets db.html drop its local helper.
        """
        js = (_STATIC / "bx-ui.js").read_text(encoding="utf-8")
        assert "function (r) { clearTimeout(timer); return r; }" in js

    def test_db_fetch_json_bounded_arms_the_deadline_through_the_body(self) -> None:
        html = _db_html()
        assert "async function dbFetchJsonBounded(url) {" in html
        assert "/* timer deliberately still armed through the body read */" in html
        # The deadline clears only in the finally — AFTER the body read settled.
        body = _extract_function(html, "dbFetchJsonBounded")
        assert "finally {" in body
        assert "clearTimeout(timer);" in body
        assert "AbortController" in body
        # Every failure shape collapses to null for the retry path.
        assert "if (!r.ok) return null;" in body
        assert "return null;" in body

    def test_fno_fetch_delegates_and_the_unbounded_body_read_is_gone(self) -> None:
        html = _db_html()
        # KILLED token: the old unbounded shape (bxFetch clears its timer at
        # headers; the body read hung forever on a stalled response).
        assert "const r = await bxFetch(url); return r.ok ? await r.json() : null;" not in html
        assert "return dbFetchJsonBounded(url);" in _extract_function(html, "_fnoFetch")

    def test_datasets_and_tables_arms_ride_the_bounded_helper(self) -> None:
        html = _db_html()
        assert 'dbFetchJsonBounded("/api/v1/db/candles/datasets")' in html
        assert 'dbFetchJsonBounded("/api/v1/db/tables")' in html
        # KILLED tokens: the direct headers-only-bounded fetches.
        assert 'bxFetch("/api/v1/db/candles/datasets")' not in html
        assert 'bxFetch("/api/v1/db/tables")' not in html


# ---------------------------------------------------------------------------
# (b) Keep-last-good — the fails-before clobber.
# ---------------------------------------------------------------------------


class TestKeepLastGood:
    def test_clobber_above_null_check_is_gone(self) -> None:
        """KILLED tokens: the pre-fix shapes assigned the list BEFORE the
        ``body === null`` check (db.html:1260 spot / :1190 fno), so a failed
        refresh wiped a previously-usable picker with ``[]``."""
        html = _db_html()
        assert "cstate.spot = { syms: (body && body.symbols) || []" not in html
        assert "cstate.fno = { unds: (body && body.underlyings) || []" not in html

    @pytest.mark.parametrize(
        ("fn", "assign_token"),
        [
            ("loadSpotCascade", "cstate.spot = { syms: bxDiscKeepLast(body.symbols"),
            ("loadFnoCascade", "cstate.fno = { unds: bxDiscKeepLast(body.underlyings"),
        ],
    )
    def test_null_check_precedes_every_list_assignment(self, fn: str, assign_token: str) -> None:
        body = _extract_function(_db_html(), fn)
        assert body.index("if (body === null)") < body.index(assign_token), (
            f"{fn}: the fresh-list assignment must sit BELOW the null check "
            "(keep-last-good — a failed refresh never clobbers a usable picker)"
        )

    @pytest.mark.skipif(_NODE is None, reason="node not installed")
    def test_clobber_reproduced_on_old_shape_and_killed_on_new(self) -> None:
        """The fails-before teeth, node-EXECUTED.

        The OLD shape (verbatim semantics of pre-fix db.html:1260-1264)
        reproduces the clobber: a failed refresh leaves the picker EMPTY. The
        NEW shape — driven by the REAL extracted ``bxDiscKeepLast`` — keeps
        the last-good list. Reverting the fix flips the second assert.
        """
        keep_last = _extract_function(_db_html(), "bxDiscKeepLast")
        script = (
            keep_last
            + r"""
// OLD shape (pre-fix db.html:1260-1264): assignment BEFORE the null check.
function oldLoadSpot(cstate, body) {
  cstate.spot = { syms: (body && body.symbols) || [], sym: "" };
  if (body === null) return "dead-card";
  return "ok";
}
// NEW shape (this fix): null check FIRST; the merge is the template's own
// bxDiscKeepLast (extracted verbatim above).
function newLoadSpot(cstate, body) {
  if (body === null) {
    cstate.spot = { syms: bxDiscKeepLast(null, cstate.spot.syms), sym: cstate.spot.sym || "" };
    return "retrying";
  }
  cstate.spot = { syms: bxDiscKeepLast(body.symbols || [], cstate.spot.syms), sym: "" };
  return "ok";
}
const good = [{ exchange: "NSE", segment: "INDEX", symbol: "NIFTY" }];
const a = { spot: { syms: good.slice(), sym: "NSE|INDEX|NIFTY" } };
const b = { spot: { syms: good.slice(), sym: "NSE|INDEX|NIFTY" } };
const oldOutcome = oldLoadSpot(a, null);
const newOutcome = newLoadSpot(b, null);
console.log(JSON.stringify({
  oldOutcome: oldOutcome, oldKept: a.spot.syms.length,
  newOutcome: newOutcome, newKept: b.spot.syms.length, newSym: b.spot.sym,
  freshWins: newLoadSpot(b, { symbols: [1, 2] }) === "ok" && b.spot.syms.length === 2,
}));
"""
        )
        out = _run_node(script)
        assert out == (
            '{"oldOutcome":"dead-card","oldKept":0,'
            '"newOutcome":"retrying","newKept":1,"newSym":"NSE|INDEX|NIFTY",'
            '"freshWins":true}'
        )

    def test_tables_sidebar_keeps_last_good_rows(self) -> None:
        body = _extract_function(_db_html(), "dbTblFail")
        # A usable sidebar (any .tbl row) stays rendered + gets the status line.
        assert 'if (tablesEl.querySelector(".tbl")) {' in body
        assert 'dbTblStatusLine(bxDiscLastGoodNote("the tables list", delay));' in body


# ---------------------------------------------------------------------------
# (c) Node-executed goldens for the extracted pure helpers.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_NODE is None, reason="node not installed")
class TestPureHelperGoldens:
    def test_backoff_schedule_3s_to_30s_capped_never_gives_up(self) -> None:
        fn = _extract_function(_db_html(), "bxDiscBackoff")
        out = _run_node(fn + "\nconsole.log(JSON.stringify([0,1,2,3,4,5,10].map(bxDiscBackoff)));")
        assert out == "[3000,6000,12000,24000,30000,30000,30000]"

    def test_supersede_generation_logic(self) -> None:
        fn = _extract_function(_db_html(), "bxDiscStale")
        script = (
            fn
            + r"""
// A late response from generation 1 arrives after the user bumped to 2:
const painted = [];
function lateResponse(gen, cur, body) { if (bxDiscStale(gen, cur)) return; painted.push(body); }
lateResponse(1, 2, "stale");   // superseded — must NOT repaint
lateResponse(2, 2, "fresh");   // current — paints
console.log(JSON.stringify([bxDiscStale(0, 0), bxDiscStale(1, 2), painted]));
"""
        )
        assert _run_node(script) == '[false,true,["fresh"]]'

    def test_keep_last_good_merge(self) -> None:
        fn = _extract_function(_db_html(), "bxDiscKeepLast")
        script = (
            fn
            + r"""
console.log(JSON.stringify([
  bxDiscKeepLast(null, ["A"]),        // failed refresh keeps last-good
  bxDiscKeepLast(undefined, ["A"]),   // undefined counts as a failure too
  bxDiscKeepLast(null, null),         // no last-good → honest []
  bxDiscKeepLast([], ["A"]),          // a genuinely EMPTY refresh is DATA — adopted
  bxDiscKeepLast(["B"], ["A"]),       // fresh wins
]));
"""
        )
        assert _run_node(script) == '[["A"],["A"],[],[],["B"]]'

    def test_retry_copy_strings_exact(self) -> None:
        """The design's exact copy strings (countdown set once at schedule
        time — no per-second ticker)."""
        html = _db_html()
        script = (
            _extract_function(html, "bxDiscRetryHint")
            + _extract_function(html, "bxDiscLastGoodNote")
            + r"""
console.log(bxDiscRetryHint(12000, 3));
console.log(bxDiscLastGoodNote("the symbol list", 12000));
"""
        )
        lines = _run_node(script).splitlines()
        assert lines[0] == (
            "the server may be busy (a pull/prebuild may be running) — retrying in 12s (attempt 3)"
        )
        assert lines[1] == (
            "couldn't refresh the symbol list — showing the last-loaded list · retrying in 12s"
        )


# ---------------------------------------------------------------------------
# (d) The retry driver wiring (source pins — the honest no-JS-runner envelope).
# ---------------------------------------------------------------------------


class TestRetryDriverWiring:
    def test_all_six_arms_are_on_the_driver(self) -> None:
        html = _db_html()
        for label in (
            '"spot-symbols"',
            '"fno-underlyings"',
            '"fno-expiries"',
            '"fno-contracts"',
            '"option-datasets"',
            '"db-tables"',
        ):
            assert label in html, f"discovery arm missing from the retry driver: {label}"
        # Every arm gen-guards its late response (5 data arms + the tables arm).
        assert html.count("/* a stale late response never repaints */") == 6

    def test_one_shot_dead_cards_are_gone(self) -> None:
        """KILLED tokens: the direct dead-card renders with no retry
        scheduled (the pre-fix one-shot arms)."""
        html = _db_html()
        assert 'renderCandleView(null, "couldn' not in html
        assert 'renderCandleView(null, "error " + r.status)' not in html
        # The tables loader's dead offline card (the coordinator-evidenced
        # arm) is gone with its try/catch shape.
        assert "tablesEl.innerHTML = '<h3>Tables</h3>' + bxOfflineHtml(e);" not in html
        assert "tablesEl.innerHTML = '<h3>Tables</h3>' + bxErrorHtml(\"error \"" not in html

    def test_hidden_tab_defers_and_visibilitychange_catches_up(self) -> None:
        html = _db_html()
        assert "if (document.hidden) { dbDiscArmVis(label, run, gen, genFn); return; }" in html
        assert 'document.addEventListener("visibilitychange", dbDiscOnVis);' in html
        assert 'document.removeEventListener("visibilitychange", dbDiscOnVis);' in html
        # The catch-up is gen-guarded per label — a superseded defer no-ops.
        assert "if (v && !bxDiscStale(v.gen, v.genFn())) v.run();" in html

    def test_user_actions_bump_the_generation(self) -> None:
        html = _db_html()
        # selectCandles + the two fno selection handlers bump the data-pane gen.
        assert html.count("dbDiscBump();") == 3
        # The sidebar chain is INDEPENDENT: loadTables bumps its own gen, and
        # the data-pane bump resets only the data-pane labels.
        assert "dbTblGen += 1;" in _extract_function(html, "loadTables")
        assert "DB_DISC_DATA_LABELS.forEach(k => { delete dbDiscFails[k]; });" in html

    def test_render_once_update_hint_and_status_line_are_textcontent_only(self) -> None:
        html = _db_html()
        # Render-once: the error card paints through the EXISTING error arm;
        # later ticks touch only the retained .hint node's textContent.
        assert "renderCandleView(null, errText);   /* render-once" in html
        assert "if (hint) hint.textContent = bxDiscRetryHint(delay, attempt);" in html
        # The keep-last-good status lines are createElement + textContent.
        for fn in ("dbDiscStatusLine", "dbTblStatusLine"):
            body = _extract_function(html, fn)
            assert 'document.createElement("div")' in body
            assert "line.textContent = text;" in body
            assert ".innerHTML" not in body

    def test_manual_retry_stays_and_forces_an_immediate_attempt(self) -> None:
        html = _db_html()
        assert 'if (ev.target.closest(".bx-state-retry")) rerunCurrent();' in html
        assert 'if (ev.target.closest(".bx-state-retry")) loadTables();' in html

    def test_driver_block_adds_no_innerhtml_sink(self) -> None:
        """The new driver + helpers introduce ZERO innerHTML assignment sinks
        (the baseline guard in test_dashboard_js_perf_frontend.py stays the
        overall gate; this pins the ADDED block specifically)."""
        html = _db_html()
        start = html.index("Self-recovering discovery loaders")
        end = html.index("async function loadOptionDatasets")
        block = html[start:end]
        assert not re.findall(r"\.innerHTML\s*\+?=", block)


# ---------------------------------------------------------------------------
# (e) node --check on every inline script block of db.html (the #1720 idiom).
# ---------------------------------------------------------------------------


class _InlineScriptExtractor(HTMLParser):
    """Collect every src-less inline ``<script>`` body (stdlib parser — no
    tag regex; script bodies arrive verbatim via ``handle_data``)."""

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


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_db_inline_scripts_parse_under_node_check(tmp_path: Path) -> None:
    parser = _InlineScriptExtractor()
    parser.feed(_db_html())
    parser.close()
    blocks = [body for body in parser.bodies if body.strip()]
    assert blocks, "db.html: expected at least one inline script block"
    for i, body in enumerate(blocks):
        target = tmp_path / f"db.html.{i}.js"
        target.write_text(body, encoding="utf-8")
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            [str(_NODE), "--check", str(target)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, (
            f"db.html inline script block #{i} failed node --check: {proc.stderr}"
        )
