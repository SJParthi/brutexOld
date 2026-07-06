"""DEC-PULL-PROGRESS-HONESTY-001 — frontend + route surfaces of the honest progress.

Whitebox substring asserts on the rendered ``universe.html`` (the file's own
convention — ``test_dashboard_universe_pull_consolidate.py``) + the static
``bx-rate-monitor.js`` widget source, plus a route-level pin that the pull-progress
snapshot's NEW keys are ADDITIVE (every pre-existing flat key still ships).

Fails-before framing: the page rendered a fabricated ``100%`` headline during the
F&O phase (grow-total pin), had NO ETA anywhere, and the rate widget carried no
time-to-done signal at all.

DEC-PULL-UI-STALENESS-001 + DEC-PULL-HEADER-SUBUNIT-001 (operator incident
2026-07-06 — a browser tab froze for 38 minutes showing stale pull data with zero
indication): both poll surfaces gain a client-clock "updated Xs ago" staleness
readout + a self-recovering exponential-backoff poll, and the /universe header
gains client-derived per-expiry sub-unit progress. HONEST envelope (Golden Rule
#10): every test below is a SUBSTRING assert pinning the PRESENCE of the JS
(constants / branches / element ids / class names) in the shipped source — NOT
its execution; this repo has no headless JS runner.
"""

from __future__ import annotations

import re
from pathlib import Path

from brutex.dashboard.app import DashboardState, _render_universe_html, build_app
from fastapi.testclient import TestClient

_RATE_MONITOR_JS = (
    Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static" / "bx-rate-monitor.js"
)


# ── universe.html — ETA + "total unknown" rendering (textContent-only) ──────────


def test_universe_running_headline_gains_the_eta_suffix() -> None:
    html = _render_universe_html()
    # The fmtEta/etaSuffix helpers ship, and the running headline appends the suffix.
    assert "function fmtEta(sec)" in html
    assert "function etaSuffix(s)" in html
    assert "` · ETA ${eta}`" in html
    # "~Xh Ym" / "~Ym" — the two honest formats.
    assert "`~${h}h ${m}m`" in html
    assert "`~${m}m`" in html


def test_universe_eta_hidden_when_null_or_open_ended() -> None:
    html = _render_universe_html()
    # A non-number (null) ETA renders NOTHING …
    assert "if (typeof sec !== 'number' || !isFinite(sec) || sec < 0) return '';" in html
    # … and an open-ended phase never shows a linear ETA at all (the refuter pin).
    assert "if (s.open_ended) return '';" in html


def test_universe_open_ended_renders_total_unknown_not_a_pinned_percent() -> None:
    # Fails-before: the ONLY running headline was the percent one, which read a
    # fabricated 100% through the whole F&O phase. Now the open_ended branch says
    # the honest thing — units done, total unknown — via textContent.
    html = _render_universe_html()
    assert "units · total unknown · ${esc(s.current || '…')}`" in html
    assert "s.open_ended" in html
    # The counter mirrors it ("N / ?"), instead of a fake "N / N".
    assert "' / ?'" in html


def test_universe_of_total_stat_reads_question_mark_when_open_ended() -> None:
    html = _render_universe_html()
    assert "['of total', s.open_ended ? null : s.total, '']" in html
    assert "(v === null) ? '?'" in html


# ── bx-rate-monitor.js — the ETA pill row ───────────────────────────────────────


def test_rate_monitor_gains_a_hidden_by_default_eta_row() -> None:
    js = _RATE_MONITOR_JS.read_text(encoding="utf-8")
    assert 'PROGRESS_ENDPOINT = "/api/v1/universe/pull-progress"' in js
    assert 'etaRow.id = "bxrmEtaRow"' in js
    assert 'etaRow.style.display = "none"' in js  # hidden until a real ETA exists
    assert "function renderEta(progress)" in js
    # Hidden when not running / null ETA; the backend owns the open-ended verdict.
    assert "progress.running === true ? fmtEtaSeconds(progress.eta_seconds)" in js


def test_rate_monitor_eta_renders_via_textcontent_only() -> None:
    js = _RATE_MONITOR_JS.read_text(encoding="utf-8")
    # The widget's one text sink is setText (textContent) — the ETA rides it too.
    assert 'setText("bxrmEta", text)' in js
    assert "innerHTML" not in js  # the whole widget stays markup-parse-free


def test_rate_monitor_eta_format_matches_universe() -> None:
    js = _RATE_MONITOR_JS.read_text(encoding="utf-8")
    assert 'return h > 0 ? "~" + h + "h " + m + "m" : "~" + m + "m";' in js


# ── the route — additive snapshot keys ──────────────────────────────────────────


def test_pull_progress_route_ships_the_new_keys_additively() -> None:
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    body = client.get("/api/v1/universe/pull-progress").json()
    # NEW keys present …
    for key in ("started_at", "units_per_sec", "eta_seconds", "open_ended"):
        assert key in body
    # … honest idle values (no fabricated rate/ETA on an idle job) …
    assert body["eta_seconds"] is None
    assert body["units_per_sec"] == 0.0
    assert body["open_ended"] is False
    # … and every pre-existing flat key STILL ships (the additive contract).
    for key in (
        "state",
        "total",
        "completed",
        "failed",
        "gaps",
        "bars",
        "current",
        "detail",
        "percent",
        "running",
        "updated_at",
        "phase",
        "stalled",
        "stall_detail",
    ):
        assert key in body


# ── bx-rate-monitor.js — client staleness + self-recovering backoff poll ────────
# (DEC-PULL-UI-STALENESS-001; substring asserts pin PRESENCE of the JS, not execution.)


def _widget_js() -> str:
    return _RATE_MONITOR_JS.read_text(encoding="utf-8")


def test_regression_rate_widget_silent_catch_hid_staleness() -> None:
    """REGRESSION (bug 1a, operator incident 2026-07-06): the widget's poll swallowed
    every failure — a non-ok response mapped to ``null`` (render skipped) and the
    catch was a silent no-op ("leave the last render in place"), so the last DOM
    values persisted FOREVER with no staleness marker and the cadence never adapted.
    Now a non-ok status THROWS, every failure is counted, and the next poll is
    scheduled on backoff. Substring-presence pins (no headless JS runner).
    """
    js = _widget_js()
    assert "leave the last render in place" not in js, "the silent-catch comment must be gone"
    assert 'throw new Error("HTTP " + r.status)' in js, "a non-ok response must be a FAILED poll"
    assert "pollFailures += 1;" in js, "every failure must be counted"
    assert "schedulePoll(backoffDelayMs(pollFailures));" in js, (
        "a failed poll must reschedule on backoff — never a dead stop"
    )


def test_rate_widget_backoff_constants_and_overflow_clamp() -> None:
    """Backoff = base 3 s doubling to a 30 s cap; the exponent is clamped BEFORE
    ``Math.pow`` so the arithmetic can never overflow. Presence pins only.
    """
    js = _widget_js()
    assert "var BACKOFF_BASE_MS = 3000;" in js
    assert "var BACKOFF_MAX_MS = 30000;" in js
    assert "function backoffDelayMs(failures)" in js
    assert "Math.min(failures > 0 ? failures - 1 : 0, 4)" in js, "exponent clamp missing"
    assert "Math.min(BACKOFF_BASE_MS * Math.pow(2, exp), BACKOFF_MAX_MS)" in js, "cap missing"


def test_rate_widget_success_resets_cadence_and_stamps_client_clock() -> None:
    """A successful poll resets the failure count, stamps the CLIENT clock, and
    resumes the normal cadence; the fixed ``setInterval(poll, …)`` loop is GONE
    (the completion-scheduled chain is the backoff carrier). Presence pins only.
    """
    js = _widget_js()
    assert "pollFailures = 0;" in js
    assert "lastPollOkMs = nowMs();" in js, "the client staleness stamp is missing"
    assert "schedulePoll(POLL_MS);" in js, "success must resume the normal cadence"
    assert "setInterval(poll, POLL_MS)" not in js, (
        "the fixed interval must be replaced by the self-scheduling chain"
    )


def test_rate_widget_age_readout_and_thresholds() -> None:
    """The ~1 s client ticker renders "updated Xs ago"; amber past 10 s (~3 missed
    3 s polls + margin), LOUD RED explicit-STALE past 30 s (10x the poll cadence ==
    the backoff cap, so "retrying" reads literally true). Presence pins only.
    """
    js = _widget_js()
    assert "var STALE_WARN_MS = 10000;" in js
    assert "var STALE_RED_MS = 30000;" in js
    assert "var AGE_TICK_MS = 1000;" in js
    assert "function renderAge()" in js
    assert "setInterval(renderAge, AGE_TICK_MS);" in js, "the ~1 s age ticker is missing"
    assert '"STALE — " + base + " — retrying"' in js, "the explicit STALE wording is missing"
    assert '"no data yet (" + ageS + "s)"' in js, "the never-succeeded boot wording is missing"
    assert 'pillRow("updated", "bxrmAge", null)' in js, "the age pill row is missing"
    assert 'ageRow.id = "bxrmAgeRow"' in js


def test_rate_widget_stale_css_classes() -> None:
    """The stale/warn CSS classes exist for both the pill and the overlay, plus the
    widget-root red border while STALE. Presence pins only."""
    js = _widget_js()
    assert ".bxrm-stale{color:var(--red,#FF5C5C);font-weight:800;}" in js
    assert ".bxrm-stale-warn{color:var(--yellow,#FFB74D)" in js
    assert ".bxrm-stale{border-color:var(--red,#FF5C5C);}" in js, "the root border rule is missing"
    assert 'w.classList.toggle("bxrm-stale"' in js, "the root-class toggle is missing"


def test_rate_widget_age_clock_is_monotonic_client_side() -> None:
    """The age readout is the CLIENT's monotonic clock (performance.now, immune to
    NTP jumps; Date.now documented fallback) — the task's explicit requirement.
    A never-succeeding page still ages honestly from the widget-boot baseline."""
    js = _widget_js()
    assert "function nowMs()" in js
    assert "performance.now" in js
    assert "Date.now()" in js
    assert "var bootMs = null;" in js, "the boot baseline is missing"


def test_rate_widget_poll_overlap_guard() -> None:
    """Re-entrancy token: a superseded in-flight response never renders or schedules
    (visibility re-poll / overlapping chains cannot double-schedule)."""
    js = _widget_js()
    assert "var seq = ++pollSeq;" in js
    assert "if (seq !== pollSeq) { return; }" in js


def test_rate_widget_backgrounded_tab_repolls_on_visibility() -> None:
    """A background-throttled tab re-polls immediately on becoming visible instead
    of waiting out a backoff (the age is timestamp-derived, so it is already
    correct the instant it renders)."""
    js = _widget_js()
    assert '"visibilitychange"' in js
    assert "!document.hidden" in js
    assert "schedulePoll(0);" in js


def test_rate_widget_malformed_payload_and_hung_fetch_feed_backoff() -> None:
    """A 200-with-junk body throws into the same counted-failure path, and a hung
    fetch is aborted after FETCH_TIMEOUT_MS so the self-scheduling chain can never
    wedge. Presence pins only."""
    js = _widget_js()
    assert 'throw new Error("malformed payload")' in js
    assert 'typeof data !== "object"' in js
    assert "AbortController" in js
    assert "ctrl.abort()" in js
    assert "FETCH_TIMEOUT_MS" in js
    assert "function fetchJson(url)" in js


def test_rate_widget_eta_failure_hides_not_freezes() -> None:
    """The secondary ETA fetch now HIDES the row on failure (renderEta(null))
    instead of freezing a stale figure; it never drives scheduling/staleness —
    the main endpoint owns the widget's numbers (stated honestly in the JS)."""
    js = _widget_js()
    assert "renderEta(null)" in js


def test_rate_widget_server_age_shown_additionally_never_replacing() -> None:
    """The payload's SERVER-side seconds_since_update is shown ADDITIONALLY in the
    overlay — it never replaces the client-clock measurement (a null value reads
    the honest "never ticked")."""
    js = _widget_js()
    assert 'gridMetric("updated", "bxrmEAge", "bxrmEAgeC")' in js
    assert "seconds_since_update" in js
    assert '"server: never ticked"' in js


# ── universe.html — client staleness + never-give-up poll ───────────────────────
# (DEC-PULL-UI-STALENESS-001; substring asserts pin PRESENCE of the JS, not execution.)


def _poll_progress_src(html: str) -> str:
    fn = re.search(r"async function pollProgress\(label\) \{.*?\n\}", html, re.DOTALL)
    assert fn is not None, "pollProgress is missing"
    return fn.group(0)


def test_regression_universe_poll_gave_up_and_froze_38_minutes() -> None:
    """REGRESSION (bug 1b, operator incident 2026-07-06): after 5 quick retries the
    old catch painted "live progress feed dropped" via innerHTML and called
    finishLive(), PERMANENTLY stopping the poll — the page needed a manual refresh
    (the 38-minute frozen tab). The catch now backs off and NEVER gives up; the
    pinned "live progress feed dropped" wording survives INSIDE the never-give-up
    stale chip. Substring-presence pins (no headless JS runner)."""
    html = _render_universe_html()
    poll = _poll_progress_src(html)
    catch = re.search(r"\} catch \(e\) \{(.*?)\n\}", poll, re.DOTALL)
    assert catch is not None, "the pollProgress catch is missing"
    assert "finishLive()" not in catch.group(1), (
        "the catch must NEVER settle the watch — a poll failure is not a pull verdict"
    )
    assert "feedRetryDelayMs()" in catch.group(1), "the catch must reschedule on backoff"
    assert "(the pull may still be running); ⟳ Refresh the verdict below to re-check" not in html, (
        "the old give-up tail must be gone"
    )
    assert "live progress feed dropped" in html, (
        "the pinned wording must survive (now inside the never-give-up stale chip)"
    )


def test_regression_universe_non_ok_poll_never_settles_the_header() -> None:
    """REGRESSION: the old pollProgress had NO r.ok check — a 500-with-JSON body
    yielded s.state === undefined, fell through to the settled branch, painted
    "— nothing to pull" and killed the watch. A non-ok status and a malformed body
    now BOTH throw before s.state is ever consulted."""
    html = _render_universe_html()
    poll = _poll_progress_src(html)
    assert "if (!r.ok) throw new Error('HTTP ' + r.status);" in poll
    assert "throw new Error('malformed snapshot')" in poll
    ok_throw = poll.index("if (!r.ok) throw new Error('HTTP ' + r.status);")
    shape_throw = poll.index("throw new Error('malformed snapshot')")
    state_use = poll.index("s.state === 'running'")
    assert ok_throw < state_use and shape_throw < state_use, (
        "both throws must precede any s.state use — a failed poll is never a settled snapshot"
    )


def test_universe_feed_age_chip_dom_css_and_thresholds() -> None:
    """The #feedage chip + its CSS states + the justified thresholds: warn 5 s
    (past the ~3 s quick-retry envelope, ~8 missed 600 ms polls), red 15 s (the
    cadence miss + the first two failed backoff waits 3+6 s). Presence pins."""
    html = _render_universe_html()
    assert 'id="feedage"' in html
    assert ".chip.stale {" in html
    assert ".chip.stale.on {" in html
    assert ".chip.stale.red {" in html
    assert "const FEED_STALE_WARN_MS = 5000;" in html
    assert "const FEED_STALE_RED_MS = 15000;" in html
    assert "const FEED_BACKOFF_BASE_MS = 3000;" in html
    assert "const FEED_BACKOFF_MAX_MS = 30000;" in html
    assert "'updated ' + ageS + 's ago — retrying'" in html
    assert "'⚠ STALE — no live update for ' + ageS + 's · '" in html
    assert "function renderFeedAge()" in html
    assert "setInterval(renderFeedAge, 1000)" in html


def test_universe_stale_clears_on_success_and_settle() -> None:
    """A successful poll stamps the client clock (chip clears instantly); a settled
    watch stops the ticker + bumps the generation so a straggler never repaints;
    a watch whose FIRST poll fails still escalates from the watch-start baseline."""
    html = _render_universe_html()
    poll = _poll_progress_src(html)
    assert "lastProgressOkMs = nowMs();" in poll, "the success path must stamp the client clock"
    finish = re.search(r"function finishLive\(\) \{(.*?)\n\}", html, re.DOTALL)
    assert finish is not None, "finishLive is missing"
    assert "stopFeedAgeTicker();" in finish.group(1)
    assert "pollGen += 1;" in finish.group(1)
    assert "function startFeedAgeTicker()" in html
    assert "if (lastProgressOkMs === null) lastProgressOkMs = nowMs();" in html, (
        "the watch-start baseline is missing — a first-poll failure must still escalate"
    )


def test_universe_quick_retry_budget_then_backoff_never_gives_up() -> None:
    """POLL_MAX_FEED_RETRIES keeps its genuine semantics as the quick-retry budget
    in FRONT of the exponential backoff; past it the poll self-recovers on a 3 s
    doubling to a 30 s cap — never a dead stop."""
    html = _render_universe_html()
    assert "function feedRetryDelayMs()" in html
    assert "if (pollFeedRetries <= POLL_MAX_FEED_RETRIES) return PROGRESS_MS;" in html
    assert "Math.min(FEED_BACKOFF_MAX_MS, FEED_BACKOFF_BASE_MS * Math.pow(2, exp))" in html
    assert "pollTimer = setTimeout(() => pollProgress(label), feedRetryDelayMs());" in html


def test_universe_poll_reentrancy_generation_guard() -> None:
    """Every pollProgress invocation claims the chain; a superseded in-flight
    response renders nothing and schedules nothing."""
    html = _render_universe_html()
    assert "const gen = ++pollGen;" in html
    assert "if (gen !== pollGen) return;" in html


def test_universe_backgrounded_tab_repolls_on_visibility() -> None:
    """On tab return, re-poll NOW instead of waiting out a backoff (background-tab
    timer throttling stretched the old quick-retry budget into minutes)."""
    html = _render_universe_html()
    assert "document.addEventListener('visibilitychange'" in html
    assert "lastPollLabel" in html


# ── universe.html — per-expiry sub-unit progress (DEC-PULL-HEADER-SUBUNIT-001) ──


def test_regression_giant_unit_progress_was_invisible() -> None:
    """REGRESSION (bug 2, operator incident 2026-07-06): one giant unit (a whole
    index option chain) held completed/total still for minutes while s.current
    ticked per processed pair — the header showed NO movement. The header + the
    #subunit counter now derive sub-unit progress ENTIRELY client-side from
    s.current transitions. Substring-presence pins (no headless JS runner)."""
    html = _render_universe_html()
    assert "function trackSubUnit(s)" in html
    assert "function subUnitNote(s, count)" in html
    assert html.count("(subNote ? ' · ' + subNote : '')") >= 2, (
        "the sub-unit note must ride BOTH running-header arms (open-ended + percent)"
    )
    assert "'sub-unit ' + count.toLocaleString('en-IN') + ' this unit'" in html
    assert 'id="subunit"' in html


def test_universe_expiry_token_parse_right_anchored_mirrors_fno_symbol() -> None:
    """expiryFromCurrent mirrors data/fno_symbol.py's right-anchored grammar
    (EXCH-UNDERLYING-DDMmmYY[-STRIKE]-TYPE): last token FUT/CE/PE; option >=5
    tokens -> expiry at len-3; future >=4 -> len-2 — a hyphenated underlying
    (BAJAJ-AUTO) can never confuse the parse. The token is regex-whitelisted."""
    html = _render_universe_html()
    assert "function expiryFromCurrent(cur)" in html
    assert "parts[parts.length - 3]" in html
    assert "parts.length >= 5" in html
    assert "parts[parts.length - 2]" in html
    assert "parts.length >= 4" in html
    assert "(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)" in html
    assert ".split(' ')[0]" in html, "the label's interval suffix must be stripped first"


def test_universe_subunit_resets_per_unit_and_per_pull() -> None:
    """The tracker resets on ANY completed/total movement (the unit boundary,
    covering frozen-total AND grow-total phases) and on watch start + settle."""
    html = _render_universe_html()
    assert "String(s.completed || 0) + '/' + String(s.total || 0)" in html
    assert "function resetSubUnit()" in html
    assert html.count("resetSubUnit();") >= 2, (
        "resetSubUnit must run at BOTH watch start (startAndPoll) and settle (finishLive)"
    )


def test_universe_subunit_cap_bounds_memory() -> None:
    """The distinct-label Set is capped; past the cap the count degrades honestly
    to transition counting instead of growing without bound over a long watch."""
    html = _render_universe_html()
    assert "SUBUNIT_SET_CAP = 4096" in html
    assert "cur !== subUnitLastCur" in html


def test_universe_subunit_omitted_for_spot_or_empty_current() -> None:
    """Graceful degrade: a spot label / empty current / junk token yields NO
    expiry chunk (null, never garbage), and the count chunk hides below 2 —
    the header stays byte-identical to today when there is nothing to say."""
    html = _render_universe_html()
    assert "count >= 2" in html
    assert "bits.push('expiry ' + exp)" in html
    assert "? tok : null" in html
