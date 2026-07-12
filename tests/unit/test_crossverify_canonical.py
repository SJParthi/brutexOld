"""Unit tests — :mod:`brutex.crossverify.canonical` (the §1.3 mapping table)."""

from __future__ import annotations

from datetime import date

import pytest
from brutex.crossverify.canonical import (
    CLASS_CASH_SPOT,
    CLASS_FUT,
    CLASS_INDEX_SPOT,
    EXPORT_FUTURES_UNDERLYINGS,
    EXPORT_INDEX_COORDS,
    SEGMENT_BSE_EQ,
    SEGMENT_BSE_FNO,
    SEGMENT_IDX,
    SEGMENT_NSE_EQ,
    SEGMENT_NSE_FNO,
    canonical_identity,
    s3_key,
    symbol_key_safe,
)
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.universe import V1_INDICES


class TestIndexMapping:
    def test_allowlist_is_exactly_the_three_indices(self) -> None:
        # Mirror-pinned against V1_INDICES minus reference-only (design §1.3).
        assert (
            frozenset({("NSE", "NIFTY"), ("NSE", "BANKNIFTY"), ("BSE", "SENSEX")})
            == EXPORT_INDEX_COORDS
        )

    def test_v1_indices_cross_check(self) -> None:
        derived = {
            (spec.exchange.value, spec.underlying_symbol)
            for spec in V1_INDICES
            if not spec.reference_only
        }
        assert frozenset(derived) == EXPORT_INDEX_COORDS

    def test_nifty(self) -> None:
        identity = canonical_identity(exchange="NSE", segment="INDEX", lake_symbol="NIFTY")
        assert identity is not None
        assert identity.canonical_symbol == "NIFTY"
        assert identity.segment_token == SEGMENT_IDX == "IDX_I"
        assert identity.instrument_class == CLASS_INDEX_SPOT

    def test_sensex_is_idx_i_cross_exchange(self) -> None:
        identity = canonical_identity(exchange="BSE", segment="INDEX", lake_symbol="SENSEX")
        assert identity is not None
        assert identity.segment_token == "IDX_I"
        assert identity.exchange == "BSE"

    def test_indiavix_excluded(self) -> None:
        assert canonical_identity(exchange="NSE", segment="INDEX", lake_symbol="INDIAVIX") is None

    def test_giftnifty_excluded(self) -> None:
        assert canonical_identity(exchange="NSE", segment="INDEX", lake_symbol="GIFTNIFTY") is None

    def test_case_exact(self) -> None:
        assert canonical_identity(exchange="NSE", segment="INDEX", lake_symbol="nifty") is None


class TestCashMapping:
    def test_reliance_verbatim(self) -> None:
        identity = canonical_identity(exchange="NSE", segment="CASH", lake_symbol="RELIANCE")
        assert identity is not None
        assert identity.canonical_symbol == "RELIANCE"
        assert identity.segment_token == SEGMENT_NSE_EQ == "NSE_EQ"
        assert identity.instrument_class == CLASS_CASH_SPOT

    def test_hyphenated_symbol_never_split(self) -> None:
        identity = canonical_identity(exchange="NSE", segment="CASH", lake_symbol="BAJAJ-AUTO")
        assert identity is not None
        assert identity.canonical_symbol == "BAJAJ-AUTO"

    def test_ampersand_symbol_verbatim(self) -> None:
        identity = canonical_identity(exchange="NSE", segment="CASH", lake_symbol="M&M")
        assert identity is not None
        assert identity.canonical_symbol == "M&M"

    def test_bse_eq_structural(self) -> None:
        # STRUCTURAL only — never emitted today (stocks are NSE-only,
        # coordinator ruling 3); the token itself is pinned.
        identity = canonical_identity(exchange="BSE", segment="CASH", lake_symbol="XYZ")
        assert identity is not None
        assert identity.segment_token == SEGMENT_BSE_EQ == "BSE_EQ"

    def test_unknown_exchange_excluded(self) -> None:
        assert canonical_identity(exchange="MCX", segment="CASH", lake_symbol="GOLD") is None

    def test_blank_symbol_excluded(self) -> None:
        assert canonical_identity(exchange="NSE", segment="CASH", lake_symbol="") is None

    def test_padded_symbol_excluded(self) -> None:
        assert canonical_identity(exchange="NSE", segment="CASH", lake_symbol="M&M ") is None

    def test_key_unsafe_symbol_excluded(self) -> None:
        assert canonical_identity(exchange="NSE", segment="CASH", lake_symbol="A/B") is None


class TestSymbolAllowlist:
    """F5 (round-1 security review): symbol safety is a POSITIVE allowlist
    (uppercase + digits + ``& - .``), never a blocklist — control chars and
    lowercase junk refuse the identity (deny-by-default, never a crash)."""

    @pytest.mark.parametrize(
        "symbol", ["M&M", "BAJAJ-AUTO", "L&TFH", "3MINDIA", "NAM-INDIA", "M&MFIN", "J&KBANK"]
    )
    def test_every_legal_nse_form_accepted(self, symbol: str) -> None:
        assert symbol_key_safe(symbol) is True
        identity = canonical_identity(exchange="NSE", segment="CASH", lake_symbol=symbol)
        assert identity is not None
        assert identity.canonical_symbol == symbol

    @pytest.mark.parametrize(
        "symbol",
        [
            "\x1bESC",  # ESC control char
            "A\tB",  # TAB
            "A\x00B",  # NUL
            "A\nB",  # newline
            "..",  # pure-dot path token
            ".",  # pure-dot path token
            "reliance",  # lowercase junk
            "A B",  # whitespace
            "A/B",  # path separator
            'A"B',  # CSV quote
            "A,B",  # CSV comma
            "",  # empty
            "M&M ",  # padded
        ],
    )
    def test_junk_symbol_refused_identity_denied(self, symbol: str) -> None:
        assert symbol_key_safe(symbol) is False
        # Deny-by-default disposition: the identity is refused, never a crash.
        assert canonical_identity(exchange="NSE", segment="CASH", lake_symbol=symbol) is None


class TestFuturesMapping:
    def test_nifty_future_token(self) -> None:
        identity = canonical_identity(
            exchange="NSE", segment="FNO", lake_symbol="NSE-NIFTY-30Jul26-FUT"
        )
        assert identity is not None
        assert identity.canonical_symbol == "NIFTY-2026-07-30-FUT"
        assert identity.segment_token == SEGMENT_NSE_FNO == "NSE_FNO"
        assert identity.instrument_class == CLASS_FUT
        assert identity.lake_symbol == "NSE-NIFTY-30Jul26-FUT"

    def test_bse_sensex_future_structural(self) -> None:
        identity = canonical_identity(
            exchange="BSE", segment="FNO", lake_symbol="BSE-SENSEX-24Sep26-FUT"
        )
        assert identity is not None
        assert identity.canonical_symbol == "SENSEX-2026-09-24-FUT"
        assert identity.segment_token == SEGMENT_BSE_FNO == "BSE_FNO"

    def test_option_legs_excluded(self) -> None:
        assert (
            canonical_identity(
                exchange="NSE", segment="FNO", lake_symbol="NSE-NIFTY-30Jul26-24650-CE"
            )
            is None
        )
        assert (
            canonical_identity(
                exchange="NSE", segment="FNO", lake_symbol="NSE-NIFTY-30Jul26-24650-PE"
            )
            is None
        )

    def test_unparsed_fno_dir_excluded(self) -> None:
        assert canonical_identity(exchange="NSE", segment="FNO", lake_symbol="junk") is None

    def test_hyphenated_underlying_parses_whole_but_is_out_of_allowlist(self) -> None:
        # parse_fno_symbol keeps BAJAJ-AUTO intact (never the #1646 truncation);
        # the FUT allowlist then excludes it.
        parts = parse_fno_symbol("NSE-BAJAJ-AUTO-30Sep25-FUT")
        assert parts is not None
        assert parts.underlying == "BAJAJ-AUTO"
        assert (
            canonical_identity(
                exchange="NSE", segment="FNO", lake_symbol="NSE-BAJAJ-AUTO-30Sep25-FUT"
            )
            is None
        )

    def test_exchange_dir_mismatch_excluded(self) -> None:
        assert (
            canonical_identity(exchange="BSE", segment="FNO", lake_symbol="NSE-NIFTY-30Jul26-FUT")
            is None
        )

    def test_unmapped_fno_exchange_excluded(self) -> None:
        # parse succeeds (any leading token), but only NSE/BSE mint a token.
        assert (
            canonical_identity(exchange="MCX", segment="FNO", lake_symbol="MCX-NIFTY-30Jul26-FUT")
            is None
        )

    def test_futures_allowlist(self) -> None:
        assert frozenset({"NIFTY", "BANKNIFTY", "SENSEX"}) == EXPORT_FUTURES_UNDERLYINGS

    def test_canonical_token_is_no_lake_namespace_collision(self) -> None:
        # The pinned collision check: the canonical token is NOT parseable as
        # a lake contract symbol ("30" fails %d%b%y), and identity is the
        # PARSED (underlying, expiry) — never the token string.
        assert parse_fno_symbol("NIFTY-2026-07-30-FUT") is None


class TestUnknownSegment:
    def test_other_segment_excluded(self) -> None:
        assert canonical_identity(exchange="NSE", segment="LIVE", lake_symbol="NIFTY") is None


class TestS3Key:
    def test_key_layout(self) -> None:
        identity = canonical_identity(exchange="NSE", segment="INDEX", lake_symbol="NIFTY")
        assert identity is not None
        key = s3_key(prefix="crossverify/groww", day=date(2026, 7, 10), identity=identity)
        assert key == "crossverify/groww/2026-07-10/IDX_I/NIFTY.csv"

    def test_raw_ampersand_key(self) -> None:
        # PINNED: raw `&` in the key (M&M.csv) — no %26 minting (lake_keys pledge).
        identity = canonical_identity(exchange="NSE", segment="CASH", lake_symbol="M&M")
        assert identity is not None
        key = s3_key(prefix="crossverify/groww", day=date(2026, 7, 10), identity=identity)
        assert key == "crossverify/groww/2026-07-10/NSE_EQ/M&M.csv"
        assert "%26" not in key

    def test_futures_key(self) -> None:
        identity = canonical_identity(
            exchange="NSE", segment="FNO", lake_symbol="NSE-NIFTY-30Jul26-FUT"
        )
        assert identity is not None
        key = s3_key(prefix="p", day=date(2026, 7, 10), identity=identity)
        assert key == "p/2026-07-10/NSE_FNO/NIFTY-2026-07-30-FUT.csv"
