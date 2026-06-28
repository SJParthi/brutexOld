"""Honest wall-clock ETA for a historical pull (GAP C5).

A pull's wall time is dominated by the number of ``(contract, interval)`` fetch
WINDOWS divided by the ACHIEVED request rate. This module is the ONE pure,
honest estimate of that duration — no fake O(1), no "minutes" magic:

    seconds = total_windows / max(achieved_rps, EPSILON_RPS)

Golden Rule #10 honesty:

* ``achieved_rps`` is a MEASURED input (e.g. from the live rate-controller
  snapshot / ``/metrics``), NOT a constant baked here — the caller passes what
  the pull is actually achieving.
* the rate is floored at :data:`EPSILON_RPS` so a stalled (0 r/s) pull yields a
  FINITE (large) estimate rather than ``inf`` / a ``ZeroDivisionError``.
* the estimate ignores write-back contention, vendor 429 back-off, and reauth
  pauses, so the REAL pull is generally SLOWER — see ``docs/runbooks/PULL_ETA.md``
  for the MEASURED-vs-PROJECTED worked numbers + the M4 Pro hardware envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Floor on the achieved rate so a stalled pull never divides by zero (r/s).
EPSILON_RPS: Final[float] = 1e-3


@dataclass(frozen=True, slots=True)
class PullEta:
    """An honest wall-clock estimate for a pull.

    Attributes
    ----------
    total_windows
        The number of fetch windows remaining (clamped ``>= 0``).
    achieved_rps
        The MEASURED achieved request rate used for the estimate (the input
        rate, before the :data:`EPSILON_RPS` floor — so callers can see what
        they passed).
    seconds
        The estimated wall-clock duration in seconds.
    """

    total_windows: int
    achieved_rps: float
    seconds: float

    @property
    def hours(self) -> float:
        """The estimated duration in hours (``seconds / 3600``)."""
        return self.seconds / 3600.0


def estimate_pull_eta(total_windows: int, achieved_rps: float) -> PullEta:
    """Estimate a pull's wall-clock duration — ``windows / max(rps, epsilon)``.

    Parameters
    ----------
    total_windows
        The number of fetch windows remaining. A negative value is clamped to 0.
    achieved_rps
        The MEASURED achieved request rate (requests/second). A non-positive
        value is floored at :data:`EPSILON_RPS` so the result is always finite.

    Returns
    -------
    PullEta
        The honest estimate. ``seconds == 0`` when there are no windows.
    """
    windows = max(total_windows, 0)
    rate = achieved_rps if achieved_rps > EPSILON_RPS else EPSILON_RPS
    return PullEta(total_windows=windows, achieved_rps=achieved_rps, seconds=windows / rate)


def format_eta(eta: PullEta) -> str:
    """Render *eta* as a compact human string (``Xh Ym`` / ``Xm Ys`` / ``Xs``).

    Parameters
    ----------
    eta
        The estimate to render.

    Returns
    -------
    str
        ``"<h>h <m>m"`` for an hour or more, ``"<m>m <s>s"`` for a minute or
        more, else ``"<s>s"``.
    """
    total = int(eta.seconds)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


__all__ = ["EPSILON_RPS", "PullEta", "estimate_pull_eta", "format_eta"]
