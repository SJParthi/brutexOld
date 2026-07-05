"""Unit tests for the sweep launch planner (`brutex.engine.sweep_launch`).

Covers every public symbol and every branch for the strict 100% line + branch
gate (CLAUDE.md §4 / Golden Rule #12):

* R2-SL-F1 — the dead launch Bloom is KILLED: :func:`plan_launch_dedup` on the
  production DIGEST path returns the no-alloc :class:`DigestPathNoDedup`
  WITHOUT calling ``bloom_capacity_hint`` or constructing an
  ``OptionDedupIndex``/``BloomFilter`` (spy-proven), even for a grid whose
  mask-identity count is far above the ~4.1e9 MAX_BLOOM_BYTES wall — the exact
  grid shape that previously REFUSED to launch with ``BloomTooLargeError``.
* The defensive legacy (no-digest) arm still builds a REAL grid-sized
  ``ComboDedup`` — and maps an over-the-wall grid to the clean operator-facing
  :class:`SweepLaunchCapacityError` carrying the capacity math, raised BEFORE
  any bit-array allocation (``BloomFilter.__post_init__`` guards before
  ``_load_rust_bloom`` — mirrors tests/unit/test_orchestrator_bloom.py).
* R2-SL-F4-F5 — :func:`resolve_launch_cache_knobs` validates the
  ``--sweep-cache-map-mb`` / ``--sweep-cache-bloom-capacity`` knobs (flag >
  env > ``None``-means-module-default) via the COMMON
  ``resolve_positive_int_env`` resolver.
* R2-SL-F8 / R2-CC-G4 — :func:`sweep_abort_alert` fires the emitter exactly
  once on an ``Exception`` abort and re-raises unchanged; a clean span fires
  nothing; ``KeyboardInterrupt`` / ``SystemExit`` (``BaseException``) pass
  through with NO alert.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, NoReturn

import pytest
from brutex.engine.sweep_launch import (
    LAUNCH_DEDUP_FPR,
    DigestPathNoDedup,
    LaunchCacheKnobs,
    SweepLaunchCapacityError,
    SweepLaunchError,
    SweepLaunchUsageError,
    launch_bloom_identity_ceiling,
    plan_launch_dedup,
    resolve_launch_cache_knobs,
    sweep_abort_alert,
)
from brutex.engine.sweep_orchestrator import SweepGridConfig, Timeframe, bloom_capacity_hint
from brutex.engine.sweep_run import ComboDedup, DedupSink
from brutex.models import BruteXError
from brutex.strategies.types import Side


def _grid(**overrides: object) -> SweepGridConfig:
    """A tiny valid grid (the test_engine_sweep_orchestrator fixture pattern)."""
    base: dict[str, object] = {
        "instruments": ["NIFTY"],
        "timeframes": [Timeframe("1minute", 1)],
        "directions": [Side.BUY],
        "k_values": [1],
        "condition_bits": [0, 1, 2, 3, 4],
        "stop_losses": [80],
        "take_profits": [50],
        "proximities": [None],
        "mode": "A",
        "data_snapshot_hash": "snap0001",
        "indicator_version": "ind-v1",
        "code_commit_sha": "deadbeef",
    }
    base.update(overrides)
    return SweepGridConfig(**base)  # type: ignore[arg-type]


def _oversized_grid() -> SweepGridConfig:
    """A REAL grid whose mask-identity count exceeds the ~4.1e9 Bloom wall.

    ``C(60, 6) = 50_063_860`` masks per tuple × (2 instruments × 2 directions
    × 5 SL × 5 TP) = 100 tuples → ``5_006_386_000`` identities — the exact
    launch shape that previously crashed ``cmd_sweep_all`` with
    ``BloomTooLargeError`` (R2-SL-F1). ``max_combos`` is raised so the
    per-tuple honest guard admits the construction; no Bloom is ever
    allocated by building the CONFIG itself (pure math).
    """
    return _grid(
        instruments=["NIFTY", "BANKNIFTY"],
        directions=[Side.BUY, Side.SELL],
        k_values=[6],
        condition_bits=list(range(60)),
        stop_losses=[10, 20, 30, 40, 50],
        take_profits=[10, 20, 30, 40, 50],
        max_combos=60_000_000,
    )


# ────── error hierarchy ──────


def test_error_hierarchy() -> None:
    """Both launch errors root at SweepLaunchError → BruteXError."""
    assert issubclass(SweepLaunchError, BruteXError)
    assert issubclass(SweepLaunchCapacityError, SweepLaunchError)
    assert issubclass(SweepLaunchUsageError, SweepLaunchError)


# ────── DigestPathNoDedup — the no-alloc raising sink ──────


def test_no_dedup_sink_satisfies_dedup_sink_not_combo_dedup() -> None:
    """The sink rides the structural DedupSink seam and is NOT a ComboDedup.

    REGRESSION (CodeQL py/missing-call-to-init, code-scanning alert #533):
    the sink previously subclassed ``ComboDedup`` but skipped
    ``super().__init__()`` (the parent REQUIRES a real ``OptionDedupIndex``
    — the exact allocation the sink exists to avoid), leaving a
    half-initialised parent. It is now a standalone class satisfying the
    runtime-checkable :class:`~brutex.engine.sweep_run.DedupSink` Protocol
    the ``run_supervised_grid`` / ``run_sweep_grid`` annotations accept.
    """
    sink = DigestPathNoDedup()
    assert isinstance(sink, DedupSink)
    assert not isinstance(sink, ComboDedup)
    assert type(sink).__mro__ == (DigestPathNoDedup, object)


def test_no_dedup_sink_seen_raises() -> None:
    """A `seen` probe is a digest/bypass invariant break — raised loudly."""
    with pytest.raises(SweepLaunchUsageError, match="dedup.seen"):
        DigestPathNoDedup().seen("ab" * 32)


def test_no_dedup_sink_mark_raises() -> None:
    """A `mark` probe is a digest/bypass invariant break — raised loudly."""
    with pytest.raises(SweepLaunchUsageError, match="dedup.mark"):
        DigestPathNoDedup().mark("ab" * 32)


def test_no_dedup_sink_mark_many_raises() -> None:
    """A `mark_many` probe is a digest/bypass invariant break — raised loudly."""
    with pytest.raises(SweepLaunchUsageError, match="dedup.mark_many"):
        DigestPathNoDedup().mark_many(["ab" * 32])


# ────── launch_bloom_identity_ceiling ──────


def test_identity_ceiling_matches_bloom_sizing_inverse() -> None:
    """The ceiling is the exact inverse of BloomFilter's sizing at MAX_BLOOM_BYTES."""
    from brutex.orchestrator.bloom import MAX_BLOOM_BYTES

    ceiling = launch_bloom_identity_ceiling()
    # The inverse tracks the LIVE (RAM-derived, DEC-RAM-BUDGETS-001) wall.
    expected = int(MAX_BLOOM_BYTES * 8 / (-math.log(LAUNCH_DEDUP_FPR) / math.log(2) ** 2))
    assert ceiling == expected


def test_identity_ceiling_at_the_16_gib_floor_is_the_r2_sl_f1_figure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """≈4.097e9 at fpr 1e-7 when the wall sits at the 16 GiB undetected-RAM floor.

    Since DEC-RAM-BUDGETS-001 the live wall is RAM-derived, so the historical
    absolute figure is pinned deterministically by forcing the floor (the
    lazy import inside ``launch_bloom_identity_ceiling`` reads the module
    attribute at CALL time, so the patch is honored).
    """
    from brutex.orchestrator.bloom import MAX_BLOOM_BYTES_FLOOR

    monkeypatch.setattr("brutex.orchestrator.bloom.MAX_BLOOM_BYTES", MAX_BLOOM_BYTES_FLOOR)
    assert 4_000_000_000 < launch_bloom_identity_ceiling() < 4_200_000_000


def test_identity_ceiling_looser_fpr_admits_more() -> None:
    """A looser FPR needs fewer bits per item → a higher identity ceiling."""
    assert launch_bloom_identity_ceiling(1e-3) > launch_bloom_identity_ceiling(1e-7)


# ────── plan_launch_dedup — digest path (the R2-SL-F1 fix) ──────


def test_digest_path_returns_no_alloc_sink_without_touching_bloom_machinery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The digest plan constructs NOTHING: no hint, no index, no Bloom.

    The lazy imports inside the legacy arm resolve at call time, so poisoning
    the SOURCE-module symbols (string-target ``monkeypatch.setattr`` — one
    import style per module) proves the digest arm never reaches them.
    """

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("digest path must not touch the Bloom/dedup machinery")

    monkeypatch.setattr("brutex.engine.sweep_orchestrator.bloom_capacity_hint", _boom)
    monkeypatch.setattr("brutex.data.option_dedup.OptionDedupIndex", _boom)
    monkeypatch.setattr("brutex.orchestrator.bloom.BloomFilter", _boom)

    sink = plan_launch_dedup(_grid(), digest_path=True)
    assert isinstance(sink, DigestPathNoDedup)


def test_digest_path_launches_an_over_the_wall_grid() -> None:
    """REGRESSION (R2-SL-F1): a >4.1e9-identity grid now LAUNCHES on the digest path.

    Before the fix the CLI eagerly built
    ``ComboDedup(OptionDedupIndex(capacity=bloom_capacity_hint(grid), ...))``,
    so this exact grid refused to launch with ``BloomTooLargeError`` (and any
    grid just below the wall allocated up to ~16 GiB of never-read bit array).
    """
    grid = _oversized_grid()
    assert bloom_capacity_hint(grid) > launch_bloom_identity_ceiling()
    sink = plan_launch_dedup(grid, digest_path=True)  # no raise, no allocation
    assert isinstance(sink, DigestPathNoDedup)


# ────── plan_launch_dedup — legacy no-digest arm ──────


def test_legacy_path_builds_real_combo_dedup_roundtrip() -> None:
    """The legacy arm still builds a REAL grid-sized ComboDedup that dedups."""
    sink = plan_launch_dedup(_grid(), digest_path=False)
    assert isinstance(sink, ComboDedup)
    assert not isinstance(sink, DigestPathNoDedup)
    run_id = "ab" * 32
    assert sink.seen(run_id) is False
    assert sink.mark_many([run_id]) == 1
    assert sink.seen(run_id) is True


def test_legacy_path_threads_injected_exact_tier() -> None:
    """An injected exact backing (the LMDB seam) receives the marked identities."""
    exact: set[bytes] = set()
    sink = plan_launch_dedup(_grid(), digest_path=False, exact=exact)
    assert isinstance(sink, ComboDedup)  # narrows DedupSink → the real class
    sink.mark("cd" * 32)
    assert bytes.fromhex("cd" * 32) in exact


def test_legacy_path_over_the_wall_raises_clean_capacity_error() -> None:
    """An over-the-wall legacy build maps to SweepLaunchCapacityError + the math.

    The raise happens BEFORE any bit-array allocation
    (``BloomFilter.__post_init__`` raises ahead of ``_load_rust_bloom``), so
    this test never allocates GiB.
    """
    from brutex.orchestrator.bloom import MAX_BLOOM_BYTES, BloomTooLargeError

    grid = _oversized_grid()
    with pytest.raises(SweepLaunchCapacityError) as excinfo:
        plan_launch_dedup(grid, digest_path=False)
    message = str(excinfo.value)
    # The capacity math: grid mask-identity count, the MAX_BLOOM_BYTES wall,
    # the ~4.1e9-identity ceiling, and the remedy.
    assert f"{bloom_capacity_hint(grid):_}" in message
    assert f"MAX_BLOOM_BYTES={MAX_BLOOM_BYTES:_}" in message
    assert f"{launch_bloom_identity_ceiling(LAUNCH_DEDUP_FPR):_}" in message
    assert "--max-tuples" in message
    assert "digest path" in message
    # The vendor-side cause is chained, never swallowed (Golden Rule #10).
    assert isinstance(excinfo.value.__cause__, BloomTooLargeError)


# ────── behavior preservation on the digest engine path ──────


def test_no_dedup_sink_preserves_digest_path_output(tmp_path: Path) -> None:
    """run_sweep_grid with the no-alloc sink ≡ a real-ComboDedup run, byte-for-byte.

    The recount seam of ``cmd_sweep_verify`` (and the substitution proof for
    ``cmd_sweep_all``): the digest path never consults the sink, so swapping
    the eager ``ComboDedup(OptionDedupIndex(...))`` for
    :class:`DigestPathNoDedup` must change NOTHING — identical summary,
    ``skipped_dedup == 0``, and byte-identical persisted Parquet stores. A
    consult would raise :class:`SweepLaunchUsageError` and fail this test.
    """
    from datetime import UTC, datetime, timedelta, timezone
    from decimal import Decimal

    from brutex.data.models import CandleBar
    from brutex.data.option_dedup import OptionDedupIndex
    from brutex.engine.sweep_orchestrator import SweepGridSummary, make_store_for, run_sweep_grid
    from brutex.orchestrator.audit import AuditWriter

    ist = timezone(timedelta(hours=5, minutes=30))
    bars: list[CandleBar] = []
    price = 100.0
    for day in range(3):
        for minute in range(60):
            close = price + 0.6 if minute % 3 else price - 0.3
            ts = (
                datetime(2026, 1, 5, 9, 16, tzinfo=ist) + timedelta(days=day, minutes=minute)
            ).astimezone(UTC)
            bars.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal(str(round(price, 2))),
                    high=Decimal(str(round(max(price, close) + 0.5, 2))),
                    low=Decimal(str(round(min(price, close) - 0.5, 2))),
                    close=Decimal(str(round(close, 2))),
                    volume=100,
                    open_interest=None,
                )
            )
            price = close

    grid = _grid()

    def _run(root: Path, dedup: DedupSink) -> SweepGridSummary:
        with AuditWriter.open(root / "audit.jsonl") as audit:
            return run_sweep_grid(
                grid,
                bars_for=lambda s, i: bars,
                store_for=make_store_for(root, grid),
                dedup=dedup,
                audit=audit,
            )

    real_root = tmp_path / "real"
    noop_root = tmp_path / "noop"
    real_summary = _run(real_root, ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6)))
    noop_summary = _run(noop_root, DigestPathNoDedup())

    assert noop_summary == real_summary
    assert noop_summary.skipped_dedup == 0
    real_files = sorted(p.relative_to(real_root) for p in real_root.rglob("*.parquet"))
    noop_files = sorted(p.relative_to(noop_root) for p in noop_root.rglob("*.parquet"))
    assert real_files == noop_files
    assert real_files  # the sweep persisted something — no vacuous pass
    for rel in real_files:
        assert (noop_root / rel).read_bytes() == (real_root / rel).read_bytes()


# ────── resolve_launch_cache_knobs (R2-SL-F4-F5) ──────


def test_knobs_unset_resolve_to_none_keeping_module_defaults() -> None:
    """No flag + no env → (None, None) — each store keeps its OWN default."""
    knobs = resolve_launch_cache_knobs(env={})
    assert knobs == LaunchCacheKnobs(map_size_mb=None, bloom_capacity=None)


def test_knobs_explicit_flags_win_over_env() -> None:
    """Flag values beat the env vars (the resolve_positive_int_env contract)."""
    env = {
        "BRUTEX_SWEEP_CACHE_MAP_MB": "1024",
        "BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY": "7000000",
    }
    knobs = resolve_launch_cache_knobs(map_size_mb=512, bloom_capacity=6_000_000, env=env)
    assert knobs == LaunchCacheKnobs(map_size_mb=512, bloom_capacity=6_000_000)


def test_knobs_env_fallback_when_flags_unset() -> None:
    """Unset flags fall back to the shared env vars."""
    env = {
        "BRUTEX_SWEEP_CACHE_MAP_MB": "1024",
        "BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY": "7000000",
    }
    knobs = resolve_launch_cache_knobs(env=env)
    assert knobs == LaunchCacheKnobs(map_size_mb=1024, bloom_capacity=7_000_000)


def test_knobs_blank_env_is_unset() -> None:
    """A blank/whitespace env value means unset, never a parse error."""
    env = {"BRUTEX_SWEEP_CACHE_MAP_MB": "  ", "BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY": ""}
    assert resolve_launch_cache_knobs(env=env) == LaunchCacheKnobs(None, None)


@pytest.mark.parametrize("bad", [0, -5])
def test_knobs_non_positive_flag_is_loud(bad: int) -> None:
    """A non-positive flag raises a ValueError naming the knob (Golden Rule #10)."""
    with pytest.raises(ValueError, match="BRUTEX_SWEEP_CACHE_MAP_MB"):
        resolve_launch_cache_knobs(map_size_mb=bad, env={})
    with pytest.raises(ValueError, match="BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY"):
        resolve_launch_cache_knobs(bloom_capacity=bad, env={})


def test_knobs_malformed_env_is_loud() -> None:
    """A mistyped env value raises, never a silent fallback."""
    with pytest.raises(ValueError, match="BRUTEX_SWEEP_CACHE_MAP_MB"):
        resolve_launch_cache_knobs(env={"BRUTEX_SWEEP_CACHE_MAP_MB": "lots"})
    with pytest.raises(ValueError, match="BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY"):
        resolve_launch_cache_knobs(env={"BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY": "many"})


# ────── sweep_abort_alert (R2-SL-F8 / R2-CC-G4) ──────


def _raise(exc: BaseException) -> NoReturn:
    """Raise *exc* from a call site so no with-body statement is a bare raise."""
    raise exc


def test_abort_alert_clean_span_fires_nothing() -> None:
    """A clean span never emits — only the launcher's sweep_complete fires."""
    fired: list[Exception] = []
    with sweep_abort_alert(fired.append):
        pass
    assert fired == []


def test_abort_alert_fires_once_then_reraises_unchanged() -> None:
    """An Exception abort emits EXACTLY once with the same instance, then re-raises."""
    fired: list[Exception] = []
    boom = MemoryError("oom mid-grid")
    with pytest.raises(MemoryError) as excinfo, sweep_abort_alert(fired.append):
        _raise(boom)
    assert excinfo.value is boom
    assert fired == [boom]


def test_abort_alert_keyboard_interrupt_passes_through_silently() -> None:
    """Ctrl-C is BaseException — it propagates with NO alert."""
    fired: list[Exception] = []
    with pytest.raises(KeyboardInterrupt), sweep_abort_alert(fired.append):
        _raise(KeyboardInterrupt())
    assert fired == []


def test_abort_alert_system_exit_passes_through_silently() -> None:
    """SystemExit is BaseException — it propagates with NO alert."""
    fired: list[Exception] = []
    with pytest.raises(SystemExit), sweep_abort_alert(fired.append):
        _raise(SystemExit(3))
    assert fired == []
