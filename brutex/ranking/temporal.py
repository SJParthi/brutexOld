"""Temporal-robustness YEAR GATE — the calendar-coverage + concentration predicates
(DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

The operator demand: a combo found on 2020→2026 data must have worked ACROSS the
periods — a combo that "only worked one lucky year" MUST NOT occupy a top-K slot.
This module is the PURE predicate core (stdlib-only, beside
:mod:`brutex.ranking.overfit` — the structural template): the per-combo inputs are
an exact int64-paisa :class:`TemporalTriple` reduced from the per-judged-year pnl
fold, and the two doors are

* **P1 — coverage:** ``profitable_judged_years / n_judged_years >=
  min_profitable_years_fraction``. The denominator is the JUDGED calendar-year
  count, NEVER the years-with-trades count (an active-years denominator scores the
  trades-only-in-its-lucky-year combo 1/1 and makes the gate vacuous for exactly
  the shape it targets); a zero-trade judged year has ``P_y = 0`` and counts
  AGAINST. Profitable is strictly ``P_y > 0`` — a flat year is not profitable.
* **P2 — concentration (positive-mass share):** with ``S⁺ = Σ positive per-judged-
  year pnl`` and ``M⁺ = max positive per-judged-year pnl``, pass iff ``S⁺ == 0``
  (per-combo ABSTAIN — no division is ever performed; such a combo is already
  dead at the profit-factor door and at P1) or ``M⁺/S⁺ <=
  max_single_year_pnl_share``. The positive-mass definition is the only
  mixed-sign-robust one: a net-total share (``max/Σnet``) is meaning-free under
  mixed signs (``> 1``, negative, ``÷0``).

**Exact threshold math — no float compare, no NaN pathway by construction.** Both
door comparisons are exact ``Fraction(str(cfg.value))`` cross-comparisons over
exact integer paisa; the config floats are validated finite + in-range at the
frozen-dataclass ``__post_init__`` (the :class:`~brutex.ranking.overfit.
OverfitGateConfig` template), so no non-finite value can reach a comparison.

**Judged years** (:func:`judged_years`): interior calendar years of the tuple's
bar window are ALWAYS judged; an EDGE year (first/last) is judged iff its
data-grounded distinct-IST-session count is ``>= min_edge_year_sessions``
(default 120 ≈ 6 months of ~20.5 NSE sessions/month — full-floor 2020 with ~248
sessions is judged; a Feb-swept current year is not). The exclusion is
SYMMETRIC: a sliver edge year counts in NEITHER numerator NOR denominator
(counting it would punish every combo mechanically each Jan 1; counting it
only-when-profitable would be directional dishonesty).

**Whole-gate ABSTENTION:** below ``min_judged_years`` (default 2) judged years
the whole gate abstains — NO combo is rejected (:func:`temporal_gate_positions`
returns ``None``; :func:`apply_temporal_gate` keeps every survivor and records
``abstained``). A sub-year window cannot evidence cross-year robustness, and at
one judged year P2's share is identically 1.0 — an un-abstained gate would
silently kill every profitable combo. Guidance: trust the gate at >= 4 judged
years.

Honest envelope (Golden Rule #10) — ships verbatim, never softened
-------------------------------------------------------------------
* **This gate does NOT remove curve-fitting.** Under the no-edge null (year
  signs i.i.d. Bernoulli(0.5)) the default P1 passes ``P(>= 4 of 7 profitable)
  = 64/128 = 0.50`` — the year gate alone passes HALF of pure noise over 7
  judged years. It is a **concentration filter, not a significance test**;
  multiplicity correction is and remains the DSR/CPCV/WFO cascade's job.
  Year-gating ADDS a calendar-aligned constraint on top of that stack.
* **The false-negative bill:** a genuine post-structural-break edge (valid only
  from 2023, say) fails P1 by construction — the gate cannot distinguish "new
  regime" from "lucky window"; that is the cascade's job. The price of the
  operator decree is paid visibly: observe mode + the /results strips mean
  nothing dies invisibly, and the thresholds are config.
* **Exit-year attribution:** a boundary-straddling trade books wholly to its
  EXIT year (the ``combo_aggregates.aggregate_combo`` law — property-pinned so
  the two folds can never diverge).
* **Zero-trade judged years count as unprofitable** — deliberate severity;
  sparse-strategy operators must tune ``min_profitable_years_fraction``
  consciously.

MODES: ``"gate"`` filters; ``"observe"`` runs the identical predicates, counts
would-be kills, filters nothing (the shipped CLI default — the operator sees
kills before candidates die). Mode is orchestration state consumed by
:func:`apply_temporal_gate`; the predicates themselves are mode-blind.

IDENTITY RULING: this config does NOT enter ``mask_run_id`` /
``canonical_trade_params`` / ``_tuple_digest`` (the verified DSR precedent — a
pure rejection gate changes WHICH rows persist, never any row's computed
bytes); provenance lives on the hash-chained sweep audit payload, emitted only
when configured.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

__all__ = [
    "MODE_GATE",
    "MODE_OBSERVE",
    "YEAR_GATE_PROFITABLE_YEARS",
    "YEAR_GATE_REASON_ORDER",
    "YEAR_GATE_SINGLE_YEAR_SHARE",
    "TemporalGateInput",
    "TemporalGateStats",
    "TemporalRobustnessConfig",
    "TemporalTriple",
    "YearGateDiagnostic",
    "apply_temporal_gate",
    "judged_years",
    "positive_mass_share",
    "temporal_gate_passes",
    "temporal_gate_positions",
    "temporal_gate_reasons",
    "triple_from_year_pnls",
    "year_gate_diagnostic",
]

#: Gate mode: the temporal verdict FILTERS the hard-door survivors.
MODE_GATE: Final[str] = "gate"
#: Observe mode: identical predicate evaluation; would-be kills are COUNTED
#: (``TemporalGateStats.observed_kills``) + surfaced on the sweep summary/audit,
#: but nothing is filtered — the shipped CLI rollout default.
MODE_OBSERVE: Final[str] = "observe"
_VALID_MODES: Final[frozenset[str]] = frozenset((MODE_GATE, MODE_OBSERVE))

# ── Reason vocabulary (OURS — a SEPARATE wire vocabulary from the frozen
#    audit-1-owned ``brutex.ranking.GATE_REASON_ORDER``, which is pinned to
#    ``passes_gates``' exact five doors and is byte-untouched by this DEC) ──
#: P1 failed — too few profitable judged years (coverage).
YEAR_GATE_PROFITABLE_YEARS: Final[str] = "year_gate_profitable_years"
#: P2 failed — one year carries too much of the positive pnl mass (concentration).
YEAR_GATE_SINGLE_YEAR_SHARE: Final[str] = "year_gate_single_year_share"
#: The COMPLETE temporal reason vocabulary in EVALUATION order — the exact door
#: order :func:`temporal_gate_reasons` judges. A stable wire contract: renaming
#: or reordering a token is a breaking change (pinned by test).
YEAR_GATE_REASON_ORDER: Final[tuple[str, ...]] = (
    YEAR_GATE_PROFITABLE_YEARS,
    YEAR_GATE_SINGLE_YEAR_SHARE,
)


@dataclass(frozen=True, slots=True)
class TemporalRobustnessConfig:
    """Opt-in configuration for the temporal-robustness year gate.

    Passing an instance (via ``SweepRunConfig.temporal`` → the rank seams)
    activates the gate; ``None`` keeps the legacy ranking byte-identical (the
    :class:`~brutex.ranking.overfit.OverfitGateConfig` opt-in shape).

    Parameters
    ----------
    min_profitable_years_fraction
        P1: the minimum fraction of JUDGED years with strictly positive pnl.
        In ``(0, 1]``; default ``0.5`` — over 7 judged years that demands
        >= 4 profitable (``Fraction(3,7) < 1/2 <= Fraction(4,7)``), killing
        the 1/2/3-lucky-year shapes while tolerating 3 flat/losing years (a
        real vol-conditional edge sleeping through a grind year survives).
        H₀ honesty: at 0.5 over 7 years the door alone passes 50% of pure
        noise — see the module docstring; the DSR stack stays the
        significance authority.
    max_single_year_pnl_share
        P2: the maximum share of the positive pnl mass one judged year may
        carry (``M⁺/S⁺``). In ``(0, 1]``; default ``0.6`` — the uniform-edge
        ideal over 7 years is ~0.14, but Indian index regimes are lumpy
        (2020 COVID vol, 2024 election vol), so a 55%-in-2020 vol-harvester
        passes at 0.6 and dies at 0.5. The /results share column is the
        recalibration evidence.
    min_edge_year_sessions
        An EDGE (first/last) calendar year is judged iff it has at least this
        many distinct IST session dates in the tuple's own bar window
        (data-grounded — immune to window holes). ``>= 0``; default ``120``
        (~6 months of NSE sessions). ``0`` judges every edge year.
    min_judged_years
        Below this many judged years the WHOLE gate abstains (no combo
        rejected). ``>= 1``; default ``2``.
    mode
        ``"gate"`` (filter) or ``"observe"`` (count + surface would-be kills,
        filter nothing).

    Raises
    ------
    ValueError
        If a fraction/share is non-finite or outside ``(0, 1]``, a count knob
        is out of range, or *mode* is unknown.
    """

    min_profitable_years_fraction: float = 0.5
    max_single_year_pnl_share: float = 0.6
    min_edge_year_sessions: int = 120
    min_judged_years: int = 2
    mode: str = MODE_GATE

    def __post_init__(self) -> None:
        for name in ("min_profitable_years_fraction", "max_single_year_pnl_share"):
            value: float = getattr(self, name)
            if not math.isfinite(value) or not 0.0 < value <= 1.0:
                raise ValueError(
                    f"TemporalRobustnessConfig.{name} must be a finite fraction in "
                    f"(0, 1], got {value!r}"
                )
        if self.min_edge_year_sessions < 0:
            raise ValueError(
                f"TemporalRobustnessConfig.min_edge_year_sessions must be >= 0, "
                f"got {self.min_edge_year_sessions}"
            )
        if self.min_judged_years < 1:
            raise ValueError(
                f"TemporalRobustnessConfig.min_judged_years must be >= 1, "
                f"got {self.min_judged_years}"
            )
        if self.mode not in _VALID_MODES:
            raise ValueError(
                f"TemporalRobustnessConfig.mode must be one of {sorted(_VALID_MODES)}, "
                f"got {self.mode!r}"
            )

    def p1_threshold(self) -> Fraction:
        """P1's EXACT threshold — ``Fraction(str(...))`` of the config value."""
        return Fraction(str(self.min_profitable_years_fraction))

    def p2_threshold(self) -> Fraction:
        """P2's EXACT threshold — ``Fraction(str(...))`` of the config value."""
        return Fraction(str(self.max_single_year_pnl_share))


@dataclass(frozen=True, slots=True)
class TemporalTriple:
    """One combo's per-judged-year pnl fold, reduced to the 3 gate inputs.

    Exact int64 paisa throughout. The reduction of the per-year fold
    ``P_y = Σ pnl_paisa of trades whose EXIT bar falls in IST year y`` over the
    JUDGED year set:

    * ``profitable_judged_years`` — ``#{y judged : P_y > 0}`` (strictly
      positive; a flat or zero-trade judged year is NOT profitable).
    * ``max_pos_year_pnl_paisa`` — ``M⁺ = max{P_y : P_y > 0}`` (``0`` when no
      judged year is profitable).
    * ``sum_pos_year_pnl_paisa`` — ``S⁺ = Σ{P_y : P_y > 0}`` (``0`` when no
      judged year is profitable).

    Raises
    ------
    ValueError
        On an inconsistent reduction — negative components, ``S⁺ < M⁺``, or a
        positive-count/positive-mass disagreement (``profitable > 0`` must hold
        iff ``M⁺ > 0`` iff ``S⁺ > 0``). Fabricated triples are structurally
        impossible to gate on (Golden Rule #1).
    """

    profitable_judged_years: int
    max_pos_year_pnl_paisa: int
    sum_pos_year_pnl_paisa: int

    def __post_init__(self) -> None:
        if self.profitable_judged_years < 0:
            raise ValueError(
                f"TemporalTriple.profitable_judged_years must be >= 0, "
                f"got {self.profitable_judged_years}"
            )
        if self.max_pos_year_pnl_paisa < 0 or self.sum_pos_year_pnl_paisa < 0:
            raise ValueError(
                f"TemporalTriple positive-mass components must be >= 0, got "
                f"M⁺={self.max_pos_year_pnl_paisa} S⁺={self.sum_pos_year_pnl_paisa}"
            )
        if self.sum_pos_year_pnl_paisa < self.max_pos_year_pnl_paisa:
            raise ValueError(
                f"TemporalTriple: S⁺={self.sum_pos_year_pnl_paisa} < "
                f"M⁺={self.max_pos_year_pnl_paisa} — an impossible positive-mass fold"
            )
        has_profitable = self.profitable_judged_years > 0
        if has_profitable != (self.max_pos_year_pnl_paisa > 0):
            raise ValueError(
                f"TemporalTriple: profitable_judged_years={self.profitable_judged_years} "
                f"disagrees with M⁺={self.max_pos_year_pnl_paisa} — a profitable year "
                f"exists iff a positive mass exists"
            )


class TemporalGateStats:
    """Mutable per-rank recorder the rank seams fill for the sweep summary/audit.

    A tiny out-parameter (not a return-type change): :func:`brutex.ranking.
    rank_combos` / the bounded columnar rank keep their public return types, and
    the caller (``run_persisted_sweep``) reads the recorder after ranking to
    stamp ``temporal_gated`` / ``temporal_observed_kills`` / ``temporal_abstained``
    onto its summary + hash-chained audit payload.
    """

    __slots__ = ("abstained", "evaluated", "killed", "observed_kills")

    def __init__(self) -> None:
        #: The gate RAN over a non-empty survivor set (config active).
        self.evaluated: bool = False
        #: Whole-gate abstention fired (judged years below the config floor).
        self.abstained: bool = False
        #: Survivors REMOVED by the gate (``mode="gate"`` only).
        self.killed: int = 0
        #: Survivors that WOULD have been removed (``mode="observe"`` only).
        self.observed_kills: int = 0


@dataclass(frozen=True, slots=True)
class TemporalGateInput:
    """The rank-seam bundle activating the temporal gate for one leaderboard.

    Parameters
    ----------
    cfg
        The active :class:`TemporalRobustnessConfig`.
    n_judged_years
        ``len(judged_years(...))`` for the tuple's bar window (``>= 0``; the
        whole-gate abstention below ``cfg.min_judged_years`` is applied by the
        predicate core, so a sliver window is safe to pass through).
    triples
        BATCH path: the per-candidate :class:`TemporalTriple`\\ s aligned 1:1
        with the rank candidates (``None`` entries are a wiring bug — the rank
        seam refuses them loudly, naming the mask, never a silent pass-all).
        BOUNDED path: ``None`` — the triples live in the
        ``SurvivorColumns(with_temporal=True)`` arrays.
    stats
        Optional :class:`TemporalGateStats` recorder.
    """

    cfg: TemporalRobustnessConfig
    n_judged_years: int
    triples: Sequence[TemporalTriple | None] | None = None
    stats: TemporalGateStats | None = None


def judged_years(
    sessions_per_year: Mapping[int, int], cfg: TemporalRobustnessConfig
) -> tuple[int, ...]:
    """Return the sorted JUDGED calendar years of one tuple's bar window.

    Interior years (strictly between the window's first and last calendar year)
    are ALWAYS judged — including a year the data skips entirely (a window hole
    is a zero-trade judged year that honestly counts against, exactly like a
    flat year). An EDGE year (first/last) is judged iff its data-grounded
    distinct-IST-session count meets ``cfg.min_edge_year_sessions`` — no
    contiguity assumption, only the tuple's own observed sessions.

    Parameters
    ----------
    sessions_per_year
        ``{IST calendar year: distinct IST session dates observed}`` from the
        tuple's own bars (the per-tuple O(N_bars) precompute). Empty ⇒ ``()``.
    cfg
        The active config (``min_edge_year_sessions``).

    Raises
    ------
    ValueError
        If a session count is negative (a fabricated census).
    """
    if not sessions_per_year:
        return ()
    for year, sessions in sessions_per_year.items():
        if sessions < 0:
            raise ValueError(f"judged_years: sessions_per_year[{year}] = {sessions} is negative")
    first = min(sessions_per_year)
    last = max(sessions_per_year)
    judged = set(range(first + 1, last))  # interior years — always judged
    for edge in {first, last}:
        if sessions_per_year.get(edge, 0) >= cfg.min_edge_year_sessions:
            judged.add(edge)
    return tuple(sorted(judged))


def triple_from_year_pnls(pnl_by_year: Mapping[int, int], judged: Iterable[int]) -> TemporalTriple:
    """Fold a per-year pnl map into the gate's :class:`TemporalTriple` over *judged*.

    The ONE reduction law (CLAUDE.md §11 COMMON): the rank-time array fold in
    ``sweep_run`` and the serve-time diagnostic both reduce through these exact
    semantics (property-pinned equal). A judged year absent from *pnl_by_year*
    contributes ``P_y = 0`` (zero-trade ⇒ not profitable); a year present in
    *pnl_by_year* but NOT judged contributes NOTHING (symmetric exclusion).

    Parameters
    ----------
    pnl_by_year
        ``{IST exit year: Σ pnl_paisa}`` — exact int paisa.
    judged
        The judged year set (see :func:`judged_years`). Duplicates are ignored.
    """
    profitable = 0
    max_pos = 0
    sum_pos = 0
    for year in set(judged):
        pnl = pnl_by_year.get(year, 0)
        if pnl > 0:
            profitable += 1
            sum_pos += pnl
            max_pos = max(max_pos, pnl)
    return TemporalTriple(
        profitable_judged_years=profitable,
        max_pos_year_pnl_paisa=max_pos,
        sum_pos_year_pnl_paisa=sum_pos,
    )


def _validate_predicate_inputs(triple: TemporalTriple, n_judged_years: int) -> None:
    """Shared guard for the per-combo predicates — fail loud on impossible inputs."""
    if n_judged_years < 1:
        raise ValueError(
            f"temporal gate predicates need n_judged_years >= 1, got {n_judged_years} "
            f"(the whole-gate abstention below min_judged_years is the caller's job — "
            f"see temporal_gate_positions)"
        )
    if triple.profitable_judged_years > n_judged_years:
        raise ValueError(
            f"TemporalTriple.profitable_judged_years={triple.profitable_judged_years} "
            f"exceeds n_judged_years={n_judged_years} — an impossible fold"
        )


def temporal_gate_reasons(
    triple: TemporalTriple, *, n_judged_years: int, cfg: TemporalRobustnessConfig
) -> tuple[str, ...]:
    """The reason-returning twin of :func:`temporal_gate_passes` — every failed door.

    Both doors are judged INDEPENDENTLY (no short-circuit), in
    :data:`YEAR_GATE_REASON_ORDER`; the empty tuple ⇔ the combo passes. The two
    functions can never disagree: ``temporal_gate_passes(t, ...) ==
    (temporal_gate_reasons(t, ...) == ())`` holds for every input
    (property-pinned). Exact Fraction math on both doors; P2's ``S⁺ == 0`` arm
    ABSTAINS (passes) with NO division performed.

    Raises
    ------
    ValueError
        If ``n_judged_years < 1`` or the triple is impossible for it (see
        :func:`_validate_predicate_inputs`).
    """
    _validate_predicate_inputs(triple, n_judged_years)
    reasons: list[str] = []
    if Fraction(triple.profitable_judged_years, n_judged_years) < cfg.p1_threshold():
        reasons.append(YEAR_GATE_PROFITABLE_YEARS)
    if triple.sum_pos_year_pnl_paisa > 0:  # S⁺ == 0 ⇒ P2 abstains — no division, ever
        share = Fraction(triple.max_pos_year_pnl_paisa, triple.sum_pos_year_pnl_paisa)
        if share > cfg.p2_threshold():
            reasons.append(YEAR_GATE_SINGLE_YEAR_SHARE)
    return tuple(reasons)


def temporal_gate_passes(
    triple: TemporalTriple, *, n_judged_years: int, cfg: TemporalRobustnessConfig
) -> bool:
    """Return ``True`` iff the combo clears BOTH temporal doors (P1 and P2).

    The boolean twin of :func:`temporal_gate_reasons` (delegates — one door
    law, they can never drift). Boundary semantics are EXACT: a P1 fraction
    exactly AT the threshold passes (``>=``); a P2 share exactly AT the
    threshold passes (``<=``).
    """
    return temporal_gate_reasons(triple, n_judged_years=n_judged_years, cfg=cfg) == ()


def temporal_gate_positions(
    survivor_triples: Sequence[TemporalTriple],
    *,
    n_judged_years: int,
    cfg: TemporalRobustnessConfig,
) -> list[int] | None:
    """The temporal gate CORE over survivor triples — shared by both rank paths.

    The structural twin of :func:`brutex.ranking.overfit_gate_positions`
    (CLAUDE.md §11 COMMON): the batch rank and the bounded columnar rank both
    consume THIS function, so their kept sets are bit-identical by construction.
    Mode-BLIND — ``"observe"`` orchestration lives in :func:`apply_temporal_gate`.

    Returns
    -------
    list[int] | None
        ``None`` when the WHOLE gate abstains (``n_judged_years <
        cfg.min_judged_years`` — a sub-floor window cannot evidence cross-year
        robustness; no combo may be rejected, Golden Rule #10); else the kept
        survivor POSITIONS (indices into *survivor_triples*, ascending input
        order).
    """
    if n_judged_years < cfg.min_judged_years:
        return None
    return [
        position
        for position, triple in enumerate(survivor_triples)
        if temporal_gate_passes(triple, n_judged_years=n_judged_years, cfg=cfg)
    ]


def apply_temporal_gate(
    survivor_triples: Sequence[TemporalTriple],
    *,
    cfg: TemporalRobustnessConfig,
    n_judged_years: int,
    stats: TemporalGateStats | None = None,
) -> list[int]:
    """Apply the gate under its MODE; return the survivor positions to KEEP.

    The ONE mode-orchestration fold both rank paths share: runs
    :func:`temporal_gate_positions`, records *stats*, and returns

    * every position unchanged on a whole-gate ABSTENTION
      (``stats.abstained = True``);
    * every position unchanged in ``"observe"`` mode with the would-be kills
      counted (``stats.observed_kills``);
    * the kept positions in ``"gate"`` mode with the kills counted
      (``stats.killed``).

    O(survivors) exact-integer predicate evaluations; no allocation beyond the
    returned position list.
    """
    if stats is not None:
        stats.evaluated = True
    outcome = temporal_gate_positions(survivor_triples, n_judged_years=n_judged_years, cfg=cfg)
    if outcome is None:
        if stats is not None:
            stats.abstained = True
        return list(range(len(survivor_triples)))
    kills = len(survivor_triples) - len(outcome)
    if cfg.mode == MODE_OBSERVE:
        if stats is not None:
            stats.observed_kills += kills
        return list(range(len(survivor_triples)))
    if stats is not None:
        stats.killed += kills
    return outcome


def positive_mass_share(triple: TemporalTriple) -> float | None:
    """``M⁺/S⁺`` as a DISPLAY-ONLY float, or ``None`` when ``S⁺ == 0``.

    Verdicts NEVER use this figure — they compare exact Fractions
    (:func:`temporal_gate_reasons`); this is the /results share column only.
    """
    if triple.sum_pos_year_pnl_paisa == 0:
        return None
    return triple.max_pos_year_pnl_paisa / triple.sum_pos_year_pnl_paisa


@dataclass(frozen=True, slots=True)
class YearGateDiagnostic:
    """A serve-time year-gate verdict recomputed from STORED inputs.

    The display twin (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001, Q4): the /results view
    folds each row's stored per-year pnl cells + judged-years column through the
    SAME predicate core at the given (default) config, UI-labeled
    "temporal @ defaults" — never presented as the producing run's rank-time
    verdict, which lives on the sweep audit chain.

    Attributes
    ----------
    passed
        The verdict at the supplied config, or ``None`` when abstained.
    abstained
        Whole-gate abstention (judged years below the config floor).
    reasons
        The failed-door tokens (:data:`YEAR_GATE_REASON_ORDER` order; empty on
        a pass OR an abstention).
    profitable_years, n_judged_years
        The raw P1 diagnostics rendered beside the verdict.
    max_year_share
        The raw P2 diagnostic (display float; ``None`` when ``S⁺ == 0``).
    """

    passed: bool | None
    abstained: bool
    reasons: tuple[str, ...]
    profitable_years: int
    n_judged_years: int
    max_year_share: float | None


def year_gate_diagnostic(
    pnl_by_year: Mapping[int, int],
    judged: Iterable[int],
    cfg: TemporalRobustnessConfig | None = None,
) -> YearGateDiagnostic:
    """Recompute the year-gate verdict from stored per-year pnl + judged years.

    Parameters
    ----------
    pnl_by_year
        ``{IST exit year: Σ pnl_paisa}`` decoded from the row's stored
        ``stats_by_year_json`` cells (exact ints — stored values only, never a
        re-simulation).
    judged
        The row's stored judged-year list (``temporal_judged_years_json``).
    cfg
        The config to judge at; ``None`` ⇒ the defaults (the "@ defaults"
        display diagnostic). ``mode`` is ignored — a diagnostic filters nothing.
    """
    active = cfg if cfg is not None else TemporalRobustnessConfig()
    judged_set = set(judged)
    triple = triple_from_year_pnls(pnl_by_year, judged_set)
    share = positive_mass_share(triple)
    if len(judged_set) < active.min_judged_years:
        return YearGateDiagnostic(
            passed=None,
            abstained=True,
            reasons=(),
            profitable_years=triple.profitable_judged_years,
            n_judged_years=len(judged_set),
            max_year_share=share,
        )
    reasons = temporal_gate_reasons(triple, n_judged_years=len(judged_set), cfg=active)
    return YearGateDiagnostic(
        passed=reasons == (),
        abstained=False,
        reasons=reasons,
        profitable_years=triple.profitable_judged_years,
        n_judged_years=len(judged_set),
        max_year_share=share,
    )
