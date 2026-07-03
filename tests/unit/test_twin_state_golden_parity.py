"""Twin-parity goldens for the STATEFUL indicator twins (R2-RC-F2).

``VwapState`` (session-anchored VWAP — Group-10 bits 52/53) and
``MarketStructureState`` (BOS/CHoCH — Group-12 bits 56-59) each exist twice:

* the **Rust production twin** in ``trading_core`` (the only side
  ``brutex/engine/stage1a.py`` drives — DEC-STAGE1A-WIRE-VWAP-BOS-001);
* the **Python mirror** in ``brutex/indicators/`` (documented "bit-for-bit
  twin", DEC-SHARED-CORE-001 / ADR-0012 D7).

Before this test the bit-identity claim rested on manual mirroring: the frozen
400-case ``vocab_golden_vectors.json`` answer-key pins the EVALUATOR bits, not
the per-bar ``.update()`` derivation, and no test imported both twins. Here we
replay the language-neutral golden streams in
``tests/golden/fixtures/twin_state_golden_vectors.json`` (fixed-seed, expected
values frozen FROM THE RUST PRODUCTION TWIN by
``tests/golden/generate_twin_state_golden.py``) through BOTH twins and assert
every per-bar output is bit-identical to the frozen expectation — mirroring the
``TestIndicatorComputeParity`` pattern in
``tests/integration/test_python_rust_parity.py``.

The streams cover IST session boundaries (day-ordinal resets, the 09:00-09:15
pre-open auction exclusion, warm/cold time-of-day rewinds, contract rolls),
FFI-ceiling ``MAX_BAR_PAISA`` bars whose ``cum_pv`` far exceeds i64 (the i128 /
unbounded-int truncating-division seam), the k-bar fractal confirmation delay
(k ∈ {1, 2, 3}, incl. a break on the exact confirming bar and tied-extreme
windows that confirm nothing), and i64-extreme market-structure levels.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import trading_core
from brutex.indicators.market_structure import MarketStructureState
from brutex.indicators.vwap import VwapState

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _REPO_ROOT / "tests" / "golden" / "fixtures" / "twin_state_golden_vectors.json"
_RAW = _FIXTURE.read_text(encoding="utf-8")
_DATA: dict[str, Any] = json.loads(_RAW)

# Byte-freeze manifest (fixtures/README.md convention): any edit to the vectors
# — accidental or a regeneration — must consciously update this digest.
_EXPECTED_SHA256 = "85a0e57dbde7f2dcba91e71184f8c2c4bbf488a92e1d8104a9db92477f3dd08e"


def _signal_list(sig: Any) -> list[bool]:
    """The 4-bool wire order shared by the fixture and the Rust FFI tuple."""
    return [
        bool(sig.bos_bullish),
        bool(sig.bos_bearish),
        bool(sig.choch_bullish),
        bool(sig.choch_bearish),
    ]


class TestFixtureIntegrity:
    """The fixture itself is frozen and non-vacuous."""

    def test_fixture_sha256_manifest(self) -> None:
        """Byte-for-byte no-drift gate on the golden vector file."""
        assert hashlib.sha256(_RAW.encode("utf-8")).hexdigest() == _EXPECTED_SHA256

    def test_vwap_vectors_are_non_vacuous(self) -> None:
        """The frozen VWAP expectations exercise both the None warm-up and a
        cum_pv that exceeds i64 (the i128-accumulator seam has teeth)."""
        i64_max = 2**63 - 1
        all_bars = [b for s in _DATA["vwap_streams"] for b in s["bars"]]
        assert any(b["expected_vwap_paisa"] is None for b in all_bars)
        assert any(b["expected_vwap_paisa"] is not None for b in all_bars)
        # Re-derive the running cum_pv of the large-magnitude stream and prove
        # it passes i64::MAX (so the goldens genuinely cross the i128 seam).
        (large,) = [s for s in _DATA["vwap_streams"] if s["id"] == "vwap_large_magnitude_i64_edges"]
        cum_pv = 0
        for bar in large["bars"]:
            if not bar["is_pre_open"] and bar["volume"] > 0:
                cum_pv += bar["typical_paisa"] * bar["volume"]
        assert cum_pv > i64_max

    def test_market_structure_vectors_fire_all_four_signals(self) -> None:
        """Each of the four BOS/CHoCH booleans reads true somewhere in the
        frozen streams (no signal is pinned only in its all-false state)."""
        fired = [False, False, False, False]
        for stream in _DATA["market_structure_streams"]:
            for bar in stream["bars"]:
                for i, flag in enumerate(bar["expected_signal"]):
                    fired[i] = fired[i] or flag
        assert fired == [True, True, True, True], f"unfired signal slots: {fired}"
        # The documented engulfing case is pinned: one bar reads one up-signal
        # AND one down-signal together (choch_bullish + bos_bearish).
        (engulf,) = [
            s for s in _DATA["market_structure_streams"] if s["id"] == "ms_engulf_both_sides_k2"
        ]
        assert any(bar["expected_signal"] == [False, True, True, False] for bar in engulf["bars"])


class TestVwapTwinParity:
    """Python ``brutex.indicators.vwap.VwapState`` == Rust ``trading_core.VwapState``
    == the frozen golden expectation, per bar, bit-for-bit."""

    def test_both_twins_reproduce_every_golden_bar(self) -> None:
        """Replay every stream through both twins; collect any divergence."""
        mismatches: list[str] = []
        for stream in _DATA["vwap_streams"]:
            py = VwapState()
            rs = trading_core.VwapState()
            for i, bar in enumerate(stream["bars"]):
                py_out = py.update(
                    bar["typical_paisa"],
                    bar["volume"],
                    is_pre_open=bar["is_pre_open"],
                    session_key=bar["session_key"],
                    session_day=date.fromordinal(bar["session_day_ordinal"]),
                    tod_seconds=bar["tod_seconds"],
                )
                rs_out = rs.update(
                    bar["typical_paisa"],
                    bar["volume"],
                    bar["is_pre_open"],
                    bar["session_key"],
                    bar["session_day_ordinal"],
                    bar["tod_seconds"],
                )
                expected = bar["expected_vwap_paisa"]
                if not (py_out == rs_out == expected):
                    mismatches.append(
                        f"{stream['id']} bar {i}: python={py_out} rust={rs_out} golden={expected}"
                    )
                # The idempotent read must agree with the update return on both.
                if py.current() != py_out or rs.current() != rs_out:
                    mismatches.append(
                        f"{stream['id']} bar {i}: current() drift "
                        f"(python {py.current()} vs {py_out}, rust {rs.current()} vs {rs_out})"
                    )
        assert not mismatches, "VWAP twin divergence:\n" + "\n".join(mismatches[:20])


class TestMarketStructureTwinParity:
    """Python ``MarketStructureState`` == Rust ``trading_core.MarketStructureState``
    == the frozen golden signal, per bar, bit-for-bit."""

    def test_both_twins_reproduce_every_golden_bar(self) -> None:
        """Replay every stream (k per stream) through both twins."""
        mismatches: list[str] = []
        for stream in _DATA["market_structure_streams"]:
            py = MarketStructureState(k=stream["k"])
            rs = trading_core.MarketStructureState(stream["k"])
            for i, bar in enumerate(stream["bars"]):
                py_sig = _signal_list(py.update(bar["high"], bar["low"], bar["close"]))
                rs_sig = list(rs.update(bar["high"], bar["low"], bar["close"]))
                expected = [bool(v) for v in bar["expected_signal"]]
                if not (py_sig == rs_sig == expected):
                    mismatches.append(
                        f"{stream['id']} (k={stream['k']}) bar {i}: "
                        f"python={py_sig} rust={rs_sig} golden={expected}"
                    )
        assert not mismatches, "market-structure twin divergence:\n" + "\n".join(mismatches[:20])

    def test_python_confirmed_swing_levels_track_the_golden_replay(self) -> None:
        """The Python twin's confirmed-swing state stays self-consistent over the
        golden replay: after each stream the last confirmed levels are either
        ``None`` (nothing confirmed — the flat/tied streams) or real paisa ints.
        (The Rust FFI does not expose its swing levels, so this pins the Python
        side's OBSERVABLE state shape; the per-bar signals above already pin the
        cross-language behaviour that consumes those levels.)"""
        for stream in _DATA["market_structure_streams"]:
            py = MarketStructureState(k=stream["k"])
            for bar in stream["bars"]:
                py.update(bar["high"], bar["low"], bar["close"])
            assert py.last_swing_high is None or isinstance(py.last_swing_high, int)
            assert py.last_swing_low is None or isinstance(py.last_swing_low, int)
            if stream["id"] in {
                "ms_tied_extremes_no_confirm_k2",
                "ms_flat_window_k2",
                "ms_confirmation_vetoed_by_kth_bar_k2",
            }:
                assert py.last_swing_high is None
                assert py.last_swing_low is None
