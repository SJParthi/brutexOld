"""Shared per-run context: lake root, clock, pull-activity probe, fingerprints.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). ONE shared window-ledger
load (RSS disclosed by the pull check), ONE shared in-process dashboard client,
the pull-activity probe (journal heartbeat + pull lease — observed, never
fought; the pull lock is NEVER taken), and the per-check input-fingerprint /
retry-once TOCTOU law (§4 of the design): moved inputs retry ONCE, moved again
⇒ ``ABSTAIN(input_moved_during_check)`` — never a PASS over a torn read.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
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

    # ── clocks ─────────────────────────────────────────────────────────
    def now_ist(self) -> datetime:
        """The injected clock shifted to naive IST wall time."""
        return (self.now_utc().astimezone(UTC) + IST_OFFSET).replace(tzinfo=None)

    def run_date(self) -> date:
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

    def client(self) -> Any:
        """The ONE shared in-process dashboard client (surface-trial precedent)."""
        if self._client is None:
            factory = self.client_factory or self._default_client_factory
            self._client = factory()
        return self._client

    def _default_client_factory(self) -> Any:
        """Build the real ``build_app`` + ``TestClient`` pair against the lake."""
        from fastapi.testclient import TestClient

        from brutex.dashboard.app import DashboardState, build_app
        from brutex.data.instrument_store import InstrumentStore

        store = None
        store_path = self.lake_root / "instruments.duckdb"
        if store_path.is_file():
            store = InstrumentStore.open(store_path)
        else:
            # No master beside the lake: the app resolves the lake via env.
            os.environ["BRUTEX_LAKE_ROOT"] = str(self.lake_root)
        app = build_app(DashboardState(), instrument_store=store)
        # The dashboard's Host allowlist is loopback-only (R2-DS-F1) — the
        # default "testserver" base would 400 every probe.
        return TestClient(app, base_url="http://localhost")

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
    ) -> tuple[Any, dict[str, Any], bool]:
        """Run *compute* under the stat-token fingerprint + retry-once law.

        Returns ``(result, {"moved", "retried", "movers"}, ok)`` — ``ok=False``
        means the inputs moved on BOTH attempts and the caller must ABSTAIN
        (``input_moved_during_check``), never PASS over a torn read.
        """
        moved_paths: list[str] = []
        for attempt in (0, 1):
            paths = list(paths_fn())
            before = fingerprint_paths(paths)
            result = compute()
            after = fingerprint_paths(list(paths_fn()))
            moved_paths = sorted(
                set(k for k in before if before[k] != after.get(k, "absent"))
                | set(k for k in after if k not in before)
            )
            if not moved_paths:
                return result, {"moved": attempt > 0, "retried": attempt > 0}, True
            _LOG.warning(
                "acceptance.input_moved",
                attempt=attempt,
                movers=moved_paths[:5],
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
