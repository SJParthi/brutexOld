"""Frozen golden replay for the STATEFUL Rust indicator states (R2-RC-F2).

``trading_core.VwapState`` (session-anchored VWAP — Group-10 bits 52/53) and
``trading_core.MarketStructureState`` (BOS/CHoCH — Group-12 bits 56-59) are the
SINGLE source of the two stateful indicators: production
(``brutex/engine/stage1a.py``) drives ONLY these Rust pyclasses, and the
hand-synced Python twins that once mirrored them in ``brutex/indicators/``
were deleted by DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 (this file was the
both-twins parity suite that proved the twins bit-identical before the
deletion).

The frozen contract survives the deletion in full: the language-neutral golden
streams in ``tests/golden/fixtures/twin_state_golden_vectors.json``
(fixed-seed, expected values frozen FROM the Rust production state by
``tests/golden/generate_twin_state_golden.py`` — the fixture and its SHA-256
manifest are NEVER regenerated; TickVault's native Rust side replays the same
file) are replayed here through the production pyclasses and every per-bar
output is asserted bit-identical to the frozen expectation. A drift in the
Rust core, the PyO3 boundary, or a stale installed wheel fails this suite.

The streams cover IST session boundaries (day-ordinal resets, the 09:00-09:15
pre-open auction exclusion, warm/cold time-of-day rewinds, contract rolls),
FFI-ceiling ``MAX_BAR_PAISA`` bars whose ``cum_pv`` far exceeds i64 (the i128
truncating-division seam), the k-bar fractal confirmation delay (k ∈ {1, 2, 3},
incl. a break on the exact confirming bar and tied-extreme windows that confirm
nothing), and i64-extreme market-structure levels.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import trading_core

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _REPO_ROOT / "tests" / "golden" / "fixtures" / "twin_state_golden_vectors.json"
_RAW = _FIXTURE.read_text(encoding="utf-8")
_DATA: dict[str, Any] = json.loads(_RAW)

# Byte-freeze manifest (fixtures/README.md convention): any edit to the vectors
# — accidental or a regeneration — must consciously update this digest.
# DELIBERATE RE-LOCK (the vocab-train Regen, DEC-VOCAB-VWAP-RETAIN-001,
# MIGRATION.md #2206): the wipe-era v2 (MS-only) fixture regains its VWAP
# half. The regen run PROVED the MS half byte-identical to BOTH the v2
# fixture AND the pre-wipe v1, and the VWAP half byte-identical to the
# pre-wipe v1 (sha 85a0e57dbde7f2dcba91e71184f8c2c4bbf488a92e1d8104a9db92
# 477f3dd08e) — the ONLY delta is `generated_with_crate_version`
# 0.3.0 -> 0.6.0.
_EXPECTED_SHA256 = "e069248fefd6b1f44723346918833273a58f87056b2675865762988db8b55b52"

#: Golden streams frozen with NO confirmable swing (tied/flat/vetoed windows) —
#: after their full replay both swing-level getters must still read ``None``.
_NO_CONFIRM_STREAM_IDS = frozenset(
    {
        "ms_tied_extremes_no_confirm_k2",
        "ms_flat_window_k2",
        "ms_confirmation_vetoed_by_kth_bar_k2",
    }
)


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


class TestVwapGoldenReplay:
    """``trading_core.VwapState`` == the frozen golden expectation, per bar,
    bit-for-bit."""

    def test_rust_state_reproduces_every_golden_bar(self) -> None:
        """Replay every stream through the production state; collect any divergence."""
        mismatches: list[str] = []
        for stream in _DATA["vwap_streams"]:
            rs = trading_core.VwapState()
            for i, bar in enumerate(stream["bars"]):
                rs_out = rs.update(
                    bar["typical_paisa"],
                    bar["volume"],
                    bar["is_pre_open"],
                    bar["session_key"],
                    bar["session_day_ordinal"],
                    bar["tod_seconds"],
                )
                expected = bar["expected_vwap_paisa"]
                if rs_out != expected:
                    mismatches.append(f"{stream['id']} bar {i}: rust={rs_out} golden={expected}")
                # The idempotent read must agree with the update return.
                if rs.current() != rs_out:
                    mismatches.append(
                        f"{stream['id']} bar {i}: current() drift (rust {rs.current()} vs {rs_out})"
                    )
        assert not mismatches, "VWAP golden divergence:\n" + "\n".join(mismatches[:20])


class TestMarketStructureGoldenReplay:
    """``trading_core.MarketStructureState`` == the frozen golden signal, per
    bar, bit-for-bit."""

    def test_rust_state_reproduces_every_golden_bar(self) -> None:
        """Replay every stream (k per stream) through the production state."""
        mismatches: list[str] = []
        for stream in _DATA["market_structure_streams"]:
            rs = trading_core.MarketStructureState(stream["k"])
            for i, bar in enumerate(stream["bars"]):
                rs_sig = list(rs.update(bar["high"], bar["low"], bar["close"]))
                expected = [bool(v) for v in bar["expected_signal"]]
                if rs_sig != expected:
                    mismatches.append(
                        f"{stream['id']} (k={stream['k']}) bar {i}: rust={rs_sig} golden={expected}"
                    )
        assert not mismatches, "market-structure golden divergence:\n" + "\n".join(mismatches[:20])

    def test_confirmed_swing_levels_track_the_golden_replay(self) -> None:
        """The production state's confirmed-swing getters (the additive
        ``last_swing_high``/``last_swing_low`` introspection of
        DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001) stay self-consistent over
        the golden replay: after each stream the levels are either ``None``
        (nothing confirmed — pinned exactly for the tied/flat/vetoed streams)
        or real paisa ints, and at least one stream confirms BOTH levels (the
        ``Some`` arm is non-vacuous)."""
        some_high_seen = False
        some_low_seen = False
        for stream in _DATA["market_structure_streams"]:
            rs = trading_core.MarketStructureState(stream["k"])
            for bar in stream["bars"]:
                rs.update(bar["high"], bar["low"], bar["close"])
            assert rs.last_swing_high is None or isinstance(rs.last_swing_high, int)
            assert rs.last_swing_low is None or isinstance(rs.last_swing_low, int)
            if stream["id"] in _NO_CONFIRM_STREAM_IDS:
                assert rs.last_swing_high is None
                assert rs.last_swing_low is None
            some_high_seen = some_high_seen or rs.last_swing_high is not None
            some_low_seen = some_low_seen or rs.last_swing_low is not None
        assert some_high_seen and some_low_seen
