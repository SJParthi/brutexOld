"""Tests for the pure Dhan token lifecycle math (DEC-DHAN-AUTH-001)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from brutex.adapters.dhan.constants import DHAN_TOKEN_LIFETIME_S, DHAN_TOKEN_REFRESH_AGE_S
from brutex.adapters.dhan.token_lifecycle import (
    ensure_budget_verdict,
    token_age_s,
    token_usable,
)
from brutex.adapters.dhan.token_store_aws import DhanTokenEnvelope

_ANCHOR = datetime(2026, 7, 12, 6, 0, 0, tzinfo=UTC)


def _env(minted: datetime) -> DhanTokenEnvelope:
    return DhanTokenEnvelope(token="FAKE-TOKEN", minted_at_utc=minted, source="mint")


def test_age() -> None:
    env = _env(_ANCHOR)
    assert token_age_s(env, _ANCHOR + timedelta(seconds=120)) == 120.0


def test_usable_fresh() -> None:
    env = _env(_ANCHOR)
    assert token_usable(env, _ANCHOR + timedelta(seconds=1)) is True


def test_usable_at_refresh_boundary_is_false() -> None:
    """Exactly at the refresh age the token is NO LONGER usable (strict `<`; kills `<=`)."""
    env = _env(_ANCHOR)
    at_boundary = _ANCHOR + timedelta(seconds=DHAN_TOKEN_REFRESH_AGE_S)
    assert token_usable(env, at_boundary) is False


def test_usable_just_before_boundary_true() -> None:
    env = _env(_ANCHOR)
    just_before = _ANCHOR + timedelta(seconds=DHAN_TOKEN_REFRESH_AGE_S - 1)
    assert token_usable(env, just_before) is True


def test_usable_at_zero_age_true() -> None:
    env = _env(_ANCHOR)
    assert token_usable(env, _ANCHOR) is True


def test_negative_age_unusable() -> None:
    """A future-stamped (corrupt/rolled-back) token is UNUSABLE regardless of the window."""
    env = _env(_ANCHOR)
    assert token_usable(env, _ANCHOR - timedelta(seconds=1)) is False


class TestBudgetVerdict:
    def test_ok(self) -> None:
        env = _env(_ANCHOR)
        v = ensure_budget_verdict(env, _ANCHOR + timedelta(seconds=100), needed_s=3_600)
        assert v.ok is True
        assert v.reason == "ok"
        assert v.remaining_s == DHAN_TOKEN_LIFETIME_S - 100

    def test_insufficient(self) -> None:
        env = _env(_ANCHOR)
        now = _ANCHOR + timedelta(seconds=DHAN_TOKEN_LIFETIME_S - 100)
        v = ensure_budget_verdict(env, now, needed_s=3_600)
        assert v.ok is False
        assert v.reason == "insufficient"

    def test_exact_boundary_ok(self) -> None:
        env = _env(_ANCHOR)
        now = _ANCHOR + timedelta(seconds=DHAN_TOKEN_LIFETIME_S - 3_600)
        v = ensure_budget_verdict(env, now, needed_s=3_600)
        assert v.ok is True  # remaining == needed (>= boundary)

    def test_corrupt_negative_age(self) -> None:
        env = _env(_ANCHOR)
        v = ensure_budget_verdict(env, _ANCHOR - timedelta(seconds=5), needed_s=0)
        assert v.ok is False
        assert v.reason == "corrupt_negative_age"
