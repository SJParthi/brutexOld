# pyright: reportPrivateUsage=none
"""Tests for the sweep DEFAULT condition-bit set (DEC-SWEEP-DEFAULT-CONDITIONS-60-001).

The default swept condition set (used when ``--conditions`` is not given) must
track the vocabulary's ``IMPLEMENTED_MASK`` — the bits whose evaluator computes
a real boolean — so it can never again silently lag behind the vocabulary (the
pre-fix bug: hardcoded ``range(52)`` while the vocabulary had grown to 60
conditions, excluding bits 52..59 — VWAP position, pivot R5/S5, BOS/CHoCH —
from every default sweep).

These tests pin BOTH the correctness (52..59 are now swept by default) AND the
safety envelope (the loud-refusal ``max_combos`` cap still fires for a wide
grid — broadening the vocabulary changes WHICH conditions are available, never
the refusal envelope).
"""

from __future__ import annotations

import argparse
from math import comb

import pytest
from brutex import cli
from brutex.engine.sweep_driver import DEFAULT_MAX_COMBOS
from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe
from brutex.strategies.types import Side
from brutex.vocabulary.bits import IMPLEMENTED_MASK, TOTAL_CONDITIONS

# ── default_condition_bits: correctness ─────────────────────────────────


def test_default_includes_active_bits_and_excludes_dead_ones() -> None:
    """The default sweeps EVERY active bit — all 74 on the retention tree.

    The 60→66 reshape gave pivot R5/S5 (54/55), BOS/CHoCH (56-59) and the
    CPR bits (60-63); the vocab-66 SuperTrend implemented-flip added bits
    64/65; the gap trio (66-68) + the express bits (69-73) landed
    implemented; and DEC-VOCAB-VWAP-RETAIN-001 (MIGRATION.md #2206) reversed
    the bits-52/53 wipe — the session-VWAP pair is RETAINED-ACTIVE, back in
    the default pool (no dead bit exists to exclude).
    """
    bits = cli.default_condition_bits()
    for active_bit in range(52, 74):
        assert active_bit in bits, f"bit {active_bit} must be swept by default"


def test_default_covers_every_implemented_bit() -> None:
    """The default is EXACTLY the implemented-bit set (one source of truth)."""
    bits = cli.default_condition_bits()
    expected = [bit for bit in range(TOTAL_CONDITIONS) if IMPLEMENTED_MASK & (1 << bit)]
    assert bits == expected


def test_default_is_ascending_and_unique() -> None:
    bits = cli.default_condition_bits()
    assert bits == sorted(bits)
    assert len(bits) == len(set(bits))


def test_default_length_is_the_74_active_bits() -> None:
    """The 74-condition layout carries 74 ACTIVE bits — the default grid.

    (The pre-reshape dense-``IMPLEMENTED_MASK`` era swept all 60; the 60→66
    reshape swept 62; the vocab-66 SuperTrend implemented-flip made it 64;
    DEC-GAP-MIDPOINT-CONDITIONS-001 appended the implemented gap trio at
    bits 66-68; DEC-FIB-TEMPLATE-EXPRESS-001 (MIGRATION.md #2205) appended
    the five implemented express bits 69-73; and DEC-VOCAB-VWAP-RETAIN-001
    (MIGRATION.md #2206) un-tombstoned the session-VWAP pair 52/53 —
    C(74, k) now, the disclosed +15.1% k=5 grid cost the operator ruled
    acceptable. No bit is excluded.)
    """
    bits = cli.default_condition_bits()
    assert IMPLEMENTED_MASK == (1 << 74) - 1
    assert len(bits) == 74
    assert bits == [*range(74)]


def test_default_excludes_layout_only_bits(monkeypatch: pytest.MonkeyPatch) -> None:
    """A LAYOUT-ONLY bit (implemented=false) is NEVER swept by default.

    Deriving from ``IMPLEMENTED_MASK`` (not ``range(TOTAL_CONDITIONS)``) is the
    honest choice — a dead bit that always evaluates to 0 must not pollute the
    default grid. Simulate a future manifest where the top bit is layout-only.
    """
    dead_bit = TOTAL_CONDITIONS - 1
    layout_only = IMPLEMENTED_MASK & ~(1 << dead_bit)
    monkeypatch.setattr(cli, "IMPLEMENTED_MASK", layout_only)
    bits = cli.default_condition_bits()
    assert dead_bit not in bits
    assert bits == [bit for bit in range(TOTAL_CONDITIONS) if layout_only & (1 << bit)]


# ── parser help strings track the count ─────────────────────────────────


def test_parser_conditions_help_references_the_implemented_count() -> None:
    """Both ``--conditions`` help strings carry the honest ACTIVE count.

    TOMB-03 companion: every count is DERIVED
    (``IMPLEMENTED_MASK.bit_count()`` / ``TOTAL_CONDITIONS``), never a
    hand-kept literal. On the 74/74 retention tree (#2206) the two counts
    COINCIDE — "all 74 implemented conditions" is an honest claim, so the
    wipe-era "never claim the DECLARED count is all implemented" negative
    guard is retired with the tombstones.
    """
    from brutex.vocabulary.bits import IMPLEMENTED_MASK

    parser = cli.build_parser()
    hits = 0
    for action in _walk_actions(parser):
        if "--conditions" in action.option_strings:
            hits += 1
            assert action.help is not None
            assert f"all {IMPLEMENTED_MASK.bit_count()} implemented conditions" in action.help
            assert f"declares {TOTAL_CONDITIONS} bits" in action.help
            # The stale "0..51 / all 52" wording must be gone.
            assert "0..51" not in action.help
    assert hits >= 1, "expected at least one --conditions argument"


def _walk_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    """Depth-first flatten of a parser + all its subparser actions."""
    out: list[argparse.Action] = []
    stack: list[argparse.ArgumentParser] = [parser]
    while stack:
        cur = stack.pop()
        for action in cur._actions:  # pyright: ignore[reportPrivateUsage]
            if isinstance(action, argparse._SubParsersAction):
                stack.extend(action.choices.values())
            else:
                out.append(action)
    return out


# ── safety envelope: the loud-refusal cap still fires ───────────────────


def _grid(**overrides: object) -> SweepGridConfig:
    base: dict[str, object] = {
        "instruments": ["NIFTY"],
        "timeframes": [Timeframe("1minute", 1)],
        "directions": [Side.BUY],
        "k_values": [1],
        "condition_bits": cli.default_condition_bits(),
        "stop_losses": [None],
        "take_profits": [None],
        "proximities": [None],
        "mode": "A",
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
    }
    base.update(overrides)
    return SweepGridConfig(**base)  # type: ignore[arg-type]


def test_default_bits_low_k_within_cap() -> None:
    """The production default k values (1, 2) stay well within the cap."""
    grid = _grid(k_values=[1, 2])
    n = len(cli.default_condition_bits())
    assert comb(n, 1) <= DEFAULT_MAX_COMBOS
    assert comb(n, 2) <= DEFAULT_MAX_COMBOS
    # Construction did not raise → the grid is admissible.
    assert set(grid.condition_bits) == set(cli.default_condition_bits())


def test_default_bits_k3_within_cap() -> None:
    """k=3 over the 72 active bits is C(72,3)=59,640 — still under the 100k cap."""
    n = len(cli.default_condition_bits())
    assert comb(n, 3) <= DEFAULT_MAX_COMBOS
    grid = _grid(k_values=[3])
    assert 3 in grid.k_values


def test_default_bits_k4_hits_loud_refusal_cap() -> None:
    """k=4 over the 72 active bits is C(72,4)=1,028,790 > 100k → loud refusal.

    The broadened default does NOT weaken the safety envelope: the exact same
    cap that refused C(52,4)=270,725 at n=52 refuses C(72,4) at n=72. The
    refusal is a clear, raise-explicitly ValueError, never a silent blowup.
    """
    n = len(cli.default_condition_bits())
    assert comb(n, 4) > DEFAULT_MAX_COMBOS
    with pytest.raises(ValueError, match="exceeding max_combos"):
        _grid(k_values=[4])
