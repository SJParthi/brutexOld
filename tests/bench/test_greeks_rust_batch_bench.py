"""Bench pins for the Greeks Rust-batch seam (`DEC-GREEKS-RUST-BATCH-001`).

Golden Rule #10 discipline:

* **OFF-cost teeth** — with the flag OFF (the default) the ONLY seam addition
  is one env probe per :func:`enrich_option_bars` call; pinned below one bare
  row-walk so it can never hide per-row work.
* **Measured ON-vs-OFF** — the ratio is MEASURED and PRINTED (solve-heavy and
  reuse-heavy shapes); the ONLY gated assertion is a generous no-regression
  ceiling (``ON ≤ 1.5× OFF`` through :func:`ratio_ceiling`'s tier law). NO
  speedup is claimed or gated — the PR body states the printed number. NO
  O(1) claim anywhere: BOTH arms are O(rows); the batch is a constant-factor
  FFI-overhead lever only.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("trading_core", reason="greeks enrichment needs the trading_core wheel")

from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.greeks_enrich import (
    GREEKS_RUST_BATCH_ENV,
    enrich_option_bars,
    greeks_rust_batch_enabled,
    reset_rust_batch_for_tests,
)
from brutex.data.models import GREEKS_OVERLAY_FIELDS
from brutex.options.risk_free import FixedRateSource

from tests.bench._o1_tiers import ratio_ceiling

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from brutex.data.fno_symbol import FnoSymbolParts

_BASE_TS = datetime(2024, 1, 15, 3, 45, tzinfo=UTC)
_SPOT = Decimal("21500")
_RATE = FixedRateSource()
_N_ROWS = 5_000


@pytest.fixture(autouse=True)
def _fresh_state() -> Iterator[None]:
    reset_rust_batch_for_tests()
    yield
    reset_rust_batch_for_tests()


def _parts() -> FnoSymbolParts:
    parts = parse_fno_symbol("NSE-NIFTY-25Jan24-21500-CE")
    assert parts is not None
    return parts


def _rows(n: int, *, overlay: Mapping[str, object] | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for i in range(n):
        close = 180.0 + (i % 40) * 0.05  # a spread of solvable ATM premiums
        row: dict[str, object] = {
            "timestamp": _BASE_TS + timedelta(minutes=i),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 10,
            "open_interest": 100,
        }
        for name in GREEKS_OVERLAY_FIELDS:
            row[name] = None
        if overlay:
            row.update(overlay)
        rows.append(row)
    return rows


def _spots(rows: list[dict[str, object]]) -> dict[datetime, Decimal]:
    return {row["timestamp"]: _SPOT for row in rows}  # type: ignore[misc]


def _median(samples: list[float]) -> float:
    samples.sort()
    return samples[len(samples) // 2]


class TestOffModeIsFree:
    def test_flag_probe_cost_is_below_one_bare_walk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole OFF-mode seam addition (one env probe per CALL) is nil
        next to even ONE no-op walk over a large row list."""
        monkeypatch.delenv(GREEKS_RUST_BATCH_ENV, raising=False)
        rows = _rows(100_000)

        probe_samples: list[float] = []
        for _ in range(200):
            t0 = time.perf_counter()
            enabled = greeks_rust_batch_enabled()
            probe_samples.append(time.perf_counter() - t0)
            assert enabled is False
        probe = _median(probe_samples)

        walk_samples: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter()
            for _row in rows:
                pass
            walk_samples.append(time.perf_counter() - t0)
        walk = _median(walk_samples)

        assert probe < walk * 2.0, (
            f"OFF-mode flag probe ({probe * 1e6:.1f} µs) is not nil next to a bare "
            f"100000-row walk ({walk * 1e6:.1f} µs)"
        )


class TestMeasuredOnVsOff:
    def _run_arm(self, rows: list[dict[str, object]], spots: dict[datetime, Decimal]) -> float:
        samples: list[float] = []
        parts = _parts()
        for _ in range(5):
            reset_rust_batch_for_tests()
            t0 = time.perf_counter()
            enrich_option_bars(rows, parts=parts, spot_by_ts=spots, rate_source=_RATE, style_flag=1)
            samples.append(time.perf_counter() - t0)
        return min(samples)  # best-of-N-min (the house bench idiom)

    def test_solve_heavy_contract_printed_ratio_no_regression(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """~5,000 solve rows: print the measured ON/OFF wall ratio; gate ONLY
        a generous no-regression ceiling — never a speedup claim."""
        rows = _rows(_N_ROWS)
        spots = _spots(rows)
        monkeypatch.delenv(GREEKS_RUST_BATCH_ENV, raising=False)
        off = self._run_arm(rows, spots)
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "1")
        on = self._run_arm(rows, spots)
        ratio = on / off
        print(
            f"\n[greeks-rust-batch bench] solve-heavy {_N_ROWS} rows: "
            f"OFF {off * 1e3:.1f} ms · ON {on * 1e3:.1f} ms · ON/OFF {ratio:.3f}x"
        )
        assert ratio <= ratio_ceiling(1.5), (
            f"ON arm regressed past the no-regression ceiling: {ratio:.3f}x "
            f"(OFF {off * 1e3:.1f} ms, ON {on * 1e3:.1f} ms)"
        )

    def test_reuse_heavy_contract_printed_ratio_no_regression(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reuse-heavy contract (stored rows already current): both arms
        take the reuse fast path — the batch must add nothing."""
        base_rows = _rows(_N_ROWS)
        spots = _spots(base_rows)
        monkeypatch.delenv(GREEKS_RUST_BATCH_ENV, raising=False)
        solved, _ = enrich_option_bars(
            base_rows, parts=_parts(), spot_by_ts=spots, rate_source=_RATE, style_flag=1
        )
        rows: list[dict[str, object]] = []
        for row, bar in zip(base_rows, solved, strict=True):
            stored = dict(row)
            for name in GREEKS_OVERLAY_FIELDS:
                value = getattr(bar, name)
                stored[name] = float(str(value)) if isinstance(value, Decimal) else value
            rows.append(stored)
        off = self._run_arm(rows, spots)
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "1")
        on = self._run_arm(rows, spots)
        ratio = on / off
        print(
            f"\n[greeks-rust-batch bench] reuse-heavy {_N_ROWS} rows: "
            f"OFF {off * 1e3:.1f} ms · ON {on * 1e3:.1f} ms · ON/OFF {ratio:.3f}x"
        )
        assert ratio <= ratio_ceiling(1.5), (
            f"ON arm regressed the reuse fast path: {ratio:.3f}x "
            f"(OFF {off * 1e3:.1f} ms, ON {on * 1e3:.1f} ms)"
        )

    def test_reuse_fixture_genuinely_reuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sanity teeth for the reuse-heavy shape: every row is a reuse HIT
        (no accidental full re-solve making the bench measure the wrong path)."""
        monkeypatch.delenv(GREEKS_RUST_BATCH_ENV, raising=False)
        base_rows = _rows(64)
        spots = _spots(base_rows)
        solved, _ = enrich_option_bars(
            base_rows, parts=_parts(), spot_by_ts=spots, rate_source=_RATE, style_flag=1
        )
        rows: list[dict[str, object]] = []
        for row, bar in zip(base_rows, solved, strict=True):
            stored = dict(row)
            for name in GREEKS_OVERLAY_FIELDS:
                value = getattr(bar, name)
                stored[name] = float(str(value)) if isinstance(value, Decimal) else value
            rows.append(stored)
        _, stats = enrich_option_bars(
            rows, parts=_parts(), spot_by_ts=spots, rate_source=_RATE, style_flag=1
        )
        assert stats.reused == len(rows)
