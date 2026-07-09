"""End-to-end proof of ``brutex sweep-all``'s launch wiring (R2-SL-F1/F4-F5/F8).

The CLI shim ``cmd_sweep_all`` is ``# pragma: no cover`` (an orchestration shim
over the 100%-tested engine), so this is the HONEST functional validation of
its launch wiring. It persists a tiny lake with the same writers ingest uses
and drives ``cli.cmd_sweep_all`` for real:

* **R2-SL-F1 — dead launch Bloom KILLED.** The production launcher runs the
  DIGEST path (``run_one_tuple`` always digests → ``bypass_dedup`` is always
  True), so the grid-sized ``ComboDedup(OptionDedupIndex(...))`` + the LMDB
  ``combo-dedup`` store it used to build eagerly were never consulted — dead
  weight below the ~4.1e9-identity Bloom wall and a ``BloomTooLargeError``
  launch REFUSAL above it. The launcher now injects the no-alloc
  :class:`~brutex.engine.sweep_launch.DigestPathNoDedup`: these tests prove no
  ``OptionDedupIndex`` is ever constructed (poisoned constructor), no
  ``combo-dedup`` directory appears, and the sweep still completes.
  (This file previously pinned the OPPOSITE — the eager LMDB combo tier —
  per DEC-SWEEP-DEDUP-LMDB-001; that tier is provably dead on the digest
  path, so the pin is flipped. The LMDB-backed TUPLE tier — ``SweepCache`` —
  stays, now operator-tunable.)
* **R2-SL-F4-F5 — off-heap store knobs.** ``--sweep-cache-map-mb`` /
  ``--sweep-cache-bloom-capacity`` thread into ``SweepCache.open``; a
  mistyped env knob is a clean ``EXIT_USER_ERROR``, never a traceback.
* **R2-SL-F8 / R2-CC-G4 — sweep-abort alert.** An abort propagating out of
  the grid fires ``cli._emit_pull_halt_alert`` exactly once then re-raises
  (the cmd_fetch DEC-TELEGRAM-PULL-HALT-001 pattern); a clean run fires only
  ``sweep_complete``; ``KeyboardInterrupt`` never alerts.
* **DEC-SLA-LAUNCH-GUARD-001 — SLA launch guard.** An over-envelope
  projection REFUSES the launch (``EXIT_USER_ERROR``, honest message, zero
  artifacts); ``--acknowledge-sla-overrun`` proceeds with the loud warning;
  a malformed / NaN ``BRUTEX_SWEEP_PER_TUPLE_SECONDS`` is a clean user error.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.engine import sweep_cache as sweep_cache_mod
from brutex.engine import sweep_parallel, sweep_supervisor
from brutex.engine.sweep_launch import DigestPathNoDedup
from brutex.notifications import sweep_alerts

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"
_INTERVAL = "1minute"


def _bars() -> list[CandleBar]:
    """A short in-session NIFTY minute window (stored UTC), valid OHLC."""
    out: list[CandleBar] = []
    for i in range(8):
        ts = datetime(2024, 1, 2, 9, 16 + i, tzinfo=_IST).astimezone(UTC)
        out.append(
            CandleBar(
                timestamp=ts,
                open=Decimal("100.00"),
                high=Decimal("102.00"),
                low=Decimal("99.00"),
                close=Decimal("100.00") + Decimal(i) / 10,
                volume=1000,
                open_interest=None,
            )
        )
    return out


def _persist(tmp_path: Path) -> tuple[Path, Path]:
    """Write the NIFTY bars to a temp lake; return (lake_root, catalog_path)."""
    lake_root = tmp_path / "lake"
    catalog_path = tmp_path / "catalog.duckdb"
    writer = ParquetWriter(lake_root=lake_root)
    with Catalog.open(catalog_path) as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol=_SYMBOL,
            interval=_INTERVAL,
            bars=_bars(),
        )
    return lake_root, catalog_path


def _sweep_all(lake_root: Path, catalog_path: Path, results_root: Path, **overrides: Any) -> int:
    """Drive ``cli.cmd_sweep_all`` over the tiny persisted lake."""
    kwargs: dict[str, Any] = {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": results_root,
        "exchange": _EXCHANGE,
        "segment": _SEGMENT,
        "symbols": _SYMBOL,
        "intervals": _INTERVAL,
        "start": "2024-01-02",
        "end": "2024-01-03",
        "directions": "BUY",
        "k": "2",
        "conditions": "0,1,2,3",  # C(4,2) = 6 combos > 1 → a real multi-combo sweep
        "sl": "none",
        "tp": "none",
        "top": 10,
        "mode": "A",
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "options_sim": False,
    }
    kwargs.update(overrides)
    return cli.cmd_sweep_all(**kwargs)


def test_sweep_all_injects_no_alloc_digest_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R2-SL-F1: the launcher injects the no-alloc sink — no OptionDedupIndex, no combo-dedup LMDB."""
    import brutex.data.option_dedup as option_dedup_mod

    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"

    captured: dict[str, Any] = {}
    real = sweep_supervisor.run_supervised_grid

    def _spy(*args: Any, **kwargs: Any) -> Any:
        captured["dedup"] = kwargs["dedup"]
        return real(*args, **kwargs)

    # cmd_sweep_all imports run_supervised_grid locally from sweep_supervisor, so
    # patch it at its source module (resolved at call time).
    monkeypatch.setattr(sweep_supervisor, "run_supervised_grid", _spy)

    # Poison the dead machinery: the launch must never construct an
    # OptionDedupIndex (the SweepCache tuple tier builds its OWN BloomFilter —
    # that one is live and stays).
    def _no_index(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("sweep-all launch must not construct an OptionDedupIndex")

    monkeypatch.setattr(option_dedup_mod, "OptionDedupIndex", _no_index)
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == 0
    # The injected sink is the no-alloc digest stand-in (a standalone class
    # riding the structural DedupSink seam — NOT a ComboDedup subclass, per
    # CodeQL py/missing-call-to-init alert #533).
    assert isinstance(captured["dedup"], DigestPathNoDedup)
    # The dead per-mask combo-dedup LMDB store is GONE from the results root …
    assert not (results_root / "combo-dedup").exists()
    # … while the live LMDB-backed TUPLE tier (SweepCache) still persists.
    assert (results_root / "dedup-cache" / "exact").exists()
    # And the sweep actually ran (the report rendered).
    assert "sweep-all done" in capsys.readouterr().out


def test_sweep_all_threads_cache_knobs_into_sweep_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2-SL-F4-F5: the flags reach SweepCache.open(map_size_mb=…, bloom_capacity=…)."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"

    captured: dict[str, Any] = {}
    real_open = sweep_cache_mod.SweepCache.open.__func__  # type: ignore[attr-defined]

    def _spy_open(cls: Any, path: Path, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_open(cls, path, **kwargs)

    monkeypatch.setattr(sweep_cache_mod.SweepCache, "open", classmethod(_spy_open))
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")

    rc = _sweep_all(
        lake_root,
        catalog_path,
        results_root,
        sweep_cache_map_mb=64,
        sweep_cache_bloom_capacity=100_000,
    )

    assert rc == 0
    assert captured["map_size_mb"] == 64
    assert captured["bloom_capacity"] == 100_000


def test_sweep_all_unset_knobs_thread_as_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset knobs reach SweepCache.open as None — module defaults stay in charge."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"

    captured: dict[str, Any] = {}
    real_open = sweep_cache_mod.SweepCache.open.__func__  # type: ignore[attr-defined]

    def _spy_open(cls: Any, path: Path, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_open(cls, path, **kwargs)

    monkeypatch.setattr(sweep_cache_mod.SweepCache, "open", classmethod(_spy_open))
    monkeypatch.delenv("BRUTEX_SWEEP_CACHE_MAP_MB", raising=False)
    monkeypatch.delenv("BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY", raising=False)
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == 0
    assert captured["map_size_mb"] is None
    assert captured["bloom_capacity"] is None


def test_sweep_all_malformed_env_knob_is_clean_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mistyped env knob exits EXIT_USER_ERROR cleanly — no traceback, no alert."""
    lake_root, catalog_path = _persist(tmp_path)
    fired: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", fired.append)
    monkeypatch.setenv("BRUTEX_SWEEP_CACHE_MAP_MB", "lots")

    rc = _sweep_all(lake_root, catalog_path, tmp_path / "sweeps")

    assert rc == cli.EXIT_USER_ERROR
    assert "BRUTEX_SWEEP_CACHE_MAP_MB" in capsys.readouterr().err
    assert fired == []  # a config error is not an abort — never a false alert


def test_cmd_sweep_all_alerts_then_reraises_on_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2-SL-F8: a grid abort fires _emit_pull_halt_alert exactly once, then re-raises."""
    lake_root, catalog_path = _persist(tmp_path)
    fired: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", fired.append)

    boom = MemoryError("oom mid-grid")

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise boom

    monkeypatch.setattr(sweep_supervisor, "run_supervised_grid", _raise)
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")

    with pytest.raises(MemoryError) as excinfo:
        _sweep_all(lake_root, catalog_path, tmp_path / "sweeps")

    assert excinfo.value is boom  # re-raised unchanged, never swallowed
    assert fired == [boom]  # exactly ONE alert


def test_cmd_sweep_all_parallel_abort_alerts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parallel dispatch arm rides the same abort-alert wrap."""
    lake_root, catalog_path = _persist(tmp_path)
    fired: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", fired.append)

    boom = RuntimeError("worker pool wedged")

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise boom

    monkeypatch.setattr(sweep_parallel, "run_parallel_grid", _raise)
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "2")

    with pytest.raises(RuntimeError) as excinfo:
        _sweep_all(lake_root, catalog_path, tmp_path / "sweeps")

    assert excinfo.value is boom
    assert fired == [boom]


def test_cmd_sweep_all_clean_run_fires_no_halt_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean run fires ZERO halt alerts and exactly one sweep_complete."""
    lake_root, catalog_path = _persist(tmp_path)
    fired: list[Exception] = []
    completed: list[Any] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", fired.append)

    def _spy_complete(self: Any, summary: Any) -> bool:
        completed.append(summary)
        return False

    monkeypatch.setattr(sweep_alerts.SweepAlerter, "sweep_complete", _spy_complete)
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")

    rc = _sweep_all(lake_root, catalog_path, tmp_path / "sweeps")

    assert rc == 0
    assert fired == []  # never both — the clean path alerts only completion
    assert len(completed) == 1


def test_cmd_sweep_all_delivers_the_summary_to_on_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G5b end-to-end: a real run delivers exactly ONE SupervisedGridSummary.

    Drives the REAL ``cmd_sweep_all`` True-branch of ``deliver_summary_safe``
    (the fenced review-fold seam) — the dashboard job's capture path — over the
    tiny persisted lake.
    """
    lake_root, catalog_path = _persist(tmp_path)
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")
    captured: list[Any] = []

    rc = _sweep_all(lake_root, catalog_path, tmp_path / "sweeps", on_summary=captured.append)

    assert rc == 0
    assert len(captured) == 1
    assert isinstance(captured[0], sweep_supervisor.SupervisedGridSummary)
    assert captured[0].tuples_run >= 1


def test_cmd_sweep_all_runs_the_sweep_chain_inside_keep_awake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G9: the whole sweep chain runs INSIDE the keep_awake window.

    ``cmd_sweep_all`` imports ``keep_awake`` lazily inside the function, so the
    SOURCE ``brutex.observability.power_keepawake.keep_awake`` is monkeypatched
    with a recording context manager (mirrors ``test_nightly_run.py`` G9). The
    sweep leg (``run_supervised_grid``) records a marker so the ordering proves
    it ran between the keep_awake enter and exit — a sleeping Mac can no longer
    pause a multi-day sweep mid-flight.
    """
    import contextlib
    from collections.abc import Iterator

    from brutex.observability import power_keepawake

    lake_root, catalog_path = _persist(tmp_path)
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")
    marks: list[str] = []

    @contextlib.contextmanager
    def _recording_keep_awake(reason: str) -> Iterator[None]:
        marks.append(f"enter:{reason}")
        try:
            yield
        finally:
            marks.append("exit")

    monkeypatch.setattr(power_keepawake, "keep_awake", _recording_keep_awake)

    real = sweep_supervisor.run_supervised_grid

    def _spy(*args: Any, **kwargs: Any) -> Any:
        marks.append("sweep")  # the SWEEP leg ran INSIDE the keep-awake window
        return real(*args, **kwargs)

    monkeypatch.setattr(sweep_supervisor, "run_supervised_grid", _spy)

    rc = _sweep_all(lake_root, catalog_path, tmp_path / "sweeps")

    assert rc == 0
    assert marks[0] == "enter:brutex sweep-all"
    assert marks[-1] == "exit"
    assert "sweep" in marks
    assert marks.index("enter:brutex sweep-all") < marks.index("sweep") < marks.index("exit")


def test_cmd_sweep_all_small_grid_auto_resolves_sequential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR-0016 small-grid fallback END-TO-END: a real tiny sweep runs 1 worker.

    With ``BRUTEX_SWEEP_WORKERS`` UNSET on a (simulated) 14-core box — where the
    AUTO count alone would be 8 — the launcher threads the grid's real
    ``count_sweep_tuples`` into ``resolve_sweep_workers``, the tiny grid
    (``tuple_count < 2 × auto``) resolves to 1, and the SEQUENTIAL
    ``run_supervised_grid`` path runs; the parallel driver is never dispatched.
    """
    lake_root, catalog_path = _persist(tmp_path)
    monkeypatch.delenv("BRUTEX_SWEEP_WORKERS", raising=False)
    monkeypatch.delenv("BRUTEX_SWEEP_SMALL_GRID_FACTOR", raising=False)
    # resolve_sweep_workers defaults its probe to os.cpu_count AT CALL TIME, so
    # this patch is honored — a 14-core box, auto = 8 (the fallback's teeth:
    # without the small-grid rule this run WOULD dispatch the parallel driver).
    monkeypatch.setattr("os.cpu_count", lambda: 14)

    sequential_runs: list[bool] = []
    real = sweep_supervisor.run_supervised_grid

    def _spy(*args: Any, **kwargs: Any) -> Any:
        sequential_runs.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(sweep_supervisor, "run_supervised_grid", _spy)

    def _never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a small grid must never dispatch the parallel driver")

    monkeypatch.setattr(sweep_parallel, "run_parallel_grid", _never)

    rc = _sweep_all(lake_root, catalog_path, tmp_path / "sweeps")

    assert rc == 0
    assert sequential_runs == [True]  # exactly one sequential grid run
    out = capsys.readouterr().out
    assert "parallel mode" not in out  # the parallel banner never printed
    assert "sweep-all done" in out


def test_cmd_sweep_all_big_grid_auto_dispatches_parallel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR-0016 AUTO path END-TO-END: a big-enough grid actually goes parallel.

    The exact inverse of the small-grid test above (review MINOR-2): with
    ``BRUTEX_SWEEP_WORKERS`` UNSET on a (simulated) 4-core box (auto = 2,
    small-grid threshold = 2 × 2 = 4), a REAL 4-tuple grid (2 directions ×
    2 k-values — exactly AT the strict boundary, so NOT small) dispatches the
    PARALLEL driver through ``cmd_sweep_all`` for real; the sequential
    ``run_supervised_grid`` is poisoned to fail the test if entered. A future
    regression at the composition point (e.g. the resolver called before the
    grid exists → a wrong ``tuple_count`` demoting everything to sequential)
    trips the poison.
    """
    lake_root, catalog_path = _persist(tmp_path)
    monkeypatch.delenv("BRUTEX_SWEEP_WORKERS", raising=False)
    monkeypatch.delenv("BRUTEX_SWEEP_SMALL_GRID_FACTOR", raising=False)
    monkeypatch.setattr("os.cpu_count", lambda: 4)  # auto = 2 → threshold 4

    def _never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a big grid on the AUTO path must dispatch the parallel driver")

    monkeypatch.setattr(sweep_supervisor, "run_supervised_grid", _never)

    rc = _sweep_all(
        lake_root,
        catalog_path,
        tmp_path / "sweeps",
        directions="BUY,SELL",
        k="2,3",  # 2 × 2 = 4 tuples == 2·auto → NOT small (strict `<` boundary)
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "parallel mode — 2 workers" in out  # the AUTO count, not an env override
    assert "sweep-all done" in out


def test_cmd_sweep_all_keyboard_interrupt_never_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator Ctrl-C propagates with NO halt alert (BaseException pass-through)."""
    lake_root, catalog_path = _persist(tmp_path)
    fired: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", fired.append)

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(sweep_supervisor, "run_supervised_grid", _raise)
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")

    with pytest.raises(KeyboardInterrupt):
        _sweep_all(lake_root, catalog_path, tmp_path / "sweeps")

    assert fired == []


# ────────────────────────────────────────────────────────────────────────
# SLA-honesty launch guard wiring (DEC-SLA-LAUNCH-GUARD-001)
#
# ``cmd_sweep_all`` is pragma-no-cover, so — exactly like the dedup/abort
# tests above — these are the HONEST functional validation of the guard's
# production wiring (hostile-review MAJOR-1): a mutation swapping the
# REFUSED branch, or deleting the guard block, fails here.
# ────────────────────────────────────────────────────────────────────────


def test_sweep_all_sla_guard_refuses_over_envelope_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An over-envelope projection REFUSES the launch with the honest message —
    EXIT_USER_ERROR, no sweep artifacts, no false abort alert."""
    lake_root, catalog_path = _persist(tmp_path)
    results_root = tmp_path / "sweeps"
    fired: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", fired.append)
    # 1 tuple × 1e7 s ≈ 115 days ≫ the §8 4-day envelope.
    monkeypatch.setenv("BRUTEX_SWEEP_PER_TUPLE_SECONDS", "1e7")
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")

    rc = _sweep_all(lake_root, catalog_path, results_root)

    assert rc == cli.EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err
    assert "BRUTEX_SWEEP_PER_TUPLE_SECONDS" in captured.err
    assert "does NOT certify" in captured.err  # never claims the §8 SLA either way
    assert "sweep-all done" not in captured.out  # the sweep never launched …
    assert not (results_root / "audit").exists()  # … and wrote nothing
    assert fired == []  # a refusal is not an abort — never a false alert


def test_sweep_all_sla_acknowledge_proceeds_with_loud_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--acknowledge-sla-overrun proceeds past the same projection, LOUDLY."""
    lake_root, catalog_path = _persist(tmp_path)
    monkeypatch.setenv("BRUTEX_SWEEP_PER_TUPLE_SECONDS", "1e7")
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")

    rc = _sweep_all(lake_root, catalog_path, tmp_path / "sweeps", acknowledge_sla_overrun=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "SLA OVERRUN ACKNOWLEDGED" in out  # the loud warning …
    assert "sweep-all done" in out  # … and the sweep genuinely ran


def test_sweep_all_sla_malformed_env_is_clean_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mistyped calibration knob is EXIT_USER_ERROR naming the env var —
    no traceback, no false abort alert (mirrors the cache-knob env test)."""
    lake_root, catalog_path = _persist(tmp_path)
    fired: list[Exception] = []
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", fired.append)
    monkeypatch.setenv("BRUTEX_SWEEP_PER_TUPLE_SECONDS", "not-a-number")
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")

    rc = _sweep_all(lake_root, catalog_path, tmp_path / "sweeps")

    assert rc == cli.EXIT_USER_ERROR
    assert "BRUTEX_SWEEP_PER_TUPLE_SECONDS" in capsys.readouterr().err
    assert fired == []


def test_sweep_all_sla_nan_env_is_clean_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A NaN override — which bypasses a bare "> 0" check — is refused as the
    SAME clean user-config error (hostile-review MINOR-1 regression)."""
    lake_root, catalog_path = _persist(tmp_path)
    monkeypatch.setenv("BRUTEX_SWEEP_PER_TUPLE_SECONDS", "nan")
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")

    rc = _sweep_all(lake_root, catalog_path, tmp_path / "sweeps")

    assert rc == cli.EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "BRUTEX_SWEEP_PER_TUPLE_SECONDS" in err
    assert "nan days" not in err  # never a silently-poisoned projection
