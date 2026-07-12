"""Integration gate for the DEC-FEED-RUST-CORE-001 seam in
:func:`brutex.feed.replay.arrow_table_to_bars`.

Proves, end to end through the REAL public entry point:

* the default-OFF law (a bomb kernel is NEVER consulted with the flag unset
  — the slice-7 ``_BombKernel`` precedent);
* every degrade token (wheel_missing / schema_mismatch / null_column /
  ts_out_of_ist_fixed_range / kernel_invalid / kernel_error) falls back to
  OUTPUT that is oracle-identical — including exception TYPE + message
  parity for null / naive / int-price / ``timestamp[ms]`` / missing-column
  tables;
* the kill-switch interplay (``BRUTEX_BAD_TICK_SCREEN=off`` + flag ON →
  ``ts_us=[]``, spike kept, no warn, ON ≡ OFF);
* the emit_at_row replay-law ordering end to end, with ``capture_logs``
  full-sequence equality against the OFF arm for the four interleaving
  shapes (positivity-at-boundary, warned-session-then-constructor-raise,
  clean multi-session finish, last-kept-row warn before a positivity stop);
* the structural bit-identity pin that the ONLY ``Decimal(`` / price-``str(``
  materialization sites in replay.py live inside ``_build_bar``.
"""

from __future__ import annotations

import ast
import sys
import types
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from brutex import feed_rust
from brutex.data.bad_tick import bad_tick_screen_enabled
from brutex.feed import replay
from brutex.feed.replay import arrow_table_to_bars
from brutex.feed_rust import FEED_RUST_ENV, FEED_RUST_FALLBACK_EVENT, reset_for_tests
from structlog.testing import capture_logs

#: 09:15 IST on 2024-01-02 — the canonical session anchor.
_BASE = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)

#: One canonical row: (ts, open, high, low, close, volume, open_interest).
Row = tuple[Any, float, float, float, float, Any, Any]

_SOURCE = "NSE/INDEX/NSE-NIFTY/1minute"


@pytest.fixture(autouse=True)
def _fresh_warn_registry() -> None:
    """Isolate the feed_rust warn-once registry between tests."""
    reset_for_tests()


def _table(
    rows: Sequence[Any],
    *,
    timestamp_type: pa.DataType | None = None,
    price_type: pa.DataType | None = None,
    volume_type: pa.DataType | None = None,
) -> pa.Table:
    """Build a bar table; type overrides mutate ONE column family at a time."""
    return pa.table(
        {
            "timestamp": pa.array(
                [r[0] for r in rows],
                type=timestamp_type if timestamp_type is not None else pa.timestamp("us", tz="UTC"),
            ),
            "open": pa.array(
                [r[1] for r in rows], type=price_type if price_type is not None else pa.float64()
            ),
            "high": pa.array([r[2] for r in rows], type=pa.float64()),
            "low": pa.array([r[3] for r in rows], type=pa.float64()),
            "close": pa.array([r[4] for r in rows], type=pa.float64()),
            "volume": pa.array(
                [r[5] for r in rows], type=volume_type if volume_type is not None else pa.int64()
            ),
            "open_interest": pa.array([r[6] for r in rows], type=pa.int64()),
        }
    )


def _clean_rows() -> list[Row]:
    return [
        (_BASE, 27940.0, 27950.0, 27930.0, 27945.0, 10, None),
        (_BASE + timedelta(minutes=1), 27946.0, 27960.0, 27940.0, 27955.0, 12, 5),
        (_BASE + timedelta(minutes=2), 27955.0, 27980.0, 27950.0, 27970.0, 9, None),
    ]


def _spike_rows() -> list[Row]:
    """Session 1 with a ÷10 spike + a next-session reopen gap (kept 3, skip 1)."""
    return [
        (_BASE, 27940.0, 27950.0, 27930.0, 27945.0, 10, None),
        (_BASE + timedelta(minutes=1), 2794.5, 2795.0, 2793.0, 2794.5, 10, None),
        (_BASE + timedelta(minutes=2), 27946.0, 27960.0, 27940.0, 27955.0, 10, None),
        (_BASE + timedelta(days=1), 6000.0, 6010.0, 5990.0, 6005.0, 10, None),
    ]


def _run_both_arms(
    table: pa.Table,
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: str | None = _SOURCE,
) -> tuple[list[Any], list[Any], Sequence[Any], Sequence[Any]]:
    """Drive OFF then ON over *table*; return (off_bars, on_bars, off_logs, on_logs)."""
    monkeypatch.delenv(FEED_RUST_ENV, raising=False)
    with capture_logs() as off_logs:
        off = arrow_table_to_bars(table, source=source)
    reset_for_tests()
    monkeypatch.setenv(FEED_RUST_ENV, "1")
    with capture_logs() as on_logs:
        on = arrow_table_to_bars(table, source=source)
    return off, on, off_logs, on_logs


def _assert_bars_str_identical(off: list[Any], on: list[Any]) -> None:
    """Field-level ``str()`` equality — stricter than ``==`` (Decimal is numeric)."""
    assert off == on
    for a, b in zip(off, on, strict=True):
        assert a.timestamp.isoformat() == b.timestamp.isoformat()
        assert str(a.open) == str(b.open)
        assert str(a.high) == str(b.high)
        assert str(a.low) == str(b.low)
        assert str(a.close) == str(b.close)
        assert repr(a.volume) == repr(b.volume)
        assert repr(a.open_interest) == repr(b.open_interest)


def _bad_tick_events(logs: Sequence[Any]) -> list[Any]:
    return [rec for rec in logs if rec["event"] == "feed.bad_tick_skipped"]


def _fallback_reasons(logs: Sequence[Any]) -> list[str]:
    return [str(rec["reason"]) for rec in logs if rec["event"] == FEED_RUST_FALLBACK_EVENT]


# ── Default-OFF proof (the bomb-kernel pin, slice-7 precedent) ──────────────


class TestDefaultOff:
    """Flag unset/absent ⇒ the kernel machinery is NEVER consulted."""

    def test_bomb_kernel_never_consulted_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _bomb() -> object:
            raise AssertionError("resolve_feed_kernel consulted with BRUTEX_FEED_RUST unset")

        monkeypatch.setattr(replay, "resolve_feed_kernel", _bomb)
        monkeypatch.delenv(FEED_RUST_ENV, raising=False)
        bars = arrow_table_to_bars(_table(_clean_rows()), source=_SOURCE)
        assert len(bars) == 3
        assert str(bars[0].open) == "27940.0"

    def test_bomb_kernel_never_consulted_flag_falsy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _bomb() -> object:
            raise AssertionError("resolve_feed_kernel consulted with BRUTEX_FEED_RUST=0")

        monkeypatch.setattr(replay, "resolve_feed_kernel", _bomb)
        monkeypatch.setenv(FEED_RUST_ENV, "0")
        assert len(arrow_table_to_bars(_table(_spike_rows()), source=_SOURCE)) == 3


# ── Engagement + happy-path parity ──────────────────────────────────────────


class TestKernelEngagement:
    """Flag ON + wheel present ⇒ the kernel genuinely decides."""

    def test_kernel_engaged_and_parity_on_clean_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("brutex_feed", reason="wheel required for the engagement pin")
        real = feed_rust.resolve_feed_kernel()
        assert real is not None
        calls: list[tuple[Any, ...]] = []

        def _spy(*args: Any) -> Any:
            calls.append(args)
            return real(*args)

        monkeypatch.setattr(replay, "resolve_feed_kernel", lambda: _spy)
        off, on, off_logs, on_logs = _run_both_arms(_table(_clean_rows()), monkeypatch)
        assert len(calls) == 1, "the ON arm must have consulted the kernel exactly once"
        _assert_bars_str_identical(off, on)
        assert off_logs == on_logs == []

    def test_parity_and_warn_equality_on_spike_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("brutex_feed", reason="wheel required for the parity pin")
        off, on, off_logs, on_logs = _run_both_arms(_table(_spike_rows()), monkeypatch)
        _assert_bars_str_identical(off, on)
        assert _bad_tick_events(off_logs) == _bad_tick_events(on_logs)
        assert len(_bad_tick_events(on_logs)) == 1
        assert _fallback_reasons(on_logs) == []

    def test_extra_column_is_ignored_and_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("brutex_feed", reason="wheel required for the extra-column pin")
        base = _table(_clean_rows())
        extra = base.append_column("extra", pa.array([1.0, 2.0, 3.0], type=pa.float64()))
        off, on, _, on_logs = _run_both_arms(extra, monkeypatch)
        _assert_bars_str_identical(off, on)
        assert _fallback_reasons(on_logs) == []

    def test_empty_table_short_circuits_before_the_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(FEED_RUST_ENV, "1")

        def _bomb() -> object:
            raise AssertionError("empty table must return [] before the kernel branch")

        monkeypatch.setattr(replay, "resolve_feed_kernel", _bomb)
        assert arrow_table_to_bars(_table([]), source=_SOURCE) == []


# ── Degrade matrix — every token, oracle-identical output ───────────────────


class TestDegradeMatrix:
    """Each eligibility refusal warns once and serves the oracle's exact output."""

    def test_wheel_missing_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_feed", None)
        off, on, off_logs, on_logs = _run_both_arms(_table(_spike_rows()), monkeypatch)
        _assert_bars_str_identical(off, on)
        assert _fallback_reasons(on_logs) == ["wheel_missing"]
        assert _bad_tick_events(off_logs) == _bad_tick_events(on_logs)

    def test_wheel_stale_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_feed", types.ModuleType("brutex_feed"))
        off, on, _, on_logs = _run_both_arms(_table(_clean_rows()), monkeypatch)
        _assert_bars_str_identical(off, on)
        assert _fallback_reasons(on_logs) == ["wheel_stale"]

    @pytest.mark.parametrize(
        ("kwargs", "column"),
        [
            ({"timestamp_type": pa.timestamp("ms", tz="UTC")}, "timestamp"),
            ({"price_type": pa.int64()}, "open"),
            ({"volume_type": pa.int32()}, "volume"),
        ],
    )
    def test_schema_mismatch_degrades_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], column: str
    ) -> None:
        pytest.importorskip("brutex_feed", reason="wheel required for the schema gate")
        rows = _clean_rows()
        if "price_type" in kwargs:
            # An int64 OPEN column: Decimal(str(27940)) == Decimal("27940"),
            # NOT "27940.0" — the input-shape laundering class the gate
            # refuses to let the kernel classify (risk #4). Each row keeps
            # its OWN open (int-truncated, still inside [low, high]).
            rows = [(r[0], int(r[1]), r[2], r[3], r[4], r[5], r[6]) for r in rows]
        table = _table(rows, **kwargs)
        off, on, _, on_logs = _run_both_arms(table, monkeypatch)
        _assert_bars_str_identical(off, on)
        events = [rec for rec in on_logs if rec["event"] == FEED_RUST_FALLBACK_EVENT]
        assert [rec["reason"] for rec in events] == ["schema_mismatch"]
        assert events[0]["column"] == column

    def test_naive_timestamp_schema_degrades_with_exception_parity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("brutex_feed", reason="wheel required for the schema gate")
        naive_rows: list[Row] = [(datetime(2024, 1, 2, 9, 15), 100.0, 101.0, 99.0, 100.5, 10, None)]
        table = _table(naive_rows, timestamp_type=pa.timestamp("us"))
        monkeypatch.delenv(FEED_RUST_ENV, raising=False)
        with pytest.raises(ValueError, match="timezone-aware") as off_exc:
            arrow_table_to_bars(table, source=_SOURCE)
        monkeypatch.setenv(FEED_RUST_ENV, "1")
        with capture_logs() as on_logs, pytest.raises(ValueError, match="timezone-aware") as on_exc:
            arrow_table_to_bars(table, source=_SOURCE)
        assert str(off_exc.value) == str(on_exc.value)
        assert type(off_exc.value.__cause__) is type(on_exc.value.__cause__)
        assert _fallback_reasons(on_logs) == ["schema_mismatch"]

    @pytest.mark.parametrize(
        ("row", "expected_exc"),
        [
            # Null CLOSE: the oracle's positivity compare hits `None <= 0`.
            ((_BASE, 100.0, 101.0, 99.0, None, 10, None), TypeError),
            # Null VOLUME: the oracle's `int(None)` raises.
            ((_BASE, 100.0, 101.0, 99.0, 100.5, None, None), TypeError),
            # Null TIMESTAMP: the oracle's `.astimezone` on None raises.
            ((None, 100.0, 101.0, 99.0, 100.5, 10, None), AttributeError),
        ],
    )
    def test_null_column_degrades_with_exception_parity(
        self,
        monkeypatch: pytest.MonkeyPatch,
        row: Row,
        expected_exc: type[BaseException],
    ) -> None:
        pytest.importorskip("brutex_feed", reason="wheel required for the null gate")
        table = _table([row])
        monkeypatch.delenv(FEED_RUST_ENV, raising=False)
        with pytest.raises(expected_exc) as off_exc:
            arrow_table_to_bars(table, source=_SOURCE)
        monkeypatch.setenv(FEED_RUST_ENV, "1")
        with capture_logs() as on_logs, pytest.raises(expected_exc) as on_exc:
            arrow_table_to_bars(table, source=_SOURCE)
        assert str(off_exc.value) == str(on_exc.value)
        assert _fallback_reasons(on_logs) == ["null_column"]

    def test_null_open_interest_is_legal_never_degrades(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("brutex_feed", reason="wheel required for the OI-null pin")
        off, on, _, on_logs = _run_both_arms(_table(_clean_rows()), monkeypatch)
        assert off[0].open_interest is None
        _assert_bars_str_identical(off, on)
        assert _fallback_reasons(on_logs) == []

    def test_missing_column_raises_keyerror_in_both_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        table = _table(_clean_rows()).drop_columns(["open_interest"])
        monkeypatch.delenv(FEED_RUST_ENV, raising=False)
        with pytest.raises(KeyError) as off_exc:
            arrow_table_to_bars(table, source=_SOURCE)
        monkeypatch.setenv(FEED_RUST_ENV, "1")
        with pytest.raises(KeyError) as on_exc:
            arrow_table_to_bars(table, source=_SOURCE)
        # The shared to_pylist() extraction raises pyarrow's own KeyError at
        # the IDENTICAL site in both arms — the kernel branch is never reached.
        assert str(off_exc.value) == str(on_exc.value)

    def test_pre_1970_timestamp_abstains_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("brutex_feed", reason="wheel required for the abstain gate")
        rows: list[Row] = [
            (datetime(1969, 12, 31, 23, 59, tzinfo=UTC), 100.0, 101.0, 99.0, 100.5, 10, None)
        ]
        off, on, _, on_logs = _run_both_arms(_table(rows), monkeypatch)
        _assert_bars_str_identical(off, on)
        assert _fallback_reasons(on_logs) == ["ts_out_of_ist_fixed_range"]

    def test_kernel_error_belt_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raising_kernel(*_args: Any) -> Any:
            raise RuntimeError("kernel exploded")

        monkeypatch.setattr(replay, "resolve_feed_kernel", lambda: _raising_kernel)
        off, on, off_logs, on_logs = _run_both_arms(_table(_spike_rows()), monkeypatch)
        _assert_bars_str_identical(off, on)
        errors = [rec for rec in on_logs if rec["event"] == FEED_RUST_FALLBACK_EVENT]
        assert [rec["reason"] for rec in errors] == ["kernel_error"]
        assert errors[0]["log_level"] == "error"
        assert "RuntimeError" in str(errors[0]["error"])
        assert _bad_tick_events(off_logs) == _bad_tick_events(on_logs)

    def test_kernel_invalid_poison_degrades_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _poison_kernel(*_args: Any) -> Any:
            return ([2, 1], [], None)  # non-ascending kept rows

        monkeypatch.setattr(replay, "resolve_feed_kernel", lambda: _poison_kernel)
        off, on, _, on_logs = _run_both_arms(_table(_clean_rows()), monkeypatch)
        _assert_bars_str_identical(off, on)
        assert _fallback_reasons(on_logs) == ["kernel_invalid"]


# ── Kill-switch interplay ───────────────────────────────────────────────────


class TestBadTickKillSwitchInterplay:
    """``BRUTEX_BAD_TICK_SCREEN=off`` + flag ON: ts never crosses, spike kept."""

    def test_screen_off_passes_empty_ts_and_keeps_spike(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("brutex_feed", reason="wheel required for the interplay pin")
        real = feed_rust.resolve_feed_kernel()
        assert real is not None
        calls: list[tuple[Any, ...]] = []

        def _spy(*args: Any) -> Any:
            calls.append(args)
            return real(*args)

        monkeypatch.setattr(replay, "resolve_feed_kernel", lambda: _spy)
        monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "off")
        table = _table(_spike_rows())
        monkeypatch.delenv(FEED_RUST_ENV, raising=False)
        with capture_logs() as off_logs:
            off = arrow_table_to_bars(table, source=_SOURCE)
        monkeypatch.setenv(FEED_RUST_ENV, "1")
        with capture_logs() as on_logs:
            on = arrow_table_to_bars(table, source=_SOURCE)
        assert calls[0][0] == []  # ts_us NEVER crosses with the screen off
        assert calls[0][5] is False  # screen_enabled=False
        assert len(on) == 4  # the spike is KEPT
        _assert_bars_str_identical(off, on)
        assert _bad_tick_events(off_logs) == _bad_tick_events(on_logs) == []


# ── The three warn/raise interleaving laws (capture_logs sequence parity) ───


class TestOrderingLaws:
    """The emit_at_row replay law reproduces the oracle's side-effect order."""

    def _sequences(
        self,
        table: pa.Table,
        monkeypatch: pytest.MonkeyPatch,
        exc_type: type[BaseException],
    ) -> tuple[Sequence[Any], Sequence[Any], str, str]:
        """Drive OFF then ON over *table*, raising *exc_type* in both arms.

        R8 diagnosability hardening (MIGRATION #1741 REFUTER ROUND 8 — the
        R7c one-time anomaly closed unreproducible-unknown): the only state
        consistent with that symptom is the ON arm's bad-tick screen reading
        DISABLED, so each arm asserts the screen is enabled immediately
        before running, and the ON arm spies the kernel (the
        TestKernelEngagement idiom) to assert it was consulted exactly once
        with ``screen_enabled=True`` and returned the ``(kept, warns, stop)``
        shape — a recurrence now self-diagnoses as "screen disabled in the
        ON arm" instead of reading as "logs differ".
        """
        monkeypatch.delenv(FEED_RUST_ENV, raising=False)
        assert bad_tick_screen_enabled() is True
        with capture_logs() as off_logs, pytest.raises(exc_type) as off_exc:
            arrow_table_to_bars(table, source=_SOURCE)
        reset_for_tests()
        monkeypatch.setenv(FEED_RUST_ENV, "1")
        assert bad_tick_screen_enabled() is True
        real = feed_rust.resolve_feed_kernel()
        assert real is not None, "wheel required for the ordering pins"
        calls: list[tuple[Any, ...]] = []
        results: list[Any] = []

        def _spy(*args: Any) -> Any:
            calls.append(args)
            out = real(*args)
            results.append(out)
            return out

        monkeypatch.setattr(replay, "resolve_feed_kernel", lambda: _spy)
        with capture_logs() as on_logs, pytest.raises(exc_type) as on_exc:
            arrow_table_to_bars(table, source=_SOURCE)
        assert len(calls) == 1, "the ON arm must have consulted the kernel exactly once"
        assert calls[0][5] is True, "the ON arm's kernel must see screen_enabled=True"
        assert results[0] is not None, "in-envelope 2024 timestamps must never abstain"
        kept, warns, stop = results[0]  # the (kept, warns, stop) shape law
        assert isinstance(kept, list)
        assert isinstance(warns, list)
        assert stop is None or isinstance(stop, tuple)
        return off_logs, on_logs, str(off_exc.value), str(on_exc.value)

    def test_positivity_at_boundary_row_suppresses_flush_and_finish(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("brutex_feed", reason="wheel required for the ordering pins")
        rows: list[Row] = [
            (_BASE, 27940.0, 27950.0, 27930.0, 27945.0, 10, None),
            (_BASE + timedelta(minutes=1), 2794.5, 2795.0, 2793.0, 2794.5, 10, None),
            # The next SESSION's first row fails POSITIVITY: the owed session-1
            # flush is suppressed (positivity precedes keep()) and no finish
            # flush ever fires.
            (_BASE + timedelta(days=1), -5.0, 6010.0, 5990.0, 6005.0, 10, None),
        ]
        off_logs, on_logs, off_msg, on_msg = self._sequences(_table(rows), monkeypatch, ValueError)
        assert off_msg == on_msg
        assert "non-positive price" in on_msg
        assert _bad_tick_events(off_logs) == _bad_tick_events(on_logs) == []

    def test_warned_session_then_constructor_raise_emits_boundary_warns_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("brutex_feed", reason="wheel required for the ordering pins")
        rows: list[Row] = [
            (_BASE, 27940.0, 27950.0, 27930.0, 27945.0, 10, None),
            (_BASE + timedelta(minutes=1), 2794.5, 2795.0, 2793.0, 2794.5, 10, None),
            # Session 2: first bar KEPT (flushes session 1's warn), a spike
            # (owes a session-2 warn), then a constructor violation
            # (high < low) that raises BEFORE session 2's warn can flush.
            (_BASE + timedelta(days=1), 6000.0, 6010.0, 5990.0, 6005.0, 10, None),
            (_BASE + timedelta(days=1, minutes=1), 600.0, 601.0, 599.0, 600.5, 10, None),
            (_BASE + timedelta(days=1, minutes=2), 6001.0, 5990.0, 6010.0, 6001.0, 10, None),
        ]
        off_logs, on_logs, off_msg, on_msg = self._sequences(_table(rows), monkeypatch, ValueError)
        assert off_msg == on_msg
        assert "corrupt stored bar" in on_msg
        off_warns = _bad_tick_events(off_logs)
        on_warns = _bad_tick_events(on_logs)
        assert off_warns == on_warns
        assert len(on_warns) == 1  # ONLY session 1's boundary warn escaped
        assert on_warns[0]["session"] == "2024-01-02"

    def test_clean_multi_session_finish_flush_sequence_parity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("brutex_feed", reason="wheel required for the ordering pins")
        rows: list[Row] = [
            (_BASE, 27940.0, 27950.0, 27930.0, 27945.0, 10, None),
            (_BASE + timedelta(minutes=1), 2794.5, 2795.0, 2793.0, 2794.5, 10, None),
            (_BASE + timedelta(days=1), 6000.0, 6010.0, 5990.0, 6005.0, 10, None),
            (_BASE + timedelta(days=1, minutes=1), 600.0, 601.0, 599.0, 600.5, 10, None),
        ]
        off, on, off_logs, on_logs = _run_both_arms(_table(rows), monkeypatch)
        _assert_bars_str_identical(off, on)
        assert off_logs == on_logs  # FULL capture_logs sequence equality
        assert [rec["session"] for rec in _bad_tick_events(on_logs)] == [
            "2024-01-02",
            "2024-01-03",
        ]

    def test_boundary_warn_on_last_kept_row_fires_before_positivity_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R6a regression (MIGRATION #1741 REFUTER ROUND 6): the warn-drain
        boundary mutant ``<= i -> < i`` in ``_bars_from_scan``.

        Kernel shape: ``kept=[0, 2]``, ``warns=[(2, 0, 1, 0, 1)]``,
        ``stop=(3, 0)`` — session 1's owed bad-tick warn anchors on kept
        row 2 (session 2's first row = the LAST kept row) and row 3 fails
        positivity, so the trailing drain after the stop-raise is
        unreachable.  The warn MUST fire from the in-loop drain (``<= i``)
        before the raise; under the mutant it is silently dropped and the
        full-sequence parity below fails.  Kill-proof recorded in the
        impl-log: mutant applied by hand -> this test FAILS; restored ->
        PASSES.
        """
        pytest.importorskip("brutex_feed", reason="wheel required for the ordering pins")
        rows: list[Row] = [
            (_BASE, 27940.0, 27950.0, 27930.0, 27945.0, 10, None),
            (_BASE + timedelta(minutes=1), 2794.5, 2795.0, 2793.0, 2794.5, 10, None),
            # Session 2's first row: KEPT — session 1's owed warn anchors
            # HERE, on what turns out to be the LAST kept row...
            (_BASE + timedelta(days=1), 6000.0, 6010.0, 5990.0, 6005.0, 10, None),
            # ...because the very next row fails POSITIVITY (open <= 0).
            (_BASE + timedelta(days=1, minutes=1), -5.0, 6010.0, 5990.0, 6005.0, 10, None),
        ]
        off_logs, on_logs, off_msg, on_msg = self._sequences(_table(rows), monkeypatch, ValueError)
        assert off_msg == on_msg
        assert "non-positive price" in on_msg
        assert off_logs == on_logs  # FULL sequence: the WARN, THEN the raise
        warns = _bad_tick_events(on_logs)
        assert len(warns) == 1
        assert warns[0]["session"] == "2024-01-02"

    def test_source_none_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("brutex_feed", reason="wheel required for the source-None pin")
        off, on, off_logs, on_logs = _run_both_arms(_table(_spike_rows()), monkeypatch, source=None)
        _assert_bars_str_identical(off, on)
        warns = _bad_tick_events(on_logs)
        assert warns == _bad_tick_events(off_logs)
        assert warns[0]["source"] is None


# ── Structural bit-identity pin: materialization lives ONLY in _build_bar ───


class TestMaterializationPin:
    """The only ``Decimal(`` / ``str(`` call sites in replay.py are `_build_bar`."""

    def test_decimal_and_str_calls_confined_to_build_bar(self) -> None:
        source = Path(replay.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders: list[tuple[str, str]] = []

        class _Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.stack: list[str] = ["<module>"]

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                """Track the enclosing function name while walking calls."""
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def visit_Call(self, node: ast.Call) -> None:
                """Record Decimal()/str() calls outside _build_bar."""
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id in {"Decimal", "str"}
                    and self.stack[-1] != "_build_bar"
                ):
                    offenders.append((self.stack[-1], node.func.id))
                self.generic_visit(node)

        _Visitor().visit(tree)
        assert offenders == [], offenders

    def test_bars_from_scan_result_matches_manual_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Belt: the ON-arm output bar VALUES equal a hand-built expectation
        # (not merely OFF-arm equality) — 100.5 renders "100.5", never
        # "100.50"; integer 20000.0 renders "20000.0".
        pytest.importorskip("brutex_feed", reason="wheel required")
        rows: list[Row] = [(_BASE, 20000.0, 20000.5, 19999.5, 20000.0, 7, 3)]
        monkeypatch.setenv(FEED_RUST_ENV, "1")
        (bar,) = arrow_table_to_bars(_table(rows), source=None)
        assert str(bar.open) == "20000.0"
        assert str(bar.high) == "20000.5"
        assert str(bar.low) == "19999.5"
        assert bar.volume == 7
        assert bar.open_interest == 3
        assert bar.open == Decimal("20000.0")
