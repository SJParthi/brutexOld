# pyright: reportPrivateUsage=none
"""``cmd_fetch`` end-to-end replay-capture wiring — the PRODUCTION seam pinned.

DEC-AUDIT-LAKE-REPLAY-ORACLE-001, round-2 review fix (MAJOR): the round-1
BLOCKER fix (the FOURTH capture drain seam, ``flush_capture_to_audit`` in
``cmd_fetch``'s ``finally`` AFTER ``drain_fetch_pipeline`` /
``drain_write_consumer``) had NO test driving ``cmd_fetch`` itself with
capture wired — ``tests/integration/test_audit_capture_pipeline.py`` proves
the seam's MECHANISM against a hand-built pipeline harness, but three killing
mutations of the CLI wiring survived the whole suite:

(a) delete ``capture=capture`` at the ``run_groww_spot_ingest_with_retry``
    call — the in-run controller drain seams go dead;
(b) delete the ``flush_capture_to_audit(capture, ingest_audit)`` call — the
    fourth seam goes dead (async-deferred records stranded);
(c) move that flush ABOVE the two pipeline drains — re-introducing the
    round-1 stranded-records BLOCKER verbatim.

This module drives the REAL ``cli.cmd_fetch`` (dry_run=False) with
``BRUTEX_AUDIT_REPLAY_CAPTURE=capture`` over the established live-path fakes
(creds gate + one synthetic index + a fake vendor adapter returning one real
bar per window — the exact ``test_cli_fetch_isolation.py`` shape; the spine,
the capture resolver, the sink stack, the ledger, and the audit chain are the
REAL production stack) and asserts from the durable chain ALONE:

* **sync path (pipeline kill-switched)** — every ``LAKE_APPEND_WRITTEN``
  event lands BEFORE the run's ``INGEST_RUN_COMPLETE`` (the in-run
  ``capture=`` seams are LIVE — kills mutation (a)), and
  ``replay_lake_from_audit`` + ``assert_lake_byte_equal(strict=True)`` are
  green over the real lake.
* **async path (default-ON pipeline, GATED consumer tail)** — the consumer
  tail is gated at its session-filter step (BEFORE the capture wrapper's
  lock, so the run's own lock-taking drain seams stay free) to execute only
  once ``drain_fetch_pipeline`` runs (a delegating spy on the COMMON drain
  helper releases the gate), so every capture record is buffered AFTER the
  run's own final drain; the chain still carries them (the FOURTH seam —
  kills mutations (b) and (c)) and strict byte-replay is green.

This also executes ``cmd_fetch``'s ``resolve_audit_capture`` wiring in the
WRAPPED (env=capture) arm — the pragma lifted from that seam in this round
rides these tests plus the pre-existing live-path suites (identity arm).
"""

from __future__ import annotations

import importlib
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.data.audit_capture import (
    DEDUP_ADD_ACTION,
    LAKE_APPEND_ACTION,
    reset_capture_enable_warnings,
)
from brutex.data.models import CandleBar
from brutex.orchestrator.audit import AuditEntry, read_entries, verify_chain
from brutex.orchestrator.lake_replay import assert_lake_byte_equal, replay_lake_from_audit

#: The KNOWN-GREEN live-path fixture range (mirrors
#: ``test_cli_fetch_isolation.py``): ~8 monthly 1-minute vendor windows, one
#: in-session bar per window, rc == EXIT_OK on BOTH pipeline arms.
_START, _END = "2021-01-01", "2021-09-01"


def _bar(start: date) -> CandleBar:
    """One in-session bar dated to the window's first day (04:05 UTC = 09:35 IST)."""
    ts = datetime(start.year, start.month, start.day, 4, 5, tzinfo=UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=1,
        open_interest=None,
    )


class _OneBarAdapter:
    """A fake live spot adapter returning one deterministic bar per window."""

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval, end
        return [_bar(start)]


def _fake_live_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake ONLY the network-shaped edges (creds gate + universe + vendor build).

    The ``tests/unit/test_cli_retry_storm.py`` / ``test_cli_fetch_isolation.py``
    live-path shape: everything below — the ingest spine, the capture
    resolver, the REAL ``ParquetBarSink``/``ParquetWriter``, the window
    ledger, and the hash-chained ingest audit — is the production stack.
    """
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink
    from brutex.universe import BRUTE_FORCE_INDICES

    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    monkeypatch.setattr(
        cli,
        "_build_live_groww_spot",
        lambda lake, catalog=None: (
            _OneBarAdapter(),
            ParquetBarSink(writer=ParquetWriter(lake_root=lake), catalog=catalog),
        ),
    )


def _capture_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable capture mode for the pull (+ re-arm the once-per-process WARN)."""
    reset_capture_enable_warnings()
    monkeypatch.setenv("BRUTEX_AUDIT_REPLAY_CAPTURE", "capture")
    monkeypatch.delenv("BRUTEX_WRITE_BATCH", raising=False)


def _chain_indices(entries: list[AuditEntry], action: str) -> list[int]:
    """Chain-order indices of every entry whose action is *action*."""
    return [i for i, entry in enumerate(entries) if entry.action == action]


def _assert_chain_replays_byte_equal(lake: Path, audit_path: Path, replay_root: Path) -> int:
    """The shared oracle tail: replay the chain alone → strict byte equality.

    Returns the number of replayed appends. STRICT mode is the mutation
    tooth: a durable append whose ``LAKE_APPEND_WRITTEN`` record never
    reached the chain leaves an un-audited live partition (or diverged
    bytes) and fails loudly — exactly what mutations (a)/(b)/(c) produce.
    """
    report = replay_lake_from_audit(audit_path, replay_root=replay_root)
    assert report.appends_replayed >= 1
    assert_lake_byte_equal(original_root=lake, replay_root=replay_root, report=report, strict=True)
    return report.appends_replayed


def test_cmd_fetch_sync_path_drains_capture_at_the_in_run_seams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SYNC (pipeline off): capture events land BEFORE ``INGEST_RUN_COMPLETE``.

    Kills mutation (a): with ``capture=capture`` deleted from the
    ``run_groww_spot_ingest_with_retry`` call, the in-run pair-drain seams go
    dead and every event could only reach the chain via the fourth
    (post-run) seam — i.e. AFTER every ``INGEST_RUN_COMPLETE`` — flipping the
    ordering assertion below. Strict byte-replay from the chain alone pins
    the capture payloads themselves.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    lake = tmp_path / "lake"
    _fake_live_path(monkeypatch)
    _capture_env(monkeypatch)
    monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")

    rc = cli.cmd_fetch(
        lake,
        _START,
        _END,
        "1m",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=1,
    )
    assert rc == cli.EXIT_OK

    audit_path = lake / "audit" / "spot-ingest.jsonl"
    assert verify_chain(audit_path) > 0
    entries = read_entries(audit_path)
    capture_idx = _chain_indices(entries, LAKE_APPEND_ACTION)
    run_complete_idx = _chain_indices(entries, "INGEST_RUN_COMPLETE")
    assert capture_idx and run_complete_idx
    # The in-run `capture=` seams are LIVE: at least the first pass's events
    # were drained onto the chain BEFORE its INGEST_RUN_COMPLETE (mutation (a)
    # would defer EVERY event past the LAST run-complete via the fourth seam).
    assert min(capture_idx) < max(run_complete_idx)
    # The L3 journal rode the CLI's LMDB dir: one `add` row per net-new bar.
    add_idx = _chain_indices(entries, DEDUP_ADD_ACTION)
    assert len(add_idx) >= 1
    assert (lake / "audit" / "dedup-live.lmdb").is_dir()

    replayed = _assert_chain_replays_byte_equal(lake, audit_path, tmp_path / "replayed")
    assert replayed == len(capture_idx)


def test_cmd_fetch_async_path_fourth_seam_lands_pipeline_deferred_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ASYNC (default-ON pipeline): the fourth seam lands drain-deferred records.

    The pipeline consumer's tail is GATED at its session-filter step to
    execute only once the COMMON ``drain_fetch_pipeline`` runs (a delegating
    spy releases the gate). The gate sits BEFORE the capture wrapper is
    entered — gating inside ``sink.append`` would block WHILE HOLDING the
    capture lock and deadlock the run's own lock-taking ``drain_pair`` /
    ``drain_remaining`` seams. So EVERY durable append (and its buffered
    capture record) happens strictly AFTER the run's own final drain — only
    ``cmd_fetch``'s post-drain ``flush_capture_to_audit(capture,
    ingest_audit)`` can land the records on the chain. Kills mutation (b)
    (flush deleted → zero events on the chain) AND mutation (c) (flush
    hoisted above the drains → the records buffered DURING the drain are
    stranded) — both fail the event-count + strict byte-replay oracle.
    """
    pytest.importorskip("polars")
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    lake = tmp_path / "lake"
    _fake_live_path(monkeypatch)
    _capture_env(monkeypatch)
    monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)  # default ON

    gate = threading.Event()

    # Gate the consumer tail at the session filter (module-level import in
    # the spot ingest tail — runs on the pipeline consumer thread, before the
    # capture-wrapped sink is touched, holding NO locks).
    ingest_mod = importlib.import_module("brutex.adapters.groww.ingest")
    real_filter = ingest_mod.filter_to_trading_session

    def _gated_filter(*args: Any, **kwargs: Any) -> Any:
        assert gate.wait(timeout=120.0), "drain_fetch_pipeline never released the gate"
        return real_filter(*args, **kwargs)

    monkeypatch.setattr(ingest_mod, "filter_to_trading_session", _gated_filter)

    # Release the gate exactly when cmd_fetch's finally enters the COMMON
    # pipeline drain (cli imports the helper by name at call time, so the
    # source-module attribute is what cmd_fetch binds) — every durable append
    # therefore runs DURING the drain, after any earlier-placed flush.
    pull_pipeline = importlib.import_module("brutex.adapters.groww.pull_pipeline")
    real_drain = pull_pipeline.drain_fetch_pipeline

    def _release_then_drain(pipeline: Any) -> None:
        gate.set()
        real_drain(pipeline)

    monkeypatch.setattr(pull_pipeline, "drain_fetch_pipeline", _release_then_drain)

    cli.cmd_fetch(
        lake,
        _START,
        _END,
        "1m",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=1,
    )
    assert gate.is_set()  # the drain ran (and released the gated appends)

    audit_path = lake / "audit" / "spot-ingest.jsonl"
    assert verify_chain(audit_path) > 0
    entries = read_entries(audit_path)
    capture_idx = _chain_indices(entries, LAKE_APPEND_ACTION)
    run_complete_idx = _chain_indices(entries, "INGEST_RUN_COMPLETE")
    assert capture_idx and run_complete_idx
    # Every durable append was deferred past the run's own drains, so ONLY the
    # fourth seam could land these — all strictly after the last RUN_COMPLETE.
    assert min(capture_idx) > max(run_complete_idx)
    # The lake genuinely carries the deferred writes…
    assert list((lake / "bars").rglob("*.parquet"))
    # …and the chain ALONE rebuilds them byte-for-byte (strict: an event
    # missing for ANY durable append would flag an un-audited partition).
    replayed = _assert_chain_replays_byte_equal(lake, audit_path, tmp_path / "replayed")
    assert replayed == len(capture_idx)
