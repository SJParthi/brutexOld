"""Launch-time planning for the ``sweep-all`` / ``sweep-verify`` CLI shims.

The CLI launch entry points (``cmd_sweep_all`` / ``cmd_sweep_verify --recount``)
are ``# pragma: no cover`` orchestration shims, so every load-bearing launch
decision lives HERE, in a measured 100%-line+branch module (CLAUDE.md §2
Rule 12). Three concerns, one module:

1. **Dead launch Bloom killed (R2-SL-F1).** The production launchers run the
   DIGEST path: :func:`brutex.engine.sweep_orchestrator.run_one_tuple` always
   passes a non-``None`` ``trial_stats_digest``, so
   :func:`brutex.engine.sweep_run.run_persisted_sweep` sets
   ``bypass_dedup = trial_stats_digest is not None`` → ``True`` and NEVER calls
   ``dedup.seen`` / ``dedup.mark_many`` (the parallel driver's workers likewise
   use their own raising ``_NoDedup`` stand-in). Eagerly building
   ``ComboDedup(OptionDedupIndex(capacity=bloom_capacity_hint(grid), …))`` at
   launch therefore allocated up to :data:`~brutex.orchestrator.bloom.MAX_BLOOM_BYTES`
   (RAM-derived since DEC-RAM-BUDGETS-001; 16 GiB on the §8 box / undetected
   floor) of never-read Bloom bit array below the wall — and REFUSED the
   launch with :class:`~brutex.orchestrator.bloom.BloomTooLargeError` above it
   (≈4.1e9 mask identities at fpr 1e-7 at that 16 GiB figure) for a filter
   that would never be consulted. :func:`plan_launch_dedup` returns the no-alloc
   :class:`DigestPathNoDedup` on the digest path and maps the legacy build's
   ``BloomTooLargeError`` to the operator-facing
   :class:`SweepLaunchCapacityError` naming the capacity math.

2. **Off-heap dedup-store knobs (R2-SL-F4-F5).**
   :func:`resolve_launch_cache_knobs` eagerly validates the
   ``--sweep-cache-map-mb`` / ``--sweep-cache-bloom-capacity`` flags against the
   COMMON :func:`brutex.data.lmdb_membership.resolve_positive_int_env` resolver
   (flag > env > ``None``-means-module-default) so a mistyped knob is a clean
   user error BEFORE any store opens, never a mid-launch traceback.

3. **Sweep-abort Telegram alert (R2-SL-F8 / R2-CC-G4).**
   :func:`sweep_abort_alert` is the fire-exactly-once abort-alert wrapper the
   launch shim puts around the whole grid/stage-3/cascade span, mirroring
   ``cmd_fetch``'s DEC-TELEGRAM-PULL-HALT-001 pattern: any propagating
   ``Exception`` fires the injected best-effort emitter ONCE and re-raises
   unchanged (``KeyboardInterrupt`` / ``SystemExit`` are ``BaseException`` —
   an operator Ctrl-C never alerts), closing the phone-silent-abort gap
   (CLAUDE.md §2 Rule 11 AUTOMATED — zero clicks per day).

Engine scope is UNCHANGED (NIFTY/BANKNIFTY/SENSEX + the operator-authorized
NIFTY futures track); no vocab/scope/rate change — pure launch operability.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from brutex.engine.sweep_run import ComboDedup, DedupSink
from brutex.models import BruteXError

if TYPE_CHECKING:  # pragma: no cover — typing-only imports
    from brutex.data.option_dedup import ExactMembership
    from brutex.engine.sweep_orchestrator import SweepGridConfig

#: The FPR target the legacy (no-digest) launch Bloom is sized at — the ONE
#: source of the ``1e-7`` the CLI shims previously inlined at every
#: ``OptionDedupIndex(…, fpr_target=1e-7, …)`` construction site.
LAUNCH_DEDUP_FPR: Final[float] = 1e-7


class SweepLaunchError(BruteXError):
    """A sweep launch-planning failure (the ``sweep_launch`` error root)."""


class SweepLaunchCapacityError(SweepLaunchError):
    """The legacy no-digest launch Bloom cannot be sized for this grid.

    The operator-facing mapping of
    :class:`~brutex.orchestrator.bloom.BloomTooLargeError`: the message names
    the grid's mask-identity count (:func:`~brutex.engine.sweep_orchestrator.bloom_capacity_hint`),
    the computed bit-array bytes vs :data:`~brutex.orchestrator.bloom.MAX_BLOOM_BYTES`
    (via the chained cause's own message), the identity ceiling implied by the
    machine's RAM-derived wall (≈4.1e9 at the 16 GiB floor), and the
    remedy — so the CLI exits with an actionable error instead of a raw
    traceback. Raised only on the legacy ``digest_path=False`` arm; the
    production digest path never builds this Bloom at all.
    """


class SweepLaunchUsageError(SweepLaunchError):
    """The no-alloc digest-path dedup sink was consulted — an invariant break.

    On the digest path ``run_persisted_sweep`` bypasses dedup entirely
    (``bypass_dedup = trial_stats_digest is not None`` —
    ``brutex/engine/sweep_run.py``), so a real call into
    :class:`DigestPathNoDedup` means the digest/bypass wiring regressed.
    Raising loudly (belt-and-braces, the same contract as the parallel
    driver's private ``_NoDedup``) beats silently returning fabricated
    dedup verdicts (Golden Rule #1).
    """


class DigestPathNoDedup:
    """A no-alloc dedup stand-in for the production DIGEST launch path.

    Substitutes for the eager ``ComboDedup(OptionDedupIndex(...))`` the CLI
    launchers used to build: it allocates NO Bloom bit array, NO
    ``OptionDedupIndex``, and needs NO LMDB exact tier — because the digest
    path provably never consults ``seen`` / ``mark`` / ``mark_many``
    (R2-SL-F1). Deliberately a STANDALONE class, NOT a
    :class:`~brutex.engine.sweep_run.ComboDedup` subclass:
    ``ComboDedup.__init__`` REQUIRES a real
    :class:`~brutex.data.option_dedup.OptionDedupIndex` (Bloom + exact tier)
    — the exact allocation this sink exists to avoid — so the earlier
    subclass had to skip ``super().__init__()`` entirely, which CodeQL
    rightly flags as ``py/missing-call-to-init`` (a half-initialised parent
    with no honest cheap sentinel to hand it). The sink instead satisfies
    the runners' structural :class:`~brutex.engine.sweep_run.DedupSink`
    Protocol (``seen`` + ``mark_many`` — the same seam the parallel driver's
    private ``_NoDedup`` rides), so the ``DedupSink``-annotated
    ``run_supervised_grid`` / ``run_sweep_grid`` accept it unchanged; every
    probe raises :class:`SweepLaunchUsageError`. Construction stays O(1) and
    alloc-free: no state at all (the implicit ``object.__init__``).

    COMMON note (CLAUDE.md §11): this mirrors the parallel driver's private
    ``_NoDedup`` (``brutex/engine/sweep_parallel.py``); promoting that symbol
    to a shared public sink is a flagged follow-up owned by that module.
    """

    def seen(self, run_id_hex: str) -> bool:
        """RAISE — the digest path must never consult ``dedup.seen``.

        Raises
        ------
        SweepLaunchUsageError
            Always — see the class docstring.
        """
        raise SweepLaunchUsageError(
            "digest-path launch sink consulted dedup.seen — the digest/bypass "
            "wiring regressed (run_persisted_sweep must set bypass_dedup=True "
            f"on this path); probed run_id={run_id_hex!r}"
        )

    def mark(self, run_id_hex: str) -> None:
        """RAISE — the digest path must never consult ``dedup.mark``.

        Raises
        ------
        SweepLaunchUsageError
            Always — see the class docstring.
        """
        raise SweepLaunchUsageError(
            "digest-path launch sink consulted dedup.mark — the digest/bypass "
            "wiring regressed (run_persisted_sweep must set bypass_dedup=True "
            f"on this path); marked run_id={run_id_hex!r}"
        )

    def mark_many(self, run_id_hexes: Iterable[str]) -> int:
        """RAISE — the digest path must never consult ``dedup.mark_many``.

        Raises
        ------
        SweepLaunchUsageError
            Always — see the class docstring.
        """
        raise SweepLaunchUsageError(
            "digest-path launch sink consulted dedup.mark_many — the "
            "digest/bypass wiring regressed (run_persisted_sweep must set "
            "bypass_dedup=True on this path)"
        )


def launch_bloom_identity_ceiling(fpr_target: float = LAUNCH_DEDUP_FPR) -> int:
    """Return the max mask-identity capacity a legacy launch Bloom can hold.

    The inverse of :class:`~brutex.orchestrator.bloom.BloomFilter`'s sizing
    (``m_bits = ceil(-capacity · ln p / ln 2²)``) solved at
    ``m_bits = MAX_BLOOM_BYTES · 8``: capacities above this wall raise
    :class:`~brutex.orchestrator.bloom.BloomTooLargeError` at construction.
    ≈4.097e9 identities at the launch default ``fpr_target=1e-7`` and the
    16 GiB ``MAX_BLOOM_BYTES`` undetected-RAM floor — the ceiling tracks the
    RAM-derived wall automatically (DEC-RAM-BUDGETS-001). Used for
    the operator-facing capacity math in :class:`SweepLaunchCapacityError`
    ONLY — never for sizing (the honest, slightly-conservative-free
    approximation ignores the per-capacity ``ceil`` rounding; Golden
    Rule #10).

    Parameters
    ----------
    fpr_target
        The Bloom false-positive-rate target (default
        :data:`LAUNCH_DEDUP_FPR`).

    Returns
    -------
    int
        The approximate identity-count ceiling implied by
        :data:`~brutex.orchestrator.bloom.MAX_BLOOM_BYTES`.
    """
    # Imported lazily — bloom.py stays out of this module's import-time graph
    # (mirrors sweep_orchestrator's deliberate import hygiene).
    from brutex.orchestrator.bloom import MAX_BLOOM_BYTES

    bits_per_item = -math.log(fpr_target) / math.log(2) ** 2
    return int(MAX_BLOOM_BYTES * 8 / bits_per_item)


def plan_launch_dedup(
    grid: SweepGridConfig,
    *,
    digest_path: bool,
    exact: ExactMembership | None = None,
) -> DedupSink:
    """Plan the launch dedup sink for one grid — no-alloc on the digest path.

    The ONE launch-dedup decision point for the ``sweep-all`` /
    ``sweep-verify --recount`` shims (R2-SL-F1). The ``digest_path`` flag
    mirrors the exact production predicate — ``run_one_tuple`` always passes a
    non-``None`` ``trial_stats_digest``, so ``run_persisted_sweep``'s
    ``bypass_dedup = trial_stats_digest is not None`` is always ``True`` — the
    same convention :func:`brutex.engine.sweep_parallel.refuse_parallel_on_legacy_path`
    already uses (there is no importable runtime predicate; both CLI call
    sites are structurally digest-path and pass ``digest_path=True``).

    Parameters
    ----------
    grid
        The grid config (consulted ONLY on the legacy arm, for
        :func:`~brutex.engine.sweep_orchestrator.bloom_capacity_hint`).
    digest_path
        ``True`` (the production paths): return the no-alloc
        :class:`DigestPathNoDedup` — no ``bloom_capacity_hint`` call, no
        ``OptionDedupIndex``, no Bloom, no LMDB exact tier. ``False`` (the
        defensive legacy no-digest arm, unreachable from any live CLI flow
        today): build the real grid-sized ``ComboDedup``.
    exact
        The exact-confirm backing for the legacy arm (e.g. an
        ``LmdbMembership``; ``None`` = ``OptionDedupIndex``'s in-memory
        ``set`` default). Ignored on the digest path — the sink has no tiers.

    Returns
    -------
    DedupSink
        The sink to inject into ``run_supervised_grid`` / ``run_sweep_grid``:
        the no-alloc :class:`DigestPathNoDedup` on the digest path, a real
        grid-sized :class:`~brutex.engine.sweep_run.ComboDedup` on the
        legacy arm (both satisfy the structural
        :class:`~brutex.engine.sweep_run.DedupSink` seam).

    Raises
    ------
    SweepLaunchCapacityError
        Legacy arm only — the grid's mask-identity count needs a Bloom bit
        array above :data:`~brutex.orchestrator.bloom.MAX_BLOOM_BYTES`; the
        message carries the capacity math + remedy, and the raise happens
        BEFORE any bit-array allocation (``BloomFilter.__post_init__`` guards
        before ``_load_rust_bloom``).
    """
    if digest_path:
        return DigestPathNoDedup()
    # Legacy no-digest arm — imported lazily so the digest arm provably never
    # touches the hint / index machinery (and the fno_math wheel behind
    # option_dedup stays off the digest launch's import path).
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.sweep_orchestrator import bloom_capacity_hint
    from brutex.orchestrator.bloom import MAX_BLOOM_BYTES, BloomTooLargeError

    capacity = bloom_capacity_hint(grid)
    try:
        index = OptionDedupIndex(capacity=capacity, fpr_target=LAUNCH_DEDUP_FPR, exact=exact)
    except BloomTooLargeError as exc:
        raise SweepLaunchCapacityError(
            f"legacy no-digest sweep dedup cannot launch: the grid enumerates "
            f"{capacity:_} mask run-identities (bloom_capacity_hint), above the "
            f"~{launch_bloom_identity_ceiling(LAUNCH_DEDUP_FPR):_}-identity "
            f"MAX_BLOOM_BYTES={MAX_BLOOM_BYTES:_} wall at "
            f"fpr_target={LAUNCH_DEDUP_FPR} — {exc} Shrink the grid "
            f"(--max-tuples / --max-combos / fewer symbols, intervals, k "
            f"values) or run the default digest path, which never builds this "
            f"Bloom."
        ) from exc
    return ComboDedup(index)


@dataclass(frozen=True, slots=True)
class LaunchCacheKnobs:
    """The validated off-heap dedup-store knobs (R2-SL-F4-F5).

    ``None`` means "no flag and no env var set — let each store keep its OWN
    module default" (critical: an unset knob must never collapse the
    ``LmdbMembership`` 10 GiB default and the ``SweepCache`` 2 GiB default
    into one another).

    Attributes
    ----------
    map_size_mb
        The resolved LMDB memory-map ceiling (MiB), or ``None``.
    bloom_capacity
        The resolved tuple-dedup Bloom capacity, or ``None``.
    """

    map_size_mb: int | None
    bloom_capacity: int | None


def resolve_launch_cache_knobs(
    *,
    map_size_mb: int | None = None,
    bloom_capacity: int | None = None,
    env: Mapping[str, str] | None = None,
) -> LaunchCacheKnobs:
    """Validate + resolve the sweep-cache operator knobs, flag > env > ``None``.

    Called EAGERLY by the launch shim (before any lake preload or store open)
    so a mistyped ``--sweep-cache-map-mb`` / ``--sweep-cache-bloom-capacity``
    flag OR a mistyped ``BRUTEX_SWEEP_CACHE_MAP_MB`` /
    ``BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY`` env var is a clean user-config error
    (the CLI maps the ``ValueError`` to ``EXIT_USER_ERROR``), never a
    mid-launch traceback — and never a false abort alert. Delegates to the
    COMMON :func:`brutex.data.lmdb_membership.resolve_positive_int_env`
    (CLAUDE.md §11 — one resolver), preserving its ``None``-when-unset
    contract so :meth:`brutex.engine.sweep_cache.SweepCache.open` keeps its
    own defaults.

    Parameters
    ----------
    map_size_mb
        The ``--sweep-cache-map-mb`` flag value (``None`` when unset).
    bloom_capacity
        The ``--sweep-cache-bloom-capacity`` flag value (``None`` when unset).
    env
        Environment mapping (defaults to :data:`os.environ`) — injectable for
        tests.

    Returns
    -------
    LaunchCacheKnobs
        The validated knobs, ready to thread into ``SweepCache.open``.

    Raises
    ------
    ValueError
        If either knob (flag or env) is not a positive integer — loud, naming
        the offending knob (Golden Rule #10).
    """
    # Imported lazily: lmdb_membership pulls the runtime-only ``lmdb`` wheel
    # and sweep_cache mirrors the same hygiene for its constants.
    from brutex.data.lmdb_membership import SWEEP_CACHE_MAP_MB_ENV, resolve_positive_int_env
    from brutex.engine.sweep_cache import SWEEP_CACHE_BLOOM_CAPACITY_ENV

    return LaunchCacheKnobs(
        map_size_mb=resolve_positive_int_env(SWEEP_CACHE_MAP_MB_ENV, map_size_mb, env=env),
        bloom_capacity=resolve_positive_int_env(
            SWEEP_CACHE_BLOOM_CAPACITY_ENV, bloom_capacity, env=env
        ),
    )


@contextmanager
def sweep_abort_alert(emit: Callable[[Exception], None]) -> Generator[None, None, None]:
    """Fire *emit* exactly once when the wrapped span aborts, then re-raise.

    The sweep twin of ``cmd_fetch``'s halt-alert pattern
    (DEC-TELEGRAM-PULL-HALT-001; R2-SL-F8 / R2-CC-G4): the launch shim wraps
    the whole grid/stage-3/cascade span in this context manager so an abort
    that would otherwise unwind phone-silently fires ONE best-effort CRITICAL
    push before propagating unchanged. Catches ``Exception`` ONLY —
    ``KeyboardInterrupt`` / ``SystemExit`` are ``BaseException`` and pass
    straight through with NO alert (an operator Ctrl-C is not an abort). The
    clean path fires nothing.

    Parameters
    ----------
    emit
        The alert emitter (the CLI's ``_emit_pull_halt_alert``). MUST be
        fire-and-forget — the production emitter delegates to
        :func:`brutex.notifications.sweep_alerts.emit_pull_halt_alert`, whose
        blanket guard swallows every publish error, so emitting can never mask
        the abort it reports (Golden Rule #10).

    Yields
    ------
    None
        Control for the wrapped span.
    """
    try:
        yield
    except Exception as exc:
        emit(exc)
        raise


__all__ = [
    "LAUNCH_DEDUP_FPR",
    "DigestPathNoDedup",
    "LaunchCacheKnobs",
    "SweepLaunchCapacityError",
    "SweepLaunchError",
    "SweepLaunchUsageError",
    "launch_bloom_identity_ceiling",
    "plan_launch_dedup",
    "resolve_launch_cache_knobs",
    "sweep_abort_alert",
]
