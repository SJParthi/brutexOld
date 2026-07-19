"""The §3.5 read/producer scope predicates — DEC-UNIVERSE-INDEX-ONLY-001.

The dataset-predicate matrix (incl. the ``-G1S`` undecodable-FNO arm, the
junk exchange/segment total-never-raise arms and the drifted-index-dir
recovery-ordering arm) plus the WINDOW-row predicate form shared by R11 + P9
(modern F&O contract keys, spot recovered forms, the legacy ``sid|segment``
shape via the admitted-sid map — index legacy ADMITTED; an unmapped sid reads
the DISTINCT ``legacy_unresolved``, never a silent drop).
"""

from __future__ import annotations

import pytest
from brutex.config.feed_scope import (
    FEED_SCOPE_ENV,
    resolve_feed_scope,
)
from brutex.data.scope_filter import (
    WINDOW_ADMITTED,
    WINDOW_EXCLUDED,
    WINDOW_LEGACY_UNRESOLVED,
    classify_window_row,
    scope_admits_dataset,
    scope_admits_fno_contract,
    scope_admitted_symbol_names,
    scope_excluded_echo,
    scope_note_suffix,
    window_scope_admit,
)


def _index4():
    return resolve_feed_scope("groww")


def _full(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(FEED_SCOPE_ENV, "full")
    return resolve_feed_scope("groww")


class TestScopeAdmitsDataset:
    @pytest.mark.parametrize(
        ("exchange", "segment", "symbol", "admitted"),
        [
            # Spot admitted set (INDEX segment).
            ("NSE", "INDEX", "NIFTY", True),
            ("NSE", "INDEX", "BANKNIFTY", True),
            ("BSE", "INDEX", "SENSEX", True),
            ("NSE", "INDEX", "INDIAVIX", True),
            ("NSE", "INDEX", "NIFTYIT", False),  # sectoral — excluded
            ("BSE", "INDEX", "NIFTY", False),  # pair-keyed
            # CASH → recovered then fails admits (same outcome, honest path).
            ("NSE", "CASH", "RELIANCE", False),
            ("NSE", "CASH", "BAJAJ-AUTO", False),
            # Modern F&O contract dirs.
            ("NSE", "FNO", "NSE-NIFTY-30Sep25-FUT", True),
            ("NSE", "FNO", "NSE-BANKNIFTY-30Sep25-24000-CE", True),
            ("BSE", "FNO", "BSE-SENSEX-25Sep25-79500-PE", True),
            ("NSE", "FNO", "NSE-RELIANCE-30Sep25-FUT", False),
            ("NSE", "FNO", "NSE-BAJAJ-AUTO-30Sep25-1000-CE", False),
        ],
    )
    def test_bounded_matrix(self, exchange: str, segment: str, symbol: str, admitted: bool) -> None:
        assert scope_admits_dataset(exchange, segment, symbol, _index4()) is admitted

    def test_drifted_index_dir_recovered_then_admitted(self) -> None:
        """The Rev-2 ordering law: scope judgement AFTER drift recovery — a

        legacy ``NSE-NIFTY`` / trailing-space INDEX dir is
        recovered-then-admitted, never silently dropped (the
        DEC-COMPLETENESS-SPOT-KEY-RECOVER-001 lake shape)."""
        scope = _index4()
        assert scope_admits_dataset("NSE", "INDEX", "NSE-NIFTY", scope) is True
        assert scope_admits_dataset("NSE", "INDEX", "NIFTY ", scope) is True
        assert scope_admits_dataset("BSE", "INDEX", "BSE-SENSEX", scope) is True
        # A drifted CASH dir recovers then FAILS admits — honest path.
        assert scope_admits_dataset("NSE", "CASH", "NSE-RELIANCE", scope) is False

    @pytest.mark.parametrize(
        ("exchange", "segment", "symbol"),
        [
            ("MCX", "INDEX", "NIFTY"),  # junk exchange token
            ("", "INDEX", "NIFTY"),  # empty exchange
            ("NSE", "SNAP1S", "NIFTY"),  # unknown segment
            ("NSE", "FNO", "NSE-NIFTY-G1S"),  # undecodable derived symbol (-G1S)
            ("NSE", "FNO", "junk"),  # undecodable FNO dir
            ("G1S", "FNO", "G1S-NIFTY-30Sep25-FUT"),  # junk parsed exchange token
        ],
    )
    def test_hostile_coordinates_total_never_raise(
        self, exchange: str, segment: str, symbol: str
    ) -> None:
        """The §3.5 totality law: junk coordinates → False under bounded —

        NEVER a raise (the #1594 planted-tree DoS class)."""
        assert scope_admits_dataset(exchange, segment, symbol, _index4()) is False

    def test_unbounded_scope_pass_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        full = _full(monkeypatch)
        assert scope_admits_dataset("NSE", "CASH", "RELIANCE", full) is True
        assert scope_admits_dataset("MCX", "SNAP1S", "junk", full) is True
        assert scope_admits_dataset("NSE", "FNO", "NSE-NIFTY-G1S", full) is True


_SIDS = {"1": "NIFTY", "25": "BANKNIFTY", "99": "RELIANCE"}


class TestClassifyWindowRow:
    """The R11/P9 window form (spot keys carry NO exchange coordinate)."""

    @pytest.mark.parametrize("symbol", ["NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX"])
    def test_spot_admitted(self, symbol: str) -> None:
        assert classify_window_row(symbol, _index4()) == WINDOW_ADMITTED

    @pytest.mark.parametrize("symbol", ["RELIANCE", "BAJAJ-AUTO", "NIFTYIT"])
    def test_spot_excluded(self, symbol: str) -> None:
        assert classify_window_row(symbol, _index4()) == WINDOW_EXCLUDED

    def test_bare_exchange_prefix_excluded_never_raises(self) -> None:
        """A degenerate key that IS exactly an exchange prefix (``"NSE-"``)

        strips to an EMPTY candidate — excluded, never a raise (totality)."""
        assert classify_window_row("NSE-", _index4()) == WINDOW_EXCLUDED
        assert classify_window_row("BSE-", _index4()) == WINDOW_EXCLUDED

    @pytest.mark.parametrize("symbol", ["NSE-NIFTY", "NIFTY ", "BSE-SENSEX"])
    def test_spot_recovered_forms_admitted(self, symbol: str) -> None:
        assert classify_window_row(symbol, _index4()) == WINDOW_ADMITTED

    def test_modern_fno_contract_keys(self) -> None:
        scope = _index4()
        assert classify_window_row("NSE-NIFTY-30Sep25-FUT", scope) == WINDOW_ADMITTED
        assert classify_window_row("BSE-SENSEX-25Sep25-79500-CE", scope) == WINDOW_ADMITTED
        assert classify_window_row("NSE-RELIANCE-30Sep25-FUT", scope) == WINDOW_EXCLUDED
        assert classify_window_row("NSE-BAJAJ-AUTO-30Sep25-1000-PE", scope) == WINDOW_EXCLUDED

    def test_legacy_sid_shape_admitted_via_map(self) -> None:
        """An INDEX legacy `25|NSE_FNO` window is KEPT via the admitted-sid map

        (parse_fno_symbol returns None for the folded shape — a parse-keyed
        predicate would have excluded IN-SCOPE index legacy windows)."""
        scope = _index4()
        assert classify_window_row("25|NSE_FNO", scope, admitted_sids=_SIDS) == WINDOW_ADMITTED

    def test_legacy_stock_sid_excluded(self) -> None:
        """A mapped STOCK sid is judged against the scope — excluded, never

        reopened (the map is instrument-derived, the scope stays the law)."""
        scope = _index4()
        assert classify_window_row("99|NSE_FNO", scope, admitted_sids=_SIDS) == WINDOW_EXCLUDED

    def test_legacy_unmapped_sid_distinct_verdict(self) -> None:
        """An UNMAPPED sid reads the DISTINCT legacy_unresolved — counted,

        never silently dropped, never misattributed purely to the DEC."""
        scope = _index4()
        assert (
            classify_window_row("777|NSE_FNO", scope, admitted_sids=_SIDS)
            == WINDOW_LEGACY_UNRESOLVED
        )
        assert (
            classify_window_row("777|NSE_FNO", scope, admitted_sids=None)
            == WINDOW_LEGACY_UNRESOLVED
        )

    def test_unbounded_admits_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        full = _full(monkeypatch)
        for security_id in ("RELIANCE", "99|NSE_FNO", "NSE-RELIANCE-30Sep25-FUT"):
            assert classify_window_row(security_id, full) == WINDOW_ADMITTED


class TestWindowScopeAdmit:
    def test_unbounded_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """None under full — the legacy unfiltered audit, zero overhead."""
        assert window_scope_admit(_full(monkeypatch)) is None

    def test_bounded_predicate_over_window_keys(self) -> None:
        admit = window_scope_admit(_index4(), admitted_sids=_SIDS)
        assert admit is not None
        assert admit(("groww", "NIFTY", "1minute", "2024-01-01", "2024-01-31")) is True
        assert admit(("groww", "RELIANCE", "1minute", "2024-01-01", "2024-01-31")) is False
        assert admit(("groww-future", "NSE-NIFTY-30Sep25-FUT", "1minute", "a", "b")) is True
        assert admit(("groww-option", "25|NSE_FNO", "OPT|WEEK|1|ATM+3|CALL", "a", "b")) is True
        # legacy_unresolved → NOT admitted (skipped-not-reopened).
        assert admit(("groww-option", "777|NSE_FNO", "OPT|WEEK|1|ATM+3|CALL", "a", "b")) is False


# ── stage-2a additions: the R1 note/echo helpers + the R4 contract form ───


class TestScopeNoteSuffix:
    def test_zero_and_negative_are_empty(self) -> None:
        assert scope_note_suffix(0) == ""
        assert scope_note_suffix(-3) == ""

    def test_wording_names_count_and_dec(self) -> None:
        # The R1 wording law — the exact design string, thousands-grouped.
        assert scope_note_suffix(1) == (
            " 1 stored-only symbols excluded by scope (DEC-UNIVERSE-INDEX-ONLY-001)."
        )
        assert scope_note_suffix(1234) == (
            " 1,234 stored-only symbols excluded by scope (DEC-UNIVERSE-INDEX-ONLY-001)."
        )


class TestScopeExcludedEcho:
    def test_emit_on_nonzero(self) -> None:
        assert scope_excluded_echo(0) == {}
        assert scope_excluded_echo(-1) == {}
        assert scope_excluded_echo(7) == {"scope_excluded": 7}


class TestScopeAdmitsFnoContract:
    def test_admitted_pair_and_stock_pair(self) -> None:
        scope = _index4()
        assert scope_admits_fno_contract("NSE", "NIFTY", scope) is True
        assert scope_admits_fno_contract("BSE", "SENSEX", scope) is True
        assert scope_admits_fno_contract("NSE", "RELIANCE", scope) is False
        # Pair-exact: BSE derivative legs of an NSE-scoped index are OUT.
        assert scope_admits_fno_contract("BSE", "NIFTY", scope) is False

    def test_junk_exchange_excluded_under_bounded_scope(self) -> None:
        # The §3.5 totality law — never a raise, excluded + counted by callers.
        assert scope_admits_fno_contract("MCX", "NIFTY", _index4()) is False
        assert scope_admits_fno_contract("", "NIFTY", _index4()) is False

    def test_unbounded_scope_is_pass_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wide = _full(monkeypatch)
        assert scope_admits_fno_contract("MCX", "ANYTHING", wide) is True
        assert scope_admits_fno_contract("NSE", "RELIANCE", wide) is True


class TestScopeAdmittedSymbolNames:
    """R11's SQL-bind name set (stage-3a gate completion — module-gate 100%)."""

    def test_bounded_reads_the_four_exchange_folded_names(self) -> None:
        assert scope_admitted_symbol_names(_index4()) == frozenset(
            {"NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX"}
        )

    def test_unbounded_reads_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty set under full — callers gate on ``scope.bounded()`` first."""
        assert scope_admitted_symbol_names(_full(monkeypatch)) == frozenset()
