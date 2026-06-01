"""Synthetic rolling-option client — the dry-run options data source (offline).

The options analog of :class:`~brutex.adapters.dhan.synthetic_adapter.SyntheticDhanAdapter`
(DEC-VENDOR-PIVOT-001): a :class:`~brutex.adapters.dhan.option_ingest.JsonPoster`
whose ``post_json`` returns a valid synthetic per-side ``ce`` / ``pe`` columnar
rolling-option payload instead of hitting the network, so the whole
parse → session-filter → option-sink pipeline (and the fan-out over the strike
window × expiries × CE/PE) runs **offline** with no token and no network — the
dry-run proof the options pipe works before any credentials are set.

Pure + deterministic: timestamps start at **09:15 IST** (session open) and step
by one minute, so every synthetic bar survives the in-session filter
(CLAUDE.md §7). Both sides are populated so a dry-run exercises the CE and PE
transpose paths.
"""

from __future__ import annotations

from collections.abc import Mapping

#: 2021-01-04 03:45:00 UTC == 09:15 IST (session open) — the first synthetic
#: option bar's epoch; +60 s per bar keeps the window inside [09:15, 15:30) IST.
_SESSION_OPEN_EPOCH: int = 1609731900


def _synthetic_side(bars_per_window: int) -> dict[str, object]:
    """Build one side's columnar arrays — *bars_per_window* in-session 1-min bars."""
    timestamps = [_SESSION_OPEN_EPOCH + i * 60 for i in range(bars_per_window)]
    opens = [100.0 + i for i in range(bars_per_window)]
    return {
        "timestamp": timestamps,
        "open": opens,
        "high": [o + 1.0 for o in opens],
        "low": [o - 1.0 for o in opens],
        "close": [o + 0.5 for o in opens],
        "volume": [1000 + i for i in range(bars_per_window)],
        "oi": [5000 + i for i in range(bars_per_window)],
        "strike": [17500.0 for _ in range(bars_per_window)],
        "spot": [17480.5 for _ in range(bars_per_window)],
        "iv": [0.15 for _ in range(bars_per_window)],
    }


class SyntheticOptionClient:
    """A dry-run ``JsonPoster`` returning synthetic in-session rolling-option payloads.

    Drop-in for the live :class:`~brutex.adapters.dhan.client.DhanClient` on the
    options dry-run path: every ``post_json`` returns ``bars_per_window`` valid
    in-session bars on **both** the ``ce`` and ``pe`` sides, regardless of the
    request body — a deterministic offline stand-in (DEC-VENDOR-PIVOT-001).
    """

    def __init__(self, *, bars_per_window: int = 5) -> None:
        """Build the synthetic client.

        Parameters
        ----------
        bars_per_window
            Number of synthetic in-session bars emitted per side per call
            (must be > 0).

        Raises
        ------
        ValueError
            If *bars_per_window* is not positive.
        """
        if bars_per_window <= 0:
            raise ValueError(f"bars_per_window must be positive, got {bars_per_window}")
        self._n = bars_per_window

    def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
        """Return a synthetic ``{"data": {"ce": …, "pe": …}}`` rolling-option payload."""
        del endpoint, body  # synthetic: the output never depends on the request
        side = _synthetic_side(self._n)
        return {"data": {"ce": side, "pe": dict(side)}}


__all__ = ["SyntheticOptionClient"]
