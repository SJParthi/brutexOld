"""Frozen golden corpus for the fno_symbol Rust seam (3-arm parity).

DEC-DATA-RUST-PURELAW-001 (MIGRATION.md #1940). Every case runs THREE arms —
the oracle (``_parse_fno_symbol_python``), the facade flag-OFF (byte-identical
by contract), and the facade flag-ON in the post-burn-in steady state
(``reset(remaining=0)``, REAL wheel — skipped with a visible reason when the
``brutex_data`` wheel is absent; teeth in the data-rust-parity lane, the
``test_feed_rust`` parity precedent) — all equal under the STRICT compare
(dataclass equality + ``str(strike)``), with the expected values FROZEN
inline. The corpus transcribes the probed CPython ``strptime`` truth table
(design §2.4) end-to-end, including the accept/reject BOUNDARY TWINS (the
``' [1-9]'`` space-day arm's reject twin — a naive ``' \\d\\d'`` Rust mirror
would wrongly accept).

The localized-month cases (#18) are pinned under the DEFAULT (C/POSIX) test
locale — the non-C-locale arms are owned by the LOCALE-GUARD battery in
``tests/unit/test_fno_symbol_rust.py``, not this corpus.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from brutex.data import fno_symbol_rust as fsr
from brutex.data.fno_symbol import (
    FnoSymbolParts,
    _parse_fno_symbol_python,
    parse_fno_symbol,
)
from structlog.testing import capture_logs

_WHEEL_PRESENT = importlib.util.find_spec("brutex_data") is not None


def _fut(exchange: str, underlying: str, expiry: date) -> FnoSymbolParts:
    return FnoSymbolParts(
        exchange=exchange,
        underlying=underlying,
        expiry=expiry,
        instrument_type="FUT",
        strike=None,
        option_type=None,
    )


def _opt(exchange: str, underlying: str, expiry: date, strike: str, side: str) -> FnoSymbolParts:
    return FnoSymbolParts(
        exchange=exchange,
        underlying=underlying,
        expiry=expiry,
        instrument_type=side,
        strike=Decimal(strike),
        option_type=side,
    )


#: The frozen corpus: (symbol, expected parts-or-None, expected str(strike)
#: or None). ``str(strike)`` is pinned SEPARATELY because dataclass __eq__
#: compares Decimal numerically (24000.5 == 24000.50) — representation is
#: part of the frozen contract (the burn-in strict-compare law).
CORPUS: tuple[tuple[str, FnoSymbolParts | None, str | None], ...] = (
    # 1. well-formed futures
    ("NSE-NIFTY-30Sep25-FUT", _fut("NSE", "NIFTY", date(2025, 9, 30)), None),
    ("BSE-SENSEX-25Sep25-FUT", _fut("BSE", "SENSEX", date(2025, 9, 25)), None),
    # 2. well-formed options, integer strikes
    ("NSE-NIFTY-30Sep25-24650-CE", _opt("NSE", "NIFTY", date(2025, 9, 30), "24650", "CE"), "24650"),
    (
        "BSE-SENSEX-25Sep25-79500-PE",
        _opt("BSE", "SENSEX", date(2025, 9, 25), "79500", "PE"),
        "79500",
    ),
    # 3. fractional + representation strikes (Decimal semantics stay Python)
    (
        "NSE-NIFTY-30Sep25-24000.5-CE",
        _opt("NSE", "NIFTY", date(2025, 9, 30), "24000.5", "CE"),
        "24000.5",
    ),
    (
        "NSE-NIFTY-30Sep25-24000.50-CE",
        _opt("NSE", "NIFTY", date(2025, 9, 30), "24000.50", "CE"),
        "24000.50",
    ),
    ("NSE-NIFTY-30Sep25-1E4-CE", _opt("NSE", "NIFTY", date(2025, 9, 30), "1E4", "CE"), "1E+4"),
    (
        "NSE-NIFTY-30Sep25-24_000-CE",
        _opt("NSE", "NIFTY", date(2025, 9, 30), "24_000", "CE"),
        "24000",
    ),
    (
        "NSE-NIFTY-30Sep25-+24000-CE",
        _opt("NSE", "NIFTY", date(2025, 9, 30), "+24000", "CE"),
        "24000",
    ),
    # 4. hyphenated underlying (right-anchored — DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001)
    ("NSE-BAJAJ-AUTO-30Sep25-FUT", _fut("NSE", "BAJAJ-AUTO", date(2025, 9, 30)), None),
    (
        "NSE-BAJAJ-AUTO-30Sep25-6000-CE",
        _opt("NSE", "BAJAJ-AUTO", date(2025, 9, 30), "6000", "CE"),
        "6000",
    ),
    # 5. &-names
    ("NSE-M&M-30Sep25-3000-CE", _opt("NSE", "M&M", date(2025, 9, 30), "3000", "CE"), "3000"),
    ("NSE-M&MFIN-30Sep25-FUT", _fut("NSE", "M&MFIN", date(2025, 9, 30)), None),
    # 6. -G1S provenance suffix (#1781) → None
    ("NSE-NIFTY-30Sep25-24650-CE-G1S", None, None),
    ("NSE-NIFTY-30Sep25-FUT-G1S", None, None),
    # 7. the NSE-NIFTY-FUT series alias + short shapes → None
    ("NSE-NIFTY-FUT", None, None),
    ("NSE-30Sep25-FUT", None, None),
    ("NSE-NIFTY-30Sep25-CE", None, None),
    # 8. junk expiry tokens → None
    ("NSE-NIFTY-30Jun75-FUT", None, None),  # pivot 1975 → window reject
    ("NSE-NIFTY-31Sep25-FUT", None, None),  # calendar
    ("NSE-NIFTY-30Sept25-FUT", None, None),  # 3-letter table only
    ("NSE-NIFTY-30Sep2025-FUT", None, None),  # %y exactly 2 digits
    ("NSE-NIFTY-00Sep25-FUT", None, None),
    ("NSE-NIFTY-030Sep25-FUT", None, None),
    ("NSE-NIFTY-30Sep25 -FUT", None, None),  # trailing space in expiry token
    # 9. expiry acceptance edges — accept/reject boundary TWINS
    ("NSE-NIFTY-3Sep25-FUT", _fut("NSE", "NIFTY", date(2025, 9, 3)), None),
    ("NSE-NIFTY-03Sep25-FUT", _fut("NSE", "NIFTY", date(2025, 9, 3)), None),
    ("NSE-NIFTY- 3Sep25-FUT", _fut("NSE", "NIFTY", date(2025, 9, 3)), None),  # the ' [1-9]' arm
    ("NSE-NIFTY- 30Sep25-FUT", None, None),  # the KILLING twin (single-digit only)
    ("NSE-NIFTY-29Feb24-FUT", _fut("NSE", "NIFTY", date(2024, 2, 29)), None),  # leap
    ("NSE-NIFTY-29Feb25-FUT", None, None),  # non-leap twin
    ("NSE-NIFTY-01Jan19-FUT", _fut("NSE", "NIFTY", date(2019, 1, 1)), None),  # window floor
    ("NSE-NIFTY-31Dec18-FUT", None, None),  # floor twin (2018)
    ("NSE-NIFTY-31Dec59-FUT", _fut("NSE", "NIFTY", date(2059, 12, 31)), None),  # window cap
    ("NSE-NIFTY-01Jan60-FUT", None, None),  # cap twin (pivot 2060)
    # the %y 68/69 pivot boundary: NO in-window accept twin exists — 68 →
    # 2068 and 69 → 1969 are BOTH outside [2019, 2059]; pinned as rejects
    # (the cargo table pins the underlying pivot years).
    ("NSE-NIFTY-30Sep68-FUT", None, None),
    ("NSE-NIFTY-30Sep69-FUT", None, None),
    ("NSE-NIFTY-30Sep99-FUT", None, None),  # 1999
    # 10. Unicode digits — the ASCII gate routes to the oracle; parity holds
    ("NSE-NIFTY-30Sep٢٥-FUT", _fut("NSE", "NIFTY", date(2025, 9, 30)), None),  # oracle ACCEPTS
    ("NSE-NIFTY-٣٠Sep25-FUT", None, None),
    ("NSE-NIFTY-1٢Sep25-FUT", _fut("NSE", "NIFTY", date(2025, 9, 12)), None),  # day 12
    # 11. case variants
    ("NSE-NIFTY-30SEP25-FUT", _fut("NSE", "NIFTY", date(2025, 9, 30)), None),
    ("NSE-NIFTY-30sep25-FUT", _fut("NSE", "NIFTY", date(2025, 9, 30)), None),
    ("NSE-NIFTY-30sEp25-FUT", _fut("NSE", "NIFTY", date(2025, 9, 30)), None),
    ("NSE-NIFTY-30Sep25-fut", None, None),  # type token case-SENSITIVE
    ("NSE-NIFTY-30Sep25-24650-ce", None, None),
    ("nse-NIFTY-30Sep25-FUT", _fut("nse", "NIFTY", date(2025, 9, 30)), None),  # oracle truth
    # 12. empty / degenerate
    ("", None, None),
    ("-", None, None),
    ("FUT", None, None),
    ("NSE--30Sep25-FUT", None, None),  # empty underlying
    ("-NIFTY-30Sep25-FUT", None, None),  # empty exchange
    # 13. whitespace-wrapped symbol
    ("NSE-NIFTY-30Sep25-FUT ", None, None),  # type token mismatch
    (" NSE-NIFTY-30Sep25-FUT", _fut(" NSE", "NIFTY", date(2025, 9, 30)), None),  # oracle truth
    # 14. strike rejects → None (the SHARED parse_strike_token law)
    ("NSE-NIFTY-30Sep25-0-CE", None, None),
    ("NSE-NIFTY-30Sep25-inf-CE", None, None),
    ("NSE-NIFTY-30Sep25-nan-CE", None, None),
    ("NSE-NIFTY-30Sep25-abc-CE", None, None),
    ("NSE-NIFTY-30Sep25-24,000-CE", None, None),
    ("NSE-NIFTY-30Sep25--CE", None, None),  # EMPTY strike token (structural)
    ("NSE-NIFTY-30Sep25--5-CE", None, None),  # "-5" splits — expiry slot "5" junk
    # 15. strike whitespace tolerance (Decimal strips)
    (
        "NSE-NIFTY-30Sep25- 24000-CE",
        _opt("NSE", "NIFTY", date(2025, 9, 30), "24000", "CE"),
        "24000",
    ),
    # 17. double-type token (expiry slot holds "FUT")
    ("NSE-NIFTY-30Sep25-FUT-FUT", None, None),
    # 18. localized-month symbols under the DEFAULT (C/POSIX) test locale
    ("NSE-NIFTY-30ene25-FUT", None, None),
    ("NSE-NIFTY-30Okt25-FUT", None, None),
)

#: Case 16 (long strings) — built programmatically, same 3-arm law.
LONG_ACCEPT = "NSE-" + "A" * 1_000 + "-30Sep25-FUT"
LONG_REJECT = "x" * 10_000

_IDS = [repr(sym) for sym, _, _ in CORPUS]


def _check(symbol: str, expected: FnoSymbolParts | None, strike_repr: str | None) -> None:
    """Assert one arm's result against the frozen expectation."""
    result = parse_fno_symbol(symbol)
    assert result == expected
    if expected is not None:
        assert result is not None
        assert str(result.strike) == str(expected.strike)
        if strike_repr is not None:
            assert str(result.strike) == strike_repr


class TestOracleArm:
    """Arm 1: the extracted verbatim Python law against the frozen values."""

    @pytest.mark.parametrize(("symbol", "expected", "strike_repr"), CORPUS, ids=_IDS)
    def test_oracle_matches_frozen(
        self, symbol: str, expected: FnoSymbolParts | None, strike_repr: str | None
    ) -> None:
        result = _parse_fno_symbol_python(symbol)
        assert result == expected
        if expected is not None:
            assert result is not None
            assert str(result.strike) == str(expected.strike)
            if strike_repr is not None:
                assert str(result.strike) == strike_repr

    def test_long_symbols(self) -> None:
        parts = _parse_fno_symbol_python(LONG_ACCEPT)
        assert parts is not None and len(parts.underlying) == 1_000
        assert _parse_fno_symbol_python(LONG_REJECT) is None


class TestFlagOffArm:
    """Arm 2: the routed public entry, flag unset — byte-identical oracle."""

    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        # Explicit "0" (kill switch) — the wave default is ON, so an unset OFF
        # arm would silently route the "oracle" class through Rust.
        monkeypatch.setenv(fsr.DATA_RUST_ENV, "0")
        fsr.reset_fno_symbol_rust_for_tests()
        yield
        fsr.reset_fno_symbol_rust_for_tests()

    @pytest.mark.parametrize(("symbol", "expected", "strike_repr"), CORPUS, ids=_IDS)
    def test_flag_off_matches_frozen(
        self, symbol: str, expected: FnoSymbolParts | None, strike_repr: str | None
    ) -> None:
        _check(symbol, expected, strike_repr)

    def test_long_symbols(self) -> None:
        parts = parse_fno_symbol(LONG_ACCEPT)
        assert parts is not None and len(parts.underlying) == 1_000
        assert parse_fno_symbol(LONG_REJECT) is None


@pytest.mark.skipif(
    not _WHEEL_PRESENT,
    reason="brutex_data wheel not installed — the data-rust-parity lane builds it (teeth there)",
)
class TestFlagOnArm:
    """Arm 3: the routed public entry, flag ON, REAL wheel, steady state."""

    @pytest.fixture(autouse=True)
    def _engaged(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        monkeypatch.setenv(fsr.DATA_RUST_ENV, "1")
        fsr.reset_fno_symbol_rust_for_tests(remaining=0)  # post-burn-in steady state
        yield
        fsr.reset_fno_symbol_rust_for_tests()

    @pytest.mark.parametrize(("symbol", "expected", "strike_repr"), CORPUS, ids=_IDS)
    def test_flag_on_matches_frozen(
        self, symbol: str, expected: FnoSymbolParts | None, strike_repr: str | None
    ) -> None:
        _check(symbol, expected, strike_repr)
        # Anti-vacuous: no guard latched the seam OFF mid-corpus (an
        # engaged run must stay engaged — a latch would silently demote
        # every later case to the oracle arm).
        assert fsr.data_rust_disabled() is False

    def test_long_symbols(self) -> None:
        parts = parse_fno_symbol(LONG_ACCEPT)
        assert parts is not None and len(parts.underlying) == 1_000
        assert parse_fno_symbol(LONG_REJECT) is None
        assert fsr.data_rust_disabled() is False

    def test_engagement_is_genuine(self) -> None:
        # The engaged INFO names the real crate version — proof the corpus
        # above exercised Rust, not a silently-latched oracle path.
        with capture_logs() as logs:
            assert parse_fno_symbol("NSE-NIFTY-30Sep25-FUT") is not None
        engaged = [e for e in logs if e["event"] == "fno_symbol_rust.engaged"]
        assert len(engaged) == 1
        assert engaged[0]["crate_version"]

    def test_burn_in_arm_also_clean(self) -> None:
        # A full-burn-in pass over the corpus (remaining=16) must complete
        # with ZERO autoreverts — the oracle-compare belt agrees with Rust.
        fsr.reset_fno_symbol_rust_for_tests()  # remaining = BURN_IN_SAMPLES
        for symbol, _, _ in CORPUS:
            parse_fno_symbol(symbol)
        assert fsr.data_rust_disabled() is False
