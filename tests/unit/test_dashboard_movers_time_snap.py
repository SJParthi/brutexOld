"""The /movers persistent as-of instant + bucket-snap law (DEC-MOVERS-TIME-SNAP-UX-001).

FAILS-BEFORE (operator-reported: changing the TIMEFRAME reset the selected
TIME). Two reset producers, both captured VERBATIM by the DOM-shim harness
(scratchpad ``repro/harness.js``, run 2026-07-09 against the real templates):

* **current main** — ``bxResolveTime``'s not-a-slot ``""`` fallback: picking
  15:29 on 1minute then switching to 3minute yielded
  ``state.time="" · dropdown=Latest · request=(no time param)`` (harness case
  A1, verbatim) — the pick silently reset to Latest.
* **pre-#1243 (the operator's observed 09:15)** — the old ``fillTimes()``
  pinned ``state.time = sel.value`` after an innerHTML rebuild, and a rebuilt
  ``<select>`` auto-selects its FIRST option → ``09:15``. That page could
  still reach the operator's browser because the ``/movers`` route shipped NO
  ``Cache-Control`` header (fixed here with the ``/db`` ``no-store``
  precedent). Honest envelope: harness-proven against the old template text,
  not CI-executable against the retired git ref.

THE FIX (this suite's teeth): ONE persistent ``state.instant`` (minute-of-day;
written ONLY by an explicit pick) snapped per TF switch to the CONTAINING
bucket of the new timeframe under the ONE mapping law::

    bucketStart(t, m) = 555 + floor((clamp(t, 555, 929) - 555) / m) * m
    bucketFinal(t, m) = min(bucketStart(t, m) + m - 1, 929)

Each dropdown option: label = HH:MM(bucketStart) (the /db row label), value =
HH:MM(bucketFinal) (the SENT ``?time=`` cutoff) — so an explicit pick serves
the COMPLETED /db-equal bar WHENEVER the lake holds the bucket in full (the
backend cut is inclusive-<= on the raw 1-minute frame BEFORE resampling),
killing the operator's 24360.00-vs-24355.1 partial-cut confusion at the root.
HONEST LIMIT (envelope-pinned below): a pick whose bucket-final lies BEYOND
the day's last stored minute (a day incomplete in the DB — pull still in
progress / truncated day) serves exactly the data stored so far instead,
undetectable client-side — ``latest_time`` is echoed only on Latest requests
(named follow-up DEC-MOVERS-EXPLICIT-PICK-LATEST-ECHO-001). Per the
operator's FINAL stored-data-only spec the "forming bar" concept is CUT as a
feature: the Latest label's residual disclosure speaks ONLY about the stored
data ("data through HH:MM — day incomplete in DB"), never a "forming" bar.

House idiom: node-executed goldens run the REAL shipped ``bx-movers-time.js``
(and the REAL ``fillTimes()`` / handler lines extracted verbatim from
``movers.html`` under a faithful ``<select>`` shim) cross-checked against the
Python ``movers_snapshot`` primitives; substring pins assert PRESENCE of the
wiring — no headless-browser execution is claimed (honest envelope).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest
from brutex.dashboard.movers_snapshot import (
    compute_spot_candidates,
    interval_minutes,
    minute_to_hhmm,
    session_bar_times,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

_DASHBOARD = Path(__file__).resolve().parents[2] / "brutex" / "dashboard"
_STATIC_JS = _DASHBOARD / "static" / "bx-movers-time.js"
_MOVERS = _DASHBOARD / "templates" / "movers.html"
_DB = _DASHBOARD / "templates" / "db.html"

_OPEN = 9 * 60 + 15  # 555 — 09:15 IST session open
_LAST = 15 * 60 + 30 - 1  # 929 — 15:29, the last session minute

#: The 20 intraday timeframes (1day = 375 is the daily floor, no time choice).
_INTRADAY_LABELS = (
    "1minute",
    "2minute",
    "3minute",
    "4minute",
    "5minute",
    "6minute",
    "7minute",
    "8minute",
    "9minute",
    "10minute",
    "11minute",
    "12minute",
    "13minute",
    "14minute",
    "15minute",
    "30minute",
    "1hour",
    "2hour",
    "3hour",
    "4hour",
)

#: The machine-verified 20-TF golden (the spec table of
#: DEC-MOVERS-TIME-SNAP-UX-001): minutes → (slot count, last label, last
#: bucket width, last "15:29" value _stored_serves_cutoff-aligned?). The last
#: VALUE is ALWAYS "15:29"; alignment holds iff 375 % m == 0 (m ∈ {1,3,5,15}).
_TF_GOLDEN: dict[int, tuple[int, str, int, bool]] = {
    1: (375, "15:29", 1, True),
    2: (188, "15:29", 1, False),
    3: (125, "15:27", 3, True),
    4: (94, "15:27", 3, False),
    5: (75, "15:25", 5, True),
    6: (63, "15:27", 3, False),
    7: (54, "15:26", 4, False),
    8: (47, "15:23", 7, False),
    9: (42, "15:24", 6, False),
    10: (38, "15:25", 5, False),
    11: (35, "15:29", 1, False),
    12: (32, "15:27", 3, False),
    13: (29, "15:19", 11, False),
    14: (27, "15:19", 11, False),
    15: (25, "15:15", 15, True),
    30: (13, "15:15", 15, False),
    60: (7, "15:15", 15, False),
    120: (4, "15:15", 15, False),
    180: (3, "15:15", 15, False),
    240: (2, "13:15", 135, False),
}


def _hhmm_to_min(hhmm: str) -> int:
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)


def _node(script: str) -> object:
    proc = subprocess.run(  # nosec B603 — fixed argv, test-only
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(proc.stdout)


def _run_js(expr: str) -> object:
    return _node(
        f"const t = require({json.dumps(str(_STATIC_JS))});console.log(JSON.stringify({expr}));"
    )


node_required = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


# ── the DOM-shim scenario flip (the operator's bug, fixed) ────────────────


def _extract(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    assert match is not None, f"movers.html no longer carries {pattern!r}"
    return match.group(0)


def _scenario_script() -> str:
    """Compose the node scenario program from the REAL shipped sources.

    Extracts — VERBATIM — movers.html's ``const state = {...}``, ``TF_MIN``,
    ``fillTimes()`` and the two control ``onchange`` handler lines, and runs
    them against the real ``bx-movers-time.js`` under a faithful ``<select>``
    shim (an innerHTML rebuild auto-selects the FIRST option, exactly like a
    browser — the pre-#1243 09:15-reset mechanism; ``.value =`` with no
    matching option leaves ``selectedIndex = -1`` → ``""``).
    """
    html = _MOVERS.read_text(encoding="utf-8")
    state_src = _extract(r"const state = \{.*?\};", html)
    tf_min_src = _extract(r"const TF_MIN = \{[\s\S]*?\};", html)
    fill_src = _extract(r"function fillTimes\(\) \{[\s\S]*?\n\}", html)
    interval_handler = _extract(r'\$\("interval"\)\.onchange = .*;', html)
    time_handler = _extract(r'\$\("time"\)\.onchange = .*;', html)
    return f"""
"use strict";
const BXMoversTime = require({json.dumps(str(_STATIC_JS))});
/* faithful <select> shim */
function makeSelect() {{
  const el = {{ options: [], selectedIndex: -1, disabled: false }};
  Object.defineProperty(el, "innerHTML", {{
    set(html) {{
      el.options = [];
      const re = /<option(?:\\s+value="([^"]*)")?\\s*>([^<]*)<\\/option>/g;
      let m;
      while ((m = re.exec(html)) !== null)
        el.options.push({{ value: m[1] !== undefined ? m[1] : m[2], text: m[2] }});
      el.selectedIndex = el.options.length ? 0 : -1;
    }},
  }});
  Object.defineProperty(el, "value", {{
    get() {{ return el.selectedIndex >= 0 ? el.options[el.selectedIndex].value : ""; }},
    set(v) {{ el.selectedIndex = el.options.findIndex((o) => o.value === v); }},
  }});
  return el;
}}
const dom = {{ interval: {{ value: "1day", onchange: null }}, time: makeSelect() }};
const $ = (id) => dom[id];
const load = () => {{}};
{state_src}
{tf_min_src}
{fill_src}
{interval_handler}
{time_handler}
function switchTF(tf) {{ dom.interval.value = tf; dom.interval.onchange(); }}
function pickLabel(label) {{
  const i = dom.time.options.findIndex((o) => o.text === label);
  if (i === -1) throw new Error("no option labeled " + label);
  dom.time.selectedIndex = i;
  dom.time.onchange();
}}
function snap() {{
  const sel = dom.time.selectedIndex >= 0 ? dom.time.options[dom.time.selectedIndex] : null;
  return {{
    time: state.time,
    instant: state.instant,
    label: sel ? sel.text : null,
    disabled: dom.time.disabled,
  }};
}}
const out = {{}};
/* A — the operator's exact example: 1min@15:29 → 3min → 5min → 1min */
fillTimes();
switchTF("1minute"); pickLabel("15:29");
out.A0 = snap();
switchTF("3minute"); out.A1 = snap();
switchTF("5minute"); out.A2 = snap();
switchTF("1minute"); out.A3 = snap();
/* C — pick whose label is on neither grid boundary: 5min label 09:20 (value 09:24) */
switchTF("5minute"); pickLabel("09:20");
out.C0 = snap();
switchTF("3minute"); out.C1 = snap();
switchTF("5minute"); out.C2 = snap();
/* D — round trip THROUGH 1day (the daily branch must not wipe the instant) */
switchTF("1minute"); pickLabel("12:34");
out.D0 = snap();
switchTF("1day"); out.D1 = snap();
switchTF("1minute"); out.D2 = snap();
/* E3 — Latest stays Latest across every walk (explicit Latest pick) */
switchTF("3minute"); pickLabel("Latest");
out.E3a = snap();
switchTF("5minute"); out.E3b = snap();
switchTF("3hour"); out.E3c = snap();
/* E6 — 2minute width-1 truncated last bucket (label 15:29) is stable */
switchTF("2minute"); pickLabel("15:29");
out.E60 = snap();
switchTF("3minute"); out.E61 = snap();
switchTF("2minute"); out.E62 = snap();
/* F — coarse→coarse where neither grid contains the other: 3hour@12:15 → 4hour */
switchTF("3hour"); pickLabel("12:15");
out.F0 = snap();
switchTF("4hour"); out.F1 = snap();
switchTF("3hour"); out.F2 = snap();
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def out() -> dict[str, dict[str, object]]:
    """One node run of the scenario program (skipped when node is absent)."""
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    result = _node(_scenario_script())
    assert isinstance(result, dict)
    return result


@node_required
class TestFillTimesScenarioFlip:
    """The REAL fillTimes + handlers, scenario-driven — the fails-before flip.

    On current-main sources case A1 was ``state.time="" · dropdown=Latest``
    (captured verbatim — the Latest reset); on the pre-#1243 template it was
    ``09:15`` (first-option autoselect). Both are dead: every case below pins
    persist + containing-bucket snap.
    """

    def test_a_operator_example_1min_1529_snaps_across_tfs(self, out: dict) -> None:
        assert out["A0"] == {"time": "15:29", "instant": 929, "label": "15:29", "disabled": False}
        # → 3minute: the COMPLETED 15:27-labeled bucket, sending its final 15:29.
        assert out["A1"] == {"time": "15:29", "instant": 929, "label": "15:27", "disabled": False}
        assert out["A2"] == {"time": "15:29", "instant": 929, "label": "15:25", "disabled": False}
        # round-trip 1→3→5→1min preserves the instant exactly.
        assert out["A3"] == {"time": "15:29", "instant": 929, "label": "15:29", "disabled": False}

    def test_c_mid_grid_pick_snaps_both_directions(self, out: dict) -> None:
        # 5minute label 09:20 SENDS its bucket-final 09:24 (the /db-equal bar).
        assert out["C0"] == {"time": "09:24", "instant": 564, "label": "09:20", "disabled": False}
        # → 3minute: the bucket containing 09:24 is labeled 09:24, final 09:26.
        assert out["C1"] == {"time": "09:26", "instant": 564, "label": "09:24", "disabled": False}
        # → back to 5minute: the original slot restored.
        assert out["C2"] == {"time": "09:24", "instant": 564, "label": "09:20", "disabled": False}

    def test_d_instant_survives_a_1day_round_trip(self, out: dict) -> None:
        assert out["D0"] == {"time": "12:34", "instant": 754, "label": "12:34", "disabled": False}
        # 1day: no time param sent, dropdown disabled — semantics kept …
        assert out["D1"] == {"time": "", "instant": 754, "label": "(daily)", "disabled": True}
        # … and the instant SURVIVES: back on 1minute the pick is restored.
        assert out["D2"] == {"time": "12:34", "instant": 754, "label": "12:34", "disabled": False}

    def test_e3_latest_stays_latest_never_overridden(self, out: dict) -> None:
        for key in ("E3a", "E3b", "E3c"):
            row = out[key]
            assert row["time"] == "" and row["instant"] is None, key
            assert row["label"] == "Latest", key

    def test_e6_width_one_truncated_bucket_is_stable(self, out: dict) -> None:
        assert out["E60"] == {"time": "15:29", "instant": 929, "label": "15:29", "disabled": False}
        assert out["E61"] == {"time": "15:29", "instant": 929, "label": "15:27", "disabled": False}
        assert out["E62"] == {"time": "15:29", "instant": 929, "label": "15:29", "disabled": False}

    def test_f_coarse_to_coarse_containing_bucket(self, out: dict) -> None:
        # 3hour label 12:15 sends its final 15:14; 4hour's containing bucket is
        # 13:15 (final 15:29 — session-truncated); back restores 12:15/15:14.
        assert out["F0"] == {"time": "15:14", "instant": 914, "label": "12:15", "disabled": False}
        assert out["F1"] == {"time": "15:29", "instant": 914, "label": "13:15", "disabled": False}
        assert out["F2"] == {"time": "15:14", "instant": 914, "label": "12:15", "disabled": False}

    def test_never_reset_to_latest_or_0915(self, out: dict) -> None:
        # The two captured reset targets, dead across every explicit-pick case:
        # never "" (the current-main Latest reset), never a first-slot 09:15
        # (the pre-#1243 reset) for picks made away from the first bucket.
        for key in ("A1", "A2", "A3", "C1", "C2", "E61", "E62", "F1", "F2"):
            row = out[key]
            assert row["time"] not in ("", "09:15"), (key, row)
            assert row["label"] != "09:15", (key, row)


# ── node goldens vs the Python oracle (no new Python surface) ─────────────


@pytest.fixture(scope="module")
def pairs_by_m() -> dict[str, list[dict[str, str]]]:
    """One node run collecting timeOptionPairs for every TF + daily + junk."""
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    minutes = sorted(_TF_GOLDEN)
    script = (
        f"const t = require({json.dumps(str(_STATIC_JS))});"
        f"const out = {{}};"
        f"for (const m of {json.dumps(minutes)}) out[m] = t.timeOptionPairs(m);"
        f'out["375"] = t.timeOptionPairs(375); out["junk"] = t.timeOptionPairs(undefined);'
        f"console.log(JSON.stringify(out));"
    )
    result = _node(script)
    assert isinstance(result, dict)
    return result


@node_required
class TestTimeOptionPairsGolden:
    def test_labels_equal_python_session_bar_times(self, pairs_by_m: dict) -> None:
        # The option LABELS are exactly the /db bucket-START grid — the Python
        # session_bar_times single source of truth, all 20 intraday TFs.
        for interval in _INTRADAY_LABELS:
            m = interval_minutes(interval)
            assert m is not None
            labels = [p["label"] for p in pairs_by_m[str(m)]]
            assert labels == session_bar_times(interval), interval

    def test_values_are_bucket_finals_last_always_1529(self, pairs_by_m: dict) -> None:
        for m_str, pairs in pairs_by_m.items():
            if m_str in ("375", "junk"):
                continue
            m = int(m_str)
            for i, pair in enumerate(pairs):
                start = _hhmm_to_min(pair["label"])
                expected = minute_to_hhmm(min(start + m - 1, _LAST))
                assert pair["value"] == expected, (m, pair)
                is_last = i == len(pairs) - 1
                if is_last:
                    assert pair["value"] == "15:29", m  # universal invariant
                else:
                    # every NON-last value is _stored_serves_cutoff-aligned:
                    # (cutoff − 555 + 1) % m == 0 → the stored fast path.
                    assert (_hhmm_to_min(pair["value"]) - _OPEN + 1) % m == 0, (m, pair)

    def test_the_20_tf_table_golden(self, pairs_by_m: dict) -> None:
        for m, (slots, last_label, last_width, aligned) in _TF_GOLDEN.items():
            pairs = pairs_by_m[str(m)]
            assert len(pairs) == slots, m
            last = pairs[-1]
            assert last["label"] == last_label, m
            width = _LAST - _hhmm_to_min(last["label"]) + 1
            assert width == last_width, m
            assert ((_LAST - _OPEN + 1) % m == 0) is aligned, m

    def test_values_are_distinct_selectable(self, pairs_by_m: dict) -> None:
        # <select>.value = snapped must select exactly ONE option.
        for m_str, pairs in pairs_by_m.items():
            if m_str in ("375", "junk"):
                continue
            values = [p["value"] for p in pairs]
            assert len(values) == len(set(values)), m_str

    def test_daily_and_unknown_yield_empty(self, pairs_by_m: dict) -> None:
        assert pairs_by_m["375"] == []
        assert pairs_by_m["junk"] == []


@node_required
class TestSnapLawProperty:
    def test_snap_law_all_20_tfs_every_session_minute(self) -> None:
        # For every intraday TF × every instant 555..929: the snapped value is
        # the containing bucket's final; the bucket CONTAINS the instant; the
        # snap is a fixed point (snapping the sent value re-yields itself);
        # the sent value is always a dropdown option value.
        minutes = sorted(_TF_GOLDEN)
        script = f"""
const t = require({json.dumps(str(_STATIC_JS))});
const bad = [];
for (const m of {json.dumps(minutes)}) {{
  const values = new Set(t.timeOptionPairs(m).map((p) => p.value));
  for (let i = 555; i <= 929; i++) {{
    const start = t.bucketStart(i, m), fin = t.bucketFinal(i, m);
    const sent = t.snapTime(i, m);
    if (sent !== t.minuteToHHMM(Math.min(start + m - 1, 929))) bad.push([m, i, "law", sent]);
    if (!(start <= i && i <= fin)) bad.push([m, i, "containment", start, fin]);
    if (t.snapTime(t.cutoffMinute(sent), m) !== sent) bad.push([m, i, "fixed-point", sent]);
    if (!values.has(sent)) bad.push([m, i, "not-an-option", sent]);
  }}
}}
console.log(JSON.stringify(bad.slice(0, 5)));
"""
        assert _node(script) == []

    def test_snap_abstains_to_latest(self) -> None:
        assert _run_js("t.snapTime(null, 3)") == ""
        assert _run_js("t.snapTime(undefined, 3)") == ""
        assert _run_js("t.snapTime(929, 375)") == ""  # daily
        assert _run_js("t.snapTime(929, undefined)") == ""  # unknown TF

    def test_out_of_session_instants_clamp(self) -> None:
        assert _run_js("t.snapTime(0, 3)") == "09:17"  # clamps to the first bucket
        assert _run_js("t.snapTime(1439, 3)") == "15:29"  # clamps to the last

    def test_cutoff_minute_round_trips_and_abstains(self) -> None:
        script = f"""
const t = require({json.dumps(str(_STATIC_JS))});
const bad = [];
for (let i = 555; i <= 929; i++)
  if (t.cutoffMinute(t.minuteToHHMM(i)) !== i) bad.push(i);
for (const junk of ["", null, undefined, "junk", "9:5", "aa:bb", "12:345", 42])
  if (t.cutoffMinute(junk) !== null) bad.push(String(junk));
console.log(JSON.stringify(bad));
"""
        assert _node(script) == []


@node_required
class TestRoundTripNeverResetProperty:
    def test_every_ordered_tf_pair_every_slot(self) -> None:
        # For EVERY ordered intraday TF pair (src, dst) with src != dst (a
        # self-pair is the fixed-point law, already pinned above) × EVERY slot
        # of src: pick the slot (instant = its bucket-final) → switch to dst →
        # back to src reproduces the original slot exactly (value AND label),
        # and the dst view is NEVER "" (the Latest reset) — the whole reset bug
        # class, both captured targets, dead by property. Exactly
        # 1,278 slots × 19 destinations = 24,282 cases in one node run,
        # EXACT-pinned below against the _TF_GOLDEN slot counts so the ledger's
        # case number can never silently drift from what actually executes.
        script = f"""
const t = require({json.dumps(str(_STATIC_JS))});
const tfs = {json.dumps(sorted(_TF_GOLDEN))};
const pairsOf = {{}};
for (const m of tfs) pairsOf[m] = t.timeOptionPairs(m);
const bad = [];
let cases = 0;
for (const src of tfs) for (const dst of tfs) {{
  if (src === dst) continue;
  for (const p of pairsOf[src]) {{
    cases++;
    const instant = t.cutoffMinute(p.value);
    const atDst = t.snapTime(instant, dst);
    const back = t.snapTime(instant, src);
    if (atDst === "" || atDst === null) bad.push([src, dst, p.value, "reset-to-latest"]);
    if (back !== p.value) bad.push([src, dst, p.value, "round-trip", back]);
  }}
}}
console.log(JSON.stringify({{ cases, bad: bad.slice(0, 5) }}));
"""
        result = _node(script)
        assert isinstance(result, dict)
        assert result["bad"] == []
        # The property genuinely swept the whole grid: every src slot visits
        # every OTHER TF once — Σ slots × (|TFs| − 1), derived (never a magic
        # literal) from the machine-verified golden slot counts = 24,282.
        expected_cases = sum(slots for slots, _, _, _ in _TF_GOLDEN.values()) * (
            len(_TF_GOLDEN) - 1
        )
        assert expected_cases == 24_282  # 1,278 slots × 19 destinations (the ledger number)
        assert result["cases"] == expected_cases


@node_required
class TestStoredCutInfo:
    """The stored-data verdict (the operator's FINAL spec: labels speak only
    about the STORED DATA — the "forming bar" concept is CUT). The note fires
    iff the day is incomplete in the DB (latest stored minute mid-session);
    the completed-bar tail applies only when that minute is also MID-bucket.
    """

    def test_operator_example_3min_at_1527_is_incomplete_mid_bucket(self) -> None:
        info = _run_js("t.storedCutInfo('15:27', 3)")
        assert info == {
            "dayIncomplete": True,
            "midBucket": True,
            "barLabel": "15:27",
            "dataThrough": "15:27",
            "closesAt": "15:29",
        }

    def test_first_bucket_mid_bucket_e10(self) -> None:
        info = _run_js("t.storedCutInfo('09:15', 5)")
        assert info == {
            "dayIncomplete": True,
            "midBucket": True,
            "barLabel": "09:15",
            "dataThrough": "09:15",
            "closesAt": "09:19",
        }

    def test_finalized_day_never_flagged_e11(self) -> None:
        # latest 15:29 → final == 929 on EVERY TF (session-truncated last
        # bucket, incl. ragged non-divisors via min(start+m-1, 929)) — a
        # FINALIZED day NEVER shows the stored-data note on any timeframe.
        for m in sorted(_TF_GOLDEN):
            info = _run_js(f"t.storedCutInfo('15:29', {m})")
            assert isinstance(info, dict)
            assert info["dayIncomplete"] is False, m
            assert info["midBucket"] is False, m
            assert info["closesAt"] == "15:29", m

    def test_bucket_final_minute_mid_day_is_incomplete_not_mid_bucket(self) -> None:
        # 09:17 IS the 09:15-bucket final: the on-screen 3minute bar is
        # complete, but the DAY is not — the note fires WITHOUT the
        # completed-bar tail (stored-data semantics).
        info = _run_js("t.storedCutInfo('09:17', 3)")
        assert isinstance(info, dict)
        assert info["dayIncomplete"] is True
        assert info["midBucket"] is False

    def test_one_minute_tf_mid_day_is_incomplete_not_mid_bucket(self) -> None:
        # TF=1minute: every stored bar is complete, yet a mid-day latest
        # still discloses the incomplete day (pull may be running).
        info = _run_js("t.storedCutInfo('13:42', 1)")
        assert isinstance(info, dict)
        assert info["dayIncomplete"] is True
        assert info["midBucket"] is False
        assert info["dataThrough"] == "13:42"

    def test_absent_malformed_daily_abstain_e9(self) -> None:
        assert _run_js("t.storedCutInfo(null, 3)") is None
        assert _run_js("t.storedCutInfo('', 3)") is None
        assert _run_js("t.storedCutInfo('junk', 3)") is None
        assert _run_js("t.storedCutInfo('12:00', 375)") is None  # daily
        assert _run_js("t.storedCutInfo('12:00', undefined)") is None  # unknown TF

    def test_out_of_session_echo_never_claims_incomplete(self) -> None:
        # e.g. a Muhurat evening session's echo clamps to 15:29 → finalized.
        info = _run_js("t.storedCutInfo('16:00', 3)")  # clamped to 15:29 → final
        assert isinstance(info, dict)
        assert info["dayIncomplete"] is False
        assert info["midBucket"] is False


# ── the KEY-SEMANTICS teeth: explicit pick == the /db bar, value-for-value ──


def _bar_at(day: date, hhmm: str, close: str, volume: int) -> CandleBar:
    """A 1-minute bar at IST ``hhmm`` on *day* (IST = UTC+05:30, no DST)."""
    total = _hhmm_to_min(hhmm) - (5 * 60 + 30)
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=None,
    )


class TestExplicitPickEqualsDbBar:
    """The mapping — not luck — closes the operator's partial-cut gap.

    A 3-minute last bucket whose 1m closes DIVERGE: ``?time=15:29`` (the new
    sent bucket-final) must equal the /db 3minute bar labeled 15:27 (the
    resampler oracle), while ``?time=15:27`` (the OLD wire — a bucket-START
    cutoff) serves the DIFFERENT mid-bucket partial value. The operator's
    confirmed confusion: movers 3min@15:27 = 24360.00 (mid-bucket cut) vs
    /db bar 15:27 = 24355.1 (full bucket) — both correct, different
    semantics; the dropdown now always sends the completed-bucket semantics.

    HONEST LIMIT (the round-3 hostile-review fold): the /db-equality holds
    WHENEVER the lake holds the picked bucket in full. A pick whose
    bucket-final lies BEYOND the day's last stored minute (a day incomplete
    in the DB — pull still in progress / truncated day) serves exactly the
    data stored so far instead — and the client structurally cannot detect
    it today, because the backend resolves ``latest_time`` only when no
    explicit ``time`` was sent (app.py's ``time is None`` gate,
    integration-pinned by ``test_explicit_time_leaves_latest_time_none``).
    Echoing ``latest_time`` on explicit picks + extending the stored-data
    note there is the NAMED follow-up
    DEC-MOVERS-EXPLICIT-PICK-LATEST-ECHO-001 (a payload-VALUE change
    deliberately kept out of this byte-identical-payload change).
    """

    _DAY = date(2024, 1, 2)
    _DATASET: ClassVar[list[dict[str, str]]] = [
        {"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "1minute"}
    ]

    def _write_lake(self, lake: Path) -> None:
        bars = [
            _bar_at(self._DAY, "15:27", "24360.00", 10),
            _bar_at(self._DAY, "15:28", "24357.55", 20),
            _bar_at(self._DAY, "15:29", "24355.10", 30),
        ]
        ParquetWriter(lake_root=lake).write(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=bars
        )

    def _ltp(self, lake: Path, time: str) -> Decimal:
        candidates = compute_spot_candidates(
            lake_root=lake,
            interval="3minute",
            date=self._DAY.isoformat(),
            time=time,
            datasets=self._DATASET,
        )
        assert len(candidates.snapshots) == 1
        return Decimal(str(candidates.snapshots[0].ltp))

    def test_bucket_final_cutoff_equals_the_db_bar(self, tmp_path: Path) -> None:
        import polars as pl
        from brutex.data.tf_resampler import resample_1m_to_tf

        self._write_lake(tmp_path)
        # The /db oracle: the SAME resampler the prebuild writes stored TFs
        # with — the 3minute bar labeled 15:27 covers 15:27–15:29.
        df_1m = pl.DataFrame(
            {
                "ts_utc": [
                    datetime(2024, 1, 2, 9, 57, tzinfo=UTC),
                    datetime(2024, 1, 2, 9, 58, tzinfo=UTC),
                    datetime(2024, 1, 2, 9, 59, tzinfo=UTC),
                ],
                "open": [24360.00, 24357.55, 24355.10],
                "high": [24360.00, 24357.55, 24355.10],
                "low": [24360.00, 24357.55, 24355.10],
                "close": [24360.00, 24357.55, 24355.10],
                "volume": [10, 20, 30],
            }
        )
        df_3m = resample_1m_to_tf(df_1m, 3)
        assert df_3m.height == 1
        db_bar_close = Decimal(str(df_3m.get_column("close")[0]))
        assert db_bar_close == Decimal("24355.10")  # the COMPLETED bucket close
        # The new wire (?time=15:29, the sent bucket-final) == the /db bar.
        assert self._ltp(tmp_path, "15:29") == db_bar_close

    def test_bucket_start_cutoff_serves_the_partial_value(self, tmp_path: Path) -> None:
        self._write_lake(tmp_path)
        # The OLD wire (?time=15:27 — a bucket-START cutoff) cuts MID-bucket:
        # the partial cut's close is the 15:27 1m close — a DIFFERENT number.
        partial = self._ltp(tmp_path, "15:27")
        completed = self._ltp(tmp_path, "15:29")
        assert partial == Decimal("24360.00")
        assert partial != completed

    def test_pick_beyond_end_of_data_serves_the_stored_latest_envelope(
        self, tmp_path: Path
    ) -> None:
        """HONEST-LIMIT envelope pin (the round-3 hostile-review fold).

        A day incomplete in the DB: the 1m lake ends at 13:00 IST. An
        explicit ``?time=15:29`` (the 15:27-labeled slot's sent bucket-final)
        does NOT serve any completed 15:27 bucket — none exists — it serves
        exactly the data stored so far: the 13:00-bucket cut (3minute bucket
        13:00–13:02 holding one minute), byte-equal to the Latest (no-time)
        whole-day cut. The client cannot detect this today (``latest_time``
        is echoed only on Latest requests —
        ``test_explicit_time_leaves_latest_time_none``); making it detectable
        is the NAMED follow-up DEC-MOVERS-EXPLICIT-PICK-LATEST-ECHO-001. If
        this test ever FAILS, the envelope moved — reconcile the ledger #1633
        HONEST section, the movers.html as-of note, and the bx-movers-time.js
        header together.
        """
        bars = [
            _bar_at(self._DAY, "12:57", "101.00", 1),
            _bar_at(self._DAY, "12:58", "102.00", 1),
            _bar_at(self._DAY, "12:59", "104.00", 1),
            _bar_at(self._DAY, "13:00", "103.00", 1),
        ]
        ParquetWriter(lake_root=tmp_path).write(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=bars
        )
        # The picked 15:27–15:29 bucket does not exist in the lake; the served
        # value is the 13:00-bucket cut (the 13:00 1m close — the stored end) …
        assert self._ltp(tmp_path, "15:29") == Decimal("103.00")
        # … byte-equal to the Latest whole-day cut (the pick degenerated to
        # "the data stored so far", exactly what the disclosed envelope states).
        latest = compute_spot_candidates(
            lake_root=tmp_path,
            interval="3minute",
            date=self._DAY.isoformat(),
            time=None,
            datasets=self._DATASET,
        )
        assert len(latest.snapshots) == 1
        assert Decimal(str(latest.snapshots[0].ltp)) == Decimal("103.00")


# ── substring presence pins (honest envelope: presence, not execution) ────


class TestTemplateWiringPins:
    def test_filltimes_uses_pairs_and_snap(self) -> None:
        text = _MOVERS.read_text(encoding="utf-8")
        assert "BXMoversTime.timeOptionPairs(m)" in text
        assert "BXMoversTime.snapTime(state.instant, m)" in text
        assert 'opts += `<option value="${p.value}">${p.label}</option>`' in text

    def test_state_carries_the_instant(self) -> None:
        text = _MOVERS.read_text(encoding="utf-8")
        assert "instant:null" in text

    def test_daily_branch_never_writes_the_instant(self) -> None:
        text = _MOVERS.read_text(encoding="utf-8")
        daily = _extract(r"if \(m >= 375\) \{.*", text)
        assert 'state.time=""' in daily
        assert "state.instant" not in daily  # the pick survives a 1day round-trip

    def test_time_onchange_is_the_only_instant_writer(self) -> None:
        text = _MOVERS.read_text(encoding="utf-8")
        writers = [ln for ln in text.splitlines() if re.search(r"state\.instant\s*=", ln)]
        assert len(writers) == 1
        assert 'state.instant = BXMoversTime.cutoffMinute($("time").value)' in writers[0]

    def test_asset_tab_handler_never_touches_the_time(self) -> None:
        text = _MOVERS.read_text(encoding="utf-8")
        handler = _extract(r'\$\("tabs"\)\.querySelectorAll\(".tab"\)[\s\S]*?\n\}\);', text)
        assert "fillTimes" not in handler
        assert "state.time" not in handler
        assert "state.instant" not in handler

    def test_explicit_span_label_and_stored_data_note(self) -> None:
        text = _MOVERS.read_text(encoding="utf-8")
        # The explicit-pick bucket SPAN label (start–final, textContent-only).
        assert "BXMoversTime.bucketStart(pickedMin, asofM)" in text
        # The item-4 stored-data disclosure literals (client-derived from
        # latest_time; the operator's FINAL spec wording — about the STORED
        # DATA, never a "forming" bar).
        assert "day incomplete in DB" in text
        assert "bar would close" in text
        assert "BXMoversTime.storedCutInfo(j.latest_time, asofM)" in text
        # The retired "forming bar" copy must never come back (the label's
        # code path speaks only about the stored data).
        assert "forming ${" not in text
        assert "includes trades up to" not in text

    def test_honest_limit_disclosure_names_the_followup(self) -> None:
        # The round-3 fold: the explicit-pick-beyond-end-of-data stored-data
        # limit (an explicit pick's /db-equality holds only when the lake
        # holds the bucket in full) must stay DISCLOSED at both frontend
        # sources, each naming the follow-up that would make it
        # client-detectable. Presence pins only (honest envelope) — the
        # behavior itself is pinned by
        # test_pick_beyond_end_of_data_serves_the_stored_latest_envelope.
        for path in (_MOVERS, _STATIC_JS):
            text = path.read_text(encoding="utf-8")
            assert "DEC-MOVERS-EXPLICIT-PICK-LATEST-ECHO-001" in text, path.name
        assert "last stored minute" in _MOVERS.read_text(encoding="utf-8")

    def test_adopt_block_stays_defensive_and_instant_free(self) -> None:
        text = _MOVERS.read_text(encoding="utf-8")
        adopt = _extract(r"if \(j\.time && state\.time !== j\.time\) \{[\s\S]*?\n    \}", text)
        assert 'document.createElement("option")' in adopt  # createElement, never innerHTML
        assert "state.instant" not in adopt  # an echo can never forward-drift the instant

    def test_resolve_time_is_gone_everywhere(self) -> None:
        # No definition, no export, no consumer (the module doc header may
        # still NAME the superseded function as history — that is not a sink).
        js = _STATIC_JS.read_text(encoding="utf-8")
        assert "function bxResolveTime(" not in js
        assert "resolveTime:" not in js
        assert "resolveTime" not in _MOVERS.read_text(encoding="utf-8")

    def test_db_bucket_label_note(self) -> None:
        text = _DB.read_text(encoding="utf-8")
        assert (
            "intraday bars are labeled by their bucket-START minute (IST)"
            " — e.g. a 3minute bar labeled 15:27 covers 15:27–15:29"
        ) in text


class TestMoversPageNoStore:
    def test_movers_page_ships_cache_control_no_store(self) -> None:
        # The actual producer of the operator's observed 09:15: the pre-#1243
        # page served STALE from the browser cache (the /movers route shipped
        # no Cache-Control). The /db precedent (DEC-DB-VIEWER-NOSTORE-001).
        pytest.importorskip("fastapi", reason="route test needs the [ui] extra")
        pytest.importorskip("httpx", reason="TestClient needs httpx")
        from brutex.dashboard.app import DashboardState, build_app
        from fastapi.testclient import TestClient

        client = TestClient(build_app(DashboardState(), instrument_store=None))
        response = client.get("/movers")
        assert response.status_code == 200
        assert response.headers.get("cache-control") == "no-store"
