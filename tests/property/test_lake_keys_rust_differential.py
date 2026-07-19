"""Hypothesis differential for the lake_keys Rust seam (routed ≡ oracle).

DEC-DATA-RUST-LAKE-KEYS-001 (T10 PR-2), the
``test_fno_symbol_rust_differential`` template: every property forces the
flag ON in the post-burn-in steady state (``reset(remaining=0)``, REAL
wheel — ``importorskip``-skipped in main CI, teeth in the data-rust-parity
lane) and asserts the routed public entries equal the ``_*_python`` verbatim
oracles under exact compare, NEVER raise, and never latch the seam OFF.

HONESTY NOTE (module contract): the non-ASCII arms prove the GATE —
identical behavior via the oracle route, not the Rust mirror. The
Rust-facing domain is the ASCII arm; both are load-bearing (the gate IS a
law of the seam).
"""

from __future__ import annotations

import pytest
from brutex.data import fno_symbol_rust as fsr
from brutex.data import lake_keys as lk
from brutex.data import lake_keys_rust as lkr
from brutex.data.lake_keys import (
    _spot_key_candidates_python,
    _spot_recovery_candidates_python,
    _stock_drift_fold_python,
    spot_key_candidates,
    spot_recovery_candidates,
    stock_drift_fold,
)
from brutex.universe import normalize_underlying_symbol
from hypothesis import given, settings
from hypothesis import strategies as st

# --------------------------------------------------------------------------
# Generators — token pools with adversarial shapes (design §6.2).
# --------------------------------------------------------------------------

_EXCHANGES = st.sampled_from(["NSE", "BSE", "", " NSE ", "nse", "-"])
_UNDERLYINGS = st.sampled_from(
    [
        "NIFTY",
        "NIFTYJR",
        "NIFTYNXT50",
        "MIDCPNIFTY",
        "BAJAJ-AUTO",
        "M&M",
        "M%26M",
        " M&M ",
        "NSE-M&M",  # drifted-SHAPED underlying
        "NSE-M&M\x1f",  # strip-set-wrapped
        "\x1bNSE-M&M",  # strip-set REJECT shape
        "",
        "  ",
        "X Y",
    ]
)
_ASCII_TEXT = st.text(alphabet=st.characters(min_codepoint=9, max_codepoint=126), max_size=48)
_FULL_TEXT = st.text(max_size=48)
_ENV_PAYLOADS = st.sampled_from(
    [
        None,  # unset — sourced pairs only
        '{"tcs": "TATACS"}',
        '{"foo": "bar"}',  # lowercase VALUE — the C4/C5 asymmetry domain
        '{" x ": " y "}',
        '{"": "z"}',  # blank key ignored
        "not json",
        "[1]",
        '{"a": 1}',
    ]
)
_BUCKETS = st.lists(st.text(max_size=24), max_size=4)


def _with_engaged_seam(
    monkey_env: str | None,
) -> pytest.MonkeyPatch:
    """Build a MonkeyPatch context with the seam forced ON (caller manages)."""
    mp = pytest.MonkeyPatch()
    if monkey_env is None:
        mp.delenv(lk.FNO_INDEX_ALIASES_ENV, raising=False)
    else:
        mp.setenv(lk.FNO_INDEX_ALIASES_ENV, monkey_env)
    mp.setenv(fsr.DATA_RUST_ENV, "1")
    return mp


def _assert_fold_parity(symbol: str) -> str:
    pytest.importorskip("brutex_data")
    mp = _with_engaged_seam(None)
    lkr.reset_lake_keys_rust_for_tests(remaining=0)  # steady state
    try:
        routed = stock_drift_fold(symbol)
    finally:
        latched = lkr.lake_keys_rust_disabled()
        lkr.reset_lake_keys_rust_for_tests()
        mp.undo()
    oracle = _stock_drift_fold_python(symbol)
    assert routed == oracle, f"fold divergence on {symbol!r}: {routed!r} != {oracle!r}"
    assert not latched, f"seam latched OFF on fold({symbol!r})"
    return routed


def _assert_candidates_parity(symbol: str, exchange: str) -> tuple[str, ...]:
    pytest.importorskip("brutex_data")
    mp = _with_engaged_seam(None)
    lkr.reset_lake_keys_rust_for_tests(remaining=0)
    try:
        routed = spot_key_candidates(symbol, exchange)
    finally:
        latched = lkr.lake_keys_rust_disabled()
        lkr.reset_lake_keys_rust_for_tests()
        mp.undo()
    oracle = _spot_key_candidates_python(symbol, exchange)
    assert routed == oracle, (
        f"candidates divergence on ({symbol!r}, {exchange!r}): {routed!r} != {oracle!r}"
    )
    assert not latched, f"seam latched OFF on candidates({symbol!r}, {exchange!r})"
    return routed


def _assert_recovery_parity(
    underlying: str,
    exchange: str,
    env: str | None,
    bucket_entries: list[str],
) -> tuple[str, ...]:
    pytest.importorskip("brutex_data")
    fold = _stock_drift_fold_python(underlying)
    drift: dict[tuple[str, str], tuple[str, ...]] = (
        {(exchange, fold): tuple(bucket_entries)} if fold else {}
    )
    mp = _with_engaged_seam(env)
    lkr.reset_lake_keys_rust_for_tests(remaining=0)
    try:
        routed = spot_recovery_candidates(underlying, exchange, stock_drift=drift)
        pairs = lk.fno_index_alias_pairs()
    finally:
        latched = lkr.lake_keys_rust_disabled()
        lkr.reset_lake_keys_rust_for_tests()
        mp.undo()
    bucket = tuple(drift.get((exchange, fold), ())) if fold else ()
    oracle = _spot_recovery_candidates_python(
        underlying, exchange, alias_pairs=pairs, drift_bucket=bucket
    )
    assert routed == oracle, (
        f"recovery divergence on ({underlying!r}, {exchange!r}, env={env!r}, "
        f"bucket={bucket!r}): {routed!r} != {oracle!r}"
    )
    assert not latched, f"seam latched OFF on recovery({underlying!r}, {exchange!r})"
    return routed


class TestFoldDifferential:
    """P1/P2 on law A — pooled + raw ASCII + the non-ASCII GATE arm."""

    @settings(max_examples=200, deadline=None)
    @given(symbol=_ASCII_TEXT)
    def test_ascii_domain(self, symbol: str) -> None:
        _assert_fold_parity(symbol)

    @settings(max_examples=100, deadline=None)
    @given(symbol=_FULL_TEXT)
    def test_unicode_blobs_prove_the_gate(self, symbol: str) -> None:
        # Non-ASCII inputs route to the oracle (the ASCII pre-gate) — this
        # arm proves the GATE, not the Rust mirror (module docstring).
        _assert_fold_parity(symbol)

    @settings(max_examples=100, deadline=None)
    @given(symbol=_FULL_TEXT)
    def test_fold_mirror_pin_on_arm(self, symbol: str) -> None:
        # P4: the routed (flag-ON) fold stays the exact mirror of
        # universe.normalize_underlying_symbol — the OFF-arm mirror
        # property in test_lake_keys.py stays untouched.
        routed = _assert_fold_parity(symbol)
        assert routed == normalize_underlying_symbol(symbol)


class TestCandidatesDifferential:
    """P1/P2/P5 on law B."""

    @settings(max_examples=200, deadline=None)
    @given(symbol=st.one_of(_UNDERLYINGS, _ASCII_TEXT), exchange=_EXCHANGES)
    def test_ascii_pools(self, symbol: str, exchange: str) -> None:
        routed = _assert_candidates_parity(symbol, exchange)
        # P5: ≤2 candidates, identity excluded — both facts on the ROUTED
        # value (== the oracle by P1, so both arms are covered).
        assert len(routed) <= 2
        assert symbol not in routed

    @settings(max_examples=100, deadline=None)
    @given(symbol=_FULL_TEXT, exchange=st.one_of(_EXCHANGES, _FULL_TEXT))
    def test_unicode_blobs_prove_the_gate(self, symbol: str, exchange: str) -> None:
        _assert_candidates_parity(symbol, exchange)


class TestRecoveryDifferential:
    """P1/P2/P3 on law C — env alias pairs + generated buckets."""

    @settings(max_examples=200, deadline=None)
    @given(
        underlying=st.one_of(_UNDERLYINGS, _ASCII_TEXT),
        exchange=_EXCHANGES,
        env=_ENV_PAYLOADS,
        bucket=_BUCKETS,
    )
    def test_ascii_pools(
        self, underlying: str, exchange: str, env: str | None, bucket: list[str]
    ) -> None:
        routed = _assert_recovery_parity(underlying, exchange, env, bucket)
        assert underlying not in routed  # identity excluded, both arms via P1

    @settings(max_examples=100, deadline=None)
    @given(underlying=_FULL_TEXT, exchange=_EXCHANGES, bucket=_BUCKETS)
    def test_unicode_blobs_prove_the_gate(
        self, underlying: str, exchange: str, bucket: list[str]
    ) -> None:
        _assert_recovery_parity(underlying, exchange, None, bucket)

    @settings(max_examples=100, deadline=None)
    @given(
        underlying=st.one_of(_UNDERLYINGS, _ASCII_TEXT),
        exchange=_EXCHANGES,
        env=_ENV_PAYLOADS,
        bucket=_BUCKETS,
    )
    def test_prefix_invariant_both_arms(
        self, underlying: str, exchange: str, env: str | None, bucket: list[str]
    ) -> None:
        # P3: the with-bucket ladder always startswith the no-bucket ladder,
        # on the flag-ON arm AND the flag-OFF arm.
        pytest.importorskip("brutex_data")
        fold = _stock_drift_fold_python(underlying)
        drift: dict[tuple[str, str], tuple[str, ...]] = (
            {(exchange, fold): tuple(bucket)} if fold else {}
        )
        for flag_on in (True, False):
            mp = _with_engaged_seam(env)
            if not flag_on:
                mp.delenv(fsr.DATA_RUST_ENV, raising=False)
            lkr.reset_lake_keys_rust_for_tests(remaining=0)
            try:
                without = spot_recovery_candidates(underlying, exchange)
                with_bucket = spot_recovery_candidates(underlying, exchange, stock_drift=drift)
            finally:
                lkr.reset_lake_keys_rust_for_tests()
                mp.undo()
            assert with_bucket[: len(without)] == without, (
                f"prefix invariant broken (flag_on={flag_on}) on "
                f"({underlying!r}, {exchange!r}): {without!r} !≺ {with_bucket!r}"
            )
