"""Durable per-day publish journal + staging tree (idempotency, journal-side).

DEC-CROSSVERIFY-PRODUCER-001 §3.6 / DEC-CROSSVERIFY-S3-WRITE-POSTURE-001: the
S3 grant is PutObject-ONLY, so the publisher never reads remote state —
idempotency rests on deterministic bytes + THIS journal. Layout under the
lake root (not ``*.parquet`` ⇒ invisible to every lake scanner):

* ``<lake_root>/.crossverify/<YYYY-MM-DD>.json`` — the day journal (atomic
  writes via :func:`~brutex.data.atomic_io.atomic_write_via`, so a disk-full
  maps to ``LakeWriteHalt`` — the lake-root halt semantics);
* ``<lake_root>/.crossverify/staging/<YYYY-MM-DD>/<segment>/<symbol>.csv`` —
  the frozen staged bytes, mirroring the S3 key layout.

A corrupt journal loads as ``None`` + one WARN — the next run re-renders and
re-stages everything (deterministic bytes ⇒ convergent). O(entries) per
save/load; the pending scan is O(lookback_days) file probes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.atomic_io import atomic_write_via

if TYPE_CHECKING:  # pragma: no cover — typing-only imports
    from pathlib import Path

    from brutex.crossverify.canonical import CanonicalIdentity

_LOG = structlog.get_logger("brutex.crossverify.journal")

#: The sidecar directory under the lake root.
CROSSVERIFY_DIRNAME: Final[str] = ".crossverify"

#: Journal schema version (forward-compat: an unknown version loads as corrupt).
JOURNAL_VERSION: Final[int] = 1

#: Sanity cap on the journal file size :func:`load_journal` will read —
#: the ``SweepCheckpointStore`` / stats-manifest precedent (round-3 hostile
#: review X2): a real journal is ~150 KiB at the full ~760-target universe
#: (one ~160-byte entry per key), so 16 MiB leaves ~100× headroom while a
#: planted multi-GiB file is REFUSED before the read (never an unbounded
#: ``read_bytes`` → ``MemoryError``, never a parse of attacker-scale bytes).
MAX_JOURNAL_BYTES: Final[int] = 1 << 24

#: A staged file mirrors the S3 key's last two components (segment/file).
_KEY_TAIL_COMPONENTS: Final[int] = 2

#: The segment-token component of a staged key must be a plain uppercase
#: token (``IDX_I`` / ``NSE_EQ`` / …) — positive charset, so a tampered
#: journal key can never smuggle ``..`` / control chars into a path join.
_SEGMENT_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Z][A-Z0-9_]*")


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One staged S3 object's durable record."""

    key: str
    sha256: str
    size: int
    published: bool


@dataclass
class DayJournal:
    """The mutable in-run journal of one day's staged/published objects."""

    day: date
    run_id: str
    entries: dict[str, JournalEntry] = field(default_factory=dict)
    withheld: dict[str, str] = field(default_factory=dict)  # symbol -> reason (honesty record)

    def mark_staged(self, key: str, sha256: str, size: int) -> None:
        """Record freshly staged bytes for *key*.

        Idempotency law: a key already journaled ``published=True`` with the
        SAME sha256 keeps its published flag (0 puts on a clean re-run); a
        DIFFERENT sha256 re-arms publishing (a changed lake ⇒ re-render
        differs ⇒ overwrite same key is the CORRECT action).
        """
        prior = self.entries.get(key)
        published = prior is not None and prior.published and prior.sha256 == sha256
        self.entries[key] = JournalEntry(key=key, sha256=sha256, size=size, published=published)

    def mark_published(self, key: str) -> None:
        """Flip *key*'s entry to ``published=True`` (the entry must exist)."""
        entry = self.entries[key]
        self.entries[key] = JournalEntry(
            key=entry.key, sha256=entry.sha256, size=entry.size, published=True
        )

    def pending_keys(self) -> tuple[str, ...]:
        """The staged-but-unpublished keys, deterministic (sorted) order."""
        return tuple(sorted(key for key, entry in self.entries.items() if not entry.published))

    def save(self, lake_root: Path) -> None:
        """Atomically persist the journal (lake-root ⇒ ``LakeWriteHalt`` semantics)."""
        payload = {
            "version": JOURNAL_VERSION,
            "day": self.day.isoformat(),
            "run_id": self.run_id,
            "entries": {
                key: {"sha256": e.sha256, "size": e.size, "published": e.published}
                for key, e in sorted(self.entries.items())
            },
            "withheld": dict(sorted(self.withheld.items())),
        }
        dest = journal_path(lake_root, self.day)
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")
        atomic_write_via(dest, lambda tmp: tmp.write_bytes(body))


def journal_path(lake_root: Path, day: date) -> Path:
    """``<lake_root>/.crossverify/<YYYY-MM-DD>.json``."""
    return lake_root / CROSSVERIFY_DIRNAME / f"{day.isoformat()}.json"


def last_run_path(lake_root: Path) -> Path:
    """``<lake_root>/.crossverify/last_run.json`` — the durable last-run record.

    DEC-CROSSVERIFY-LASTRUN-SURFACE (folded into
    DEC-CROSSVERIFY-WITHHELD-REGATE-001): the runner persists a small
    end-of-run summary here so the dashboard's progress route can surface
    the LAST materially-run outcome (verdict + counts + capped failed keys
    + detail) across dashboard restarts — a finished failure no longer
    vanishes on a page reload. Written at run end (never by dry-runs, never
    by the honest skip verdicts — a weekend skip must not clobber Friday's
    failure record); the per-day journal stays the authoritative durable
    done-record, this file is a VIEW convenience only (derive-on-read from
    the journal cannot carry verdict/exit_code/detail — hence the written
    record, the documented design choice).
    """
    return lake_root / CROSSVERIFY_DIRNAME / "last_run.json"


def write_last_run(lake_root: Path, record: dict[str, object]) -> None:
    """Atomically persist the last-run *record* (raises on I/O failure —
    the caller guards best-effort; a failed view write never kills a run)."""
    from brutex.data.atomic_io import atomic_write_simple

    dest = last_run_path(lake_root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(record, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")
    atomic_write_simple(dest, lambda tmp: tmp.write_bytes(body))


def load_last_run(lake_root: Path) -> dict[str, object] | None:
    """Load the last-run record; absent / corrupt / oversized → ``None`` (+ one
    WARN on corrupt) — the :func:`load_journal` corruption-tolerance contract
    (a view sidecar is never load-bearing; the journal is the truth)."""
    path = last_run_path(lake_root)
    try:
        if path.stat().st_size > MAX_JOURNAL_BYTES:
            _LOG.warning(
                "crossverify.last_run_corrupt",
                path=str(path),
                error_type="OversizedRecord",
                error=f"exceeds the {MAX_JOURNAL_BYTES}-byte sanity cap",
            )
            return None
        payload = json.loads(path.read_bytes().decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != JOURNAL_VERSION:
            raise ValueError(f"unsupported last-run shape/version at {path}")
    except FileNotFoundError:
        return None
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ) as exc:
        # The load_journal degrade set: bit rot / a directory at the path /
        # junk JSON / the two stdlib-json escapes (round-3 X2) all read as
        # an honest absent record, never a crashed route.
        _LOG.warning(
            "crossverify.last_run_corrupt",
            path=str(path),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    return {str(key): value for key, value in payload.items()}


def staging_dir(lake_root: Path, day: date) -> Path:
    """``<lake_root>/.crossverify/staging/<YYYY-MM-DD>/``."""
    return lake_root / CROSSVERIFY_DIRNAME / "staging" / day.isoformat()


def staging_path(lake_root: Path, day: date, identity: CanonicalIdentity) -> Path:
    """The staged file path — mirrors the S3 layout (``<segment>/<symbol>.csv``)."""
    return (
        staging_dir(lake_root, day) / identity.segment_token / (identity.canonical_symbol + ".csv")
    )


def staging_path_for_key(lake_root: Path, day: date, key: str) -> Path:
    """Resolve a journaled S3 *key* back to its staged file path, VALIDATED.

    The staging tree mirrors the key's LAST TWO components
    (``<segment_token>/<symbol>.csv``). Both tail components are validated
    BEFORE the join (round-1 hostile review — the journal is plain JSON on
    disk, so a tampered/corrupt key must never traverse outside the staging
    tree): the segment token must match :data:`_SEGMENT_TOKEN_PATTERN` and
    the filename must be ``<symbol>.csv`` with *symbol* passing the canonical
    positive allowlist
    (:func:`brutex.crossverify.canonical.symbol_key_safe` — which refuses
    ``.``/``..`` and every control char). A malformed key raises
    :class:`ValueError` — the publisher counts it failed + WARNs, never a
    crash and never a join.
    """
    parts = [part for part in key.split("/") if part]
    if len(parts) < _KEY_TAIL_COMPONENTS:
        raise ValueError(f"journal key {key!r} has fewer than 2 path components")
    segment, filename = parts[-_KEY_TAIL_COMPONENTS:]
    if _SEGMENT_TOKEN_PATTERN.fullmatch(segment) is None:
        raise ValueError(f"journal key {key!r}: unsafe segment token {segment!r}")
    # Lazy import — journal stays a light leaf; canonical never imports journal.
    from brutex.crossverify.canonical import symbol_key_safe

    suffix = ".csv"
    if not filename.endswith(suffix) or not symbol_key_safe(filename[: -len(suffix)]):
        raise ValueError(f"journal key {key!r}: unsafe symbol filename {filename!r}")
    return staging_dir(lake_root, day).joinpath(segment, filename)


def load_journal(lake_root: Path, day: date) -> DayJournal | None:
    """Load one day's journal; absent / corrupt → ``None`` (+ one WARN on corrupt).

    A corrupt journal is NOT an error state — the deterministic re-render
    converges, so the honest degrade is a fresh start for that day. The
    READ itself rides the same corruption-tolerant path (round-2 hostile
    review M1): invalid-UTF-8 bytes (bit rot / a torn write) and any
    ``OSError`` beyond plain absence (a directory planted at the journal
    path, EIO, …) load as ``None`` + one WARN — pre-fix an invalid-UTF-8
    journal raised ``UnicodeDecodeError`` uncaught, crashing the run at
    BOTH call sites and, via the recovery scan, wedging EVERY future run
    until the corrupt prior-day file aged out of the catchup window. The
    PARSE rides the same degrade for the two classes stdlib ``json`` lets
    escape a ValueError/TypeError guard (round-3 hostile review X2, the
    ``SweepCheckpointStore`` corruption-mapping precedent): ``json.loads``
    accepts the bareword ``Infinity`` so ``int(float("inf"))`` in a ``size``
    field raises ``OverflowError``, and a pathologically NESTED payload
    raises ``RecursionError`` — both previously escaped ``load_journal``
    raw AND wedged the recovery scan; an over-:data:`MAX_JOURNAL_BYTES`
    file is refused BEFORE the read (the same precedent's size cap).
    """
    path = journal_path(lake_root, day)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    except OSError as exc:
        _LOG.warning(
            "crossverify.journal_corrupt",
            path=str(path),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    if size > MAX_JOURNAL_BYTES:
        _LOG.warning(
            "crossverify.journal_corrupt",
            path=str(path),
            error_type="OversizedJournal",
            error=(
                f"{size} bytes exceeds the {MAX_JOURNAL_BYTES}-byte sanity cap "
                "for a small-JSON day journal — refusing to read it (corruption/tampering)"
            ),
        )
        return None
    try:
        raw = path.read_bytes().decode("utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        # A file raced away between the stat and the read folds in here too
        # (FileNotFoundError IS an OSError) — one WARN, honest degrade.
        _LOG.warning(
            "crossverify.journal_corrupt",
            path=str(path),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("version") != JOURNAL_VERSION:
            raise ValueError(f"unsupported journal shape/version at {path}")
        loaded_day = date.fromisoformat(payload["day"])
        run_id = payload["run_id"]
        if not isinstance(run_id, str):
            raise ValueError("run_id is not a string")
        entries: dict[str, JournalEntry] = {}
        for key, entry in payload["entries"].items():
            entries[key] = JournalEntry(
                key=key,
                sha256=str(entry["sha256"]),
                size=int(entry["size"]),
                published=bool(entry["published"]),
            )
        withheld = {str(k): str(v) for k, v in payload["withheld"].items()}
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        # OverflowError: `int(float("inf"))` — json accepts the bareword
        # Infinity in a numeric field (subclasses ArithmeticError, NOT
        # ValueError). RecursionError: a deeply-nested valid-JSON payload
        # blows the parser stack well under the size cap. Both are journal
        # corruption, same contract as malformed JSON (round-3 X2).
        _LOG.warning(
            "crossverify.journal_corrupt",
            path=str(path),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    return DayJournal(day=loaded_day, run_id=run_id, entries=entries, withheld=withheld)


def scan_pending_journals(lake_root: Path, *, today: date, lookback_days: int) -> list[DayJournal]:
    """PRIOR days' journals with pending (staged-but-unpublished) keys, oldest first.

    Scans exactly ``today − lookback_days … today − 1`` (today's own journal
    belongs to the current run). O(lookback_days) probes; corrupt journals are
    skipped via :func:`load_journal`'s WARN-and-``None`` degrade.
    """
    pending: list[DayJournal] = []
    for offset in range(lookback_days, 0, -1):
        journal = load_journal(lake_root, today - timedelta(days=offset))
        if journal is not None and journal.pending_keys():
            pending.append(journal)
    return pending


def scan_withheld_journals(lake_root: Path, *, today: date, lookback_days: int) -> list[DayJournal]:
    """PRIOR days' journals with WITHHELD symbols still on record, oldest first.

    The auto-re-gate scan (DEC-CROSSVERIFY-WITHHELD-REGATE-001) — the
    ``withheld`` twin of :func:`scan_pending_journals`: a journal whose
    ``withheld`` map is non-empty names symbols the gate refused on that
    day's run; a later run re-gates them from the (since-healed) lake. Same
    window law (``today − lookback_days … today − 1`` — today's own withheld
    map belongs to the current run's settle-retry belt), same O(lookback_days)
    probes, same corrupt-journal WARN-and-skip degrade.
    """
    withheld: list[DayJournal] = []
    for offset in range(lookback_days, 0, -1):
        journal = load_journal(lake_root, today - timedelta(days=offset))
        if journal is not None and journal.withheld:
            withheld.append(journal)
    return withheld


__all__ = [
    "CROSSVERIFY_DIRNAME",
    "JOURNAL_VERSION",
    "MAX_JOURNAL_BYTES",
    "DayJournal",
    "JournalEntry",
    "journal_path",
    "last_run_path",
    "load_journal",
    "load_last_run",
    "scan_pending_journals",
    "scan_withheld_journals",
    "staging_dir",
    "staging_path",
    "staging_path_for_key",
    "write_last_run",
]
