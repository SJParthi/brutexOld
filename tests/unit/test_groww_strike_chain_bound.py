"""Unit + property tests for ``brutex.adapters.groww.strike_chain_bound``.

DEC-FETCH-PER-WINDOW-GUARANTEE-001. 100% line+branch on the pure adaptive strike-chain
bounding: the env resolvers (default / override / blank / non-int / out-of-range / clamp),
the median-center band classification, the sparse per-contract empty-retry budget map, the
futures-keep-full + too-small-chain + no-strike guards, and the ``attempts_for`` lookup.

The CORRECTNESS contract these tests pin (the operator's data-loss concern): the budget map
reduces ONLY the empty-retry COUNT for confirmed-deep strikes — it never marks a near-money
strike deep, never narrows below 1 fetch, and never narrows ABOVE the full budget. The
clean-empty-vs-transient SEPARATION (a transient is never counted) is enforced by the ingest
spine, proven in ``test_groww_option_ingest.py`` (the ``TestEmptyRetryBudgetCorrectness``
permutation suite).
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.adapters.groww.strike_chain_bound import (
    DEFAULT_DEEP_EMPTY_RETRY,
    DEFAULT_STRIKE_BAND,
    StrikeChainBudget,
    build_empty_retry_budget,
    resolve_deep_empty_retry,
    resolve_strike_band,
)
from brutex.adapters.protocol import DerivativeContract

_EXPIRY = date(2025, 1, 30)


def _option(strike: float, opt_type: str = "CE", *, expiry: date = _EXPIRY) -> DerivativeContract:
    return DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=expiry,
        symbol=f"NSE-NIFTY-30Jan25-{int(strike)}-{opt_type}",
        contract_type=opt_type,
        strike=strike,
    )


def _future(*, expiry: date = _EXPIRY) -> DerivativeContract:
    return DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=expiry,
        symbol="NSE-NIFTY-30Jan25-FUT",
        contract_type="FUT",
        strike=None,
    )


def _chain(n_per_side: int, *, center: int = 23000, step: int = 50) -> list[DerivativeContract]:
    out: list[DerivativeContract] = []
    for offset in range(-n_per_side, n_per_side + 1):
        for opt_type in ("CE", "PE"):
            out.append(_option(float(center + offset * step), opt_type))
    return out


# ── env resolvers ──────────────────────────────────────────────────────────────────────


def test_resolve_strike_band_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRUTEX_FNO_STRIKE_BAND", raising=False)
    assert resolve_strike_band() == DEFAULT_STRIKE_BAND


@pytest.mark.parametrize("raw", ["", "   ", "abc", "-1", "-100"])
def test_resolve_strike_band_falls_back_on_bad_value(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Blank / non-int / negative all fall back to the wide default (never an unsafe narrow)."""
    monkeypatch.setenv("BRUTEX_FNO_STRIKE_BAND", raw)
    assert resolve_strike_band() == DEFAULT_STRIKE_BAND


@pytest.mark.parametrize(("raw", "expected"), [("0", 0), ("5", 5), ("30", 30)])
def test_resolve_strike_band_honours_valid_override(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    monkeypatch.setenv("BRUTEX_FNO_STRIKE_BAND", raw)
    assert resolve_strike_band() == expected


def test_resolve_deep_empty_retry_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRUTEX_FNO_DEEP_EMPTY_RETRY", raising=False)
    assert resolve_deep_empty_retry() == DEFAULT_DEEP_EMPTY_RETRY


@pytest.mark.parametrize("raw", ["", "  ", "nope"])
def test_resolve_deep_empty_retry_falls_back_on_bad_value(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("BRUTEX_FNO_DEEP_EMPTY_RETRY", raw)
    assert resolve_deep_empty_retry() == DEFAULT_DEEP_EMPTY_RETRY


@pytest.mark.parametrize(("raw", "expected"), [("0", 1), ("-3", 1), ("1", 1), ("2", 2)])
def test_resolve_deep_empty_retry_clamps_to_at_least_one(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    """A value < 1 is clamped UP to 1 — a deep strike is ALWAYS fetched at least once."""
    monkeypatch.setenv("BRUTEX_FNO_DEEP_EMPTY_RETRY", raw)
    assert resolve_deep_empty_retry() == expected


# ── band classification ────────────────────────────────────────────────────────────────


def test_deep_strikes_get_reduced_budget_in_band_keep_full() -> None:
    """A 41-per-side chain with band 12: ranks >12 from center are deep, the rest full."""
    contracts = _chain(40)
    budget = build_empty_retry_budget(contracts, full_attempts=3, band=12, deep_attempts=1)
    # center strike 23000 keeps full; a strike 13 grid-steps out (23650) is deep.
    assert budget.attempts_for("NSE-NIFTY-30Jan25-23000-CE") == 3
    assert budget.attempts_for("NSE-NIFTY-30Jan25-23600-CE") == 3  # rank 12 — exactly the band edge
    assert budget.attempts_for("NSE-NIFTY-30Jan25-23650-CE") == 1  # rank 13 — first deep
    assert budget.attempts_for("NSE-NIFTY-30Jan25-21000-CE") == 1  # far deep
    assert budget.deep_contracts > 0
    # all 162 contracts are CE/PE options in one (NIFTY, expiry) chain ≥ 7 strikes, so every one
    # is CONSIDERED — total_contracts counts the considered options, here the whole list.
    assert budget.total_contracts == len(contracts)


def test_futures_always_keep_full_budget() -> None:
    """A FUT leg has no strike — it is never classified deep, always full."""
    contracts = [*_chain(40), _future()]
    budget = build_empty_retry_budget(contracts, full_attempts=3, band=12, deep_attempts=1)
    assert budget.attempts_for("NSE-NIFTY-30Jan25-FUT") == 3
    assert "NSE-NIFTY-30Jan25-FUT" not in budget.by_symbol


def test_small_chain_left_full_for_every_contract() -> None:
    """A chain below the 7-strike minimum is too small to estimate a center — all full."""
    contracts = _chain(2)  # 5 strikes/side*... actually 5 distinct strikes < 7 minimum
    budget = build_empty_retry_budget(contracts, full_attempts=3, band=1, deep_attempts=1)
    assert budget.deep_contracts == 0
    assert dict(budget.by_symbol) == {}
    assert all(budget.attempts_for(c.symbol) == 3 for c in contracts)


def test_band_zero_marks_every_non_center_strike_deep() -> None:
    """Band 0 is the most aggressive: only the exact-center strike(s) keep full."""
    contracts = _chain(40)
    budget = build_empty_retry_budget(contracts, full_attempts=3, band=0, deep_attempts=1)
    assert budget.attempts_for("NSE-NIFTY-30Jan25-23000-CE") == 3  # the median center
    assert budget.attempts_for("NSE-NIFTY-30Jan25-23050-CE") == 1  # one step out — deep


def test_deep_attempts_clamped_into_one_to_full() -> None:
    """``deep_attempts`` is clamped into [1, full]: never < 1, never > the full budget."""
    contracts = _chain(40)
    # deep_attempts 0 -> clamped to 1
    b0 = build_empty_retry_budget(contracts, full_attempts=3, band=12, deep_attempts=0)
    assert b0.deep_attempts == 1
    # deep_attempts 9 (> full) -> clamped to full (3) — a "reduction" can never inflate
    b9 = build_empty_retry_budget(contracts, full_attempts=3, band=12, deep_attempts=9)
    assert b9.deep_attempts == 3


def test_full_attempts_clamped_to_at_least_one() -> None:
    """A degenerate full_attempts < 1 is clamped to 1 (always fetch once)."""
    contracts = _chain(40)
    budget = build_empty_retry_budget(contracts, full_attempts=0, band=12, deep_attempts=1)
    assert budget.full_attempts == 1
    assert budget.deep_attempts == 1  # min(1, full=1)


def test_negative_band_arg_treated_as_default_floor() -> None:
    """A negative explicit band arg is floored to 0 (never starves via a negative width)."""
    contracts = _chain(40)
    budget = build_empty_retry_budget(contracts, full_attempts=3, band=-5, deep_attempts=1)
    # band floored to 0 -> only center full
    assert budget.attempts_for("NSE-NIFTY-30Jan25-23050-CE") == 1


def test_per_expiry_center_independent() -> None:
    """Two expiries with different strike ranges each get their OWN median center."""
    exp_a = date(2025, 1, 30)
    exp_b = date(2025, 2, 27)
    contracts: list[DerivativeContract] = []
    for offset in range(-20, 21):
        contracts.append(_option(float(23000 + offset * 50), "CE", expiry=exp_a))
        contracts.append(_option(float(30000 + offset * 50), "CE", expiry=exp_b))
    budget = build_empty_retry_budget(contracts, full_attempts=3, band=5, deep_attempts=1)
    # each expiry's center (its own median) keeps full
    assert budget.attempts_for("NSE-NIFTY-30Jan25-23000-CE") == 3
    assert budget.attempts_for("NSE-NIFTY-30Jan25-30000-CE") == 3


def _under_option(underlying: str, strike: float) -> DerivativeContract:
    """A CE for *underlying* at *strike* on the shared _EXPIRY (the multi-underlying case)."""
    return DerivativeContract(
        underlying_symbol=underlying,
        expiry=_EXPIRY,
        symbol=f"NSE-{underlying}-30Jan25-{int(strike)}-CE",
        contract_type="CE",
        strike=strike,
    )


def test_multi_underlying_same_expiry_not_pooled() -> None:
    """MAJOR fix: two underlyings with DISJOINT strike ranges on the SAME expiry are NOT pooled.

    RELIANCE (₹2800-3200) + HDFCBANK (₹1600-1780) share the same monthly expiry in the
    production all-cached pull. Keying by ``(underlying, expiry)`` (not expiry alone) gives each
    its OWN median — so each underlying's ATM strike keeps the FULL budget. Pooling by expiry
    alone would land the median in the inter-cluster gap (~₹2290) and over-flag both clusters'
    live strikes.
    """
    contracts: list[DerivativeContract] = []
    # RELIANCE chain: 2800..3200 step 20 (21 strikes), ATM ~3000.
    for k in range(2800, 3201, 20):
        contracts.append(_under_option("RELIANCE", float(k)))
    # HDFCBANK chain: 1600..1780 step 10 (19 strikes), ATM ~1690.
    for k in range(1600, 1781, 10):
        contracts.append(_under_option("HDFCBANK", float(k)))

    budget = build_empty_retry_budget(contracts, full_attempts=3, band=5, deep_attempts=1)
    # Each underlying's ATM-cluster median keeps the FULL budget (proving NO pooling): the
    # RELIANCE median (3000) and the HDFCBANK median (1690) are both in-band for their OWN chain.
    assert budget.attempts_for("NSE-RELIANCE-30Jan25-3000-CE") == 3
    assert budget.attempts_for("NSE-HDFCBANK-30Jan25-1690-CE") == 3
    # And a strike near one cluster's center is NOT dragged deep by the other cluster — under the
    # buggy pooled median (~2290) the HDFCBANK ATM (1690) would have been far-deep; here it's full.
    assert "NSE-HDFCBANK-30Jan25-1690-CE" not in budget.by_symbol
    assert "NSE-RELIANCE-30Jan25-3000-CE" not in budget.by_symbol


def test_total_contracts_counts_only_considered_options() -> None:
    """MINOR fix: total_contracts excludes FUT legs + small-chain options (the honest ratio).

    A pull with a big NIFTY chain (considered) + a FUT (no strike, excluded) + a tiny 3-strike
    chain (left full, excluded) reports total_contracts = only the considered big-chain options.
    """
    big = _chain(40)  # 162 CE/PE options in one (NIFTY, _EXPIRY) chain — all considered
    fut = _future()  # no strike — excluded
    tiny = [_under_option("TINYCO", float(100 + i * 10)) for i in range(3)]  # < 7 — excluded
    budget = build_empty_retry_budget([*big, fut, *tiny], full_attempts=3, band=12, deep_attempts=1)
    assert budget.total_contracts == len(big)  # only the considered options, not 162+1+3


def test_empty_chain_is_empty_budget() -> None:
    budget = build_empty_retry_budget([], full_attempts=3, band=12, deep_attempts=1)
    assert budget.deep_contracts == 0
    assert budget.total_contracts == 0
    assert dict(budget.by_symbol) == {}


def test_resolves_from_env_when_band_and_deep_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """``band=None`` / ``deep_attempts=None`` route through the env resolvers."""
    monkeypatch.setenv("BRUTEX_FNO_STRIKE_BAND", "3")
    monkeypatch.setenv("BRUTEX_FNO_DEEP_EMPTY_RETRY", "2")
    contracts = _chain(40)
    budget = build_empty_retry_budget(contracts)  # band/deep default to None -> env
    assert budget.deep_attempts == 2
    # band 3 -> a rank-4 strike (23200) is deep
    assert budget.attempts_for("NSE-NIFTY-30Jan25-23200-CE") == 2


def test_attempts_for_unmapped_symbol_returns_full() -> None:
    budget = StrikeChainBudget(
        by_symbol={"X": 1}, full_attempts=3, deep_attempts=1, deep_contracts=1, total_contracts=2
    )
    assert budget.attempts_for("X") == 1
    assert budget.attempts_for("UNKNOWN") == 3


# ── property: the budget never starves a near-money strike + never inflates ───────────────


def test_property_every_mapped_attempt_within_one_to_full() -> None:
    """Property: every value in the sparse map is in [1, full] (never < 1, never > full)."""
    contracts = _chain(60)
    budget = build_empty_retry_budget(contracts, full_attempts=3, band=8, deep_attempts=1)
    for attempts in budget.by_symbol.values():
        assert 1 <= attempts <= budget.full_attempts


def test_property_center_strike_always_full() -> None:
    """Property: the ATM center strike is NEVER classified deep, for any band >= 0."""
    contracts = _chain(50)
    for band in (0, 1, 5, 12, 25):
        budget = build_empty_retry_budget(contracts, full_attempts=3, band=band, deep_attempts=1)
        assert budget.attempts_for("NSE-NIFTY-30Jan25-23000-CE") == 3
