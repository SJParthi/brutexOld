"""Extreme-volatility must-survive stratum law (DEC-REGIME-MUST-SURVIVE-STRATUM-001).

The default-OFF cascade filter #10 law: a strategy must SURVIVE the current
regime's extreme-volatility stratum (sessions on/after :data:`STRATUM_START`,
2024-01-01 → today) — a candidate whose measured expectancy over that stratum
is negative, or whose stratum P&L variance explodes past
:data:`VARIANCE_RATIO_MAX` × its calm-stratum variance, is rejected; a
candidate with NO stratum evidence is an honest, VISIBLE abstain
(``"unproven_current_regime"`` / ``"insufficient_stratum_trades"``), never a
fabricated verdict and never a silent pass (Golden Rule #1).

The constants below are CONSERVATIVE defaults pending operator calibration —
they gate nothing until the ``CascadeConfig.extreme_vol_stratum`` flag is
flipped ON (its own DEC).

VIX arm — NAMED FOLLOW-UP, deliberately NOT consumed in v1: the cascade's
candidate re-simulations are UNSTAMPED trade streams by design
(DEC-VIX-STAMP-WIRE-001 round-6 — the cascade chain's stamper seam was
DELETED because every trade object is discarded at the CandidateInput scalar
conversion), so classifying sessions by the India-VIX level here would
require reviving that seam (add a VIX surface to CandidateInput FIRST — the
recorded revert lever). v1 therefore classifies extreme sessions by the
instrument's OWN daily close-to-close move — honest, self-contained, no
fabrication; :data:`VIX_EXTREME` is defined + documented as the follow-up
arm's threshold only.

Pure + stdlib-only: no lake I/O, no engine imports, deterministic, total on
degenerate input (never raises on empty/short series).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Final, Literal, cast

#: First session of the "current regime" extreme-vol stratum (inclusive).
STRATUM_START = date(2024, 1, 1)

#: A session is EXTREME when the instrument's own close-to-close daily move
#: is at least this many percent, in either direction (conservative default
#: pending operator calibration).
EXTREME_DAILY_MOVE_PCT = 2.0

#: The FOLLOW-UP VIX arm's session-classification threshold (India VIX level
#: at/above which a session would classify extreme). Defined + documented
#: only — NOT consumed in v1 (see the module docstring: the cascade's
#: re-simulated trades are unstamped by design, so a VIX classification
#: needs the named stamper-seam revival first).
VIX_EXTREME = 25.0

#: Minimum stratum trades for a statistically meaningful verdict; below it
#: the filter ABSTAINS (``"insufficient_stratum_trades"``), never rejects.
MIN_STRATUM_TRADES = 30

#: Reject when stratum P&L variance exceeds this multiple of the calm
#: stratum's variance (strict ``>`` — exactly at the ratio still passes).
VARIANCE_RATIO_MAX = 4.0

#: The verdict vocabulary (stable wire tokens).
StratumStatus = Literal[
    "ok",
    "fail",
    "unproven_current_regime",
    "insufficient_stratum_trades",
]

#: The honest-ABSTAIN members of :data:`StratumStatus` — recorded verdicts
#: that gate NOTHING (never a reject). Exported so read-time consumers (the
#: /results ``regime_robust`` preset — the DEC-REGIME-REJECT-TIER-001
#: proposal) derive their vocabulary from THIS producer module instead of
#: re-minting literals (CLAUDE.md §11 COMMON). ``"unproven_current_regime"``
#: is also minted by the cascade's no-input arm (``brutex.metrics.cascade``);
#: the token's vocabulary home is THIS Literal.
ABSTAIN_STATUSES: Final[tuple[str, ...]] = (
    "unproven_current_regime",
    "insufficient_stratum_trades",
)


def classify_extreme_sessions(daily_closes: Mapping[date, float]) -> frozenset[date]:
    """Return the extreme-vol sessions among *daily_closes* (pure, total).

    A session ``d`` on/after :data:`STRATUM_START` is EXTREME when
    ``|close[d] / close[prev] - 1| * 100 >= EXTREME_DAILY_MOVE_PCT``, where
    ``prev`` is the immediately preceding session KEY in date order (whatever
    calendar the caller's mapping carries — lake truth, never a fabricated
    calendar). The first session in the mapping has no predecessor, so it can
    never classify extreme; a zero/non-finite previous close abstains that
    session (no fabricated ratio). Empty / single-entry input → empty set.

    Parameters
    ----------
    daily_closes
        Per-session last close (rupees as float), keyed by IST session date.

    Returns
    -------
    frozenset[date]
        The extreme sessions (subset of the input keys, all
        ``>= STRATUM_START``).
    """
    ordered = sorted(daily_closes)
    extreme: set[date] = set()
    for prev_day, day in pairwise(ordered):
        if day < STRATUM_START:
            continue
        prev_close = daily_closes[prev_day]
        close = daily_closes[day]
        if not math.isfinite(prev_close) or not math.isfinite(close) or prev_close == 0.0:
            continue
        move_pct = abs(close / prev_close - 1.0) * 100.0
        if move_pct >= EXTREME_DAILY_MOVE_PCT:
            extreme.add(day)
    return frozenset(extreme)


@dataclass(frozen=True, slots=True)
class StratumVerdict:
    """The measured extreme-vol stratum verdict for one candidate.

    ``status`` is the decision token; the measured fields are recorded for
    EVERY status that could compute them (``None`` = not computable — an
    empty side), so an abstain is still fully visible evidence. ``detail``
    is a short human string carrying the measured numbers.
    """

    status: StratumStatus
    stratum_trades: int
    calm_trades: int
    stratum_expectancy_paisa: float | None
    calm_expectancy_paisa: float | None
    stratum_variance: float | None
    calm_variance: float | None
    variance_ratio: float | None
    detail: str


def _mean_and_pvariance(pnls: Sequence[int]) -> tuple[float | None, float | None]:
    """Mean + population variance of *pnls*, ``(None, None)`` when empty."""
    if not pnls:
        return None, None
    return float(statistics.fmean(pnls)), float(statistics.pvariance(pnls))


def evaluate_stratum(
    stratum_pnls_paisa: Sequence[int], calm_pnls_paisa: Sequence[int]
) -> StratumVerdict:
    """Evaluate the must-survive law over one candidate's split P&L series.

    Decision law (pure, deterministic, never raises on degenerate input):

    * 0 stratum trades → ``"unproven_current_regime"`` (honest abstain — no
      evidence in the current regime, never a fabricated pass/fail).
    * ``0 < n < MIN_STRATUM_TRADES`` → ``"insufficient_stratum_trades"``
      (abstain — the sample is too small to gate on).
    * Else FAIL iff stratum expectancy < 0, OR (both variances computable AND
      ``calm_variance > 0`` AND ``stratum_variance / calm_variance >
      VARIANCE_RATIO_MAX``). The variance arm ABSTAINS when the calm side is
      empty or its variance is zero (no denominator — the expectancy arm
      still applies).
    * Else ``"ok"``.

    Parameters
    ----------
    stratum_pnls_paisa
        Per-trade net P&L (paisa) of the trades ENTERED on extreme sessions.
    calm_pnls_paisa
        Per-trade net P&L (paisa) of every other trade.

    Returns
    -------
    StratumVerdict
        The measured verdict (status + the numbers that produced it).
    """
    stratum_exp, stratum_var = _mean_and_pvariance(stratum_pnls_paisa)
    calm_exp, calm_var = _mean_and_pvariance(calm_pnls_paisa)
    n_stratum = len(stratum_pnls_paisa)
    n_calm = len(calm_pnls_paisa)
    variance_ratio: float | None = None
    if stratum_var is not None and calm_var is not None and calm_var > 0.0:
        variance_ratio = stratum_var / calm_var

    def verdict(status: StratumStatus, detail: str) -> StratumVerdict:
        return StratumVerdict(
            status=status,
            stratum_trades=n_stratum,
            calm_trades=n_calm,
            stratum_expectancy_paisa=stratum_exp,
            calm_expectancy_paisa=calm_exp,
            stratum_variance=stratum_var,
            calm_variance=calm_var,
            variance_ratio=variance_ratio,
            detail=detail,
        )

    if n_stratum == 0:
        return verdict(
            "unproven_current_regime",
            "no trades in the extreme-vol stratum "
            f"(>= {STRATUM_START.isoformat()}) — unproven, not rejected",
        )
    if n_stratum < MIN_STRATUM_TRADES:
        return verdict(
            "insufficient_stratum_trades",
            f"only {n_stratum} stratum trade(s) < {MIN_STRATUM_TRADES} — abstain, not rejected",
        )
    # stratum_exp is non-None here (n_stratum >= MIN_STRATUM_TRADES >= 1) —
    # cast, never a runtime assert (ruff S101; the cascade_wire cast precedent).
    stratum_exp = cast(float, stratum_exp)
    if stratum_exp < 0.0:
        return verdict(
            "fail",
            f"stratum expectancy {stratum_exp:.2f} paisa < 0 over {n_stratum} trade(s)",
        )
    if variance_ratio is not None and variance_ratio > VARIANCE_RATIO_MAX:
        return verdict(
            "fail",
            f"stratum variance ratio {variance_ratio:.4f} > {VARIANCE_RATIO_MAX} "
            f"({n_stratum} stratum vs {n_calm} calm trade(s))",
        )
    return verdict(
        "ok",
        f"stratum expectancy {stratum_exp:.2f} paisa >= 0 over {n_stratum} trade(s)"
        + (
            f"; variance ratio {variance_ratio:.4f} <= {VARIANCE_RATIO_MAX}"
            if variance_ratio is not None
            else "; variance arm abstained (no calm denominator)"
        ),
    )


__all__ = [
    "ABSTAIN_STATUSES",
    "EXTREME_DAILY_MOVE_PCT",
    "MIN_STRATUM_TRADES",
    "STRATUM_START",
    "VARIANCE_RATIO_MAX",
    "VIX_EXTREME",
    "StratumStatus",
    "StratumVerdict",
    "classify_extreme_sessions",
    "evaluate_stratum",
]
