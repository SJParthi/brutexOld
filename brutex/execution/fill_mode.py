"""Global fill-mode vocabulary — the worst-case anti-illusion floor (stdlib-only leaf).

DEC-FILL-WORST-CASE-001 (MIGRATION.md #1786). Every entry/exit across the
spot/index engine (and, in Stage 2, the futures/options executors) fills at
the bar's WORST price by DEFAULT, so the backtest can only UNDERSTATE — the
operator-approved anti-illusion floor. The precise semantics are the
**adverse-max law** (never naive bar extremes):

* BUY fill on bar ``B`` = ``max(B.high, trigger_level)`` when a triggered
  SL/TP level exists, else ``B.high``.
* SELL fill on bar ``B`` = ``min(B.low, trigger_level)`` when a triggered
  SL/TP level exists, else ``B.low``.
* Open-anchored events (entry, forced 15:20, next-open exits, signal exits):
  the pure adverse extreme of the fill bar (no level).
* ``end_of_data`` / ``session_gap``: the adverse extreme of the last stored
  bar (PRECISE uses its close).

Why ``max``/``min`` with the level (two proven counterexamples): a long TP on
a gap-up bar entirely ABOVE the target (``low > tp``) must fill at
``min(tp, low) == tp`` — a naive sell-at-low would OVERSTATE vs today's
at-level fill; and a flat futures/options bar (``high == open``) must carry
the ``+tick`` on the extreme (Stage 2) or the bare ``high < open + tick``
would be BETTER than today.

This module is the ONE vocabulary + env-resolution home (CLAUDE.md §11
COMMON): a deliberately stdlib-only leaf (``enum`` + ``os`` + ``Mapping``)
so every consumer — the engine walker, the Rust-kernel seam, the sweep
config/identity layer, the CLI — imports the SAME two-member enum without
pulling any heavy dependency into its import chain.

Identity law (Golden Rule #4): :data:`FillMode.PRECISE` is the LEGACY mode —
``canonical_trade_params`` emits NO ``fill_mode`` key for it, so every
historical run identity is byte-identical; :data:`FillMode.WORST_CASE`
(the production default, :data:`DEFAULT_FILL_MODE`) emits the key and
re-keys the run.
"""

from __future__ import annotations

import enum
import os
from collections.abc import Mapping
from typing import Final

__all__ = [
    "DEFAULT_FILL_MODE",
    "FILL_MODE_ENV",
    "FillMode",
    "resolve_fill_mode",
]


class FillMode(enum.Enum):
    """How a fill PRICE is chosen once an event's fill bar is decided.

    The mode moves ONLY the fill price — entry/exit TIMING (signal bar ``N``
    → fill bar ``N+1``), the inclusive SL/TP trigger comparisons, the
    SL-first same-bar tie, exit reasons, and the excursion fold are all
    UNCHANGED between modes. SL/TP LEVELS anchor on the ACTUAL entry fill,
    so under :data:`WORST_CASE` they anchor on the adverse-extreme entry —
    the honest simulation of the worst realized entry; trigger timing can
    therefore differ between modes (documented, accepted).
    """

    #: The adverse-max law (module docstring): every fill assumes the worst
    #: price the fill bar printed — the anti-illusion floor. The PRODUCTION
    #: default (:data:`DEFAULT_FILL_MODE`).
    WORST_CASE = "worst_case"
    #: The legacy exact-fill conventions (next-bar OPEN entry, at-LEVEL SL/TP,
    #: OPEN-anchored forced/next-open exits, CLOSE-anchored end-of-data /
    #: session-gap). The LIBRARY-level default on direct constructions
    #: (byte-identical legacy behaviour + run identities).
    PRECISE = "precise"


#: The PRODUCTION default (DEC-FILL-WORST-CASE-001): the worst-case floor.
#: Applied at the production CONFIG layer (``SweepRunConfig`` /
#: ``SweepGridConfig`` / the CLI launch) — the engine-library default on a
#: bare ``FillPolicy()`` stays :data:`FillMode.PRECISE` so direct
#: constructions are byte-identical legacy.
DEFAULT_FILL_MODE: Final[FillMode] = FillMode.WORST_CASE

#: The operator env knob :func:`resolve_fill_mode` reads (fail-loud on junk).
FILL_MODE_ENV: Final[str] = "BRUTEX_FILL_MODE"


def resolve_fill_mode(env: Mapping[str, str] | None = None) -> FillMode:
    """Resolve the launch fill mode from ``$BRUTEX_FILL_MODE`` (fail-loud).

    Parameters
    ----------
    env
        The environment mapping to read (defaults to ``os.environ`` — the
        injection seam keeps the resolver pure for tests).

    Returns
    -------
    FillMode
        ``worst_case`` / ``precise`` (case-insensitive, whitespace-trimmed);
        unset or blank → :data:`DEFAULT_FILL_MODE` (the worst-case floor).

    Raises
    ------
    ValueError
        On any other value — a junk env var must fail LOUD at launch
        (the ``BRUTEX_LOG_LEVEL`` fail-loud precedent,
        DEC-SWEEP-LOG-VOLUME-GUARD-001), never silently fall back to a mode
        the operator did not choose.
    """
    source = os.environ if env is None else env
    raw = source.get(FILL_MODE_ENV, "")
    token = raw.strip().lower()
    if not token:
        return DEFAULT_FILL_MODE
    try:
        return FillMode(token)
    except ValueError:
        valid = ", ".join(mode.value for mode in FillMode)
        raise ValueError(
            f"invalid ${FILL_MODE_ENV}={raw!r} — expected one of: {valid} "
            f"(case-insensitive), or unset for the default "
            f"({DEFAULT_FILL_MODE.value})"
        ) from None
