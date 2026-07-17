"""Daily quota tracker — a VENDOR-NEUTRAL primitive, DORMANT under Groww.

DEC-QUOTA-VERDICT-MOOT-001: the live vendor (Groww) has **no daily quota** for
backtesting (``GROWW_DAILY_QUOTA_UNVERIFIED=None``, operator-confirmed
2026-06-13 — CLAUDE.md §2 Rule 6 / §6), so ZERO production sites construct this
tracker (AST-pinned in ``tests/unit/test_quota_verdict_moot.py``). The class is
KEPT as the single injection seam for a hypothetical future capped vendor —
wire one instance into ``RateController(quota_tracker=...)`` at the two
controller build sites and the whole enforcement chain (the bucket's
``acquire_or_raise``, the ``stop_quota`` until-clean brake, the
``quota_exhausted`` verdict, and the raise-path Telegram halt alert) lights up
with zero new code. The 100,000/day figure cited by the historical decisions
below was the PRIOR vendor's documented limit, not a Groww fact.

Historically per MIGRATION.md item 91 (quota is global to the vendor API key,
not per-run, hence persisted at ``~/.brutex/quota/quota.lmdb/``).

The IST-midnight day-boundary policy is locked by DEC-P0-028 (PR #50a
foundation, MIGRATION.md entry #166): counter resets at ``00:00:00 IST``
each calendar day; muhurat session counts against its calendar date;
weekends/holidays reset normally; rationale: simplest, weekend-quota-unused
is fine because Track 1 backfill is bursty + spec'd at <0.1% daily quota.

## Design constraints (DEC-P0-029 pre-impl 4-agent hostile review)

* **Single-tuple LMDB persistence** — one key (``b"quota"``) per LMDB env,
  value = ``canonical_json({"date": "YYYY-MM-DD", "count": int})``. Single
  write txn per ``acquire_or_raise()`` call; no torn writes possible (Agent
  3 §B).
* **threading.Lock discipline** — every public method acquires the lock for
  the FULL critical section (check + rollover + threshold-set + LMDB write
  + threshold-set update). Mirrors :class:`AdaptiveTokenBucket` pattern at
  ``brutex/orchestrator/rate_limiter.py`` (Agent 1 §G).
* **Constructor-injected clock** — production default is
  ``lambda: datetime.now(ZoneInfo("Asia/Kolkata"))``; tests inject synthetic
  clocks. No env-var hook (Agent 2 §B clock-skew-attack defence).
* **Categorical audit payload** — emissions use ``{"event":
  "quota_threshold", "level": "80pct"|"95pct"|"exceeded"}`` only. No raw
  count, no date, no token in the payload (Agent 2 §C, Agent 3 §E).
* **Audit emit AFTER LMDB commit** — if commit raises, audit is NOT
  emitted; threshold-set update is NOT applied. The audit chain only
  records actions that completed (Agent 3 §E).
* **safe_message on all errors** — :class:`DailyQuotaExceeded` and
  :class:`ClockSkewError` use the safe_message discipline from
  ``brutex/models.py:174`` (PR #50a, DEC-P0-028). ``str(exc)`` returns
  the categorical message only.
* **Clock-skew detection** — if persisted date > current IST date, raise
  :class:`ClockSkewError` rather than silently reset (Agent 3 §C).
* **No auto-resume after exhaustion** — when ``DailyQuotaExceeded`` raises,
  the adapter maps it to ``Outcome.FATAL`` (PR #50a) which halts the
  phase. Operator must restart explicitly after IST midnight or via the
  escape hatch (delete the LMDB env file). NO automatic midnight resume
  (Agent 3 §I — would be Rule-3 scope creep + Rule-11 false-OK).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import lmdb

from brutex.calendars.nse import IST
from brutex.models import BruteXError

#: LEGACY vendor-neutral DEFAULT daily limit — the PRIOR vendor's documented
#: 100,000/day figure, NOT a Groww fact (Groww backtesting has no daily quota:
#: ``GROWW_DAILY_QUOTA_UNVERIFIED=None``, CLAUDE.md §2 Rule 6 / §6;
#: DEC-QUOTA-VERDICT-MOOT-001). Only reachable when a future capped vendor
#: constructs a tracker without an explicit ``daily_limit``.
DAILY_QUOTA_LIMIT: Final[int] = 100_000

#: Threshold percentages (0.0..1.0) that emit a categorical audit entry
#: when crossed. Computed against ``daily_limit`` at runtime so the tracker
#: scales for both production (limit=100_000) and tests (limit=10).
#: Order matters: lowest first so the iteration stops at the first
#: uncrossed level.
_THRESHOLD_PCTS: Final[tuple[tuple[float, str], ...]] = (
    (0.80, "80pct"),
    (0.95, "95pct"),
    (1.00, "exceeded"),
)

#: IST timezone instance used by the production clock default. Held at
#: module scope to avoid re-constructing on every call — the ONE canonical
#: :data:`brutex.calendars.nse.IST` root (DEC-TIMESTAMP-IST-SINGLESOURCE-001).
_IST: Final[ZoneInfo] = IST

#: LMDB key under which the quota state tuple is stored. Single key per
#: env — single write txn per acquire — no torn writes possible.
_LMDB_KEY: Final[bytes] = b"quota"

#: LMDB env map size for the quota store. The stored value is a
#: ~50-byte JSON blob under one key, so 1 MiB is generous. LMDB grows
#: the file on demand but never above this ceiling.
_DEFAULT_MAP_SIZE_BYTES: Final[int] = 1 * 1024 * 1024

#: Process-wide LMDB environment registry: resolved path → (env, refcount).
#:
#: LMDB forbids opening the same environment twice within one process
#: (``lmdb.Error: ... is already open in this process``), so EVERY
#: :class:`DailyQuotaTracker` on the same ``store_path`` must share ONE
#: cached env (DEC-QUOTA-ENV-SINGLE-OPEN-001). HISTORICAL discovery, in a
#: live operator run: the dashboard's then-lazy ``/api/v1/quota`` tracker
#: (DEC-DASHBOARD-QUOTA-PERSISTED-001) collided with the pull
#: controller's tracker (DEC-AUTO-RESUME-QUOTA-001) inside one uvicorn
#: process — every 3 s gauge poll raised and 500'd. BOTH construction
#: sites have since been removed (the lazy route build by
#: DEC-GROWW-NO-DAILY-QUOTA-UI-001 #718, the route itself + the controller
#: wiring per DEC-QUOTA-VERDICT-MOOT-001 / DEC-GROWW-NO-DAILY-QUOTA-001);
#: the registry stays — it guards the process-level LMDB constraint for
#: any future multi-instance use.
#:
#: This is deliberate module-level state: the uniqueness constraint is a
#: property of the PROCESS (LMDB's), so the share point must live at
#: process scope — the narrow, documented exception to the "no global
#: mutable state" rule (CLAUDE.md §4), exactly like LMDB's own internal
#: per-process registry it mirrors. Ref-counted so :meth:`close` only
#: closes the env when the LAST tracker on that path releases it. The
#: first opener's ``map_size_bytes`` wins for the process lifetime
#: (identical default everywhere; tests use distinct tmp paths).
_ENV_REGISTRY: dict[str, tuple[lmdb.Environment, int]] = {}
_ENV_REGISTRY_LOCK: Final[threading.Lock] = threading.Lock()


def _open_shared_env(key: str, store_path: Path, map_size_bytes: int) -> lmdb.Environment:
    """Return the process-shared LMDB env for *key*, opening it on first use."""
    with _ENV_REGISTRY_LOCK:
        entry = _ENV_REGISTRY.get(key)
        if entry is not None:
            env, refs = entry
            _ENV_REGISTRY[key] = (env, refs + 1)
            return env
        env = lmdb.open(
            str(store_path),
            map_size=map_size_bytes,
            subdir=True,
            create=True,
            lock=True,
        )
        _ENV_REGISTRY[key] = (env, 1)
        return env


def _release_shared_env(key: str) -> None:
    """Drop one reference to *key*'s shared env; close it on the last release."""
    with _ENV_REGISTRY_LOCK:
        env, refs = _ENV_REGISTRY[key]
        if refs <= 1:
            del _ENV_REGISTRY[key]
            env.close()
        else:
            _ENV_REGISTRY[key] = (env, refs - 1)


def _default_quota_path() -> Path:
    """Return the per-user quota LMDB env path.

    Per MIGRATION.md item 91, the vendor daily quota is global to the API
    key (not per-run), so the persistence lives in the user home — not
    under ``data/runs/<run_id>/``. The ``~/.brutex/`` convention matches
    other per-user state in this codebase (e.g., ``~/.brutex/credentials.json``
    for the vendor access token per ``docs/TRACK1_DATA_INGEST_DESIGN.md``).
    """
    return Path.home() / ".brutex" / "quota" / "quota.lmdb"


def _default_ist_clock() -> datetime:
    """Production clock — returns the current IST datetime.

    Used as the default factory for the ``clock`` constructor argument.
    Tests MUST override by passing a synthetic clock callable.
    """
    return datetime.now(_IST)


# ────────────────────────────────────────────────────────────────────────
# Exceptions
# ────────────────────────────────────────────────────────────────────────


class DailyQuotaExceeded(BruteXError):
    """Raised when the daily quota of ``DAILY_QUOTA_LIMIT`` is hit.

    The adapter layer (PR #50d retry classifier) maps this exception to
    :class:`brutex.orchestrator.state.Outcome.FATAL`. The FSM transitions
    the work item to ``QUARANTINED`` with reason ``FATAL_ERROR`` (DEC-P0-028
    PR #50a). Pipeline halts the phase at the next boundary; operator
    restart required.

    Notes
    -----
    No automatic midnight resume — that would mask real-world quota
    exhaustion (Agent 3 §I false-OK class).
    """

    def __init__(self, detailed_message: str = "") -> None:
        super().__init__(
            detailed_message,
            safe_message="Daily vendor API quota exceeded",
        )


class ClockSkewError(BruteXError):
    """Raised when the persisted IST date is strictly in the future
    relative to the observed IST date.

    Indicates either NTP rollback, container-clock drift, or deliberate
    tamper. Recovery path documented at module level: operator verifies
    system clock (``timedatectl``) and either waits for the clock to catch
    up or deletes the quota LMDB env file as an escape hatch.

    The detailed message includes both dates for local debugging but the
    safe message used in audit emissions and structlog redacts to
    a categorical statement only (Agent 2 §B timing oracle defence).
    """

    def __init__(self, persisted: date, observed: date) -> None:
        super().__init__(
            f"Clock skew detected: persisted={persisted.isoformat()}, "
            f"observed={observed.isoformat()}",
            safe_message=(
                "Persisted quota date is in the future relative to the "
                "observed IST date; refusing to proceed."
            ),
        )


class QuotaInvariantError(BruteXError):
    """Raised when the persisted quota state violates a class invariant.

    Examples: negative count (corruption), malformed JSON, missing required
    keys. Defensive guard against on-disk tampering or LMDB-write
    interruption that left the value half-written (Agent 2 §D).
    """

    def __init__(self, detailed_message: str) -> None:
        super().__init__(
            detailed_message,
            safe_message="Persisted quota state is corrupt or malformed",
        )


# ────────────────────────────────────────────────────────────────────────
# DailyQuotaTracker
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class DailyQuotaTracker:
    """Persisted, lock-serialised, IST-midnight daily quota counter.

    Parameters
    ----------
    daily_limit
        Maximum API calls allowed per IST calendar day. Default is the
        LEGACY vendor-neutral :data:`DAILY_QUOTA_LIMIT` (100,000 — the prior
        vendor's figure, not a Groww fact; DEC-QUOTA-VERDICT-MOOT-001).
        Lower values useful in tests.
    store_path
        Filesystem path for the LMDB env (a directory because
        ``subdir=True``). Default is ``~/.brutex/quota/quota.lmdb/`` per
        MIGRATION.md item 91.
    clock
        Callable returning the current IST datetime. Production default
        uses :func:`_default_ist_clock`. Tests inject synthetic clocks to
        cover day-boundary edge cases.
    map_size_bytes
        LMDB env map size ceiling. Default 1 MiB — generous for the
        single-key, ~50-byte payload.

    Notes
    -----
    Public API is :meth:`acquire_or_raise` (the atomic check-and-increment
    primitive) + :meth:`current_count` and :meth:`remaining` (read-only
    queries). All three methods take the lock for their full critical
    section.
    """

    daily_limit: int = DAILY_QUOTA_LIMIT
    store_path: Path = field(default_factory=_default_quota_path)
    clock: Callable[[], datetime] = field(default=_default_ist_clock)
    map_size_bytes: int = _DEFAULT_MAP_SIZE_BYTES

    _env: lmdb.Environment = field(init=False)
    _env_key: str = field(init=False)
    _closed: bool = field(init=False)
    _lock: threading.Lock = field(init=False)
    _fired_today: set[str] = field(init=False)
    _thresholds: tuple[tuple[int, str], ...] = field(init=False)

    def __post_init__(self) -> None:
        if self.daily_limit <= 0:
            raise ValueError(f"daily_limit must be positive, got {self.daily_limit}")
        if self.map_size_bytes <= 0:
            raise ValueError(f"map_size_bytes must be positive, got {self.map_size_bytes}")

        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        # One env per path per PROCESS (DEC-QUOTA-ENV-SINGLE-OPEN-001):
        # a second tracker on the same path shares the cached env instead
        # of crashing on LMDB's already-open-in-this-process rule (the
        # historical gauge-vs-pull collision — see _ENV_REGISTRY above).
        self._env_key = str(self.store_path.resolve())
        self._env = _open_shared_env(self._env_key, self.store_path, self.map_size_bytes)
        self._closed = False
        self._lock = threading.Lock()
        self._fired_today = set()
        # Any failure AFTER the env refcount was incremented above (a corrupt
        # persisted state raising QuotaInvariantError in _read_state, say) must
        # release that reference before the constructor re-raises — otherwise
        # the instance never binds, close() is unreachable, and the
        # (env, refcount) entry leaks in _ENV_REGISTRY, growing on every
        # corrupt load (DEC-ORCH-AUDIT-RATE-HARDEN-001). Mirror close()'s
        # teardown exactly: flag closed + release the shared env.
        try:
            # Compute absolute threshold counts from percentages x daily_limit.
            # For daily_limit=100_000: (80_000, 95_000, 100_000).
            # For daily_limit=10 (tests): (8, 9, 10). The "exceeded" label is
            # always exactly daily_limit so the gate triggers at the right point.
            self._thresholds = tuple(
                (int(self.daily_limit * pct), label) for pct, label in _THRESHOLD_PCTS
            )

            # Rehydrate the fired-today set from the persisted date if we
            # are resuming on the same IST day. Without this, a process
            # restart at count=85_000 would re-emit the 80pct threshold on
            # the next acquire (Rule-11 false-OK: "fire once per day"
            # would actually fire twice across the restart boundary).
            persisted = self._read_state()
            if persisted is not None:
                persisted_date, persisted_count = persisted
                today = self.clock().date()
                if persisted_date == today:
                    for limit, label in self._thresholds:
                        if persisted_count >= limit:
                            self._fired_today.add(label)
        except Exception:
            self._closed = True
            _release_shared_env(self._env_key)
            raise

    def close(self) -> None:
        """Release this tracker's reference to the shared LMDB env.

        Safe to call multiple times. The underlying env is closed only
        when the LAST tracker on this ``store_path`` releases it
        (DEC-QUOTA-ENV-SINGLE-OPEN-001) — so the dashboard gauge closing
        its tracker can never yank the env out from under a live pull.
        """
        if getattr(self, "_env", None) is not None and not self._closed:
            self._closed = True
            _release_shared_env(self._env_key)

    # ── Public API ────────────────────────────────────────────────────

    def acquire_or_raise(self) -> int:
        """Atomically check + increment + persist the quota counter.

        Returns the post-increment count if quota is available. Raises
        :class:`DailyQuotaExceeded` if the limit is already reached.

        Raises
        ------
        DailyQuotaExceeded
            When ``current_count >= daily_limit`` BEFORE increment.
        ClockSkewError
            When the persisted IST date is strictly in the future
            relative to the current observed IST date.
        QuotaInvariantError
            When the persisted state is corrupt (e.g., negative count).
        """
        with self._lock:
            observed_date = self.clock().date()
            # Read + decide + write inside ONE LMDB write txn: since
            # DEC-QUOTA-ENV-SINGLE-OPEN-001 allows several tracker
            # instances on the same env in one process, the increment
            # must be linearizable ACROSS instances, not just under this
            # instance's Python lock. LMDB write txns are exclusive per
            # env, so the get-check-put below cannot interleave with
            # another instance's — no lost update possible. A raise
            # inside the block aborts the txn before any write.
            with self._env.begin(write=True) as txn:
                raw = txn.get(_LMDB_KEY)
                persisted = None if raw is None else self._decode_state(bytes(raw))

                if persisted is None:
                    current_date = observed_date
                    current_count = 0
                else:
                    current_date, current_count = persisted
                    if current_date > observed_date:
                        raise ClockSkewError(persisted=current_date, observed=observed_date)
                    if current_date < observed_date:
                        # Day rollover — reset counter + fired-today set.
                        current_date = observed_date
                        current_count = 0
                        self._fired_today.clear()

                if current_count >= self.daily_limit:
                    raise DailyQuotaExceeded(
                        f"daily_limit={self.daily_limit} already reached on "
                        f"{current_date.isoformat()}"
                    )

                new_count = current_count + 1
                # Single put under the same txn — atomic, no torn write.
                txn.put(_LMDB_KEY, self._encode_state(current_date, new_count))

            return new_count

    def current_count(self) -> int:
        """Return the persisted count for today, or 0 if no state yet.

        Read-only — does not advance the counter or trigger rollover.
        """
        with self._lock:
            persisted = self._read_state()
            if persisted is None:
                return 0
            persisted_date, persisted_count = persisted
            observed_date = self.clock().date()
            if persisted_date == observed_date:
                return persisted_count
            return 0

    def remaining(self) -> int:
        """Return how many calls remain today (clamped to >= 0)."""
        return max(0, self.daily_limit - self.current_count())

    def newly_crossed_thresholds(self) -> list[str]:
        """Return categorical labels for thresholds crossed since the
        last call to this method.

        After this method returns, the labels are added to the
        ``_fired_today`` set and will NOT be returned again until day
        rollover. This is the audit-emission hook — callers should
        invoke this AFTER ``acquire_or_raise()`` returns successfully
        and emit one audit entry per returned label.

        Caller is responsible for actually appending the audit entry —
        the tracker itself does NOT call AuditWriter directly because
        AuditWriter is not thread-safe (Agent 4 §B2) and lock-ordering
        with the caller's locks would be fragile. Caller serialises
        audit appends through their own discipline.

        Notes
        -----
        The audit-emission ordering MUST be: LMDB commit (inside
        :meth:`acquire_or_raise`) → THIS method (under tracker lock) →
        caller's audit append. If the audit append fails, the
        threshold is already in the fired-today set so it will not
        re-fire next call. That is an acceptable Rule-11 trade — we
        lose a single audit entry rather than emit twice for the same
        crossing.
        """
        with self._lock:
            persisted = self._read_state()
            if persisted is None:
                return []
            persisted_date, persisted_count = persisted
            observed_date = self.clock().date()
            if persisted_date != observed_date:
                return []
            newly_crossed: list[str] = []
            for limit, label in self._thresholds:
                if persisted_count >= limit and label not in self._fired_today:
                    self._fired_today.add(label)
                    newly_crossed.append(label)
            return newly_crossed

    # ── Internal — LMDB I/O ───────────────────────────────────────────

    def _read_state(self) -> tuple[date, int] | None:
        """Read the persisted (date, count) tuple. Returns None if empty."""
        with self._env.begin(write=False) as txn:
            raw = txn.get(_LMDB_KEY)
        if raw is None:
            return None
        return self._decode_state(bytes(raw))

    @staticmethod
    def _encode_state(current_date: date, count: int) -> bytes:
        """Encode (date, count) as canonical JSON bytes."""
        data: dict[str, Any] = {
            "date": current_date.isoformat(),
            "count": count,
        }
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("ascii")

    @staticmethod
    def _decode_state(raw: bytes) -> tuple[date, int]:
        """Decode the persisted JSON payload into a (date, count) tuple.

        Raises :class:`QuotaInvariantError` on any deviation from the
        documented schema (negative count, malformed JSON, missing keys,
        wrong types).
        """
        try:
            decoded: dict[str, Any] = json.loads(raw.decode("ascii"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise QuotaInvariantError(f"persisted quota payload is not valid JSON: {exc}") from exc

        if not isinstance(decoded, dict):
            raise QuotaInvariantError(
                f"persisted quota payload is not a dict: {type(decoded).__name__}"
            )
        if "date" not in decoded or "count" not in decoded:
            raise QuotaInvariantError(
                f"persisted quota payload missing required keys: {sorted(decoded.keys())}"
            )
        raw_date = decoded["date"]
        raw_count = decoded["count"]
        if not isinstance(raw_date, str):
            raise QuotaInvariantError(
                f"persisted quota date is not a string: {type(raw_date).__name__}"
            )
        if not isinstance(raw_count, int) or isinstance(raw_count, bool):
            raise QuotaInvariantError(
                f"persisted quota count is not an int: {type(raw_count).__name__}"
            )
        if raw_count < 0:
            raise QuotaInvariantError(f"persisted quota count is negative: {raw_count}")
        try:
            parsed_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise QuotaInvariantError(
                f"persisted quota date is not ISO-format: {raw_date!r}"
            ) from exc
        return (parsed_date, raw_count)


__all__ = [
    "DAILY_QUOTA_LIMIT",
    "ClockSkewError",
    "DailyQuotaExceeded",
    "DailyQuotaTracker",
    "QuotaInvariantError",
]
