"""Integration gate for the wheel-REQUIRED DEC-FEED-RUST-MATERIALIZE-002
contract in :func:`brutex.feed.replay.arrow_table_to_bars`.

The Python oracle loop was DELETED — the strict ``brutex_feed`` Rust kernel
is the ONLY decision path. Proves, end to end through the REAL public entry
point:

* the wheel-REQUIRED law (a missing wheel is a typed ``FeedError`` naming
  the rebuild remediation; an EMPTY table returns ``[]`` without ever
  touching the wheel);
* the retired-flag law (a SET falsy/junk ``BRUTEX_FEED_MATERIALIZE_RUST``
  or legacy ``BRUTEX_FEED_RUST`` refuses loudly; truthy/unset proceed);
* happy-path materialization (``Decimal(str(float))`` exact bytes, int
  volume, ``open_interest`` ``None`` passthrough);
* the null gate (a null in ANY consulted column is ONE typed
  corrupt-column ``ValueError``; a null ``open_interest`` alone is legal);
* the strict stops (non-positive → the DEC-STORED-DATA-VALIDATION-001
  refusal; inf/NaN/-inf → the DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001
  refusal — finiteness checked BEFORE positivity, so ``-inf`` reads
  non-FINITE);
* the emit_at_row warn/raise interleaving laws with ABSOLUTE
  ``capture_logs`` expectations (one WARN per spiked session; a warned
  session's boundary WARN escapes before a later-session positivity stop;
  a pending same-session warn is suppressed by a non-finite raise; the
  ``BRUTEX_BAD_TICK_SCREEN=off`` kill switch keeps the spike, emits zero
  warns and passes ``ts_us == []`` to the kernel);
* non-canonical-table materialization fidelity (int64 prices, ms
  timestamps, naive/string-typed columns, missing columns);
* the fixed-IST envelope refusal on an out-of-envelope consulted
  timestamp (and the screen-off bypass — ts never consulted);
* the refuse-poison law over a structurally invalid / raising kernel
  (``MemoryError`` re-raises RAW);
* the structural pin that the ONLY ``Decimal(`` / price-``str(``
  materialization sites in replay.py live inside ``_build_bar``.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
import pytest
from brutex import feed_rust
from brutex.feed import replay
from brutex.feed.errors import FeedError
from brutex.feed.replay import arrow_table_to_bars
from brutex.feed_rust import FEED_RUST_ENV, MATERIALIZE_RUST_ENV
from structlog.testing import capture_logs

#: 09:15 IST on 2024-01-02 — the canonical session anchor.
_BASE = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)

#: One canonical row: (ts, open, high, low, close, volume, open_interest).
Row = tuple[Any, float, float, float, float, Any, Any]

_SOURCE = "NSE/INDEX/NSE-NIFTY/1minute"


def _pylist_repr(ts: datetime) -> str:
    """The repr of *ts* as pyarrow's ``to_pylist()`` materializes it.

    pyarrow hands back ``ZoneInfo("UTC")``-tagged datetimes (not
    ``datetime.UTC``), and the corrupt-bar messages embed THAT object's
    repr — pin the exact bytes.
    """
    return repr(ts.replace(tzinfo=ZoneInfo("UTC")))


@pytest.fixture(autouse=True)
def _clean_flag_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from the unset-flag default (the shipping arm)."""
    monkeypatch.delenv(MATERIALIZE_RUST_ENV, raising=False)
    monkeypatch.delenv(FEED_RUST_ENV, raising=False)


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


def _bad_tick_events(logs: Sequence[Any]) -> list[Any]:
    return [rec for rec in logs if rec["event"] == "feed.bad_tick_skipped"]


class _KernelSpy:
    """Wrap the REAL wheel, recording every ``scan_bars_strict`` call."""

    def __init__(self) -> None:
        self._real = feed_rust.require_feed_kernel()
        self.calls: list[tuple[Any, ...]] = []

    def scan_bars_strict(self, *args: Any) -> Any:
        self.calls.append(args)
        return self._real.scan_bars_strict(*args)  # type: ignore[attr-defined]


# ── 1. The wheel-REQUIRED law ───────────────────────────────────────────────


class TestWheelRequired:
    """A missing wheel refuses LOUDLY; an empty table never consults it."""

    def test_missing_wheel_raises_typed_feed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_feed", None)
        with pytest.raises(FeedError, match="REQUIRED for feed materialization") as exc:
            arrow_table_to_bars(_table(_clean_rows()), source=_SOURCE)
        assert "maturin develop" in str(exc.value)
        assert "DEC-FEED-RUST-MATERIALIZE-002" in str(exc.value)

    def test_empty_table_returns_empty_without_touching_the_wheel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "brutex_feed", None)

        def _bomb() -> object:
            raise AssertionError("an empty table must return [] before the kernel resolve")

        monkeypatch.setattr(replay, "require_feed_kernel", _bomb)
        assert arrow_table_to_bars(_table([]), source=_SOURCE) == []


# ── 2. The retired-flag law ─────────────────────────────────────────────────


class TestRetiredFlagLaw:
    """A SET falsy/junk flag refuses naming the DEC; truthy/unset proceed."""

    @pytest.mark.parametrize("env", [MATERIALIZE_RUST_ENV, FEED_RUST_ENV])
    def test_falsy_flag_refuses_naming_the_deleted_arm(
        self, monkeypatch: pytest.MonkeyPatch, env: str
    ) -> None:
        monkeypatch.setenv(env, "0")
        with pytest.raises(FeedError, match="DELETED Python feed-materialization arm") as exc:
            arrow_table_to_bars(_table(_clean_rows()), source=_SOURCE)
        assert env in str(exc.value)

    def test_junk_flag_refuses_as_unrecognised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MATERIALIZE_RUST_ENV, "bananas")
        with pytest.raises(FeedError, match="unrecognised"):
            arrow_table_to_bars(_table(_clean_rows()), source=_SOURCE)

    @pytest.mark.parametrize("env", [MATERIALIZE_RUST_ENV, FEED_RUST_ENV])
    def test_truthy_flag_serves_normally(self, monkeypatch: pytest.MonkeyPatch, env: str) -> None:
        monkeypatch.setenv(env, "1")
        bars = arrow_table_to_bars(_table(_clean_rows()), source=_SOURCE)
        assert len(bars) == 3


# ── 3. Happy-path materialization ───────────────────────────────────────────


class TestHappyPath:
    """Canonical tables materialize the deleted oracle's exact bytes."""

    def test_clean_table_serves_decimal_str_exact_bars(self) -> None:
        bars = arrow_table_to_bars(_table(_clean_rows()), source=_SOURCE)
        assert len(bars) == 3
        assert str(bars[0].open) == "27940.0"
        assert str(bars[0].close) == "27945.0"
        assert bars[0].close == Decimal("27945.0")
        assert bars[0].volume == 10
        assert isinstance(bars[0].volume, int)
        assert bars[0].open_interest is None
        assert bars[1].open_interest == 5
        assert bars[2].open_interest is None
        assert bars[1].timestamp == _BASE + timedelta(minutes=1)

    def test_no_source_serves_identically(self) -> None:
        bars = arrow_table_to_bars(_table(_clean_rows()), source=None)
        assert [str(b.close) for b in bars] == ["27945.0", "27955.0", "27970.0"]


# ── 4. The null gate ────────────────────────────────────────────────────────


_NULL_COLUMN_INDEX = {
    "timestamp": 0,
    "open": 1,
    "high": 2,
    "low": 3,
    "close": 4,
    "volume": 5,
}


class TestNullGate:
    """A null consulted column is ONE typed corrupt-column ValueError."""

    @pytest.mark.parametrize("column", sorted(_NULL_COLUMN_INDEX))
    def test_null_consulted_column_refuses_naming_the_column(self, column: str) -> None:
        rows = [list(r) for r in _clean_rows()]
        rows[1][_NULL_COLUMN_INDEX[column]] = None
        with pytest.raises(ValueError, match="corrupt stored bar column") as exc:
            arrow_table_to_bars(_table([tuple(r) for r in rows]), source=_SOURCE)
        message = str(exc.value)
        assert repr(column) in message
        assert "holds null value(s)" in message
        assert _SOURCE in message

    def test_null_open_interest_alone_is_legal(self) -> None:
        rows = [list(r) for r in _clean_rows()]
        rows[1][6] = None
        bars = arrow_table_to_bars(_table([tuple(r) for r in rows]), source=_SOURCE)
        assert [b.open_interest for b in bars] == [None, None, None]
        assert str(bars[1].close) == "27955.0"


# ── 5. Strict stops end-to-end ──────────────────────────────────────────────


class TestStrictStops:
    """Finiteness before positivity, both with the frozen DEC wordings."""

    def test_negative_price_raises_the_positivity_refusal(self) -> None:
        rows = [list(r) for r in _clean_rows()]
        rows[1][1] = -1.0
        with pytest.raises(ValueError, match="non-positive price") as exc:
            arrow_table_to_bars(_table([tuple(r) for r in rows]), source=_SOURCE)
        message = str(exc.value)
        assert "DEC-STORED-DATA-VALIDATION-001" in message
        assert "open=-1.0" in message
        assert _SOURCE in message
        assert _pylist_repr(_BASE + timedelta(minutes=1)) in message

    @pytest.mark.parametrize(
        "value", [float("inf"), float("nan"), float("-inf")], ids=["+inf", "nan", "-inf"]
    )
    def test_nonfinite_price_raises_the_finiteness_refusal(self, value: float) -> None:
        # -inf is ALSO <= 0 — the non-FINITE refusal winning proves the
        # kernel checks finiteness BEFORE positivity.
        rows = [list(r) for r in _clean_rows()]
        rows[1][2] = value
        with pytest.raises(ValueError, match="non-finite price") as exc:
            arrow_table_to_bars(_table([tuple(r) for r in rows]), source=_SOURCE)
        message = str(exc.value)
        assert "DEC-LAKE-READ-NONFINITE-OHLC-GUARD-001" in message
        assert f"high={value}" in message
        assert _SOURCE in message
        assert _pylist_repr(_BASE + timedelta(minutes=1)) in message


# ── 6. Warn/raise interleaving (absolute capture_logs expectations) ─────────


class TestWarnRaiseInterleaving:
    """The emit_at_row replay law reproduces the oracle's side-effect order."""

    def test_one_warn_per_spiked_session(self) -> None:
        rows: list[Row] = [
            (_BASE, 27940.0, 27950.0, 27930.0, 27945.0, 10, None),
            (_BASE + timedelta(minutes=1), 2794.5, 2795.0, 2793.0, 2794.5, 10, None),
            (_BASE + timedelta(days=1), 6000.0, 6010.0, 5990.0, 6005.0, 10, None),
            (_BASE + timedelta(days=1, minutes=1), 600.0, 601.0, 599.0, 600.5, 10, None),
        ]
        with capture_logs() as logs:
            bars = arrow_table_to_bars(_table(rows), source=_SOURCE)
        assert len(bars) == 2  # one kept bar per session, one skip each
        assert logs == [
            {
                "source": _SOURCE,
                "session": "2024-01-02",
                "skipped": 1,
                "max_ratio": 4.0,
                "first_prev_close": 27945.0,
                "first_close": 2794.5,
                "event": "feed.bad_tick_skipped",
                "log_level": "warning",
            },
            {
                "source": _SOURCE,
                "session": "2024-01-03",
                "skipped": 1,
                "max_ratio": 4.0,
                "first_prev_close": 6005.0,
                "first_close": 600.5,
                "event": "feed.bad_tick_skipped",
                "log_level": "warning",
            },
        ]

    def test_warned_session_boundary_warn_escapes_before_positivity_stop(self) -> None:
        # Session 1's owed warn anchors on session 2's first (kept) row —
        # the LAST kept row, because the next row fails positivity. The
        # in-loop drain (`<= i`) MUST emit it before the raise (the R6a
        # boundary-mutant law, now against an absolute expectation).
        rows: list[Row] = [
            (_BASE, 27940.0, 27950.0, 27930.0, 27945.0, 10, None),
            (_BASE + timedelta(minutes=1), 2794.5, 2795.0, 2793.0, 2794.5, 10, None),
            (_BASE + timedelta(days=1), 6000.0, 6010.0, 5990.0, 6005.0, 10, None),
            (_BASE + timedelta(days=1, minutes=1), -5.0, 6010.0, 5990.0, 6005.0, 10, None),
        ]
        with capture_logs() as logs, pytest.raises(ValueError, match="non-positive price"):
            arrow_table_to_bars(_table(rows), source=_SOURCE)
        assert logs == [
            {
                "source": _SOURCE,
                "session": "2024-01-02",
                "skipped": 1,
                "max_ratio": 4.0,
                "first_prev_close": 27945.0,
                "first_close": 2794.5,
                "event": "feed.bad_tick_skipped",
                "log_level": "warning",
            }
        ]

    def test_pending_same_session_warn_suppressed_by_nonfinite_stop(self) -> None:
        # The spike's warn is still PENDING (no session boundary yet) when
        # the non-finite row stops the scan — the oracle raised before its
        # flush, so NO warn is emitted.
        rows: list[Row] = [
            (_BASE, 27940.0, 27950.0, 27930.0, 27945.0, 10, None),
            (_BASE + timedelta(minutes=1), 2794.5, 2795.0, 2793.0, 2794.5, 10, None),
            (_BASE + timedelta(minutes=2), 27946.0, float("nan"), 27940.0, 27955.0, 10, None),
        ]
        with capture_logs() as logs, pytest.raises(ValueError, match="non-finite price"):
            arrow_table_to_bars(_table(rows), source=_SOURCE)
        assert logs == []

    def test_screen_kill_switch_keeps_spike_and_passes_empty_ts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "off")
        spy = _KernelSpy()
        monkeypatch.setattr(replay, "require_feed_kernel", lambda: spy)
        with capture_logs() as logs:
            bars = arrow_table_to_bars(_table(_spike_rows()), source=_SOURCE)
        assert len(bars) == 4  # the spike is KEPT
        assert str(bars[1].close) == "2794.5"
        assert _bad_tick_events(logs) == []
        assert logs == []
        assert len(spy.calls) == 1
        assert spy.calls[0][0] == []  # ts_us NEVER crosses with the screen off
        assert spy.calls[0][5] is False  # screen_enabled=False


# ── 7. Non-canonical tables (materialization fidelity) ──────────────────────


class TestNonCanonicalTables:
    """Decision copies for the kernel; the ORIGINAL objects materialize."""

    def test_int64_price_column_materializes_decimal_of_int(self) -> None:
        # Decimal(str(5)) == Decimal("5"), NOT "5.0" — materialization
        # reads the ORIGINAL int object; only the DECISION plane is
        # float-cast.
        rows: list[Row] = [(_BASE, 5, 27950.0, 4.0, 27945.0, 10, None)]
        (bar,) = arrow_table_to_bars(_table(rows, price_type=pa.int64()), source=_SOURCE)
        assert str(bar.open) == "5"
        assert bar.open == Decimal("5")
        assert str(bar.high) == "27950.0"

    def test_timestamp_ms_column_serves_identically_to_us(self) -> None:
        us_bars = arrow_table_to_bars(_table(_clean_rows()), source=_SOURCE)
        ms_bars = arrow_table_to_bars(
            _table(_clean_rows(), timestamp_type=pa.timestamp("ms", tz="UTC")), source=_SOURCE
        )
        assert [b.timestamp for b in ms_bars] == [b.timestamp for b in us_bars]
        assert [str(b.close) for b in ms_bars] == [str(b.close) for b in us_bars]

    def test_naive_timestamp_with_screen_on_raises_wrapped_constructor_refusal(self) -> None:
        # The naive object is treated as-if-UTC in the DECISION plane only;
        # the first kept row's CandleBar constructor rejects it with the
        # wrapped corrupt-bar ValueError before any output is visible.
        rows: list[Row] = [(datetime(2024, 1, 2, 9, 15), 100.0, 101.0, 99.0, 100.5, 10, None)]
        with pytest.raises(ValueError, match="corrupt stored bar") as exc:
            arrow_table_to_bars(_table(rows, timestamp_type=pa.timestamp("us")), source=_SOURCE)
        message = str(exc.value)
        assert "must be timezone-aware" in message
        assert "DEC-STORED-DATA-VALIDATION-001" in message

    def test_string_price_column_raises_corrupt_column(self) -> None:
        rows: list[tuple[Any, ...]] = [(_BASE, "abc", 101.0, 99.0, 100.5, 10, None)]
        with pytest.raises(ValueError, match="corrupt stored bar column") as exc:
            arrow_table_to_bars(_table(rows, price_type=pa.string()), source=_SOURCE)
        message = str(exc.value)
        assert repr("open") in message
        assert "holds a non-numeric price value" in message

    def test_string_timestamp_column_with_screen_on_raises_corrupt_column(self) -> None:
        rows: list[Row] = [("2024-01-02", 100.0, 101.0, 99.0, 100.5, 10, None)]
        with pytest.raises(ValueError, match="corrupt stored bar column") as exc:
            arrow_table_to_bars(_table(rows, timestamp_type=pa.string()), source=_SOURCE)
        message = str(exc.value)
        assert repr("timestamp") in message
        assert "holds a non-timestamp value" in message

    def test_missing_column_raises_pyarrow_keyerror(self) -> None:
        table = _table(_clean_rows()).drop_columns(["open_interest"])
        with pytest.raises(KeyError, match="open_interest"):
            arrow_table_to_bars(table, source=_SOURCE)


# ── 8. The fixed-IST envelope refusal ───────────────────────────────────────


#: One tz-aware pre-epoch row — outside the kernel's fixed-IST envelope.
_PRE_1970_ROWS: list[Row] = [
    (datetime(1960, 1, 1, 5, tzinfo=UTC), 100.0, 101.0, 99.0, 100.5, 10, None)
]


class TestEnvelopeRefusal:
    """An out-of-envelope consulted timestamp refuses; screen off bypasses."""

    def test_pre_1970_timestamp_with_screen_on_refuses(self) -> None:
        with pytest.raises(FeedError, match="fixed-IST envelope") as exc:
            arrow_table_to_bars(_table(_PRE_1970_ROWS), source=_SOURCE)
        assert _SOURCE in str(exc.value)
        assert "DEC-FEED-RUST-MATERIALIZE-002" in str(exc.value)

    def test_pre_1970_timestamp_with_screen_off_serves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "off")
        (bar,) = arrow_table_to_bars(_table(_PRE_1970_ROWS), source=_SOURCE)
        assert str(bar.close) == "100.5"
        assert bar.timestamp == datetime(1960, 1, 1, 5, tzinfo=UTC)


# ── 9. The refuse-poison law over the kernel result ─────────────────────────


class _StubKernel:
    """A fake wheel whose ``scan_bars_strict`` is injected per test."""

    def __init__(self, fn: Any) -> None:
        self._fn = fn

    def scan_bars_strict(self, *args: Any) -> Any:
        return self._fn(*args)


class TestPoisonedWheel:
    """A poisoned/raising kernel refuses loudly — never serves."""

    def test_structurally_invalid_result_refuses_naming_the_law(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _StubKernel(lambda *_: ([0, 0], [], None))  # duplicate kept rows
        monkeypatch.setattr(replay, "require_feed_kernel", lambda: stub)
        with pytest.raises(FeedError, match="structurally invalid") as exc:
            arrow_table_to_bars(_table(_clean_rows()), source=_SOURCE)
        assert "kept_order" in str(exc.value)
        assert "maturin develop" in str(exc.value)

    def test_kernel_raise_wraps_into_feed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_args: Any) -> Any:
            raise RuntimeError("kernel exploded")

        monkeypatch.setattr(replay, "require_feed_kernel", lambda: _StubKernel(_boom))
        with pytest.raises(FeedError, match="kernel fault is a defect") as exc:
            arrow_table_to_bars(_table(_clean_rows()), source=_SOURCE)
        assert "RuntimeError('kernel exploded')" in str(exc.value)
        assert isinstance(exc.value.__cause__, RuntimeError)

    def test_kernel_memory_error_propagates_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _oom(*_args: Any) -> Any:
            raise MemoryError

        monkeypatch.setattr(replay, "require_feed_kernel", lambda: _StubKernel(_oom))
        with pytest.raises(MemoryError):
            arrow_table_to_bars(_table(_clean_rows()), source=_SOURCE)


# ── 10. Structural pin: materialization lives ONLY in _build_bar ────────────


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

    def test_bars_from_scan_result_matches_manual_build(self) -> None:
        # Belt: the served bar VALUES equal a hand-built expectation —
        # 100.5 renders "100.5", never "100.50"; integer-valued 20000.0
        # renders "20000.0".
        rows: list[Row] = [(_BASE, 20000.0, 20000.5, 19999.5, 20000.0, 7, 3)]
        (bar,) = arrow_table_to_bars(_table(rows), source=None)
        assert str(bar.open) == "20000.0"
        assert str(bar.high) == "20000.5"
        assert str(bar.low) == "19999.5"
        assert bar.volume == 7
        assert bar.open_interest == 3
        assert bar.open == Decimal("20000.0")
