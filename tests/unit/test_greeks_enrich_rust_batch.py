"""Unit tests for the Rust columnar batch arm of the Greeks enrichment.

`DEC-GREEKS-RUST-BATCH-001`: the default-OFF ``BRUTEX_GREEKS_RUST_BATCH``
tristate flag, the OFF ≡ ON bit-identity over a hostile per-row status
matrix (every screen family + reuse + skip interleaves), deferred-accounting
order invariance, the 16-element per-process burn-in self-check, the
autorevert latch (exception AND mismatch shapes, MemoryError re-raise), and
the :data:`GREEKS_BATCH_MAX_ROWS` chunking law.
"""

from __future__ import annotations

import dataclasses
import math
import struct
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

pytest.importorskip("trading_core", reason="greeks enrichment needs the trading_core wheel")

from brutex.data import greeks_enrich
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.greeks_enrich import (
    _RUST_BATCH_STATE,
    GREEKS_BATCH_MAX_ROWS,
    GREEKS_RUST_BATCH_DEFAULT,
    GREEKS_RUST_BATCH_ENV,
    STATUS_AT_EXPIRY,
    STATUS_BAD_INPUT,
    STATUS_BELOW_INTRINSIC,
    STATUS_NO_RATE,
    STATUS_NO_SOLUTION,
    STATUS_NO_SPOT,
    STATUS_OK,
    STYLE_EUROPEAN_EXACT,
    EnrichStats,
    _no_arb_floor,
    enrich_option_bars,
    expiry_instant_utc,
    greeks_rust_batch_enabled,
    pack_greeks_provenance,
    reset_rust_batch_for_tests,
    t_years_act365,
    unpack_greeks_provenance,
)
from brutex.data.models import GREEKS_OVERLAY_FIELDS, FnoGreeksBar
from brutex.options.greeks import GreeksBatchOutcome, compute_greeks_batch
from brutex.options.risk_free import CsvRateSource, FixedRateSource

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from brutex.data.fno_symbol import FnoSymbolParts
    from structlog.typing import EventDict

_CE_SYMBOL = "NSE-NIFTY-25Jan24-21500-CE"
_PE_SYMBOL = "NSE-NIFTY-25Jan24-21500-PE"
_EXPIRY = date(2024, 1, 25)
#: 2024-01-15 09:15 IST — ten days before expiry, a comfortably-solvable T.
_BAR_TS = datetime(2024, 1, 15, 3, 45, tzinfo=UTC)
_SPOT = Decimal("21500")
_RATE = FixedRateSource()
_STYLE = STYLE_EUROPEAN_EXACT


@pytest.fixture(autouse=True)
def _clean_batch_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test starts flag-unset with pristine process state."""
    monkeypatch.delenv(GREEKS_RUST_BATCH_ENV, raising=False)
    reset_rust_batch_for_tests()
    yield
    reset_rust_batch_for_tests()


def _parts(symbol: str = _CE_SYMBOL) -> FnoSymbolParts:
    parts = parse_fno_symbol(symbol)
    assert parts is not None
    return parts


def _row(
    ts: datetime,
    *,
    close: float,
    low: float | None = None,
    high: float | None = None,
    volume: int = 10,
    oi: int | None = 100,
    overlay: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """One lake row shaped like the schema-tolerant option reader's output."""
    row: dict[str, object] = {
        "timestamp": ts,
        "open": close,
        "high": close if high is None else high,
        "low": close if low is None else low,
        "close": close,
        "volume": volume,
        "open_interest": oi,
    }
    for name in GREEKS_OVERLAY_FIELDS:
        row[name] = None
    if overlay:
        row.update(overlay)
    return row


def _bar_key(bar: FnoGreeksBar) -> tuple[object, ...]:
    """Bit-exact bar compare key: floats by IEEE bit pattern (struct.pack,
    signed zeros included), Decimals by numeric == AND (sign, digits,
    exponent), pids by int."""
    key: list[object] = [bar.timestamp, bar.volume, bar.open_interest, bar.greeks_provenance_id]
    for name in ("iv", "delta", "gamma", "theta", "vega", "rho", "t_years_used", "rate_used"):
        value: float | None = getattr(bar, name)
        key.append(None if value is None else struct.pack("<d", value))
    for name in ("open", "high", "low", "close", "spot_at_bar"):
        dec: Decimal | None = getattr(bar, name)
        key.append(None if dec is None else (str(dec), dec.as_tuple()))
    return tuple(key)


def _run_both_arms(
    rows: Sequence[Mapping[str, object]],
    *,
    parts: FnoSymbolParts | None = None,
    spot_by_ts: Mapping[datetime, Decimal] | None = None,
    rate_source: object = _RATE,
    monkeypatch: pytest.MonkeyPatch,
    pre_engage: bool = False,
) -> tuple[
    tuple[list[FnoGreeksBar], EnrichStats, list[EventDict]],
    tuple[list[FnoGreeksBar], EnrichStats, list[EventDict]],
]:
    """Run the SAME inputs OFF then ON (env-injected), each with fresh state,
    capturing the structlog event stream of each arm."""
    the_parts = parts if parts is not None else _parts()
    spots = spot_by_ts if spot_by_ts is not None else {_BAR_TS: _SPOT}

    monkeypatch.delenv(GREEKS_RUST_BATCH_ENV, raising=False)
    reset_rust_batch_for_tests()
    with capture_logs() as off_logs:
        bars_off, stats_off = enrich_option_bars(
            rows,
            parts=the_parts,
            spot_by_ts=spots,
            rate_source=rate_source,  # type: ignore[arg-type]
            style_flag=_STYLE,
        )

    monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "1")
    reset_rust_batch_for_tests()
    if pre_engage:
        # Consume the once-per-process engaged INFO on a throwaway ATM
        # solvable row (the DEFAULT contract — always dispatches) so the
        # captured ON stream below is the steady-state one.
        warmed, _ = enrich_option_bars(
            [_row(_BAR_TS, close=180.0)],
            parts=_parts(),
            spot_by_ts={_BAR_TS: _SPOT},
            rate_source=_RATE,
            style_flag=_STYLE,
        )
        assert _RUST_BATCH_STATE.engaged is True, warmed
    with capture_logs() as on_logs:
        bars_on, stats_on = enrich_option_bars(
            rows,
            parts=the_parts,
            spot_by_ts=spots,
            rate_source=rate_source,  # type: ignore[arg-type]
            style_flag=_STYLE,
        )
    return (bars_off, stats_off, off_logs), (bars_on, stats_on, on_logs)


class TestFlagResolution:
    """The default-OFF tristate law + warn-once + per-call resolution."""

    def test_unset_and_blank_default_off_silently(self) -> None:
        assert GREEKS_RUST_BATCH_DEFAULT is False
        with capture_logs() as logs:
            assert greeks_rust_batch_enabled({}) is False
            assert greeks_rust_batch_enabled({GREEKS_RUST_BATCH_ENV: ""}) is False
            assert greeks_rust_batch_enabled({GREEKS_RUST_BATCH_ENV: "   "}) is False
        assert logs == []

    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
    def test_truthy_enables(self, raw: str) -> None:
        assert greeks_rust_batch_enabled({GREEKS_RUST_BATCH_ENV: raw}) is True

    @pytest.mark.parametrize("raw", ["0", "false", "No", " OFF "])
    def test_falsy_disables(self, raw: str) -> None:
        assert greeks_rust_batch_enabled({GREEKS_RUST_BATCH_ENV: raw}) is False

    def test_junk_falls_to_default_with_exactly_one_warn(self) -> None:
        with capture_logs() as logs:
            assert greeks_rust_batch_enabled({GREEKS_RUST_BATCH_ENV: "maybe?"}) is False
            assert greeks_rust_batch_enabled({GREEKS_RUST_BATCH_ENV: "maybe?"}) is False
        warns = [e for e in logs if e["event"] == "greeks_enrich.rust_batch_flag_unrecognised"]
        assert len(warns) == 1
        assert warns[0]["value"] == repr("maybe?")

    def test_reads_process_env_when_no_mapping_given(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "1")
        assert greeks_rust_batch_enabled() is True
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "0")
        assert greeks_rust_batch_enabled() is False

    def test_per_call_env_flip_splits_arms_between_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The flag is resolved per CALL — an env flip between two calls
        routes them onto different arms with bit-identical output."""
        rows = [_row(_BAR_TS, close=180.0)]
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "1")
        bars_on, _ = enrich_option_bars(
            rows, parts=_parts(), spot_by_ts={_BAR_TS: _SPOT}, rate_source=_RATE, style_flag=_STYLE
        )
        assert _RUST_BATCH_STATE.engaged is True
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "0")
        bomb_calls: list[int] = []

        def _bomb(*_a: object, **_k: object) -> object:
            bomb_calls.append(1)
            raise AssertionError("the OFF call must never dispatch the batch")

        monkeypatch.setattr(greeks_enrich, "compute_greeks_batch", _bomb)
        bars_off, _ = enrich_option_bars(
            rows, parts=_parts(), spot_by_ts={_BAR_TS: _SPOT}, rate_source=_RATE, style_flag=_STYLE
        )
        assert bomb_calls == []
        assert [_bar_key(b) for b in bars_off] == [_bar_key(b) for b in bars_on]

    def test_off_arm_emits_zero_batch_events_and_never_dispatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default arm's log stream carries NO rust_batch event and the
        wrapper is never consulted (the bomb-spy zero-delta pin)."""

        def _bomb(*_a: object, **_k: object) -> object:
            raise AssertionError("OFF arm must never dispatch")

        monkeypatch.setattr(greeks_enrich, "compute_greeks_batch", _bomb)
        with capture_logs() as logs:
            bars, stats = enrich_option_bars(
                [_row(_BAR_TS, close=180.0), _row(_BAR_TS + timedelta(minutes=1), close=181.0)],
                parts=_parts(),
                spot_by_ts={_BAR_TS: _SPOT, _BAR_TS + timedelta(minutes=1): _SPOT},
                rate_source=_RATE,
                style_flag=_STYLE,
            )
        assert stats.solved == 2
        assert len(bars) == 2
        assert [e for e in logs if "rust_batch" in str(e.get("event"))] == []


def _boundary_below_intrinsic_closes(
    *, is_call: bool, spot: Decimal, strike: float, t_years: float
) -> tuple[float, float]:
    """The exact float boundary pair of the BELOW_INTRINSIC band predicate
    ``close <= floor + 1e-9·close``: (last inside, first outside)."""
    rate = _RATE.rate_on(_BAR_TS.date())
    assert rate is not None
    floor = _no_arb_floor(
        is_call=is_call,
        spot=float(spot),
        strike=strike,
        rate=rate.rate,
        t_years=t_years,
    )
    c = floor / (1.0 - 1e-9)
    while c <= floor + 1e-9 * c:  # walk up to the first OUTSIDE value
        c = math.nextafter(c, math.inf)
    inside = math.nextafter(c, -math.inf)
    assert inside <= floor + 1e-9 * inside
    assert not (c <= floor + 1e-9 * c)
    return inside, c


class TestBatchParityHostileMatrix:
    """OFF ≡ ON over one hostile mixed contract — bars, stats and logs."""

    @staticmethod
    def _matrix_rows() -> tuple[list[dict[str, object]], dict[datetime, Decimal], CsvRateSource]:
        """One CE contract's rows covering every per-row family in ONE call."""
        expiry_at = expiry_instant_utc(_EXPIRY)
        t_at_bar = t_years_act365(_BAR_TS, expiry_at)
        inside, outside = _boundary_below_intrinsic_closes(
            is_call=True, spot=_SPOT, strike=18_000.0, t_years=t_at_bar
        )
        ts = [_BAR_TS + timedelta(minutes=i) for i in range(20)]
        # A CSV rate whose coverage starts 2024-01-15 — ts[1] lands on the
        # PRE-first-date 2024-01-14 (IST) → honest NO_RATE.
        rate_source = CsvRateSource(
            dates=(date(2024, 1, 15),), rates=(_RATE.rate,), source="csv:test"
        )
        pre_rate_ts = datetime(2024, 1, 14, 5, 0, tzinfo=UTC)
        # Exactly AT the 600 s floor (solvable) and one µs INSIDE it (AT_EXPIRY).
        at_floor_ts = expiry_at - timedelta(seconds=600)
        inside_floor_ts = at_floor_ts + timedelta(microseconds=1)
        rows = [
            _row(ts[0], close=180.0),  # ATM CE solve
            _row(pre_rate_ts, close=180.0),  # NO_RATE (CSV pre-first-date)
            _row(ts[2], close=float("nan")),  # non-finite OHLC → skip + WARN
            _row(ts[3], close=181.0),  # NO_SPOT (minute absent from the join)
            _row(ts[4], close=182.0),  # invalid spot (≤ 0) → WARN + NO_SPOT
            _row(ts[5], close=183.0),  # invalid spot (NaN) → WARN + NO_SPOT
            _row(ts[6], close=0.0, low=0.0, oi=None),  # close ≤ 0 → BAD_INPUT
            _row(at_floor_ts, close=5.0),  # exactly AT the T floor → solve path
            _row(inside_floor_ts, close=5.0),  # one µs inside → AT_EXPIRY
            _row(ts[9], close=inside, low=inside - 1.0, high=inside + 1.0),  # ±1 ULP inside band
            _row(ts[10], close=outside, low=outside - 1.0, high=outside + 1.0),  # first outside
            _row(ts[11], close=30_000.0),  # above the no-arb upper bound → NO_SOLUTION
            _row(ts[12], close=179.5),  # second solvable ATM row
            _row(ts[13], close=5.0),  # repr-boundary spot 100.005 joins here
        ]
        spots = {r["timestamp"]: _SPOT for r in rows}
        spots[ts[4]] = Decimal("0")  # non-positive join spot
        spots[ts[5]] = Decimal("NaN")  # non-finite join spot
        spots[ts[9]] = _SPOT
        spots[ts[13]] = Decimal("100.005")  # ROUND_HALF_UP repr boundary → 100.01
        del spots[ts[3]]  # the NO_SPOT minute
        # Strike 18000 rows need the deep-ITM band: give them their own spot.
        return rows, {k: v for k, v in spots.items() if isinstance(k, datetime)}, rate_source

    def test_off_and_on_bars_stats_and_logs_are_identical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows, spots, rate_source = self._matrix_rows()
        # The band rows were built for strike 18000 — run the matrix on a
        # deep-ITM-capable contract so the band predicate is genuinely hit.
        parts = _parts("NSE-NIFTY-25Jan24-18000-CE")
        (bars_off, stats_off, off_logs), (bars_on, stats_on, on_logs) = _run_both_arms(
            rows,
            parts=parts,
            spot_by_ts=spots,
            rate_source=rate_source,
            monkeypatch=monkeypatch,
            pre_engage=True,
        )
        assert [_bar_key(b) for b in bars_off] == [_bar_key(b) for b in bars_on]
        assert stats_off == stats_on
        assert stats_off.skipped == 1
        assert off_logs == on_logs  # event sequence, names AND fields

    def test_matrix_genuinely_covers_every_status_family(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows, spots, rate_source = self._matrix_rows()
        parts = _parts("NSE-NIFTY-25Jan24-18000-CE")
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "1")
        bars, stats = enrich_option_bars(
            rows, parts=parts, spot_by_ts=spots, rate_source=rate_source, style_flag=_STYLE
        )
        statuses = {unpack_greeks_provenance(bar.greeks_provenance_id or 0).status for bar in bars}
        assert {
            STATUS_OK,
            STATUS_NO_RATE,
            STATUS_NO_SPOT,
            STATUS_BAD_INPUT,
            STATUS_AT_EXPIRY,
            STATUS_BELOW_INTRINSIC,
            STATUS_NO_SOLUTION,
        } <= statuses
        assert stats.solved >= 2
        assert stats.skipped == 1

    def test_reuse_hit_and_every_gate_term_miss_are_identical_across_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A verbatim reuse hit plus one miss per reuse-gate term, OFF ≡ ON."""
        # Build the CURRENT-inputs overlay by solving the row once.
        base, _ = enrich_option_bars(
            [_row(_BAR_TS, close=180.0)],
            parts=_parts(),
            spot_by_ts={_BAR_TS: _SPOT},
            rate_source=_RATE,
            style_flag=_STYLE,
        )
        solved = base[0]
        ok_overlay: dict[str, object] = {
            name: getattr(solved, name) for name in GREEKS_OVERLAY_FIELDS
        }
        ok_overlay["spot_at_bar"] = float(str(solved.spot_at_bar))
        stale_pid = pack_greeks_provenance(
            formula_version=1, rate_source_id=1, style_flag=_STYLE, status=STATUS_OK
        )
        ts = [_BAR_TS + timedelta(minutes=i) for i in range(8)]
        rows = [
            _row(_BAR_TS, close=180.0, overlay=ok_overlay),  # reuse HIT
            _row(ts[1], close=180.0, overlay={**ok_overlay, "greeks_provenance_id": stale_pid}),
            _row(  # stored status non-OK (fresh NO_SOLUTION stamp)
                ts[2],
                close=180.0,
                overlay={
                    **ok_overlay,
                    "greeks_provenance_id": pack_greeks_provenance(
                        rate_source_id=1, style_flag=_STYLE, status=STATUS_NO_SOLUTION
                    ),
                },
            ),
            _row(  # style mismatch (stored AMERICAN vs this pass EUROPEAN)
                ts[3],
                close=180.0,
                overlay={
                    **ok_overlay,
                    "greeks_provenance_id": pack_greeks_provenance(
                        rate_source_id=1, style_flag=2, status=STATUS_OK
                    ),
                },
            ),
            _row(ts[4], close=180.0, overlay={**ok_overlay, "rate_used": 0.0999}),  # rate drift
            _row(ts[5], close=180.0, overlay={**ok_overlay, "spot_at_bar": 21_499.0}),  # spot
            _row(ts[6], close=180.0, overlay=ok_overlay),  # spot minute MISSING → recompute
        ]
        spots = {r["timestamp"]: _SPOT for r in rows}
        del spots[ts[6]]
        (bars_off, stats_off, _), (bars_on, stats_on, _) = _run_both_arms(
            rows,
            spot_by_ts={k: v for k, v in spots.items() if isinstance(k, datetime)},
            monkeypatch=monkeypatch,
            pre_engage=True,
        )
        assert [_bar_key(b) for b in bars_off] == [_bar_key(b) for b in bars_on]
        assert stats_off == stats_on
        assert stats_off.reused == 1  # ONLY the verbatim hit
        assert stats_off.solved + stats_off.nulls == len(rows) - 1

    def test_put_side_boundary_band_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The BELOW_INTRINSIC ±1 ULP band on the PE side (D2 graft)."""
        parts = _parts("NSE-NIFTY-25Jan24-24000-PE")
        t_at_bar = t_years_act365(_BAR_TS, expiry_instant_utc(_EXPIRY))
        inside, outside = _boundary_below_intrinsic_closes(
            is_call=False, spot=_SPOT, strike=24_000.0, t_years=t_at_bar
        )
        rows = [
            _row(_BAR_TS, close=inside, low=inside - 1.0, high=inside + 1.0),
            _row(
                _BAR_TS + timedelta(minutes=1),
                close=outside,
                low=outside - 1.0,
                high=outside + 1.0,
            ),
        ]
        spots = {r["timestamp"]: _SPOT for r in rows}
        (bars_off, stats_off, _), (bars_on, stats_on, _) = _run_both_arms(
            rows,
            parts=parts,
            spot_by_ts={k: v for k, v in spots.items() if isinstance(k, datetime)},
            monkeypatch=monkeypatch,
        )
        assert [_bar_key(b) for b in bars_off] == [_bar_key(b) for b in bars_on]
        assert stats_off == stats_on
        inside_status = unpack_greeks_provenance(bars_on[0].greeks_provenance_id or 0).status
        assert inside_status == STATUS_BELOW_INTRINSIC

    def test_empty_rows_are_a_no_op_on_both_arms(self, monkeypatch: pytest.MonkeyPatch) -> None:
        (bars_off, stats_off, _), (bars_on, stats_on, _) = _run_both_arms(
            [], monkeypatch=monkeypatch
        )
        assert bars_off == bars_on == []
        assert stats_off == stats_on == EnrichStats(solved=0, nulls=0, reused=0, changed=0)

    def test_all_null_contract_never_dispatches_the_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag ON with zero solvable rows: pending stays empty, the wrapper
        is never consulted (bomb-spy), output equals OFF."""
        rows = [_row(_BAR_TS, close=180.0), _row(_BAR_TS + timedelta(minutes=1), close=181.0)]
        spots: dict[datetime, Decimal] = {}  # every row NO_SPOT
        (bars_off, stats_off, _), _ = _run_both_arms(
            rows, spot_by_ts=spots, monkeypatch=monkeypatch
        )
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "1")
        reset_rust_batch_for_tests()

        def _bomb(*_a: object, **_k: object) -> object:
            raise AssertionError("no pending rows — batch must not dispatch")

        monkeypatch.setattr(greeks_enrich, "compute_greeks_batch", _bomb)
        bars_on, stats_on = enrich_option_bars(
            rows, parts=_parts(), spot_by_ts=spots, rate_source=_RATE, style_flag=_STYLE
        )
        assert [_bar_key(b) for b in bars_off] == [_bar_key(b) for b in bars_on]
        assert stats_off == stats_on
        assert stats_on.nulls == 2


class TestBatchOrderingInterleave:
    """Reused / skipped / pending interleave keeps ascending input order."""

    def test_interleaved_positions_are_index_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base, _ = enrich_option_bars(
            [_row(_BAR_TS, close=180.0)],
            parts=_parts(),
            spot_by_ts={_BAR_TS: _SPOT},
            rate_source=_RATE,
            style_flag=_STYLE,
        )
        ok_overlay: dict[str, object] = {
            name: getattr(base[0], name) for name in GREEKS_OVERLAY_FIELDS
        }
        ok_overlay["spot_at_bar"] = float(str(base[0].spot_at_bar))
        ts = [_BAR_TS + timedelta(minutes=i) for i in range(6)]
        rows = [
            _row(_BAR_TS, close=180.0, overlay=ok_overlay),  # 0: reuse
            _row(ts[1], close=181.0),  # 1: pending (solve)
            _row(ts[2], close=float("inf")),  # 2: skipped (no bar emitted)
            _row(ts[3], close=182.0),  # 3: pending (solve)
            _row(ts[4], close=183.0),  # 4: NO_SPOT null
            _row(ts[5], close=184.0),  # 5: pending (solve)
        ]
        spots = {r["timestamp"]: _SPOT for r in rows}
        del spots[ts[4]]
        (bars_off, _, _), (bars_on, _, _) = _run_both_arms(
            rows,
            spot_by_ts={k: v for k, v in spots.items() if isinstance(k, datetime)},
            monkeypatch=monkeypatch,
        )
        expected_ts = [_BAR_TS, ts[1], ts[3], ts[4], ts[5]]  # skip drops ts[2]
        assert [b.timestamp for b in bars_on] == expected_ts
        assert [_bar_key(b) for b in bars_off] == [_bar_key(b) for b in bars_on]


class TestFillInCompleteness:
    """Every returned element is a real bar — no placeholder ever escapes."""

    @pytest.mark.parametrize("flag", ["0", "1"])
    def test_length_and_types_on_both_arms(
        self, flag: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, flag)
        ts = [_BAR_TS + timedelta(minutes=i) for i in range(5)]
        rows = [
            _row(ts[0], close=180.0),
            _row(ts[1], close=float("nan")),  # skipped
            _row(ts[2], close=181.0),
            _row(ts[3], close=182.0),  # NO_SPOT
            _row(ts[4], close=183.0),
        ]
        spots = {ts[0]: _SPOT, ts[2]: _SPOT, ts[4]: _SPOT}
        bars, stats = enrich_option_bars(
            rows, parts=_parts(), spot_by_ts=spots, rate_source=_RATE, style_flag=_STYLE
        )
        assert len(bars) == len(rows) - stats.skipped
        assert stats.skipped == 1
        assert all(isinstance(bar, FnoGreeksBar) for bar in bars)


class TestAutorevertLatch:
    """A batch-path fault ships the oracle values + latches OFF, loudly."""

    @staticmethod
    def _solve_rows() -> tuple[list[dict[str, object]], dict[datetime, Decimal]]:
        ts = [_BAR_TS + timedelta(minutes=i) for i in range(3)]
        rows = [_row(t, close=180.0 + i) for i, t in enumerate(ts)]
        return rows, dict.fromkeys(ts, _SPOT)

    @pytest.mark.parametrize(
        ("exc", "name"),
        [
            (ValueError("invalid_input: greeks_batch column lengths differ — x"), "ValueError"),
            (
                AttributeError("module 'trading_core' has no attribute 'greeks_batch'"),
                "AttributeError",
            ),
        ],
    )
    def test_exception_ships_oracle_bytes_and_latches(
        self, exc: Exception, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows, spots = self._solve_rows()
        (bars_off, stats_off, _), _ = _run_both_arms(
            rows, spot_by_ts=spots, monkeypatch=monkeypatch
        )
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "1")
        reset_rust_batch_for_tests()

        def _raise(*_a: object, **_k: object) -> object:
            raise exc

        monkeypatch.setattr(greeks_enrich, "compute_greeks_batch", _raise)
        with capture_logs() as logs:
            bars_on, stats_on = enrich_option_bars(
                rows, parts=_parts(), spot_by_ts=spots, rate_source=_RATE, style_flag=_STYLE
            )
        assert [_bar_key(b) for b in bars_off] == [_bar_key(b) for b in bars_on]
        assert stats_off == stats_on
        errors = [e for e in logs if e["event"] == "greeks_enrich.rust_batch_autoreverted"]
        assert len(errors) == 1
        assert errors[0]["reason"] == "exception"
        assert errors[0]["error_type"] == name
        # The message rides repr()'d (house log-injection convention) — no
        # raw newline/escape could forge a log line through the payload.
        assert errors[0]["error"] == repr(str(exc))
        assert _RUST_BATCH_STATE.disabled is True

        # The latch holds: the NEXT call never dispatches (bomb-spy).
        def _bomb(*_a: object, **_k: object) -> object:
            raise AssertionError("latched process must never dispatch again")

        monkeypatch.setattr(greeks_enrich, "compute_greeks_batch", _bomb)
        bars_again, stats_again = enrich_option_bars(
            rows, parts=_parts(), spot_by_ts=spots, rate_source=_RATE, style_flag=_STYLE
        )
        assert [_bar_key(b) for b in bars_again] == [_bar_key(b) for b in bars_off]
        assert stats_again == stats_off

    def test_memory_error_re_raises_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows, spots = self._solve_rows()
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "1")

        def _oom(*_a: object, **_k: object) -> object:
            raise MemoryError

        monkeypatch.setattr(greeks_enrich, "compute_greeks_batch", _oom)
        with pytest.raises(MemoryError):
            enrich_option_bars(
                rows, parts=_parts(), spot_by_ts=spots, rate_source=_RATE, style_flag=_STYLE
            )
        # The fence deliberately does NOT latch on MemoryError — the raise
        # propagates to the pool/CLI boundary (house law).
        assert _RUST_BATCH_STATE.disabled is False

    def test_reset_restores_all_four_fields(self) -> None:
        _RUST_BATCH_STATE.disabled = True
        _RUST_BATCH_STATE.self_check_left = 0
        _RUST_BATCH_STATE.engaged = True
        _RUST_BATCH_STATE.flag_warned = True
        reset_rust_batch_for_tests()
        assert _RUST_BATCH_STATE.disabled is False
        assert _RUST_BATCH_STATE.self_check_left == 16
        assert _RUST_BATCH_STATE.engaged is False
        assert _RUST_BATCH_STATE.flag_warned is False


class TestBurnInSelfCheck:
    """The first 16 batch-served elements per process are oracle-verified."""

    @staticmethod
    def _rows(
        n: int, start_minute: int = 0
    ) -> tuple[list[dict[str, object]], dict[datetime, Decimal]]:
        ts = [_BAR_TS + timedelta(minutes=start_minute + i) for i in range(n)]
        rows = [_row(t, close=180.0 + (i % 7)) for i, t in enumerate(ts)]
        return rows, dict.fromkeys(ts, _SPOT)

    def test_perturbed_wheel_ships_oracle_values_and_latches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows, spots = self._rows(4)
        (bars_off, stats_off, _), _ = _run_both_arms(
            rows, spot_by_ts=spots, monkeypatch=monkeypatch
        )
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "1")
        reset_rust_batch_for_tests()
        real = compute_greeks_batch

        def _perturbed(requests: object) -> list[GreeksBatchOutcome]:
            outcomes = real(requests)  # type: ignore[arg-type]
            first = outcomes[0]
            assert first.result is not None
            poisoned = dataclasses.replace(first.result, iv=first.result.iv + 1e-12)
            return [GreeksBatchOutcome(result=poisoned, invalid_input=None), *outcomes[1:]]

        monkeypatch.setattr(greeks_enrich, "compute_greeks_batch", _perturbed)
        with capture_logs() as logs:
            bars_on, stats_on = enrich_option_bars(
                rows, parts=_parts(), spot_by_ts=spots, rate_source=_RATE, style_flag=_STYLE
            )
        # The oracle values ship — bit-equal to the OFF arm, NOT the poison.
        assert [_bar_key(b) for b in bars_on] == [_bar_key(b) for b in bars_off]
        assert stats_on == stats_off
        errors = [e for e in logs if e["event"] == "greeks_enrich.rust_batch_autoreverted"]
        assert len(errors) == 1
        assert errors[0]["reason"] == "self_check_mismatch"
        assert errors[0]["slot"] == 0
        assert _RUST_BATCH_STATE.disabled is True
        assert [e for e in logs if e["event"] == "greeks_enrich.rust_batch_engaged"] == []

    def test_clean_batches_engage_exactly_once_per_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "1")
        with capture_logs() as logs:
            for start in (0, 100):
                rows, spots = self._rows(3, start_minute=start)
                enrich_option_bars(
                    rows, parts=_parts(), spot_by_ts=spots, rate_source=_RATE, style_flag=_STYLE
                )
        engaged = [e for e in logs if e["event"] == "greeks_enrich.rust_batch_engaged"]
        assert len(engaged) == 1

    def test_budget_decrements_and_stops_comparing_after_16(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "1")
        oracle_calls: list[int] = []
        real_solve = greeks_enrich._solve_one

        def _counting_solve(p: object) -> object:
            oracle_calls.append(1)
            return real_solve(p)  # type: ignore[arg-type]

        monkeypatch.setattr(greeks_enrich, "_solve_one", _counting_solve)
        rows, spots = self._rows(20)
        enrich_option_bars(
            rows, parts=_parts(), spot_by_ts=spots, rate_source=_RATE, style_flag=_STYLE
        )
        assert len(oracle_calls) == 16  # min(16, 20) burn-in solves only
        assert _RUST_BATCH_STATE.self_check_left == 0
        oracle_calls.clear()
        rows2, spots2 = self._rows(20, start_minute=100)
        enrich_option_bars(
            rows2, parts=_parts(), spot_by_ts=spots2, rate_source=_RATE, style_flag=_STYLE
        )
        assert oracle_calls == []  # budget exhausted — pure batch serving

    def test_budget_spans_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "1")
        rows, spots = self._rows(5)
        enrich_option_bars(
            rows, parts=_parts(), spot_by_ts=spots, rate_source=_RATE, style_flag=_STYLE
        )
        assert _RUST_BATCH_STATE.self_check_left == 11
        rows2, spots2 = self._rows(5, start_minute=50)
        enrich_option_bars(
            rows2, parts=_parts(), spot_by_ts=spots2, rate_source=_RATE, style_flag=_STYLE
        )
        assert _RUST_BATCH_STATE.self_check_left == 6


class TestChunking:
    """The GREEKS_BATCH_MAX_ROWS chunk law: bounded crossings, one order."""

    def test_chunked_dispatch_preserves_order_and_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows, spots = TestBurnInSelfCheck._rows(10)
        (bars_off, stats_off, _), _ = _run_both_arms(
            rows, spot_by_ts=spots, monkeypatch=monkeypatch
        )
        monkeypatch.setenv(GREEKS_RUST_BATCH_ENV, "1")
        reset_rust_batch_for_tests()
        monkeypatch.setattr(greeks_enrich, "GREEKS_BATCH_MAX_ROWS", 4)
        calls: list[int] = []
        real = compute_greeks_batch

        def _spy(requests: Sequence[object]) -> object:
            calls.append(len(requests))
            return real(requests)  # type: ignore[arg-type]

        monkeypatch.setattr(greeks_enrich, "compute_greeks_batch", _spy)
        bars_on, stats_on = enrich_option_bars(
            rows, parts=_parts(), spot_by_ts=spots, rate_source=_RATE, style_flag=_STYLE
        )
        assert calls == [4, 4, 2]  # 10 pending rows through a cap of 4
        assert [_bar_key(b) for b in bars_on] == [_bar_key(b) for b in bars_off]
        assert stats_on == stats_off

    def test_default_cap_is_the_documented_constant(self) -> None:
        assert GREEKS_BATCH_MAX_ROWS == 65_536
