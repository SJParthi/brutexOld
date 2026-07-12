"""DhanHQ v2 token lifecycle — pure, injectable-clock age / usability / budget math.

DEC-DHAN-AUTH-001. All timing is computed from OUR stored mint instant
(:attr:`~brutex.adapters.dhan.token_store_aws.DhanTokenEnvelope.minted_at_utc`), NEVER a JWT
claim (claims UNVERIFIED). No I/O — every function takes an explicit ``now`` so the reauth
spine + PR-3's pre-pull gate drive it deterministically. A NEGATIVE age (a token minted "in
the future" — a corrupt envelope or a clock rollback) reads as UNUSABLE (never trusted).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from brutex.adapters.dhan.constants import DHAN_TOKEN_LIFETIME_S, DHAN_TOKEN_REFRESH_AGE_S
from brutex.adapters.dhan.token_store_aws import DhanTokenEnvelope


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """Whether a token has enough life left for a needed window (PR-3's pre-pull gate).

    Attributes
    ----------
    ok
        ``True`` iff the token is non-negative-age AND has at least the needed seconds of
        remaining lifetime.
    remaining_s
        Seconds of lifetime left (``DHAN_TOKEN_LIFETIME_S - age``; may be negative = expired).
    reason
        A short machine reason: ``"ok"`` / ``"corrupt_negative_age"`` / ``"insufficient"``.
    """

    ok: bool
    remaining_s: float
    reason: str


def token_age_s(envelope: DhanTokenEnvelope, now: datetime) -> float:
    """Return the token age in seconds (``now - minted_at_utc``). May be negative. O(1)."""
    return (now - envelope.minted_at_utc).total_seconds()


def token_usable(envelope: DhanTokenEnvelope, now: datetime) -> bool:
    """True iff the token is fresh enough to use: ``0 <= age < DHAN_TOKEN_REFRESH_AGE_S``.

    A negative age (future-stamped / corrupt / clock rollback) is UNUSABLE. At exactly the
    refresh age the token is NO LONGER usable (strict ``<`` — the boundary re-mints), so a
    token on the 20h boundary is refreshed, not used. O(1).
    """
    age = token_age_s(envelope, now)
    return 0.0 <= age < DHAN_TOKEN_REFRESH_AGE_S


def ensure_budget_verdict(
    envelope: DhanTokenEnvelope, now: datetime, needed_s: float
) -> BudgetVerdict:
    """Verdict: does the token have at least *needed_s* of remaining lifetime? (PR-3 gate).

    ``remaining_s = DHAN_TOKEN_LIFETIME_S - age``. A negative age is a corrupt/rolled-back
    clock ⇒ ``ok=False`` (``"corrupt_negative_age"``) regardless of *needed_s*. Otherwise
    ``ok`` iff ``remaining_s >= needed_s`` (``"ok"`` / ``"insufficient"``). O(1).
    """
    age = token_age_s(envelope, now)
    remaining = float(DHAN_TOKEN_LIFETIME_S) - age
    if age < 0.0:
        return BudgetVerdict(ok=False, remaining_s=remaining, reason="corrupt_negative_age")
    if remaining >= needed_s:
        return BudgetVerdict(ok=True, remaining_s=remaining, reason="ok")
    return BudgetVerdict(ok=False, remaining_s=remaining, reason="insufficient")


__all__ = ["BudgetVerdict", "ensure_budget_verdict", "token_age_s", "token_usable"]
