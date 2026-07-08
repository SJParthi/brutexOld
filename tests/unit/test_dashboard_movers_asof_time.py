"""The shared /movers as-of TIME default/persist logic (DEC-MOVERS-ASOF-LATEST-DEFAULT-001).

Fails-before (operator-found): ``movers.html``'s ``fillTimes()`` rebuilt a
per-timeframe 09:15-step grid and PINNED ``state.time`` to the first grid slot,
so a coarse timeframe (whose grid never reaches 15:29) defaulted to a partial-
bucket close (3-minute → 15:27) while 1-minute reached 15:29 — a timeframe
switch on a finalized day showed DIFFERENT closes. The fix defaults the as-of to
blank ("" ⇒ the backend whole-day cut ⇒ the latest traded minute, identical
across every timeframe) and preserves an explicit pick across TF switches only
when it is still a valid slot.

The repo has NO executable-browser harness (an honest, known limit), so this
suite pins the fix in two layers, mirroring
``test_dashboard_halfup_format.py``:

* **String pins** — ``movers.html`` loads the ONE shared
  ``/static/bx-movers-time.js`` (CLAUDE.md §11 COMMON) BEFORE its inline script,
  ``fillTimes()`` prepends a ``Latest`` option and NO LONGER pins the first
  slot, the as-of label reads ``latest_time`` (not the wrong ``(daily)``), and
  the explicit-pick adopt reflects ``j.time`` via ``.value``/``.textContent`` +
  ``createElement`` (never ``innerHTML`` of server data).
* **Node-executed golden** — when ``node`` is available (it is on CI), the REAL
  shipped ``bx-movers-time.js`` is executed and its ``defaultTimeOptions`` /
  ``resolveTime`` / ``minuteToHHMM`` are asserted against the Python-side
  ``movers_snapshot`` primitives (``session_bar_times`` / ``minute_to_hhmm`` /
  ``is_intraday_interval``) — the single source of truth they mirror.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from pathlib import Path

import pytest
from brutex.dashboard.movers_snapshot import (
    interval_minutes,
    is_intraday_interval,
    minute_to_hhmm,
    session_bar_times,
)

_DASHBOARD = Path(__file__).resolve().parents[2] / "brutex" / "dashboard"
_STATIC_JS = _DASHBOARD / "static" / "bx-movers-time.js"
_MOVERS = _DASHBOARD / "templates" / "movers.html"


class TestSharedTimeModuleWiring:
    def test_static_module_exists_and_exports_the_primitives(self) -> None:
        text = _STATIC_JS.read_text(encoding="utf-8")
        assert "function bxDefaultTimeOptions(" in text
        assert "function bxResolveTime(" in text
        assert "function bxMinuteToHHMM(" in text
        assert "module.exports = BXMoversTime;" in text

    def test_movers_loads_shared_module_before_inline_script(self) -> None:
        text = _MOVERS.read_text(encoding="utf-8")
        include = text.index('<script src="/static/bx-movers-time.js"></script>')
        first_inline = text.index("<script>\n")
        assert include < first_inline, (
            "bx-movers-time.js must load (non-defer) BEFORE the inline script that "
            "calls BXMoversTime.defaultTimeOptions in fillTimes()"
        )

    def test_filltimes_defaults_to_latest_and_drops_first_slot_pin(self) -> None:
        text = _MOVERS.read_text(encoding="utf-8")
        # The Latest default is prepended as a STATIC literal option.
        assert '<option value="">Latest</option>' in text
        # The pre-fix first-slot pin is GONE (it froze a coarse TF at a partial close).
        assert "state.time = sel.value" not in text
        # The grid is built from the shared numeric-generated primitive.
        assert "BXMoversTime.defaultTimeOptions(m)" in text
        assert "BXMoversTime.resolveTime(state.time, m)" in text

    def test_asof_label_uses_latest_time_not_daily_for_intraday_default(self) -> None:
        text = _MOVERS.read_text(encoding="utf-8")
        # The intraday DEFAULT reads "latest (HH:MM)" from the additive echo, never "(daily)".
        assert "`latest (${j.latest_time})`" in text
        assert "const asofTime = j.time" in text
        # The wrong `${j.time||"(daily)"}` label the bug printed is gone.
        assert '${j.time||"(daily)"}' not in text

    def test_explicit_pick_adopt_uses_textcontent_not_innerhtml(self) -> None:
        text = _MOVERS.read_text(encoding="utf-8")
        # An off-grid echoed minute is injected via createElement + textContent/.value,
        # NEVER innerHTML of server data (DEC-DASHBOARD-XSS-HARDENING-001).
        assert 'document.createElement("option")' in text
        assert "opt.textContent = j.time" in text
        assert "tsel.value = j.time" in text
        # The adopt block must not build server data into innerHTML.
        assert "innerHTML = j.time" not in text
        assert "innerHTML = `${j.time" not in text

    def test_module_source_has_no_server_data_innerhtml_sink(self) -> None:
        # The shared module is pure string math — it must never ASSIGN innerHTML
        # (the doc comment names it as an XSS caveat; a real sink is `.innerHTML =`).
        text = _STATIC_JS.read_text(encoding="utf-8")
        assert ".innerHTML" not in text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestNodeExecutedGolden:
    #: Timeframes to cross-check JS ↔ Python (divisor + non-divisor + hours + daily).
    _INTERVALS = ("1minute", "2minute", "3minute", "5minute", "30minute", "3hour", "1day")

    def _run_js(self, expr: str) -> object:
        script = (
            f"const t = require({json.dumps(str(_STATIC_JS))});console.log(JSON.stringify({expr}));"
        )
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            ["node", "-e", script],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

    def test_default_time_options_match_session_bar_times(self) -> None:
        # The JS grid strings equal the Python session_bar_times for every TF —
        # so an injected minute is always a selectable option.
        for interval in self._INTERVALS:
            minutes = interval_minutes(interval)
            js_grid = self._run_js(f"t.defaultTimeOptions({minutes})")
            assert js_grid == session_bar_times(interval), interval

    def test_minute_to_hhmm_matches_python(self) -> None:
        for minute in (555, 556, 600, 925, 929):
            assert self._run_js(f"t.minuteToHHMM({minute})") == minute_to_hhmm(minute)

    def test_is_intraday_matches_python(self) -> None:
        for interval in self._INTERVALS:
            minutes = interval_minutes(interval)
            assert self._run_js(f"t.isIntradayMinutes({minutes})") == is_intraday_interval(interval)

    def test_resolve_time_default_blank_persists_valid_drops_invalid(self) -> None:
        # Default ("") stays Latest on every TF; a valid prior pick persists across
        # a TF switch; an invalid (off-grid) pick falls back to Latest.
        assert self._run_js("t.resolveTime('', 3)") == ""
        assert self._run_js("t.resolveTime('09:15', 5)") == "09:15"  # 09:15 is on every grid
        assert self._run_js("t.resolveTime('09:20', 5)") == "09:20"  # valid 5-min slot
        assert self._run_js("t.resolveTime('09:16', 5)") == ""  # not a 5-min slot → Latest
        assert self._run_js("t.resolveTime('15:27', 1)") == "15:27"  # valid 1-min slot
        assert self._run_js("t.resolveTime('15:29', 3)") == ""  # not a 3-min slot → Latest

    def test_teeth_coarse_tf_last_slot_is_not_the_latest_minute(self) -> None:
        # The bug reproduction at the source: a 3-minute grid's last slot is 15:27,
        # NOT the 15:29 the 1-minute grid reaches — the OLD first/last-slot default
        # would freeze a 3-minute TF at 15:27. The Latest ("") default removes it.
        three = self._run_js("t.defaultTimeOptions(3)")
        one = self._run_js("t.defaultTimeOptions(1)")
        assert isinstance(three, list) and isinstance(one, list)
        assert three[-1] == "15:27"
        assert one[-1] == "15:29"
        assert three[-1] != one[-1]
