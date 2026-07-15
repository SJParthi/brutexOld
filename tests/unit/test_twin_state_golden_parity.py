"""Frozen golden replay for the STATEFUL Rust indicator state (R2-RC-F2).

``trading_core.MarketStructureState`` (BOS/CHoCH — Group-12 bits 56-59) is the
SINGLE source of the stateful market-structure indicator: production
(``brutex/engine/stage1a.py``) drives ONLY this Rust pyclass, and the
hand-synced Python twin that once mirrored it in ``brutex/indicators/``
was deleted by DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 (this file was the
both-twins parity suite that proved the twins bit-identical before the
deletion).

The former ``VwapState`` half of this suite was RETIRED with the bits-52/53
VWAP tombstone (the 60→66 reshape — the pyclass was deleted from the crate at
0.4.0). The fixture is the schema ``twin-state-golden-v2`` re-emission of the
SAME seeded generator with the VWAP streams dropped; the regeneration run
PROVED the market-structure half byte-identical to the frozen v1 fixture's MS
half (the retired builders' RNG draws are burned draw-for-draw —
``generate_twin_state_golden._burn_retired_vwap_draws``), so the MS contract
survives the reshape bit-for-bit.

The language-neutral golden streams (fixed-seed, expected values frozen FROM
the Rust production state by ``tests/golden/generate_twin_state_golden.py``;
TickVault's native Rust side replays the same file) are replayed here through
the production pyclass and every per-bar output is asserted bit-identical to
the frozen expectation. A drift in the Rust core, the PyO3 boundary, or a
stale installed wheel fails this suite.

The streams cover the k-bar fractal confirmation delay (k ∈ {1, 2, 3}, incl.
a break on the exact confirming bar and tied-extreme windows that confirm
nothing), flat windows, engulfing both-side breaks, full BOS→CHoCH trend-flip
sequences, and i64-extreme market-structure levels.
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
# DELIBERATE RE-LOCK (the 60→66 reshape): schema v2 — the VWAP streams
# dropped; the MS half proven byte-identical to the v1 fixture's MS half.
# Old digest: 85a0e57dbde7f2dcba91e71184f8c2c4bbf488a92e1d8104a9db92477f3dd08e.
_EXPECTED_SHA256 = "d6d8cc89369a3a07a59136b1e2886a5cc2c508aa83dd9699899114d869e60b94"

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

    def test_schema_is_v2_with_no_vwap_streams(self) -> None:
        """The reshape re-emission: schema v2, the VWAP streams GONE."""
        assert _DATA["schema"] == "twin-state-golden-v2"
        assert "vwap_streams" not in _DATA

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
