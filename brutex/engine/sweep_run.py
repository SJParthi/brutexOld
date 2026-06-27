"""Persisted + deduped + audited single-(instrument, TF) brute-force sweep.

Phase 3 Slice-1 (DEC-SWEEP-RUN-001): the orchestration seam that turns *"sweep
the K-of-my-chosen-conditions mask space for ONE (instrument, TF, direction,
params)"* into a **reproducible, idempotent, audited** leaderboard on disk. It
**composes** the golden-locked engine bricks — it re-implements none of them
(CLAUDE.md §11 COMMON):

    bars --build_bar_bits--> bar_bits                       (Stage-1A, ONCE)
    condition_bits --iter_masks_k_of_bits--> masks          (combo enumeration)
         --has_conflict--> prune degenerate pairs           (vocabulary)
         --mask_run_id (combo_hash)--> run-identity          (Golden Rule #4)
         --ComboDedup.seen--> skip already-evaluated         (Bloom→exact, O(1))
         --count_supermasks--> Stage-1B min-hits prefilter   (combo_mask)
         --metrics_for_mask--> (mask, ResultMetrics)         (sweep_driver)
         --rank_combos--> RankedCombo leaderboard            (ranking)
         --ranked_combo_to_result_row--> ResultRow           (the projector)
         --ResultsStore.append/flush--> Parquet sink         (results_store)

Three invariants make this a *sweep RUN*, not just a sweep:

* **Reproducibility (Golden Rule #4).** Every mask's identity is the 9-field
  BLAKE3 :func:`~brutex.strategies.combo.combo_hash` with
  ``strategy_id = "mask:%016x"`` of the u64 mask (DEC-MASK-IDENTITY-001 — the
  mask *is* the strategy). Same inputs → same run-ids → same
  :class:`~brutex.engine.results_store.FlushResult.content_hash`, bit-for-bit.
* **Idempotence (Golden Rule #7).** On the digest path (the production
  launcher), redo is decided by the per-tuple COMPLETION SENTINEL
  (:mod:`brutex.engine.tuple_complete`, DEC-MARK-MANY-SENTINEL-001): a paired
  sentinel short-circuits the whole tuple; an absent/unpaired sentinel forces
  a FULL re-evaluation with dedup probes BYPASSED — never a subset re-rank —
  then rewrites every artifact byte-identically. This closed the
  partial-``mark_many`` redo hole (MIGRATION.md #650): the per-key mark loop
  is not atomic, so its marks are ADVISORY, never the redo authority. Legacy
  no-digest callers keep the probe semantics: a re-run against the same dedup
  state skips every previously-evaluated mask (Bloom→exact-confirm, O(1) per
  mask, BEFORE the expensive trade sim) and re-produces a byte-identical
  store; dedup keys are marked seen **after** the store flush (redo-not-skip
  — never a silent hole).
* **Audit trail (Golden Rule #8).** Exactly THREE hash-chained audit lines per
  full sweep — ``SWEEP_START`` / ``SWEEP_FLUSH`` / ``SWEEP_COMPLETE`` — never
  one per combo (the audit cost is O(1) in the combo count, not O(N)). A
  sentinel short-circuit emits exactly ONE line (``SWEEP_RESUMED``).

Honest envelope (Golden Rule #10): :func:`run_persisted_sweep` refuses to
enumerate more than ``cfg.max_combos`` masks (raising before any ``bar_bits``
build), the same opt-in guardrail :func:`brutex.engine.sweep_driver.rank_sweep_over_bits`
uses — an oversized sweep is a conscious choice, not an accident.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.data.models import CandleBar
from brutex.engine.combo_mask import (
    MAX_VOCAB_BITS,
    count_combos,
    count_supermasks,
    iter_masks_k_of_bits,
    mask_to_entry_signals,
)
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.sweep_driver import (
    DEFAULT_MAX_COMBOS,
    DEFAULT_START_EQUITY_PAISA,
    metrics_for_mask,
)
from brutex.engine.trade_sim import simulate_trades
from brutex.engine.trial_stats import (
    TrialStatsError,
    read_tuple_trial_stats,
    trial_stats_sidecar_path,
    tuple_trial_stats_from_sharpes,
    write_tuple_trial_stats,
)
from brutex.engine.tuple_complete import (
    TupleCompletionError,
    TupleCompletionSentinel,
    completion_sentinel_path,
    read_completion_sentinel,
    write_completion_sentinel,
)
from brutex.metrics.compute import ResultMetrics
from brutex.models import BruteXError
from brutex.money import round_money
from brutex.ranking import MIN_TRADES, OverfitGateConfig, RankedCombo, rank_combos
from brutex.strategies.combo import combo_hash
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams, has_conflict

if TYPE_CHECKING:
    # Annotation-only imports — kept out of the runtime import graph so the
    # `fastbloom_rs` PyO3 wheel (pulled in transitively by both modules below
    # via brutex.orchestrator.bloom) never enters brutex.engine's import chain.
    # That single-init-per-interpreter wheel breaks the strict per-module
    # coverage gates for the other brutex.engine.* modules otherwise (the same
    # constraint recorded in DEC-COV-BACKFILL-LIMIT-001). ``ComboDedup`` only
    # *receives* an already-built OptionDedupIndex; the runner only *receives*
    # an already-open AuditWriter — neither is constructed here.
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.sweep_kernel import ResidentSweepKernel
    from brutex.orchestrator.audit import AuditWriter

_LOG = structlog.get_logger("brutex.engine.sweep_run")

#: Audit action labels — the three lines a full sweep emits, plus the single
#: line a sentinel short-circuit emits (DEC-MARK-MANY-SENTINEL-001).
_ACTION_START: Final[str] = "SWEEP_START"
_ACTION_FLUSH: Final[str] = "SWEEP_FLUSH"
_ACTION_COMPLETE: Final[str] = "SWEEP_COMPLETE"
_ACTION_RESUMED: Final[str] = "SWEEP_RESUMED"


class SweepComboError(BruteXError):
    """One combo's metric evaluation raised — names the offending mask + run-id + tuple.

    A raise here is treated CONSERVATIVELY as tuple-wide (the safe redo-everything
    choice): the only per-combo-varying input to `metrics_for_mask` is the mask, so a
    mask-specific edge cannot be ruled out — skipping the whole tuple is the conservative
    response. The grid drivers SKIP the whole tuple (deterministic redo next run), never
    the one mask.
    """


def _direction_label(direction: Side) -> str:
    """Map a :class:`~brutex.strategies.types.Side` to its sweep direction label.

    Parameters
    ----------
    direction
        :data:`~brutex.strategies.types.Side.BUY` (long) or ``Side.SELL`` (short).

    Returns
    -------
    str
        ``"bull"`` for ``BUY``, ``"bear"`` for ``SELL`` — the labels the combo
        identity + result row carry (matching :func:`brutex.strategies.combo.combo_hash`).

    Raises
    ------
    ValueError
        If *direction* is :data:`~brutex.strategies.types.Side.HOLD` — a sweep
        backtests a directional edge; ``HOLD`` is not a trade direction.
    """
    if direction is Side.BUY:
        return "bull"
    if direction is Side.SELL:
        return "bear"
    raise ValueError(f"sweep direction must be Side.BUY or Side.SELL, not {direction!r}")


class ComboDedup:
    """O(1) combo run-identity dedup — a thin adapter over an :class:`OptionDedupIndex`.

    The sweep dedups on the 32-byte BLAKE3 run-identity of each mask (the raw
    bytes of :func:`mask_run_id`'s hex). This is a **composition** of the
    golden-locked :class:`~brutex.data.option_dedup.OptionDedupIndex` (its
    Bloom→exact-confirm, thread-safe two-tier logic is reused verbatim, NOT
    re-implemented) — this class only re-keys from a hex string to the raw bytes
    the index consumes (CLAUDE.md §11 COMMON).

    Parameters
    ----------
    index
        The shared dedup index. Inject one built from a
        :class:`~brutex.orchestrator.bloom.BloomFilter` + an
        :class:`~brutex.data.lmdb_membership.LmdbMembership` (persistent) or a
        plain ``set`` (in-memory) per the run's recoverability needs.
    """

    def __init__(self, index: OptionDedupIndex) -> None:
        self._index = index

    def seen(self, run_id_hex: str) -> bool:
        """Return ``True`` iff *run_id_hex* was already marked — O(1), atomic.

        Parameters
        ----------
        run_id_hex
            A 64-char lowercase BLAKE3 hex run-identity (from :func:`mask_run_id`).

        Returns
        -------
        bool
            ``True`` if the identity is present (Bloom-positive **and**
            exact-confirmed), else ``False`` (Bloom-negative is definitively new).
        """
        return self._index.seen(bytes.fromhex(run_id_hex))

    def mark(self, run_id_hex: str) -> None:
        """Record *run_id_hex* as seen in both dedup tiers — O(1), atomic.

        Parameters
        ----------
        run_id_hex
            A 64-char lowercase BLAKE3 hex run-identity to mark.
        """
        self._index.add(bytes.fromhex(run_id_hex))

    def mark_many(self, run_id_hexes: Iterable[str]) -> int:
        """Mark every identity in *run_id_hexes* as seen; return how many were marked.

        Idempotent: re-marking an already-seen identity is a harmless no-op that
        still counts toward the returned total (it reflects identities processed,
        not net-new insertions — the underlying tiers absorb duplicates).

        Parameters
        ----------
        run_id_hexes
            Iterable of 64-char lowercase BLAKE3 hex run-identities.

        Returns
        -------
        int
            The number of identities marked.
        """
        count = 0
        for run_id_hex in run_id_hexes:
            self._index.add(bytes.fromhex(run_id_hex))
            count += 1
        return count


def mask_run_id(
    *,
    mask: int,
    direction: str,
    instrument: str,
    tf: str,
    mode: str,
    params: Mapping[str, Any],
    data_snapshot_hash: str,
    indicator_version: str,
    code_commit_sha: str,
) -> str:
    """Return the 9-field BLAKE3 run-identity of one strategy *mask* (Golden Rule #4).

    DEC-MASK-IDENTITY-001: the u64 mask **is** the strategy identity, encoded as
    the ``strategy_id`` ``"mask:%016x"`` (16 lowercase hex chars, zero-padded);
    the remaining eight fields are forwarded unchanged to
    :func:`brutex.strategies.combo.combo_hash`. Two masks share a run-identity
    iff all nine inputs collide, so the same mask under the same context + params
    + data + code always hashes to the same id (reproducibility) and a different
    mask never aliases it.

    Parameters
    ----------
    mask
        The u64 strategy mask. Encoded as ``"mask:%016x"`` for the ``strategy_id``.
    direction
        Direction label (``"bull"`` / ``"bear"``); non-empty.
    instrument, tf, mode
        Context labels (e.g. ``"NIFTY"``, ``"5m"``, ``"A"``); each non-empty.
    params
        Canonical parameter dict (sl/tp/prior_n/proximity/equity) —
        canonicalised deterministically by
        :func:`brutex.strategies.combo.canonicalise_params`.
    data_snapshot_hash, indicator_version, code_commit_sha
        The data-slice / indicator-stack / code-commit identity; each non-empty.

    Returns
    -------
    str
        The 64-char lowercase BLAKE3 hex run-identity.

    Raises
    ------
    brutex.strategies.errors.StrategyError
        If any required string field is empty (delegated to
        :func:`brutex.strategies.combo.combo_hash`).
    """
    return combo_hash(
        strategy_id=f"mask:{mask:016x}",
        direction=direction,
        instrument=instrument,
        tf=tf,
        mode=mode,
        params=params,
        data_snapshot_hash=data_snapshot_hash,
        indicator_version=indicator_version,
        code_commit_sha=code_commit_sha,
    )


@dataclass(frozen=True, slots=True)
class SweepRunConfig:
    """Immutable configuration for one persisted sweep run.

    Captures everything the sweep needs that is NOT the bars themselves: the
    mask space (``condition_bits`` + ``k``), the trade-sim knobs, the run-identity
    context (instrument / tf / direction / mode + the three reproducibility
    hashes), and the honest combo-count cap. Validated at construction so an
    impossible sweep fails fast.

    Attributes
    ----------
    instrument, tf, mode
        Context labels stamped on the run identity + result rows; each non-empty.
    direction
        Trade direction (:data:`~brutex.strategies.types.Side.BUY` or ``SELL``;
        ``HOLD`` is rejected by :func:`run_persisted_sweep`, not here, so the
        config stays a pure data holder).
    condition_bits
        The condition bit positions (of the 52) to brute-force over; duplicates
        are ignored (set semantics) for both the enumeration and the ``k`` bound.
    k
        How many of the chosen conditions each combo requires
        (``0 <= k <= n_unique``).
    data_snapshot_hash, indicator_version, code_commit_sha
        The three reproducibility identity fields; each non-empty.
    tf_minutes
        Bar timeframe in whole minutes (``>= 1``).
    sl_paisa, tp_paisa
        Stop-loss / take-profit distances in paisa (``> 0``), or ``None`` to
        disable that leg.
    prior_n
        Prior-bar pattern look-back ∈ {1, 2, 3, 4} (passed to Stage-1A).
    proximity
        Proximity tolerances for the ``near_*`` conditions, or ``None`` for the
        Stage-1A default (10 bps).
    start_equity_paisa
        Notional capital base (paisa) for the percentage-metric denominator.
    max_combos
        Refuse to enumerate more than this many masks (the honest opt-in guard).
    top_k
        Persist only the top ``top_k`` ranked combos, or ``None`` for all ranked.
    min_hits
        Stage-1B coarse pre-filter threshold (defaults to
        :data:`brutex.ranking.MIN_TRADES`); a mask firing on fewer bars can never
        be rankable, so it is pruned before the trade sim. ``0`` disables it.
    overfit
        Opt-in :class:`~brutex.ranking.overfit.OverfitGateConfig` activating the
        Stage-3 multiple-testing **overfit gate** (6th gate + Deflated-Sharpe
        ranking term, DEC-RANKING-OVERFIT-001) inside :func:`brutex.ranking.rank_combos`.
        ``None`` (the default) keeps the legacy five-gate / seven-term ranking. The
        leaderboard's trial count ``N`` is this tuple's backtested-combo count —
        the authoritative per-(instrument, TF) ``N`` — so the persisted store is, by
        construction, the overfit-gated output (see the ``overfit`` module's honest
        envelope: no statistically-valid post-store re-gate exists).

    Raises
    ------
    ValueError
        If a context / identity string is empty, ``tf_minutes < 1``, ``k`` is
        outside ``[0, n_unique]``, or a paisa knob is non-positive.
    """

    instrument: str
    tf: str
    direction: Side
    mode: str
    condition_bits: Sequence[int]
    k: int
    data_snapshot_hash: str
    indicator_version: str
    code_commit_sha: str
    tf_minutes: int
    sl_paisa: int | None = None
    tp_paisa: int | None = None
    prior_n: int = 3
    proximity: ProximityParams | None = None
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA
    max_combos: int = DEFAULT_MAX_COMBOS
    top_k: int | None = None
    min_hits: int = MIN_TRADES
    overfit: OverfitGateConfig | None = None

    def __post_init__(self) -> None:
        for name in ("instrument", "tf", "mode"):
            if not getattr(self, name):
                raise ValueError(f"SweepRunConfig.{name} must be non-empty")
        for name in ("data_snapshot_hash", "indicator_version", "code_commit_sha"):
            if not getattr(self, name):
                raise ValueError(f"SweepRunConfig.{name} must be non-empty")
        if self.tf_minutes < 1:
            raise ValueError(f"SweepRunConfig.tf_minutes must be >= 1, got {self.tf_minutes}")
        # Validate every condition bit is in the u64 vocabulary range AT CONFIG
        # TIME (DEC-SWEEP-DEDUP-LMDB-001): an out-of-range bit otherwise slips
        # past the k-bound check and fails LATE as a bare ValueError raised from
        # the lazy ``iter_masks_k_of_bits`` generator mid-enumeration, aborting
        # the whole grid. Reject it here (the fail-fast contract this class
        # promises) so a bad combo is caught — and tuple-skipped — at planning.
        out_of_range = sorted({bit for bit in self.condition_bits if not 0 <= bit < MAX_VOCAB_BITS})
        if out_of_range:
            raise ValueError(
                f"SweepRunConfig.condition_bits {out_of_range} out of range [0, {MAX_VOCAB_BITS})"
            )
        n_unique = len(set(self.condition_bits))
        if not 0 <= self.k <= n_unique:
            raise ValueError(
                f"SweepRunConfig.k={self.k} must be in [0, {n_unique}] "
                f"({n_unique} unique condition bits)"
            )
        for name in ("sl_paisa", "tp_paisa"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"SweepRunConfig.{name} must be > 0 or None, got {value}")
        if self.start_equity_paisa <= 0:
            raise ValueError(
                f"SweepRunConfig.start_equity_paisa must be > 0, got {self.start_equity_paisa}"
            )
        if self.top_k is not None and self.top_k < 1:
            raise ValueError(f"SweepRunConfig.top_k must be >= 1 or None, got {self.top_k}")
        if self.min_hits < 0:
            raise ValueError(f"SweepRunConfig.min_hits must be >= 0, got {self.min_hits}")


@dataclass(frozen=True, slots=True)
class SweepRunSummary:
    """The tally a :func:`run_persisted_sweep` returns (and stamps into the audit).

    Attributes
    ----------
    enumerated
        Total masks yielded by the K-of-bits enumeration.
    conflict_pruned
        Of those, how many carried a degenerate conflict pair (pruned, never
        evaluated).
    skipped_dedup
        Of the non-conflict masks, how many were already-seen (skipped before
        evaluation — the idempotence path).
    evaluated
        Non-conflict, non-dedup masks whose fate this run decided (their identity
        is marked seen so a re-run skips them) — includes those the Stage-1B
        pre-filter dropped.
    ranked
        Surviving combos that cleared the hard rejection gates — and, when
        ``cfg.overfit`` is set, the Stage-3 multiple-testing overfit gate too (the
        leaderboard size before ``top_k`` truncation).
    persisted
        Ranked finalists appended to the store (``ranked`` truncated to
        ``cfg.top_k``).
    store_content_hash
        The :class:`~brutex.engine.results_store.FlushResult` content hash — the
        sweep's reproducible content address.
    """

    enumerated: int
    conflict_pruned: int
    skipped_dedup: int
    evaluated: int
    ranked: int
    persisted: int
    store_content_hash: str
    #: Combos that actually ran ``metrics_for_mask`` (the Bailey-LdP trial
    #: population — ADR-0008 condition 3); ``evaluated - minhits_dropped``.
    backtested: int = 0
    #: Stage-1B drops among the evaluated survivors (audit context).
    minhits_dropped: int = 0
    #: Finite-Sharpe trials + their fsum moments (the sidecar's V inputs;
    #: zero when no sidecar was written this run).
    trial_n_finite: int = 0
    trial_sum_sharpe: float = 0.0
    trial_sum_sharpe_sq: float = 0.0
    #: ``True`` iff this run wrote/overwrote the tuple's trial-stats sidecar.
    #: With a digest supplied this is every non-short-circuited run: the
    #: sentinel-authoritative redo BYPASSES dedup probes, so the population is
    #: always FULL — ADR-0008 condition 2's preserve-on-partial-resume rule is
    #: now enforced structurally (a partial population cannot occur on the
    #: digest path; DEC-MARK-MANY-SENTINEL-001).
    trial_sidecar_written: bool = False
    #: ``True`` iff the tuple was short-circuited by a PAIRED completion
    #: sentinel (DEC-MARK-MANY-SENTINEL-001): nothing was evaluated, flushed,
    #: written, or marked — every other tally is zero and
    #: ``store_content_hash`` echoes the sentinel's recorded hash.
    resumed_complete: bool = False
    #: ``True`` iff this run wrote the tuple's completion sentinel (strictly
    #: last, after ``mark_many``).
    sentinel_written: bool = False


def canonical_trade_params(
    *,
    sl_paisa: int | None,
    tp_paisa: int | None,
    prior_n: int,
    proximity: ProximityParams | None,
    start_equity_paisa: int,
) -> dict[str, Any]:
    """Return the deterministic trade-sim params dict for the run-identity hash.

    Bundles the trade-sim knobs into one JSON-canonicalisable dict (no floats,
    no objects — only ints / ``None``) so
    :func:`brutex.strategies.combo.canonicalise_params` can hash it stably. The
    proximity tolerances are flattened to their three integer bps values (or
    ``None`` when defaulted) so two configs with the same effective proximity
    hash identically.

    This is the ONE source of the params layout inside every persisted mask
    run-identity (CLAUDE.md §11 COMMON): the sweep hashes through it via
    :func:`_canonical_params`, and the Stage-3 wiring re-derives survivor
    identities through it to recover each survivor's grid cell + proximity by
    ``run_id`` match (DEC-STAGE3-WIRE-001, hostile-review condition F4 — never
    guessed, always re-derived).
    """
    if proximity is None:
        proximity_bps: list[int] | None = None
    else:
        proximity_bps = [
            proximity.pivot_proximity_bps,
            proximity.pdh_pdl_proximity_bps,
            proximity.fib_proximity_bps,
        ]
    return {
        "sl_paisa": sl_paisa,
        "tp_paisa": tp_paisa,
        "prior_n": prior_n,
        "proximity_bps": proximity_bps,
        "start_equity_paisa": start_equity_paisa,
    }


def _canonical_params(cfg: SweepRunConfig) -> dict[str, Any]:
    """Project *cfg*'s trade-sim knobs through :func:`canonical_trade_params`."""
    return canonical_trade_params(
        sl_paisa=cfg.sl_paisa,
        tp_paisa=cfg.tp_paisa,
        prior_n=cfg.prior_n,
        proximity=cfg.proximity,
        start_equity_paisa=cfg.start_equity_paisa,
    )


def ranked_combo_to_result_row(
    rc: RankedCombo,
    *,
    run_id: str,
    instrument: str,
    tf: str,
    direction: str,
    mode: str,
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    tf_minutes: int,
    sl_paisa: int | None,
    tp_paisa: int | None,
) -> ResultRow:
    """Project a :class:`~brutex.ranking.RankedCombo` into a storable :class:`ResultRow`.

    The seam between the ranked leaderboard (rich metrics, no storage discipline)
    and the deterministic store (money/metric/price families + the readable-mask
    surface). The winning combo is stamped two ways (DEC-SWEEP-WINNERS-BY-NAME-001):
    ``mask_bits=rc.mask`` (the machine-readable u64) and — derived from it inside
    :class:`ResultRow` via the single-source-of-truth vocabulary name table — the
    human-readable ``condition_names``; ``rc.deflated_sharpe`` is carried through for
    leaderboard transparency (``None`` when the pool was not deflatable). The rest of
    the mapping is mechanical except two deliberate choices:

    * **``None`` metrics → ``0.0``.** ``ResultMetrics`` carries ``sortino_ratio``
      / ``calmar_ratio`` / ``profit_factor`` as ``float | None`` (mathematically
      undefined for the sample), but ``ResultRow``'s metric columns are
      non-optional ``float``. A ranked combo has already cleared the gates (so
      ``profit_factor`` is real), but the others can still be ``None``; they map
      to ``0.0`` (documented, not silent). The full-precision real values are
      preserved unchanged.
    * **Representative price anchors from REAL OHLC (Golden Rule #1).**
      ``ResultMetrics`` carries no per-trade fill prices, so the finalist's
      trades are re-simulated here and ``avg_entry_price`` / ``avg_exit_price``
      are the mean of the real ``open`` (at each trade's entry bar) / ``close``
      (at each trade's exit bar), snapped to the 2dp tick grid via
      :func:`brutex.money.round_money`. These are HONEST anchors drawn from real
      bars, never fabricated. Threading the exact fill price through
      :class:`~brutex.metrics.types.ClosedTrade` is a tracked follow-up. A
      sweep-ranked combo always has ``>= MIN_TRADES`` trades; a Stage-3 refined
      evaluation (DEC-STAGE3-WIRE-001) is UNGATED and may have zero trades, in
      which case both anchors are the 0.00 placeholder
      (``round_money(Decimal("0"))``) — documented, not fabricated.

    Parameters
    ----------
    rc
        The ranked combo to project (its ``mask`` + full ``metrics``).
    run_id
        The 64-char BLAKE3 run-identity for ``rc.mask`` (the store dedup key).
    instrument, tf, direction, mode
        Context labels carried onto the row.
    bars, bar_bits
        The same bars + pre-built Stage-1A matrix the sweep used (for the price
        re-simulation; ``bar_bits`` is reused, not rebuilt).
    tf_minutes, sl_paisa, tp_paisa
        Trade-sim knobs — must match the sweep's so the re-simulated trades
        reproduce the ranked combo's trades exactly.

    Returns
    -------
    ResultRow
        The validated, price-snapped row ready for
        :meth:`brutex.engine.results_store.ResultsStore.append`.
    """
    m = rc.metrics
    side = Side.BUY if direction == "bull" else Side.SELL
    trades = simulate_trades(
        bars,
        mask_to_entry_signals(bar_bits, rc.mask),
        direction=side,
        tf_minutes=tf_minutes,
        stop_loss_paisa=sl_paisa,
        take_profit_paisa=tp_paisa,
    )
    if trades:
        ts_to_bar = {bar.timestamp: bar for bar in bars}
        entries = [ts_to_bar[t.entry_ts].open for t in trades]
        exits = [ts_to_bar[t.exit_ts].close for t in trades]
        avg_entry = round_money(sum(entries, Decimal("0")) / len(entries))
        avg_exit = round_money(sum(exits, Decimal("0")) / len(exits))
    else:
        # REACHABLE since DEC-STAGE3-WIRE-001: a Stage-3 refined evaluation is
        # an UNGATED Bailey-LdP trial (ADR-0007 condition 6) and may close zero
        # trades on the training slice; its price anchors are then a 0.00
        # placeholder pair (a sweep-ranked combo still always has >= MIN_TRADES).
        avg_entry = round_money(Decimal("0"))
        avg_exit = round_money(Decimal("0"))

    return ResultRow(
        run_id=run_id,
        instrument=instrument,
        tf=tf,
        direction=direction,
        mode=mode,
        mask_bits=rc.mask,
        deflated_sharpe=rc.deflated_sharpe,
        net_pnl_paisa=m.net_pnl_paisa,
        gross_profit_paisa=m.gross_profit_paisa,
        gross_loss_paisa=abs(m.gross_loss_paisa),
        max_drawdown_paisa=abs(m.max_drawdown_paisa),
        expectancy_paisa=m.expectancy_paisa,
        total_trades=m.total_trades,
        winning_trades=m.winning_trades,
        losing_trades=m.losing_trades,
        sharpe_ratio=_metric_or_zero(m.sharpe_ratio),
        sortino_ratio=_metric_or_zero(m.sortino_ratio),
        calmar_ratio=_metric_or_zero(m.calmar_ratio),
        profit_factor=_metric_or_zero(m.profit_factor),
        win_rate=m.win_rate,
        max_drawdown_pct=m.max_drawdown_pct,
        avg_entry_price=avg_entry,
        avg_exit_price=avg_exit,
    )


def _metric_or_zero(value: float | None) -> float:
    """Map a ``float | None`` metric to a non-optional float (``None`` → ``0.0``).

    ``ResultMetrics`` ratios are ``None`` when mathematically undefined for the
    sample; the ``ResultRow`` schema's metric columns are non-optional, so a
    ``None`` becomes ``0.0`` (documented in :func:`ranked_combo_to_result_row`).
    Real values pass through with full precision.
    """
    return value if value is not None else 0.0


def _sentinel_resume(store_path: Path, digest: str) -> TupleCompletionSentinel | None:
    """Return the tuple's PAIRED completion sentinel, or ``None`` to force a full redo.

    The redo decision authority (DEC-MARK-MANY-SENTINEL-001): COMPLETE iff the
    sentinel exists, reads valid, carries this tuple's *digest*, its recorded
    ``store_content_hash`` matches a re-hash of the actual store bytes, and —
    when it records a written trial-stats sidecar — that sidecar still reads
    and pairs. ANY failure is logged loudly (``sweep_run.sentinel_unpaired``)
    and returns ``None``: the caller re-runs the FULL population and rewrites
    every artifact, so a stale/tampered/corrupt state self-heals rather than
    being falsely skipped or crashing the grid.
    """
    path = completion_sentinel_path(store_path)
    if not path.exists():
        return None

    def _unpaired(reason: str) -> None:
        _LOG.warning("sweep_run.sentinel_unpaired", path=str(path), reason=reason)

    try:
        sentinel = read_completion_sentinel(path)
    except TupleCompletionError as exc:
        _unpaired(f"unreadable sentinel: {exc}")
        return None
    if sentinel.tuple_digest != digest:
        _unpaired("tuple digest mismatch")
        return None
    try:
        content = store_path.read_bytes()
    except OSError as exc:
        _unpaired(f"store unreadable: {exc}")
        return None
    if hashlib.sha256(content).hexdigest() != sentinel.store_content_hash:
        _unpaired("store content hash mismatch")
        return None
    if sentinel.sidecar_written:
        try:
            stats = read_tuple_trial_stats(trial_stats_sidecar_path(store_path))
        except TrialStatsError as exc:
            _unpaired(f"sidecar unreadable: {exc}")
            return None
        if stats.tuple_digest != digest or stats.store_content_hash != sentinel.store_content_hash:
            _unpaired("sidecar pairing mismatch")
            return None
    return sentinel


def run_persisted_sweep(
    bars: Sequence[CandleBar],
    cfg: SweepRunConfig,
    *,
    store: ResultsStore,
    dedup: ComboDedup,
    audit: AuditWriter,
    kernel: ResidentSweepKernel | None = None,
    trial_stats_digest: str | None = None,
) -> SweepRunSummary:
    """Run a reproducible, deduped, audited K-of-bits sweep over ONE (instrument, TF).

    The orchestrator that composes the engine bricks (see the module docstring
    for the data flow). Sequence of operations:

    1. Validate (non-empty bars; ``HOLD`` rejected; combo count within
       ``cfg.max_combos`` — the honest opt-in guard, raising BEFORE the Stage-1A
       build).
    2. **Completion-sentinel check** (digest path only,
       DEC-MARK-MANY-SENTINEL-001): a PAIRED sentinel proves the tuple's full
       write sequence completed — emit one ``SWEEP_RESUMED`` audit line and
       short-circuit, touching nothing. Absent/unpaired → fall through to a
       FULL re-run with dedup probes BYPASSED (the marks are advisory; a
       partially-marked dedup must never produce a subset re-rank —
       MIGRATION.md #650).
    3. Emit the ``SWEEP_START`` audit line.
    4. Build the Stage-1A ``bar_bits`` **once**.
    5. Enumerate the K-of-bits masks; for each: prune conflicts; on the legacy
       no-digest path, skip already-seen identities (O(1) Bloom→exact, BEFORE
       any trade sim); else collect the ``(mask, run_id)`` survivor.
    6. Stage-1B coarse pre-filter (``cfg.min_hits``): drop survivors that fire on
       too few bars to ever rank (still counted as ``evaluated`` + marked seen).
    7. Backtest the kept survivors (:func:`~brutex.engine.sweep_driver.metrics_for_mask`),
       rank them (:func:`brutex.ranking.rank_combos`), take the top ``cfg.top_k``.
    8. Project each finalist to a :class:`ResultRow` and append to the store.
    9. Flush the store; write the trial-stats sidecar (digest path); mark
       **all** evaluated identities seen; then write the completion sentinel
       STRICTLY LAST (digest path) — its presence proves the whole sequence.
    10. Emit ``SWEEP_FLUSH`` + ``SWEEP_COMPLETE`` audit lines.

    Exactly three audit lines per full sweep regardless of combo count (Golden
    Rule #8 — O(1) audit cost); one line for a sentinel short-circuit. The
    store receives only finalists.

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)``
        (non-empty).
    cfg
        The validated :class:`SweepRunConfig`.
    store
        The destination :class:`~brutex.engine.results_store.ResultsStore`
        (finalist sink).
    dedup
        The :class:`ComboDedup` carrying the run's cross-run dedup state.
    audit
        An open :class:`~brutex.orchestrator.audit.AuditWriter`.
    kernel
        Optional pre-built :class:`~brutex.engine.sweep_kernel.ResidentSweepKernel`
        for this tuple's ``(instrument, tf, proximity, prior_n)`` bar window
        (DEC-SWEEP-RESIDENT-KERNEL-001). When supplied, the Stage-1A ``bar_bits``
        build is skipped (its pre-built matrix is reused) and the Stage-1B count is
        served from the resident Rust buffer (no bar re-marshal) — the
        :func:`~brutex.engine.sweep_orchestrator.run_sweep_grid` reuse path. The
        result is **byte-identical** to the ``kernel=None`` path (both delegate to
        the same SUPERSET kernel; Golden Rule #4); only redundant rebuild/re-marshal
        work is removed. ``None`` (default) builds + counts inline as before. The
        kernel MUST be built from the SAME ``bars`` (a length mismatch raises).
    trial_stats_digest
        The tuple's 16-hex path digest, enabling the SENTINEL-AUTHORITATIVE
        redo protocol (DEC-MARK-MANY-SENTINEL-001) and the trial-stats sidecar
        write (DEC-TRIAL-STATS-WRITE-001): when provided, a PAIRED completion
        sentinel short-circuits the tuple; otherwise the run BYPASSES dedup
        probes (full population, ``skipped_dedup == 0`` by construction),
        atomically writes the per-tuple (N, V-moments) sidecar between flush
        and dedup-mark, and writes the completion sentinel strictly last.
        ADR-0008 condition 2's preserve-on-partial-resume rule is thereby
        enforced structurally — a partial population cannot occur on this
        path. ``None`` (default) keeps the legacy probe semantics and writes
        no sidecar/sentinel (library callers; the launcher path supplies the
        digest via :func:`~brutex.engine.sweep_orchestrator.run_one_tuple`).

    Returns
    -------
    SweepRunSummary
        The full tally + the store content hash.

    Raises
    ------
    ValueError
        If ``bars`` is empty, ``cfg.direction`` is ``HOLD``, the enumerated combo
        count exceeds ``cfg.max_combos``, or ``kernel`` was built from a different
        number of bars than ``bars``.
    """
    if not bars:
        raise ValueError("run_persisted_sweep: bars must be non-empty")
    direction_label = _direction_label(cfg.direction)

    n_unique = len(set(cfg.condition_bits))
    n_masks = count_combos(cfg.k, n_unique)  # C(n, k); validates 0 <= k <= n
    if n_masks > cfg.max_combos:
        raise ValueError(
            f"run_persisted_sweep: {n_masks} combos (C({n_unique},{cfg.k})) exceeds "
            f"max_combos={cfg.max_combos}; raise max_combos explicitly to run a sweep this large"
        )

    params = _canonical_params(cfg)
    subject = f"sweep:{cfg.instrument}:{cfg.tf}:{direction_label}:k={cfg.k}"

    # ── Completion-sentinel resume check (DEC-MARK-MANY-SENTINEL-001) ──
    # The sentinel — never the dedup marks — decides whether this tuple is
    # already complete. A paired sentinel short-circuits everything below.
    if trial_stats_digest is not None:
        sentinel = _sentinel_resume(store.path, trial_stats_digest)
        if sentinel is not None:
            resume_payload = {
                "tuple_digest": trial_stats_digest,
                "store_content_hash": sentinel.store_content_hash,
                "sidecar_written": sentinel.sidecar_written,
                "n_marked": sentinel.n_marked,
            }
            audit.append(action=_ACTION_RESUMED, subject=subject, payload=resume_payload)
            _LOG.info("sweep_run.resumed_complete", subject=subject, **resume_payload)
            return SweepRunSummary(
                enumerated=0,
                conflict_pruned=0,
                skipped_dedup=0,
                evaluated=0,
                ranked=0,
                persisted=0,
                store_content_hash=sentinel.store_content_hash,
                resumed_complete=True,
            )

    audit.append(
        action=_ACTION_START,
        subject=subject,
        payload={
            "instrument": cfg.instrument,
            "tf": cfg.tf,
            "direction": direction_label,
            "mode": cfg.mode,
            "k": cfg.k,
            "n_bits": n_unique,
            "n_masks_enumerated": n_masks,
            "data_snapshot_hash": cfg.data_snapshot_hash,
            "indicator_version": cfg.indicator_version,
            "code_commit_sha": cfg.code_commit_sha,
            "params": params,
        },
    )

    if kernel is None:
        bar_bits = build_bar_bits(bars, proximity=cfg.proximity, prior_n=cfg.prior_n)
    else:
        if kernel.n_bars != len(bars):
            raise ValueError(
                f"run_persisted_sweep: kernel has {kernel.n_bars} bars but {len(bars)} "
                f"bars were supplied — the kernel must be built from this tuple's bars"
            )
        bar_bits = kernel.bar_bits

    # Sentinel-authoritative redo (DEC-MARK-MANY-SENTINEL-001): with a digest
    # and NO paired sentinel, the marks cannot be trusted (a crash inside the
    # per-key mark loop leaves a subset — MIGRATION.md #650), so probes are
    # BYPASSED: the FULL population re-runs and every artifact rewrites
    # byte-identically. Only legacy no-digest callers consult the probes.
    bypass_dedup = trial_stats_digest is not None
    enumerated = 0
    conflict_pruned = 0
    skipped_dedup = 0
    survivors: list[tuple[int, str]] = []
    for mask in iter_masks_k_of_bits(cfg.condition_bits, cfg.k):
        enumerated += 1
        if has_conflict(mask):
            conflict_pruned += 1
            continue
        run_id = mask_run_id(
            mask=mask,
            direction=direction_label,
            instrument=cfg.instrument,
            tf=cfg.tf,
            mode=cfg.mode,
            params=params,
            data_snapshot_hash=cfg.data_snapshot_hash,
            indicator_version=cfg.indicator_version,
            code_commit_sha=cfg.code_commit_sha,
        )
        if not bypass_dedup and dedup.seen(run_id):
            skipped_dedup += 1
            continue
        survivors.append((mask, run_id))

    # Stage-1B coarse pre-filter — drop survivors that can never reach the
    # trade-count gate (still "evaluated": we decided their fate + mark them seen).
    kept = survivors
    if cfg.min_hits > 0 and survivors:
        survivor_masks = [mask for mask, _ in survivors]
        # Resident buffer (kernel) avoids re-marshalling the bar window; both arms
        # are bit-identical (same SUPERSET kernel) so the kept set never differs.
        hits = (
            kernel.count_hits(survivor_masks)
            if kernel is not None
            else count_supermasks(bar_bits, survivor_masks)
        )
        kept = [
            (mask, run_id)
            for (mask, run_id), hit in zip(survivors, hits, strict=True)
            if hit >= cfg.min_hits
        ]
    evaluated = len(survivors)

    run_id_by_mask = dict(survivors)
    # Combo evaluation (fail-loud-with-fingerprint): each `metrics_for_mask` is
    # wrapped so a raise is re-thrown as a `SweepComboError` naming the exact
    # offending combo (mask hex + run-id + tuple coordinate). Per
    # "Resilient: skip + continue", the grid drivers catch THIS error and skip
    # the WHOLE tuple — treated CONSERVATIVELY as tuple-wide (the safe
    # redo-everything choice): the only per-combo-varying input is the mask, so a
    # mask-specific edge cannot be ruled out; the tuple is left un-sentineled /
    # un-cached so it redoes next run. A MemoryError is re-raised bare (OOM is a
    # GLOBAL stop, DEC-LAKE-WRITE-HALT-001, never a per-tuple skip). On the success
    # path `pairs` is byte-identical to the prior list comprehension (same order,
    # same tuples).
    pairs: list[tuple[int, ResultMetrics]] = []
    for mask, _ in kept:
        try:
            metrics = metrics_for_mask(
                bars,
                bar_bits,
                mask,
                direction=cfg.direction,
                tf_minutes=cfg.tf_minutes,
                stop_loss_paisa=cfg.sl_paisa,
                take_profit_paisa=cfg.tp_paisa,
                start_equity_paisa=cfg.start_equity_paisa,
            )
        except MemoryError:
            # OOM is a GLOBAL stop (DEC-LAKE-WRITE-HALT-001), never a per-tuple
            # skip — re-raise bare so it aborts the whole sweep instead of being
            # downgraded into a SweepComboError tuple-skip below.
            raise
        except Exception as exc:
            raise SweepComboError(
                f"combo 0x{mask:016x} (run_id={run_id_by_mask[mask]}) in "
                f"{cfg.instrument}/{cfg.tf}/{direction_label} k={cfg.k}: {exc}"
            ) from exc
        pairs.append((mask, metrics))
    ranked = rank_combos(pairs, overfit=cfg.overfit)
    finalists = ranked if cfg.top_k is None else ranked[: cfg.top_k]

    persisted = 0
    for rc in finalists:
        row = ranked_combo_to_result_row(
            rc,
            run_id=run_id_by_mask[rc.mask],
            instrument=cfg.instrument,
            tf=cfg.tf,
            direction=direction_label,
            mode=cfg.mode,
            bars=bars,
            bar_bits=bar_bits,
            tf_minutes=cfg.tf_minutes,
            sl_paisa=cfg.sl_paisa,
            tp_paisa=cfg.tp_paisa,
        )
        store.append(row)
        persisted += 1

    flush = store.flush()
    # Trial-stats sidecar (DEC-TRIAL-STATS-WRITE-001): written BETWEEN flush
    # and mark — a crash in this window re-does the whole tuple and rewrites a
    # byte-identical sidecar. ADR-0008 condition 2 required the FULL mask
    # population; the dedup bypass above guarantees it structurally on this
    # path (``skipped_dedup == 0`` by construction), so the old
    # preserve-on-partial-resume arm is unreachable and removed
    # (DEC-MARK-MANY-SENTINEL-001) — a subset population can no longer exist
    # to be preserved against.
    sidecar_written = False
    trial_n_finite = 0
    trial_sum_sharpe = 0.0
    trial_sum_sharpe_sq = 0.0
    if trial_stats_digest is not None:
        stats = tuple_trial_stats_from_sharpes(
            [metrics.sharpe_ratio for _, metrics in pairs],
            n_enumerated=enumerated,
            n_conflict_pruned=conflict_pruned,
            n_minhits_dropped=evaluated - len(kept),
            tuple_digest=trial_stats_digest,
            store_content_hash=flush.content_hash,
        )
        write_tuple_trial_stats(trial_stats_sidecar_path(store.path), stats)
        sidecar_written = True
        trial_n_finite = stats.n_finite
        trial_sum_sharpe = stats.sum_sharpe
        trial_sum_sharpe_sq = stats.sum_sharpe_sq
    # Mark seen AFTER the flush so a crash between evaluation and flush re-does
    # the work rather than skipping it (redo-not-skip; Golden Rule #7). The
    # marks are ADVISORY on the digest path (the sentinel below is the redo
    # authority); they remain load-bearing only for legacy no-digest callers.
    # On ``bypass_dedup`` (the digest path) the probes were NEVER consulted —
    # ``seen`` was skipped above — so marking is a pure, wasted O(survivors)
    # write to the (now off-heap LMDB) exact tier; skip it (DEC-SWEEP-DEDUP-LMDB-001).
    # The sentinel below is the redo authority on this path, so ``n_marked``
    # honestly reads 0 (no marks were written).
    marked = 0 if bypass_dedup else dedup.mark_many(run_id for _, run_id in survivors)
    # Completion sentinel STRICTLY LAST (DEC-MARK-MANY-SENTINEL-001): its
    # presence proves flush + sidecar + mark all completed; a crash anywhere
    # before this line leaves it absent → the next run redoes the FULL tuple.
    sentinel_written = False
    if trial_stats_digest is not None:
        write_completion_sentinel(
            completion_sentinel_path(store.path),
            TupleCompletionSentinel(
                tuple_digest=trial_stats_digest,
                store_content_hash=flush.content_hash,
                sidecar_written=sidecar_written,
                n_marked=marked,
            ),
        )
        sentinel_written = True

    tallies = {
        "enumerated": enumerated,
        "conflict_pruned": conflict_pruned,
        "skipped_dedup": skipped_dedup,
        "evaluated": evaluated,
        "ranked": len(ranked),
        "persisted": persisted,
        # Additive trial-stats tallies (DEC-TRIAL-STATS-WRITE-001) — the
        # hash-chain is per-line, so new payload keys are chain-safe.
        "backtested": len(pairs),
        "minhits_dropped": evaluated - len(kept),
        "trial_n_finite": trial_n_finite,
        "trial_sidecar_written": sidecar_written,
        # Additive sentinel tally (DEC-MARK-MANY-SENTINEL-001) — chain-safe.
        "sentinel_written": sentinel_written,
    }
    audit.append(
        action=_ACTION_FLUSH,
        subject=subject,
        payload={**tallies, "store_content_hash": flush.content_hash},
    )
    audit.append(action=_ACTION_COMPLETE, subject=subject, payload=tallies)
    _LOG.info("sweep_run.complete", subject=subject, **tallies)

    return SweepRunSummary(
        enumerated=enumerated,
        conflict_pruned=conflict_pruned,
        skipped_dedup=skipped_dedup,
        evaluated=evaluated,
        ranked=len(ranked),
        persisted=persisted,
        store_content_hash=flush.content_hash,
        backtested=len(pairs),
        minhits_dropped=evaluated - len(kept),
        trial_n_finite=trial_n_finite,
        trial_sum_sharpe=trial_sum_sharpe,
        trial_sum_sharpe_sq=trial_sum_sharpe_sq,
        trial_sidecar_written=sidecar_written,
        sentinel_written=sentinel_written,
    )


__all__ = [
    "ComboDedup",
    "SweepComboError",
    "SweepRunConfig",
    "SweepRunSummary",
    "canonical_trade_params",
    "mask_run_id",
    "ranked_combo_to_result_row",
    "run_persisted_sweep",
]
