"""Observational ``bxsnap2`` parity oracle for a FUTURE Rust ``arrow_table_to_bars`` port.

DEC-BXSNAP2-PARITY-GOLDEN-001 (MIGRATION.md #1618). READ-ONLY tripwire — it
changes NO production behaviour; it freezes the CURRENT Python
bar-materialization → run-identity digest so a future Rust/Arrow port (or an
accidental Python edit) that diverges fails loudly and mechanically.

The contract under freeze (see ``tests/golden/_bxsnap2_parity/__init__.py``):
:func:`brutex.feed.replay.arrow_table_to_bars` materializes each Arrow row into
a :class:`~brutex.data.models.CandleBar` whose price fields are
``Decimal(str(float))``, and
:func:`brutex.engine.stage3_wire.data_snapshot_hash` (preimage ``bxsnap2``)
folds those Decimals' ``__str__`` into the streamed BLAKE3 that keys the sweep's
run identity (Golden Rule #4). CPython ``str(float)`` is shortest-round-trip and
``Decimal`` re-renders large/small magnitudes specially — a Rust port must
reproduce BOTH steps byte-for-byte.

Two layers:

1. **Digest-equality goldens** — the current path must reproduce every frozen
   ``bxsnap2`` digest in ``fixtures/bxsnap2_parity_golden.json`` (integer /
   half-paisa / bad-tick kept-set / Muhurat kept-set), plus the
   positivity-refusal contract.
2. **Formatting-contract unit pins** — the exact ``str(float)`` /
   ``str(Decimal(str(float)))`` strings at boundaries a realistic bar cannot
   reach (the ``1e16`` / ``1e17`` sci-notation thresholds, ``.0`` integer
   rendering, negative/zero), documenting the parity contract the Rust port
   must match even outside real price ranges.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from brutex.data.bad_tick import BAD_TICK_SKIP_EVENT
from brutex.feed.replay import arrow_table_to_bars
from structlog.testing import capture_logs

from tests.golden._bxsnap2_parity import (
    DIGEST_CASES,
    PARITY_END,
    PARITY_EXCHANGE,
    PARITY_INTERVAL,
    PARITY_SEGMENT,
    PARITY_START,
    PARITY_SYMBOL,
    POSITIVITY_REFUSAL_ROWS,
    ParityCase,
    build_bar_table,
    materialize,
    run_case,
)

_FIXTURE_PATH: Final[Path] = Path(__file__).parent / "fixtures" / "bxsnap2_parity_golden.json"


def _load_fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    return data


def _fixture_cases() -> list[dict[str, Any]]:
    """The frozen golden cases (module-level parametrize source)."""
    cases: list[dict[str, Any]] = _load_fixture()["cases"]
    return cases


def _case_by_name(name: str) -> ParityCase:
    for case in DIGEST_CASES:
        if case.name == name:
            return case
    raise AssertionError(f"no DIGEST_CASES case named {name!r}")  # pragma: no cover


# ── Layer 1: digest-equality goldens ────────────────────────────────────────


def test_fixture_scope_pins_the_domain_tag() -> None:
    """The frozen scope labels must equal the helper's — a scope edit re-keys."""
    scope = _load_fixture()["scope"]
    assert scope == {
        "exchange": PARITY_EXCHANGE,
        "segment": PARITY_SEGMENT,
        "start": PARITY_START,
        "end": PARITY_END,
        "symbol": PARITY_SYMBOL,
        "interval": PARITY_INTERVAL,
    }


def test_fixture_covers_exactly_the_digest_cases() -> None:
    """The golden JSON case set must match ``DIGEST_CASES`` — no silent drift."""
    fixture = _load_fixture()
    assert fixture["preimage_version"] == "bxsnap2"
    fixture_names = [c["name"] for c in _fixture_cases()]
    assert fixture_names == [c.name for c in DIGEST_CASES]


@pytest.mark.parametrize("entry", _fixture_cases())
def test_current_path_reproduces_frozen_digest(entry: dict[str, Any]) -> None:
    """The CURRENT Python path must reproduce each frozen ``bxsnap2`` digest.

    This is the tripwire: a Rust port of ``arrow_table_to_bars`` (or any change
    to the Decimal rendering / positivity / bad-tick screen / field order) that
    diverges the run identity fails HERE.
    """
    case = _case_by_name(str(entry["name"]))
    bars, digest = run_case(case)
    assert len(bars) == entry["kept"] == case.expected_kept
    assert digest == entry["digest"]


def test_integer_prices_render_shortest_round_trip() -> None:
    """An integer f64 price folds into the hash as ``"20000.0"`` — NOT ``"20000.00"``."""
    case = _case_by_name("integer_prices")
    bars = materialize(case)
    # The load-bearing pin: shortest-round-trip, not 2dp-padded.
    assert str(bars[0].open) == "20000.0"
    assert str(bars[0].close) == "20000.0"
    assert "20000.00" not in {str(bars[0].open), str(bars[0].close)}
    # Mixed OI: first None (renders "None" in the hash), then ints.
    assert bars[0].open_interest is None
    assert [b.open_interest for b in bars] == [None, 100, 200]


def test_half_paisa_grid_renders_exact_decimals() -> None:
    """A ``.5`` close renders ``"100.5"`` (not ``"100.50"``); 2dp values stay exact."""
    case = _case_by_name("half_paisa_grid")
    bars = materialize(case)
    assert [str(b.close) for b in bars] == ["100.05", "100.5", "100.0"]
    assert str(bars[0].low) == "99.95"


def test_bad_tick_kept_set_skips_scaled_spike_with_one_warn() -> None:
    """The ÷10 spike is SKIPPED (one WARN, skipped=1); reopen-gap first bar KEPT."""
    case = _case_by_name("bad_tick_kept_set")
    with capture_logs() as logs:
        bars = arrow_table_to_bars(build_bar_table(case.rows), source=case.source)
    # spike (2794.5) absent; healthy successor + new-session first bar kept.
    assert [str(b.close) for b in bars] == ["27945.0", "27955.0", "6005.0"]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 1
    assert warns[0]["source"] == case.source


def test_muhurat_evening_session_bars_all_kept() -> None:
    """The read boundary applies NO calendar filter — every Muhurat bar is kept."""
    case = _case_by_name("muhurat_kept_set")
    with capture_logs() as logs:
        bars = materialize(case)
    assert len(bars) == 3
    # No bad-tick trip on the tightly-clustered Muhurat closes.
    assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]


def test_non_positive_bar_is_refused_never_hashed() -> None:
    """A non-positive price RAISES at the read boundary — never enters a digest."""
    with pytest.raises(ValueError, match="non-positive price"):
        arrow_table_to_bars(
            build_bar_table(POSITIVITY_REFUSAL_ROWS),
            source="NSE/INDEX/NSE-NIFTY/1minute",
        )


# ── Layer 2: str(float) → Decimal formatting-contract unit pins ──────────────
#
# The parity contract a Rust port must reproduce, pinned at boundaries a
# realistic OHLC bar cannot reach. Each row is the EXACT CPython output:
#   (float, str(float), str(Decimal(str(float))))
# The THIRD element is what actually enters the bxsnap2 hash (the bar's Decimal
# ``__str__``); the SECOND documents the intermediate ``str(float)`` a Rust f64
# formatter must match first. Captured from CPython 3.12, Golden Rule #1.
_STR_FLOAT_CONTRACT: Final[tuple[tuple[float, str, str], ...]] = (
    # Integer rendering: shortest round-trip appends ".0", never 2dp padding.
    (20000.0, "20000.0", "20000.0"),
    (100.0, "100.0", "100.0"),
    (27945.0, "27945.0", "27945.0"),
    # Half / 2dp values render minimally.
    (20000.5, "20000.5", "20000.5"),
    (99.95, "99.95", "99.95"),
    (100.05, "100.05", "100.05"),
    # Sci-notation thresholds: 1e15 stays fixed; 1e16 / 1e17 flip to exponent —
    # and Decimal re-renders the exponent UPPERCASE ("1E+16"), a real Rust trap.
    (1e15, "1000000000000000.0", "1000000000000000.0"),
    (1e16, "1e+16", "1E+16"),
    (1e17, "1e+17", "1E+17"),
    # Small magnitudes: 1e-4 stays fixed; 1e-5 flips to exponent in str(float)
    # but Decimal EXPANDS it back to "0.00001".
    (1e-4, "0.0001", "0.0001"),
    (1e-5, "1e-05", "0.00001"),
    # Negative / zero: unreachable by the positivity screen, pinned for the port.
    (-1.5, "-1.5", "-1.5"),
    (0.0, "0.0", "0.0"),
    (-0.0, "-0.0", "-0.0"),
)


@pytest.mark.parametrize(("value", "expected_str", "expected_decimal_str"), _STR_FLOAT_CONTRACT)
def test_str_float_to_decimal_formatting_contract(
    value: float, expected_str: str, expected_decimal_str: str
) -> None:
    """Pin the exact ``str(float)`` and ``str(Decimal(str(float)))`` a port must match."""
    assert str(value) == expected_str
    assert str(Decimal(str(value))) == expected_decimal_str
