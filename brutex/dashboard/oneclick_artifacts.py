"""One-click sweep artifact adapter — the ONE integration seam (DEC-OPERATOR-PAGE-REAL-001).

INTEGRATION ADAPTER (sweep-parallel-integration-contracts) — reads the P2/P3
artifact CONTRACT shapes from disk; fixture-backed until P2/P3 merge; the real
trades binding (ResultsStore paged API) lands at merge-integration. Do not
drift the shapes without updating the contracts memory.

Contract surfaces consumed here (read-only, never written):

* ``<sweep_runs root>/<run_id>/manifest.json`` — the P2 run manifest
  ``{run_id, selection, estimate, actual, per_stage_timings, per_door_kills,
  dedup_skips, tripwires[], suspect}``.
* ``<sweep_runs root>/<run_id>/top100.json`` — the SHIPPED engine artifact
  (``brutex.engine.top100_artifact.write_top100``): a JSON OBJECT root
  ``{schema_version, run_id, preset, source, baselines, entries: [...]}``
  whose ``entries`` carry the ordered leaderboard rows ``{rank, mask_hex,
  conditions[], tf, direction, arm, metrics{}, regime{}, vix_band{},
  family_id}`` plus the SHIPPED additive keys (``run_id``, ``equity_ref``,
  …) the production trades reader keys on. The reader ADOPTS the writer's
  object root (DEC-ONECLICK-TOP100-OBJECT-ROOT-001 — the pre-#1621 bare-array
  fixture form is RETIRED and reads a NAMED corrupt) and maps the writer's
  metric names onto the display vocabulary at this ONE boundary
  (:data:`TOP100_METRIC_KEY_MAP`).
* ``<sweep_runs root>/<run_id>/events.jsonl`` — the P2 flight-recorder JSONL
  (record shape ``{ts_utc, run_id, kind, payload}`` + hash-chaining). The
  FILENAME is an ASSUMPTION registered with the contracts memory (§0.4) and
  lives in ONE constant (:data:`EVENTS_FILENAME`); the chain ALGORITHM is
  likewise ASSUMED behind the :func:`chain_hash` seam until P2 merges.
* ``<sweep_runs root>/<run_id>/results-depth/<instrument>/<tf>/<stem>.*`` —
  the SHIPPED P3 results-depth sidecar trio + stamp (#1275), paged via
  :class:`TradeStoreReader` → :func:`brutex.engine.trade_store.read_trade_page`
  (the merge-integration trades rebind, DEC-ONECLICK-SWEEP-RUNS-REBIND-001).
* ``<sweep_runs root>/<run_id>/trades/rank-%04d.json`` — the INTERIM
  FIXTURE-ONLY trades layout (§0.3), served only under the fail-loud
  :data:`FIXTURE_ROOT_ENV` override.

The ``<sweep_runs root>`` above is the gap-register Ruling-4 (2026-07-08)
location: ``<results_root parent>/sweep_runs/`` — a SIBLING of the sweeps
results tree, derived by :func:`runs_root` consistently with the ONE engine
path-builder seam :func:`brutex.engine.trade_store.sweep_run_dir`
(mirror-PINNED). The earlier ``<results_root>/runs/`` layout of the original
DEC-OPERATOR-PAGE-REAL-001 contract is SUPERSEDED — there is deliberately NO
legacy ``runs/`` fallback probe (a dual root could silently serve stale
artifacts); the committed fixture corpus migrated to the new layout in the
same change.

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

from brutex.engine.top100_artifact import TOP100_SCHEMA_VERSION
from brutex.engine.trade_store import (
    RESULTS_DEPTH_DIRNAME,
    SWEEP_RUNS_DIRNAME,
    SidecarPaths,
    TradeStoreError,
    read_trade_page,
)
from brutex.money import format_inr

_LOG = structlog.get_logger(__name__)

__all__ = [
    "AWAITING_DETAIL",
    "EVENTS_FILENAME",
    "FIXTURE_ROOT_ENV",
    "FIXTURE_SWEEPS_DIRNAME",
    "MANIFEST_FILENAME",
    "MANIFEST_REQUIRED_KEYS",
    "PREFLIGHT_FILENAME",
    "PREFLIGHT_REQUIRED_KEYS",
    "SWEEP_RUNS_DIRNAME",
    "TOP100_FILENAME",
    "TOP100_METRIC_KEY_MAP",
    "TOP100_ROW_REQUIRED_KEYS",
    "TOP100_SCHEMA_VERSION",
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
    "TradeStoreReader",
    "TradesReader",
    "TradesResult",
    "artifact_revision",
    "canonical_record_bytes",
    "chain_hash",
    "discover_runs",
    "fixture_results_root",
    "is_safe_run_id",
    "load_manifest",
    "load_preflight_fixture",
    "load_top100",
    "probe_run_dir",
    "raise_chain_not_verified",
    "resolve_fixture_root",
    "runs_revision",
    "runs_root",
    "sidecar_paths_from_ref",
    "tail_events",
    "trades_path",
    "verify_events_chain",
]

#: One artifact's read verdict. ``awaiting`` = not written yet (loud, honest);
#: ``corrupt`` = present but unreadable; ``partial`` = readable with a NAMED
#: defect (rows kept up to it / keys missing); ``ok`` = the full contract shape.
ArtifactStatus = Literal["ok", "awaiting", "corrupt", "partial"]

#: The pseudo sweeps-results directory INSIDE a fixture tree — see
#: :func:`fixture_results_root` (keeps the Ruling-4 parent-sibling math
#: self-contained under the fixture root; the dir itself need not exist).
FIXTURE_SWEEPS_DIRNAME: Final[str] = "sweeps"

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
#: (:class:`TradeStoreReader` over the lake-derived results root, or the
#: honest :class:`AwaitingTradesReader` when no lake is wired); set to an
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

#: The ONE reader-boundary metric-key mapping — engine writer name → display
#: name (DEC-ONECLICK-TOP100-OBJECT-ROOT-001). The SHIPPED engine artifact
#: (``brutex.engine.top100_artifact._metrics_payload``) emits the full
#: engine metric names; the display vocabulary (bx-screener.js COLUMNS /
#: bx-oneclick.js / strategy.html) reads the short names. Applied ADDITIVELY
#: in :func:`load_top100`: the display alias is added ONLY when absent, the
#: engine key passes through verbatim, every unmapped key is untouched, and
#: a metric the writer does not emit (``cpcv_pass_pct`` — no CPCV source in
#: the artifact today) stays ABSENT so the client renders the honest "—",
#: NEVER a fabricated 0.
TOP100_METRIC_KEY_MAP: Final[tuple[tuple[str, str], ...]] = (
    ("max_drawdown_paisa", "max_dd_paisa"),
    ("sharpe_ratio", "sharpe"),
    ("deflated_sharpe", "dsr"),
    ("total_trades", "trades"),
)

#: The contract's required preflight-verdict keys (P1 → P4).
PREFLIGHT_REQUIRED_KEYS: Final[tuple[str, ...]] = ("ok", "refusals", "clamps", "estimator")

#: Hard byte bound on one :func:`tail_events` read window (O(page) law).
_EVENTS_BYTES_CAP: Final[int] = 1 << 20  # 1 MiB

#: Hard per-page line bound for :func:`tail_events`.
_MAX_EVENTS_LIMIT: Final[int] = 1000

#: Hard per-page row bound for the trades readers.
_MAX_TRADES_LIMIT: Final[int] = 1000

#: Hard per-page row bound for the equity-series reader — one row per
#: SESSION (≈250/year), so 5000 covers ~20 years in one chart fetch while
#: still bounding a hostile/corrupt artifact read (O(page) law).
_MAX_EQUITY_LIMIT: Final[int] = 5000

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
        The :data:`ArtifactStatus` verdict — a VALID empty or short entries
        list is ``ok`` (the artifact EXISTS; T-M2 status law).
    rows
        The validated entries (kept up to the first bad row on ``partial``),
        metric keys already mapped via :data:`TOP100_METRIC_KEY_MAP` and the
        writer's ``arm`` dict already bridged to its display string
        (:func:`_bridge_top100_arm`; the raw dict rides as ``arm_raw``).
    detail
        Defect note naming the bad row index / missing keys (empty when ok).
    revision
        The ``"mtime_ns:size"`` stat token, ``None`` when absent.
    preset
        The artifact object root's ranking ``preset`` token (the R30 sort
        basis — surfaced so the pages can LABEL the ordering honestly);
        ``None`` when the artifact never parsed that far.
    """

    status: ArtifactStatus
    rows: tuple[dict[str, Any], ...]
    detail: str
    revision: str | None
    preset: str | None = None


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
    """Return the run-artifact tree root — ``<results_root parent>/sweep_runs``.

    THE one discovery seam (DEC-ONECLICK-SWEEP-RUNS-REBIND-001): the Ruling-4
    (2026-07-08) ``sweep_runs/`` SIBLING root beside the sweeps results tree,
    exactly where the SHIPPED #1275 writers land run artifacts —
    ``runs_root(root) / run_id`` is mirror-PINNED byte-equal to
    :func:`brutex.engine.trade_store.sweep_run_dir(root, run_id)` (the engine
    builder raises on an empty run_id, so this listing-level twin re-derives
    the law from the SAME imported :data:`SWEEP_RUNS_DIRNAME` constant rather
    than probing with a dummy id). SUPERSEDES the original
    ``<results_root>/runs/`` layout; deliberately NO legacy ``runs/`` fallback
    probe — a dual root could silently serve stale artifacts (fixtures
    migrated instead).

    Parameters
    ----------
    results_root
        The sweep results root (``<lake>/sweeps`` in production, or the
        fixture tree's pseudo root from :func:`fixture_results_root`).

    Returns
    -------
    Path
        The ``sweep_runs`` directory (may not exist yet — callers degrade to
        awaiting).
    """
    return results_root.parent / SWEEP_RUNS_DIRNAME


def fixture_results_root(fixture_root: Path) -> Path:
    """Return the pseudo sweeps root INSIDE a fixture tree (self-containment law).

    :func:`runs_root` derives the run tree from the results root's PARENT
    (Ruling 4), so a raw fixture directory passed as a results root would
    escape its own tree. The fixture override therefore treats
    ``<fixture_root>`` as a pseudo LAKE root: this pseudo
    ``<fixture_root>/sweeps`` results root (the dir need not exist) puts the
    derived run tree at ``<fixture_root>/sweep_runs/`` — always inside the
    fixture tree, mirroring the production lake layout exactly.

    Parameters
    ----------
    fixture_root
        The resolved :data:`FIXTURE_ROOT_ENV` directory.

    Returns
    -------
    Path
        ``<fixture_root>/sweeps`` — pass THIS wherever a results root is
        expected.
    """
    return fixture_root / FIXTURE_SWEEPS_DIRNAME


def trades_path(results_root: Path, run_id: str, rank: int) -> Path:
    """Return the INTERIM fixture trades page path ``sweep_runs/<run_id>/trades/rank-%04d.json``.

    Parameters
    ----------
    results_root
        The fixture tree's pseudo results root (:func:`fixture_results_root`
        — this layout exists ONLY under the fixture tree).
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
    """Return the runs-tree revision token keying the cached runs LISTING.

    The token folds the ``sweep_runs/`` directory's own stat token with a
    digest over every run's ``manifest.json`` stat token
    (DEC-ONECLICK-TOP100-OBJECT-ROOT-001 fold of finding [12]): the listing's
    ``suspect`` badge is PARSED from each manifest, and an IN-PLACE manifest
    rewrite (e.g. ``suspect`` flipping true mid-run) moves the manifest's
    file token but NOT the directory mtime — the old directory-only key
    served the stale badge until the next run landed. Probe cost is honest
    O(run dirs) ``stat`` calls per poll (never a parse — the parses stay
    behind the memo this token keys); the primed cache HIT itself remains a
    dict lookup. DELIBERATELY UNCAPPED (hostile-round F5 ruling): bounding
    the digest to the listing's newest-50 cap would need one ``stat`` per
    run DIR to rank by mtime first — N dir stats + 50 manifest stats is MORE
    syscalls than today's N manifest stats, so the full walk IS the cheaper
    honest option; the O(run dirs) scaling is disclosed here and in the
    ``test_oneclick_o1.py`` B-2 bench docstring instead. Remaining bound
    (Golden Rule #10): an in-place rewrite of
    ``top100.json``/``events.jsonl`` still does not move this token — the
    listing carries only their PRESENCE, which cannot change without the
    directory mtime moving, so nothing served from the listing goes stale.

    Parameters
    ----------
    results_root
        The sweep results root.

    Returns
    -------
    str or None
        ``"<dir mtime_ns:size>:m<manifest-digest16>"`` for the Ruling-4
        ``<results_root parent>/sweep_runs`` tree; the bare directory token
        when the tree cannot be listed; ``None`` when it cannot be statted.
    """
    root = runs_root(results_root)
    base = artifact_revision(root)
    if base is None:
        return None
    try:
        entries = list(os.scandir(root))
    except OSError:
        return base
    names: list[str] = []
    for entry in entries:
        try:
            if entry.is_dir():
                names.append(entry.name)
        except OSError:
            continue
    digest = sha256(base.encode("utf-8"))
    for name in sorted(names):
        token = artifact_revision(root / name / MANIFEST_FILENAME)
        digest.update(f"{name}={token}\n".encode())
    return f"{base}:m{digest.hexdigest()[:16]}"


# ── Discovery ───────────────────────────────────────────────────────────


def _is_file(path: Path) -> bool:
    """``Path.is_file`` (already swallows OSError → ``False``) — one probe point."""
    return path.is_file()


def probe_run_dir(run_dir: Path) -> RunInfo | None:
    """Probe ONE run directory into a :class:`RunInfo` (or ``None`` when absent).

    Parameters
    ----------
    run_dir
        The ``sweep_runs/<run_id>`` directory.

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
    """Discover run directories under the Ruling-4 ``sweep_runs/`` tree, newest first.

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
        The ``sweep_runs/<run_id>`` directory.

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


def _top100_row_shape_defect(row: dict[str, Any]) -> str | None:
    """Name the first INNER-shape defect of one required-keys-complete row.

    Sub-key validation (DEC-ONECLICK-TOP100-OBJECT-ROOT-001): the required
    keys must also carry usable SHAPES — ``metrics``/``regime`` objects (the
    metric mapping and every downstream ``row.metrics.*`` read key on them),
    ``conditions`` an array, ``rank`` an integer, ``vix_band`` an object or
    the honest R31 null. Returns ``None`` when sound, else the named defect.
    """
    rank = row["rank"]
    if not isinstance(rank, int) or isinstance(rank, bool):
        return "carries a non-integer rank"
    if not isinstance(row["conditions"], list):
        return "conditions is not an array"
    # Deliberately NO metric SUB-key mandate (hostile-round finder (a) pin):
    # the writer legitimately emits null metric values (``deflated_sharpe``
    # can be ``None``) and omits others entirely (``cpcv_pass_pct``) — a
    # sub-key/type mandate here would false-flag REAL artifacts as partial.
    # An absent/None metric sub-key renders the honest "—" client-side BY
    # DESIGN; only the CONTAINER shapes are gated.
    if not isinstance(row["metrics"], dict):
        return "metrics is not an object"
    if not isinstance(row["regime"], dict):
        return "regime is not an object"
    vix_band = row["vix_band"]
    if vix_band is not None and not isinstance(vix_band, dict):
        return "vix_band is neither an object nor null"
    return None


def _map_top100_metrics(row: dict[str, Any]) -> dict[str, Any]:
    """Apply :data:`TOP100_METRIC_KEY_MAP` additively to one validated row.

    The display alias is added ONLY when absent (an artifact already carrying
    the display name is never overwritten); the engine key stays verbatim;
    everything else — the row AND its unmapped metric keys — passes through
    untouched at full precision (money 2dp is client-side only,
    DEC-MONEY-2DP-001). An absent engine key adds NOTHING — ``cpcv_pass_pct``
    et al. stay absent so the client renders "—", never a fabricated 0.
    """
    metrics: dict[str, Any] = dict(row["metrics"])
    changed = False
    for engine_key, display_key in TOP100_METRIC_KEY_MAP:
        if engine_key in metrics and display_key not in metrics:
            metrics[display_key] = metrics[engine_key]
            changed = True
    if not changed:
        return row
    mapped = dict(row)
    mapped["metrics"] = metrics
    return mapped


def _arm_leg_text(value: object) -> str:
    """One SL/TP leg of the arm display string — ``format_inr`` paisa→₹ 2dp.

    A non-integer leg (the writer's honest ``None``, or hand-edited junk)
    renders the "—" segment — never a fabricated amount (Golden Rule #1).
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return format_inr(value)
    return "—"


def _bridge_top100_arm(row: dict[str, Any]) -> dict[str, Any]:
    """Bridge the writer's arm DICT to an honest display string (hostile-round F1).

    The SHIPPED engine writer emits ``arm`` as an OBJECT
    (``brutex.engine.top100_artifact._entry_payload`` —
    ``{"sl_paisa": int | None, "tp_paisa": int | None}``), but every display
    site stringifies the field raw (``String(row.arm)`` in bx-oneclick.js /
    strategy.html / screener.html), which renders ``"[object Object]"`` on a
    REAL sweep card. This ONE reader boundary (the :data:`TOP100_METRIC_KEY_MAP`
    pattern) replaces the dict with ``"SL <₹sl> · TP <₹tp>"`` — each leg via
    the house :func:`brutex.money.format_inr` (paisa → rupees, exactly 2dp,
    DEC-MONEY-2DP-001), a ``None``/junk leg reading "—" — and keeps the raw
    dict ADDITIVELY under ``arm_raw`` (only when that key is absent). A
    non-dict ``arm`` (a legacy display string) passes through untouched.
    """
    arm = row["arm"]
    if not isinstance(arm, dict):
        return row
    bridged = dict(row)
    bridged["arm"] = (
        f"SL {_arm_leg_text(arm.get('sl_paisa'))} · TP {_arm_leg_text(arm.get('tp_paisa'))}"
    )
    if "arm_raw" not in bridged:
        bridged["arm_raw"] = arm
    return bridged


def load_top100(run_dir: Path) -> Top100Result:
    """Load ``top100.json`` — the WRITER's object root, entries validated per row.

    READER ADOPTS WRITER (DEC-ONECLICK-TOP100-OBJECT-ROOT-001): the canonical
    shape is the SHIPPED engine artifact object root
    ``{schema_version, run_id, preset, source, baselines, entries: [...]}``
    (``brutex.engine.top100_artifact.write_top100``). The pre-#1621
    bare-array fixture form is RETIRED — it reads a NAMED ``corrupt``, never
    a silent second dialect (a dual shape let the fixture corpus green-light
    a reader every real sweep artifact failed). An unknown FUTURE
    ``schema_version`` degrades to a NAMED ``corrupt`` (never a crash); a
    missing ``schema_version`` key is tolerated (validated only when
    present). A VALID EMPTY or SHORT entries list is ``status="ok"`` — the
    artifact EXISTS (T-M2 status law); the row count is ALWAYS ``len(rows)``,
    never assumed 100. A bad ROW → ``partial``, rows kept up to it, defect
    named (required keys + the :func:`_top100_row_shape_defect` inner
    shapes). Each kept row's metrics gain the :data:`TOP100_METRIC_KEY_MAP`
    display aliases additively, and the writer's ``arm`` OBJECT is bridged to
    its display string via :func:`_bridge_top100_arm` (the raw dict rides
    along additively as ``arm_raw``) — the display sites ``String()`` the
    field, so an unbridged dict rendered ``"[object Object]"`` on every real
    sweep card (hostile-round F1).

    Parameters
    ----------
    run_dir
        The ``sweep_runs/<run_id>`` directory.

    Returns
    -------
    Top100Result
        Beyond the mapping aliases, rows pass through UNTOUCHED (full
        precision); ``preset`` carries the artifact's ranking token (the
        sort-basis label source) once the object root parsed.
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
    if isinstance(obj, list):
        return Top100Result(
            "corrupt",
            (),
            f"{TOP100_FILENAME} is a bare JSON array — the retired pre-#1621 fixture "
            "form; the engine writer emits an object root with an entries array",
            revision,
        )
    if not isinstance(obj, dict):
        return Top100Result(
            "corrupt", (), f"{TOP100_FILENAME} is not the writer's JSON object root", revision
        )
    version = obj.get("schema_version")
    # Hostile-round finder (b): the version must be a GENUINE int — a JSON
    # ``true`` (Python bool, ``True == 1``) or a float ``1.0`` pun must not
    # slip past ``!=`` equality; both name themselves corrupt instead.
    if version is not None and (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != TOP100_SCHEMA_VERSION
    ):
        return Top100Result(
            "corrupt",
            (),
            f"{TOP100_FILENAME} schema_version {version!r} is not the supported "
            f"integer {TOP100_SCHEMA_VERSION} — a newer writer? upgrade the display reader",
            revision,
        )
    raw_preset = obj.get("preset")
    preset = raw_preset if isinstance(raw_preset, str) and raw_preset else None
    entries_raw = obj.get("entries")
    if not isinstance(entries_raw, list):
        return Top100Result(
            "corrupt",
            (),
            f"{TOP100_FILENAME} object root carries no entries array",
            revision,
            preset,
        )
    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(entries_raw):
        if not isinstance(row, dict):
            return Top100Result(
                "partial",
                tuple(rows),
                f"row {idx} is not an object — rows kept up to it",
                revision,
                preset,
            )
        missing = [key for key in TOP100_ROW_REQUIRED_KEYS if key not in row]
        if missing:
            return Top100Result(
                "partial",
                tuple(rows),
                f"row {idx} missing contract keys: {', '.join(missing)} — rows kept up to it",
                revision,
                preset,
            )
        defect = _top100_row_shape_defect(row)
        if defect is not None:
            return Top100Result(
                "partial",
                tuple(rows),
                f"row {idx} {defect} — rows kept up to it",
                revision,
                preset,
            )
        rows.append(_bridge_top100_arm(_map_top100_metrics(row)))
    return Top100Result("ok", tuple(rows), "", revision, preset)


def load_preflight_fixture(run_dir: Path) -> ManifestResult:
    """Load the P1 preflight-verdict fixture ``preflight.json`` when present.

    Renders the P1 CONTRACT verdict shape ``{ok, refusals[], clamps[],
    estimator{trials, est_seconds_provisional, est_bytes}}``; absent →
    awaiting ("estimator not wired yet — lands at merge-integration (P1)").

    Parameters
    ----------
    run_dir
        The ``sweep_runs/<run_id>`` directory.

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
        The ``sweep_runs/<run_id>`` directory.
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
        The ``sweep_runs/<run_id>`` directory.
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
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def equity_page(self, run_id: str, rank: int, *, offset: int, limit: int) -> TradesResult:
        """Return one page of stored equity-series points for (*run_id*, *rank*)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class AwaitingTradesReader:
    """No-lake FALLBACK: always ``status='awaiting'``, ``source='none'``.

    Since DEC-ONECLICK-SWEEP-RUNS-REBIND-001 the PRODUCTION default is
    :class:`TradeStoreReader` (the shipped #1275 results-depth sidecars);
    this reader serves only the no-lake-root-wired configuration — it NEVER
    fabricates trade rows (Golden Rule #1).
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
            detail=f"{AWAITING_DETAIL} — no lake root wired for trades",
        )

    def equity_page(self, run_id: str, rank: int, *, offset: int, limit: int) -> TradesResult:
        """Return the honest awaiting equity page (parameters accepted, unused).

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
            detail=f"{AWAITING_DETAIL} — no lake root wired for the equity series",
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
            The tree holding ``sweep_runs/<run_id>/trades/rank-%04d.json``
            pages (paths derived via :func:`fixture_results_root`, so the
            reader never leaves the fixture tree).
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
        path = trades_path(fixture_results_root(self._root), run_id, rank)
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

    def equity_page(self, run_id: str, rank: int, *, offset: int, limit: int) -> TradesResult:
        """Return the honest awaiting equity page — the fixture tree has no sidecars.

        The interim §0.3 fixture layout carries trades pages ONLY; the equity
        SERIES lives in the #1275 results-depth sidecars, which only the
        production :class:`TradeStoreReader` serves. The fixture arm therefore
        reads honest awaiting — never a synthesized curve (Golden Rule #1).

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
            detail=f"{AWAITING_DETAIL} — the fixture tree carries no results-depth equity sidecars",
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


# ── Production trades reader (the merge-integration rebind — #1275 sidecars) ─


#: A ``tuple_ref`` is exactly ``<instrument>/<tf>/<store_stem>`` — three parts
#: (the ``top100_artifact.tuple_ref`` law, mirror-pinned).
_TUPLE_REF_PARTS: Final[int] = 3


def sidecar_paths_from_ref(run_dir: Path, tuple_ref: str) -> SidecarPaths | None:
    """Build the results-depth sidecar trio + stamp paths for one ``equity_ref.tuple``.

    *tuple_ref* is the top100 entry's ``<instrument>/<tf>/<store_stem>``
    (``top100_artifact``'s ``tuple_ref`` law). The filename suffixes mirror
    :func:`brutex.engine.trade_store.sidecar_paths_for` — that engine builder
    maps a STORE path under the sweeps root (which the display zone does not
    hold), so this display-side twin is mirror-PINNED byte-equal against it by
    a unit test rather than delegated (engine edit forbidden in this zone).

    Parameters
    ----------
    run_dir
        The ``sweep_runs/<run_id>`` directory.
    tuple_ref
        The artifact-supplied tuple reference (UNTRUSTED data).

    Returns
    -------
    SidecarPaths or None
        ``None`` when the ref is not exactly three safe path segments — a
        hostile/malformed artifact value never reaches the filesystem.
    """
    parts = tuple_ref.split("/")
    if len(parts) != _TUPLE_REF_PARTS or not all(is_safe_run_id(part) for part in parts):
        return None
    instrument, tf, stem = parts
    base = run_dir / RESULTS_DEPTH_DIRNAME / instrument / tf
    return SidecarPaths(
        trades=base / f"{stem}.trades.parquet",
        aggs=base / f"{stem}.aggs.parquet",
        equity=base / f"{stem}.equity.parquet",
        stamp=base / f"{stem}.stamp.json",
    )


def _load_equity_records(path: Path, combo_run_id: str) -> tuple[dict[str, Any], ...]:
    """Read ONE combo's equity-series rows from the ``<stem>.equity.parquet`` sidecar.

    The display-zone read of ``trade_store.EQUITY_COLUMNS`` (engine edits are
    forbidden in this zone and the engine ships no equity page reader — the
    stamp indexes only ``trade_spans``): an O(equity-file) ``run_id`` filter
    in session-date order (the writer sorts per capture), memoised by the
    caller per (file revision, combo). Every value is projected onto the
    wire shape verbatim — never synthesized.

    Raises
    ------
    TradeStoreError
        When polars is unavailable or the sidecar is unreadable/mis-shaped
        (the SAME error family the trades reader maps to a NAMED corrupt).
    """
    try:
        import polars as pl
    except ImportError as exc:  # the [data] extra is absent — never a route 500
        raise TradeStoreError(
            "polars is required to read the equity sidecar — install brutex[data]"
        ) from exc
    try:
        frame = pl.scan_parquet(path).filter(pl.col("run_id") == combo_run_id).collect()
        records = tuple(
            {
                "session_date": str(record["session_date"]),
                "equity_close_paisa": int(record["equity_close_paisa"]),
                # A no-1m-bars session stores an honest None low (the
                # SessionEquityPoint contract) — passed through, never 0.
                "equity_intraday_low_paisa": (
                    None
                    if record["equity_intraday_low_paisa"] is None
                    else int(record["equity_intraday_low_paisa"])
                ),
                "trades_closed": int(record["trades_closed"]),
            }
            for record in frame.iter_rows(named=True)
        )
    except (pl.exceptions.PolarsError, OSError, KeyError, TypeError, ValueError) as exc:
        raise TradeStoreError(f"unreadable equity sidecar at {path}: {exc}") from exc
    return records


class TradeStoreReader:
    """PRODUCTION trades reader over the SHIPPED #1275 results-depth sidecars.

    The merge-integration trades rebind (DEC-ONECLICK-SWEEP-RUNS-REBIND-001)
    — the DEFAULT whenever a lake root is wired and no fixture override is
    active. Resolution per ``(run_id, rank)``:

    1. the run's ``top100.json`` (memoised once per stat-token revision) maps
       *rank* → the entry's SHIPPED additive keys — ``run_id`` (the
       finalist's 64-hex store row key) + ``equity_ref.tuple``
       (``<instrument>/<tf>/<store_stem>``); a pre-#1275 artifact without
       them degrades to honest awaiting, never a guess;
    2. the sidecar trio at ``results-depth/<tuple_ref>.*`` pages via
       :func:`brutex.engine.trade_store.read_trade_page` — an absent stamp is
       honest ``awaiting``; stamp/page damage is a NAMED ``corrupt`` (never a
       500, never fabricated rows).

    ``limit`` beyond :data:`brutex.engine.trade_store.PAGE_LIMIT_MAX` is
    clamped INSIDE ``read_trade_page`` (the db_view clamp law) and the clamp
    is named in ``detail``. ``lots``/``costs_paisa`` pass through as honest
    ``None`` (NULL-by-design until P5 — Ruling 18); the additive
    ``trade_seq``/``path_1m_status``/``mae_granularity`` keys pass through
    verbatim. ``MemoryError`` re-raises raw (house law).

    Attributes
    ----------
    parse_count
        Deterministic spy seam: incremented on every REAL top100 parse (the
        no-reparse tests read it — no timing needed).
    """

    def __init__(self, results_root: Path) -> None:
        """Bind the reader to *results_root* (the sweeps tree — NOT the run tree).

        Parameters
        ----------
        results_root
            The sweep results root; runs resolve via :func:`runs_root`.
        """
        self._results_root = results_root
        self._lock = threading.Lock()
        self._top100_memo: tuple[str, str, Top100Result] | None = None
        self._equity_memo: tuple[tuple[str, str, str], tuple[dict[str, Any], ...]] | None = None
        self.parse_count: int = 0

    def page(self, run_id: str, rank: int, *, offset: int, limit: int) -> TradesResult:
        """Return one page of REAL trade rows for (*run_id*, *rank*).

        Parameters
        ----------
        run_id
            The sweep run directory name (unsafe ids degrade to awaiting —
            the route 400s them first; this is the defensive belt).
        rank
            The 1-based leaderboard rank.
        offset, limit
            Page window (clamped inside ``read_trade_page`` — offset ≥ 0,
            limit to ``[1, PAGE_LIMIT_MAX]``).

        Returns
        -------
        TradesResult
            ``source='results_store'`` whenever the sidecars were consulted;
            absent artifacts → honest awaiting (``total=None``, never 0).
        """
        resolved = self._resolve_sidecars(run_id, rank)
        if isinstance(resolved, TradesResult):
            return resolved
        combo_run_id, paths = resolved
        try:
            page = read_trade_page(paths, run_id=combo_run_id, offset=offset, limit=limit)
        except TradeStoreError as exc:
            _LOG.warning(
                "oneclick.artifact_corrupt",
                artifact="results_depth_trades",
                path=str(paths.stamp),
                error_type=type(exc).__name__,
            )
            return TradesResult(
                "corrupt", (), None, "results_store", f"results-depth read failed: {exc}"
            )
        detail = ""
        if limit != page.limit:
            detail = f"limit clamped to {page.limit} (trade_store page law)"
        return TradesResult(
            status="ok",
            rows=tuple(dict(row) for row in page.rows),
            total=page.total,
            source="results_store",
            detail=detail,
        )

    def equity_page(self, run_id: str, rank: int, *, offset: int, limit: int) -> TradesResult:
        """Return one page of the combo's STORED equity-series points.

        The R27 "equity curve = stored real series" read
        (DEC-ONECLICK-WINNER-DETAIL-TRUTH-001, audit item 4C — overlaps #48):
        the entry's ``equity_ref`` sidecar (``<stem>.equity.parquet``,
        ``trade_store.EQUITY_COLUMNS`` — one row per (finalist, session))
        resolved through the SAME rank → ``equity_ref.tuple`` chain the
        trades page uses, with the SAME honesty degrades (pre-#1275 artifact /
        absent sidecars → awaiting; hostile ref / unreadable file → NAMED
        corrupt; never a 500, never a fabricated curve).

        HONEST envelope: the stamp carries NO equity span index (unlike the
        trades' ``trade_spans``), so the FIRST read per (file revision, combo)
        is an O(equity-file) ``run_id`` filter; the single-entry memo makes
        every repeat page an O(page) slice. ``limit`` clamps to
        ``[1, 5000]`` (one row per session ≈ 250/year — one fetch charts ~20
        years) and the clamp is named in ``detail``.

        Parameters
        ----------
        run_id
            The sweep run directory name (unsafe ids degrade to awaiting).
        rank
            The 1-based leaderboard rank.
        offset, limit
            Page window (offset clamped ≥ 0; limit to ``[1, 5000]``).

        Returns
        -------
        TradesResult
            ``rows`` are ``{session_date, equity_close_paisa,
            equity_intraday_low_paisa, trades_closed}`` wire points;
            ``total`` is the combo's full series length.
        """
        resolved = self._resolve_sidecars(run_id, rank)
        if isinstance(resolved, TradesResult):
            return resolved
        combo_run_id, paths = resolved
        try:
            series = self._equity_cached(paths.equity, combo_run_id)
        except TradeStoreError as exc:
            _LOG.warning(
                "oneclick.artifact_corrupt",
                artifact="results_depth_equity",
                path=str(paths.equity),
                error_type=type(exc).__name__,
            )
            return TradesResult(
                "corrupt", (), None, "results_store", f"equity sidecar read failed: {exc}"
            )
        page_offset = max(0, offset)
        page_limit = max(1, min(limit, _MAX_EQUITY_LIMIT))
        detail = ""
        if page_limit != limit:
            detail = f"limit clamped to {page_limit} (equity page law)"
        return TradesResult(
            status="ok",
            rows=series[page_offset : page_offset + page_limit],
            total=len(series),
            source="results_store",
            detail=detail,
        )

    def _resolve_sidecars(self, run_id: str, rank: int) -> tuple[str, SidecarPaths] | TradesResult:
        """Resolve (*run_id*, *rank*) → the combo's ``(store run_id, sidecar paths)``.

        The ONE rank → ``equity_ref.tuple`` resolution chain shared by the
        trades AND equity pages (steps 1-2 of the class docstring). Returns a
        :class:`TradesResult` degrade instead of raising — awaiting for
        absent/pre-#1275 artifacts, NAMED corrupt for a hostile tuple ref.
        """
        if not is_safe_run_id(run_id) or rank < 1:
            return TradesResult(
                "awaiting", (), None, "none", "invalid run_id/rank — nothing to read"
            )
        run_dir = runs_root(self._results_root) / run_id
        top100 = self._top100_cached(run_dir)
        if top100.status in ("awaiting", "corrupt"):
            return TradesResult(top100.status, (), None, "none", top100.detail)
        entry = next(
            (row for row in top100.rows if row.get("rank") == rank),
            None,
        )
        if entry is None:
            return TradesResult(
                "awaiting",
                (),
                None,
                "none",
                f"{AWAITING_DETAIL} — no leaderboard entry at rank {rank}",
            )
        combo_run_id = entry.get("run_id")
        equity_ref = entry.get("equity_ref")
        tuple_ref = equity_ref.get("tuple") if isinstance(equity_ref, dict) else None
        if not isinstance(combo_run_id, str) or not combo_run_id or not isinstance(tuple_ref, str):
            return TradesResult(
                "awaiting",
                (),
                None,
                "none",
                f"{AWAITING_DETAIL} — top100 entry carries no results-depth reference "
                "(pre-#1275 artifact)",
            )
        paths = sidecar_paths_from_ref(run_dir, tuple_ref)
        if paths is None:
            return TradesResult(
                "corrupt",
                (),
                None,
                "none",
                f"top100 entry tuple ref {tuple_ref!r} is not three safe path segments",
            )
        if artifact_revision(paths.stamp) is None:
            return TradesResult(
                "awaiting",
                (),
                None,
                "none",
                f"{AWAITING_DETAIL} — results-depth sidecars absent for {tuple_ref}",
            )
        return combo_run_id, paths

    def _equity_cached(self, path: Path, combo_run_id: str) -> tuple[dict[str, Any], ...]:
        """Serve the single-entry ``(path, revision, combo)`` equity memo.

        A ``None`` revision (absent/unstattable) reads fresh and caches
        NOTHING (the ``lake_revision`` doctrine — no stale illusion).

        Raises
        ------
        TradeStoreError
            When the sidecar is unreadable (the caller names it corrupt).
        """
        revision = artifact_revision(path)
        if revision is None:
            return _load_equity_records(path, combo_run_id)
        key = (str(path), revision, combo_run_id)
        with self._lock:
            memo = self._equity_memo
        if memo is not None and memo[0] == key:
            return memo[1]
        series = _load_equity_records(path, combo_run_id)
        with self._lock:
            self._equity_memo = (key, series)
        return series

    def _top100_cached(self, run_dir: Path) -> Top100Result:
        """Serve the single-entry ``(path, revision)`` top100 memo, parsing on a miss.

        A ``None`` revision (absent/unstattable) computes fresh and caches
        NOTHING (the ``lake_revision`` doctrine — no stale illusion).
        """
        path = run_dir / TOP100_FILENAME
        revision = artifact_revision(path)
        if revision is None:
            self.parse_count += 1
            return load_top100(run_dir)
        key = str(path)
        with self._lock:
            memo = self._top100_memo
        if memo is not None and memo[0] == key and memo[1] == revision:
            return memo[2]
        self.parse_count += 1
        parsed = load_top100(run_dir)
        with self._lock:
            self._top100_memo = (key, revision, parsed)
        return parsed
