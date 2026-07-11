"""Deterministic daily-rotating sampler + coverage ledger (design §3, ONE law).

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). The engine set is
ALWAYS in scope; a seeded fill of ``--sample N`` extra datasets rotates daily
(``seed = --seed or the IST report date``), so a nightly cron covers the whole
population in ~⌈population/N⌉ nights with zero configuration and any night is
reproducible by passing its date as ``--seed``. The coverage ledger makes the
rotation's accumulation VISIBLE — a forever-missed region cannot hide.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.data.lake_invariants import ENGINE_INDEX_COORDS

_LOG = structlog.get_logger(__name__)

#: The India-VIX reference series — always sampled (CLAUDE.md §2 Rule 3).
VIX_COORD: Final[tuple[str, str, str]] = ("NSE", "INDEX", "INDIAVIX")

#: The always-in-scope dataset coordinates: the 3 engine index series + VIX.
ENGINE_COORDS: Final[tuple[tuple[str, str, str], ...]] = (*ENGINE_INDEX_COORDS, VIX_COORD)

#: Coverage-ledger accumulation window rendered in the report headline.
LEDGER_WINDOW_NIGHTS: Final[int] = 14

COVERAGE_LEDGER_FILENAME: Final[str] = "coverage_ledger.json"


def dataset_key(coord: tuple[str, str, str]) -> str:
    """Canonical string key for an ``(exchange, segment, symbol)`` coordinate."""
    return "/".join(coord)


def enumerate_spot_datasets(lake_root: Path) -> list[tuple[str, str, str]]:
    """Enumerate stored NON-F&O dataset coordinates by scanning ``bars/``.

    One ``os.scandir`` walk of ``bars/<exchange>/<segment>/<symbol>`` — the
    sampling population. F&O contract dirs are a separate population
    (:func:`enumerate_fno_contracts`). Missing tree ⇒ empty list (the caller's
    ABSTAIN arm).
    """
    out: list[tuple[str, str, str]] = []
    bars = lake_root / "bars"
    if not bars.is_dir():
        return out
    for exch in sorted(p for p in bars.iterdir() if p.is_dir()):
        for seg in sorted(p for p in exch.iterdir() if p.is_dir()):
            if seg.name == "FNO":
                continue
            out.extend(
                (exch.name, seg.name, sym.name)
                for sym in sorted(p for p in seg.iterdir() if p.is_dir())
            )
    return out


def enumerate_fno_contracts(lake_root: Path, *, exchange: str = "NSE") -> list[str]:
    """Enumerate stored F&O contract symbol dirs under ``bars/<exchange>/FNO/``."""
    fno = lake_root / "bars" / exchange / "FNO"
    if not fno.is_dir():
        return []
    return sorted(p.name for p in fno.iterdir() if p.is_dir())


def _rotation_rank(seed: str, key: str) -> str:
    """Deterministic per-(seed, key) sort token: sha256(seed ‖ key)."""
    return hashlib.sha256(f"{seed}\x1f{key}".encode()).hexdigest()


@dataclass(frozen=True)
class SamplePlan:
    """The resolved scope of one check: forced engine set + the seeded fill."""

    engine: tuple[tuple[str, str, str], ...]
    sampled: tuple[tuple[str, str, str], ...]
    population: int
    seed: str
    mode: str  # "full" | "sample"

    @property
    def selected(self) -> tuple[tuple[str, str, str], ...]:
        """Engine set first, then the seeded fill — the examined coordinates."""
        return (*self.engine, *self.sampled)

    @property
    def selection_keys(self) -> tuple[str, ...]:
        """Canonical string keys for the coverage block."""
        return tuple(dataset_key(c) for c in self.selected)


def sample_datasets(
    population: Sequence[tuple[str, str, str]],
    *,
    seed: str,
    sample_n: int,
    full: bool,
) -> SamplePlan:
    """Resolve the sampling law: engine force-include + seeded rotating fill.

    ``--full`` (or ``sample_n >= |non-engine population|``) lifts sampling to
    ``mode='full'``. The fill is ``sorted(pop, key=sha256(seed ‖ key))[:N]``
    — deterministic daily rotation when *seed* is the IST report date.
    """
    pop_set = set(population)
    engine = tuple(c for c in ENGINE_COORDS if c in pop_set)
    rest = sorted(pop_set - set(engine))
    if full or sample_n >= len(rest):
        return SamplePlan(
            engine=engine,
            sampled=tuple(rest),
            population=len(pop_set),
            seed=seed,
            mode="full",
        )
    picked = sorted(rest, key=lambda c: _rotation_rank(seed, dataset_key(c)))[:sample_n]
    return SamplePlan(
        engine=engine,
        sampled=tuple(sorted(picked)),
        population=len(pop_set),
        seed=seed,
        mode="sample",
    )


# ────────────────────────────────────────────────────────────────────────
# Coverage ledger — sampling accumulation made visible (§3.6)
# ────────────────────────────────────────────────────────────────────────


def coverage_ledger_path(lake_root: Path) -> Path:
    """The ledger's home: ``<lake_root>/acceptance/coverage_ledger.json``."""
    return lake_root / "acceptance" / COVERAGE_LEDGER_FILENAME


def load_coverage_ledger(path: Path) -> dict[str, dict[str, str]]:
    """Load the ``{check_id: {dataset_key: last_sampled_iso_date}}`` ledger.

    Corrupt / missing ⇒ empty dict (the ledger is an accumulation aid, never
    a verdict input — losing it only resets the visibility counters).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for check_id, cells in raw.items():
        if isinstance(cells, dict):
            out[str(check_id)] = {str(k): str(v) for k, v in cells.items()}
    return out


def update_coverage_ledger(
    path: Path,
    *,
    check_id: str,
    dataset_keys: Iterable[str],
    run_date: date,
) -> None:
    """Record this run's sampled cells (atomic replace; best-effort, warn-only)."""
    ledger = load_coverage_ledger(path)
    cells = ledger.setdefault(check_id, {})
    stamp = run_date.isoformat()
    for key in dataset_keys:
        cells[key] = stamp
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_simple(
            path,
            lambda tmp: tmp.write_text(
                json.dumps(ledger, indent=1, sort_keys=True), encoding="utf-8"
            ),
        )
    except OSError as exc:  # visibility aid only — never fails the run
        _LOG.warning("acceptance.coverage_ledger_write_failed", path=str(path), error=str(exc))


def coverage_ledger_summary(
    ledger: dict[str, dict[str, str]],
    *,
    population_keys: Sequence[str],
    run_date: date,
) -> dict[str, Any]:
    """Headline accumulation stats: % sampled in the window + oldest never-sampled."""
    if not population_keys:
        return {"datasets_sampled_window_pct": None, "oldest_unsampled": None}
    floor = (run_date - timedelta(days=LEDGER_WINDOW_NIGHTS)).isoformat()
    sampled_recent: set[str] = set()
    ever: set[str] = set()
    for cells in ledger.values():
        for key, stamp in cells.items():
            ever.add(key)
            if stamp >= floor:
                sampled_recent.add(key)
    pop = set(population_keys)
    pct = round(100.0 * len(sampled_recent & pop) / len(pop), 1)
    never = sorted(pop - ever)
    return {
        "datasets_sampled_window_pct": pct,
        "oldest_unsampled": never[0] if never else None,
    }
