"""Global fill vocabulary — WORST-CASE is the ONLY fill law (stdlib-only leaf).

DEC-FILL-WORST-CASE-ONLY-001 (operator directive 2026-07-15: "real live should
be completely reproducible; backtests never more optimistic than reality").
Every entry/exit across the spot/index engine AND the futures/options
executors fills at the bar's WORST price — there is no other mode, no env
knob, no CLI flag, no per-call parameter. The precise semantics are the
**adverse-max law** (never naive bar extremes):

* BUY fill on bar ``B`` = ``max(B.high, trigger_level)`` when a triggered
  SL/TP level exists, else ``B.high``.
* SELL fill on bar ``B`` = ``min(B.low, trigger_level)`` when a triggered
  SL/TP level exists, else ``B.low``.
* Open-anchored events (entry, forced 15:20, next-open exits, signal exits):
  the pure adverse extreme of the fill bar (no level).
* ``end_of_data`` / ``session_gap``: the adverse extreme of the last stored
  bar.

Why ``max``/``min`` with the level (two proven counterexamples): a long TP on
a gap-up bar entirely ABOVE the target (``low > tp``) must fill at
``min(tp, low) == tp`` — a naive sell-at-low would OVERSTATE vs an at-level
fill; and a flat futures/options bar (``high == open``) must carry the
``+tick`` on the extreme or the bare ``high < open + tick`` would be BETTER
than the retired precise fill.

This module is the ONE vocabulary home (CLAUDE.md §11 COMMON): a deliberately
stdlib-only leaf (``enum`` + ``os`` + ``Mapping``) so every consumer — the
identity layer, the sweep-verify refusal, the cascade manifest — imports the
SAME single-member enum without pulling any heavy dependency into its import
chain.

Identity law (Golden Rule #4): ``canonical_trade_params`` emits
``{"fill_mode": "worst_case"}`` UNCONDITIONALLY — removing the key would
alias worst-case run_ids with the frozen precise-era identities the retired
emit-on-non-PRECISE elision protected (the absorbed #1522 design).

Knob-refusal law (the fail-loud posture, DEC-SWEEP-LOG-VOLUME-GUARD-001
precedent): the historical ``$BRUTEX_FILL_MODE`` env knob is REMOVED, and a
launch that finds it SET refuses LOUDLY via
:func:`require_fill_mode_env_unset` — even ``worst_case`` refuses, because
the knob itself no longer exists and silently tolerating it would let a
stale ``precise`` deployment config read as "still honoured".
"""

from __future__ import annotations

import enum
import os
from collections.abc import Mapping
from typing import Final

__all__ = [
    "FILL_MODE_ENV",
    "FillMode",
    "FillModeRemovedError",
    "require_fill_mode_env_unset",
]


class FillMode(enum.Enum):
    """The typed fill-law token — a SINGLE member by design.

    Worst-case is the only fill law (DEC-FILL-WORST-CASE-ONLY-001): the enum
    survives purely as the typed vocabulary token the identity layer / the
    audit wire / the cascade manifest spell ``"worst_case"`` from — never a
    configuration surface. The retired ``PRECISE`` member (DEC-FILL-WORST-
    CASE-001's byte-identical legacy opt-out) is DELETED, not deprecated.
    """

    #: The adverse-max law (module docstring): every fill assumes the worst
    #: price the fill bar printed — the anti-illusion floor. The ONLY law.
    WORST_CASE = "worst_case"


#: The REMOVED operator env knob — retained ONLY so the launch-time refusal
#: guard (:func:`require_fill_mode_env_unset`) can name the stale variable it
#: refuses. Nothing resolves a mode from it any more.
FILL_MODE_ENV: Final[str] = "BRUTEX_FILL_MODE"


class FillModeRemovedError(ValueError):
    """A launch found the removed ``$BRUTEX_FILL_MODE`` knob still SET.

    DEC-FILL-WORST-CASE-ONLY-001: fill mode is no longer configurable —
    worst-case only. Raised (never warned) so a stale deployment config that
    still exports the variable — including a redundant ``worst_case`` —
    fails LOUD at launch instead of silently pretending the knob is honoured
    (fail-loud beats silent tolerance; the ``BRUTEX_LOG_LEVEL`` precedent).
    """


def require_fill_mode_env_unset(env: Mapping[str, str] | None = None) -> None:
    """Refuse LOUDLY when the removed ``$BRUTEX_FILL_MODE`` knob is set.

    Wired at sweep/executor LAUNCH (the CLI ``sweep-all`` launch resolution +
    the cascade manual replay — exactly where the retired resolver used to
    read the env), so the refusal fires before any compute.

    Parameters
    ----------
    env
        The environment mapping to read (defaults to ``os.environ`` — the
        injection seam keeps the guard pure for tests).

    Raises
    ------
    FillModeRemovedError
        When the variable is present with ANY non-empty value —
        ``worst_case`` included: the knob itself is removed
        (DEC-FILL-WORST-CASE-ONLY-001), and accepting the spelling that
        happens to match the one law would silently bless stale configs.
        An UNSET or empty-string variable passes (empty = the common
        ``VAR= cmd`` unset idiom; nothing was configured).
    """
    source = os.environ if env is None else env
    raw = source.get(FILL_MODE_ENV, "")
    if raw.strip():
        raise FillModeRemovedError(
            f"${FILL_MODE_ENV}={raw!r} is set, but fill mode is no longer "
            f"configurable — worst-case only (DEC-FILL-WORST-CASE-ONLY-001). "
            f"Unset the variable; the removed knob is refused for EVERY value, "
            f"including 'worst_case'."
        )
