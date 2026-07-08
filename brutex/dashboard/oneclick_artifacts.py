"""One-click sweep artifact adapter — the ONE integration seam (DEC-OPERATOR-PAGE-REAL-001).

INTEGRATION ADAPTER (sweep-parallel-integration-contracts) — reads the P2/P3
artifact CONTRACT shapes from disk; fixture-backed until P2/P3 merge; the real
trades binding (ResultsStore paged API) lands at merge-integration. Do not
drift the shapes without updating the contracts memory.

Contract surfaces consumed here (read-only, never written):

* ``<results_root>/runs/<run_id>/manifest.json`` — the P2 run manifest
  ``{run_id, selection, estimate, actual, per_stage_timings, per_door_kills,
  dedup_skips, tripwires[], suspect}``.
* ``<results_root>/runs/<run_id>/top100.json`` — the P3 ordered leaderboard
  rows ``{rank, mask_hex, conditions[], tf, direction, arm, metrics{},
  regime{}, vix_band{}, family_id}``.
* ``<results_root>/runs/<run_id>/events.jsonl`` — the P2 flight-recorder JSONL
  (record shape ``{ts_utc, run_id, kind, payload}`` + hash-chaining). The
  PATH is an ASSUMPTION registered with the contracts memory (§0.4) and lives
  in ONE constant (:data:`EVENTS_FILENAME`); the chain ALGORITHM is likewise
  ASSUMED behind the :func:`chain_hash` seam until P2 merges.
* ``<results_root>/runs/<run_id>/trades/rank-%04d.json`` — the INTERIM
  fixture-only trades layout (§0.3); production trades stay ``awaiting`` until
  the ResultsStore paged API lands (merge-integration rebind).

Honesty laws (Golden Rule #1/#10): a missing artifact is ``awaiting``, a
broken one is ``corrupt``/``partial`` with the defect NAMED — never a 500,
never a fabricated count/row/total. ``MemoryError`` re-raises raw (house
law); every other read defect degrades to a named status.

Latency envelope: :func:`tail_events` is O(page) via a byte-offset seek with a
1 MiB read-window bound; :func:`verify_events_chain` is O(file) — button-click
only, never a poll; the loaders are O(artifact) per parse and the routes memo
them per stat-token revision (the first read per revision pays the parse).
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Final, Literal, NoReturn, Protocol

import structlog

_LOG = structlog.get_logger(__name__)

__all__ = [
    "AWAITING_DETAIL",
    "EVENTS_FILENAME",
    "FIXTURE_ROOT_ENV",
    "MANIFEST_FILENAME",
    "MANIFEST_REQUIRED_KEYS",
    "PREFLIGHT_FILENAME",
    "PREFLIGHT_REQUIRED_KEYS",
    "RUNS_DIRNAME",
    "TOP100_FILENAME",
    "TOP100_ROW_REQUIRED_KEYS",
    "TRADES_DIRNAME",
    "ArtifactStatus",
    "AwaitingTradesReader",
    "ChainNotVerifiedError",
    "EventsPage",
    "FixtureTradesReader",
    "ManifestResult",
    "OneclickConfigError",
    "RunInfo",
    "Top100Result",
    "TradesReader",
    "TradesResult",
    "artifact_revision",
    "canonical_record_bytes",
    "chain_hash",
    "discover_runs",
    "is_safe_run_id",
    "load_manifest",
    "load_preflight_fixture",
    "load_top100",
    "probe_run_dir",
    "raise_chain_not_verified",
    "resolve_fixture_root",
    "runs_revision",
    "runs_root",
    "tail_events",
    "trades_path",
    "verify_events_chain",
]

#: One artifact's read verdict. ``awaiting`` = not written yet (loud, honest);
#: ``corrupt`` = present but unreadable; ``partial`` = readable with a NAMED
#: defect (rows kept up to it / keys missing); ``ok`` = the full contract shape.
ArtifactStatus = Literal["ok", "awaiting", "corrupt", "partial"]

#: The runs directory under ``<results_root>`` (the contract's ``runs/`` tree).
RUNS_DIRNAME: Final[str] = "runs"

#: The P2 run-manifest artifact filename (contract-pinned).
MANIFEST_FILENAME: Final[str] = "manifest.json"

#: The P3 top-100 leaderboard artifact filename (contract-pinned).
TOP100_FILENAME: Final[str] = "top100.json"

#: The P2 flight-recorder JSONL filename. ASSUMED path (§0.4 of the plan —
#: registered with the contracts memory); ONE constant so a P2 rename at
#: merge-integration is a one-line rebind.
EVENTS_FILENAME: Final[str] = "events.jsonl"

#: The P1 preflight-verdict fixture filename (rendered when present; absent →
#: the honest "estimator not wired yet" awaiting state).
PREFLIGHT_FILENAME: Final[str] = "preflight.json"

#: The per-run trades sub-directory of the INTERIM fixture-only layout (§0.3).
TRADES_DIRNAME: Final[str] = "trades"

#: The fail-loud fixture-root env switch: unset → production behaviour
#: (:class:`AwaitingTradesReader` + the lake-derived results root); set to an
#: existing directory → that tree serves the artifacts (LOUD badge, fixture
#: reader active); set to junk → :class:`OneclickConfigError` (route → 400).
FIXTURE_ROOT_ENV: Final[str] = "BRUTEX_ONECLICK_FIXTURE_ROOT"

#: The one loud awaiting-state phrase every absent-artifact detail carries.
AWAITING_DETAIL: Final[str] = "awaiting real run artifact"

#: The contract's required manifest top-level keys (P2 → P4).
MANIFEST_REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "run_id",
    "selection",
    "estimate",
    "actual",
    "per_stage_timings",
    "per_door_kills",
    "dedup_skips",
    "tripwires",
    "suspect",
)

#: The contract's required per-row keys of ``top100.json`` (P3 → P4).
TOP100_ROW_REQUIRED_KEYS: Final[tuple[str, ...]] = (
    "rank",
    "mask_hex",
    "conditions",
    "tf",
    "direction",
    "arm",
    "metrics",
    "regime",
    "vix_band",
    "family_id",
)

#: The contract's required preflight-verdict keys (P1 → P4).
PREFLIGHT_REQUIRED_KEYS: Final[tuple[str, ...]] = ("ok", "refusals", "clamps", "estimator")

#: Hard byte bound on one :func:`tail_events` read window (O(page) law).
_EVENTS_BYTES_CAP: Final[int] = 1 << 20  # 1 MiB

#: Hard per-page line bound for :func:`tail_events`.
_MAX_EVENTS_LIMIT: Final[int] = 1000

#: Hard per-page row bound for the trades readers.
_MAX_TRADES_LIMIT: Final[int] = 1000

#: The genesis ``prev_hash`` of the ASSUMED events chain law (§0.1 proposal).
_CHAIN_GENESIS: Final[str] = "0" * 64

#: The permanent honesty suffix every chain verdict carries (T-M1).
_CHAIN_ASSUMED_NOTE: Final[str] = "(algorithm assumed until P2 merges)"

#: Safe run-id grammar: ONE path segment, no traversal, no separators.
_RUN_ID_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class OneclickConfigError(ValueError):
    """Raised when :data:`FIXTURE_ROOT_ENV` is set but unusable (fail-loud → route 400)."""


class ChainNotVerifiedError(Exception):
    """Peek sentinel: raised by :func:`raise_chain_not_verified` so a cache
    lookup WITHOUT ``check=chain`` never runs the O(file) verification (a
    raising ``compute`` stores nothing — the MoversSnapshotCache contract).
    """


def raise_chain_not_verified() -> NoReturn:
    """Raise :class:`ChainNotVerifiedError` — the cache-peek ``compute`` sentinel.

    Returns
    -------
    NoReturn
        Always raises; used as the ``compute=`` closure when the verify route
        only wants the LAST cached chain verdict, never a fresh O(file) scan.
    """
    raise ChainNotVerifiedError("chain not verified yet for this events revision")


# ── Result types (split per artifact — the isinstance narrowing happens ONCE here) ──


@dataclass(frozen=True, slots=True)
class ManifestResult:
    """One manifest (or preflight) read verdict.

    Attributes
    ----------
    status
        The :data:`ArtifactStatus` verdict.
    manifest
        The parsed JSON object — ``None`` unless *status* is ``ok``/``partial``.
    detail
        Human-readable defect/awaiting note (empty when ``ok``).
    revision
        The ``"mtime_ns:size"`` stat token of the file, ``None`` when absent
        or unstattable (⇒ compute fresh, cache nothing).
    """

    status: ArtifactStatus
    manifest: dict[str, Any] | None
    detail: str
    revision: str | None


@dataclass(frozen=True, slots=True)
class Top100Result:
    """One ``top100.json`` read verdict.

    Attributes
    ----------
    status
        The :data:`ArtifactStatus` verdict — a VALID empty or short list is
        ``ok`` (the artifact EXISTS; T-M2 status law).
    rows
        The validated rows (kept up to the first bad row on ``partial``).
    detail
        Defect note naming the bad row index / missing keys (empty when ok).
    revision
        The ``"mtime_ns:size"`` stat token, ``None`` when absent.
    """

    status: ArtifactStatus
    rows: tuple[dict[str, Any], ...]
    detail: str
    revision: str | None


@dataclass(frozen=True, slots=True)
class TradesResult:
    """One paged trades read verdict.

    Attributes
    ----------
    status
        The :data:`ArtifactStatus` verdict (trades CAN be ``partial`` — a bad
        row is tolerated with its index named).
    rows
        The requested page of contract trade rows.
    total
        The artifact's declared total row count, or ``None`` when unknown —
        NEVER a fabricated 0.
    source
        ``fixture`` (the interim file layout), ``results_store`` (the future
        merge-integration rebind), or ``none`` (nothing served).
    detail
        Defect/awaiting note.
    """

    status: ArtifactStatus
    rows: tuple[dict[str, Any], ...]
    total: int | None
    source: Literal["fixture", "results_store", "none"]
    detail: str


@dataclass(frozen=True, slots=True)
class RunInfo:
    """One discovered run directory's cheap probe facts.

    Attributes
    ----------
    run_id
        The directory name (one safe path segment).
    dir_mtime_ns
        The run directory's mtime (newest-first ordering key).
    has_manifest, has_top100, has_events
        Per-artifact file presence.
    suspect
        The manifest's ``suspect`` flag — ``None`` until the manifest is
        readable and carries a boolean (never a fabricated ``False``).
    """

    run_id: str
    dir_mtime_ns: int
    has_manifest: bool
    has_top100: bool
    has_events: bool
    suspect: bool | None


@dataclass(frozen=True, slots=True)
class EventsPage:
    """One O(page) events-tail read.

    Attributes
    ----------
    status
        ``ok`` / ``awaiting`` / ``corrupt`` / ``partial`` (an unparseable
        COMPLETE line inside the window ⇒ ``partial``, skipped + counted).
    records
        The parsed records ``{ts_utc, run_id, kind, payload[, hash]}`` that
        survived the ``kind``/``q`` window filters.
    next_offset
        Byte offset AFTER the last complete line consumed — the resume cursor.
    total_bytes
        File size at read time (the client's "caught up" signal).
    truncated_tail
        ``True`` when the last line in the window was half-written (a live
        writer mid-append) — honest, NOT an error; it is re-read next poll.
    detail
        Defect/awaiting note.
    """

    status: ArtifactStatus
    records: tuple[dict[str, Any], ...]
    next_offset: int
    total_bytes: int
    truncated_tail: bool
    detail: str


# ── Path + revision helpers ─────────────────────────────────────────────


def runs_root(results_root: Path) -> Path:
    """Return ``<results_root>/runs`` — the contract's per-run artifact tree.

    Parameters
    ----------
    results_root
        The sweep results root (``<lake>/sweeps`` in production, or the
        fixture root override).

    Returns
    -------
    Path
        The runs directory (may not exist yet — callers degrade to awaiting).
    """
    return results_root / RUNS_DIRNAME


def trades_path(results_root: Path, run_id: str, rank: int) -> Path:
    """Return the INTERIM fixture trades page path ``runs/<run_id>/trades/rank-%04d.json``.

    Parameters
    ----------
    results_root
        The fixture root (this layout exists ONLY under the fixture tree).
    run_id
        The run directory name.
    rank
        The 1-based leaderboard rank whose trade list is requested.

    Returns
    -------
    Path
        The interim-layout page file (§0.3 of the registered proposals).
    """
    return runs_root(results_root) / run_id / TRADES_DIRNAME / f"rank-{rank:04d}.json"


def is_safe_run_id(run_id: str) -> bool:
    """``True`` iff *run_id* is ONE safe path segment (no traversal, no separators).

    Parameters
    ----------
    run_id
        The raw query value.

    Returns
    -------
    bool
        ``True`` for ``[A-Za-z0-9][A-Za-z0-9._-]{0,127}`` (which structurally
        excludes ``.``/``..``/empty/slashes); ``False`` otherwise.
    """
    return bool(_RUN_ID_RE.fullmatch(run_id))


def artifact_revision(path: Path) -> str | None:
    """Return *path*'s ``"mtime_ns:size"`` stat token, or ``None`` when unavailable.

    Parameters
    ----------
    path
        The artifact file (or directory) to stat.

    Returns
    -------
    str or None
        The content token used to key the parse-once-per-revision memos;
        ``None`` (absent / stat error) means "no signal" — compute fresh,
        cache nothing (the ``lake_revision`` doctrine).
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{st.st_mtime_ns}:{st.st_size}"


def runs_revision(results_root: Path) -> str | None:
    """Return the stat token of the ``runs/`` directory itself.

    Honest envelope (Golden Rule #10): a directory mtime moves when a run dir
    is ADDED/REMOVED — an in-place rewrite of a file INSIDE an existing run
    dir does not move it, so the cached runs LISTING may lag such a rewrite
    until the next run lands (per-artifact routes key on the artifact's own
    file token and do not share this bound).

    Parameters
    ----------
    results_root
        The sweep results root.

    Returns
    -------
    str or None
        The ``"mtime_ns:size"`` token of ``<results_root>/runs``, or ``None``.
    """
    return artifact_revision(runs_root(results_root))


# ── Discovery ───────────────────────────────────────────────────────────


def _is_file(path: Path) -> bool:
    """``Path.is_file`` (already swallows OSError → ``False``) — one probe point."""
    return path.is_file()


def probe_run_dir(run_dir: Path) -> RunInfo | None:
    """Probe ONE run directory into a :class:`RunInfo` (or ``None`` when absent).

    Parameters
    ----------
    run_dir
        The ``runs/<run_id>`` directory.

    Returns
    -------
    RunInfo or None
        ``None`` when the path is missing / not a directory / unstattable.
        ``suspect`` is filled ONLY from a readable manifest carrying a
        boolean ``suspect`` — otherwise it stays ``None`` (never fabricated).
    """
    try:
        st = os.stat(run_dir)
    except OSError:
        return None
    if not run_dir.is_dir():
        return None
    return _probe_run_dir_known(run_dir, st.st_mtime_ns)


def _probe_run_dir_known(run_dir: Path, dir_mtime_ns: int) -> RunInfo:
    """Build the :class:`RunInfo` for a KNOWN-good run directory (always returns)."""
    has_manifest = _is_file(run_dir / MANIFEST_FILENAME)
    suspect: bool | None = None
    if has_manifest:
        loaded = load_manifest(run_dir)
        if loaded.manifest is not None:
            raw = loaded.manifest.get("suspect")
            if isinstance(raw, bool):
                suspect = raw
    return RunInfo(
        run_id=run_dir.name,
        dir_mtime_ns=dir_mtime_ns,
        has_manifest=has_manifest,
        has_top100=_is_file(run_dir / TOP100_FILENAME),
        has_events=_is_file(run_dir / EVENTS_FILENAME),
        suspect=suspect,
    )


def discover_runs(results_root: Path, *, limit: int = 50) -> tuple[RunInfo, ...]:
    """Discover run directories under ``<results_root>/runs``, newest first.

    O(run dirs) — one ``scandir`` plus per-dir stat/manifest probes; an
    unreadable entry is SKIPPED with one ``oneclick.run_skipped`` WARN, never
    a raise (the whole listing survives one poisoned dir).

    Parameters
    ----------
    results_root
        The sweep results root.
    limit
        Maximum runs returned (newest ``dir_mtime_ns`` first; ties break on
        ``run_id`` for determinism). Non-positive ⇒ empty.

    Returns
    -------
    tuple of RunInfo
        The discovered runs; empty when the tree is absent/unreadable.
    """
    root = runs_root(results_root)
    try:
        entries = list(os.scandir(root))
    except OSError:
        return ()
    infos: list[RunInfo] = []
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            mtime_ns = entry.stat().st_mtime_ns
        except OSError as exc:
            _LOG.warning(
                "oneclick.run_skipped",
                run=entry.name,
                error_type=type(exc).__name__,
            )
            continue
        infos.append(_probe_run_dir_known(root / entry.name, mtime_ns))
    infos.sort(key=lambda i: (i.dir_mtime_ns, i.run_id), reverse=True)
    return tuple(infos[: max(0, limit)])


# ── Artifact loaders ────────────────────────────────────────────────────


def _read_artifact_text(path: Path, *, artifact: str) -> tuple[str | None, str | None]:
    """Read one artifact file → ``(text, error_detail)``; exactly one is non-None.

    ``FileNotFoundError`` maps to ``(None, None)`` — the AWAITING signal;
    any other read/decode defect maps to a NAMED corrupt detail (logged).
    """
    try:
        return path.read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeDecodeError) as exc:
        _LOG.warning(
            "oneclick.artifact_corrupt",
            artifact=artifact,
            path=str(path),
            error_type=type(exc).__name__,
        )
        return None, f"{artifact} unreadable/corrupt: {type(exc).__name__}"


def load_manifest(run_dir: Path) -> ManifestResult:
    """Load ``manifest.json`` (missing → awaiting; bad JSON → corrupt; missing keys → partial).

    Parameters
    ----------
    run_dir
        The ``runs/<run_id>`` directory.

    Returns
    -------
    ManifestResult
        NEVER invents defaults for counts — a ``partial`` result names the
        missing contract keys and keeps the object as-read.
    """
    path = run_dir / MANIFEST_FILENAME
    text, error = _read_artifact_text(path, artifact=MANIFEST_FILENAME)
    if text is None:
        if error is None:
            return ManifestResult(
                "awaiting", None, f"{AWAITING_DETAIL} — {MANIFEST_FILENAME} absent", None
            )
        return ManifestResult("corrupt", None, error, artifact_revision(path))
    revision = artifact_revision(path)
    try:
        obj = json.loads(text)
    except ValueError as exc:
        _LOG.warning(
            "oneclick.artifact_corrupt",
            artifact=MANIFEST_FILENAME,
            path=str(path),
            error_type=type(exc).__name__,
        )
        return ManifestResult("corrupt", None, f"{MANIFEST_FILENAME} is not valid JSON", revision)
    if not isinstance(obj, dict):
        return ManifestResult(
            "corrupt", None, f"{MANIFEST_FILENAME} is not a JSON object", revision
        )
    missing = [key for key in MANIFEST_REQUIRED_KEYS if key not in obj]
    if missing:
        return ManifestResult(
            "partial",
            obj,
            f"{MANIFEST_FILENAME} missing contract keys: {', '.join(missing)}",
            revision,
        )
    return ManifestResult("ok", obj, "", revision)


def load_top100(run_dir: Path) -> Top100Result:
    """Load ``top100.json`` (list-of-dict validated; a bad ROW → partial, rows kept up to it).

    A VALID EMPTY or SHORT list is ``status="ok"`` — the artifact EXISTS
    (T-M2 status law); the row count is ALWAYS ``len(rows)``, never assumed
    100.

    Parameters
    ----------
    run_dir
        The ``runs/<run_id>`` directory.

    Returns
    -------
    Top100Result
        Rows are passed through UNTOUCHED (metrics keep full precision —
        money 2dp is client-side only, DEC-MONEY-2DP-001).
    """
    path = run_dir / TOP100_FILENAME
    text, error = _read_artifact_text(path, artifact=TOP100_FILENAME)
    if text is None:
        if error is None:
            return Top100Result(
                "awaiting", (), f"{AWAITING_DETAIL} — {TOP100_FILENAME} absent", None
            )
        return Top100Result("corrupt", (), error, artifact_revision(path))
    revision = artifact_revision(path)
    try:
        obj = json.loads(text)
    except ValueError as exc:
        _LOG.warning(
            "oneclick.artifact_corrupt",
            artifact=TOP100_FILENAME,
            path=str(path),
            error_type=type(exc).__name__,
        )
        return Top100Result("corrupt", (), f"{TOP100_FILENAME} is not valid JSON", revision)
    if not isinstance(obj, list):
        return Top100Result("corrupt", (), f"{TOP100_FILENAME} is not a JSON array", revision)
    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(obj):
        if not isinstance(row, dict):
            return Top100Result(
                "partial",
                tuple(rows),
                f"row {idx} is not an object — rows kept up to it",
                revision,
            )
        missing = [key for key in TOP100_ROW_REQUIRED_KEYS if key not in row]
        if missing:
            return Top100Result(
                "partial",
                tuple(rows),
                f"row {idx} missing contract keys: {', '.join(missing)} — rows kept up to it",
                revision,
            )
        rows.append(row)
    return Top100Result("ok", tuple(rows), "", revision)


def load_preflight_fixture(run_dir: Path) -> ManifestResult:
    """Load the P1 preflight-verdict fixture ``preflight.json`` when present.

    Renders the P1 CONTRACT verdict shape ``{ok, refusals[], clamps[],
    estimator{trials, est_seconds_provisional, est_bytes}}``; absent →
    awaiting ("estimator not wired yet — lands at merge-integration (P1)").

    Parameters
    ----------
    run_dir
        The ``runs/<run_id>`` directory.

    Returns
    -------
    ManifestResult
        Reuses the manifest result shape (a small JSON-object verdict).
    """
    path = run_dir / PREFLIGHT_FILENAME
    text, error = _read_artifact_text(path, artifact=PREFLIGHT_FILENAME)
    if text is None:
        if error is None:
            return ManifestResult(
                "awaiting",
                None,
                "estimator not wired yet — lands at merge-integration (P1)",
                None,
            )
        return ManifestResult("corrupt", None, error, artifact_revision(path))
    revision = artifact_revision(path)
    try:
        obj = json.loads(text)
    except ValueError:
        return ManifestResult("corrupt", None, f"{PREFLIGHT_FILENAME} is not valid JSON", revision)
    if not isinstance(obj, dict):
        return ManifestResult(
            "corrupt", None, f"{PREFLIGHT_FILENAME} is not a JSON object", revision
        )
    missing = [key for key in PREFLIGHT_REQUIRED_KEYS if key not in obj]
    if missing:
        return ManifestResult(
            "partial",
            obj,
            f"{PREFLIGHT_FILENAME} missing contract keys: {', '.join(missing)}",
            revision,
        )
    return ManifestResult("ok", obj, "", revision)


# ── Events tail (O(page) byte-offset reader) ────────────────────────────


def tail_events(
    run_dir: Path,
    *,
    offset: int = 0,
    limit: int = 200,
    kind: str | None = None,
    q: str | None = None,
) -> EventsPage:
    """Read ONE bounded page of the flight-recorder JSONL from a byte offset.

    O(page) law: ``seek(offset)`` then read forward at most *limit* complete
    lines bounded by a 1 MiB byte cap — server work is bounded by the read
    WINDOW, never the file size. ``kind``/``q`` filter the READ WINDOW only
    (honest: not a whole-file search). ``offset=-1`` (any negative) anchors
    the window at the file TAIL (last ≤1 MiB, aligned to the next newline).

    Parameters
    ----------
    run_dir
        The ``runs/<run_id>`` directory.
    offset
        Byte offset to resume from; negative ⇒ tail-anchored; past-EOF ⇒
        empty page with the cursor reset to EOF (named in ``detail``).
    limit
        Max records returned (clamped to ``[1, 1000]``).
    kind
        Exact-match filter on the record's ``kind`` within the window.
    q
        Substring filter on the record's serialized JSON within the window.

    Returns
    -------
    EventsPage
        A half-written LAST line sets ``truncated_tail`` (honest, not an
        error — ``next_offset`` stays at its start so it is re-read once
        complete); an unparseable COMPLETE line ⇒ ``partial`` (skipped +
        counted, first defect byte named).
    """
    path = run_dir / EVENTS_FILENAME
    try:
        fh = path.open("rb")
    except FileNotFoundError:
        return EventsPage(
            "awaiting", (), 0, 0, False, f"{AWAITING_DETAIL} — {EVENTS_FILENAME} absent"
        )
    except OSError as exc:
        return EventsPage(
            "corrupt", (), 0, 0, False, f"{EVENTS_FILENAME} unreadable: {type(exc).__name__}"
        )
    page_limit = max(1, min(limit, _MAX_EVENTS_LIMIT))
    detail = ""
    with fh:
        size = os.fstat(fh.fileno()).st_size
        if offset < 0:
            start = max(0, size - _EVENTS_BYTES_CAP)
        elif offset > size:
            start = size
            detail = "offset past EOF — cursor reset to end of file"
        else:
            start = offset
        fh.seek(start)
        pos = start
        if offset < 0 and start > 0:
            # Tail anchor landed mid-line: discard up to the next newline so
            # the window starts at a record boundary.
            skipped = fh.readline()
            pos += len(skipped)
        records: list[dict[str, Any]] = []
        truncated = False
        bad_lines = 0
        first_bad: int | None = None
        consumed = 0
        while len(records) < page_limit and consumed < _EVENTS_BYTES_CAP:
            line = fh.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                truncated = True
                break
            line_start = pos
            pos += len(line)
            consumed += len(line)
            stripped = line.strip()
            if not stripped:
                continue
            record: Any
            try:
                record = json.loads(stripped)
            except ValueError:
                record = None
            if not isinstance(record, dict):
                bad_lines += 1
                if first_bad is None:
                    first_bad = line_start
                continue
            if kind is not None and record.get("kind") != kind:
                continue
            if q is not None and q not in json.dumps(record, ensure_ascii=False):
                continue
            records.append(record)
    status: ArtifactStatus = "ok"
    if bad_lines:
        status = "partial"
        note = f"{bad_lines} unparseable line(s) skipped (first at byte {first_bad})"
        detail = f"{detail}; {note}" if detail else note
    return EventsPage(status, tuple(records), pos, size, truncated, detail)


# ── Hash-chain seam (ASSUMED algorithm — §0.1 proposal, T-M1) ───────────


def canonical_record_bytes(record: dict[str, Any]) -> bytes:
    """Canonical JSON bytes of *record* WITHOUT its ``hash`` field.

    The ONE encoding definition shared by the fixture GENERATOR and the
    VERIFIER (they import this same function, so the golden proves
    generator↔verifier SELF-consistency — NOT P2 compatibility).

    Parameters
    ----------
    record
        One parsed events record (its ``hash`` key, if present, is excluded).

    Returns
    -------
    bytes
        ``json.dumps(..., sort_keys=True, separators=(",", ":"))`` UTF-8.
    """
    body = {key: value for key, value in record.items() if key != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def chain_hash(prev_hex: str, record_bytes: bytes) -> str:
    """The ASSUMED chain law: ``sha256(prev_hash_hex_bytes + canonical_record_bytes)``.

    The contract pins "hash-chained" ONLY — this exact algorithm is a
    registered PROPOSAL (§0.1) isolated behind this one seam; re-bind at
    merge-integration if P2's writer differs.

    Parameters
    ----------
    prev_hex
        The previous record's hex hash (:data:`_CHAIN_GENESIS` for record 1).
    record_bytes
        The record's canonical bytes (:func:`canonical_record_bytes`).

    Returns
    -------
    str
        The 64-char lowercase hex SHA-256.
    """
    return sha256(prev_hex.encode("ascii") + record_bytes).hexdigest()


def verify_events_chain(
    run_dir: Path, *, max_records: int | None = None
) -> tuple[bool, int, int | None, str]:
    """Verify the events hash chain end-to-end — O(file), button-click ONLY.

    Never run this on a poll: it reads and hashes EVERY record (the honest
    envelope of R44 check #2). A half-written trailing line is tolerated
    (the live writer's mid-append), not a break.

    Parameters
    ----------
    run_dir
        The ``runs/<run_id>`` directory.
    max_records
        Optional bound — verification stops (verdict ``True`` so far) after
        this many records.

    Returns
    -------
    tuple
        ``(ok, records_checked, first_break_line, detail)``; *detail* ALWAYS
        carries "algorithm assumed until P2 merges" on ANY verdict (T-M1).
    """
    path = run_dir / EVENTS_FILENAME
    try:
        fh = path.open("rb")
    except FileNotFoundError:
        return (
            False,
            0,
            None,
            f"{EVENTS_FILENAME} absent — nothing to verify {_CHAIN_ASSUMED_NOTE}",
        )
    except OSError as exc:
        return (
            False,
            0,
            None,
            f"{EVENTS_FILENAME} unreadable: {type(exc).__name__} {_CHAIN_ASSUMED_NOTE}",
        )
    prev = _CHAIN_GENESIS
    checked = 0
    with fh:
        for line_no, raw in enumerate(fh, start=1):
            if not raw.endswith(b"\n"):
                break  # half-written tail tolerated — the writer is mid-append.
            stripped = raw.strip()
            if not stripped:
                continue
            record: Any
            try:
                record = json.loads(stripped)
            except ValueError:
                return (
                    False,
                    checked,
                    line_no,
                    f"unparseable record at line {line_no} {_CHAIN_ASSUMED_NOTE}",
                )
            if not isinstance(record, dict) or not isinstance(record.get("hash"), str):
                return (
                    False,
                    checked,
                    line_no,
                    f"record at line {line_no} carries no hash {_CHAIN_ASSUMED_NOTE}",
                )
            expected = chain_hash(prev, canonical_record_bytes(record))
            if expected != record["hash"]:
                return (
                    False,
                    checked,
                    line_no,
                    f"chain break at line {line_no} {_CHAIN_ASSUMED_NOTE}",
                )
            prev = record["hash"]
            checked += 1
            if max_records is not None and checked >= max_records:
                return (
                    True,
                    checked,
                    None,
                    f"chain verified: {checked} records (bounded at {max_records}) "
                    f"{_CHAIN_ASSUMED_NOTE}",
                )
    return (True, checked, None, f"chain verified: {checked} records {_CHAIN_ASSUMED_NOTE}")


# ── Fixture root resolution (fail-loud) ─────────────────────────────────

_FIXTURE_ROOT_LOG_LOCK: Final[threading.Lock] = threading.Lock()
# Once-per-process log latch — a dict cell (not a module global) so no
# ``global`` rebinding is needed anywhere (CodeQL py/unused-global-variable).
_FIXTURE_ROOT_LOG_STATE: Final[dict[str, bool]] = {"logged": False}


def _reset_fixture_root_logged() -> None:
    """Test seam: re-arm the once-per-process ``oneclick.fixture_root_active`` log."""
    with _FIXTURE_ROOT_LOG_LOCK:
        _FIXTURE_ROOT_LOG_STATE["logged"] = False


def resolve_fixture_root() -> Path | None:
    """Resolve :data:`FIXTURE_ROOT_ENV` — ``None`` (unset), a directory, or fail-loud.

    Returns
    -------
    Path or None
        ``None`` when the env var is unset/blank (production default); the
        fixture tree when it names an existing directory (logged ONCE per
        process as ``oneclick.fixture_root_active``).

    Raises
    ------
    OneclickConfigError
        When set but NOT an existing directory (the route maps it to a 400
        naming the env var) — a real lake path can never silently masquerade
        as fixture data.
    """
    raw = os.environ.get(FIXTURE_ROOT_ENV, "").strip()
    if not raw:
        return None
    root = Path(raw)
    if not root.is_dir():
        raise OneclickConfigError(
            f"{FIXTURE_ROOT_ENV}={raw!r} is not an existing directory — "
            "unset it or point it at a fixture tree"
        )
    with _FIXTURE_ROOT_LOG_LOCK:
        if not _FIXTURE_ROOT_LOG_STATE["logged"]:
            _LOG.info("oneclick.fixture_root_active", root=str(root))
            _FIXTURE_ROOT_LOG_STATE["logged"] = True
    return root


# ── Trades readers (typed seam — production default is HONEST awaiting) ─


class TradesReader(Protocol):
    """The typed trades seam ``build_app`` accepts (rebound at merge-integration)."""

    def page(self, run_id: str, rank: int, *, offset: int, limit: int) -> TradesResult:
        """Return one page of trade rows for (*run_id*, *rank*)."""


class AwaitingTradesReader:
    """PRODUCTION DEFAULT: always ``status='awaiting'``, ``source='none'``.

    Trades land at merge-integration via the ResultsStore paged API — until
    then production NEVER fabricates trade rows (Golden Rule #1).
    """

    def page(self, run_id: str, rank: int, *, offset: int, limit: int) -> TradesResult:
        """Return the honest awaiting page (parameters accepted, unused).

        Parameters
        ----------
        run_id, rank, offset, limit
            Accepted for :class:`TradesReader` conformance.

        Returns
        -------
        TradesResult
            ``status='awaiting'``, ``source='none'``, ``total=None``.
        """
        return TradesResult(
            status="awaiting",
            rows=(),
            total=None,
            source="none",
            detail="trades land at merge-integration (ResultsStore paged API)",
        )


@dataclass(frozen=True, slots=True)
class _ParsedTrades:
    """One fully-parsed trades artifact (the per-revision memo payload)."""

    status: ArtifactStatus
    rows: tuple[dict[str, Any], ...]
    total: int | None
    detail: str


class FixtureTradesReader:
    """FIXTURE-ONLY trades reader over the interim ``rank-%04d.json`` layout (§0.3).

    Activated ONLY when :func:`resolve_fixture_root` is non-``None``. Parses
    the file ONCE per stat-token revision into a single-entry memo keyed by
    ``(path, revision)``, so a repeat page click is an O(page) slice — the
    FIRST parse per revision is O(file), stated honestly.

    Attributes
    ----------
    parse_count
        Deterministic spy seam: incremented on every REAL parse (the
        no-reparse tests read it — no timing needed).
    """

    def __init__(self, fixture_root: Path) -> None:
        """Bind the reader to *fixture_root* (the resolved env directory).

        Parameters
        ----------
        fixture_root
            The tree holding ``runs/<run_id>/trades/rank-%04d.json`` pages.
        """
        self._root = fixture_root
        self._lock = threading.Lock()
        self._memo: tuple[str, str, _ParsedTrades] | None = None
        self.parse_count: int = 0

    def page(self, run_id: str, rank: int, *, offset: int, limit: int) -> TradesResult:
        """Return one page of fixture trade rows (memoised parse + O(page) slice).

        Parameters
        ----------
        run_id
            The run directory name (unsafe ids degrade to awaiting — the
            route 400s them first; this is the defensive belt).
        rank
            The 1-based leaderboard rank.
        offset, limit
            Page window (offset clamped ≥ 0, limit clamped to ``[1, 1000]``).

        Returns
        -------
        TradesResult
            ``source='fixture'`` whenever the artifact was consulted;
            missing page file → honest awaiting (``total=None``, never 0).
        """
        if not is_safe_run_id(run_id) or rank < 1:
            return TradesResult(
                "awaiting", (), None, "none", "invalid run_id/rank — nothing to read"
            )
        path = trades_path(self._root, run_id, rank)
        revision = artifact_revision(path)
        if revision is None:
            return TradesResult(
                "awaiting",
                (),
                None,
                "none",
                f"{AWAITING_DETAIL} — no trades fixture for rank {rank}",
            )
        parsed = self._parse_cached(path, revision)
        page_offset = max(0, offset)
        page_limit = max(1, min(limit, _MAX_TRADES_LIMIT))
        return TradesResult(
            status=parsed.status,
            rows=parsed.rows[page_offset : page_offset + page_limit],
            total=parsed.total,
            source="fixture",
            detail=parsed.detail,
        )

    def _parse_cached(self, path: Path, revision: str) -> _ParsedTrades:
        """Serve the single-entry ``(path, revision)`` memo, parsing on a miss."""
        key = str(path)
        with self._lock:
            memo = self._memo
        if memo is not None and memo[0] == key and memo[1] == revision:
            return memo[2]
        parsed = self._parse(path)
        with self._lock:
            self._memo = (key, revision, parsed)
        return parsed

    def _parse(self, path: Path) -> _ParsedTrades:
        """Parse the whole interim trades file — O(file), once per revision."""
        self.parse_count += 1
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _LOG.warning(
                "oneclick.artifact_corrupt",
                artifact="trades",
                path=str(path),
                error_type=type(exc).__name__,
            )
            return _ParsedTrades(
                "corrupt", (), None, f"trades fixture unreadable/corrupt: {type(exc).__name__}"
            )
        if not isinstance(obj, dict) or not isinstance(obj.get("rows"), list):
            return _ParsedTrades(
                "corrupt", (), None, 'trades fixture is not {"rows": [...], "total": N}'
            )
        raw_total = obj.get("total")
        total = (
            raw_total if isinstance(raw_total, int) and not isinstance(raw_total, bool) else None
        )
        rows: list[dict[str, Any]] = []
        for idx, row in enumerate(obj["rows"]):
            if not isinstance(row, dict):
                return _ParsedTrades(
                    "partial",
                    tuple(rows),
                    total,
                    f"trade row {idx} is not an object — rows kept up to it",
                )
            rows.append(row)
        return _ParsedTrades("ok", tuple(rows), total, "")
