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
    # The cap comment states the SETTLE-relative bound the code delivers (refuter
    # finding, round 4: "recovery noticed <=30 s after the server returns"
    # overclaimed — a max-backoff attempt already in flight when the server
    # recovers still stalls to the 10 s abort THEN waits the full 30 s cap, so the
    # worst-case recovery notice is ~= FETCH_TIMEOUT_MS + cap ~= 40 s).
    assert "next attempt <=30 s after the previous poll SETTLES" in js, (
        "the honest settle-relative cap comment is missing"
    )
    assert "recovery noticed <=30 s after the server returns" not in js, (
        "the overclaiming recovery-latency comment must be gone"
    )


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
    # schedulePoll's BODY is the primitive ALL three pinned call sites ride
    # (refuter finding, round 5: the call sites were pinned but the body was not —
    # a no-op body polls exactly ONCE at start() and never again, the exact
    # frozen-widget class this PR fixes, with the whole suite green; the universe
    # twin's reschedule was already pinned verbatim, so this closes the asymmetry).
    assert "function schedulePoll(delayMs)" in js, "the schedulePoll definition is missing"
    assert "pollTimer = setTimeout(poll, delayMs);" in js, (
        "schedulePoll's setTimeout body is the self-scheduling chain's teeth"
    )


def test_rate_widget_age_readout_and_thresholds() -> None:
    """The ~1 s client ticker renders "updated Xs ago"; amber past 10 s (~3 missed
    3 s polls + margin), LOUD RED explicit-STALE past 30 s (10x the poll cadence ==
    the backoff cap, so "retrying" — shown only when a failure was COUNTED, round 5
    — reads literally true). Presence pins only.
    """
    js = _widget_js()
    assert "var STALE_WARN_MS = 10000;" in js
    assert "var STALE_RED_MS = 30000;" in js
    assert "var AGE_TICK_MS = 1000;" in js
    assert "function renderAge()" in js
    assert "setInterval(renderAge, AGE_TICK_MS);" in js, "the ~1 s age ticker is missing"
    assert '"STALE — " + base + tail' in js, "the explicit STALE wording is missing"
    # BOTH escalation branches pinned — the red tier AND the amber warn tier
    # (refuter finding: the warn BRANCH itself was unpinned; deleting it kept
    # every test green while killing the ~3-missed-polls amber requirement).
    # Round 5: both branches render the ONE failure-gated computed tail (see
    # test_regression_rate_widget_claimed_retrying_during_slow_successful_poll).
    assert 'if (age >= STALE_RED_MS) { text = "STALE — " + base + tail; cls = "bxrm-stale"; }' in js
    assert (
        'else if (age >= STALE_WARN_MS) { text = base + tail; cls = "bxrm-stale-warn"; }' in js
    ), "the amber warn tier branch is missing"
    assert '"no data yet (" + ageS + "s)"' in js, "the never-succeeded boot wording is missing"
    assert 'pillRow("updated", "bxrmAge", null)' in js, "the age pill row is missing"
    assert 'ageRow.id = "bxrmAgeRow"' in js


def test_regression_rate_widget_claimed_retrying_during_slow_successful_poll() -> None:
    """REGRESSION (refuter-confirmed, round 5): ``renderAge`` appended " — retrying"
    to EVERY warn/red readout purely off age. A merely SLOW but SUCCESSFUL poll —
    a 7-10 s response inside the 10 s abort bound — pushes the age past
    STALE_WARN_MS mid-flight with ``pollFailures === 0``: no failure was ever
    counted, no backoff retry exists (the normal-cadence poll is simply in
    flight), yet the pill flashed amber "updated 10s ago — retrying", implying a
    failure that had not occurred. The age figure itself was always TRUE and the
    state self-clears when the slow poll settles — only the action word
    overstated. The tail is now FAILURE-gated: "retrying" only when
    ``pollFailures > 0``; at zero failures it reads the in-flight truth,
    "awaiting response". Substring-presence pins (no headless JS runner)."""
    js = _widget_js()
    assert 'var tail = pollFailures > 0 ? " — retrying" : " — awaiting response";' in js, (
        "the failure-gated tail ternary is missing"
    )
    # BOTH escalation branches render the ONE computed tail …
    assert 'if (age >= STALE_RED_MS) { text = "STALE — " + base + tail; cls = "bxrm-stale"; }' in js
    assert 'else if (age >= STALE_WARN_MS) { text = base + tail; cls = "bxrm-stale-warn"; }' in js
    # … the failure-blind hardcoded literals are gone (the regression pin) …
    assert '"STALE — " + base + " — retrying"' not in js, (
        "the unconditional red '— retrying' literal must be gone"
    )
    assert 'text = base + " — retrying"' not in js, (
        "the unconditional amber '— retrying' literal must be gone"
    )
    # … and the gated ternary is the ONE executable "— retrying" site.
    assert js.count('" — retrying"') == 1, "exactly ONE executable site — the gated ternary"


def test_rate_widget_stale_css_classes() -> None:
    """The stale/warn CSS classes exist for both the pill and the overlay, plus the
    widget-root red border while STALE. The OVERLAY rules must carry the
    ``.bxrm-grid .gv.<state>`` compound selector (specificity 1-3-0) so they beat
    the base ``#…Overlay .bxrm-grid .gv`` colour rule (1-2-0) — refuter finding:
    the earlier ``#…Overlay .bxrm-stale`` (1-1-0) LOST that contest, so the
    expanded view's "updated" value never turned red/amber (dead CSS).
    Presence pins only."""
    js = _widget_js()
    assert ".bxrm-stale{color:var(--red,#FF5C5C);font-weight:800;}" in js
    assert ".bxrm-stale-warn{color:var(--yellow,#FFB74D)" in js
    assert ".bxrm-stale{border-color:var(--red,#FF5C5C);}" in js, "the root border rule is missing"
    assert 'w.classList.toggle("bxrm-stale"' in js, "the root-class toggle is missing"
    # Overlay rules at winning specificity (id + 3 classes)…
    assert ".bxrm-grid .gv.bxrm-stale{color:var(--red,#FF5C5C);}" in js, (
        "the overlay red rule must out-specify the base .gv colour rule"
    )
    assert ".bxrm-grid .gv.bxrm-stale-warn{color:var(--yellow,#FFB74D);}" in js, (
        "the overlay warn rule must out-specify the base .gv colour rule"
    )
    # … and the LOSING under-specified overlay rules are gone (regression pin).
    assert 'OVERLAY_ID + " .bxrm-stale{' not in js, "the dead 1-1-0 overlay rule must be gone"
    assert 'OVERLAY_ID + " .bxrm-stale-warn{' not in js, (
        "the dead 1-1-0 overlay warn rule must be gone"
    )


def test_rate_widget_age_clock_is_monotonic_client_side() -> None:
    """The age readout is the CLIENT's monotonic clock (performance.now, immune to
    NTP jumps; Date.now documented fallback) — the task's explicit requirement.
    A never-succeeding page still ages honestly from the widget-boot baseline."""
    js = _widget_js()
    assert "function nowMs()" in js
    assert "performance.now" in js
    assert "Date.now()" in js
    assert "var bootMs = null;" in js, "the boot baseline is missing"
    # The baseline must actually be STAMPED at start() (refuter finding: only the
    # null declaration was pinned, so removing the stamp — killing the honest
    # never-succeeded ageing this docstring claims — kept the suite green).
    assert "bootMs = nowMs();" in js, "the boot-baseline stamp in start() is missing"
    # … and the fallback USE (refuter finding, round 4: the declaration + stamp
    # were pinned but not the ternary — mutating it to `var ref = lastPollOkMs;`
    # kept the suite green while a page whose polls fail from boot showed '—'
    # forever with zero escalation, exactly the incident behavior on a fresh tab).
    assert "var ref = lastPollOkMs !== null ? lastPollOkMs : bootMs;" in js, (
        "the boot-baseline fallback USE is the never-succeeded ageing's teeth"
    )


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
    wedge. The abort timer must stay ARMED through the BODY read (refuter finding:
    clearing it at headers left ``r.json()`` unbounded — a headers-then-stalled-body
    response wedged the poll chain). Presence pins only."""
    js = _widget_js()
    assert 'throw new Error("malformed payload")' in js
    assert 'typeof data !== "object"' in js
    assert "AbortController" in js
    assert "ctrl.abort()" in js
    assert "var FETCH_TIMEOUT_MS = 10000;" in js, "the 10 s abort bound value is missing"
    assert "function fetchJson(url)" in js
    # The signal WIRE (refuter finding: deleting `opts.signal = ctrl.signal;` made
    # ctrl.abort() a no-op — nothing listened — with the whole suite green).
    assert "if (ctrl) { opts.signal = ctrl.signal; }" in js, (
        "the abort signal must be wired into the fetch options — the bound's teeth"
    )
    # The killer is cleared only AFTER the body parse settles …
    assert "return r.json();   // killer still armed: a stalled BODY aborts too" in js
    assert (
        "}).then(function (data) {\n"
        "      if (killer) { clearTimeout(killer); }\n"
        "      return data;\n"
        "    }).catch" in js
    ), "the killer must be cleared only after r.json() settles"
    # … and the old clear-at-headers pattern is gone (regression pin).
    assert "if (killer) { clearTimeout(killer); }\n      if (!r.ok)" not in js, (
        "the clear-at-headers pattern left the body read unbounded — must be gone"
    )


def test_rate_widget_eta_failure_hides_not_freezes() -> None:
    """The secondary ETA fetch now HIDES the row on failure (renderEta(null))
    instead of freezing a stale figure; it never drives scheduling/staleness —
    the main endpoint owns the widget's numbers (stated honestly in the JS)."""
    js = _widget_js()
    assert "renderEta(null)" in js


def test_regression_rate_widget_eta_row_starved_by_shared_pollseq() -> None:
    """REGRESSION (refuter-confirmed, round 3): the round-2 reorder fix guarded BOTH
    ETA callbacks on the MAIN chain's ``pollSeq`` — which the main poll bumps every
    3 s on success. Whenever ``/api/v1/universe/pull-progress`` was consistently
    slower than that cadence (but inside the 10 s abort bound — e.g. threadpool
    starvation during a heavy pull, the exact incident conditions), EVERY progress
    response resolved after the next ``poll()`` had bumped ``pollSeq``, so
    ``renderEta`` was NEVER called and the ETA row froze stale (or frozen-hidden)
    for as long as the latency band persisted. The ETA chain now carries its OWN
    monotonic token pair — ``etaSeq`` (issue counter) + ``etaSettledSeq`` (newest
    SETTLED response) — latest-settled-wins: an out-of-order straggler still can
    neither repaint an older ETA over a fresher render nor hide the row a fresher
    response painted (the round-2 reorder fix preserved), while liveness no longer
    couples to main-poll cadence. Substring-presence pins (no headless JS runner)."""
    js = _widget_js()
    assert "var etaSeq = 0;" in js, "the ETA chain's own issue counter is missing"
    assert "var etaSettledSeq = 0;" in js, "the newest-settled stamp is missing"
    assert "var etaReq = ++etaSeq;" in js, "each ETA fetch must claim its own token"
    assert (
        "fetchJson(PROGRESS_ENDPOINT)\n"
        "      .then(function (progress) {\n"
        "        if (etaReq <= etaSettledSeq) { return; }\n"
        "        etaSettledSeq = etaReq;\n"
        "        renderEta(progress);\n"
        "      })\n"
        "      .catch(function () {\n"
        "        if (etaReq <= etaSettledSeq) { return; }\n"
        "        etaSettledSeq = etaReq;\n"
        "        renderEta(null);\n"
        "      });" in js
    ), "both ETA callbacks must ride the latest-settled-wins guard"
    # The starving shared-token guard is GONE from the ETA callbacks (the MAIN
    # chain keeps its pollSeq guard — pinned in test_rate_widget_poll_overlap_guard).
    eta_block = js[js.index("fetchJson(PROGRESS_ENDPOINT)") :]
    assert "if (seq !== pollSeq) { return; }" not in eta_block, (
        "the ETA callbacks must NOT reuse the main chain's pollSeq — sustained"
        " progress latency above the poll cadence starved the row forever"
    )
    # The pre-round-2 unguarded one-liners stay gone (the reorder regression pin).
    assert ".then(function (progress) { renderEta(progress); })" not in js, (
        "the unguarded success callback must be gone"
    )
    assert ".catch(function () { renderEta(null); })" not in js, (
        "the unguarded failure callback must be gone"
    )


def test_rate_widget_age_row_attached_and_dom_writes_pinned() -> None:
    """The collapsed pill's age readout is genuinely REACHABLE (refuter finding:
    only the row's CREATION was pinned — detaching `ageRow,` from buildWidget's
    children, or deleting the `if (pill)`/`if (big)` writes, silently blanked the
    always-visible "updated Xs ago" text while every test stayed green, leaving
    only the red border at 30 s)."""
    js = _widget_js()
    # The attachment: etaRow + ageRow close buildWidget's children array.
    assert "        etaRow,\n        ageRow,\n      ]" in js, (
        "ageRow must be attached to the collapsed widget's children"
    )
    # The DOM writes for both twins (collapsed pill + expanded overlay value).
    assert "if (pill) { pill.textContent = text; pill.className = cls; }" in js, (
        "the collapsed-pill age write is missing"
    )
    assert (
        'if (big) { big.textContent = text; big.className = "gv" + (cls ? " " + cls : ""); }' in js
    ), "the overlay age write (gv base class preserved) is missing"


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


def _start_and_poll_src(html: str) -> str:
    fn = re.search(r"async function startAndPoll\(path, label\) \{.*?\n\}", html, re.DOTALL)
    assert fn is not None, "startAndPoll is missing"
    return fn.group(0)


def _finish_live_src(html: str) -> str:
    fn = re.search(r"function finishLive\(\) \{.*?\n\}", html, re.DOTALL)
    assert fn is not None, "finishLive is missing"
    return fn.group(0)


def _render_feed_age_src(html: str) -> str:
    fn = re.search(r"function renderFeedAge\(\) \{.*?\n\}", html, re.DOTALL)
    assert fn is not None, "renderFeedAge is missing"
    return fn.group(0)


def _stop_feed_age_ticker_src(html: str) -> str:
    fn = re.search(r"function stopFeedAgeTicker\(\) \{.*?\n\}", html, re.DOTALL)
    assert fn is not None, "stopFeedAgeTicker is missing"
    return fn.group(0)


def _track_sub_unit_src(html: str) -> str:
    fn = re.search(r"function trackSubUnit\(s\) \{.*?\n\}", html, re.DOTALL)
    assert fn is not None, "trackSubUnit is missing"
    return fn.group(0)


def test_regression_universe_hung_fetch_could_wedge_poll_forever() -> None:
    """REGRESSION (refuter-confirmed): ``pollProgress`` fetched with NO abort bound
    while the visibilitychange recovery was gated on ``pollTimer`` — which is null
    exactly while a poll is IN FLIGHT. A headers-then-stalled-body response (or a
    half-open connection dying silently) hung ``await r.json()`` indefinitely
    (browsers have NO body-read timeout) with nothing scheduled, no abort and no
    tab-return re-poll: a permanent wedge while the chip claimed "retrying". Now
    the fetch carries the SAME 10 s abort bound the widget has (killer armed
    through the BODY read, cleared only in the ``finally`` — i.e. after
    ``r.json()`` settles) and the visibility gate rides the ``watchActive`` flag
    (set in startAndPoll, cleared in finishLive; the ``pollGen`` token makes a
    re-poll during an in-flight fetch safe). Substring-presence pins (no headless
    JS runner)."""
    html = _render_universe_html()
    poll = _poll_progress_src(html)
    # The abort bound — constant, controller, killer, and the signal wire.
    assert "const FEED_FETCH_TIMEOUT_MS = 10000;" in html
    assert (
        "const ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;"
        in poll
    )
    assert (
        "const killer = ctrl ? setTimeout(() => ctrl.abort(), FEED_FETCH_TIMEOUT_MS) : null;"
        in poll
    )
    assert "if (ctrl) opts.signal = ctrl.signal;" in poll, "the signal wire is the abort's teeth"
    assert "const r = await fetch('/api/v1/universe/pull-progress', opts);" in poll
    # The killer is cleared ONLY in the finally — after r.json() settles — so a
    # stalled BODY aborts too; and the finally is the one clear site.
    assert "} finally {\n    if (killer) clearTimeout(killer);" in poll, (
        "the killer must be cleared only after the body read settles"
    )
    assert poll.count("clearTimeout(killer)") == 1, "exactly ONE clear site (the finally)"
    assert poll.index("await r.json();") < poll.index("} finally {")
    # The visibility recovery gate rides watchActive, never the in-flight-null pollTimer.
    assert "let watchActive = false;" in html
    assert (
        "if (!document.hidden && watchActive && lastPollLabel !== null) pollProgress(lastPollLabel);"
        in html
    ), "the visibility re-poll must gate on the watch-active flag"
    # The label STAMP inside pollProgress is the gate's ONLY non-null source
    # (refuter finding, round 3: it was unpinned — deleting it left the gate's
    # `lastPollLabel !== null` permanently false, killing the tab-return re-poll
    # with the suite green; the other writes are the null declaration +
    # startAndPoll's null reset).
    assert "lastPollLabel = label;" in poll, (
        "pollProgress must stamp the label — without it tab-return recovery is dead"
    )
    assert "pollTimer && lastPollLabel" not in html, (
        "the old pollTimer gate blocked tab-return recovery while a poll was in flight"
    )
    assert "watchActive = true;" in _start_and_poll_src(html), (
        "the watch-active gate must open at watch start"
    )
    assert "watchActive = false;" in _finish_live_src(html), (
        "the watch-active gate must close on settle"
    )


def test_regression_universe_hung_start_post_shows_stale() -> None:
    """REGRESSION (refuter-confirmed): ``startAndPoll``'s start POST ran BEFORE any
    staleness machinery existed — ``pollProgress`` had not run, so the ticker never
    started and a wedged POST left "Starting …" painted indefinitely with ZERO
    stale indication. The ticker now starts BEFORE the POST, so the watch-start
    baseline escalates the #feedage chip amber → red STALE if the POST hangs.
    HONEST envelope: indication only — the POST itself is deliberately NOT aborted
    (a slow-but-successful start would be mislabelled failed). The in-page remedy
    in this mode is a RELOAD, then re-click — the one pipeline button stays
    disabled while the POST is wedged (round 4), and a fresh page's enabled
    button re-attaches safely through the single-flight route."""
    html = _render_universe_html()
    start = _start_and_poll_src(html)
    assert "startFeedAgeTicker();" in start, "the ticker must start inside startAndPoll"
    assert start.index("startFeedAgeTicker();") < start.index(
        "await fetch(path, {method: 'POST'})"
    ), "the ticker must start BEFORE the start POST so a wedged POST reads STALE"


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
    # The pinned wording survives as EXECUTABLE chip text — asserted as the full JS
    # string literal INSIDE the extracted renderFeedAge body, so a comment mention
    # can never satisfy this pin (refuter finding, round 3: the bare substring was
    # satisfied by a block comment this same PR introduced, so deleting the chip's
    # actual wording kept the suite green).
    assert "'live progress feed dropped — retrying'" in _render_feed_age_src(html), (
        "the pinned wording must survive as executable text inside the stale chip"
    )
    assert html.count("'live progress feed dropped — retrying'") == 1, (
        "exactly ONE executable site — the never-give-up stale chip tail"
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
    # The amber wording splits on hadProgressSuccess (round 3 — see the dedicated
    # regression test); its two honest forms + the red prefix are pinned here.
    assert "'updated ' + ageS + 's ago'" in html
    assert "'no update yet (' + ageS + 's)'" in html
    assert "'⚠ STALE — no live update for ' + ageS + 's · '" in html
    assert "function renderFeedAge()" in html
    assert "setInterval(renderFeedAge, 1000)" in html
    # The universe surface's MONOTONIC client clock (refuter finding: only the
    # widget's nowMs was pinned — the universe twin had no pin at all).
    assert (
        "const nowMs = () => (typeof performance !== 'undefined' && performance.now)"
        " ? performance.now() : Date.now();" in html
    ), "the universe monotonic client clock is missing"
    # The VISIBILITY wiring (refuter finding: `.chip.stale` is display:none and only
    # these toggles reveal it — deleting them left the chip's textContent written but
    # permanently invisible, resurrecting the zero-indication outage with the suite
    # green). Both classList writes + the null-clear branch are load-bearing.
    assert "chip.classList.toggle('on', warn || red);" in html, (
        "the .on toggle is the ONLY thing that reveals the display:none chip"
    )
    assert "chip.classList.toggle('red', red);" in html, "the red escalation toggle is missing"
    assert (
        "if (lastProgressOkMs === null) { chip.classList.remove('on', 'red');"
        " chip.textContent = ''; return; }" in html
    ), "the null-clear branch (settled page never reads STALE) is missing"


def test_regression_universe_feedage_escalation_comparisons_pinned() -> None:
    """REGRESSION (refuter-confirmed, round 4 — MAJOR): ``renderFeedAge``'s
    ``const red = age >= FEED_STALE_RED_MS;`` / ``const warn = age >=
    FEED_STALE_WARN_MS;`` are the wires that gate the ENTIRE #feedage feature —
    ``.chip.stale`` is display:none and only ``classList.toggle('on', warn ||
    red)`` reveals it — yet no test asserted either comparison: only the constant
    DECLARATIONS were pinned, so mutating both to ``false`` (or ``>=`` to ``<``)
    kept the whole suite green while the chip never appeared — resurrecting the
    exact 2026-07-06 zero-indication incident on the /universe surface. The
    widget's twin comparisons were already pinned verbatim; these mirror them
    inside the extracted ``renderFeedAge`` body. Substring-presence pins (no
    headless JS runner)."""
    html = _render_universe_html()
    feed_age = _render_feed_age_src(html)
    assert "const red = age >= FEED_STALE_RED_MS;" in feed_age, (
        "the red escalation comparison is the chip's load-bearing wire"
    )
    assert "const warn = age >= FEED_STALE_WARN_MS;" in feed_age, (
        "the warn escalation comparison is the chip's load-bearing wire"
    )


def test_regression_age_clocks_suspend_guard_dual_clock() -> None:
    """REGRESSION (refuter-confirmed, round 4): both surfaces measured age off
    ``performance.now()`` alone, which on some browser/OS combinations may NOT
    advance while the machine is suspended (implementation variance — stated as
    such, unverified against current builds, per Golden Rule #1). Scenario: the
    laptop sleeps mid-pull for an hour; on wake the age resumes from the
    pre-sleep delta, so an unreachable server reads "updated 3s ago" against
    hour-old numbers for up to a full warn threshold — a bounded recurrence of
    the stale-illusion class this feature exists to kill. Both surfaces now ALSO
    stamp a ``Date.now()`` wall twin on every success and report
    ``max(monotonicAge, wallAge)`` — a wall-clock jump can only OVER-report
    staleness, the safe direction for a staleness indicator (monotonic stays
    primary, so NTP-jump immunity is preserved on the common path).
    Substring-presence pins (no headless JS runner)."""
    js = _widget_js()
    # Widget: wall-twin state + stamps + the max() fold in renderAge.
    assert "var lastPollOkWallMs = null;" in js, "the widget wall-twin state is missing"
    assert "var bootWallMs = null;" in js, "the widget boot wall twin is missing"
    assert "lastPollOkWallMs = Date.now();" in js, "success must stamp the wall twin"
    assert "bootWallMs = Date.now();" in js, "start() must stamp the boot wall twin"
    assert "var wallRef = lastPollOkWallMs !== null ? lastPollOkWallMs : bootWallMs;" in js, (
        "the wall reference must fall back to the boot wall baseline"
    )
    assert "if (wallRef !== null) { age = Math.max(age, Date.now() - wallRef); }" in js, (
        "renderAge must fold in the wall age — max() is the suspend guard's teeth"
    )
    # Universe: the same dual-clock on the #feedage chip.
    html = _render_universe_html()
    assert "let lastProgressOkWallMs = null;" in html, "the universe wall-twin state is missing"
    poll = _poll_progress_src(html)
    assert "lastProgressOkWallMs = Date.now();" in poll, (
        "the universe success path must stamp the wall twin"
    )
    feed_age = _render_feed_age_src(html)
    assert (
        "if (lastProgressOkWallMs !== null) age = Math.max(age, Date.now() - lastProgressOkWallMs);"
        in feed_age
    ), "renderFeedAge must fold in the wall age — max() is the suspend guard's teeth"


def test_regression_universe_amber_chip_claimed_update_that_never_arrived() -> None:
    """REGRESSION (refuter-confirmed, round 3): the amber branch rendered
    "updated Xs ago — retrying" off ``lastProgressOkMs``, but ``startFeedAgeTicker``
    seeds that stamp with the WATCH-START baseline, not a success — so a watch
    whose progress endpoint 500s/stalls from the very FIRST poll read
    "updated 7s ago" although no payload was EVER received, telling the operator
    fresh data had arrived. The chip now claims "updated Xs ago" ONLY after a
    validated snapshot landed THIS watch (``hadProgressSuccess``); before that it
    reads "no update yet (Xs)" — mirroring the widget's honest "no data yet"
    split. Substring-presence pins (no headless JS runner)."""
    html = _render_universe_html()
    assert "let hadProgressSuccess = false;" in html, "the wording flag is missing"
    # The flag flips ONLY on the validated-success path inside pollProgress, AFTER
    # the shape guard (a malformed body is never a success) …
    poll = _poll_progress_src(html)
    assert "hadProgressSuccess = true;" in poll, "the success path must flip the flag"
    assert poll.index("throw new Error('malformed snapshot')") < poll.index(
        "hadProgressSuccess = true;"
    ), "the flag must flip only AFTER the shape guard"
    # … and resets at watch start + settle, so it never leaks across watches.
    assert "hadProgressSuccess = false;" in _start_and_poll_src(html), (
        "a new watch must start with the flag down"
    )
    assert "hadProgressSuccess = false;" in _stop_feed_age_ticker_src(html), (
        "a settled watch must drop the flag"
    )
    # The amber branch splits on the flag …
    feed_age = _render_feed_age_src(html)
    assert (
        "(hadProgressSuccess ? 'updated ' + ageS + 's ago' : 'no update yet (' + ageS + 's)')"
        in feed_age
    ), "the amber wording must split on whether a snapshot ever arrived this watch"
    # … and the old unconditional claim is gone (the regression pin).
    assert "'updated ' + ageS + 's ago — retrying'" not in html, (
        "the unconditional 'updated Xs ago' amber literal must be gone"
    )


def test_regression_universe_stale_chip_claimed_retrying_during_hung_start_post() -> None:
    """REGRESSION (refuter-confirmed, rounds 3 + 4): during a hung/wedged start POST
    the poll chain has never started — ``pollProgress`` was never invoked,
    ``lastPollLabel`` is null (the visibility gate is closed), the POST is
    deliberately un-aborted — yet the red branch unconditionally appended
    "· retrying", telling the operator to wait for a retry that would NEVER come
    (round 3). Round 4 then confirmed the round-3 replacement tail ("re-click to
    re-attach") named a remedy the page itself BLOCKS: ``startAndPoll`` calls
    ``setBulkDisabled(true)`` at entry and the ONLY re-enable site is
    ``finishLive()`` — which never runs while the POST is wedged — so the one
    pipeline button is greyed out and an in-page re-click is impossible. The tail
    now names the actionable remedy: RELOAD the page, then re-click (a fresh
    page's enabled button re-attaches safely through the single-flight route);
    only a LIVE poll chain WITH a counted failure claims "retrying" (the round-5
    failure gate — see
    test_regression_universe_claimed_retrying_during_slow_successful_poll).
    Substring-presence pins (no headless JS runner)."""
    html = _render_universe_html()
    feed_age = _render_feed_age_src(html)
    assert "const tail = (lastPollLabel === null)" in feed_age, (
        "the tail must branch on whether the poll chain is live"
    )
    assert "? 'start request pending — reload the page, then re-click to re-attach'" in feed_age
    assert (
        ": (pollFeedRetries > POLL_MAX_FEED_RETRIES"
        " ? 'live progress feed dropped — retrying' : 'retrying'));" in feed_age
    ), "a live poll chain with counted failures keeps the retrying/feed-dropped tails"
    # BOTH escalation branches carry the computed tail — never a hardcoded
    # "retrying" that could overstate again.
    assert (
        "chip.textContent = '⚠ STALE — no live update for ' + ageS + 's · ' + tail;" in feed_age
    ), "the red branch must render the state-honest tail"
    assert "+ ' — ' + tail;" in feed_age, "the amber branch must render the state-honest tail"
    # The start-pending wording has exactly ONE executable site (renderFeedAge) —
    # a comment double can never satisfy the pins above.
    assert html.count("'start request pending — reload the page, then re-click to re-attach'") == 1
    # The round-3 bare-re-click literal is GONE (round-4 regression pin: the button
    # is disabled in exactly this mode, so "re-click" was unactionable advice).
    assert "'start request pending — re-click to re-attach'" not in html, (
        "the unactionable bare-re-click remedy must be gone — the pipeline button is"
        " disabled while the start POST is wedged"
    )


def test_regression_universe_claimed_retrying_during_slow_successful_poll() -> None:
    """REGRESSION (refuter-confirmed, round 5 — the universe twin of the widget's
    slow-poll finding): the live-chain tail arm read 'retrying' for EVERY
    ``pollFeedRetries <= POLL_MAX_FEED_RETRIES`` — including ``0``. Scenario: a
    poll issued at t=0 responds successfully at t=8 s (inside the 10 s abort
    bound, past the 5 s warn threshold) — from t=5 to t=8 the chip read
    "updated Xs ago — retrying" while the attempt was still IN FLIGHT and
    nothing had failed or retried. (The widget needs a 7-10 s response to hit
    its 10 s warn; the universe's 5 s warn opens the wider 5-10 s band.) The
    tail now carries a third state-honest arm: ZERO counted failures =>
    'awaiting response' (the in-flight truth); the quick-retry 'retrying' and
    'live progress feed dropped — retrying' arms survive unchanged BEHIND the
    failure gate. Escalation itself was always honest (the data IS that old) —
    this pins the honest action word. Substring-presence pins (no headless JS
    runner)."""
    html = _render_universe_html()
    feed_age = _render_feed_age_src(html)
    # The zero-failure in-flight arm …
    assert ": (pollFeedRetries === 0" in feed_age, (
        "the tail must gate 'retrying' on a counted failure"
    )
    assert "? 'awaiting response'" in feed_age, "the zero-failure in-flight arm is missing"
    # … the retrying/feed-dropped arms survive BEHIND the gate …
    assert (
        ": (pollFeedRetries > POLL_MAX_FEED_RETRIES"
        " ? 'live progress feed dropped — retrying' : 'retrying'));" in feed_age
    ), "the quick-retry + feed-dropped arms must survive behind the failure gate"
    # … and the failure-blind two-arm tail is gone (the regression pin: its
    # closing `: 'retrying');` cannot match the gated `: 'retrying'));` form).
    assert (
        "POLL_MAX_FEED_RETRIES ? 'live progress feed dropped — retrying' : 'retrying');" not in html
    ), "the failure-blind two-arm live tail must be gone"


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
    assert (
        "if (lastProgressOkMs === null) { lastProgressOkMs = nowMs();"
        " lastProgressOkWallMs = Date.now(); }" in html
    ), "the watch-start baseline is missing — a first-poll failure must still escalate"
    # The ticker must actually be STARTED from the poll loop (refuter finding: the
    # invocation was unpinned — deleting it meant the 1 s clock never ran and the
    # chip never escalated, with the suite green).
    assert "startFeedAgeTicker();" in poll, (
        "pollProgress must start the 1 s age ticker on every watch"
    )
    # stopFeedAgeTicker's chip-CLEAR wires (refuter finding, round 4: only the
    # stopFeedAgeTicker() CALL and renderFeedAge's null-clear BRANCH were pinned —
    # deleting the nulling or the trailing renderFeedAge() left a watch that
    # settled while red painted "⚠ STALE …" PERMANENTLY on the settled page,
    # ticker stopped, DOM never rewritten, suite green).
    stop = _stop_feed_age_ticker_src(html)
    assert "lastProgressOkMs = null;" in stop, (
        "settle must null the staleness stamp so renderFeedAge takes the clear branch"
    )
    assert "lastProgressOkWallMs = null;" in stop, "settle must null the wall twin too"
    assert "renderFeedAge();   // clears the chip" in stop, (
        "settle must call renderFeedAge once more — the write that actually clears the DOM"
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
    # The tracker→header LINK (refuter finding: only the DEFINITIONS were pinned —
    # `const subNote = '';` fully resurrected the invisible-giant-unit bug with the
    # suite green). Pin the derivation call, the #subunit write (its '· ' prefix
    # differs from the header's ' · ', so the count pin above does not cover it),
    # and finishLive's clear.
    poll = _poll_progress_src(html)
    assert "const subNote = subUnitNote(s, trackSubUnit(s));" in poll, (
        "the derivation call is the tracker→header link — without it nothing moves"
    )
    assert "if (suEl) suEl.textContent = subNote ? '· ' + subNote : '';" in poll, (
        "the #subunit counter write is missing"
    )
    finish = _finish_live_src(html)
    assert "const suEl = document.getElementById('subunit');" in finish
    assert "if (suEl) suEl.textContent = '';" in finish, (
        "a settled watch must clear the #subunit counter"
    )
    # The counting CORE (refuter finding, round 3: it was unpinned — deleting the
    # Set-branch increment kept the whole suite green while the counter never left
    # 0 below the 4096 cap, resurrecting this exact bug; deleting the per-unit
    # reset body made the count span units, a misleading cross-unit total). All
    # three load-bearing branches are pinned INSIDE the extracted trackSubUnit body.
    track = _track_sub_unit_src(html)
    assert "if (subUnitAnchor !== anchor) {" in track, "the per-unit reset branch is missing"
    assert (
        "subUnitSeen = new Set(); subUnitCount = 0; subUnitAnchor = anchor; subUnitLastCur = '';"
        in track
    ), "the reset body must clear the Set + count and re-anchor"
    assert "if (cur && !subUnitSeen.has(cur))" in track, "the distinct-label gate is missing"
    assert (
        "if (subUnitSeen.size < SUBUNIT_SET_CAP) { subUnitSeen.add(cur); subUnitCount += 1; }"
        in track
    ), "the below-cap increment is the counter's heart — without it the count never leaves 0"


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
    # The CE/PE/FUT TYPE gates the docstring claims (refuter finding: they were
    # unpinned — only the length/index math was asserted).
    assert (
        "if ((t === 'CE' || t === 'PE') && parts.length >= 5) tok = parts[parts.length - 3];"
        in html
    ), "the option (CE/PE) type gate is missing"
    assert "else if (t === 'FUT' && parts.length >= 4) tok = parts[parts.length - 2];" in html, (
        "the future (FUT) type gate is missing"
    )


def test_universe_subunit_resets_per_unit_and_per_pull() -> None:
    """The tracker resets on ANY completed/total movement (the unit boundary,
    covering frozen-total AND grow-total phases) and on watch start + settle.
    Location-AWARE pins (refuter finding: the old ``count >= 2`` was location-blind
    — both calls could sit anywhere and the assert message overclaimed)."""
    html = _render_universe_html()
    assert "String(s.completed || 0) + '/' + String(s.total || 0)" in html
    assert "function resetSubUnit()" in html
    assert "resetSubUnit();" in _start_and_poll_src(html), (
        "resetSubUnit must run at watch start (startAndPoll)"
    )
    assert "resetSubUnit();" in _finish_live_src(html), (
        "resetSubUnit must run at settle (finishLive)"
    )
    # The anchor-change RESET BRANCH itself (refuter finding, round 3: this test's
    # docstring claimed "resets on ANY completed/total movement" but pinned only
    # the anchor STRING + the watch-boundary resets — deleting the branch made
    # "sub-unit N this unit" display a cross-unit total with the suite green).
    track = _track_sub_unit_src(html)
    assert "if (subUnitAnchor !== anchor) {" in track, (
        "the tracker must reset when the completed/total anchor moves"
    )
    assert "subUnitAnchor = anchor;" in track, "the reset must re-anchor to the new unit"


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
    # The whitelist ENFORCEMENT call (refuter finding, round 4: the regex TEXT and
    # the `? tok : null` shape were pinned but not the `.test(` invocation —
    # mutating the line to `return tok ? tok : null;` kept both pins green while a
    # malformed 5-token label like `NSE-FOO-BAR-BAZ-CE 1minute` painted
    # "expiry BAR" in the header, contradicting this test's own docstring).
    assert "return tok && EXPIRY_TOKEN_RE.test(tok) ? tok : null;" in html, (
        "the EXPIRY_TOKEN_RE whitelist must actually be APPLIED to the token"
    )
