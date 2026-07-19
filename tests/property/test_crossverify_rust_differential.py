"""Hypothesis differential — the cross-verify Rust laws vs the Python oracles.

DEC-CROSSVERIFY-RUST-CORE. Drives the REAL ``brutex_ops`` wheel (skips loudly
when absent — the ops-rust-parity lane installs it) against the verbatim
Python oracles over hostile domains, asserting BYTES and exception
type+message parity:

* the public ``render_day_csv`` flag-ON (steady state — no burn-in cushion)
  vs the flag-OFF oracle: boundary 2dp prices, ``oi`` None/0/int, empty row
  sets, duplicate/misordered minutes, out-of-session rows — identical bytes
  or identical ``ExportContractError`` type + message;
* the raw crate assembly vs a cell-level reference: hostile cells (control
  chars / unicode / metachars) refuse in BOTH belts;
* ``canonical_identity`` + ``s3_key`` flag-ON vs flag-OFF over hostile
  symbol/segment/exchange domains (deny-by-default parity).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from brutex.calendars.nse import TradingSession, get_session_checked
from brutex.crossverify import csv_render, rust_seam
from brutex.crossverify.canonical import (
    CanonicalIdentity,
    _canonical_identity_python,
    _s3_key_python,
    canonical_identity,
    s3_key,
    symbol_key_safe,
)
from brutex.crossverify.csv_render import ExportContractError, ExportRow, render_day_csv
from hypothesis import given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:
    from collections.abc import Iterator

brutex_ops = pytest.importorskip(
    "brutex_ops",
    reason="brutex_ops wheel not installed — the differential runs in the "
    "ops-rust-parity lane (pip install ./rust/brutex-ops)",
)

_DAY = date(2026, 7, 10)
_SESSION_PROBE = get_session_checked(_DAY)
assert _SESSION_PROBE is not None
#: The explicit rebind gives the module global the NARROWED declared type —
#: pyright does not carry a module-level assert-narrow into function scopes.
_SESSION: TradingSession = _SESSION_PROBE

_SPOT_IDENTITY = CanonicalIdentity(
    exchange="NSE",
    lake_segment="CASH",
    lake_symbol="M&M",
    canonical_symbol="M&M",
    segment_token="NSE_EQ",
    instrument_class="cash_spot",
)

_FUT_IDENTITY = CanonicalIdentity(
    exchange="NSE",
    lake_segment="FNO",
    lake_symbol="NSE-NIFTY-30Jul26-FUT",
    canonical_symbol="NIFTY-2026-07-30-FUT",
    segment_token="NSE_FNO",
    instrument_class="fut",
)


@pytest.fixture(autouse=True)
def _steady_state_flag_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Flag ON + post-burn-in steady state for every differential example."""
    monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "1")
    rust_seam.reset_crossverify_rust_for_tests(remaining=0)
    yield
    rust_seam.reset_crossverify_rust_for_tests()


def _oracle_outcome(
    identity: CanonicalIdentity, rows: list[ExportRow]
) -> tuple[bytes | None, str | None]:
    try:
        return (
            csv_render._render_day_csv_python(identity=identity, rows=rows, session=_SESSION),
            None,
        )
    except ExportContractError as exc:
        return None, str(exc)


def _flag_on_outcome(
    identity: CanonicalIdentity, rows: list[ExportRow]
) -> tuple[bytes | None, str | None]:
    try:
        return render_day_csv(identity=identity, rows=rows, session=_SESSION), None
    except ExportContractError as exc:
        return None, str(exc)


#: Boundary-hostile price strategy: exact 2dp grid values, half-boundary
#: noise floats (the ``x.xx5`` ROUND_HALF_UP class), tiny + huge magnitudes.
_PRICES = st.one_of(
    st.decimals(
        min_value=Decimal("0.01"),
        max_value=Decimal("99999.99"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    st.sampled_from(
        [
            Decimal("14020.7999999999"),
            Decimal("0.005"),
            Decimal("1.005"),
            Decimal("2.675"),
            Decimal("99999.995"),
        ]
    ),
)

_MINUTES = st.lists(st.integers(min_value=0, max_value=374), min_size=0, max_size=6)


@st.composite
def _row_sets(draw: st.DrawFn) -> tuple[CanonicalIdentity, list[ExportRow]]:
    """Rows over one session day: sorted/duplicated/misordered minute mixes,
    spot (oi=None) vs futures (oi int) identities, boundary prices."""
    is_fut = draw(st.booleans())
    identity = _FUT_IDENTITY if is_fut else _SPOT_IDENTITY
    minutes = draw(_MINUTES)
    shuffle = draw(st.booleans())
    if not shuffle:
        minutes = sorted(minutes)
    open_utc = datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC)
    rows: list[ExportRow] = []
    for minute in minutes:
        price = draw(_PRICES)
        oi: int | None
        if is_fut:
            oi = draw(st.one_of(st.just(0), st.integers(min_value=0, max_value=10**9)))
        else:
            oi = None
        rows.append(
            ExportRow(
                ts_utc=open_utc + timedelta(minutes=minute),
                open=price,
                high=price + Decimal("0.30"),
                low=max(price - Decimal("0.25"), Decimal("0.01")),
                close=price + Decimal("0.10"),
                volume=draw(st.integers(min_value=0, max_value=10**9)),
                oi=oi,
            )
        )
    return identity, rows


class TestCsvDifferential:
    """Bytes AND exception type+message parity through the public entry."""

    @settings(max_examples=60, deadline=None)
    @given(payload=_row_sets())
    def test_flag_on_equals_oracle(
        self, payload: tuple[CanonicalIdentity, list[ExportRow]]
    ) -> None:
        identity, rows = payload
        rust_seam.reset_crossverify_rust_for_tests(remaining=0)
        assert _flag_on_outcome(identity, rows) == _oracle_outcome(identity, rows)

    @settings(max_examples=25, deadline=None)
    @given(minute=st.integers(min_value=375, max_value=600))
    def test_out_of_session_row_refuses_identically(self, minute: int) -> None:
        rust_seam.reset_crossverify_rust_for_tests(remaining=0)
        open_utc = datetime(_DAY.year, _DAY.month, _DAY.day, 3, 45, tzinfo=UTC)
        rows = [
            ExportRow(
                ts_utc=open_utc + timedelta(minutes=minute),
                open=Decimal("100.00"),
                high=Decimal("100.30"),
                low=Decimal("99.75"),
                close=Decimal("100.10"),
                volume=1,
                oi=None,
            )
        ]
        assert _flag_on_outcome(_SPOT_IDENTITY, rows) == _oracle_outcome(_SPOT_IDENTITY, rows)
        # The refusal is genuinely a refusal (anti-vacuous).
        assert _flag_on_outcome(_SPOT_IDENTITY, rows)[0] is None

    @settings(max_examples=40, deadline=None)
    @given(
        cell=st.text(
            alphabet=st.characters(min_codepoint=0, max_codepoint=0x2FFF),
            min_size=0,
            max_size=8,
        )
    )
    def test_hostile_cells_refuse_in_both_belts(self, cell: str) -> None:
        """The raw crate belt vs the Python cell law over hostile strings."""
        header = csv_render.CSV_HEADER
        row = [cell, "2026-07-10 09:15:00", "1.00", "1.00", "1.00", "1.00", "1", ""]
        rust_bytes, refusal = brutex_ops.crossverify_render_csv(header, [row])
        has_metachar = any(ch in ',"\n\r' for ch in cell)
        has_control = any(ord(ch) <= 0x1F or ord(ch) == 0x7F for ch in cell)
        if has_metachar or has_control:
            assert rust_bytes is None
            assert refusal is not None
            expected_code = 4 if has_metachar else 5
            assert refusal[0] == expected_code
            # The Python cell law refuses the SAME cell.
            with pytest.raises(ExportContractError):
                csv_render._validate_cells(row, ts_label=row[1])
        else:
            assert refusal is None
            assert isinstance(rust_bytes, bytes)
            # The accepted cell survives byte-verbatim in the assembly.
            assert cell.encode("utf-8") in rust_bytes


_SEGMENTS = st.sampled_from(["INDEX", "CASH", "FNO", "OPTIONS", "", "index"])
_EXCHANGES = st.sampled_from(["NSE", "BSE", "MCX", "nse", ""])
_SYMBOLS = st.one_of(
    st.sampled_from(
        [
            "NIFTY",
            "BANKNIFTY",
            "SENSEX",
            "INDIAVIX",
            "M&M",
            "BAJAJ-AUTO",
            "L&TFH",
            "3MINDIA",
            "NSE-NIFTY-30Jul26-FUT",
            "NSE-NIFTY-30Jul26-24000-CE",
            "NSE-RELIANCE-30Jul26-FUT",
            "BSEIT",
            ".",
            "..",
            "",
        ]
    ),
    st.text(
        alphabet=st.characters(min_codepoint=1, max_codepoint=0x2FF),
        min_size=0,
        max_size=12,
    ),
)


class TestCanonicalDifferential:
    """Deny-by-default parity over hostile coordinate domains."""

    @settings(max_examples=120, deadline=None)
    @given(exchange=_EXCHANGES, segment=_SEGMENTS, symbol=_SYMBOLS)
    def test_flag_on_equals_oracle(self, exchange: str, segment: str, symbol: str) -> None:
        rust_seam.reset_crossverify_rust_for_tests(remaining=0)
        flag_on = canonical_identity(exchange=exchange, segment=segment, lake_symbol=symbol)
        oracle = _canonical_identity_python(exchange=exchange, segment=segment, lake_symbol=symbol)
        assert flag_on == oracle
        # Deny-by-default in BOTH arms: a key-unsafe non-FNO symbol never maps.
        if flag_on is not None and segment in ("INDEX", "CASH"):
            assert symbol_key_safe(symbol)

    @settings(max_examples=60, deadline=None)
    @given(symbol=_SYMBOLS)
    def test_key_safe_law_parity(self, symbol: str) -> None:
        """The crate's allowlist twin agrees with the Python law (via the
        INDEX arm, where key-safety is the only symbol gate on NSE)."""
        rust_seam.reset_crossverify_rust_for_tests(remaining=0)
        mapped = canonical_identity(exchange="NSE", segment="INDEX", lake_symbol=symbol)
        assert (mapped is not None) == symbol_key_safe(symbol)


class TestS3KeyDifferential:
    @settings(max_examples=40, deadline=None)
    @given(
        prefix=st.text(
            alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E),
            min_size=1,
            max_size=16,
        ),
        day=st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31)),
    )
    def test_flag_on_equals_oracle(self, prefix: str, day: date) -> None:
        rust_seam.reset_crossverify_rust_for_tests(remaining=0)
        for identity in (_SPOT_IDENTITY, _FUT_IDENTITY):
            assert s3_key(prefix=prefix, day=day, identity=identity) == _s3_key_python(
                prefix=prefix, day=day, identity=identity
            )
