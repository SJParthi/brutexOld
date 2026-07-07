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


def test_default_includes_new_bits_52_to_59() -> None:
    """The default sweeps bits 52..59 (VWAP / pivot R5-S5 / BOS-CHoCH).

    Fail-before/pass-after: with the old ``range(52)`` default, none of
    52..59 was present; the fix derives the default from the vocabulary.
    """
    bits = cli.default_condition_bits()
    for new_bit in range(52, 60):
        assert new_bit in bits, f"bit {new_bit} must be swept by default"


def test_default_covers_every_implemented_bit() -> None:
    """The default is EXACTLY the implemented-bit set (one source of truth)."""
    bits = cli.default_condition_bits()
    expected = [bit for bit in range(TOTAL_CONDITIONS) if IMPLEMENTED_MASK & (1 << bit)]
    assert bits == expected


def test_default_is_ascending_and_unique() -> None:
    bits = cli.default_condition_bits()
    assert bits == sorted(bits)
    assert len(bits) == len(set(bits))


def test_default_length_matches_total_conditions_when_all_implemented() -> None:
    """Today every one of the ``TOTAL_CONDITIONS`` bits is implemented."""
    bits = cli.default_condition_bits()
    # Guard: this equality only holds while IMPLEMENTED_MASK is dense.
    assert IMPLEMENTED_MASK == (1 << TOTAL_CONDITIONS) - 1
    assert len(bits) == TOTAL_CONDITIONS
    assert bits == list(range(TOTAL_CONDITIONS))


def test_default_excludes_layout_only_bits(monkeypatch: pytest.MonkeyPatch) -> None:
    """A LAYOUT-ONLY bit (implemented=false) is NEVER swept by default.

    Deriving from ``IMPLEMENTED_MASK`` (not ``range(TOTAL_CONDITIONS)``) is the
    honest choice — a dead bit that always evaluates to 0 must not pollute the
    default grid. Simulate a future manifest where the top bit is layout-only.
    """
    dead_bit = TOTAL_CONDITIONS - 1
    layout_only = IMPLEMENTED_MASK & ~(1 << dead_bit)
    # The derivation seam MOVED to the engine (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001
    # — cli.default_condition_bits delegates to brutex.engine.sweep_estimate).
    monkeypatch.setattr("brutex.engine.sweep_estimate.IMPLEMENTED_MASK", layout_only)
    bits = cli.default_condition_bits()
    assert dead_bit not in bits
    assert bits == [bit for bit in range(TOTAL_CONDITIONS) if layout_only & (1 << bit)]


# ── parser help strings track the count ─────────────────────────────────


def test_parser_conditions_help_references_total_conditions() -> None:
    """Both ``--conditions`` help strings carry the authoritative count."""
    parser = cli.build_parser()
    hits = 0
    for action in _walk_actions(parser):
        if "--conditions" in action.option_strings:
            hits += 1
            assert action.help is not None
            assert str(TOTAL_CONDITIONS) in action.help
            assert str(TOTAL_CONDITIONS - 1) in action.help
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
    """k=3 over all 60 bits is C(60,3)=34220, still under the 100k cap."""
    n = len(cli.default_condition_bits())
    assert comb(n, 3) <= DEFAULT_MAX_COMBOS
    grid = _grid(k_values=[3])
    assert 3 in grid.k_values


def test_default_bits_k4_hits_loud_refusal_cap() -> None:
    """k=4 over all 60 bits is C(60,4)=487635 > 100k → loud refusal.

    The broadened default (52→60) does NOT weaken the safety envelope: the
    exact same cap that refused C(52,4)=270725 at n=52 refuses C(60,4) at
    n=60. The refusal is a clear, raise-explicitly ValueError, never a
    silent blowup.
    """
    n = len(cli.default_condition_bits())
    assert comb(n, 4) > DEFAULT_MAX_COMBOS
    with pytest.raises(ValueError, match="exceeding max_combos"):
        _grid(k_values=[4])
