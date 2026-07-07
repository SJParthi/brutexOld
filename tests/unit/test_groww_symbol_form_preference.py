"""Tests for the learned ``&``-symbol form ordering (DEC-SYMBOL-FORM-PREFERENCE-001)
and its durable cross-pull layer (DEC-FORM-WINNER-PERSIST-001).

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
  follows the SPECULATIVE SECOND form, whichever form that now is;
* the DURABLE map (DEC-FORM-WINNER-PERSIST-001): ``parse_form_winner`` /
  ``form_winner_key`` / the kill switch / :class:`PersistentSymbolFormPreference`
  (seed adoption · single-flight · write-on-change · never-crash · lock discipline ·
  16-thread hammering · store=None base-parity · the probe-stamp honesty pins) /
  :func:`form_probe_status`, plus the ``preference_key`` normalization seam in
  ``contracts.py`` and the CROSS-PULL durability acceptance through the real spine.

The ``%26`` encoding itself stays vendor-UNVERIFIED (DEC-GROWW-AMPERSAND-FALLBACK-001's
honest envelope) — this feature only reorders which hypothesis is tried first.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from datetime import date

import pytest
from brutex.adapters.groww import symbol_form_preference as sfp
from brutex.adapters.groww.candles import fetch_candle_windows
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.contracts import (
    enumerate_contracts,
    resolve_contracts,
    resolve_expiries,
    underlying_symbol_forms,
)
from brutex.adapters.groww.option_ingest import run_groww_option_ingest
from brutex.adapters.groww.symbol_form_preference import (
    FORM_PROBE_VERDICT_KEY,
    FORM_WINNER_META_PREFIX,
    FormProbeStatus,
    PersistentSymbolFormPreference,
    SymbolFormPreference,
    form_probe_status,
    form_winner_key,
    form_winner_persist_enabled,
    parse_form_winner,
)
from brutex.adapters.protocol import DerivativeContract
from brutex.data.sink import NullBarSink
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs

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

    def test_single_form_record_is_a_noop_and_never_clobbers_a_multiform_role(self) -> None:
        """Round-3 review fix: a 1-tuple carries ZERO role information.

        The metadata AND candle phases share ONE preference keyed by the PRIMARY
        underlying, and ``candles.py`` records the CONTRACT symbol's own canonical
        tuple — a 1-tuple when that spelling carries no ``&`` — under the ``&``
        ``preference_key``. Pre-fix the base record stored index 0 under the
        shared key, resetting a learned encoded=1 role back to plain-0 for the
        rest of the run (one wasted plain-form empty fetch per subsequent
        multi-form call until an encoded win re-recorded). A single-form record
        is now a NO-OP: nothing stored, nothing clobbered, ``False`` returned.
        """
        pref = SymbolFormPreference()
        forms = ("M&M", "M%26M")
        assert pref.record_winner("M&M", forms, "M%26M") is True  # encoded role learned
        # A candle win whose contract spelling has no '&' (1-tuple) under the
        # SAME primary key must not reset the learned role.
        one_form = ("NSE-M-25Sep25-100-CE",)
        assert pref.record_winner("M&M", one_form, one_form[0]) is False
        assert pref.order("M&M", forms) == ("M%26M", "M&M")  # role intact

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


# ─────────────────────────────────────────────────────────────────────────
# DEC-FORM-WINNER-PERSIST-001 — the durable cross-pull layer
# ─────────────────────────────────────────────────────────────────────────

_FORMS = ("M&M", "M%26M")


class _MemMetaStore:
    """An in-memory ``MetaKV`` twin recording EVERY call (the write/read spy)."""

    def __init__(self, *, rows: Mapping[str, str] | None = None) -> None:
        self.rows: dict[str, str] = dict(rows or {})
        self.get_calls: list[str] = []
        self.set_calls: list[tuple[str, str]] = []

    def set_meta(self, key: str, value: str) -> None:
        self.set_calls.append((key, value))
        self.rows[key] = value

    def get_meta(self, key: str) -> str | None:
        self.get_calls.append(key)
        return self.rows.get(key)


class TestParseFormWinner:
    def test_valid_form_round_trips(self) -> None:
        assert parse_form_winner("M%26M") == "M%26M"

    def test_padded_value_is_accepted_stripped(self) -> None:
        assert parse_form_winner("  M%26M \t") == "M%26M"

    def test_none_empty_whitespace_reject(self) -> None:
        assert parse_form_winner(None) is None
        assert parse_form_winner("") is None
        assert parse_form_winner("   \t ") is None

    def test_overlong_value_rejects(self) -> None:
        assert parse_form_winner("X" * 65) is None
        assert parse_form_winner("X" * 64) == "X" * 64  # the boundary is inclusive

    def test_embedded_newline_rejects(self) -> None:
        assert parse_form_winner("M%26M\nEVIL") is None
        assert parse_form_winner("M%26M\rEVIL") is None

    @given(st.text(max_size=200))
    def test_never_raises_on_arbitrary_text(self, raw: str) -> None:
        out = parse_form_winner(raw)
        if out is not None:
            assert out == out.strip()
            assert len(out) <= 64
            assert "\n" not in out and "\r" not in out


class TestFormWinnerKeys:
    def test_key_builder_prefixes_the_underlying(self) -> None:
        assert form_winner_key("M&M") == "groww.form_winner.M&M"
        assert form_winner_key("M&M").startswith(FORM_WINNER_META_PREFIX)

    def test_stamp_key_is_not_under_the_winner_prefix(self) -> None:
        """Collision pin: a bulk prefix load can never confuse the stamp for a winner."""
        assert not FORM_PROBE_VERDICT_KEY.startswith(FORM_WINNER_META_PREFIX)


class TestFormWinnerPersistEnabled:
    def test_default_unset_is_enabled(self) -> None:
        assert form_winner_persist_enabled(env={}) is True

    @pytest.mark.parametrize("value", ["0", "off", "OFF", "false", "False", " no "])
    def test_disabled_values(self, value: str) -> None:
        assert form_winner_persist_enabled(env={"BRUTEX_FORM_WINNER_PERSIST": value}) is False

    @pytest.mark.parametrize("value", ["1", "on", "true", "yes", "anything"])
    def test_other_values_stay_enabled(self, value: str) -> None:
        assert form_winner_persist_enabled(env={"BRUTEX_FORM_WINNER_PERSIST": value}) is True

    def test_default_env_source_is_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_FORM_WINNER_PERSIST", raising=False)
        assert form_winner_persist_enabled() is True
        monkeypatch.setenv("BRUTEX_FORM_WINNER_PERSIST", "off")
        assert form_winner_persist_enabled() is False


class TestPersistentSymbolFormPreference:
    def test_persisted_winner_seeds_first_order_of_a_fresh_instance(self) -> None:
        """THE durability contract: a fresh process issues the learned winner FIRST."""
        store = _MemMetaStore(rows={form_winner_key("M&M"): "M%26M"})
        pref = PersistentSymbolFormPreference(store)
        assert pref.order("M&M", _FORMS) == ("M%26M", "M&M")

    def test_unknown_persisted_form_is_ignored(self) -> None:
        store = _MemMetaStore(rows={form_winner_key("M&M"): "SOMETHING-ELSE"})
        pref = PersistentSymbolFormPreference(store)
        assert pref.order("M&M", _FORMS) == _FORMS  # stale row → canonical, no crash

    def test_corrupt_persisted_value_is_ignored(self) -> None:
        store = _MemMetaStore(rows={form_winner_key("M&M"): "bad\nvalue"})
        pref = PersistentSymbolFormPreference(store)
        assert pref.order("M&M", _FORMS) == _FORMS

    def test_single_flight_memo_reads_the_store_once(self) -> None:
        store = _MemMetaStore(rows={form_winner_key("M&M"): "M%26M"})
        pref = PersistentSymbolFormPreference(store)
        assert pref.order("M&M", _FORMS) == ("M%26M", "M&M")
        assert pref.order("M&M", _FORMS) == ("M%26M", "M&M")
        assert store.get_calls == [form_winner_key("M&M")]  # exactly ONE get_meta

    def test_single_form_key_never_touches_the_store(self) -> None:
        """The ~750 plain underlyings pay ZERO store I/O (the O(1) hot-path pin)."""
        store = _MemMetaStore()
        pref = PersistentSymbolFormPreference(store)
        assert pref.order("NIFTY", ("NIFTY",)) == ("NIFTY",)
        assert store.get_calls == []

    def test_record_winner_single_form_key_never_touches_the_store(self) -> None:
        """Review-fix regression (MAJOR): a plain single-form key must NEVER persist.

        The production record sites fire for EVERY underlying (candles.py records
        any non-empty first fetch; contracts.py records every non-empty result),
        so without the ``len(forms) > 1`` persist gate every plain F&O underlying
        wrote one junk ``schema_meta`` row per pull process (~212 rows/pull,
        never read back — order() only consults the store for multi-form keys).
        Round-3 review fix: a single-form record is now a base-class NO-OP too
        (``False`` — a 1-tuple carries zero role information), so nothing is
        stored in memory either.
        """
        store = _MemMetaStore()
        pref = PersistentSymbolFormPreference(store)
        for name in ("NIFTY", "RELIANCE", "TCS", "HDFCBANK"):
            # A single-form record is a NO-OP end to end (round 3)…
            assert pref.record_winner(name, (name,), name) is False
        # …and the durable store sees ZERO I/O — no junk rows, no hot-path writes.
        assert store.set_calls == []
        assert store.get_calls == []
        assert store.rows == {}

    def test_record_winner_single_form_contract_symbol_never_persists(self) -> None:
        """The candle path's self-keyed plain contract (no ``&``) also never writes."""
        store = _MemMetaStore()
        pref = PersistentSymbolFormPreference(store)
        symbol = "NSE-TCS-25Sep25-3000-CE"
        assert pref.record_winner(symbol, (symbol,), symbol) is False  # round-3 no-op
        assert store.set_calls == []

    def test_single_form_record_never_clobbers_a_durable_seeded_role(self) -> None:
        """Round-3 review fix: the durable-seeded encoded role survives a 1-tuple win.

        The candle phase can record a no-``&`` contract spelling (a 1-tuple from
        ``underlying_symbol_forms``) under an ``&`` PRIMARY ``preference_key``
        (``candles.py`` — ``pref_key = contract.underlying_symbol``). Pre-fix the
        base record reset the durable-seeded encoded=1 role back to plain-0
        IN MEMORY for the rest of the run (the durable row itself was already
        persist-gated), re-paying one wasted plain-form empty fetch per later
        multi-form call. Now the 1-tuple record is a no-op end to end.
        """
        store = _MemMetaStore(rows={form_winner_key("M&M"): "M%26M"})
        pref = PersistentSymbolFormPreference(store)
        assert pref.order("M&M", _FORMS) == ("M%26M", "M&M")  # durable seed adopted
        one_form = ("NSE-M-25Sep25-100-CE",)
        assert pref.record_winner("M&M", one_form, one_form[0]) is False
        assert pref.order("M&M", _FORMS) == ("M%26M", "M&M")  # seeded role INTACT
        assert store.set_calls == []  # durable row untouched too

    def test_get_meta_error_warns_and_degrades_without_a_reread_storm(self) -> None:
        class _RaisingStore(_MemMetaStore):
            def get_meta(self, key: str) -> str | None:
                super().get_meta(key)
                raise RuntimeError("duckdb wedged")

        store = _RaisingStore()
        pref = PersistentSymbolFormPreference(store)
        assert pref.order("M&M", _FORMS) == _FORMS  # canonical order, never a raise
        assert pref.order("M&M", _FORMS) == _FORMS
        assert len(store.get_calls) == 1  # the error marked the key loaded — no storm

    def test_write_on_change_persists_once_and_repeat_is_zero_io(self) -> None:
        store = _MemMetaStore()
        pref = PersistentSymbolFormPreference(store)
        assert pref.record_winner("M&M", _FORMS, "M%26M") is True
        assert store.set_calls == [(form_winner_key("M&M"), "M%26M")]
        # Repeat SAME winner: ZERO further I/O (the fan-out cannot write-storm).
        assert pref.record_winner("M&M", _FORMS, "M%26M") is False
        assert pref.record_winner("M&M", _FORMS, "M%26M") is False
        assert len(store.set_calls) == 1

    def test_change_back_to_plain_persists_the_plain_form(self) -> None:
        """Self-heal pin: a vendor that starts serving plain ``&`` re-learns plain."""
        store = _MemMetaStore()
        pref = PersistentSymbolFormPreference(store)
        pref.record_winner("M&M", _FORMS, "M%26M")
        assert pref.record_winner("M&M", _FORMS, "M&M") is True
        assert store.set_calls[-1] == (form_winner_key("M&M"), "M&M")
        assert store.rows[form_winner_key("M&M")] == "M&M"

    def test_candle_phase_win_persists_the_key_level_role_form(self) -> None:
        """Review-fix regression (MAJOR): the persisted VALUE is role-normalised.

        The candle phase records a per-CONTRACT canonical tuple under the
        UNDERLYING key; persisting the raw contract string fragmented the value
        space (the next process's metadata seed — and every OTHER contract's —
        discarded it as stale). The durable value is now the winner's ROLE
        applied to the KEY's own forms tuple, so one row serves every phase.
        """
        store = _MemMetaStore()
        pref = PersistentSymbolFormPreference(store)
        contract_forms = ("NSE-M&M-25Sep25-3000-CE", "NSE-M%26M-25Sep25-3000-CE")
        assert pref.record_winner("M&M", contract_forms, contract_forms[1]) is True
        # The KEY-level encoded form is persisted — NEVER the contract spelling.
        assert store.rows == {form_winner_key("M&M"): "M%26M"}

    def test_candle_written_row_seeds_every_phase_in_a_fresh_process(self) -> None:
        """Cross-domain durability: one row serves metadata AND other contracts."""
        store = _MemMetaStore()
        p1 = PersistentSymbolFormPreference(store)
        contract_a = ("NSE-M&M-25Sep25-3000-CE", "NSE-M%26M-25Sep25-3000-CE")
        p1.record_winner("M&M", contract_a, contract_a[1])  # candle-phase win
        # Fresh process, METADATA tuple: the learned encoded role leads.
        p2 = PersistentSymbolFormPreference(store)
        assert p2.order("M&M", _FORMS) == ("M%26M", "M&M")
        # Fresh process, a DIFFERENT contract's tuple: same role, translated.
        contract_b = ("NSE-M&M-01Feb24-2400-CE", "NSE-M%26M-01Feb24-2400-CE")
        p3 = PersistentSymbolFormPreference(store)
        assert p3.order("M&M", contract_b) == (contract_b[1], contract_b[0])

    def test_metadata_written_row_seeds_a_candle_first_fresh_process(self) -> None:
        """The reverse direction: an underlying-level row seeds a contract tuple."""
        store = _MemMetaStore(rows={form_winner_key("M&M"): "M%26M"})
        pref = PersistentSymbolFormPreference(store)
        contract = ("NSE-M&M-25Sep25-3000-CE", "NSE-M%26M-25Sep25-3000-CE")
        assert pref.order("M&M", contract) == (contract[1], contract[0])

    def test_legacy_raw_contract_string_row_still_seeds_by_membership(self) -> None:
        """A pre-normalisation row (the raw contract spelling) seeds its OWN tuple."""
        contract = ("NSE-M&M-25Sep25-3000-CE", "NSE-M%26M-25Sep25-3000-CE")
        store = _MemMetaStore(rows={form_winner_key("M&M"): contract[1]})
        pref = PersistentSymbolFormPreference(store)
        assert pref.order("M&M", contract) == (contract[1], contract[0])

    def test_plain_key_with_multiform_tuple_persists_raw_and_seeds_back(self) -> None:
        """Defensive fallback: a key whose OWN tuple cannot express the role.

        A no-``&`` key recorded against a multi-form tuple (a caller mismatch —
        never wired in production) persists the raw winning form; the seed's
        membership arm reads it back for the same tuple. Reorder-never-narrow
        holds throughout.
        """
        store = _MemMetaStore()
        pref = PersistentSymbolFormPreference(store)
        forms = ("M&M", "M%26M")
        assert pref.record_winner("PLAINKEY", forms, "M%26M") is True
        assert store.rows == {form_winner_key("PLAINKEY"): "M%26M"}  # raw fallback
        fresh = PersistentSymbolFormPreference(store)
        assert fresh.order("PLAINKEY", forms) == ("M%26M", "M&M")

    def test_unknown_winning_form_is_a_defensive_noop_with_no_io(self) -> None:
        store = _MemMetaStore()
        pref = PersistentSymbolFormPreference(store)
        assert pref.record_winner("M&M", _FORMS, "NOT-A-FORM") is False
        assert store.set_calls == []

    def test_set_meta_error_warns_and_the_in_memory_winner_still_works(self) -> None:
        class _WriteRaises(_MemMetaStore):
            def set_meta(self, key: str, value: str) -> None:
                super().set_meta(key, value)
                raise RuntimeError("disk full")

        store = _WriteRaises()
        pref = PersistentSymbolFormPreference(store)
        assert pref.record_winner("M&M", _FORMS, "M%26M") is True  # never raises
        assert pref.order("M&M", _FORMS) == ("M%26M", "M&M")  # in-memory truth effective

    def test_store_none_is_byte_identical_to_the_base_class(self) -> None:
        base = SymbolFormPreference()
        durable = PersistentSymbolFormPreference(None)
        script: list[tuple[str, str]] = [
            ("order", "M&M"),
            ("record", "M%26M"),
            ("order", "M&M"),
            ("record", "M&M"),
            ("order", "M&M"),
            ("record", "NOT-A-FORM"),
            ("order", "M&M"),
        ]
        for action, arg in script:
            if action == "order":
                assert base.order(arg, _FORMS) == durable.order(arg, _FORMS)
            else:
                assert base.record_winner("M&M", _FORMS, arg) == durable.record_winner(
                    "M&M", _FORMS, arg
                )

    def test_kill_switch_disables_writes_but_not_ordering(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRUTEX_FORM_WINNER_PERSIST", "off")
        store = _MemMetaStore()
        pref = PersistentSymbolFormPreference(store)
        assert pref.record_winner("M&M", _FORMS, "M%26M") is True
        assert store.set_calls == []  # write-side disabled
        assert pref.order("M&M", _FORMS) == ("M%26M", "M&M")  # in-memory learning intact

    def test_kill_switch_enabled_value_persists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_FORM_WINNER_PERSIST", "1")
        store = _MemMetaStore()
        pref = PersistentSymbolFormPreference(store)
        pref.record_winner("M&M", _FORMS, "M%26M")
        assert store.set_calls == [(form_winner_key("M&M"), "M%26M")]

    def test_in_process_win_is_never_clobbered_by_a_stale_persisted_row(self) -> None:
        """setdefault pin: memory recorded BEFORE the first order beats the old row."""
        store = _MemMetaStore(rows={form_winner_key("M&M"): "M%26M"})
        pref = PersistentSymbolFormPreference(store)
        pref.record_winner("M&M", _FORMS, "M&M")  # in-process truth: plain wins
        assert pref.order("M&M", _FORMS) == _FORMS  # NOT reordered by the stale row

    def test_persist_reentering_order_on_another_key_does_not_deadlock(self) -> None:
        """Lock-discipline pin: the persist runs OUTSIDE the map lock."""
        pref_box: list[PersistentSymbolFormPreference] = []

        class _ReentrantStore(_MemMetaStore):
            def set_meta(self, key: str, value: str) -> None:
                super().set_meta(key, value)
                # Re-enter order() on ANOTHER key mid-persist (a hostile MetaKV).
                pref_box[0].order("L&TFH", ("L&TFH", "L%26TFH"))

        store = _ReentrantStore()
        pref = PersistentSymbolFormPreference(store)
        pref_box.append(pref)
        worker = threading.Thread(target=lambda: pref.record_winner("M&M", _FORMS, "M%26M"))
        worker.start()
        worker.join(timeout=10.0)
        assert not worker.is_alive()  # a deadlock would leave the worker hung
        assert store.rows[form_winner_key("M&M")] == "M%26M"

    def test_wedged_seed_read_never_stalls_other_keys_or_a_same_key_racer(self) -> None:
        """Review-fix regression (MAJOR): the map lock is NEVER held across the seed READ.

        The pre-fix code executed ``get_meta`` INSIDE the map lock, so ONE
        slow/wedged ``instruments.duckdb`` read on the first ``&``-key order
        serialised EVERY worker's ``order``/``record_winner`` behind it
        (empirically: a plain ``order("NIFTY")`` blocked the full wedge). Now the
        key is claimed under the lock, the read runs OUTSIDE it, and every other
        caller — plain keys, other ``&`` keys, and same-key racers (which serve
        the canonical order for that one call) — proceeds immediately.
        """

        class _WedgedStore(_MemMetaStore):
            def __init__(self) -> None:
                super().__init__()
                self.release = threading.Event()
                self.read_started = threading.Event()

            def get_meta(self, key: str) -> str | None:
                if "M&M" in key:
                    self.read_started.set()
                    self.release.wait(timeout=10.0)
                return super().get_meta(key)

        store = _WedgedStore()
        pref = PersistentSymbolFormPreference(store)
        reader = threading.Thread(target=lambda: pref.order("M&M", _FORMS), daemon=True)
        reader.start()
        assert store.read_started.wait(timeout=10.0)  # the wedged read IS in flight
        started = time.monotonic()
        assert pref.order("NIFTY", ("NIFTY",)) == ("NIFTY",)  # plain key: instant
        other = ("L&TFH", "L%26TFH")
        assert sorted(pref.order("L&TFH", other)) == sorted(other)  # other & key: instant
        assert pref.order("M&M", _FORMS) == _FORMS  # same-key racer: canonical, instant
        assert pref.record_winner("L&TFH", other, "L%26TFH") is True  # record: instant
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"fan-out stalled {elapsed:.2f}s behind the wedged read"
        store.release.set()
        reader.join(timeout=10.0)
        assert not reader.is_alive()

    def test_wedged_seed_read_is_deadline_bounded_and_degrades_canonical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read that NEVER returns costs the reading worker one bounded deadline.

        The COMMON ``run_with_deadline`` freeze guard bounds the seed ``get_meta``
        (env ``BRUTEX_FORM_WINNER_READ_DEADLINE_S``): on expiry the order degrades
        to the canonical tuple (never a crash, never a narrowed search) and the
        key stays claimed — no re-read storm against the wedged volume.
        """
        monkeypatch.setenv("BRUTEX_FORM_WINNER_READ_DEADLINE_S", "0.05")
        release = threading.Event()

        class _HangingStore(_MemMetaStore):
            def get_meta(self, key: str) -> str | None:
                super().get_meta(key)
                release.wait(timeout=10.0)  # hangs past the 0.05 s deadline
                return None

        store = _HangingStore()
        pref = PersistentSymbolFormPreference(store)
        started = time.monotonic()
        assert pref.order("M&M", _FORMS) == _FORMS  # canonical — never a crash
        assert time.monotonic() - started < 5.0
        assert pref.order("M&M", _FORMS) == _FORMS  # loaded memo: no re-read storm
        assert len(store.get_calls) == 1
        release.set()  # let the abandoned daemon finish

    def test_wedged_persist_write_is_deadline_bounded_and_never_hangs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review round-2 MAJOR: a ``set_meta`` that NEVER returns cannot hang the pull.

        The read-side wedge fix (above) left the WRITE side unbounded: a
        non-raising wedged ``instruments.duckdb`` write would block
        ``record_winner`` FOREVER on the F&O fetch worker (the pull driver
        awaits that worker's future — a permanent, log-less hang: the
        ``persist_failed`` WARN only fires on a RAISING store) and queue every
        later persist behind ``_persist_lock``. The COMMON ``run_with_deadline``
        freeze guard now bounds the write (env
        ``BRUTEX_FORM_WINNER_WRITE_DEADLINE_S``): on expiry the
        ``form_winner.persist_failed`` WARN fires with ``DeadlineExceeded``,
        the in-memory winner stands, and a LATER persist pays its OWN bound —
        never a forever queue behind the wedge.
        """
        monkeypatch.setenv("BRUTEX_FORM_WINNER_WRITE_DEADLINE_S", "0.05")
        release = threading.Event()

        class _WedgedWriteStore(_MemMetaStore):
            def set_meta(self, key: str, value: str) -> None:
                super().set_meta(key, value)
                release.wait(timeout=10.0)  # wedges past the 0.05 s deadline

        store = _WedgedWriteStore()
        pref = PersistentSymbolFormPreference(store)
        started = time.monotonic()
        with capture_logs() as logs:
            assert pref.record_winner("M&M", _FORMS, "M%26M") is True  # bounded, no raise
            # A later persist (another key) is NOT permanently serialised behind
            # the wedge — it acquires the released persist lock and pays its own
            # bounded deadline.
            other = ("L&TFH", "L%26TFH")
            assert pref.record_winner("L&TFH", other, "L%26TFH") is True
        assert time.monotonic() - started < 5.0, "record_winner hung on the wedged write"
        warns = [r for r in logs if r["event"] == "form_winner.persist_failed"]
        assert [w["error_type"] for w in warns] == ["DeadlineExceeded", "DeadlineExceeded"]
        assert pref.order("M&M", _FORMS) == ("M%26M", "M&M")  # in-memory truth intact
        release.set()  # let the abandoned daemons finish

    def test_sixteen_thread_hammer_single_flight_and_one_write(self) -> None:
        """Concurrency: mixed order/record under a Barrier — one read, one write."""

        class _SlowStore(_MemMetaStore):
            def get_meta(self, key: str) -> str | None:
                time.sleep(0.01)
                return super().get_meta(key)

        store = _SlowStore()
        pref = PersistentSymbolFormPreference(store)
        barrier = threading.Barrier(16)
        errors: list[Exception] = []

        def _worker(i: int) -> None:
            try:
                barrier.wait(timeout=30.0)
                for _ in range(50):
                    if i % 2:
                        out = pref.order("M&M", _FORMS)
                        assert sorted(out) == sorted(_FORMS)  # always a permutation
                    else:
                        pref.record_winner("M&M", _FORMS, "M%26M")
            except Exception as exc:  # captured for the assert below
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60.0)
        assert errors == []
        assert len(store.get_calls) <= 1  # single-flight (0 iff a record landed first)
        assert store.set_calls == [(form_winner_key("M&M"), "M%26M")]  # ONE write
        assert store.rows[form_winner_key("M&M")] in _FORMS  # persisted value valid
        assert pref.order("M&M", _FORMS) == ("M%26M", "M&M")  # one consistent winner

    def test_steady_state_order_and_record_do_zero_store_io(self) -> None:
        """The O(1)-hot-path pin: after warm-up NO store call happens per operation."""
        store = _MemMetaStore()
        pref = PersistentSymbolFormPreference(store)
        pref.order("M&M", _FORMS)
        pref.record_winner("M&M", _FORMS, "M%26M")
        gets, sets = len(store.get_calls), len(store.set_calls)
        for _ in range(100):
            pref.order("M&M", _FORMS)
            pref.record_winner("M&M", _FORMS, "M%26M")
        assert (len(store.get_calls), len(store.set_calls)) == (gets, sets)

    def test_never_touches_the_probe_verdict_key(self) -> None:
        """HONESTY structural pin: production learning can never mint 'verified'."""
        store = _MemMetaStore(rows={form_winner_key("M&M"): "M%26M"})
        pref = PersistentSymbolFormPreference(store)
        pref.order("M&M", _FORMS)
        pref.record_winner("M&M", _FORMS, "M&M")
        pref.record_winner("M&M", _FORMS, "M%26M")
        assert all(key != FORM_PROBE_VERDICT_KEY for key, _v in store.set_calls)
        assert all(key != FORM_PROBE_VERDICT_KEY for key in store.get_calls)

    def test_module_source_has_no_stamp_writer(self) -> None:
        """AST-level pin: this module contains NO ``set_meta(FORM_PROBE_VERDICT_KEY`` site."""
        import inspect

        source = inspect.getsource(sfp)
        assert "set_meta(FORM_PROBE_VERDICT_KEY" not in source


class TestFormProbeStatus:
    def test_none_store_is_unverified(self) -> None:
        assert form_probe_status(None) == FormProbeStatus(verified=False)

    def test_missing_row_is_unverified(self) -> None:
        assert form_probe_status(_MemMetaStore()).verified is False

    def test_corrupt_json_is_unverified_never_raises(self) -> None:
        store = _MemMetaStore(rows={FORM_PROBE_VERDICT_KEY: "{not json"})
        assert form_probe_status(store).verified is False

    def test_non_mapping_json_is_unverified(self) -> None:
        store = _MemMetaStore(rows={FORM_PROBE_VERDICT_KEY: '["a", "b"]'})
        assert form_probe_status(store).verified is False

    def test_store_read_error_is_unverified(self) -> None:
        class _RaisingStore(_MemMetaStore):
            def get_meta(self, key: str) -> str | None:
                raise RuntimeError("wedged")

        assert form_probe_status(_RaisingStore()).verified is False

    def test_valid_stamp_reads_verified_with_evidence_fields(self) -> None:
        stamp = (
            '{"verdict": "ENCODED_ONLY", "winners": {"M&M": "M%26M"}, '
            '"transcript": "/lake/_liveproof/lp-1/transcript.jsonl", '
            '"run_id": "lp-1", "probed_at": "2026-07-06T00:00:00+00:00"}'
        )
        store = _MemMetaStore(rows={FORM_PROBE_VERDICT_KEY: stamp})
        status = form_probe_status(store)
        assert status.verified is True
        assert status.verdict == "ENCODED_ONLY"
        assert status.winners == {"M&M": "M%26M"}
        assert status.transcript_path == "/lake/_liveproof/lp-1/transcript.jsonl"
        assert status.run_id == "lp-1"
        assert status.probed_at == "2026-07-06T00:00:00+00:00"

    def test_partial_stamp_fields_degrade_to_none(self) -> None:
        store = _MemMetaStore(rows={FORM_PROBE_VERDICT_KEY: '{"winners": "not-a-map"}'})
        status = form_probe_status(store)
        assert status.verified is True  # the stamp row exists and parses
        assert status.verdict is None
        assert status.winners is None
        assert status.transcript_path is None
        assert status.run_id is None
        assert status.probed_at is None


# ─────────────────────────────────────────────────────────────────────────
# Property pins — the try-both never-narrow law over arbitrary inputs
# ─────────────────────────────────────────────────────────────────────────

_forms_strategy = st.lists(
    st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Nd"), whitelist_characters="&%"),
        min_size=1,
        max_size=12,
    ),
    min_size=1,
    max_size=4,
    unique=True,
).map(tuple)


@given(key=st.text(min_size=0, max_size=16), forms=_forms_strategy, seed=st.text(max_size=80))
def test_property_order_is_always_a_permutation(
    key: str, forms: tuple[str, ...], seed: str
) -> None:
    """Arbitrary key/forms/persisted content: order() only ever PERMUTES the input."""
    store = _MemMetaStore(rows={form_winner_key(key): seed})
    pref = PersistentSymbolFormPreference(store)
    out = pref.order(key, forms)
    assert sorted(out) == sorted(forms)


@given(forms=_forms_strategy, winner_idx=st.integers(min_value=0, max_value=3))
def test_property_record_then_order_moves_the_winner_first(
    forms: tuple[str, ...], winner_idx: int
) -> None:
    """record → order round-trip: the winner leads, membership is preserved, stable."""
    store = _MemMetaStore()
    pref = PersistentSymbolFormPreference(store)
    winner = forms[winner_idx % len(forms)]
    pref.record_winner("K", forms, winner)
    out = pref.order("K", forms)
    assert out[0] == winner
    assert sorted(out) == sorted(forms)
    assert pref.order("K", forms) == out  # stable across repeats


# ─────────────────────────────────────────────────────────────────────────
# contracts.py — the preference_key normalization seam (DEC-FORM-WINNER-PERSIST-001)
# ─────────────────────────────────────────────────────────────────────────


def test_resolve_expiries_preference_key_records_under_the_primary_key() -> None:
    """The memory keys on preference_key (the PRIMARY), never the queried spelling."""
    sdk = _ResolutionSDK()
    client = _resolution_client(sdk)
    store = _MemMetaStore()
    pref = PersistentSymbolFormPreference(store)
    resolve_expiries(
        client,
        exchange="NSE",
        underlying_symbol="M&M",
        form_preference=pref,
        preference_key="PRIMARY&KEY",
    )
    # The durable row lands under the PRIMARY key — not the queried spelling —
    # and the VALUE is role-normalised to the KEY's own forms (encoded role 1).
    assert store.rows == {form_winner_key("PRIMARY&KEY"): "PRIMARY%26KEY"}
    assert pref.order("PRIMARY&KEY", _FORMS) == ("M%26M", "M&M")
    assert pref.order("M&M", _FORMS) == _FORMS  # the spelling key carries NO memory
    # And the SECOND call under the same primary key issues the winner first.
    resolve_expiries(
        client,
        exchange="NSE",
        underlying_symbol="M&M",
        form_preference=pref,
        preference_key="PRIMARY&KEY",
    )
    assert sdk.expiry_symbols == ["M&M", "M%26M", "M%26M"]


def test_resolve_contracts_preference_key_records_under_the_primary_key() -> None:
    sdk = _ResolutionSDK()
    client = _resolution_client(sdk)
    pref = SymbolFormPreference()
    resolve_contracts(
        client,
        exchange="NSE",
        underlying_symbol="M&M",
        expiry=date(2025, 1, 2),
        form_preference=pref,
        preference_key="PRIMARY&KEY",
    )
    assert pref.order("PRIMARY&KEY", _FORMS) == ("M%26M", "M&M")
    assert pref.order("M&M", _FORMS) == _FORMS
    resolve_contracts(
        client,
        exchange="NSE",
        underlying_symbol="M&M",
        expiry=date(2025, 1, 2),
        form_preference=pref,
        preference_key="PRIMARY&KEY",
    )
    assert sdk.contract_symbols == ["M&M", "M%26M", "M%26M"]


def test_resolve_expiries_default_preference_key_is_the_underlying() -> None:
    """preference_key=None keys by underlying_symbol verbatim — byte-identical."""
    sdk = _ResolutionSDK()
    pref = SymbolFormPreference()
    resolve_expiries(
        _resolution_client(sdk), exchange="NSE", underlying_symbol="M&M", form_preference=pref
    )
    assert pref.order("M&M", _FORMS) == ("M%26M", "M&M")  # keyed by the underlying


def test_cold_process_with_seeded_store_never_reprobes_the_loser() -> None:
    """Never-re-probe pin: a pre-seeded store makes the FIRST call the winner — 1 call."""
    sdk = _ResolutionSDK()
    store = _MemMetaStore(rows={form_winner_key("M&M"): "M%26M"})
    pref = PersistentSymbolFormPreference(store)  # a FRESH process
    out = resolve_expiries(
        _resolution_client(sdk), exchange="NSE", underlying_symbol="M&M", form_preference=pref
    )
    assert out == [date(2025, 1, 2)]
    assert sdk.expiry_symbols == ["M%26M"]  # winner-first; the loser never called


def test_seeded_but_wrong_winner_still_tries_the_loser_and_self_heals() -> None:
    """Reorder-NEVER-narrow: a stale %26 seed costs one empty fetch, never data."""

    class _PlainOnlySDK(_ResolutionSDK):
        def get_expiries(self, **kw: object) -> Mapping[str, object]:
            symbol = str(kw["underlying_symbol"])
            self.expiry_symbols.append(symbol)
            if "%26" in symbol:
                return {"expiries": []}  # the seeded winner is WRONG now
            return {"expiries": ["2025-01-02"]}

    sdk = _PlainOnlySDK()
    store = _MemMetaStore(rows={form_winner_key("M&M"): "M%26M"})
    pref = PersistentSymbolFormPreference(store)
    out = resolve_expiries(
        _resolution_client(sdk), exchange="NSE", underlying_symbol="M&M", form_preference=pref
    )
    assert out == [date(2025, 1, 2)]  # data still stored — the loser WAS tried
    assert sdk.expiry_symbols == ["M%26M", "M&M"]  # seeded first, plain second
    assert store.rows[form_winner_key("M&M")] == "M&M"  # the row self-healed


def test_enumerate_contracts_passes_the_preference_through_both_apis() -> None:
    """An expiries-phase %26 win reorders the SAME enumeration's contracts call."""
    sdk = _ResolutionSDK()
    pref = SymbolFormPreference()
    chain = list(
        enumerate_contracts(
            _resolution_client(sdk),
            exchange="NSE",
            underlying_symbol="M&M",
            years=[2025],
            form_preference=pref,
            preference_key="M&M",
        )
    )
    assert [c.symbol for c in chain] == ["NSE-M&M-02Jan25-2400-CE"]
    assert sdk.expiry_symbols == ["M&M", "M%26M"]  # learned here…
    assert sdk.contract_symbols == ["M%26M"]  # …and the contracts call led with it


def test_enumerate_contracts_default_none_keeps_fixed_order() -> None:
    sdk = _ResolutionSDK()
    chain = list(
        enumerate_contracts(
            _resolution_client(sdk), exchange="NSE", underlying_symbol="M&M", years=[2025]
        )
    )
    assert [c.symbol for c in chain] == ["NSE-M&M-02Jan25-2400-CE"]
    assert sdk.contract_symbols == ["M&M", "M%26M"]  # fixed plain-first, byte-identical


# ─────────────────────────────────────────────────────────────────────────
# CROSS-PULL DURABILITY ACCEPTANCE — through the real ingest spine
# ─────────────────────────────────────────────────────────────────────────


def test_cross_pull_durability_second_pull_first_call_is_the_learned_winner() -> None:
    """Two pulls over ONE store: pull 2's VERY FIRST M&M vendor call is encoded.

    The pin the PROCESS-LOCAL class cannot pass (DEC-FORM-WINNER-PERSIST-001):
    each pull builds a FRESH PersistentSymbolFormPreference (a fresh process)
    over the SAME durable store, and the second pull re-probes NOTHING.
    """
    store = _MemMetaStore()
    contract = DerivativeContract(
        symbol="NSE-M&M-02Jan25-2400-CE",
        underlying_symbol="M&M",
        expiry=date(2025, 1, 2),
        contract_type="CE",
        strike=2400.0,
    )

    def _one_pull() -> _CandleFormSDK:
        sdk = _CandleFormSDK()
        report = run_groww_option_ingest(
            GrowwClient(sdk=sdk, sleep=lambda _s: None),
            NullBarSink(),
            contracts=[contract],
            intervals=("1day",),
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            empty_retry_attempts=1,
            form_preference=PersistentSymbolFormPreference(store),  # fresh per "process"
        )
        assert not report.failures
        return sdk

    sdk1 = _one_pull()
    # Pull 1 learned the hard way: plain (empty) then encoded (bars) — and persisted
    # the KEY-level role form (never the per-contract spelling, so the row serves
    # the metadata phase and every OTHER contract too).
    assert sdk1.symbols == ["NSE-M&M-02Jan25-2400-CE", "NSE-M%26M-02Jan25-2400-CE"]
    assert store.rows[form_winner_key("M&M")] == "M%26M"
    sdk2 = _one_pull()
    # Pull 2 (fresh preference, same store): the FIRST vendor call is the winner.
    assert sdk2.symbols[0] == "NSE-M%26M-02Jan25-2400-CE"
    assert len(sdk2.symbols) == 1  # zero wasted plain fetch
