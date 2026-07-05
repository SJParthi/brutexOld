"""Per-symbol derive **stamp** — skip an unchanged prebuild re-derive (DEC-PREBUILD-INCREMENTAL-001).

Round-4 forensics (operator's 2026-07-04 run): a RE-RUN's prebuild re-READ +
re-RESAMPLED every contract's full 1-minute base across 20 timeframes (~2 h on
the full lake) even when NOTHING changed — the keep-first/latest-wins writers
made the WRITES no-ops, but the read+resample cost was paid in full every run.
This module gives :func:`~brutex.data.timeframe_materialize.materialize_lake_timeframes`
a per-SYMBOL content token + sidecar stamp so a re-run's prebuild becomes
O(changed symbols): a symbol whose 1-minute source files are byte-for-byte
unchanged since its last derive (same file set, sizes and mtimes) is skipped
without reading a single bar.

## Correct under heal / latest-wins by construction

The token is a digest over the symbol's ``1minute/**/*.parquet`` file set
(``relpath · size · mtime_ns`` per file). Every path that can change the
1-minute source — a pull appending bars, the lake healer's quarantine-move +
rewrite (DEC-LAKE-HEAL-001), a re-pull after a demotion — rewrites those files
(atomic ``os.replace`` = new inode, new mtime), so the token MOVES and the next
prebuild re-derives. The healer's derived-recall additionally removes the stamp
outright (:func:`remove_derive_stamp` — belt-and-braces on top of the token).

## The daily-cutoff rule (no stale ``1day`` skip)

The derived ``1day`` excludes the run day's forming session (``before`` —
DEC-PIPELINE-AUTOPREBUILD-001). With UNCHANGED 1-minute data a LATER run's
larger cutoff can still finalise one more daily bar iff the previous cutoff had
actually excluded a session in the data. The stamp therefore records both the
cutoff used and the data's last IST session, and a skip is allowed only when
the cutoffs match OR the previous cutoff already lay strictly AFTER the data's
last session (every session was final — a later cutoff derives identically).
An expired contract's data always ends at expiry, far before any run day, so
the ~66k-contract options tree — the measured 2 h — skips on every re-run.

## Honest envelope (Golden Rule #10)

* The skip saves the READ + RESAMPLE (O(bars × TFs) per symbol); the token
  itself is one O(files) stat walk per symbol — cheap (a footer-less stat), not
  free, and NOT an O(1)-total claim.
* The stamp is an ACCELERATOR sidecar (``<lake>/.derive_stamps/``, outside
  ``bars/`` like ``.locks``): saving it is best-effort (an ``OSError`` is
  logged, never breaks the prebuild), loading tolerates a corrupt/missing file
  (→ full derive), and deleting the whole directory simply forces one full
  re-derive — never a correctness surface.
* An out-of-band write that preserves size AND mtime of a 1-minute file is not
  detected — the same envelope as every mtime-token in the repo
  (``lake_revision.py``); no production writer does that (atomic replace).
* Escape hatch: ``BRUTEX_PREBUILD_INCREMENTAL=0`` (also ``off``/``false``/
  ``no``) disables the skip — every run derives in full, byte-identical to the
  pre-DEC-PREBUILD-INCREMENTAL-001 behaviour.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.atomic_io import atomic_write_simple

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_LOG = structlog.get_logger(__name__)

__all__ = [
    "DERIVE_STAMP_DIR",
    "DeriveStamp",
    "derive_stamp_path",
    "incremental_prebuild_enabled",
    "load_derive_stamp",
    "remove_derive_stamp",
    "save_derive_stamp",
    "should_skip_derive",
    "source_1m_token",
]

#: The stamp sidecar directory under the lake root — OUTSIDE ``bars/`` +
#: ``options/`` and never ``*.parquet``, so no lake scanner ever sees it
#: (mirrors the ``.locks`` sidecar convention of DEC-LAKE-PARTITION-FLOCK-001).
DERIVE_STAMP_DIR: Final[str] = ".derive_stamps"

#: The environment toggle. Default ON; these lowercase values disable it.
_DISABLED_VALUES: Final[frozenset[str]] = frozenset({"0", "off", "false", "no"})
_ENV_VAR: Final[str] = "BRUTEX_PREBUILD_INCREMENTAL"


def incremental_prebuild_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``True`` unless ``BRUTEX_PREBUILD_INCREMENTAL`` is an explicit off-value.

    The escape hatch (mirrors ``BRUTEX_LAKE_HEAL``'s style): unset / empty /
    any other value ⇒ enabled (the accelerator is default-ON per the round-4
    decree); ``0`` / ``off`` / ``false`` / ``no`` (case-insensitive) ⇒ disabled,
    every prebuild run derives in full.
    """
    source = env if env is not None else os.environ
    return source.get(_ENV_VAR, "").strip().lower() not in _DISABLED_VALUES


@dataclass(frozen=True, slots=True)
class DeriveStamp:
    """One symbol's last-successful-derive record (the skip decision's inputs).

    Attributes
    ----------
    token
        The :func:`source_1m_token` digest of the 1-minute file set the derive
        READ — any later source change moves it, invalidating the stamp.
    tfs
        The SORTED requested timeframe labels that were materialised; a run
        requesting a different set never skips (it may need frames the stamp's
        run never wrote).
    include_daily
        Whether the stamped run also derived the ``1day`` partition.
    daily_before
        The forming-session cutoff (ISO date) the stamped run used; ``None``
        for a non-daily run.
    last_1m_ist_date
        The data's last 1-minute IST session (ISO date) at stamp time; ``None``
        when unknown (a non-daily run, or a zero-row frame).
    written_at
        ISO UTC instant the stamp was written (diagnostic only — never part of
        the skip rule).
    """

    token: str
    tfs: tuple[str, ...]
    include_daily: bool
    daily_before: str | None
    last_1m_ist_date: str | None
    written_at: str


def derive_stamp_path(lake_root: Path, exchange: str, segment: str, symbol: str) -> Path:
    """The symbol's stamp file: ``<lake>/.derive_stamps/<exch>/<seg>/<symbol>.json``.

    The symbol token is the SAME directory name the ``bars/`` tree uses, so it
    is filesystem-safe by construction (it already names a lake directory).
    """
    return lake_root / DERIVE_STAMP_DIR / exchange / segment / f"{symbol}.json"


def source_1m_token(lake_root: Path, exchange: str, segment: str, symbol: str) -> str | None:
    """Digest the symbol's ``1minute`` source file set, or ``None`` when absent.

    One O(files) stat walk: every ``1minute/**/*.parquet`` file's
    ``relpath · size · mtime_ns`` triple, sorted (deterministic on any
    filesystem walk order), sha256-hashed. Content-move guarantees: a pull
    append, a heal rewrite, a re-pull — each atomically REPLACES a month file
    (new size and/or mtime_ns) or adds one, so the token moves. ``None`` when
    the symbol has no stored 1-minute files (the caller falls through to the
    normal no-source error path). A stat race on a file deleted mid-walk is
    treated as "no token" (→ full derive, the conservative direction).
    """
    base = lake_root / "bars" / exchange / segment / symbol / "1minute"
    if not base.is_dir():
        return None
    entries: list[str] = []
    try:
        for file in sorted(base.rglob("*.parquet")):
            stat = file.stat()
            entries.append(f"{file.relative_to(base).as_posix()}|{stat.st_size}|{stat.st_mtime_ns}")
    except OSError:
        # A file vanished mid-walk (concurrent heal/quarantine): no stable
        # token — derive in full rather than risk a stale skip.
        return None
    if not entries:
        return None
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def load_derive_stamp(path: Path) -> DeriveStamp | None:
    """Read a stamp, tolerating absence/corruption (``None`` ⇒ full derive).

    A missing file, unreadable bytes, malformed JSON, or a JSON object of the
    wrong shape all return ``None`` — the stamp is an accelerator, so every
    failure degrades to the SAFE full re-derive, never a crash and never a
    skip on bad data (Golden Rule #1).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return DeriveStamp(
            token=str(raw["token"]),
            tfs=tuple(str(t) for t in raw["tfs"]),
            include_daily=bool(raw["include_daily"]),
            daily_before=None if raw["daily_before"] is None else str(raw["daily_before"]),
            last_1m_ist_date=(
                None if raw["last_1m_ist_date"] is None else str(raw["last_1m_ist_date"])
            ),
            written_at=str(raw["written_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_derive_stamp(path: Path, stamp: DeriveStamp) -> None:
    """Persist a stamp atomically; best-effort (an ``OSError`` is logged, never raised).

    Rides the COMMON guard-free :func:`~brutex.data.atomic_io.atomic_write_simple`
    core (tmp → fsync → ``os.replace``). A failed save costs one future full
    re-derive — an acceptable accelerator miss, never a prebuild failure (the
    lake data itself was already written through the guarded writers).
    """
    payload = asdict(stamp)
    payload["tfs"] = list(stamp.tfs)
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_simple(path, lambda tmp: tmp.write_bytes(body))
    except OSError as exc:
        _LOG.warning("derive_stamp.save_failed", path=str(path), error=str(exc))


def remove_derive_stamp(lake_root: Path, exchange: str, segment: str, symbol: str) -> None:
    """Invalidate a symbol's stamp outright (the lake-heal derived-recall hook).

    Belt-and-braces on top of the token (a heal rewrites the 1-minute files,
    which already moves the token): the healer's recall delete-and-rebuilds
    derived partitions, so any stamp for the symbol is stale by definition.
    Missing file ⇒ no-op; an ``OSError`` is logged, never raised (heal must
    not fail on a sidecar).
    """
    path = derive_stamp_path(lake_root, exchange, segment, symbol)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _LOG.warning("derive_stamp.remove_failed", path=str(path), error=str(exc))


def should_skip_derive(
    stamp: DeriveStamp | None,
    *,
    token: str,
    tfs: Iterable[str],
    include_daily: bool,
    daily_before: str | None,
) -> bool:
    """The pure skip rule: unchanged source + same request ⇒ the derive is a no-op.

    ``True`` iff ALL of:

    * a stamp exists and its ``token`` equals the CURRENT source token (the
      1-minute file set is byte-for-byte the one the stamped run read);
    * the requested timeframe set (order-insensitively) equals the stamped set;
    * ``include_daily`` matches; and
    * for a daily run, the forming-session cutoff cannot change the output:
      the cutoffs are equal, OR the stamped cutoff already lay strictly AFTER
      the data's last IST session (every session was final at stamp time, so
      any later cutoff derives the identical ``1day`` series from the
      unchanged 1-minute data).

    ISO-8601 date strings compare correctly as strings (lexicographic ==
    chronological), so the rule needs no date parsing.
    """
    if stamp is None or stamp.token != token:
        return False
    if tuple(sorted(set(tfs))) != tuple(sorted(set(stamp.tfs))):
        return False
    if include_daily != stamp.include_daily:
        return False
    if not include_daily:
        return True
    if stamp.daily_before == daily_before:
        return True
    return (
        stamp.daily_before is not None
        and stamp.last_1m_ist_date is not None
        and stamp.last_1m_ist_date < stamp.daily_before
    )
