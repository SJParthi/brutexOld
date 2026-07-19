"""Dashboard sweep launcher — turn the one-click "🚀 Run Sweep" button into a grid sweep.

The glue between the dashboard's **🚀 Run Sweep** button and the already-built,
already-tested brute-force engine (``brutex sweep-all`` /
:func:`brutex.engine.sweep_orchestrator.run_sweep_grid`). The button POSTs a
handful of form fields; :func:`resolve_sweep_request` validates + normalises them
into a frozen :class:`SweepRunRequest` (400-able on bad input, **ZERO I/O** — a
pure function, fully unit-testable), and :func:`build_sweep_kwargs` maps that
request onto the exact ``cmd_sweep_all`` keyword contract the CLI already uses
(DEC-SWEEP-ORCHESTRATOR-001). The route layer (``app.py``) runs the sweep on the
single-flight background job and points ``state.sweep_progress_path`` at the file
the sweep publishes (:func:`sweep_progress_file`), so the EXISTING
``GET /api/v1/sweep-progress`` serves the live per-tuple tick with no new progress
plumbing (DEC-SWEEP-RUN-BUTTON-001).

## R5 institutional flip (gap-hunt BLOCKER #1, ruling 1)

``build_sweep_kwargs`` used to pin the LIGHT research defaults
(``overfit_gate=False``, ``stage3_enable=False``, ``top=20``,
``allow_missing_slices=True``) — so the "one-click institutional sweep"
button actually launched a gate-less, un-refined, top-20, hole-skipping
research sweep while the page claimed otherwise. It now pins the
INSTITUTIONAL defaults: 5 hard gates → composite ranking → Stage-3 CMA-ES
refinement ON, top-1000 kept per tuple, missing slices REFUSE the whole
launch (ruling O-a, refuse-whole) rather than silently skip. ``k`` defaults
to ``1,2,3`` (O-f). SL/TP legs default to a 3×3 on-grid paisa grid (O-g) —
see :data:`DEFAULT_SL`/:data:`DEFAULT_TP` for why the ``NONE`` leg (no
stop-loss) is NOT part of the institutional DEFAULT: ``stage3_enable=True``
makes :class:`~brutex.engine.sweep_orchestrator.SweepGridConfig` reject any
``None`` SL/TP value at construction time (every base-grid value must
normalize onto the Mode-A grid so CMA-ES can seed its ``x0`` from it, ADR-0007
condition 3). Per the OPERATOR RULING (2026-07-08, R54 — both SL/TP arms
AND no-SL/TP arms must be coverable) a ``none`` cell is nevertheless
first-class RUNNABLE — as a WHOLE leg (``sl="none"``) AND as a value
inside a mixed CSV leg (``sl="none,5,100,200"``, the only single-sweep
shape that covers BOTH arms in one run, and the CLI
``_parse_paisa_list`` grammar's own shape): :func:`leg_values` /
:func:`leg_has_none` are the ONE parsed-leg source of truth, and any
``none`` cell downgrades the launch's EFFECTIVE ``stage3_enable`` to
``False`` (:attr:`SweepRunRequest.stage3_enable`, the CLI's own default
posture for ``none`` legs; with Stage-3 off the engine accepts ``None``
cells AND any positive paisa value, so the Mode-A axis check stands
down too) — never a hard refusal, never a deep engine crash. The
downgrade is DISCLOSED loudly on every operator surface: the request
echo / ``POST /api/v1/sweep/run`` payload carries ``stage3_enable``
(:meth:`SweepRunRequest.to_dict`), the one-click preflight verdict
carries a ``stage3_enable`` :class:`~brutex.engine.sweep_oneclick_preflight.Clamp`
row per downgraded exchange group (:func:`oneclick_preflight_for_plan`
— the operator proceed-dialog's data source), and
:func:`build_sweep_kwargs` emits ONE
``sweep_runner.none_leg_stage3_downgrade`` WARN per built engine-run
kwargs at launch time (the read-only preflight builds no kwargs and
emits no WARN). HONEST: a mixed leg's GRIDDED cells lose Stage-3
CMA-ES refinement for that launch too — stated in the clamp reason,
never silent. Off-Mode-A-grid paisa values stay refused (the round-11
deep-crash class) ONLY under the effective ``stage3_enable=True``
posture — a ``none``-carrying launch has disabled the sole engine
constraint that refusal protects.

## SLA-overrun acknowledge (gap-hunt BLOCKER #2, ruling 2)

:func:`~brutex.engine.sweep_sla_guard.check_sweep_sla` already accepts an
``acknowledge`` flag but nothing on the dashboard path could set it — an
institutional grid past the §8 4-day envelope refused invisibly inside the
background job thread. :attr:`SweepRunRequest.acknowledge_sla_overrun` now
threads an explicit ``acknowledge_sla_overrun=True`` (set only after the
operator confirms the R20 proceed-dialog) down to ``cmd_sweep_all``'s
existing parameter of the same name — zero new SLA logic, pure wiring.

## Multi-symbol / SENSEX fan-out (gap-hunt MAJOR #9, ruling 9)

SENSEX is BSE-listed while NIFTY/BANKNIFTY are NSE-listed, but one
:class:`SweepRunRequest` carries exactly ONE ``exchange``. R2's "blank ⇒ all
3 (NIFTY-first)" default therefore needs a FAN-OUT: :func:`group_symbols_by_exchange`
+ :func:`resolve_oneclick_sweep_plan` build ONE :class:`SweepRunRequest` per
exchange group, sharing ONE logical ``sweep_run_id`` (:class:`OneClickSweepPlan`).
Run semantics beyond "one logical id wraps N engine runs, a refusal in any
group refuses the whole logical run" are an OPEN operator decision (ruling
9) — this module DEFAULTS to flag-and-run with an explicit note, never
silently drops a group.

Honest envelope (Golden Rule #10): this module is O(1)-per-field validation/mapping
only (the fan-out is O(#exchange-groups), at most 2 today). The sweep it
launches is O(T·N) over the grid — NOT O(1) total; the live progress file
reports the honest tuple/combo counts as they run.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.universe import ENGINE_SWEEP_SPOT_TRIPLES
from brutex.vocabulary.bits import TOTAL_CONDITIONS

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_LOG = structlog.get_logger(__name__)

#: Default LAKE-partition index symbols (bare trading symbols, NOT the
#: ``EXCH-SYMBOL`` groww keys) — mirrors the CLI ``--symbols`` default in
#: ``brutex.cli._add_sweep_grid_args``. This is :func:`resolve_sweep_request`'s
#: OWN single-exchange default (NSE) — the cross-exchange "all 3" default
#: (R2) lives in :data:`DEFAULT_ONECLICK_SYMBOLS`, consumed only by the
#: fan-out entry point :func:`resolve_oneclick_sweep_plan`, since a bare
#: single-exchange call cannot sanely default to a symbol outside its one
#: ``exchange``.
DEFAULT_SYMBOLS: Final[str] = "NIFTY,BANKNIFTY"
#: R2's "blank ⇒ all 3 (NIFTY-first)" default for the ONE-CLICK fan-out entry
#: point only (gap-hunt finding #9 — the old bare default silently dropped
#: SENSEX, 2 of 3).
DEFAULT_ONECLICK_SYMBOLS: Final[str] = "NIFTY,BANKNIFTY,SENSEX"
#: Default lake interval set to sweep (mirrors the CLI ``--intervals`` default).
DEFAULT_INTERVALS: Final[str] = "1minute,5minute,15minute"
#: Default trade directions (both legs of a directional edge).
DEFAULT_DIRECTIONS: Final[str] = "BUY,SELL"
#: Default conditions-per-combo K values (mirrors the CLI ``--k`` default —
#: since DEC-SWEEP-APRIORI-DEFAULT-001, MIGRATION.md #1752, the
#: Apriori-to-EXTINCTION token: per cell the k-ladder runs k=1,2,3,… until no
#: combo survives min-hits. The ONE sanctioned second copy of the CLI token
#: (`brutex.cli.EXTINCTION_K_TOKEN` — the dashboard must not import the CLI at
#: module top), drift-PINNED against the CLI argparse ``--k`` default by test.
#: A numeric K stays the classic fixed-K sweep, byte-identical.
DEFAULT_K: Final[str] = "extinction"
#: Default stop-loss / take-profit legs — a 3×3 on-grid paisa grid (O-g),
#: chosen to satisfy the Mode-A CMA-ES refinement axes
#: (:data:`brutex.engine.stage3_grid.MODE_A_SL_AXIS` = ``[5, 200]`` paisa
#: step 5; :data:`~brutex.engine.stage3_grid.MODE_A_TP_AXIS` = ``[10, 500]``
#: paisa step 10) that ``stage3_enable=True`` (R5) REQUIRES every SL/TP value
#: to normalize onto — see the module docstring for why the ``NONE`` leg is
#: therefore not part of this default.
DEFAULT_SL: Final[str] = "50,100,150"
DEFAULT_TP: Final[str] = "100,200,300"
#: Default top-N composite-ranked combos persisted per tuple (R21).
DEFAULT_TOP: Final[int] = 1000
#: Default run-mode identity label stamped on the run hash — distinguishes
#: an institutional launch from the retired light "A"-moded research runs.
DEFAULT_MODE: Final[str] = "INSTITUTIONAL"
#: Default lake exchange / segment partition for the index sweep.
DEFAULT_EXCHANGE: Final[str] = "NSE"
DEFAULT_SEGMENT: Final[str] = "INDEX"
#: Default Track-1 history start (the rolling ``2020-01-01 → today`` range —
#: GROWW_HISTORY_FLOOR, DEC-PULL-FLOOR-2020-001).
DEFAULT_START: Final[str] = "2020-01-01"

#: Accepted direction tokens (upper-cased before the check).
_DIRECTIONS: Final[frozenset[str]] = frozenset({"BUY", "SELL"})
#: Max conditions-per-combo K — the engine bounds ``k`` by the unique condition
#: count, which is the full 60-condition vocabulary by default (SweepGridConfig
#: docstring; ``TOTAL_CONDITIONS`` is the one source of truth, so a future
#: vocab growth moves this bound automatically — DEC-VOCAB-U128-CODEGEN-001).
_MAX_K: Final[int] = TOTAL_CONDITIONS

#: ``symbol -> canonical exchange`` for the 3 brute-force spot indexes,
#: derived from the ONE canonical
#: :data:`brutex.universe.ENGINE_SWEEP_SPOT_TRIPLES` source (never a
#: hand-typed copy) — the fan-out's symbol-to-exchange resolver
#: (:func:`group_symbols_by_exchange`).
_SPOT_SYMBOL_EXCHANGE: Final[dict[str, str]] = {
    symbol: exchange for exchange, _segment, symbol in ENGINE_SWEEP_SPOT_TRIPLES
}


@dataclass(frozen=True, slots=True)
class SweepRunRequest:
    """A validated, normalised sweep launch request (the pure form → engine bridge).

    Every field is already validated + normalised by :func:`resolve_sweep_request`
    into the exact CSV / scalar shape ``cmd_sweep_all`` expects, so
    :func:`build_sweep_kwargs` is a trivial 1:1 mapping (no re-validation, no I/O).

    Attributes
    ----------
    symbols, intervals, directions, k, sl, tp
        Normalised comma-separated axis strings (deduped of blank tokens).
    exchange, segment
        Lake partition coordinates the symbols live under.
    start, end
        Inclusive ``YYYY-MM-DD`` window bounds (``end >= start``, both real dates).
    mode
        Run-mode identity label stamped on the reproducibility hash.
    top
        Top-N composite-ranked combos persisted per tuple (``>= 1``).
    acknowledge_sla_overrun
        ``True`` iff the operator confirmed the R20 proceed-dialog for a
        selection whose projected wall-clock exceeds the CLAUDE.md §8
        envelope — threaded verbatim into ``cmd_sweep_all``'s existing
        ``acknowledge_sla_overrun`` parameter (gap-hunt ruling 2). Defaults
        ``False`` — an un-acknowledged overrun still REFUSES, never a silent
        proceed.
    """

    symbols: str
    intervals: str
    directions: str
    k: str
    sl: str
    tp: str
    exchange: str
    segment: str
    start: str
    end: str
    mode: str
    top: int
    acknowledge_sla_overrun: bool = False

    @property
    def stage3_enable(self) -> bool:
        """The launch's EFFECTIVE Stage-3 posture, derived from the PARSED legs.

        ``True`` (the R5 institutional default) iff NEITHER leg carries a
        ``none`` cell; any ``none`` cell — a whole-``"none"`` leg or a
        ``none`` value inside a mixed CSV leg — downgrades the launch to
        ``False`` (operator ruling 2026-07-08, R54: CMA-ES cannot seed its
        ``x0`` from a ``none`` cell, ADR-0007 condition 3; with Stage-3 off
        the engine accepts ``None`` cells, so the launch stays provably
        runnable — R16 preserved). Derived through the ONE parsed-leg
        source (:func:`leg_has_none`) so it can NEVER drift from what
        ``cmd_sweep_all``'s ``_parse_paisa_list`` hands the engine — never
        an independent whole-string comparison.
        """
        return not (leg_has_none(self.sl) or leg_has_none(self.tp))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe echo of the resolved request (for the route response).

        Carries the derived :attr:`stage3_enable` so a ``none``-leg launch's
        Stage-3 downgrade is visible in the ``POST /api/v1/sweep/run``
        response payload — an operator-facing disclosure, never a
        server-log-only fact (R54 review round 2).
        """
        return {
            "symbols": self.symbols,
            "intervals": self.intervals,
            "directions": self.directions,
            "k": self.k,
            "sl": self.sl,
            "tp": self.tp,
            "exchange": self.exchange,
            "segment": self.segment,
            "start": self.start,
            "end": self.end,
            "mode": self.mode,
            "top": self.top,
            "acknowledge_sla_overrun": self.acknowledge_sla_overrun,
            "stage3_enable": self.stage3_enable,
        }


def _clean_csv(value: str | None, default: str) -> list[str]:
    """Split *value* on commas, strip + drop blanks; fall back to *default* when empty."""
    source = value if value is not None else default
    tokens = [tok.strip() for tok in source.split(",") if tok.strip()]
    if not tokens:
        tokens = [tok.strip() for tok in default.split(",") if tok.strip()]
    return tokens


def _normalise_symbols(value: str | None) -> str:
    """Normalise the symbols axis (non-empty tokens, original case preserved)."""
    return ",".join(_clean_csv(value, DEFAULT_SYMBOLS))


def _normalise_intervals(value: str | None) -> str:
    """Validate every interval label against the engine's known set; return the CSV."""
    from brutex.feed.resampler import INTERVAL_SECONDS

    tokens = _clean_csv(value, DEFAULT_INTERVALS)
    for token in tokens:
        if token not in INTERVAL_SECONDS:
            raise ValueError(f"interval must be one of {sorted(INTERVAL_SECONDS)} — got {token!r}")
    return ",".join(tokens)


def _normalise_directions(value: str | None) -> str:
    """Validate every direction is BUY/SELL (upper-cased); return the normalised CSV."""
    tokens = [tok.upper() for tok in _clean_csv(value, DEFAULT_DIRECTIONS)]
    for token in tokens:
        if token not in _DIRECTIONS:
            raise ValueError(f"direction must be one of ['BUY', 'SELL'] — got {token!r}")
    return ",".join(tokens)


def _normalise_k(value: str | None) -> str:
    """Validate K: the ``extinction`` token OR ints in ``[1, TOTAL_CONDITIONS]``.

    DEC-SWEEP-APRIORI-DEFAULT-001: a blank field falls to :data:`DEFAULT_K`
    (the extinction token — the CLI default), which flows verbatim through the
    1:1 ``cmd_sweep_all`` kwargs to the ONE CLI resolver; a numeric CSV
    validates exactly as before (the legacy byte-identical arm). Mixing the
    token with numbers is a 400 — the two grammars don't compose.
    """
    tokens = _clean_csv(value, DEFAULT_K)
    lowered = [tok.lower() for tok in tokens]
    if DEFAULT_K in lowered:
        if lowered != [DEFAULT_K]:
            raise ValueError(
                f"k must be either '{DEFAULT_K}' (the Apriori extinction default) or a "
                f"CSV of whole numbers 1..{_MAX_K} — got {','.join(tokens)!r}"
            )
        return DEFAULT_K
    out: list[str] = []
    for token in tokens:
        try:
            parsed = int(token)
        except ValueError as exc:
            raise ValueError(f"k must be a whole number 1..{_MAX_K} — got {token!r}") from exc
        if not 1 <= parsed <= _MAX_K:
            raise ValueError(f"k must be a whole number 1..{_MAX_K} — got {token!r}")
        out.append(str(parsed))
    _validate_k_against_max_combos(out)
    return ",".join(out)


def _validate_k_against_max_combos(k_tokens: Sequence[str]) -> None:
    """Refuse a ``k`` whose mask count the engine's own ``max_combos`` guard will reject.

    A round-12 adversarial-refuter review found the SAME "preflight says
    ok, engine construction says no" failure mode round 11 fixed for SL/TP
    also applies to ``k``: ``build_sweep_kwargs`` always passes
    ``conditions=None`` (the full :data:`TOTAL_CONDITIONS`-bit vocabulary)
    and ``max_combos=None`` (→ the engine's
    :data:`~brutex.engine.sweep_driver.DEFAULT_MAX_COMBOS` = 100,000), so
    :meth:`~brutex.engine.sweep_orchestrator.SweepGridConfig.__post_init__`
    rejects any ``k`` whose ``C(TOTAL_CONDITIONS, k)`` mask count exceeds
    that cap — e.g. ``k=4`` alone enumerates ``C(60,4) = 487,635`` masks.
    ``_normalise_k`` only checked ``k`` was in the raw ``[1, 60]`` RANGE,
    never against this cap, so a single-tuple launch (too small for the
    SLA wall-clock guard to ever catch) could preflight `ok: true` — the
    preflight's OWN estimator even correctly reports the true, over-cap
    trial count — and still crash deterministically inside engine
    construction. Refuse HERE, loudly, before any background job starts.

    Parameters
    ----------
    k_tokens
        The already-range-validated ``k`` values, as decimal strings.

    Raises
    ------
    ValueError
        If any ``k`` enumerates more than
        :data:`~brutex.engine.sweep_driver.DEFAULT_MAX_COMBOS` masks over
        the full :data:`TOTAL_CONDITIONS`-bit vocabulary.
    """
    from brutex.engine.combo_mask import count_combos
    from brutex.engine.sweep_driver import DEFAULT_MAX_COMBOS

    for token in k_tokens:
        k = int(token)
        n_masks = count_combos(k, TOTAL_CONDITIONS)
        if n_masks > DEFAULT_MAX_COMBOS:
            raise ValueError(
                f"k={k} enumerates {n_masks} masks (C({TOTAL_CONDITIONS},{k})) "
                f"exceeding the engine's max_combos={DEFAULT_MAX_COMBOS} — the dashboard "
                f"has no override for this ceiling; choose a smaller k"
            )


def _normalise_leg(value: str | None, default: str, *, label: str) -> str:
    """Validate a SL/TP leg: a CSV mixing ``none`` cells and positive-paisa ints.

    Mirrors the CLI ``--sl``/``--tp`` grammar (``brutex.cli._parse_paisa_list``)
    PER VALUE, not per whole string: each token is either the literal
    ``none`` (any case — normalised to lowercase) or a positive paisa int —
    so the whole-leg ``"none"``, a fully-gridded CSV, AND the mixed
    both-arms-in-one-sweep shape (``"none,5,100,200"`` — PR #1297's default
    grid string) all resolve (operator ruling 2026-07-08, R54; the R54
    review round 2 found the original whole-string check hard-refused the
    mixed shape with a self-contradictory error).
    """
    source = value if value is not None and value.strip() else default
    tokens = [tok.strip() for tok in source.split(",") if tok.strip()]
    out: list[str] = []
    for token in tokens:
        if token.lower() == "none":
            out.append("none")
            continue
        try:
            parsed = int(token)
        except ValueError as exc:
            raise ValueError(
                f"{label} must be 'none' or positive paisa integers — got {token!r}"
            ) from exc
        if parsed <= 0:
            raise ValueError(f"{label} must be 'none' or positive paisa integers — got {token!r}")
        out.append(str(parsed))
    return ",".join(out)


def leg_values(leg: str) -> tuple[int | None, ...]:
    """Parse a normalised SL/TP leg CSV into engine cell values (``none`` → ``None``).

    The ONE parsed-leg source of truth (CLAUDE.md §2 Rule 11 COMMON) shared
    by :attr:`SweepRunRequest.stage3_enable`, :func:`_validate_mode_a_legs`
    and :func:`build_sweep_kwargs` — the R54 review round 2 found the
    none-cell decision triplicated as independent whole-string ``== "none"``
    comparisons, one grammar widening away from re-instating the round-11
    deep engine crash. Token semantics mirror ``brutex.cli._parse_paisa_list``
    exactly (pinned by test): ``none`` (any case) → ``None``, blank tokens
    skipped, everything else ``int()``. Assumes an ALREADY
    :func:`_normalise_leg`-validated string — a junk token raises the same
    ``ValueError`` the resolver would have raised earlier, never a silent
    drop.
    """
    return tuple(
        None if tok.strip().lower() == "none" else int(tok.strip())
        for tok in leg.split(",")
        if tok.strip()
    )


def leg_has_none(leg: str) -> bool:
    """True iff the normalised *leg* carries at least one ``none`` (no-SL/TP) cell."""
    return any(value is None for value in leg_values(leg))


def _validate_mode_a_legs(sl: str, tp: str) -> None:
    """Refuse an off-Mode-A-grid leg value UNDER the effective Stage-3 posture.

    History: ``build_sweep_kwargs`` launches a dashboard sweep with
    ``stage3_enable=True`` by default (R5's institutional flip) —
    :meth:`~brutex.engine.sweep_orchestrator.SweepGridConfig.__post_init__`
    (ADR-0007 condition 3) rejects any ``None`` or off-Mode-A-grid
    SL/TP value at ENGINE construction time, deep inside the background
    job. A round-11 adversarial-refuter review therefore refused BOTH
    shapes here, before any background job starts (Golden Rule #10).

    OPERATOR RULING (2026-07-08, R54 — both SL/TP arms AND no-SL/TP arms
    must be coverable): a ``none`` cell — whole leg OR mixed into a CSV —
    is first-class RUNNABLE. Any ``none`` cell downgrades the launch's
    effective ``stage3_enable`` to ``False``
    (:attr:`SweepRunRequest.stage3_enable`), and with Stage-3 OFF the
    engine accepts ANY positive paisa value — so the Mode-A axis check
    stands down for the WHOLE launch (the R54 review round 2 found the
    unconditional check refusing a provably engine-runnable
    ``sl="none", tp="1500"`` launch on a Stage-3 justification that very
    launch had just disabled). Under the effective ``stage3_enable=True``
    posture (no ``none`` cell anywhere) the off-grid refusal stays fully
    intact — the round-11 deep-crash class stays closed. The Stage-3
    downgrade itself is disclosed at LAUNCH time by
    :func:`build_sweep_kwargs` (WARN) + :func:`oneclick_preflight_for_plan`
    (clamp payload) — never here, so a request that is subsequently
    refused (e.g. a bad interval) logs no false "downgraded for this
    launch" claim.

    Parameters
    ----------
    sl, tp
        The ALREADY :func:`_normalise_leg`-validated (syntax-clean) leg
        strings — CSVs of positive paisa integers and/or ``none`` cells.

    Raises
    ------
    ValueError
        If the launch's effective posture is ``stage3_enable=True`` (no
        ``none`` cell on either leg) and any value is off its locked
        Mode-A grid. A ``none``-carrying launch never raises here
        (operator ruling 2026-07-08, R54).
    """
    from brutex.engine.stage3_grid import MODE_A_SL_AXIS, MODE_A_TP_AXIS, Stage3GridError

    if leg_has_none(sl) or leg_has_none(tp):
        # Effective stage3_enable=False (SweepRunRequest.stage3_enable): the
        # engine accepts every positive paisa value with Stage-3 off, so an
        # on-grid requirement here would refuse a runnable launch on a
        # disabled justification (R54 review round 2, finding 2).
        return
    for label, axis, value in (("sl", MODE_A_SL_AXIS, sl), ("tp", MODE_A_TP_AXIS, tp)):
        for token in value.split(","):
            try:
                axis.normalize(int(token))
            except Stage3GridError as exc:
                raise ValueError(
                    f"{label}={token!r} is off the locked Mode-A grid required by the "
                    f"institutional Stage-3 default: {exc}"
                ) from exc


def _normalise_nonblank(value: str | None, default: str) -> str:
    """Strip *value*; fall back to *default* when it is ``None`` or blank."""
    if value is None:
        return default
    stripped = value.strip()
    if not stripped:
        return default
    return stripped


def _normalise_date(value: str | None, default: str, *, label: str) -> str:
    """Validate *value* parses as an ISO ``YYYY-MM-DD`` date; return the canonical string."""
    source = value if value is not None and value.strip() else default
    try:
        return date.fromisoformat(source.strip()).isoformat()
    except ValueError as exc:
        raise ValueError(f"{label} must be a YYYY-MM-DD date — got {source!r}") from exc


def resolve_sweep_request(
    *,
    symbols: str | None = None,
    intervals: str | None = None,
    directions: str | None = None,
    k: str | None = None,
    sl: str | None = None,
    tp: str | None = None,
    top: int | None = None,
    mode: str | None = None,
    exchange: str | None = None,
    segment: str | None = None,
    start: str | None = None,
    end: str | None = None,
    acknowledge_sla_overrun: bool = False,
    today: date,
) -> SweepRunRequest:
    """Validate + normalise raw dashboard form params into a :class:`SweepRunRequest`.

    A **pure** function (no I/O, deterministic given *today*): every axis is
    cleaned of blank tokens and defaulted when absent, and every value is checked
    so a bad form field raises a clear :class:`ValueError` the route turns into a
    400 — never a deep, opaque background-job crash (Golden Rule #10 honesty). The
    ``end`` default is *today* (injected, never read from the clock here, so the
    mapping is reproducibly testable, Golden Rule #4).

    Parameters
    ----------
    symbols, intervals, directions, k, sl, tp
        Raw CSV axis strings (``None`` / blank ⇒ the CLI-matching default).
    top
        Raw top-N (``None`` ⇒ :data:`DEFAULT_TOP`); must be ``>= 1``.
    mode, exchange, segment
        Raw non-blank identity / partition strings (``None`` / blank ⇒ default).
    start, end
        Raw ``YYYY-MM-DD`` window bounds; ``end`` defaults to *today*. ``end`` must
        not precede ``start``.
    acknowledge_sla_overrun
        Passed straight through to the resulting :class:`SweepRunRequest`
        (no validation — either the operator confirmed the proceed-dialog or
        did not; gap-hunt ruling 2).
    today
        The injected "now" date used as the ``end`` default + ``end >= start`` check.

    Returns
    -------
    SweepRunRequest
        The validated, normalised request.

    Raises
    ------
    ValueError
        On any unknown interval, non-BUY/SELL direction, out-of-range / non-integer
        K, malformed SL/TP leg, an SL/TP leg with a value off the locked
        Mode-A grid under the effective ``stage3_enable=True`` posture
        (see :func:`_validate_mode_a_legs` — a round-11
        adversarial-refuter finding; a ``none`` cell — whole leg or mixed
        into a CSV — never raises: it downgrades the launch's effective
        Stage-3 posture instead, disclosed at launch/preflight time,
        operator ruling 2026-07-08, R54),
        non-positive ``top``, unparseable date,
        ``end < start``, or an out-of-engine-scope launch coordinate (the
        :func:`brutex.engine.sweep_scope.check_sweep_scope` refusal —
        DEC-ENGINE-SCOPE-GUARD-001 + its corporate-action extension
        DEC-CORP-ACTION-GATE-001 / ADR-0016 — surfaced eagerly as a 400 via
        the route's existing ``ValueError`` mapping).
    """
    resolved_top = DEFAULT_TOP if top is None else top
    if resolved_top < 1:
        raise ValueError(f"top must be a whole number >= 1 — got {resolved_top!r}")
    resolved_start = _normalise_date(start, DEFAULT_START, label="start")
    resolved_end = _normalise_date(end, today.isoformat(), label="end")
    if resolved_end < resolved_start:
        raise ValueError(f"end {resolved_end!r} must not precede start {resolved_start!r}")
    resolved_symbols = _normalise_symbols(symbols)
    resolved_exchange = _normalise_nonblank(exchange, DEFAULT_EXCHANGE)
    resolved_segment = _normalise_nonblank(segment, DEFAULT_SEGMENT)
    # Engine-scope coordinate law (DEC-ENGINE-SCOPE-GUARD-001, Layer 2): an
    # out-of-engine-scope dashboard launch is a 400 via the existing
    # ValueError → route mapping. There is deliberately NO override parameter
    # on the dashboard — the research escape hatch is CLI-only
    # (--allow-out-of-scope, loud + audit-stamped).
    from brutex.engine.sweep_scope import check_sweep_scope

    scope = check_sweep_scope(
        resolved_symbols.split(","),
        exchange=resolved_exchange,
        segment=resolved_segment,
        allow_out_of_scope=False,
    )
    if not scope.ok:
        raise ValueError(scope.refusal_message())
    resolved_sl = _normalise_leg(sl, DEFAULT_SL, label="sl")
    resolved_tp = _normalise_leg(tp, DEFAULT_TP, label="tp")
    _validate_mode_a_legs(resolved_sl, resolved_tp)
    return SweepRunRequest(
        symbols=resolved_symbols,
        intervals=_normalise_intervals(intervals),
        directions=_normalise_directions(directions),
        k=_normalise_k(k),
        sl=resolved_sl,
        tp=resolved_tp,
        exchange=resolved_exchange,
        segment=resolved_segment,
        start=resolved_start,
        end=resolved_end,
        mode=_normalise_nonblank(mode, DEFAULT_MODE),
        top=resolved_top,
        acknowledge_sla_overrun=acknowledge_sla_overrun,
    )


def default_results_root(lake_root: Path) -> Path:
    """Return the default per-tuple results root for *lake_root* (``<lake>/sweeps``).

    Mirrors ``cmd_sweep_all``'s ``out_root`` derivation exactly so the dashboard
    and the CLI write/read the SAME sweep tree (CLAUDE.md §2 Rule 11 COMMON).
    """
    return lake_root / "sweeps"


def sweep_progress_file(results_root: Path) -> Path:
    """Return the live progress file path under *results_root* (``progress/sweep-progress.json``).

    The EXACT path ``cmd_sweep_all`` publishes its
    :class:`~brutex.engine.sweep_progress.SweepProgressSnapshot` to — the route
    points ``state.sweep_progress_path`` here so the existing
    ``GET /api/v1/sweep-progress`` serves the running sweep with no new plumbing.
    """
    return results_root / "progress" / "sweep-progress.json"


def build_sweep_kwargs(
    request: SweepRunRequest,
    *,
    lake_root: Path,
    results_root: Path | None = None,
    catalog_path: Path | None = None,
) -> dict[str, Any]:
    """Map a validated *request* onto the ``cmd_sweep_all`` keyword contract.

    A pure 1:1 mapping (the request is already validated). The non-swept knobs
    are pinned to the INSTITUTIONAL dashboard defaults (R5, gap-hunt BLOCKER
    #1): ``conditions=None`` (the full 60-condition vocabulary —
    ``default_condition_bits()``), ``options_sim=False`` (signal-only —
    options are R12, parked), ``overfit_gate=True`` + ``stage3_enable=True``
    (the 5 hard gates → composite ranking → Stage-3 CMA-ES refinement chain
    ON by default — EXCEPT when either SL/TP leg carries a ``none`` cell
    (whole leg or mixed CSV): per the operator ruling 2026-07-08 (R54,
    no-SL/TP arms must be coverable) the launch's effective
    ``stage3_enable`` is ``False`` (:attr:`SweepRunRequest.stage3_enable` —
    the ONE parsed-leg derivation), since CMA-ES cannot seed its ``x0``
    from a ``none`` cell — ADR-0007 condition 3 — and the engine accepts
    ``None`` cells only with Stage-3 off; the downgrade is disclosed by
    ONE ``sweep_runner.none_leg_stage3_downgrade`` WARN per BUILT
    engine-run kwargs — i.e. at launch time, once per exchange-group
    engine run; the read-only preflight builds no kwargs and WARNs never,
    its payload carries the ``stage3_enable`` clamp row instead
    (:func:`oneclick_preflight_for_plan`) — never silent, and never a
    false "downgraded for this launch" log on a request that was refused
    before launching), and
    ``allow_missing_slices=False`` (a named gap REFUSES
    the whole launch — ruling O-a refuse-whole — never a silent partial
    sweep). ``acknowledge_sla_overrun`` threads the operator's R20
    proceed-dialog confirmation straight to ``cmd_sweep_all``'s existing
    parameter of the same name (ruling 2).

    Parameters
    ----------
    request
        The validated launch request.
    lake_root
        The resolved lake root (the sweep reads its Parquet partitions from here).
    results_root
        Per-tuple results tree (``None`` ⇒ :func:`default_results_root`).
    catalog_path
        Catalog DuckDB path (``None`` ⇒ ``cmd_sweep_all``'s ``<lake>/catalog.duckdb``).

    Returns
    -------
    dict[str, Any]
        The exact kwargs to splat into ``cmd_sweep_all``.
    """
    stage3_enable = request.stage3_enable
    if not stage3_enable:
        # Operator ruling 2026-07-08 (R54): a "none" SL/TP cell is first-class
        # RUNNABLE — Stage-3 is downgraded OFF for that launch (CMA-ES x0
        # must seed from on-grid paisa values, ADR-0007 condition 3; the
        # engine's SweepGridConfig accepts None cells only with Stage-3 off).
        # WARNed HERE — at kwargs-build = launch time — so a refused request
        # never logs a false "downgraded for this launch" claim (R54 review
        # round 2, finding 4).
        _LOG.warning(
            "sweep_runner.none_leg_stage3_downgrade",
            legs=[
                label
                for label, leg in (("sl", request.sl), ("tp", request.tp))
                if leg_has_none(leg)
            ],
            sl=request.sl,
            tp=request.tp,
            exchange=request.exchange,
            detail=(
                "SL/TP 'none' cell is first-class runnable (operator ruling "
                "2026-07-08, R54: both SL/TP arms AND no-SL/TP arms must be "
                "coverable); Stage-3 CMA-ES refinement is downgraded OFF for "
                "this launch — its gridded SL/TP cells (if any) are NOT "
                "Stage-3-refined either (the x0 must seed from on-grid paisa "
                "values, ADR-0007 condition 3)"
            ),
        )
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": results_root,
        "exchange": request.exchange,
        "segment": request.segment,
        "symbols": request.symbols,
        "intervals": request.intervals,
        "start": request.start,
        "end": request.end,
        "directions": request.directions,
        "k": request.k,
        "conditions": None,
        "sl": request.sl,
        "tp": request.tp,
        "top": request.top,
        "mode": request.mode,
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "overfit_gate": True,
        "options_sim": False,
        # Operator ruling 2026-07-08 (R54): derived from the PARSED legs via
        # the ONE SweepRunRequest.stage3_enable property (leg_has_none) —
        # never a whole-string comparison, which a mixed "none,50" leg reads
        # as gridded and re-instates the round-11 deep engine crash (R54
        # review round 2, finding 3). Fully-gridded legs keep the R5
        # institutional stage3_enable=True default.
        "stage3_enable": stage3_enable,
        "allow_missing_slices": False,
        "acknowledge_sla_overrun": request.acknowledge_sla_overrun,
        # DEC-ENGINE-SCOPE-GUARD-001: the dashboard NEVER overrides the engine
        # scope — explicit here to keep the 1:1 cmd_sweep_all kwargs doctrine
        # visible (the request was already scope-checked in
        # resolve_sweep_request; the CLI escape hatch does not exist on this
        # surface by design).
        "allow_out_of_scope": False,
    }


def run_sweep_from_request(  # pragma: no cover — orchestration shim over the 100%-tested engine
    request: SweepRunRequest,
    *,
    lake_root: Path,
    results_root: Path,
) -> int:
    """Run the full grid sweep for *request* via ``cmd_sweep_all`` (returns its exit code).

    The thin live-lake shim the background job calls: it builds the kwargs
    (:func:`build_sweep_kwargs`) and delegates to the 100%-tested
    ``brutex.cli.cmd_sweep_all`` (imported lazily to keep the dashboard import
    light + avoid a CLI import cycle). Marked ``# pragma: no cover`` exactly like
    ``cmd_sweep_all`` itself — the orchestration is exercised end-to-end by the CLI
    integration tests; the dashboard route tests replace THIS function with an
    offline fake (no lake, no sweep) to keep the unit suite hermetic.

    ENV HYGIENE (DEC-SWEEP-APRIORI-DEFAULT-001, MIGRATION.md #1752): this is
    the ONE in-process embedder of ``cmd_sweep_all``, whose extinction default
    setdefault-writes ``BRUTEX_SWEEP_APRIORI`` into the process env for the
    spawn workers. The dashboard process OUTLIVES the sweep job (unlike a CLI
    run, which exits), so the flag is snapshot-restored here — success AND
    raise — or a leak would silently flip a LATER explicit-K dashboard sweep
    into apriori mode (different tallies + a raised max_combos ⇒ a different
    sweep_id — a byte-identity violation). The single-flight job guarantees no
    concurrent reader DURING the run.

    SINGLE-FLIGHT DEPENDENCY (DEC-SWEEP-APRIORI-DEFAULT-001, MIGRATION.md
    #1752 review fix round 2): the "no concurrent reader DURING the run"
    claim above is not free-standing — it is EXACTLY the guarantee of the
    dashboard route layer launching this shim through
    ``BackgroundPullJob.start(job_kind=JOB_KIND_SWEEP)``
    (:class:`brutex.dashboard.pull_jobs.BackgroundPullJob` — one background
    job per process, a second ``start`` while a job runs is REFUSED). If a
    future caller ever invokes this function OUTSIDE that single-flight job,
    the env snapshot-restore below becomes a read-modify-write race against
    any concurrent ``BRUTEX_SWEEP_APRIORI`` reader/writer in the same
    process — re-establish an equivalent mutual exclusion first.
    """
    from brutex.cli import cmd_sweep_all
    from brutex.engine.sweep_apriori import SWEEP_APRIORI_ENV

    # INTERLEAVE HAZARD (see SINGLE-FLIGHT DEPENDENCY above): this snapshot →
    # cmd_sweep_all's setdefault-write → the finally-restore is safe ONLY
    # because BackgroundPullJob.start(job_kind=JOB_KIND_SWEEP) serialises the
    # whole span — two interleaved runs would capture each other's
    # mid-flight value as `before` and restore a stale flag.
    before = os.environ.get(SWEEP_APRIORI_ENV)
    try:
        return cmd_sweep_all(
            **build_sweep_kwargs(request, lake_root=lake_root, results_root=results_root)
        )
    finally:
        if before is None:
            os.environ.pop(SWEEP_APRIORI_ENV, None)
        else:
            os.environ[SWEEP_APRIORI_ENV] = before


# ── multi-symbol / SENSEX fan-out (gap-hunt MAJOR #9, ruling 9) ──────────────


@dataclass(frozen=True, slots=True)
class OneClickSweepPlan:
    """ONE logical ``sweep_run_id`` wrapping N per-exchange engine-run requests.

    Attributes
    ----------
    sweep_run_id
        ONE logical identifier threading every per-exchange request (R43 —
        "one run_id threads page, logs, Telegram, artifacts, audit chain";
        here it threads the N engine runs this ONE click fans out into).
    requests
        One :class:`SweepRunRequest` per exchange group, in exchange-sorted
        (deterministic) order.
    """

    sweep_run_id: str
    requests: tuple[SweepRunRequest, ...]


def group_symbols_by_exchange(symbols: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Partition *symbols* by canonical exchange (SENSEX→BSE, NIFTY/BANKNIFTY→NSE).

    Order-preserving within each group. A symbol outside the 3 canonical spot
    indexes (e.g. a stock) is bucketed under :data:`DEFAULT_EXCHANGE` — it is
    NOT refused here; the per-group :func:`resolve_sweep_request` call
    downstream refuses it with the full existing
    :func:`~brutex.engine.sweep_scope.check_sweep_scope` law citation, so
    there is exactly ONE out-of-scope refusal message in the codebase, never
    a duplicate.
    """
    groups: dict[str, list[str]] = {}
    for symbol in symbols:
        exchange = _SPOT_SYMBOL_EXCHANGE.get(symbol, DEFAULT_EXCHANGE)
        groups.setdefault(exchange, []).append(symbol)
    return {exchange: tuple(syms) for exchange, syms in groups.items()}


def resolve_oneclick_sweep_plan(
    *,
    symbols: str | None = None,
    intervals: str | None = None,
    directions: str | None = None,
    k: str | None = None,
    sl: str | None = None,
    tp: str | None = None,
    top: int | None = None,
    mode: str | None = None,
    segment: str | None = None,
    start: str | None = None,
    end: str | None = None,
    acknowledge_sla_overrun: bool = False,
    today: date,
    run_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> OneClickSweepPlan:
    """Fan a (possibly cross-exchange) one-click selection into ONE :class:`OneClickSweepPlan`.

    R2's "blank ⇒ all 3 (NIFTY-first)" default (:data:`DEFAULT_ONECLICK_SYMBOLS`)
    lives HERE, not in :func:`resolve_sweep_request` (whose own bare default
    stays the single-exchange :data:`DEFAULT_SYMBOLS` — gap-hunt finding #9: a
    blank cross-exchange default cannot be resolved by a function that only
    accepts ONE ``exchange``).

    Groups the requested symbols by canonical exchange
    (:func:`group_symbols_by_exchange`), then calls :func:`resolve_sweep_request`
    ONCE per exchange group — each group independently validated (scope,
    dates, legs, ...) with its own resolved ``exchange`` — and stamps every
    resulting request under ONE shared ``sweep_run_id``. Deliberately NO
    ``exchange`` parameter here (the fan-out RESOLVES it per group).

    Parameters
    ----------
    run_id_factory
        Injectable ``sweep_run_id`` generator (Golden Rule #4 reproducibility
        in tests); defaults to a fresh :func:`uuid.uuid4` hex per call.
    (every other parameter mirrors :func:`resolve_sweep_request` verbatim,
    minus ``exchange``.)

    Raises
    ------
    ValueError
        Propagated from any per-group :func:`resolve_sweep_request` call
        (e.g. an out-of-engine-scope symbol, an unknown interval, ...).
    """
    raw_symbols = _clean_csv(symbols, DEFAULT_ONECLICK_SYMBOLS)
    groups = group_symbols_by_exchange(raw_symbols)
    run_id = run_id_factory()
    requests = tuple(
        resolve_sweep_request(
            symbols=",".join(groups[exchange]),
            intervals=intervals,
            directions=directions,
            k=k,
            sl=sl,
            tp=tp,
            top=top,
            mode=mode,
            exchange=exchange,
            segment=segment,
            start=start,
            end=end,
            acknowledge_sla_overrun=acknowledge_sla_overrun,
            today=today,
        )
        for exchange in sorted(groups)
    )
    return OneClickSweepPlan(sweep_run_id=run_id, requests=requests)


def _dedup_axis(value: str) -> list[str]:
    """Split *value* on commas and de-duplicate, order-preserving.

    Mirrors :func:`brutex.engine.sweep_orchestrator`'s private ``_dedup_seq``
    (every :class:`~brutex.engine.sweep_orchestrator.SweepGridConfig` axis is
    de-duplicated at construction — ``k_values=[2, 2]`` is ONE tuple, not
    two). A hostile review confirmed the estimator previously counted raw
    (non-deduplicated) CSV tokens, over-counting trials/tuples relative to
    what the engine actually executes whenever a caller repeats a token
    (e.g. ``symbols="NIFTY,NIFTY"``) — deduping here keeps the R20 estimate
    honestly equal to the real grid size.
    """
    return list(dict.fromkeys(tok for tok in value.split(",")))


def _axes_counts(request: SweepRunRequest) -> tuple[list[int], int, int]:
    """Return ``(k_values, other_axes_product, tuple_count)`` for *request* — pure arithmetic.

    ``other_axes_product`` = every axis EXCEPT ``k`` (symbols × intervals ×
    directions × sl × tp — proximities/fib-anchors are not dashboard-exposed,
    so both count as 1); ``tuple_count`` additionally multiplies in
    ``len(k_values)`` (mirroring
    :func:`brutex.engine.sweep_orchestrator.count_sweep_tuples`'s own product
    definition, without constructing a full
    :class:`~brutex.engine.sweep_orchestrator.SweepGridConfig` — no data
    snapshot exists yet at preflight time). Every axis is de-duplicated
    first (:func:`_dedup_axis`) so a repeated CSV token never inflates the
    estimate beyond what the engine will actually grid.
    """
    k_values = [int(tok) for tok in _dedup_axis(request.k)]
    other_axes_product = (
        len(_dedup_axis(request.symbols))
        * len(_dedup_axis(request.intervals))
        * len(_dedup_axis(request.directions))
        * len(_dedup_axis(request.sl))
        * len(_dedup_axis(request.tp))
    )
    return k_values, other_axes_product, other_axes_product * len(k_values)


def oneclick_preflight_for_plan(
    plan: OneClickSweepPlan,
    *,
    lake_root: Path,
    today: date,
    free_bytes: int | None = None,
) -> dict[str, Any]:
    """Run the R4/R20 preflight+estimate for EVERY exchange group in *plan*.

    Folds every per-exchange-group :class:`~brutex.engine.sweep_oneclick_preflight.OneClickPreflightVerdict`
    into ONE contract dict (ruling 9: a refusal in ANY group refuses the
    WHOLE logical run; the estimator sums trials/seconds/bytes across
    groups — ``O(#groups)``, at most 2 today).

    **Multi-group SLA re-check (adversarial-refuter fix):** each per-group
    call to :func:`~brutex.engine.sweep_oneclick_preflight.run_oneclick_preflight`
    only checks THAT group's own tuple count against the CLAUDE.md §8
    envelope — but the dashboard's ``/oneclick/run`` route executes every
    group SEQUENTIALLY on one background job, so the TRUE wall-clock is the
    SUM across groups. Two groups that each individually project right at
    the envelope ceiling (``ALLOWED``) would otherwise combine into a
    silent ~2x overrun with no refusal and no acknowledge-prompt anywhere —
    exactly the invisible-refusal failure mode ruling 2 exists to prevent.
    When *plan* has more than one group, this function ADDITIONALLY re-runs
    :func:`~brutex.engine.sweep_sla_guard.check_sweep_sla` once more against
    the SUMMED tuple count and folds that verdict in (a single-group plan's
    existing per-group check already covers it exactly — re-checking would
    only duplicate the same finding).

    Cost honesty (Golden Rule #10, hostile-review fix): the free-disk-space
    check (:func:`~brutex.data.lake_footprint.evaluate_disk_guard`) measures
    the WHOLE lake's footprint — ``O(files in the lake)``, not scoped to any
    one exchange group — so it is computed EXACTLY ONCE here (never inside
    the per-group loop) and threaded into every
    :func:`~brutex.engine.sweep_oneclick_preflight.run_oneclick_preflight`
    call via its ``disk_decision`` parameter; a naive per-group call would
    pay the full-lake scan N times over for one click.

    Parameters
    ----------
    plan
        The resolved :class:`OneClickSweepPlan`.
    lake_root
        The lake root (shared across every group — one lake, one writer).
    today
        Injected "now" (Golden Rule #4).
    free_bytes
        Injectable free-disk override (tests); ``None`` reads the real disk.

    Returns
    -------
    dict[str, Any]
        ``{sweep_run_id, ok, refusals, clamps, estimator}`` — the contract
        shape plus the one logical run id threading every group. The
        ``estimator`` sub-dict additionally carries ``nominal_condition_count``
        (always :data:`~brutex.vocabulary.bits.TOTAL_CONDITIONS`) and
        ``effective_condition_count`` (the MINIMUM across groups — the
        honest worst-case VWAP dead-bits annotation, gap-hunt #8).
        ``clamps`` additionally carries one ``stage3_enable``
        requested→applied row per exchange group whose SL/TP legs carry a
        ``none`` cell (the R54 Stage-3 downgrade, disclosed on the
        proceed-dialog's own data channel — operator ruling 2026-07-08).
    """
    from brutex.data.lake_footprint import evaluate_disk_guard
    from brutex.engine.sweep_estimator import CALIBRATED_BASIS, PROVISIONAL_BASIS
    from brutex.engine.sweep_oneclick_preflight import Clamp, Refusal, run_oneclick_preflight
    from brutex.engine.sweep_sla_guard import SlaDecision, check_sweep_sla

    results_root = default_results_root(lake_root)
    disk_decision = evaluate_disk_guard(lake_root, observed_multiplier=None, free_bytes=free_bytes)
    all_refusals: list[dict[str, Any]] = []
    all_clamps: list[dict[str, Any]] = []
    total_trials = 0
    total_seconds = 0.0
    total_bytes = 0
    total_tuple_count = 0
    any_provisional = False
    nominal_condition_count = 0
    effective_condition_count: int | None = None
    for request in plan.requests:
        k_values, other_axes_product, tuple_count = _axes_counts(request)
        verdict = run_oneclick_preflight(
            lake_root=lake_root,
            results_root=results_root,
            exchange=request.exchange,
            segment=request.segment,
            symbols=tuple(request.symbols.split(",")),
            intervals=tuple(request.intervals.split(",")),
            start=date.fromisoformat(request.start),
            end=date.fromisoformat(request.end),
            today=today,
            k_values=k_values,
            other_axes_product=other_axes_product,
            tuple_count=tuple_count,
            top=request.top,
            allow_missing_slices=False,
            acknowledge_sla_overrun=request.acknowledge_sla_overrun,
            check_vix_coverage=True,
            disk_decision=disk_decision,
        )
        all_refusals.extend(r.to_dict() for r in verdict.refusals)
        all_clamps.extend(c.to_dict() for c in verdict.clamps)
        if not request.stage3_enable:
            # R54 (operator ruling 2026-07-08) disclosure on the operator
            # proceed-dialog's own data channel: a none-cell group's Stage-3
            # downgrade is a requested→applied clamp row, never a
            # server-log-only fact (R54 review round 2, finding 5). One row
            # per downgraded exchange group, exchange-named.
            none_legs = "+".join(
                label
                for label, leg in (("sl", request.sl), ("tp", request.tp))
                if leg_has_none(leg)
            )
            all_clamps.append(
                Clamp(
                    field="stage3_enable",
                    requested="true",
                    applied="false",
                    reason=(
                        f"{request.exchange}: SL/TP 'none' cell(s) on {none_legs} — "
                        "Stage-3 CMA-ES refinement is downgraded OFF for this "
                        "launch, so its gridded SL/TP cells (if any) are NOT "
                        "Stage-3-refined either (operator ruling 2026-07-08, "
                        "R54: no-SL/TP arms are first-class runnable; the "
                        "CMA-ES x0 must seed from on-grid paisa values, "
                        "ADR-0007 condition 3)"
                    ),
                ).to_dict()
            )
        total_trials += verdict.estimator.trials
        total_seconds += verdict.estimator.est_seconds_provisional
        total_bytes += verdict.estimator.est_bytes
        total_tuple_count += tuple_count
        if verdict.estimator.estimate_basis != CALIBRATED_BASIS:
            any_provisional = True
        nominal_condition_count = verdict.estimator.nominal_condition_count
        effective_condition_count = (
            verdict.estimator.effective_condition_count
            if effective_condition_count is None
            else min(effective_condition_count, verdict.estimator.effective_condition_count)
        )

    if len(plan.requests) > 1:
        # A single group's own per-group check (inside the loop above) is
        # already the exact combined check for that case — only a REAL
        # multi-group fan-out needs the summed re-check.
        acknowledge = plan.requests[0].acknowledge_sla_overrun
        combined_sla = check_sweep_sla(total_tuple_count, acknowledge=acknowledge)
        if combined_sla.decision is SlaDecision.REFUSED:
            all_refusals.append(
                Refusal(
                    symbol="*", tf="*", missing_spans=(), reason=combined_sla.refusal_message()
                ).to_dict()
            )
        elif combined_sla.decision is SlaDecision.ACKNOWLEDGED:
            all_clamps.append(
                Clamp(
                    field="sla_overrun",
                    requested=str(total_tuple_count),
                    applied="acknowledged",
                    reason=combined_sla.acknowledged_warning(),
                ).to_dict()
            )

    return {
        "sweep_run_id": plan.sweep_run_id,
        "ok": not all_refusals,
        "refusals": all_refusals,
        "clamps": all_clamps,
        "estimator": {
            "trials": total_trials,
            "est_seconds_provisional": total_seconds,
            "est_bytes": total_bytes,
            "estimate_basis": PROVISIONAL_BASIS if any_provisional else CALIBRATED_BASIS,
            "nominal_condition_count": nominal_condition_count,
            "effective_condition_count": effective_condition_count,
        },
    }


__all__ = [
    "DEFAULT_DIRECTIONS",
    "DEFAULT_EXCHANGE",
    "DEFAULT_INTERVALS",
    "DEFAULT_K",
    "DEFAULT_MODE",
    "DEFAULT_ONECLICK_SYMBOLS",
    "DEFAULT_SEGMENT",
    "DEFAULT_SL",
    "DEFAULT_START",
    "DEFAULT_SYMBOLS",
    "DEFAULT_TOP",
    "DEFAULT_TP",
    "OneClickSweepPlan",
    "SweepRunRequest",
    "build_sweep_kwargs",
    "default_results_root",
    "group_symbols_by_exchange",
    "leg_has_none",
    "leg_values",
    "oneclick_preflight_for_plan",
    "resolve_oneclick_sweep_plan",
    "resolve_sweep_request",
    "run_sweep_from_request",
    "sweep_progress_file",
]
