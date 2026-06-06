"""Tests for ``brutex.adapters.dhan.ntm`` (DEC-NIFTY-TOTAL-MARKET-DISCOVER-001).

NTM is admitted as a REFERENCE_ONLY index — its Dhan ``SECURITY_ID`` + symbol are
VERIFIED from Dhan's own web chart (web.dhan.co/Charts?exch=IDX&seg=I&secid=46,
2026-06-06) and cross-checked against the live master at runtime. These cover:
the verified constants, ``resolve_ntm_index`` (success + id-mismatch fail-loud),
and ``prepare_ntm_reference_plan`` end-to-end — including against the REAL 755-row
constituent CSV fixture — plus the fail-loud coverage rule. 100% line+branch.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from brutex.adapters.dhan.instruments import IndexDiscoveryError
from brutex.adapters.dhan.ntm import (
    NTM_SECURITY_ID,
    NTM_SYMBOL,
    NtmReferencePlan,
    prepare_ntm_reference_plan,
    resolve_ntm_index,
)
from brutex.dashboard.nse_constituents import parse_constituents
from brutex.models import Exchange
from brutex.universe_constituents import ConstituentCoverageError

#: The real NIFTY Total Market constituent list (niftyindices.com), saved as a
#: fixture — the operator's uploaded ``ind_niftytotalmarket_list.csv``.
_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "ntm" / "ind_niftytotalmarket_list.csv"
)


def _index_row(*, symbol: str = NTM_SYMBOL, security_id: str = "46") -> dict[str, str]:
    """A minimal NTM ``INDEX`` master row."""
    return {
        "EXCH_ID": "NSE",
        "SEGMENT": "I",
        "SECURITY_ID": security_id,
        "ISIN": "",
        "INSTRUMENT": "INDEX",
        "UNDERLYING_SYMBOL": "",
        "SYMBOL_NAME": symbol,
        "LOT_SIZE": "0",
        "TICK_SIZE": "0",
        "SM_FREEZE_QTY": "0",
        "OPTION_TYPE": "",
        "EXPIRY_FLAG": "",
    }


def _equity_row(*, isin: str, security_id: str, name: str) -> dict[str, str]:
    """A minimal NSE cash-equity master row carrying a constituent ISIN."""
    return {
        "EXCH_ID": "NSE",
        "SEGMENT": "E",
        "SECURITY_ID": security_id,
        "ISIN": isin,
        "INSTRUMENT": "EQUITY",
        "UNDERLYING_SYMBOL": "",
        "SYMBOL_NAME": name,
        "LOT_SIZE": "1",
        "TICK_SIZE": "0.05",
        "SM_FREEZE_QTY": "0",
        "OPTION_TYPE": "",
        "EXPIRY_FLAG": "",
    }


def test_verified_constants_match_dhan_web() -> None:
    # Pinned to the operator-verified Dhan web chart (2026-06-06). If Dhan ever
    # re-issues the id this test is the canary that forces a re-verification.
    assert NTM_SECURITY_ID == 46
    assert NTM_SYMBOL == "NIFTY TOTAL MKT"


class TestResolveNtmIndex:
    def test_finds_and_verifies_id(self) -> None:
        found = resolve_ntm_index([_index_row()])
        assert found.security_id == 46
        assert found.symbol_name == NTM_SYMBOL
        assert found.exchange is Exchange.NSE

    def test_id_mismatch_fails_loud(self) -> None:
        # The live master names an NTM index but with a DIFFERENT id than the
        # operator-verified Dhan web id → fail loud (don't trust the pull).
        with pytest.raises(IndexDiscoveryError, match=r"disagrees with Dhan web"):
            resolve_ntm_index([_index_row(security_id="999")])

    def test_skip_cross_check_when_expected_none(self) -> None:
        # expected_security_id=None → pure discovery, no id assertion.
        found = resolve_ntm_index([_index_row(security_id="999")], expected_security_id=None)
        assert found.security_id == 999

    def test_missing_index_propagates_discovery_error(self) -> None:
        with pytest.raises(IndexDiscoveryError, match=r"no NSE INDEX row matched"):
            resolve_ntm_index([_equity_row(isin="INE002A01018", security_id="2", name="RELIANCE")])

    def test_pattern_matches_market_spelling(self) -> None:
        # The master could spell it "NIFTY TOTAL MARKET"; the default pattern still
        # matches, and the id cross-check still holds.
        found = resolve_ntm_index([_index_row(symbol="NIFTY TOTAL MARKET")])
        assert found.security_id == 46


class TestPrepareNtmReferencePlan:
    def _master(self) -> list[dict[str, str]]:
        return [
            _index_row(),
            _equity_row(isin="INE466L01038", security_id="1001", name="360 ONE WAM LTD"),
            _equity_row(isin="INE470A01017", security_id="1002", name="3M INDIA LTD"),
        ]

    def _csv(self) -> str:
        return (
            "Company Name,Industry,Symbol,Series,ISIN Code\n"
            "360 ONE WAM Ltd.,Financial Services,360ONE,EQ,INE466L01038\n"
            "3M India Ltd.,Diversified,3MINDIA,EQ,INE470A01017\n"
        )

    def test_assembles_plan(self) -> None:
        plan = prepare_ntm_reference_plan(self._master(), self._csv())
        assert isinstance(plan, NtmReferencePlan)
        assert plan.index.security_id == 46
        assert plan.extra_index_symbols == frozenset({"NIFTY TOTAL MKT"})
        # reference set = the index's own id ∪ the matched constituent ids …
        assert plan.reference_security_ids == frozenset({46, 1001, 1002})
        # … and the count excludes the index id.
        assert plan.constituent_count == 2

    def test_fail_loud_on_unmatched_constituent(self) -> None:
        # A constituent ISIN absent from the master → ConstituentCoverageError
        # (so "all N" can never silently become "N − 1").
        csv = self._csv() + "Ghost Ltd.,X,GHOST,EQ,INE000ABSENT0\n"
        with pytest.raises(ConstituentCoverageError):
            prepare_ntm_reference_plan(self._master(), csv)

    def test_partial_coverage_allowed_when_opted_in(self) -> None:
        csv = self._csv() + "Ghost Ltd.,X,GHOST,EQ,INE000ABSENT0\n"
        plan = prepare_ntm_reference_plan(self._master(), csv, require_full_coverage=False)
        # The two matched constituents (+ index id) survive; the ghost is dropped.
        assert plan.reference_security_ids == frozenset({46, 1001, 1002})
        assert plan.constituent_count == 2

    def test_index_id_mismatch_propagates(self) -> None:
        master = [_index_row(security_id="999"), *self._master()[1:]]
        with pytest.raises(IndexDiscoveryError, match=r"disagrees with Dhan web"):
            prepare_ntm_reference_plan(master, self._csv())


def test_end_to_end_against_real_constituent_csv() -> None:
    """Full coverage against the REAL 755-row NIFTY Total Market CSV.

    Synthesises an NSE-equity master row per constituent ISIN (the shape the live
    Dhan master has), then proves ``prepare_ntm_reference_plan`` resolves ALL 755
    by ISIN with zero unmatched — the 100%-coverage fail-loud rule is satisfied,
    not bypassed.
    """
    csv_text = _FIXTURE.read_text(encoding="utf-8-sig")
    constituents = parse_constituents(csv_text)
    assert len(constituents) == 755  # the real list size (verified 2026-06-06)

    rows: list[Mapping[str, str]] = [_index_row()]
    for offset, con in enumerate(constituents):
        rows.append(
            _equity_row(isin=con.isin, security_id=str(2000 + offset), name=f"{con.symbol} LTD")
        )

    plan = prepare_ntm_reference_plan(list(rows), csv_text)
    assert plan.constituent_count == 755
    # index id + 755 distinct constituent ids
    assert len(plan.reference_security_ids) == 756
    assert 46 in plan.reference_security_ids
