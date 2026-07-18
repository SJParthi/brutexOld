"""Cross-process single-flight lock for a Groww account pull (DEC-ACCOUNT-PULL-SINGLEFLIGHT-001).

The Groww rate controller (:class:`~brutex.adapters.rate_common.RateController`) paces a
pull within ONE process: an adaptive per-second token bucket + a 500 req/min governor
(CLAUDE.md §2 Rule 6). That budget is **per process**, so two pulls running at the same
time — e.g. a CLI ``brutex fetch`` AND a dashboard pull sharing one Groww account /
one ``BRUTEX_LAKE_ROOT`` — each get their OWN ceiling, and the Groww ACCOUNT sees the
SUM of both rates (up to ~16 r/s at the 8 r/s production ceiling). That doubles the
account's request rate, defeating the rate safety the controller exists to provide, and
risks a sustained 429 storm or an account ban (CLAUDE.md Golden Rule #6).

This module adds a **cross-process single-flight gate**: only ONE pull may run at a
time per account/lake. It takes a NON-BLOCKING advisory :func:`fcntl.flock`
(``LOCK_EX | LOCK_NB``) on a stable lock file under the lake root
(``<lake_root>/.locks/pull_singleflight.lock``). If the lock is already held — a peer
process is mid-pull — the acquire RAISES :class:`PullAlreadyRunningError` immediately
rather than blocking or silently doubling the account rate; the caller refuses to start
the second pull and surfaces a clear, actionable message.

Design notes (COMMON · O(1) · honest):

* **Account-wide, not per-partition.** Unlike
  :func:`~brutex.data.partition_lock.partition_write_lock` (one lock PER month
  partition, so the pull's fan-out keeps its parallelism), this is ONE lock for the
  WHOLE pull — it must serialise the entire account's request stream, not a single
  file. The two locks are complementary: this gate stops a SECOND concurrent pull from
  starting; the partition lock still serialises any same-partition writers that remain.
* **Non-blocking, fail-fast.** A pull is a long (often multi-hour) job; a blocking
  acquire would silently queue a second pull behind the first for hours. Instead the
  acquire is ``LOCK_NB`` and raises on contention, so the operator gets an immediate
  "a pull is already running" instead of a mysteriously stalled second pull.
* **Stable lock file, never replaced.** The lock file is opened + locked, never
  ``os.replace``-d, so the ``flock`` always guards the same inode for the life of the
  process (mirroring the partition-lock sidecar reasoning). It lives under the
  dot-prefixed ``.locks/`` sibling of ``bars/`` + ``options/`` so no lake scanner /
  dataset glob ever sees it.
* **No stale-lock recovery needed.** An ``flock`` is released by the kernel when the
  holding fd is closed OR the process dies, so a crashed pull never leaves a stuck lock
  (unlike a PID file). The fd is held for the LIFETIME of the pull and closed (which
  releases the lock) when the pull finishes or errors.

**Honest envelope (Golden Rule #10):** this guards against TWO concurrent pulls sharing
one lake root; it is the account-rate twin of the partition-write lock, NOT a
distributed lock across hosts (an ``flock`` is local to one machine / one filesystem)
and NOT a queue (the second pull is REFUSED, not deferred). ``fcntl.flock`` is POSIX
(Linux + macOS — the Track-1 hardware envelope); Windows is out of scope.

**RESIDUAL — the lake-root key alone does NOT cover two lake roots; the ACCOUNT-keyed
second lock now does (single host).** The lake-root lock file lives UNDER ``lake_root``
(see :func:`pull_singleflight_lock_path`), so on its own it serialises only pulls that
share ONE lake root — the SAME Groww account pulled into TWO distinct
``BRUTEX_LAKE_ROOT`` s took DIFFERENT lock files, BOTH acquired, and the account still
saw the SUM of both rates (~16 r/s at the 8 r/s ceiling; the residual flagged by
DEC-PULL-HARDENING-FOLLOWUP-001, MIGRATION.md #996). That residual is now CLOSED for the
single-host case by :func:`account_pull_singleflight_lock`
(DEC-ACCOUNT-KEY-SINGLEFLIGHT-001, MIGRATION.md #1285): a SECOND flock keyed by a
Groww ``api_key`` fingerprint under ``~/.brutex/locks/pull_account<pbkdf2[:16]>.flock``
(a PBKDF2-HMAC-SHA256 derivation with a fixed app salt — deliberately expensive so the
filename never invites offline guessing of the credential it fingerprints), taken FIRST
(before the lake-root lock) at both production pull sites. HONEST envelope:
the key is the AWS-resolved ``api_key`` (DEC-AWS-ONLY-CREDS-001 — the retired env-var
path is NEVER read); the lock dir is PER-USER (``Path.home()``), so two OS users pulling
one account are still NOT covered (a system-wide ``/tmp``-style dir has its own
permission trade-offs) — that stays a NAMED follow-up, as does keying on the vendor
feed-token instead of the ``api_key``. The two-HOSTS gap now has the advisory
cross-host LEASE arm below (DEC-ACCOUNT-LEASE-CROSSHOST-001 — best-effort DETECTION on
shared lake storage, honestly NOT mutual exclusion; a cross-host distributed lock stays
out of scope for an ``flock``). A credentials-less run (dry-run / no AWS) SKIPS the
account lock gracefully — it makes no vendor calls, so there is no account rate to
protect. (See ``docs/runbooks/data-fetch-runday.md`` §6a for the operator note.)

**CROSS-HOST arm — an advisory FOREIGN-PULL LEASE, honestly NOT a lock
(DEC-ACCOUNT-LEASE-CROSSHOST-001).** Both flocks above are kernel-local: two HOSTS
mounting ONE shared lake (NFS / SMB / a network volume) each take their OWN host's
locks, BOTH acquire, and the account again sees the SUM of both rates — the #1285 named
follow-up. A true cross-host mutex is NOT built (``flock`` over NFS is
implementation-dependent; a distributed lock service is out of scope), and this module
REFUSES to pretend one exists. Instead the pull now keeps a best-effort **heartbeat
LEASE** on the shared storage itself (``<lake_root>/.locks/pull_lease.json``, written
atomically at start + refreshed every :data:`PULL_LEASE_HEARTBEAT_S` by a daemon
thread, conditionally removed at exit): a starting pull that reads a FRESH lease
(heartbeat younger than :data:`PULL_LEASE_STALE_AFTER_S`) from a DIFFERENT host
identity is REFUSED via :class:`ForeignPullActiveError` — DETECTION, not exclusion.
The lease guard is entered strictly AFTER both flocks (one fixed order everywhere), so
a SAME-host lease can be ignored SOUNDLY: holding the flocks proves no live same-host
pull exists, hence any same-host lease is a crashed/finished pull's residue.

**Honest cross-host envelope (Golden Rule #10 — NO pretend mutual exclusion):**

* **Not a mutex.** Two pulls starting within one read→write propagation window BOTH
  pass the check and BOTH run (a TOCTOU race the lease cannot close); the lease only
  narrows the window from "always" to "a few seconds of racing starts".
* **Wall clocks.** Freshness compares ``time.time()`` ACROSS hosts. A writer whose
  clock lags by more than the TTL looks stale (a live foreign pull is MISSED); a
  writer whose clock leads looks fresh a little longer after death (a dead pull
  blocks foreign starts until TTL + skew elapses, then self-heals).
* **Shared storage only.** The lease lives under the lake root — hosts that do NOT
  actually share that filesystem see only their own file and detect nothing. The same
  account pulled into two UNRELATED lake roots on two hosts remains UNCOVERED (the
  account-keyed flock covers that case on ONE host only).
* **Host identity is the hostname.** Two hosts reporting the SAME ``gethostname()``
  (identically-named containers) read as same-host → no detection.
* **Non-BruteX consumers are invisible.** TickVault (which shares the token) never
  writes a lease and is neither detected nor blocked.
* **Crash recovery is TTL-bounded.** A crashed pull's lease stops heartbeating and
  goes stale after :data:`PULL_LEASE_STALE_AFTER_S` (default 180 s) — a foreign host
  is refused for at most ~3 min after a crash, never for the length of the dead pull.
* **Best-effort writes.** A lease/heartbeat write failure WARNs once and degrades to
  today's behaviour (no cross-host detection) — it can NEVER fail or halt the pull.
* **Bounded lease I/O.** The guard's synchronous lake-file ops (enter read + first
  refresh, exit ``clear``) run under the COMMON ``run_with_deadline`` freeze guard
  (:data:`PULL_LEASE_IO_DEADLINE_S`, env :data:`PULL_LEASE_IO_DEADLINE_ENV`) because
  they execute while BOTH single-flight flocks are held — a WEDGED (non-raising)
  volume would otherwise hold the flocks forever and refuse every future pull until
  process restart (the round-1 review MAJOR). A wedged op is abandoned (one
  ``pull_lease.io_wedged`` WARN, at most one zombie daemon per op — the #999 stance);
  a leftover lease self-heals by going stale within the TTL.
* **The boot-resume probe is lease-blind.**
  :func:`brutex.dashboard.pull_intent.probe_pull_singleflight` probes only the flock;
  worst case a resumed job is refused at ``start()`` by a fresh foreign lease — the
  same accepted one-noisy-refusal envelope that probe already documents.

Kill switch: ``BRUTEX_PULL_LEASE`` (:data:`PULL_LEASE_ENV`; an explicit off-value —
the shared :data:`brutex.data.derive_stamp.DISABLED_ENV_VALUES` set — disables the
lease guard entirely, e.g. on a network volume with pathological clock skew).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

import structlog

from brutex.config.deadline import (
    DeadlineExceeded,
    DeadlineSpawnError,
    resolve_env_seconds,
    run_with_deadline,
)
from brutex.data.atomic_io import atomic_write_simple, lake_write_guard
from brutex.models import BruteXError

__all__ = [
    "ACCOUNT_LOCK_FILE_PREFIX",
    "NIGHTLY_SINGLEFLIGHT_LOCK_NAME",
    "PULL_LEASE_ENV",
    "PULL_LEASE_FILE_NAME",
    "PULL_LEASE_HEARTBEAT_S",
    "PULL_LEASE_IO_DEADLINE_ENV",
    "PULL_LEASE_IO_DEADLINE_S",
    "PULL_LEASE_SCHEMA",
    "PULL_LEASE_STALE_AFTER_S",
    "PULL_SINGLEFLIGHT_LOCK_NAME",
    "SWEEP_SINGLEFLIGHT_LOCK_NAME",
    "ForeignPullActiveError",
    "NightlyAlreadyRunningError",
    "PullAlreadyRunningError",
    "PullLease",
    "SweepAlreadyRunningError",
    "account_pull_singleflight_lock",
    "account_singleflight_lock_path",
    "default_account_lock_dir",
    "foreign_pull_lease",
    "nightly_singleflight_lock",
    "nightly_singleflight_lock_path",
    "pull_lease_enabled",
    "pull_lease_guard",
    "pull_lease_path",
    "pull_singleflight_lock",
    "pull_singleflight_lock_path",
    "read_pull_lease",
    "sweep_singleflight_lock",
    "sweep_singleflight_lock_path",
]

_LOG = structlog.get_logger(__name__)

#: Sidecar lock directory under the lake root. A dot-prefixed sibling of ``bars/`` +
#: ``options/`` so it is never matched by a dataset glob or a lake scanner (mirrors
#: :data:`brutex.data.partition_lock._LOCK_SUBDIR`).
_LOCK_SUBDIR: Final[str] = ".locks"

#: The single, stable filename for the account-wide pull single-flight lock. ONE name
#: (not per-partition) so EVERY pull against this lake root contends on the same file.
PULL_SINGLEFLIGHT_LOCK_NAME: Final[str] = "pull_singleflight.lock"

#: The single, stable filename for the NIGHTLY single-flight lock
#: (DEC-SWEEP-NIGHTLY-AUTOMATION-001). Beside the pull lock so two nightly chains
#: against one lake are mechanically impossible — the same non-blocking flock core.
NIGHTLY_SINGLEFLIGHT_LOCK_NAME: Final[str] = "nightly_singleflight.lock"

#: The single, stable filename for the SWEEP single-flight lock. Keyed by the
#: sweep OUTPUT root (not the lake), so two ``sweep-all`` runs writing the SAME
#: shared checkpoint / hash-chained supervisor audit / results store are refused
#: — the second cannot corrupt the first's state (Golden Rule #8). Reuses the ONE
#: :func:`_flock_singleflight` core, exactly like the nightly lock.
SWEEP_SINGLEFLIGHT_LOCK_NAME: Final[str] = "sweep_singleflight.lock"

#: Max bytes of the lock file read back as the best-effort HOLDER NOTE when a
#: contended acquire builds its refusal message. Advisory only — a bound, not a
#: format (a note longer than this is truncated in the echo, never an error).
_HOLDER_NOTE_MAX_BYTES: Final[int] = 120

#: Filename prefix for the ACCOUNT-keyed single-flight lock (DEC-ACCOUNT-KEY-SINGLEFLIGHT-001).
#: The full name is ``pull_account<pbkdf2_hmac_sha256(api_key)[:16]>.flock`` — a truncated
#: hex KDF fingerprint ONLY, never the api_key itself (no secret in a path or a log line).
ACCOUNT_LOCK_FILE_PREFIX: Final[str] = "pull_account"

#: Hex chars of the PBKDF2 fingerprint kept in the account lock filename. 16 hex
#: chars = 64 bits — collision-safe for the handful of accounts one host ever runs,
#: short enough for a readable filename.
_ACCOUNT_DIGEST_LEN: Final[int] = 16

#: Fixed application salt for the account-lock PBKDF2 derivation. The fingerprint must
#: be DETERMINISTIC per api_key (two processes must independently derive the SAME lock
#: filename — that is the whole point), so a random per-call salt is impossible; a
#: fixed app-scoped salt still domain-separates the fingerprint from any other use of
#: the key.
_ACCOUNT_LOCK_KDF_SALT: Final[bytes] = b"brutex-pull-account-lock"

#: PBKDF2 iteration count for the account-lock fingerprint. High enough that the
#: filename cannot be used as a cheap offline-guessing oracle for the api_key
#: (CodeQL py/weak-sensitive-data-hashing — a bare sha256 of credential material is
#: guessable at GPU speed); low enough that the one derivation per pull start costs
#: ~100 ms on commodity hardware — negligible against a multi-hour pull.
_ACCOUNT_LOCK_KDF_ITERATIONS: Final[int] = 200_000


def _read_holder_note(lock_path: Path) -> str:
    """Best-effort read of the holder note a lock owner wrote into *lock_path*.

    Returns the first :data:`_HOLDER_NOTE_MAX_BYTES` bytes of the lock file,
    UTF-8-decoded (``errors="replace"``) and stripped, or ``""`` when the file is
    absent / unreadable / empty. Advisory only (DEC-SWEEP-NIGHTLY-AUTOMATION-001
    holder-note honesty): a refusal message never LIES about who holds the lock,
    and a read failure never turns a precise refusal into a crash.
    """
    try:
        with open(lock_path, "rb") as fp:
            raw = fp.read(_HOLDER_NOTE_MAX_BYTES)
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace").strip()


def _with_holder_suffix(message: str, lock_path: Path) -> str:
    """Append ``" (holder: <note>)"`` to *message* when the lock file carries a note.

    No note (the pre-holder-note state, or a holder that wrote none) ⇒ *message*
    returned byte-identical — the existing-caller compatibility contract.
    """
    note = _read_holder_note(lock_path)
    if note:
        return f"{message} (holder: {note})"
    return message


class PullAlreadyRunningError(BruteXError):
    """Another pull already holds the account-wide single-flight lock for this lake root.

    Raised by :func:`pull_singleflight_lock` when the non-blocking ``flock`` acquire
    finds the lock already held — a peer process (a CLI ``brutex fetch`` or a second
    dashboard pull) is mid-pull against the same Groww account / lake. The caller must
    REFUSE to start a second pull rather than double the account's request rate
    (CLAUDE.md Golden Rule #6). ``lock_path`` is the contended lock file (non-secret)
    for the operator-facing message + the log — this class is the ONE canonical
    owner of the attribute (subclasses pass their contended path up, never
    re-assign it; the :class:`ForeignPullActiveError` lease path rides the same
    slot so every refusal handler reads one field). When the holder wrote a note
    into the lock file (e.g. ``nightly:<id>`` — DEC-SWEEP-NIGHTLY-AUTOMATION-001)
    the default flock-flavoured message appends ``" (holder: <note>)"`` so a pull
    refused during a nightly names the nightly instead of implying another pull;
    no note ⇒ message byte-identical to the pre-note contract.
    """

    def __init__(self, lock_path: Path, message: str | None = None) -> None:
        """*message* ``None`` composes the flock-flavoured default; a subclass
        passes its own operator-facing wording (same refusal semantics)."""
        super().__init__(
            message
            if message is not None
            else _with_holder_suffix(
                "a pull is already running for this lake "
                f"(account-wide single-flight lock held at {lock_path}); "
                "refusing to start a second concurrent pull, which would double the "
                "Groww account request rate",
                lock_path,
            )
        )
        self.lock_path = lock_path


class NightlyAlreadyRunningError(BruteXError):
    """Another nightly chain already holds the nightly single-flight lock for this lake.

    Raised by :func:`nightly_singleflight_lock` on contention
    (DEC-SWEEP-NIGHTLY-AUTOMATION-001): two overlapping nightly chains against one
    lake are refused, never queued. Carries the same best-effort holder-note echo as
    :class:`PullAlreadyRunningError` so the refusal names WHICH nightly holds it.
    """

    def __init__(self, lock_path: Path) -> None:
        super().__init__(
            _with_holder_suffix(
                "a nightly chain is already running for this lake "
                f"(nightly single-flight lock held at {lock_path}); "
                "refusing to start a second concurrent nightly",
                lock_path,
            )
        )
        self.lock_path = lock_path


class SweepAlreadyRunningError(BruteXError):
    """Another sweep already holds the sweep single-flight lock for this output dir.

    Raised by :func:`sweep_singleflight_lock` on contention: a second
    ``sweep-all`` over the SAME output root would share the first's crash-safe
    resume checkpoint, its hash-chained supervisor audit JSONL (two concurrent
    appenders would break the BLAKE3 chain — Golden Rule #8), and its results
    store. The caller REFUSES to start a second sweep rather than corrupt that
    shared state. Mirrors :class:`NightlyAlreadyRunningError` (the same
    best-effort holder-note echo so the refusal can name the running sweep).
    """

    def __init__(self, lock_path: Path) -> None:
        super().__init__(
            _with_holder_suffix(
                "another sweep is already running on this output dir "
                f"(sweep single-flight lock held at {lock_path}); refusing to "
                "start a second one, which would corrupt the shared resume "
                "checkpoint / hash-chained audit / results store",
                lock_path,
            )
        )
        self.lock_path = lock_path


def pull_singleflight_lock_path(lake_root: Path) -> Path:
    """Return the stable account-wide single-flight lock path under *lake_root*.

    A SINGLE fixed path (``<lake_root>/.locks/pull_singleflight.lock``) — not keyed by
    partition — so every pull against this lake root contends on the same file. O(1).

    Parameters
    ----------
    lake_root
        The lake root; the ``.locks`` dir lives directly under it.

    Returns
    -------
    Path
        ``<lake_root>/.locks/pull_singleflight.lock`` (never created here).
    """
    return lake_root / _LOCK_SUBDIR / PULL_SINGLEFLIGHT_LOCK_NAME


@contextmanager
def pull_singleflight_lock(
    lake_root: Path, *, holder_note: str | None = None
) -> Generator[None, None, None]:
    """Hold the account-wide single-flight lock for the *lake_root* for the ``with`` block.

    Opens (creating on first use) the stable single-flight lock file and takes a
    NON-BLOCKING exclusive :func:`fcntl.flock` (``LOCK_EX | LOCK_NB``) on it for the
    duration of the ``with`` block, releasing it by closing the fd on exit. Only ONE
    pull may hold this at a time per lake root, so a SECOND concurrent pull cannot
    start and double the Groww account request rate (CLAUDE.md Golden Rule #6).

    Parameters
    ----------
    lake_root
        The lake root (locates the ``.locks`` sidecar dir). The lock file is created on
        first use; the directory is created with ``parents=True, exist_ok=True``.
    holder_note
        Optional advisory note best-effort-written into the lock file on a successful
        acquire (e.g. ``nightly:<id>`` — DEC-SWEEP-NIGHTLY-AUTOMATION-001) so a
        contended peer's :class:`PullAlreadyRunningError` names the true holder.
        ``None`` (every pre-existing caller) ⇒ behavior byte-identical.

    Yields
    ------
    None
        Control returns to the caller while the exclusive lock is held.

    Raises
    ------
    PullAlreadyRunningError
        If the lock is already held by another process (a pull is in flight) — raised
        immediately (non-blocking), so the caller refuses to start a second pull.
    LakeWriteHalt
        If creating the ``.locks`` sidecar dir / lock file hits a full disk (ENOSPC) /
        OOM. Mapped via :func:`~brutex.data.atomic_io.lake_write_guard` so the disk-full
        contract is consistent with :func:`~brutex.data.partition_lock.partition_write_lock`
        (DEC-PULL-HARDENING-FOLLOWUP-001, MIGRATION.md #996) — a raw ``OSError`` would
        otherwise escape the dashboard ``start()`` path uncaught.
    """
    with _flock_singleflight(pull_singleflight_lock_path(lake_root), holder_note=holder_note):
        yield


def nightly_singleflight_lock_path(lake_root: Path) -> Path:
    """Return the stable nightly single-flight lock path under *lake_root*.

    ``<lake_root>/.locks/nightly_singleflight.lock`` — beside the pull lock
    (:func:`pull_singleflight_lock_path`), never created here. O(1)
    (DEC-SWEEP-NIGHTLY-AUTOMATION-001).
    """
    return lake_root / _LOCK_SUBDIR / NIGHTLY_SINGLEFLIGHT_LOCK_NAME


@contextmanager
def nightly_singleflight_lock(
    lake_root: Path, *, holder_note: str | None = None
) -> Generator[None, None, None]:
    """Hold the NIGHTLY single-flight lock for *lake_root* for the ``with`` block.

    The nightly chain's own gate (DEC-SWEEP-NIGHTLY-AUTOMATION-001): only ONE nightly
    chain may run at a time per lake root, so a schedule mis-fire / manual re-run can
    never overlap a running chain. Same mechanics as :func:`pull_singleflight_lock`
    (the ONE :func:`_flock_singleflight` core — non-blocking exclusive ``flock``,
    kernel-released on fd close / process death, no pid files, no stale-lock state).

    Parameters
    ----------
    lake_root
        The lake root (locates the ``.locks`` sidecar dir).
    holder_note
        Optional advisory note (``nightly:<id>``) best-effort-written into the lock
        file so a contended peer's refusal names this nightly.

    Raises
    ------
    NightlyAlreadyRunningError
        If another process holds the nightly lock — a nightly chain is in flight.
    LakeWriteHalt
        If creating the lock dir / file hits ENOSPC / OOM (the same
        :func:`~brutex.data.atomic_io.lake_write_guard` contract as the pull locks).
    """
    with _flock_singleflight(
        nightly_singleflight_lock_path(lake_root),
        contention_error=NightlyAlreadyRunningError,
        holder_note=holder_note,
    ):
        yield


def sweep_singleflight_lock_path(out_root: Path) -> Path:
    """Return the stable sweep single-flight lock path under the sweep *out_root*.

    ``<out_root>/.locks/sweep_singleflight.lock`` — keyed by the sweep OUTPUT
    root (where the checkpoint / supervisor audit / results store live), so two
    sweeps writing the SAME shared state contend on one file, while sweeps to
    DIFFERENT output roots never contend. Never created here. O(1).
    """
    return out_root / _LOCK_SUBDIR / SWEEP_SINGLEFLIGHT_LOCK_NAME


@contextmanager
def sweep_singleflight_lock(
    out_root: Path, *, holder_note: str | None = None
) -> Generator[None, None, None]:
    """Hold the SWEEP single-flight lock for *out_root* for the whole ``with`` block.

    The sweep's cross-process gate: only ONE ``sweep-all`` may run at a time per
    output root, held for the ENTIRE (often multi-day) sweep, so a second
    concurrent sweep cannot share — and corrupt — the first's crash-safe resume
    checkpoint, its hash-chained supervisor audit JSONL (two appenders would
    break the BLAKE3 chain, Golden Rule #8), or its results store. Same mechanics
    as :func:`pull_singleflight_lock` / :func:`nightly_singleflight_lock` (the ONE
    :func:`_flock_singleflight` core — a NON-BLOCKING exclusive ``flock``,
    kernel-released on fd close / process death, so a ``kill -9`` of the holder
    never wedges a stale lock; no pid files, no stale-lock recovery state).

    Parameters
    ----------
    out_root
        The sweep output root (locates the ``.locks`` sidecar dir). The lock file
        is created on first use.
    holder_note
        Optional advisory note (e.g. ``sweep-all:<grid_id>``) best-effort-written
        into the lock file so a contended peer's refusal names this sweep.

    Raises
    ------
    SweepAlreadyRunningError
        If another process holds the sweep lock — a sweep is in flight over this
        output root; raised immediately (non-blocking), so the caller refuses.
    LakeWriteHalt
        If creating the ``.locks`` sidecar dir / lock file hits a full disk
        (ENOSPC) / OOM (the same :func:`~brutex.data.atomic_io.lake_write_guard`
        contract as the pull / nightly locks).
    """
    with _flock_singleflight(
        sweep_singleflight_lock_path(out_root),
        contention_error=SweepAlreadyRunningError,
        holder_note=holder_note,
    ):
        yield


def default_account_lock_dir() -> Path:
    """Return the default PER-USER directory for the account-keyed lock files.

    ``~/.brutex/locks`` — deliberately OUTSIDE any lake root, so two pulls of the SAME
    account into TWO distinct ``BRUTEX_LAKE_ROOT`` s still contend on ONE file
    (DEC-ACCOUNT-KEY-SINGLEFLIGHT-001). Per-user (``Path.home()``): two OS users are NOT
    covered (documented honest envelope in the module docstring). O(1), pure path
    computation — nothing is created here.
    """
    return Path.home() / ".brutex" / "locks"


def account_singleflight_lock_path(api_key: str, *, lock_dir: Path | None = None) -> Path:
    """Return the account-keyed single-flight lock path for *api_key*.

    ``<lock_dir>/pull_account<pbkdf2(api_key)[:16]>.flock`` — the filename carries ONLY
    a truncated hex PBKDF2-HMAC-SHA256 fingerprint of the api_key, NEVER the key itself,
    so no secret ever lands in a path, an ``ls``, or a log line (the fingerprint is
    stable per key and distinct across keys). PBKDF2 (``_ACCOUNT_LOCK_KDF_ITERATIONS``
    iterations over the fixed ``_ACCOUNT_LOCK_KDF_SALT``) instead of a bare sha256 so
    the visible filename is computationally expensive to brute-force back to the
    credential (CodeQL py/weak-sensitive-data-hashing). HONEST cost: the derivation is
    DELIBERATELY slow — ~100 ms once per pull start (never per fetch), negligible
    against a multi-hour pull. *lock_dir* defaults to
    :func:`default_account_lock_dir`; tests inject a tmp dir so CI never touches the
    real home. O(1) calls per pull.
    """
    fingerprint = hashlib.pbkdf2_hmac(
        "sha256",
        api_key.encode("utf-8"),
        _ACCOUNT_LOCK_KDF_SALT,
        _ACCOUNT_LOCK_KDF_ITERATIONS,
    )
    digest = fingerprint.hex()[:_ACCOUNT_DIGEST_LEN]
    base = default_account_lock_dir() if lock_dir is None else lock_dir
    return base / f"{ACCOUNT_LOCK_FILE_PREFIX}{digest}.flock"


@contextmanager
def account_pull_singleflight_lock(
    api_key: str, *, lock_dir: Path | None = None
) -> Generator[None, None, None]:
    """Hold the ACCOUNT-keyed single-flight lock for the ``with`` block.

    DEC-ACCOUNT-KEY-SINGLEFLIGHT-001 — the second flock closing the #996 residual for
    the single-host case: keyed by the Groww ``api_key`` digest (NOT the lake root), it
    lives under the per-user :func:`default_account_lock_dir`, so ONE account pulled
    into TWO lake roots at once still contends on ONE file and the second pull is
    REFUSED instead of silently doubling the account request rate (Golden Rule #6).
    Taken FIRST — strictly BEFORE the lake-root :func:`pull_singleflight_lock` — at
    every production site (one fixed lock ORDER everywhere, so two processes can never
    deadlock-invert across the pair). Same mechanics as the lake-root lock: exclusive
    non-blocking ``flock``, released by closing the fd on exit.

    Parameters
    ----------
    api_key
        The AWS-resolved Groww api_key (:func:`brutex.config.credentials.\
resolve_groww_aws_credentials` — the SOLE credential source, DEC-AWS-ONLY-CREDS-001).
        Only its PBKDF2 fingerprint is used; the key never reaches the filesystem or a
        log.
    lock_dir
        Override of the lock directory (tests — never write the real ``$HOME`` in CI).

    Raises
    ------
    PullAlreadyRunningError
        If another process holds this ACCOUNT's lock (a pull is in flight for the same
        account, possibly against a DIFFERENT lake root).
    LakeWriteHalt
        If creating the lock dir / file hits ENOSPC / OOM (the same
        :func:`~brutex.data.atomic_io.lake_write_guard` contract as the lake-root lock).
    """
    with _flock_singleflight(account_singleflight_lock_path(api_key, lock_dir=lock_dir)):
        yield


@contextmanager
def _flock_singleflight(
    lock_path: Path,
    *,
    contention_error: Callable[[Path], BruteXError] = PullAlreadyRunningError,
    holder_note: str | None = None,
) -> Generator[None, None, None]:
    """The ONE non-blocking exclusive-flock core every single-flight lock shares (COMMON).

    Creates the lock dir/file (guarded — ENOSPC/OOM → ``LakeWriteHalt``), takes
    ``LOCK_EX | LOCK_NB`` (contention → *contention_error*, default
    :class:`PullAlreadyRunningError` — every pre-existing caller passes nothing and is
    byte-identical), and releases by closing the fd on every exit. On a SUCCESSFUL
    acquire the lock file's content is best-effort truncated (so a stale note from a
    prior holder can never lie about who holds it now) and *holder_note*, when given,
    is written for contended peers' refusal messages
    (DEC-SWEEP-NIGHTLY-AUTOMATION-001) — a write failure is swallowed (advisory only,
    never a lock failure).
    """
    # The lock-file creation runs inside ``lake_write_guard`` so a full disk at that step
    # becomes a ``LakeWriteHalt`` (the same contract the partition lock honours), never a
    # raw ``OSError`` that escapes the caller uncaught.
    with lake_write_guard():
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # 0o600 — owner-only: a private coordination sidecar, never world-readable
        # (CodeQL py/overly-permissive-file-permission).
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            # The lock is held by a peer process: a pull is already running. Refuse —
            # do NOT block (a pull runs for hours) and do NOT double the account rate.
            raise contention_error(lock_path) from exc
        with contextlib.suppress(OSError):
            # Best-effort holder note: truncate any stale note first, then write ours
            # (advisory only — a failure never blocks the acquired lock).
            os.ftruncate(fd, 0)
            if holder_note is not None:
                os.write(fd, holder_note.encode("utf-8"))
        yield
    finally:
        # Closing the fd releases the advisory lock — an ``flock`` is tied to the open
        # file description, so the close both unlocks and frees the fd in one step, on
        # every (success or error) exit. On the contended path the lock was never taken
        # by THIS fd, so closing it does not touch the peer's lock.
        os.close(fd)


# ════════════════════════════════════════════════════════════════════════
# Cross-host foreign-pull LEASE (DEC-ACCOUNT-LEASE-CROSSHOST-001)
# ════════════════════════════════════════════════════════════════════════

#: The single, stable lease file name under ``<lake_root>/.locks/``. ONE file
#: (last-writer-wins) — the two flocks guarantee at most one SAME-host writer, and a
#: cross-host multi-writer race is inside the documented no-mutex envelope.
PULL_LEASE_FILE_NAME: Final[str] = "pull_lease.json"

#: Lease schema version. The reader ignores an unrecognised schema (forward-safe —
#: an old/foreign lease shape reads as honest ``None``, never a mis-decode).
PULL_LEASE_SCHEMA: Final[int] = 1

#: Seconds between two heartbeat refreshes of the held lease (daemon thread).
PULL_LEASE_HEARTBEAT_S: Final[float] = 30.0

#: A lease whose heartbeat is OLDER than this is STALE — ignored and overwritten.
#: 6× the heartbeat interval: tolerant of a few missed/slow refresh ticks on a
#: laggy network volume, yet bounds the post-crash refusal window to ~3 minutes.
PULL_LEASE_STALE_AFTER_S: Final[float] = 180.0

#: Kill-switch env var for the lease guard (an explicit off-value disables it — the
#: shared :data:`brutex.data.derive_stamp.DISABLED_ENV_VALUES` spelling set).
PULL_LEASE_ENV: Final[str] = "BRUTEX_PULL_LEASE"

#: Bounded join for the heartbeat daemon at guard exit. ``stop.wait`` returns
#: immediately once set, so this only bites when a refresh write is WEDGED on a
#: stalled volume — the daemon is then abandoned (it can hold nothing: the lease it
#: might still write is conditionally removed / goes stale), never joined forever.
_LEASE_JOIN_TIMEOUT_S: Final[float] = 5.0

#: Wall-clock bound for the lease guard's SYNCHRONOUS lake-file I/O (the enter
#: read + first refresh, and the exit ``clear``) — the round-1 review MAJOR fix:
#: these ran UNBOUNDED while BOTH single-flight flocks were held, and both
#: release orders put the exit ``clear`` AHEAD of the flock releases, so a
#: WEDGED (non-raising) lake volume — the exact scenario the sibling bounded
#: daemon join exists for — hung ``clear()`` forever and every future pull was
#: refused with ``PullAlreadyRunningError`` until process restart. Each op now
#: runs under the COMMON :func:`~brutex.config.deadline.run_with_deadline`
#: freeze guard; on expiry the op is ABANDONED with ONE WARN and the guard
#: proceeds (enter: no cross-host detection this pull; exit: the leftover lease
#: self-heals by going stale within :data:`PULL_LEASE_STALE_AFTER_S`) — the
#: flocks are ALWAYS released promptly. 5 s: generous for a ~300-byte sidecar
#: read/write, far under the lease TTL.
PULL_LEASE_IO_DEADLINE_S: Final[float] = 5.0

#: Env override for :data:`PULL_LEASE_IO_DEADLINE_S` (fail-loud parse via the
#: COMMON :func:`~brutex.config.deadline.resolve_env_seconds` — an invalid /
#: non-positive / non-finite value falls back to the audited default, so a bad
#: value can never DISABLE the bound).
PULL_LEASE_IO_DEADLINE_ENV: Final[str] = "BRUTEX_PULL_LEASE_IO_DEADLINE_S"


class ForeignPullActiveError(PullAlreadyRunningError):
    """A FRESH pull lease from a DIFFERENT host was found on the shared lake storage.

    Raised by :func:`pull_lease_guard` on enter. Subclasses
    :class:`PullAlreadyRunningError` so every existing refusal path (CLI + dashboard)
    handles it without new plumbing. Honest wording: the foreign pull is *apparently*
    running — the lease is advisory detection over shared storage + wall clocks, not
    proof (see the module docstring's cross-host envelope).
    """

    def __init__(self, lease_path: Path, lease: PullLease, age_s: float) -> None:
        # Proper super() chain: the parent owns ``lock_path`` (one canonical
        # definition) and carries this subclass's lease-flavoured message
        # instead of composing its flock-flavoured default.
        super().__init__(
            lease_path,
            "a pull for this lake appears to be running on another host "
            f"({lease.host!r}, pid {lease.pid}; lease heartbeat {age_s:.0f}s old at "
            f"{lease_path}); refusing to start a concurrent pull, which would double "
            "the Groww account request rate. If that pull is genuinely dead, the lease "
            f"goes stale by itself within {PULL_LEASE_STALE_AFTER_S:.0f}s",
        )
        self.lease = lease


@dataclass(frozen=True, slots=True)
class PullLease:
    """One decoded pull-lease record (the on-disk JSON object, schema-pinned).

    ``wall_time`` is the heartbeat instant (``time.time()`` — wall clock, the only
    clock comparable across processes AND hosts; the rate-bridge precedent).
    ``token`` is a per-pull random identity so the holder removes ONLY its own lease
    at exit, never a racing peer's.
    """

    schema: int
    token: str
    host: str
    pid: int
    started_wall: float
    wall_time: float

    def to_json_bytes(self) -> bytes:
        """Serialize for the atomic lease write (compact, stable key order)."""
        return json.dumps(asdict(self), sort_keys=True).encode("utf-8")


def pull_lease_path(lake_root: Path) -> Path:
    """Return the stable pull-lease path under *lake_root* (never created here).

    ``<lake_root>/.locks/pull_lease.json`` — the same dot-prefixed sidecar dir as the
    flock, so no lake scanner / dataset glob ever sees it. O(1) path computation.
    """
    return lake_root / _LOCK_SUBDIR / PULL_LEASE_FILE_NAME


def pull_lease_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``True`` unless :data:`PULL_LEASE_ENV` is an explicit off-value.

    The escape hatch (the exact ``BRUTEX_PREBUILD_INCREMENTAL`` idiom — ONE shared
    off-value set, :data:`brutex.data.derive_stamp.DISABLED_ENV_VALUES`): unset /
    empty / any other value ⇒ the lease guard is ON; an off-value ⇒ the guard is a
    no-op (e.g. a network volume with pathological cross-host clock skew).
    """
    from brutex.data.derive_stamp import DISABLED_ENV_VALUES

    source = env if env is not None else os.environ
    return source.get(PULL_LEASE_ENV, "").strip().lower() not in DISABLED_ENV_VALUES


def read_pull_lease(lake_root: Path) -> PullLease | None:
    """Best-effort read the lake's pull lease, or ``None`` (never raises).

    ``None`` for EVERY non-happy case — absent, unreadable, corrupt JSON, not an
    object, unrecognised schema, missing / wrong-typed field — mirroring
    :func:`brutex.data.rate_bridge.read_rate_snapshot`: a damaged lease can never
    break (or falsely block) a pull start; it reads as the honest unknown.
    """
    return _read_lease_file(pull_lease_path(lake_root))


def _read_lease_file(path: Path) -> PullLease | None:
    """The fail-soft lease-file decode core (see :func:`read_pull_lease`)."""
    try:
        raw = path.read_bytes()
    except OSError:
        # Absent (FileNotFoundError) or unreadable (permission/IO) — no lease signal.
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, RecursionError):
        # A partial / torn / non-UTF-8 write — or a degenerately deep-nested
        # payload blowing the parser's recursion limit (RecursionError is a
        # RuntimeError subclass json.loads CAN raise on hostile/corrupt input;
        # the hostile-review round-4 MINOR: it previously escaped this decode
        # and was mislabelled a wedged-volume event upstream). Honest unknown —
        # a damaged lease can never break (or falsely block) a pull start.
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != PULL_LEASE_SCHEMA:
        # Unrecognised version — forward/backward-safe: never mis-decode another shape.
        return None
    try:
        return PullLease(
            schema=PULL_LEASE_SCHEMA,
            token=str(data["token"]),
            host=str(data["host"]),
            pid=int(data["pid"]),
            started_wall=float(data["started_wall"]),
            wall_time=float(data["wall_time"]),
        )
    except (KeyError, TypeError, ValueError):
        # A missing or wrong-typed field (hand-edited / truncated / alien file).
        return None


def foreign_pull_lease(
    lake_root: Path,
    *,
    host: str,
    now_wall: float,
    stale_after_s: float = PULL_LEASE_STALE_AFTER_S,
) -> tuple[PullLease, float] | None:
    """Return ``(lease, age_s)`` iff a FRESH lease from a DIFFERENT host is present.

    The pure cross-host verdict: ``None`` when no lease decodes (honest unknown —
    never block on garbage), when the lease is OUR OWN host's (the flocks — held by
    the caller — are the on-host authority: any same-host lease is a crashed or
    finished pull's residue), or when the heartbeat is STALE (older than
    *stale_after_s*). A FUTURE-dated heartbeat (negative age — a skewed-ahead writer
    clock) reads FRESH, the conservative refusal (documented envelope). O(1).
    """
    lease = read_pull_lease(lake_root)
    if lease is None:
        return None
    if lease.host == host:
        return None
    age_s = now_wall - lease.wall_time
    if age_s > stale_after_s:
        return None
    return lease, age_s


class _PullLeaseKeeper:
    """Holds one pull's lease identity and performs the best-effort disk writes.

    Every write failure WARNs (once per keeper for the refresh path — a stalled
    volume would otherwise WARN every heartbeat tick) and NEVER raises into the
    pull: losing the lease only degrades cross-host detection to today's behaviour.
    """

    def __init__(self, lease_path: Path, *, host: str, now_fn: Callable[[], float]) -> None:
        self._path = lease_path
        self._host = host
        self._now_fn = now_fn
        self._token = uuid.uuid4().hex
        self._started_wall = now_fn()
        self._write_warned = False

    @property
    def token(self) -> str:
        """The per-pull lease identity (random, non-secret)."""
        return self._token

    def refresh(self) -> None:
        """Atomically (re)write the lease with a fresh heartbeat — best-effort."""
        lease = PullLease(
            schema=PULL_LEASE_SCHEMA,
            token=self._token,
            host=self._host,
            pid=os.getpid(),
            started_wall=self._started_wall,
            wall_time=self._now_fn(),
        )
        payload = lease.to_json_bytes()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # 0o600 — a private coordination sidecar (non-secret, but least-privilege).
            atomic_write_simple(self._path, lambda tmp: tmp.write_bytes(payload), chmod=0o600)
        except OSError as exc:
            if not self._write_warned:
                self._write_warned = True
                _LOG.warning(
                    "pull_lease.write_failed",
                    lease_path=str(self._path),
                    error=str(exc),
                    error_type=type(exc).__name__,
                    detail="cross-host pull detection degraded; the pull is unaffected",
                )

    def clear(self) -> None:
        """Remove the lease at exit IFF it is still OURS — best-effort.

        Reads the on-disk lease back and unlinks only on a ``token`` match, so a
        racing peer's lease (which overwrote ours inside the documented no-mutex
        envelope) is never deleted from under it. Any read/unlink failure WARNs and
        is swallowed — a leftover lease self-heals by going stale within the TTL.
        """
        current = _read_lease_file(self._path)
        if current is None or current.token != self._token:
            return
        try:
            self._path.unlink()
        except OSError as exc:
            _LOG.warning(
                "pull_lease.clear_failed",
                lease_path=str(self._path),
                error=str(exc),
                error_type=type(exc).__name__,
                detail="leftover lease self-heals by going stale within the TTL",
            )


def _lease_heartbeat_loop(
    stop: threading.Event, interval_s: float, refresh: Callable[[], None]
) -> None:
    """Refresh the lease every *interval_s* until *stop* is set (daemon-thread body)."""
    while not stop.wait(interval_s):
        refresh()


def _bounded_lease_io[T](
    fn: Callable[[], T],
    *,
    fallback: T,
    deadline_s: float,
    action: str,
    lease_path: Path,
) -> T:
    """Run one synchronous lease I/O op under the COMMON freeze guard — never blocks.

    The round-1 review MAJOR fix (the ``rate_bridge_cache`` precedent,
    MIGRATION.md #1522): the lease guard's enter read / first refresh / exit
    ``clear`` all touch the lake volume WHILE the two single-flight flocks are
    held, and a wedged (non-raising) NFS/SSD volume would otherwise hang them
    forever — permanently refusing every future pull because the flocks release
    only after this I/O. Bounds *fn* by *deadline_s* via
    :func:`~brutex.config.deadline.run_with_deadline`; on expiry
    (:class:`~brutex.config.deadline.DeadlineExceeded`) OR a guard-thread spawn
    failure under thread exhaustion
    (:class:`~brutex.config.deadline.DeadlineSpawnError` — the typed
    ``Thread.start`` signal, the ``pull_lease.heartbeat_spawn_failed``
    rationale) logs ONE ``pull_lease.io_wedged`` WARN and returns *fallback* —
    the lease machinery degrades (detection lost / a leftover lease that
    self-heals by going stale within the TTL), the pull and the lock releases
    are UNAFFECTED. HONEST (#999 stance): the abandoned worker is a daemon that
    cannot be killed — at most one zombie per wedged op, dying with the
    process. EXACTLY the guard's own two failure modes are caught — the
    hostile-review round-4 MINOR fix: the prior ``except (DeadlineExceeded,
    RuntimeError)`` also swallowed a ``RuntimeError`` raised by *fn* ITSELF
    (``run_with_deadline`` re-raises fn's errors on the caller thread),
    mislabelling a genuine code bug ``pull_lease.io_wedged``. The wrapped ops
    never raise themselves (``foreign_pull_lease`` / ``keeper.refresh`` /
    ``keeper.clear`` are all fail-soft), so any fn-raised error — including a
    ``RuntimeError`` — now propagates as the genuine code bug it is.
    """
    try:
        return run_with_deadline(
            fn,
            deadline_s,
            what=f"pull-lease {action}",
            thread_name=f"brutex-pull-lease-{action}",
        )
    except (DeadlineExceeded, DeadlineSpawnError) as exc:
        _LOG.warning(
            "pull_lease.io_wedged",
            action=action,
            lease_path=str(lease_path),
            deadline_s=deadline_s,
            error=str(exc),
            error_type=type(exc).__name__,
            detail=(
                "lease I/O abandoned (wedged volume) or never spawned (thread "
                "exhaustion); cross-host detection degraded — a leftover lease "
                "self-heals by going stale within the TTL, and the "
                "single-flight locks release normally"
            ),
        )
        return fallback


@contextmanager
def pull_lease_guard(
    lake_root: Path,
    *,
    host: str | None = None,
    now_fn: Callable[[], float] = time.time,
    stale_after_s: float = PULL_LEASE_STALE_AFTER_S,
    heartbeat_interval_s: float = PULL_LEASE_HEARTBEAT_S,
    env: Mapping[str, str] | None = None,
) -> Generator[None, None, None]:
    """Hold the advisory cross-host pull LEASE for the ``with`` block.

    DEC-ACCOUNT-LEASE-CROSSHOST-001 — the cross-host arm of the pull single-flight
    (see the module docstring for the full honest envelope: DETECTION over shared
    storage + wall clocks, deliberately NOT mutual exclusion). Enter ORDER law: this
    guard is taken strictly AFTER both flocks at every production site — holding the
    flocks is what makes ignoring a SAME-host lease sound.

    On enter: refuses (raises :class:`ForeignPullActiveError`) when a FRESH lease
    from a DIFFERENT host is present; otherwise writes our own lease (best-effort)
    and spawns a daemon heartbeat refreshing it every *heartbeat_interval_s*. On
    exit: stops the heartbeat (bounded join — a wedged refresh write abandons the
    daemon, the #999 stance) and removes the lease iff still ours.

    EVERY synchronous lake-file op here (the enter read + first refresh, the
    exit ``clear``) is DEADLINE-BOUNDED under the COMMON
    :func:`~brutex.config.deadline.run_with_deadline` freeze guard
    (:data:`PULL_LEASE_IO_DEADLINE_S`, env
    :data:`PULL_LEASE_IO_DEADLINE_ENV` — the round-1 review MAJOR fix): these
    ops run while the caller already holds BOTH single-flight flocks and the
    exit ``clear`` runs BEFORE the flock releases at both production sites, so
    a WEDGED (non-raising) lake volume would otherwise hold the flocks forever
    and refuse every future pull until process restart. A wedged op is
    abandoned with ONE ``pull_lease.io_wedged`` WARN (enter: no cross-host
    detection this pull; exit: the leftover lease goes stale within the TTL) —
    the guard always enters/exits promptly and the pull is unaffected.

    Parameters are injection seams for tests (*host* defaults to
    :func:`socket.gethostname`, *now_fn* to :func:`time.time`); *env* feeds the
    :data:`PULL_LEASE_ENV` kill switch (an off-value ⇒ this guard is a pure no-op).

    Raises
    ------
    ForeignPullActiveError
        A fresh foreign-host lease is present (a pull appears to be running on
        another host against this shared lake).
    """
    if not pull_lease_enabled(env):
        yield
        return
    resolved_host = socket.gethostname() if host is None else host
    lease_path = pull_lease_path(lake_root)
    io_deadline_s = resolve_env_seconds(PULL_LEASE_IO_DEADLINE_ENV, PULL_LEASE_IO_DEADLINE_S)
    # Every SYNCHRONOUS lease I/O below runs while the caller already holds
    # BOTH single-flight flocks, so it is deadline-bounded (the round-1 review
    # MAJOR fix): a wedged read/write degrades detection with one WARN — it
    # can never wedge the guard and leave the flocks held forever.
    verdict = _bounded_lease_io(
        lambda: foreign_pull_lease(
            lake_root, host=resolved_host, now_wall=now_fn(), stale_after_s=stale_after_s
        ),
        fallback=None,
        deadline_s=io_deadline_s,
        action="read",
        lease_path=lease_path,
    )
    if verdict is not None:
        lease, age_s = verdict
        raise ForeignPullActiveError(lease_path, lease, age_s)
    keeper = _PullLeaseKeeper(lease_path, host=resolved_host, now_fn=now_fn)
    _bounded_lease_io(
        keeper.refresh,
        fallback=None,
        deadline_s=io_deadline_s,
        action="refresh",
        lease_path=lease_path,
    )
    stop = threading.Event()
    spawned = threading.Thread(
        target=_lease_heartbeat_loop,
        args=(stop, heartbeat_interval_s, keeper.refresh),
        daemon=True,
        name="brutex-pull-lease",
    )
    thread: threading.Thread | None = spawned
    try:
        spawned.start()
    except RuntimeError as exc:
        # Thread exhaustion: degrade — the lease goes stale mid-pull (cross-host
        # detection lost for THIS pull), the pull itself is unaffected.
        thread = None
        _LOG.warning(
            "pull_lease.heartbeat_spawn_failed",
            lease_path=str(lease_path),
            error=str(exc),
            detail="lease heartbeat not running; the lease will go stale mid-pull",
        )
    try:
        yield
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=_LEASE_JOIN_TIMEOUT_S)
        # Bounded (the round-1 review MAJOR fix): this finally runs BEFORE the
        # two flock releases at BOTH production sites (pull_jobs LIFO + the CLI
        # ExitStack), so an unbounded clear() on a wedged volume would hold the
        # flocks forever and refuse every future pull until process restart. A
        # wedged clear is abandoned with one WARN; the leftover lease self-heals
        # by going stale within the TTL.
        _bounded_lease_io(
            keeper.clear,
            fallback=None,
            deadline_s=io_deadline_s,
            action="clear",
            lease_path=lease_path,
        )
