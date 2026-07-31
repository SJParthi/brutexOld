"""Flag-gated Rust ranking seam — the `rank_combos` kernel binding (slice 4).

The thin binding over :mod:`brutex.engine.rust_parity_core` (the ONE COMMON
burn-in / always-on-differential / autorevert wrapper core) that serves
:func:`brutex.ranking.rank_combos` from the ``fno_math`` ranking kernels
(``rust/fno-math/src/ranking.rs``) when ``BRUTEX_RANKING_RUST`` is ON.

## The flag — ``BRUTEX_RANKING_RUST`` over ``RANKING_RUST_DEFAULT``

Per-call env resolution through the COMMON tristate vocabulary
(:func:`brutex.engine.rust_parity_core.env_flag_enabled` — the
``rust_metrics`` law): a ``_TRUTHY`` value forces ON, a ``_FALSY`` value
(``0``/``false``/``no``/``off`` — the explicit kill switch) forces OFF,
unset/whitespace-empty falls to :data:`RANKING_RUST_DEFAULT` — **ON since
the DEC-RUST-DEFAULT-ON-WAVE-001 flip (#2215)**: the operator
RUST-O(1)-EVERYWHERE directive engages the kernel by default, protected by
the SAME safety layers untouched (the always-on differential + burn-in
bit-compare + ship-the-ORACLE autorevert + refuse-poison gate +
degrade-to-oracle on a wheel-absent/stale/poisoned crate) — and any
unrecognised value fails SAFE to OFF with one warn-once
``flag_unrecognised``. The explicit ``0`` kill switch keeps the caller on
the verbatim Python path, byte-identical by construction (the dispatch is
one env-dict probe — pinned by the OFF-cost bench).

## The ONE dispatch point + per-family parity policies

:func:`brutex.ranking.rank_combos` is the ONLY production dispatch seam
(slice-4 design E-2): flag ON → :func:`rank_combos_via_rust`; a ``None``
return (wheel missing / stale) → the verbatim Python pipeline. Inside the
ON arm every kernel step runs under its float-parity class's policy
(:mod:`brutex.engine.rust_parity_core`):

* ``ranking.gates`` / ``ranking.term_inputs`` / ``ranking.order`` /
  ``ranking.overfit_verdict`` — class (a)/(b) rows, burn-in-16 tripwire;
* ``ranking.composite`` / ``ranking.dsr_z`` — class (b*) rows (they embed
  the ``_zscores`` pow-identity sites), ALWAYS-ON per-call differential:
  every call also runs the Python oracle and bit-compares; a mismatch
  ships the oracle value and latches the family OFF process-wide, so a
  divergent bit can never reach a leaderboard, a persisted row, or a redo
  comparison — on ANY call, not just during burn-in.

The ``ranking.term_inputs`` family additionally carries the facade's ONE
CLASS-C input-envelope arm: the crate's ``cr_div_i64`` risk-efficiency
division serves only operands within 2**53 paisa, so a survivor beyond that
ceiling is PRE-SCREENED in Python and served from the oracle under the
distinct ``term_inputs_beyond_crate`` reason — a legitimate input, never a
broken crate, and therefore exempt from the strict refusal
(:data:`_TERM_INPUTS_BEYOND_CRATE`).

Everything the ruling table keeps in Python stays in Python here:
``pool_sharpe_dispersion`` (exact-rational — PR-2), ``deflated_sharpe_for``
(the slice-6 seam, including the ``excess + 3.0`` exact-input add),
``ensure_unique_survivor_masks`` (byte-pinned error message),
:class:`~brutex.ranking.RankedCombo` construction, and the inactive
``dsr_z_contributions`` ``None`` arm (``[0.0] * n`` — zero float
arithmetic).

## Honest envelope (Golden Rule #10)

Rank-time, per-leaderboard code — NOT the sweep hot loop. NO speedup is
claimed: the (b*) rows double-compute the Python oracle on EVERY call by
design (bench-pinned ≤ a measured multiple of the OFF arm), and the
(a)/(b) rows double-compute through their burn-in. Past burn-in the
(a)/(b) ORACLE double-compute stops, but the refuse-poison ``validate``
gate does NOT: it runs on every kernel call FOREVER (a post-burn-in value
ships uncompared, so the shape gate must be permanent) at the validator's
own O(value) cost — the ``_is_*`` checks below are O(n)/O(n·w)
element-type scans, and the ``ranking.order`` family's
:func:`_is_permutation` pays an O(n log n) sort per call (~a second sort
beside the kernel's own), so the ON ``_ORDER`` step permanently costs
about 2× the OFF arm's sort work. A deliberate correctness-over-cost
trade, never claimed cheap. The payoff is
one-language consistency; the wire value is ALWAYS the Python oracle's
value or a bit-identical Rust value.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final, NamedTuple, cast

from brutex.engine.rust_parity_core import (
    POLICY_ALWAYS,
    POLICY_BURN_IN,
    ParityFamily,
    env_flag_enabled,
    pack_float_rows,
    pack_floats,
    resolve_kernel_symbol,
    run_with_parity,
)
from brutex.ranking.overfit import (
    OverfitGateConfig,
    deflated_sharpe_for,
    passes_overfit_gate,
    pool_sharpe_dispersion,
)

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Sequence

    from brutex.metrics.compute import ResultMetrics
    from brutex.ranking import RankedCombo

#: The env flag (module docstring). Default ON since the #2215 wave.
RANKING_RUST_ENV: Final[str] = "BRUTEX_RANKING_RUST"

#: The ONE default-arm constant — flipped to ``True`` by the operator
#: RUST-O(1)-EVERYWHERE directive (DEC-RUST-DEFAULT-ON-WAVE-001, #2215):
#: the ranking kernel serves by default, with the always-on differential +
#: burn-in bit-compare + ship-the-oracle autorevert + degrade-to-oracle
#: safety layers untouched. The explicit ``0`` env value is the kill switch.
RANKING_RUST_DEFAULT: Final[bool] = True

#: DEC-RUST-STRICT-NO-FALLBACK-001: this facade opts EVERY family into strict
#: mode. Almost every reason the families announce is class A
#: (``flag_unrecognised``, ``wheel_missing``, ``wheel_stale``,
#: ``kernel_error``) or class B (``kernel_disabled``,
#: ``kernel_result_refused``, ``divergence``) — those REFUSE. The ONE class-C
#: arm is :data:`_TERM_INPUTS_BEYOND_CRATE` (see its comment).
_STRICT_FACADE: Final[str] = "brutex.ranking.rust_ranking"

#: The f64-exactness ceiling of the crate's ``cr_div_i64`` FAST path — the
#: literal twin of ``ranking::MAX_EXACT_DIV_OPERAND``
#: (``rust/fno-math/src/ranking.rs``). Every integer with ``|x| <= 2**53``
#: converts to ``f64`` EXACTLY, so the hardware division of the two exact
#: operands is the correctly-rounded quotient — CPython's ``int / int``
#: bit-for-bit. Past it the ``as f64`` casts round BEFORE the division and
#: the result double-rounds off CPython's answer, so the crate refuses rather
#: than serve a divergent bit.
_MAX_EXACT_DIV_OPERAND: Final[int] = 1 << 53

#: CLASS-C reason (DEC-RUST-STRICT-NO-FALLBACK-001) — the ONE
#: outside-the-crate's-envelope arm in this facade.
#:
#: The 7th composite term is ``risk_efficiency`` =
#: ``avg_mfe_paisa / max(avg_mae_paisa, 1)``, which the crate computes with
#: ``cr_div_i64`` (``rust/fno-math/src/ranking.rs``). That kernel serves ONLY
#: operands within :data:`_MAX_EXACT_DIV_OPERAND`; outside it the crate's own
#: docstring says "the Python wrapper serves an out-of-envelope call from the
#: bit-exact oracle". So a survivor whose ``|avg_mfe_paisa|`` or
#: ``max(|avg_mae_paisa|, 1)`` exceeds 2**53 paisa is a LEGITIMATE ranking
#: input that Python was ALWAYS the intended handler for — never a broken
#: crate. Refusing it would convert a valid leaderboard into a crash, the
#: exact inverse of this DEC's intent, so it stays exempt.
#:
#: Honest long-term fix: WIDEN the crate — a correctly-rounded big-integer
#: division (the u128 RNE slow path the kernel's docstring parks in PR-2) —
#: then this arm becomes unreachable and the entry is deleted. Exempting is
#: the honest interim, not the destination.
_TERM_INPUTS_BEYOND_CRATE: Final[str] = "term_inputs_beyond_crate"

#: The facade's CLASS-C allowlist, handed to the ONE family that owns the
#: envelope arm. Class A / class B reasons are deliberately absent.
_STRICT_EXEMPT_REASONS: Final[frozenset[str]] = frozenset({_TERM_INPUTS_BEYOND_CRATE})

#: The seam-level family: carries the flag/wheel warn-once memos (no kernel
#: of its own — policy is irrelevant, burn-in chosen as the neutral label).
_SEAM: Final[ParityFamily] = ParityFamily(
    "ranking.seam", policy=POLICY_BURN_IN, strict_facade=_STRICT_FACADE
)

#: Class (a) — gate verdicts (comparisons only). Burn-in tripwire.
_GATES: Final[ParityFamily] = ParityFamily(
    "ranking.gates", policy=POLICY_BURN_IN, strict_facade=_STRICT_FACADE
)
#: Class (b) — term inputs (sub, mul, correctly-rounded div). Burn-in.
#: The ONLY family carrying a class-C envelope arm (the ``cr_div_i64``
#: risk-efficiency operand ceiling — see :data:`_TERM_INPUTS_BEYOND_CRATE`).
_TERMS: Final[ParityFamily] = ParityFamily(
    "ranking.term_inputs",
    policy=POLICY_BURN_IN,
    strict_facade=_STRICT_FACADE,
    strict_exempt_reasons=_STRICT_EXEMPT_REASONS,
)
#: Class (b*) — the composite pipeline EMBEDS ``_zscores`` (pow-identity
#: sites), so the whole kernel runs always-on (the strictest embedded class
#: governs the family).
_COMPOSITE: Final[ParityFamily] = ParityFamily(
    "ranking.composite", policy=POLICY_ALWAYS, strict_facade=_STRICT_FACADE
)
#: Class (b*) — the DSR z-term embeds ``_zscores`` too. Always-on.
_DSR_Z: Final[ParityFamily] = ParityFamily(
    "ranking.dsr_z", policy=POLICY_ALWAYS, strict_facade=_STRICT_FACADE
)
#: Class (a) — the `(-score, mask)` index-permutation sort. Burn-in.
_ORDER: Final[ParityFamily] = ParityFamily(
    "ranking.order", policy=POLICY_BURN_IN, strict_facade=_STRICT_FACADE
)
#: Class (a) — the batched overfit verdict (one `>=` per survivor). Burn-in.
_OVERFIT: Final[ParityFamily] = ParityFamily(
    "ranking.overfit_verdict", policy=POLICY_BURN_IN, strict_facade=_STRICT_FACADE
)

#: Every family, for the test-reset seam (order irrelevant).
_ALL_FAMILIES: Final[tuple[ParityFamily, ...]] = (
    _SEAM,
    _GATES,
    _TERMS,
    _COMPOSITE,
    _DSR_Z,
    _ORDER,
    _OVERFIT,
)


def ranking_rust_enabled() -> bool:
    """Resolve the ranking-Rust flag over :data:`RANKING_RUST_DEFAULT`.

    The COMMON tristate law (module docstring): truthy → ON, falsy → OFF
    (the explicit ``0`` kill switch), unset/empty → the default (ON since
    #2215), unrecognised → OFF with one warn-once ``flag_unrecognised``.
    Read per call — no global flag state; every parallel worker reads its
    own environment.
    """
    return env_flag_enabled(RANKING_RUST_ENV, default=RANKING_RUST_DEFAULT, warn_family=_SEAM)


def reset_ranking_rust_for_tests(*, remaining: int | None = None) -> None:
    """Reset every family's latch/burn-in/memo state (test isolation ONLY).

    ``remaining=0`` puts the burn-in families in the post-burn-in steady
    state; the default restores a fresh-process view. Never called from
    production code paths.
    """
    for family in _ALL_FAMILIES:
        if remaining is None:
            family.reset_for_tests()
        else:
            family.reset_for_tests(remaining=remaining)


class _Kernels(NamedTuple):
    """The resolved ``fno_math`` ranking entry points (one lookup per call)."""

    passes_gates: Callable[..., object]
    composite_term_inputs: Callable[..., object]
    composite_scores: Callable[..., object]
    dsr_z_contributions: Callable[..., object]
    rank_order: Callable[..., object]
    overfit_verdicts: Callable[..., object]


#: The compiled symbols the ON-arm pipeline needs, in :class:`_Kernels` order.
_KERNEL_SYMBOLS: Final[tuple[str, ...]] = (
    "ranking_passes_gates",
    "ranking_composite_term_inputs",
    "ranking_composite_scores",
    "ranking_dsr_z_contributions",
    "ranking_rank_order",
    "ranking_overfit_verdicts",
)


def _resolve_kernels() -> _Kernels | None:
    """Resolve every ranking kernel symbol, or ``None`` with a warn-once.

    A wheel-absent environment (``wheel_missing``) or a stale wheel lacking
    any slice-4 symbol (``wheel_stale``) returns ``None`` — the caller runs
    the verbatim Python pipeline, so wheel state can never change results
    or error behaviour (the Phase-A law).
    """
    resolved: list[Callable[..., object]] = []
    for symbol in _KERNEL_SYMBOLS:
        kernel = resolve_kernel_symbol(_SEAM, symbol)
        if kernel is None:
            return None
        resolved.append(kernel)
    return _Kernels(*resolved)


def _is_bool_tuple(value: object, n: int) -> bool:
    """Domain check: a length-*n* tuple of real bools (the verdict shape)."""
    return isinstance(value, tuple) and len(value) == n and all(isinstance(v, bool) for v in value)


def _is_float_list(value: object, n: int) -> bool:
    """Domain check: a length-*n* list of real floats (a value column)."""
    return isinstance(value, list) and len(value) == n and all(isinstance(v, float) for v in value)


def _is_term_rows(value: object, n: int, width: int) -> bool:
    """Domain check: *n* rows of exactly *width* real floats (the term matrix)."""
    if not isinstance(value, list) or len(value) != n:
        return False
    return all(
        isinstance(row, list) and len(row) == width and all(isinstance(v, float) for v in row)
        for row in value
    )


def _is_permutation(value: object, n: int) -> bool:
    """Domain check: a tuple that permutes ``range(n)`` (the order shape)."""
    return (
        isinstance(value, tuple)
        and len(value) == n
        and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
        and sorted(cast(tuple[int, ...], value)) == list(range(n))
    )


def _row_streak_threshold(m: ResultMetrics) -> int:
    """Door 5's DERIVED bound, computed Python-side (the transcendental
    ``libm`` derivation stays on the oracle — DEC-METRICS-RUST-STATS-002
    fold-in-Rust/combine-in-Python split): the kernel receives the finished
    ``int`` and does only the host-invariant comparison
    ``max_consecutive_losses > streak_threshold``.

    A junk ``loss_rate`` (``None`` — type-illegal on a non-nullable metric —
    OR non-finite; the twin of ``brutex.ranking._junk_loss_rate``) returns the
    legacy floor as a SENTINEL that the kernel never consumes: its own
    junk-``loss_rate`` guard trips FIRST and fails the row closed as
    ``non_finite_input``, exactly like ``passes_gates`` short-circuits before
    Door 5.
    """
    from brutex.ranking import _MAX_CONSECUTIVE_LOSSES, streak_door_threshold

    lr = m.loss_rate
    if lr is None or not math.isfinite(lr):
        return _MAX_CONSECUTIVE_LOSSES
    return streak_door_threshold(m.total_trades, lr)


def _term_inputs_within_crate_envelope(survivors: Sequence[tuple[int, ResultMetrics]]) -> bool:
    """``True`` iff EVERY survivor's risk-efficiency operands fit the crate.

    The literal Python twin of the crate's ``cr_div_i64`` guard
    (``rust/fno-math/src/ranking.rs``) evaluated on the operands
    ``composite_term_inputs_core`` feeds it — ``avg_mfe_paisa`` over
    ``max(avg_mae_paisa, 1)``, magnitudes compared against
    :data:`_MAX_EXACT_DIV_OPERAND` (Rust's ``unsigned_abs()`` is Python's
    ``abs()`` on the same value, ``i64::MIN`` included).

    BATCH-level by construction, matching the kernel: ``composite_term_inputs``
    is one call over the whole column set and its ``?`` aborts the ENTIRE call
    on the first out-of-envelope row, so one such survivor puts the whole
    leaderboard's term step on the oracle either way. Screening here — rather
    than letting the kernel raise — is what separates this legitimate CLASS-C
    input from a CLASS-A broken crate (:data:`_TERM_INPUTS_BEYOND_CRATE`); it
    also skips a doomed FFI round trip. The served VALUE is the oracle's on
    both sides of the screen, so no ranked order and no persisted value moves.
    """
    limit = _MAX_EXACT_DIV_OPERAND
    for _, m in survivors:
        if abs(m.avg_mfe_paisa) > limit:
            return False
        if abs(max(m.avg_mae_paisa, 1)) > limit:
            return False
    return True


def _gate_verdicts(kernels: _Kernels, metrics_list: Sequence[ResultMetrics]) -> tuple[bool, ...]:
    """The batched gate verdicts under the ``ranking.gates`` burn-in family.

    Door 5 (the derived binomial-null streak door) + Door 6 (the opt-in
    SL-hit-rate ceiling) ride the fold-in-Rust/combine-in-Python split: the
    per-row ``streak_threshold`` is computed by the Python oracle
    (:func:`_row_streak_threshold`) and the ``loss_rate`` / ``sl_hit_rate``
    columns cross as-is, so the kernel does only host-invariant integer/finite
    comparisons and is bit-identical on every CI host. ``rank_combos`` never
    passes a Door-6 knob, so ``max_sl_hit_rate`` is ``None`` (Door 6 inert)
    here — byte-identical to ``passes_gates(m)``'s default.
    """
    from brutex.ranking import passes_gates

    n = len(metrics_list)
    return run_with_parity(
        _GATES,
        kernel=lambda: tuple(
            cast(
                list[bool],
                kernels.passes_gates(
                    [m.total_trades for m in metrics_list],
                    [m.profit_factor for m in metrics_list],
                    [m.max_drawdown_pct for m in metrics_list],
                    [m.win_rate for m in metrics_list],
                    [m.win_loss_ratio for m in metrics_list],
                    [m.max_consecutive_losses for m in metrics_list],
                    [_row_streak_threshold(m) for m in metrics_list],
                    [m.loss_rate for m in metrics_list],
                    [m.sl_hit_rate for m in metrics_list],
                    None,
                ),
            )
        ),
        oracle=lambda: tuple(passes_gates(m) for m in metrics_list),
        key=lambda verdicts: verdicts,
        validate=lambda verdicts: _is_bool_tuple(verdicts, n),
    )


def _overfit_gate_via_rust(
    candidates: Sequence[tuple[int, ResultMetrics]],
    survivors: list[tuple[int, ResultMetrics]],
    overfit: OverfitGateConfig,
    kernels: _Kernels,
) -> tuple[list[tuple[int, ResultMetrics]], list[float] | None]:
    """The ON-arm mirror of ``brutex.ranking._overfit_gate``.

    The orchestration stays Python — ``pool_sharpe_dispersion`` (exact
    rational, PR-2 territory) + the per-survivor ``deflated_sharpe_for``
    (the slice-6 seam) run VERBATIM; only the final ``passes_overfit_gate``
    verdicts ride the Rust kernel (class (a), burn-in). Abstains exactly
    like the Python original: an undeflatable pool passes *survivors*
    through unchanged with ``None`` DSRs.
    """
    pool_sharpes = [m.sharpe_ratio for _, m in candidates]
    dispersion = pool_sharpe_dispersion(pool_sharpes)
    if not dispersion.deflatable:
        return survivors, None
    n_trials = len(pool_sharpes)
    dsrs = [
        deflated_sharpe_for(
            m.sharpe_ratio,
            m.total_trades,
            m.skewness,
            m.kurtosis,
            n_trials=n_trials,
            trial_sharpe_variance=dispersion.variance,
            cfg=overfit,
        )
        for _, m in survivors
    ]
    n = len(dsrs)
    verdicts = run_with_parity(
        _OVERFIT,
        kernel=lambda: tuple(cast(list[bool], kernels.overfit_verdicts(dsrs, overfit.dsr_min))),
        oracle=lambda: tuple(passes_overfit_gate(dsr, overfit) for dsr in dsrs),
        key=lambda v: v,
        validate=lambda v: _is_bool_tuple(v, n),
    )
    # ONE strict zip for both projections (not two parallel comprehensions:
    # ``len(dsrs) == len(survivors)`` holds unconditionally, so a second
    # ``strict=True`` over the same pairing would be a tandem-redundant
    # guard whose ``strict=False`` mutant provably survives). A kept
    # survivor's DSR is guaranteed a float (a None DSR never passes the
    # gate) — the same cast discipline as the Python original.
    kept: list[tuple[int, ResultMetrics]] = []
    kept_dsrs: list[float] = []
    for pair, dsr, ok in zip(survivors, dsrs, verdicts, strict=True):
        if ok:
            kept.append(pair)
            kept_dsrs.append(cast(float, dsr))
    return kept, kept_dsrs


def rank_combos_via_rust(
    candidates: Sequence[tuple[int, ResultMetrics]],
    *,
    overfit: OverfitGateConfig | None = None,
) -> list[RankedCombo] | None:
    """Rank one leaderboard through the Rust kernels, or ``None`` → Python.

    The ON-arm twin of :func:`brutex.ranking.rank_combos` — the SAME
    pipeline shape with each converted step served under its parity family
    (module docstring). Returns ``None`` ONLY when the compiled kernels are
    unavailable (wheel missing/stale — the caller then runs the verbatim
    Python path); every other condition — a kernel boundary error, a
    refused result, a bit divergence — is absorbed INSIDE the pipeline by
    serving that step from the Python oracle, so the returned leaderboard
    is always bit-identical to the Python arm's.

    Raises exactly like the Python original: ``ValueError`` on a duplicate
    gate-passing survivor mask (the SHARED ``ensure_unique_survivor_masks``
    — byte-identical message).
    """
    from brutex.ranking import (
        N_COMPOSITE_TERMS,
        RankedCombo,
        composite_scores,
        composite_term_inputs,
        dsr_z_contributions,
        ensure_unique_survivor_masks,
    )

    kernels = _resolve_kernels()
    if kernels is None:
        return None

    metrics_list = [m for _, m in candidates]
    verdicts = _gate_verdicts(kernels, metrics_list)
    survivors = [pair for pair, ok in zip(candidates, verdicts, strict=True) if ok]

    # No inline ``list[float] | None`` annotation — a local annotation is
    # runtime-unevaluated (PEP 526/563), so its ``| → &`` mutant provably
    # survives the mutmut gate; both branches let mypy infer the union.
    if overfit is not None and survivors:
        survivors, dsr_by_survivor = _overfit_gate_via_rust(
            candidates, list(survivors), overfit, kernels
        )
    else:
        dsr_by_survivor = None

    if not survivors:
        return []

    n = len(survivors)

    def term_oracle() -> list[list[float]]:
        return [list(composite_term_inputs(m)) for _, m in survivors]

    if _term_inputs_within_crate_envelope(survivors):
        term_rows = run_with_parity(
            _TERMS,
            kernel=lambda: cast(
                list[list[float]],
                kernels.composite_term_inputs(
                    [m.sharpe_ratio for _, m in survivors],
                    [m.sortino_ratio for _, m in survivors],
                    [m.calmar_ratio for _, m in survivors],
                    [m.profit_factor for _, m in survivors],
                    [m.recovery_factor for _, m in survivors],
                    [m.win_rate for _, m in survivors],
                    [m.win_loss_ratio for _, m in survivors],
                    [m.avg_mfe_paisa for _, m in survivors],
                    [m.avg_mae_paisa for _, m in survivors],
                ),
            ),
            oracle=term_oracle,
            key=pack_float_rows,
            validate=lambda rows: _is_term_rows(rows, n, N_COMPOSITE_TERMS),
        )
    else:
        # CLASS C — an out-of-envelope risk-efficiency operand. A legitimate
        # input the Python oracle has always served; NEVER a broken crate, so
        # it is exempt from the strict refusal (_TERM_INPUTS_BEYOND_CRATE).
        _TERMS.warn_fallback_once(_TERM_INPUTS_BEYOND_CRATE)
        term_rows = term_oracle()
    term_columns = tuple([row[term] for row in term_rows] for term in range(N_COMPOSITE_TERMS))

    if dsr_by_survivor is None:
        # The inactive 8th-term arm — zero float arithmetic, stays Python.
        dsr_contrib: list[float] = [0.0] * n
    else:
        dsr_values = dsr_by_survivor
        # ``overfit`` is non-None exactly when ``dsr_by_survivor`` is a list
        # (the gate ran and was deflatable) — the rank_combos invariant.
        overfit_cfg = cast(OverfitGateConfig, overfit)
        dsr_contrib = run_with_parity(
            _DSR_Z,
            kernel=lambda: cast(
                list[float],
                kernels.dsr_z_contributions(dsr_values, overfit_cfg.weight),
            ),
            oracle=lambda: dsr_z_contributions(dsr_values, n, overfit_cfg),
            key=pack_floats,
            validate=lambda values: _is_float_list(values, n),
        )

    composites = run_with_parity(
        _COMPOSITE,
        kernel=lambda: cast(
            list[float],
            kernels.composite_scores([list(column) for column in term_columns], dsr_contrib),
        ),
        oracle=lambda: composite_scores(term_columns, dsr_contrib),
        key=pack_floats,
        validate=lambda values: _is_float_list(values, n),
    )

    ranked = [
        RankedCombo(
            mask=mask,
            metrics=m,
            composite_score=composites[i],
            deflated_sharpe=None if dsr_by_survivor is None else dsr_by_survivor[i],
        )
        for i, (mask, m) in enumerate(survivors)
    ]
    # The SHARED unique-mask guard — byte-identical ValueError to the
    # Python arm (DEC-RANKING-TIEBREAK-001).
    masks = [rc.mask for rc in ranked]
    ensure_unique_survivor_masks(masks)
    order = run_with_parity(
        _ORDER,
        kernel=lambda: tuple(cast(list[int], kernels.rank_order(composites, masks))),
        oracle=lambda: tuple(sorted(range(n), key=lambda i: (-composites[i], masks[i]))),
        key=lambda perm: perm,
        validate=lambda perm: _is_permutation(perm, n),
    )
    return [ranked[i] for i in order]


__all__ = [
    "RANKING_RUST_DEFAULT",
    "RANKING_RUST_ENV",
    "rank_combos_via_rust",
    "ranking_rust_enabled",
    "reset_ranking_rust_for_tests",
]
