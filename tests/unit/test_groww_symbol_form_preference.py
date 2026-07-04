"""Tests for the learned ``&``-symbol form ordering (DEC-SYMBOL-FORM-PREFERENCE-001).

100% line+branch on ``brutex/adapters/groww/symbol_form_preference.py`` + the new
preference branches in ``contracts.py`` / ``candles.py`` / ``option_ingest.py``:

* the pure :class:`SymbolFormPreference` primitive (reorder / never-drop / unknown-key /
  stale-index / defensive-record / thread-safety);
* the LEARNING regressions — first call plain-empty → ``%26`` wins; the SECOND call
  issues the ``%26`` form FIRST (contracts, expiries, candles — the candle key is the
  UNDERLYING so one win transfers across the chain, proven end-to-end through the real
  ``run_groww_option_ingest`` spine);
* default ``None`` ⇒ byte-identical fixed plain-first order (regression pins);
* the error-law symmetry after a reorder: the terminal-swallow no-worse-than-before law
  follows the SPECULATIVE SECOND form, whichever form that now is.

The ``%26`` encoding itself stays vendor-UNVERIFIED (DEC-GROWW-AMPERSAND-FALLBACK-001's
honest envelope) — this feature only reorders which hypothesis is tried first.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from datetime import date

from brutex.adapters.groww.candles import fetch_candle_windows
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.contracts import (
    resolve_contracts,
    resolve_expiries,
    underlying_symbol_forms,
)
from brutex.adapters.groww.option_ingest import run_groww_option_ingest
from brutex.adapters.groww.symbol_form_preference import SymbolFormPreference
from brutex.adapters.protocol import DerivativeContract
from brutex.data.sink import NullBarSink

# ─────────────────────────────────────────────────────────────────────────
# The pure primitive
# ─────────────────────────────────────────────────────────────────────────


class TestSymbolFormPreference:
    def test_unknown_key_returns_input_unchanged(self) -> None:
        pref = SymbolFormPreference()
        forms = ("M&M", "M%26M")
        assert pref.order("M&M", forms) == forms

    def test_recorded_winner_moves_first_never_drops(self) -> None:
        pref = SymbolFormPreference()
        forms = ("M&M", "M%26M")
        pref.record_winner("M&M", forms, "M%26M")
        ordered = pref.order("M&M", forms)
        assert ordered == ("M%26M", "M&M")  # reordered — SAME members, none dropped
        assert sorted(ordered) == sorted(forms)

    def test_canonical_first_winner_keeps_input_order(self) -> None:
        pref = SymbolFormPreference()
        forms = ("M&M", "M%26M")
        pref.record_winner("M&M", forms, "M&M")  # index 0 — already first
        assert pref.order("M&M", forms) == forms

    def test_stale_out_of_range_index_abstains(self) -> None:
        pref = SymbolFormPreference()
        pref.record_winner("K", ("a", "b", "c"), "c")  # index 2
        assert pref.order("K", ("a", "b")) == ("a", "b")  # stale → unchanged, no crash

    def test_record_unknown_form_is_a_defensive_noop(self) -> None:
        pref = SymbolFormPreference()
        forms = ("M&M", "M%26M")
        pref.record_winner("M&M", forms, "SOMETHING-ELSE")
        assert pref.order("M&M", forms) == forms  # nothing poisoned

    def test_learning_transfers_across_keys_independently(self) -> None:
        pref = SymbolFormPreference()
        a = ("M&M", "M%26M")
        b = ("L&TFH", "L%26TFH")
        pref.record_winner("M&M", a, "M%26M")
        assert pref.order("M&M", a) == ("M%26M", "M&M")
        assert pref.order("L&TFH", b) == b  # other keys untouched

    def test_thread_safety_smoke(self) -> None:
        pref = SymbolFormPreference()
        forms = ("M&M", "M%26M")
        errors: list[Exception] = []

        def _worker() -> None:
            try:
                for _ in range(200):
                    pref.record_winner("M&M", forms, "M%26M")
                    out = pref.order("M&M", forms)
                    assert sorted(out) == sorted(forms)
            except Exception as exc:  # captured for the assert below (AssertionError included)
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
        assert errors == []


# ─────────────────────────────────────────────────────────────────────────
# contracts.py — resolve_expiries / resolve_contracts learning
# ─────────────────────────────────────────────────────────────────────────


class _ResolutionSDK:
    """Expiries/contracts fake: only the ``%26`` form carries data; records order."""

    def __init__(self) -> None:
        self.expiry_symbols: list[str] = []
        self.contract_symbols: list[str] = []

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("candles are not fetched by the resolution layer")

    def get_expiries(self, **kw: object) -> Mapping[str, object]:
        symbol = str(kw["underlying_symbol"])
        self.expiry_symbols.append(symbol)
        if "%26" in symbol:
            return {"expiries": ["2025-01-02"]}
        return {"expiries": []}

    def get_contracts(self, **kw: object) -> Mapping[str, object]:
        symbol = str(kw["underlying_symbol"])
        self.contract_symbols.append(symbol)
        if "%26" in symbol:
            return {"contracts": ["NSE-M&M-02Jan25-2400-CE"]}
        return {"contracts": []}


def _resolution_client(sdk: _ResolutionSDK) -> GrowwClient:
    return GrowwClient(sdk=sdk, sleep=lambda _s: None)


def test_resolve_expiries_learns_the_winning_form() -> None:
    self_key = "M&M"
    sdk = _ResolutionSDK()
    client = _resolution_client(sdk)
    pref = SymbolFormPreference()
    first = resolve_expiries(
        client, exchange="NSE", underlying_symbol=self_key, form_preference=pref
    )
    assert first == [date(2025, 1, 2)]
    assert sdk.expiry_symbols == ["M&M", "M%26M"]  # first call: canonical plain-first
    second = resolve_expiries(
        client, exchange="NSE", underlying_symbol=self_key, form_preference=pref
    )
    assert second == first
    # The SECOND call issues the LEARNED winner first — one call, zero wasted fetch.
    assert sdk.expiry_symbols[2] == "M%26M"
    assert len(sdk.expiry_symbols) == 3


def test_resolve_expiries_default_none_keeps_fixed_order() -> None:
    """Regression pin: no preference ⇒ the plain form is re-fetched first EVERY call."""
    sdk = _ResolutionSDK()
    client = _resolution_client(sdk)
    for _ in range(2):
        assert resolve_expiries(client, exchange="NSE", underlying_symbol="M&M") == [
            date(2025, 1, 2)
        ]
    assert sdk.expiry_symbols == ["M&M", "M%26M", "M&M", "M%26M"]


def test_resolve_expiries_plain_winner_records_and_keeps_order() -> None:
    """A plain-form win records index 0 — the order stays canonical (covered branch)."""

    class _PlainWinsSDK(_ResolutionSDK):
        def get_expiries(self, **kw: object) -> Mapping[str, object]:
            self.expiry_symbols.append(str(kw["underlying_symbol"]))
            return {"expiries": ["2025-01-02"]}

    sdk = _PlainWinsSDK()
    pref = SymbolFormPreference()
    for _ in range(2):
        resolve_expiries(
            client=_resolution_client(sdk),
            exchange="NSE",
            underlying_symbol="M&M",
            form_preference=pref,
        )
    assert sdk.expiry_symbols == ["M&M", "M&M"]  # plain wins → plain stays first


def test_resolve_contracts_learns_the_winning_form() -> None:
    sdk = _ResolutionSDK()
    client = _resolution_client(sdk)
    pref = SymbolFormPreference()
    expiry = date(2025, 1, 2)
    first = resolve_contracts(
        client, exchange="NSE", underlying_symbol="M&M", expiry=expiry, form_preference=pref
    )
    assert [c.symbol for c in first] == ["NSE-M&M-02Jan25-2400-CE"]
    assert sdk.contract_symbols == ["M&M", "M%26M"]
    second = resolve_contracts(
        client, exchange="NSE", underlying_symbol="M&M", expiry=expiry, form_preference=pref
    )
    assert [c.symbol for c in second] == ["NSE-M&M-02Jan25-2400-CE"]
    assert sdk.contract_symbols[2] == "M%26M"  # learned winner first
    assert len(sdk.contract_symbols) == 3


def test_resolve_contracts_default_none_keeps_fixed_order() -> None:
    sdk = _ResolutionSDK()
    client = _resolution_client(sdk)
    for _ in range(2):
        resolve_contracts(client, exchange="NSE", underlying_symbol="M&M", expiry=date(2025, 1, 2))
    assert sdk.contract_symbols == ["M&M", "M%26M", "M&M", "M%26M"]


# ─────────────────────────────────────────────────────────────────────────
# candles.py — learning keyed by the UNDERLYING (transfers across contracts)
# ─────────────────────────────────────────────────────────────────────────

_CANDLE_PAYLOAD: Mapping[str, object] = {
    "candles": [["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 777, 42]]
}


class _CandleFormSDK:
    """Candle fake: only ``%26``-encoded symbols carry data; records issue order."""

    def __init__(self, *, encoded_has_data: bool = True) -> None:
        self.symbols: list[str] = []
        self._encoded_has_data = encoded_has_data

    def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
        symbol = str(kw["groww_symbol"])
        self.symbols.append(symbol)
        if self._encoded_has_data and "%26" in symbol:
            return dict(_CANDLE_PAYLOAD)
        return {"candles": []}

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        raise AssertionError("unused")


def _fetch_contract_candles(
    client: GrowwClient,
    symbol: str,
    *,
    pref: SymbolFormPreference | None,
    key: str | None,
) -> Sequence[object]:
    return fetch_candle_windows(
        client,
        exchange="NSE",
        segment="FNO",
        groww_symbol=symbol,
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        drop_volume=False,
        form_preference=pref,
        preference_key=key,
    )


def test_candles_learning_transfers_across_contracts_via_underlying_key() -> None:
    """Contract 1's ``%26`` win reorders contract 2's fetch — keyed by the underlying."""
    sdk = _CandleFormSDK()
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None)
    pref = SymbolFormPreference()
    c1 = "NSE-M&M-02Jan25-2400-CE"
    c2 = "NSE-M&M-02Jan25-2500-PE"
    bars1 = _fetch_contract_candles(client, c1, pref=pref, key="M&M")
    assert len(bars1) == 1
    assert sdk.symbols == [c1, "NSE-M%26M-02Jan25-2400-CE"]  # plain first, then encoded
    bars2 = _fetch_contract_candles(client, c2, pref=pref, key="M&M")
    assert len(bars2) == 1
    # Contract 2 issues ITS OWN encoded form FIRST — one fetch, zero wasted plain call.
    assert sdk.symbols[2] == "NSE-M%26M-02Jan25-2500-PE"
    assert len(sdk.symbols) == 3


def test_candles_preference_key_defaults_to_the_groww_symbol() -> None:
    """No explicit key ⇒ the contract's own symbol keys the memory (self-learning)."""
    sdk = _CandleFormSDK()
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None)
    pref = SymbolFormPreference()
    c1 = "NSE-M&M-02Jan25-2400-CE"
    _fetch_contract_candles(client, c1, pref=pref, key=None)
    _fetch_contract_candles(client, c1, pref=pref, key=None)
    # First call tried both; the repeat call issued the learned encoded form first.
    assert sdk.symbols == [c1, "NSE-M%26M-02Jan25-2400-CE", "NSE-M%26M-02Jan25-2400-CE"]


def test_candles_first_form_win_records_under_preference(caplog: object) -> None:
    """A FIRST-form (plain) win with a preference wired records index 0 (branch pin)."""

    class _PlainWinsSDK(_CandleFormSDK):
        def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
            self.symbols.append(str(kw["groww_symbol"]))
            return dict(_CANDLE_PAYLOAD)

    sdk = _PlainWinsSDK()
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None)
    pref = SymbolFormPreference()
    c1 = "NSE-M&M-02Jan25-2400-CE"
    for _ in range(2):
        assert len(_fetch_contract_candles(client, c1, pref=pref, key="M&M")) == 1
    assert sdk.symbols == [c1, c1]  # plain won → plain stays first, no extra call


def test_candles_both_forms_empty_records_nothing() -> None:
    """Both forms clean-empty with a preference wired: the memory stays untouched."""
    sdk = _CandleFormSDK(encoded_has_data=False)
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None)
    pref = SymbolFormPreference()
    c1 = "NSE-M&M-02Jan25-2400-CE"
    assert _fetch_contract_candles(client, c1, pref=pref, key="M&M") == []
    assert pref.order("M&M", underlying_symbol_forms("M&M")) == underlying_symbol_forms("M&M")


def test_candles_terminal_swallow_follows_the_speculative_second_form() -> None:
    """After a reorder the terminal-swallow law tracks the SECOND (speculative) form.

    Winner = encoded (learned). On the next call the encoded form returns clean-empty
    and the PLAIN form — now the speculative second — raises a TERMINAL error: it is
    swallowed to the first form's honest clean-empty (never a quarantine), exactly as
    the pre-reorder law did for the encoded form.
    """

    # Named like the SDK exception so from_groww_exception maps it TERMINAL.
    class GrowwAPIBadRequestException(Exception):
        """Fake SDK 400 (mapped terminal by class name)."""

    class _EncodedEmptyPlainRaisesSDK(_CandleFormSDK):
        def get_historical_candles(self, **kw: object) -> Mapping[str, object]:
            symbol = str(kw["groww_symbol"])
            self.symbols.append(symbol)
            if "%26" in symbol:
                return {"candles": []}
            raise GrowwAPIBadRequestException("bad request")

    sdk = _EncodedEmptyPlainRaisesSDK()
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None)
    pref = SymbolFormPreference()
    c1 = "NSE-M&M-02Jan25-2400-CE"
    pref.record_winner("M&M", underlying_symbol_forms(c1), "NSE-M%26M-02Jan25-2400-CE")
    bars = _fetch_contract_candles(client, c1, pref=pref, key="M&M")
    assert bars == []  # the first (encoded) form's honest clean-empty — never a raise
    assert sdk.symbols == ["NSE-M%26M-02Jan25-2400-CE", c1]  # encoded FIRST, plain second


# ─────────────────────────────────────────────────────────────────────────
# option_ingest — ONE shared preference per run (end-to-end through the spine)
# ─────────────────────────────────────────────────────────────────────────


def test_run_groww_option_ingest_shares_one_preference_across_the_chain() -> None:
    """The spine constructs ONE preference: contract 1's ``%26`` win reorders contract 2."""
    sdk = _CandleFormSDK()
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None)
    contracts = [
        DerivativeContract(
            symbol="NSE-M&M-02Jan25-2400-CE",
            underlying_symbol="M&M",
            expiry=date(2025, 1, 2),
            contract_type="CE",
            strike=2400.0,
        ),
        DerivativeContract(
            symbol="NSE-M&M-02Jan25-2500-PE",
            underlying_symbol="M&M",
            expiry=date(2025, 1, 2),
            contract_type="PE",
            strike=2500.0,
        ),
    ]
    report = run_groww_option_ingest(
        client,
        NullBarSink(),
        contracts=contracts,
        intervals=("1day",),
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        empty_retry_attempts=1,
    )
    assert not report.failures
    # Contract 1: plain (empty) then encoded (bars). Contract 2: encoded FIRST.
    assert sdk.symbols == [
        "NSE-M&M-02Jan25-2400-CE",
        "NSE-M%26M-02Jan25-2400-CE",
        "NSE-M%26M-02Jan25-2500-PE",
    ]
