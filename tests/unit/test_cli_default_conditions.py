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
    """The default sweeps the ACTIVE bits 54..63 and NEVER the dead ones.

    The 60→66 reshape: pivot R5/S5 (54/55), BOS/CHoCH (56-59) and the new
    CPR bits (60-63) are swept by default; the RETIRED VWAP tombstones
    (52/53) and the declared-only SuperTrend bits (64/65) are excluded —
    a default sweep must never burn combos on a condition that can never
    fire.
    """
    bits = cli.default_condition_bits()
    for active_bit in range(54, 64):
        assert active_bit in bits, f"bit {active_bit} must be swept by default"
    for dead_bit in (52, 53, 64, 65):
        assert dead_bit not in bits, f"dead bit {dead_bit} must NOT be swept"


def test_default_covers_every_implemented_bit() -> None:
    """The default is EXACTLY the implemented-bit set (one source of truth)."""
    bits = cli.default_condition_bits()
    expected = [bit for bit in range(TOTAL_CONDITIONS) if IMPLEMENTED_MASK & (1 << bit)]
    assert bits == expected


def test_default_is_ascending_and_unique() -> None:
    bits = cli.default_condition_bits()
    assert bits == sorted(bits)
    assert len(bits) == len(set(bits))


def test_default_length_is_the_67_active_bits() -> None:
    """The 74-condition layout carries 67 ACTIVE bits — the default grid.

    (62 at the 60→66 reshape — tombstones 52/53 + layout-only 64/65 excluded —
    plus the five express bits 69-73 at DEC-FIB-TEMPLATE-EXPRESS-001,
    MIGRATION.md #1935; the reserved gap placeholders 66-68 stay excluded.)
    """
    bits = cli.default_condition_bits()
    assert IMPLEMENTED_MASK == (((1 << 64) - 1) ^ (1 << 52) ^ (1 << 53)) | (0b11111 << 69)
    assert len(bits) == 67
    assert bits == [*range(52), *range(54, 64), *range(69, 74)]


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

    TOMB-03 companion: the default sweeps the IMPLEMENTED bits (62 on the
    66-declared layout), so "all {TOTAL} implemented conditions" was a lie by
    4 — every count is DERIVED (``IMPLEMENTED_MASK.bit_count()`` /
    ``TOTAL_CONDITIONS``), never a hand-kept literal.
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
            # The stale "0..51 / all 52" wording must be gone — and the help
            # must never claim the DECLARED count is all implemented.
            assert "0..51" not in action.help
            assert f"all {TOTAL_CONDITIONS} implemented" not in action.help
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
    """k=3 over the 62 active bits is C(62,3)=37,820 — still under the 100k cap."""
    n = len(cli.default_condition_bits())
    assert comb(n, 3) <= DEFAULT_MAX_COMBOS
    grid = _grid(k_values=[3])
    assert 3 in grid.k_values


def test_default_bits_k4_hits_loud_refusal_cap() -> None:
    """k=4 over the 62 active bits is C(62,4)=557,845 > 100k → loud refusal.

    The broadened default does NOT weaken the safety envelope: the exact same
    cap that refused C(52,4)=270,725 at n=52 refuses C(62,4) at n=62. The
    refusal is a clear, raise-explicitly ValueError, never a silent blowup.
    """
    n = len(cli.default_condition_bits())
    assert comb(n, 4) > DEFAULT_MAX_COMBOS
    with pytest.raises(ValueError, match="exceeding max_combos"):
        _grid(k_values=[4])
