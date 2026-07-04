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
one account, or two HOSTS, are still NOT covered (a system-wide ``/tmp``-style dir has
its own permission trade-offs; a cross-host distributed lock is out of scope for an
``flock``) — both stay NAMED follow-ups, as does keying on the vendor feed-token instead
of the ``api_key``. A credentials-less run (dry-run / no AWS) SKIPS the account lock
gracefully — it makes no vendor calls, so there is no account rate to protect. (See
``docs/runbooks/data-fetch-runday.md`` §6a for the operator note.)
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from brutex.data.atomic_io import lake_write_guard
from brutex.models import BruteXError

__all__ = [
    "ACCOUNT_LOCK_FILE_PREFIX",
    "PULL_SINGLEFLIGHT_LOCK_NAME",
    "PullAlreadyRunningError",
    "account_pull_singleflight_lock",
    "account_singleflight_lock_path",
    "default_account_lock_dir",
    "pull_singleflight_lock",
    "pull_singleflight_lock_path",
]

#: Sidecar lock directory under the lake root. A dot-prefixed sibling of ``bars/`` +
#: ``options/`` so it is never matched by a dataset glob or a lake scanner (mirrors
#: :data:`brutex.data.partition_lock._LOCK_SUBDIR`).
_LOCK_SUBDIR: Final[str] = ".locks"

#: The single, stable filename for the account-wide pull single-flight lock. ONE name
#: (not per-partition) so EVERY pull against this lake root contends on the same file.
PULL_SINGLEFLIGHT_LOCK_NAME: Final[str] = "pull_singleflight.lock"

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


class PullAlreadyRunningError(BruteXError):
    """Another pull already holds the account-wide single-flight lock for this lake root.

    Raised by :func:`pull_singleflight_lock` when the non-blocking ``flock`` acquire
    finds the lock already held — a peer process (a CLI ``brutex fetch`` or a second
    dashboard pull) is mid-pull against the same Groww account / lake. The caller must
    REFUSE to start a second pull rather than double the account's request rate
    (CLAUDE.md Golden Rule #6). ``lock_path`` is the contended lock file (non-secret)
    for the operator-facing message + the log.
    """

    def __init__(self, lock_path: Path) -> None:
        super().__init__(
            "a pull is already running for this lake "
            f"(account-wide single-flight lock held at {lock_path}); "
            "refusing to start a second concurrent pull, which would double the "
            "Groww account request rate"
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
def pull_singleflight_lock(lake_root: Path) -> Generator[None, None, None]:
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
    with _flock_singleflight(pull_singleflight_lock_path(lake_root)):
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
def _flock_singleflight(lock_path: Path) -> Generator[None, None, None]:
    """The ONE non-blocking exclusive-flock core both single-flight locks share (COMMON).

    Creates the lock dir/file (guarded — ENOSPC/OOM → ``LakeWriteHalt``), takes
    ``LOCK_EX | LOCK_NB`` (contention → :class:`PullAlreadyRunningError`), and releases
    by closing the fd on every exit.
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
            raise PullAlreadyRunningError(lock_path) from exc
        yield
    finally:
        # Closing the fd releases the advisory lock — an ``flock`` is tied to the open
        # file description, so the close both unlocks and frees the fd in one step, on
        # every (success or error) exit. On the contended path the lock was never taken
        # by THIS fd, so closing it does not touch the peer's lock.
        os.close(fd)
