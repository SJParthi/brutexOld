"""Shared per-run context: lake root, clock, pull-activity probe, fingerprints.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). ONE shared window-ledger
load (RSS disclosed by the pull check), ONE shared in-process dashboard client,
the pull-activity probe (journal heartbeat + pull lease — observed, never
fought; the pull lock is NEVER taken), and the per-check input-fingerprint /
retry-once TOCTOU law (§4 of the design): moved inputs retry ONCE, moved again
⇒ ``ABSTAIN(input_moved_during_check)`` — never a PASS over a torn read.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import structlog

_LOG = structlog.get_logger(__name__)

#: IST is a fixed +05:30 offset (no DST).
IST_OFFSET: Final = timedelta(hours=5, minutes=30)

#: A pull journal whose last event is older than this is treated as NOT active
#: (a crashed pull must not gate the verifier forever).
PULL_ACTIVE_STALE_S: Final[float] = 900.0

#: The cross-host pull lease self-heals at 180 s (DEC-ACCOUNT-LEASE-CROSSHOST-001);
#: a lease file older than that is dead evidence.
PULL_LEASE_STALE_S: Final[float] = 180.0

#: Sentinel distinguishing "env var was absent" from "env var was empty".
_ENV_UNSET: Final = object()


class StoreUnavailable(RuntimeError):
    """instruments.duckdb could not be snapshotted READ-ONLY (locked / unreadable).

    The acceptance path NEVER opens the instrument master read-write
    (review round 1 M1): a plain ``InstrumentStore.open`` runs migration DDL
    (an older-schema store gets its ``instruments`` table DROPped) and takes
    the RW file lock a live dashboard/pull already holds. Callers map this to
    ``ABSTAIN(no_instrument_store)`` — never a migration, never a write.
    """


def open_instrument_store_readonly(path: Path) -> Any:
    """Snapshot the instrument master into an IN-MEMORY read view (no DDL, no writes).

    Attaches the file READ-ONLY for one short-lived copy (every table verbatim
    into an in-memory DuckDB), DETACHes, and wraps the memory connection in
    the production :class:`~brutex.data.instrument_store.InstrumentStore`
    shape WITHOUT going through ``InstrumentStore.open`` (whose idempotent
    schema apply DROPs older-schema tables — a verifier must observe, never
    heal). The file handle is NOT held afterwards (review round 2 fix 6): an
    in-process production path that opens its OWN connection to the same file
    (``spot_drift.load_stock_drift_map`` → ``InstrumentStore.open`` — the
    #1482/#1646 movers drift-recovery ladder) no longer hits DuckDB's
    same-file "different configuration" refusal, so leg A stays the REAL
    production wire. Lock contention / an unreadable file raises
    :class:`StoreUnavailable` (⇒ named ABSTAIN upstream).
    """
    import duckdb

    from brutex.data.instrument_store import InstrumentStore

    mem = duckdb.connect()  # in-memory — the FILE is touched only inside the try
    try:
        quoted = str(path).replace("'", "''")
        mem.execute(f"ATTACH '{quoted}' AS src (READ_ONLY)")
        tables = [
            str(row[0])
            for row in mem.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_catalog = 'src' AND table_schema = 'main' "
                "ORDER BY table_name"
            ).fetchall()
        ]
        for table in tables:
            ident = '"' + table.replace('"', '""') + '"'
            # identifiers double-quote-escaped from information_schema output;
            # the source is a READ_ONLY attach of the operator's own master.
            mem.execute(f"CREATE TABLE {ident} AS SELECT * FROM src.{ident}")  # noqa: S608
        mem.execute("DETACH src")
    except (duckdb.Error, OSError) as exc:
        with contextlib.suppress(Exception):
            mem.close()
        raise StoreUnavailable(f"read-only snapshot of {path.name} refused: {exc}") from exc
    return InstrumentStore(conn=mem, path=path)


def stat_token(path: Path) -> str:
    """``mtime_ns:size`` content token for one file; ``absent`` when unreadable."""
    try:
        st = path.stat()
    except OSError:
        return "absent"
    return f"{st.st_mtime_ns}:{st.st_size}"


def fingerprint_paths(paths: Sequence[Path]) -> dict[str, str]:
    """Stat-token map over a check's ACTUAL sampled input files."""
    return {str(p): stat_token(p) for p in paths}


@dataclass
class AcceptanceContext:
    """Everything a check runner needs, injected once (no globals, no prompts).

    Parameters
    ----------
    lake_root
        The REAL lake under verification (read-only; writes land only under
        ``<lake_root>/acceptance/``).
    seed
        Sampling seed (the IST report date by default — daily rotation).
    now_utc
        Injected clock (tests pin it; production binds ``datetime.now(UTC)``).
    client_factory
        Optional injected in-process dashboard client factory (tests inject a
        fake; production lazily builds ``build_app`` + ``TestClient``).
    """

    lake_root: Path
    seed: str
    sample_n: int = 12
    months: int = 6
    full: bool = False
    date: str | None = None
    top_n: int = 20
    latency_rounds: int = 5
    latency_budget_overrides: Mapping[str, float] = field(default_factory=dict)
    force_during_pull: bool = False
    now_utc: Callable[[], datetime] = lambda: datetime.now(UTC)
    perf_timer: Callable[[], float] = time.perf_counter
    client_factory: Callable[[], Any] | None = None
    _ledger: Any = field(default=None, repr=False)
    _ledger_loads: int = field(default=0, repr=False)
    _client: Any = field(default=None, repr=False)
    _pull_active: bool | None = field(default=None, repr=False)
    _store: Any = field(default=None, repr=False)
    _env_lake_root_prev: Any = field(default=_ENV_UNSET, repr=False)
    _env_lake_root_set: bool = field(default=False, repr=False)
    _spot_datasets: list[tuple[str, str, str]] | None = field(default=None, repr=False)
    _fno_contracts: list[str] | None = field(default=None, repr=False)

    # ── clocks ─────────────────────────────────────────────────────────
    def now_ist(self) -> datetime:
        """The injected clock shifted to naive IST wall time."""
        return (self.now_utc().astimezone(UTC) + IST_OFFSET).replace(tzinfo=None)

    def run_date(self) -> _dt.date:
        """The IST report date (the default sampling seed's basis)."""
        return self.now_ist().date()

    # ── shared loads ───────────────────────────────────────────────────
    def ledger(self) -> Any:
        """The ONE shared window-ledger load (lazy; ``None`` when absent)."""
        if self._ledger is None:
            from brutex.data.window_ledger_store import (
                ledger_path,
                load_or_migrate_window_ledger,
            )

            if not ledger_path(self.lake_root).is_file():
                return None
            self._ledger = load_or_migrate_window_ledger(self.lake_root)
            self._ledger_loads += 1
        return self._ledger

    @property
    def ledger_loads(self) -> int:
        """How many real ledger loads happened (the single-load spy hook)."""
        return self._ledger_loads

    def drop_ledger_memo(self) -> None:
        """Forget the memoised ledger so the NEXT :meth:`ledger` re-reads disk.

        The pull check's TOCTOU retry hook (review round 2 fix 4): without
        this the retried body re-served the pre-move in-memory snapshot while
        stamping ``retried: True`` — a re-read that never happened.
        """
        self._ledger = None

    def spot_datasets(self) -> list[tuple[str, str, str]]:
        """The ONE per-run spot-dataset enumeration (memoised — never re-walked)."""
        if self._spot_datasets is None:
            from brutex.acceptance.sampling import enumerate_spot_datasets

            self._spot_datasets = enumerate_spot_datasets(self.lake_root)
        return self._spot_datasets

    def fno_contracts(self) -> list[str]:
        """The ONE per-run F&O contract-dir enumeration (memoised)."""
        if self._fno_contracts is None:
            from brutex.acceptance.sampling import enumerate_fno_contracts

            self._fno_contracts = enumerate_fno_contracts(self.lake_root)
        return self._fno_contracts

    def client(self) -> Any:
        """The ONE shared in-process dashboard client (surface-trial precedent)."""
        if self._client is None:
            factory = self.client_factory or self._default_client_factory
            self._client = factory()
        return self._client

    def _default_client_factory(self) -> Any:
        """Build the real ``build_app`` + ``TestClient`` pair against the lake.

        The instrument master is opened READ-ONLY (never ``InstrumentStore.open``
        — its migration DDL would DROP an older-schema store's rows and its RW
        handle contends with a live dashboard/pull; review round 1 M1). A
        locked/unreadable master degrades to ``instrument_store=None`` (the
        store-backed routes 503 → the movers/latency checks ABSTAIN loudly).
        """
        from fastapi.testclient import TestClient

        from brutex.dashboard.app import DashboardState, build_app

        store = None
        store_path = self.lake_root / "instruments.duckdb"
        if store_path.is_file():
            try:
                store = open_instrument_store_readonly(store_path)
            except StoreUnavailable as exc:
                _LOG.warning(
                    "acceptance.store_readonly_unavailable",
                    path=repr(str(store_path)),
                    error=str(exc),
                )
        if store is None:
            # No usable master beside the lake: the app resolves the lake via
            # env — SCOPED to this run and restored by :meth:`close` (MN1).
            self._env_lake_root_prev = os.environ.get("BRUTEX_LAKE_ROOT", _ENV_UNSET)
            self._env_lake_root_set = True
            os.environ["BRUTEX_LAKE_ROOT"] = str(self.lake_root)
        self._store = store
        app = build_app(DashboardState(), instrument_store=store)
        # The dashboard's Host allowlist is loopback-only (R2-DS-F1) — the
        # default "testserver" base would 400 every probe.
        return TestClient(app, base_url="http://localhost")

    def close(self) -> None:
        """Release run-scoped resources: client, snapshot store, env override.

        Idempotent; called from ``run_acceptance``'s ``finally`` so an
        in-process caller (the F1 nightly chain) never inherits a mutated
        ``BRUTEX_LAKE_ROOT`` or a lingering DuckDB handle. The env restore
        runs in a ``finally`` chain so even a ``BaseException`` out of a
        ``close()`` (e.g. KeyboardInterrupt) cannot skip it (round 2 NIT).
        """
        try:
            client, self._client = self._client, None
            if client is not None and hasattr(client, "close"):
                try:
                    client.close()
                except Exception as exc:
                    _LOG.warning("acceptance.client_close_failed", error=str(exc))
        finally:
            try:
                store, self._store = self._store, None
                if store is not None and hasattr(store, "close"):
                    try:
                        store.close()
                    except Exception as exc:
                        _LOG.warning("acceptance.store_close_failed", error=str(exc))
            finally:
                if self._env_lake_root_set:
                    self._env_lake_root_set = False
                    prev = self._env_lake_root_prev
                    self._env_lake_root_prev = _ENV_UNSET
                    if prev is _ENV_UNSET:
                        os.environ.pop("BRUTEX_LAKE_ROOT", None)
                    else:
                        os.environ["BRUTEX_LAKE_ROOT"] = str(prev)

    # ── pull-activity probe (observed, never fought) ───────────────────
    def pull_active(self) -> bool:
        """True when a pull looks live: unfinished journal run OR a fresh lease."""
        if self._pull_active is None:
            self._pull_active = self._probe_pull_active()
        return self._pull_active

    def _probe_pull_active(self) -> bool:
        now = self.now_utc()
        if _journal_run_live(self.lake_root, now=now):
            return True
        return _lease_fresh(self.lake_root / ".locks" / "pull_lease.json", now=now)

    # ── input fingerprints + retry-once TOCTOU law ─────────────────────
    def run_fingerprinted(
        self,
        paths_fn: Callable[[], Sequence[Path]],
        compute: Callable[[], Any],
        *,
        on_retry: Callable[[], None] | None = None,
    ) -> tuple[Any, dict[str, Any], bool]:
        """Run *compute* under the stat-token fingerprint + retry-once law.

        Returns ``(result, {"moved", "retried", "movers"}, ok)`` — ``ok=False``
        means the inputs moved on BOTH attempts and the caller must ABSTAIN
        (``input_moved_during_check``), never PASS over a torn read.
        *on_retry* (if given) runs once, BEFORE the second attempt only — the
        hook a check uses to drop memoised state so the retry genuinely
        re-reads its inputs (review round 2 fix 4: pull's retried body had
        re-served the cached ledger snapshot).
        """
        moved_paths: list[str] = []
        for attempt in (0, 1):
            if attempt and on_retry is not None:
                on_retry()
            paths = list(paths_fn())
            before = fingerprint_paths(paths)
            result = compute()
            after = fingerprint_paths(list(paths_fn()))
            moved_paths = sorted(
                {k for k in before if before[k] != after.get(k, "absent")}
                | {k for k in after if k not in before}
            )
            if not moved_paths:
                return result, {"moved": attempt > 0, "retried": attempt > 0}, True
            _LOG.warning(
                "acceptance.input_moved",
                attempt=attempt,
                # repr() per the house #1594 log-injection convention — the
                # path embeds vendor-influenceable dir names (MN4).
                movers=[repr(p) for p in moved_paths[:5]],
            )
        return (
            None,
            {"moved": True, "retried": True, "movers": moved_paths[:20]},
            False,
        )


def _journal_run_live(lake_root: Path, *, now: datetime) -> bool:
    """True when the pull journal's last run is unfinished AND recently beating."""
    from brutex.observability.pull_journal import last_run, read_journal

    try:
        events = read_journal(lake_root)
    except OSError:
        return False
    run = last_run(events)
    if run is None or run.finished is not None:
        return False
    tail = run.heartbeats[-1] if run.heartbeats else run.started
    try:
        stamp = datetime.fromisoformat(str(tail.wall_utc))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (now - stamp).total_seconds() < PULL_ACTIVE_STALE_S


def _lease_fresh(lease_path: Path, *, now: datetime) -> bool:
    """True when the cross-host pull lease file exists and is not self-heal-stale."""
    try:
        st = lease_path.stat()
    except OSError:
        return False
    age = now.timestamp() - st.st_mtime
    return age < PULL_LEASE_STALE_S
