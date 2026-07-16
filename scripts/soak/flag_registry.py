#!/usr/bin/env python3
"""flag_registry.py — the ONE per-flag table for the flip-soak harness (data, not code).

DEC-FLIP-SOAK-HARNESS-001 (number pending coordinator reservation). Every
Rust-conversion default-ON flip ADR is gated on the standard five-part
evidence bundle (grid-scale twin identity · PROVEN engagement · soak-drift
cleanliness · honest wall-clock A/B · a committed schema-validated PASS
artifact — see ``docs/soak/README.md``). ~11 flags each shipping their own
180-line twin script would be exactly the copy-paste CLAUDE.md §2 Rule 11
COMMON forbids, so the harness is ONE parameterized orchestrator
(``flip_soak.sh`` + ``flip_soak_driver.py`` + ``emit_pass.py``) and THIS
module is the only place a flag's soak parameters live.

Naming law (the design §1(b) requirement, enforced by
``tests/unit/test_flip_soak_harness.py``): every flag's wrapper MUST emit
structlog event tokens of the exact shape ``<surface_prefix>.engaged`` /
``<surface_prefix>.fallback`` / ``<surface_prefix>.kernel_autoreverted``
(and, for DIFFERENTIAL-class flags, ``<surface_prefix>.divergence``), so the
parameterized engagement gate needs only the per-flag ``surface_prefix``.
Registry rows therefore DERIVE their markers from the prefix via the two
helpers below — a hand-written marker that drifts from the prefix fails the
naming-law pin.

Flag classes (the three PASS-condition oracles):

* ``SWEEP_TWIN`` — twin ``brutex sweep-all`` runs into two scratch results
  roots; PASS = byte-identical relative-path sha256 manifests over the
  persisted result artifacts (the DEC-SWEEP-UNKILLABLE-CORE-001 "per-tuple
  store SHA-256 maps identical" A/B idiom, as productionized by PR #1479's
  ``rust_enum_twin_run.sh``) + the engagement gate.
* ``SURFACE_GOLDEN`` — the surface's OWN byte goldens rendered into two
  scratch trees (lake render / audit chain / calendar / rate math / Greeks
  batch); same manifest mechanics, surface-specific ``twin_argv``.
* ``DIFFERENTIAL`` — byte-identity is trivially true BY DESIGN (the Rust
  side computes, the Python oracle's value ships — the ranking b* family),
  so PASS = engagement + ZERO ``<surface_prefix>.divergence`` occurrences
  over the whole soak (the R45 never-adopt/differential idiom).

Honest envelope (Golden Rule #10): the two rows below are the two flags the
Rust-conversion track has LANDED ON THEIR SLICE BRANCHES (PR #1479 enum ·
PR #1464 worker-kernel-cache); NEITHER flag exists on local main at this
branch's base, so a twin run against a main checkout refuses at the
engagement gate — the harness serves slice branches via the workflow's
``ref`` input. Rows for future flags are added by each slice PR. The grid
scales are PROVISIONAL pending the operator wall-time probe on the real box
(design §6 open question 1); the kernel-cache RSS ceiling converts PR
#1464's MODELED 0.66-0.73 GB/worker retention claim into a measurable soak
gate — it is a tunable heuristic, not a proven limit.

CLI (consumed by ``flip_soak.sh`` — bash-3.2-safe line-oriented output)::

    flag_registry.py --list-flags
    flag_registry.py --flag FLAG --field {flag_class,surface_prefix,
        engaged_marker,bad_markers,divergence_marker,artifact_globs,
        extra_env,rss_ceiling_kib}
    flag_registry.py --flag FLAG --render-twin-argv --lake-root R
        --results-root O --scale S
    flag_registry.py --flag FLAG --manifest-root ROOT
    flag_registry.py --flag FLAG --snapshot-root ROOT

``--manifest-root`` prints the relative-path sha256 manifest ("<sha>  <rel>"
lines, sorted) over the flag's ``artifact_globs``; ``--snapshot-root``
prints the arm's ``data_snapshot_hash`` (recursively harvested from every
``*.json`` under ROOT — stage stamps carry it as ``extra.data_snapshot_hash``,
per ``brutex.observability.stage_stamp.SNAPSHOT_EXTRA_KEY``), the empty
string when absent, or the literal token ``INCONSISTENT`` when one arm holds
MORE THAN ONE distinct hash (a lake that moved MID-arm — the caller maps
both mismatch shapes to the ``RETRY_ENV_MOVED`` verdict, never a kernel
FAIL).

Test seam: ``FLIP_SOAK_EXTRA_FLAGS`` may name a JSON file of additional rows
(a list of FlagSpec-shaped objects) merged AFTER the built-in registry —
used by the CI dry-mode tests to exercise the SURFACE_GOLDEN / DIFFERENTIAL
class arms before any production row of those classes lands. Production
rows land in THIS file via reviewed PRs, never via the env seam.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# ── Flag classes ─────────────────────────────────────────────────────

SWEEP_TWIN: Final[str] = "SWEEP_TWIN"
SURFACE_GOLDEN: Final[str] = "SURFACE_GOLDEN"
DIFFERENTIAL: Final[str] = "DIFFERENTIAL"

FLAG_CLASSES: Final[frozenset[str]] = frozenset({SWEEP_TWIN, SURFACE_GOLDEN, DIFFERENTIAL})

#: The env var naming the optional extra-rows JSON file (test seam — see module doc).
EXTRA_FLAGS_ENV: Final[str] = "FLIP_SOAK_EXTRA_FLAGS"

#: The default bad-marker alternatives (the two masked-PASS paths PR #1479's
#: script header names — stale/absent-wheel fallback and shadow-burn-in
#: autorevert — plus the refused-result and wheel-state tokens).
DEFAULT_BAD_ALTERNATIVES: Final[tuple[str, ...]] = (
    "fallback",
    "kernel_autoreverted",
    "kernel_result_refused",
    "wheel_missing",
    "wheel_stale",
)

#: Grid scales (PROVISIONAL — design §6 open question 1; the operator
#: wall-time probe on the real box ratifies/adjusts these). ``smoke`` is the
#: #1479 minutes-scale shape and is explicitly NOT flip-qualifying (its
#: honest envelope: the kernel term is microscopic at that scale); a flip
#: ADR must cite a ``default`` or ``full`` scale PASS.
GRID_SCALES: Final[dict[str, dict[str, str]]] = {
    "smoke": {
        "symbols": "NIFTY",
        "start": "2024-01-01",
        "end": "2024-03-31",
        "intervals": "1minute,15minute",
        "k": "1,2",
        "top": "1000",
    },
    "default": {
        "symbols": "NIFTY,BANKNIFTY",
        "start": "2025-01-01",
        "end": "2025-12-31",
        "intervals": "1minute,15minute",
        "k": "2,5",
        "top": "1000",
    },
    "full": {
        "symbols": "NIFTY,BANKNIFTY",
        "start": "2024-01-01",
        "end": "2025-12-31",
        "intervals": "1minute,15minute",
        "k": "2,5",
        "top": "1000",
    },
}

#: The one sweep-all twin argv template (SWEEP_TWIN class). Placeholders are
#: rendered by :func:`render_argv`. SENSEX needs ``--exchange BSE`` (a second
#: call) — deliberately out of the v1 grid, stated in GRID_SCALES' notes.
SWEEP_ALL_TWIN_ARGV: Final[tuple[str, ...]] = (
    "brutex",
    "sweep-all",
    "--lake-root",
    "{lake_root}",
    "--results-root",
    "{results_root}",
    "--catalog",
    "{results_root}/catalog.duckdb",
    "--symbols",
    "{symbols}",
    "--start",
    "{start}",
    "--end",
    "{end}",
    "--intervals",
    "{intervals}",
    "--k",
    "{k}",
    "--top",
    "{top}",
)

#: Artifact globs for the SWEEP_TWIN manifest — the #1479 set verbatim
#: (audit ``.jsonl`` EXCLUDED: hash-chained records carry wall-clock
#: timestamps that false-diff byte identity).
SWEEP_ARTIFACT_GLOBS: Final[tuple[str, ...]] = ("*.parquet", "*.trials.json", "*.complete.json")

#: Default soak RSS ceiling — inherit brutex.observability.soak_drift's
#: 4 GiB (kept as a literal here so this module stays importable without the
#: brutex package installed; the driver cross-checks the literal at runtime).
DEFAULT_RSS_CEILING_KIB: Final[int] = 4 * 1024 * 1024

#: The kernel-cache flag's ceiling: the soak-drift default PLUS PR #1464's
#: MODELED 0.66-0.73 GB/worker retention allowance (0.75 GiB, rounded up) —
#: the exact number the soak exists to convert from MODELED to MEASURED.
#: Crossing it means the in-process retention exceeded the modeled claim.
KERNEL_CACHE_RSS_CEILING_KIB: Final[int] = DEFAULT_RSS_CEILING_KIB + 786432


def engaged_marker_for(surface_prefix: str) -> str:
    """The naming-law engaged-marker regex for *surface_prefix*."""
    return rf"{surface_prefix}\.engaged"


def bad_markers_for(
    surface_prefix: str, alternatives: tuple[str, ...] = DEFAULT_BAD_ALTERNATIVES
) -> str:
    """The naming-law bad-marker regex for *surface_prefix* over *alternatives*."""
    return rf"{surface_prefix}\.({'|'.join(alternatives)})"


def divergence_marker_for(surface_prefix: str) -> str:
    """The naming-law divergence-marker regex (DIFFERENTIAL class)."""
    return rf"{surface_prefix}\.divergence"


@dataclass(frozen=True, slots=True)
class FlagSpec:
    """One flag's soak parameters — the harness reads, never hardcodes."""

    flag: str
    flag_class: str
    surface_prefix: str
    engaged_marker: str
    bad_markers: str
    divergence_marker: str
    twin_argv: tuple[str, ...]
    artifact_globs: tuple[str, ...] = SWEEP_ARTIFACT_GLOBS
    extra_env: tuple[tuple[str, str], ...] = (("BRUTEX_RESULTS_DEPTH", "0"),)
    rss_ceiling_kib: int = DEFAULT_RSS_CEILING_KIB
    notes: str = ""

    def __post_init__(self) -> None:
        if self.flag_class not in FLAG_CLASSES:
            raise ValueError(f"unknown flag_class {self.flag_class!r} for flag {self.flag!r}")
        if not self.flag.startswith("BRUTEX_") or self.flag != self.flag.upper():
            raise ValueError(f"flag {self.flag!r} must be an uppercase BRUTEX_* env var name")
        # Naming-law belt (NIT-6): an EMPTY engaged/bad marker would make the
        # engagement gate pass vacuously (grep -Ec of an empty regex matches
        # every line — the FLIP_SOAK_EXTRA_FLAGS test seam could inject it).
        if not self.engaged_marker:
            raise ValueError(f"flag {self.flag!r} has an empty engaged_marker (naming-law belt)")
        if not self.bad_markers:
            raise ValueError(f"flag {self.flag!r} has an empty bad_markers (naming-law belt)")


def _sweep_twin_spec(*, flag: str, surface_prefix: str, **overrides: object) -> FlagSpec:
    """A SWEEP_TWIN row with naming-law markers derived from *surface_prefix*."""
    kwargs: dict[str, object] = {
        "flag": flag,
        "flag_class": SWEEP_TWIN,
        "surface_prefix": surface_prefix,
        "engaged_marker": engaged_marker_for(surface_prefix),
        "bad_markers": bad_markers_for(surface_prefix),
        "divergence_marker": divergence_marker_for(surface_prefix),
        "twin_argv": SWEEP_ALL_TWIN_ARGV,
    }
    kwargs.update(overrides)
    return FlagSpec(**kwargs)  # type: ignore[arg-type]


#: The built-in registry — rows added by each slice PR (reviewed), never here
#: via the env seam. See the module docstring's honest envelope for why both
#: rows currently point at slice-branch flags.
REGISTRY: Final[tuple[FlagSpec, ...]] = (
    _sweep_twin_spec(
        flag="BRUTEX_SWEEP_RUST_ENUM",
        surface_prefix="sweep_rust_enum",
        # PR #1479's script gates on exactly these three alternatives; the
        # wheel_* tokens are folded in additively (a marker the slice never
        # emits can never false-fail — grep -E counts occurrences).
        bad_markers=bad_markers_for("sweep_rust_enum"),
        notes=(
            "PR #1479 (claude/rust-sweep-enum-slice1) — Rust enumeration kernel. "
            "Flag NOT on main; dispatch with ref=<slice branch>."
        ),
    ),
    _sweep_twin_spec(
        flag="BRUTEX_SWEEP_WORKER_KERNEL_CACHE",
        surface_prefix="sweep_worker_kernel_cache",
        rss_ceiling_kib=KERNEL_CACHE_RSS_CEILING_KIB,
        notes=(
            "PR #1464 — per-worker kernel cache. Flag NOT on main; dispatch with "
            "ref=<slice branch>. RSS ceiling = soak-drift default + the MODELED "
            "0.75 GiB/worker retention allowance (PR #1464 honest envelope) — the "
            "soak converts that claim from MODELED to MEASURED. The slice PR must "
            "emit the naming-law tokens (sweep_worker_kernel_cache.engaged / "
            ".fallback / .kernel_autoreverted) per design §1(b)."
        ),
    ),
)


def _extra_specs() -> tuple[FlagSpec, ...]:
    """Rows from the ``FLIP_SOAK_EXTRA_FLAGS`` JSON seam (CI dry-mode tests)."""
    path = os.environ.get(EXTRA_FLAGS_ENV, "")
    if not path:
        return ()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{EXTRA_FLAGS_ENV} file must hold a JSON list of FlagSpec objects")
    specs: list[FlagSpec] = []
    for obj in raw:
        if not isinstance(obj, dict):
            raise ValueError(f"{EXTRA_FLAGS_ENV} rows must be JSON objects, got {type(obj)}")
        prefix = str(obj["surface_prefix"])
        specs.append(
            FlagSpec(
                flag=str(obj["flag"]),
                flag_class=str(obj["flag_class"]),
                surface_prefix=prefix,
                engaged_marker=str(obj.get("engaged_marker", engaged_marker_for(prefix))),
                bad_markers=str(obj.get("bad_markers", bad_markers_for(prefix))),
                divergence_marker=str(obj.get("divergence_marker", divergence_marker_for(prefix))),
                twin_argv=tuple(str(t) for t in obj.get("twin_argv", SWEEP_ALL_TWIN_ARGV)),
                artifact_globs=tuple(
                    str(g) for g in obj.get("artifact_globs", SWEEP_ARTIFACT_GLOBS)
                ),
                extra_env=tuple((str(k), str(v)) for k, v in obj.get("extra_env", [])),
                rss_ceiling_kib=int(obj.get("rss_ceiling_kib", DEFAULT_RSS_CEILING_KIB)),
                notes=str(obj.get("notes", "test-seam row")),
            )
        )
    return tuple(specs)


def all_specs() -> tuple[FlagSpec, ...]:
    """The built-in registry plus any test-seam rows (built-ins first)."""
    return REGISTRY + _extra_specs()


def spec_for(flag: str) -> FlagSpec:
    """Return the spec for *flag* or raise ``KeyError`` naming the known flags."""
    for spec in all_specs():
        if spec.flag == flag:
            return spec
    known = ", ".join(s.flag for s in all_specs())
    raise KeyError(f"unknown flag {flag!r} — registry flags: {known}")


def render_argv(
    spec: FlagSpec, *, lake_root: str, results_root: str, scale: str
) -> tuple[str, ...]:
    """Render the spec's twin argv template for one arm."""
    if scale not in GRID_SCALES:
        raise KeyError(f"unknown grid scale {scale!r} — known: {', '.join(sorted(GRID_SCALES))}")
    values: dict[str, str] = {"lake_root": lake_root, "results_root": results_root}
    values.update(GRID_SCALES[scale])
    return tuple(token.format(**values) for token in spec.twin_argv)


def manifest_lines(spec: FlagSpec, root: Path) -> list[str]:
    """Relative-path sha256 manifest lines over the spec's artifact globs.

    Sorted by relative path; the #1479 shape (``<sha256>  <rel>``). Reads each
    matched file fully — the arms' result trees are scratch-sized, never the
    lake.
    """
    matches: set[Path] = set()
    for glob in spec.artifact_globs:
        matches.update(p for p in root.rglob(glob) if p.is_file())
    lines: list[str] = []
    for path in sorted(matches, key=lambda p: p.relative_to(root).as_posix()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(root).as_posix()}")
    return lines


def _harvest_snapshot_hashes(obj: object, found: set[str]) -> None:
    """Recursively collect every string ``data_snapshot_hash`` value in *obj*."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "data_snapshot_hash" and isinstance(value, str) and value:
                found.add(value)
            else:
                _harvest_snapshot_hashes(value, found)
    elif isinstance(obj, list):
        for value in obj:
            _harvest_snapshot_hashes(value, found)


def snapshot_token(root: Path) -> str:
    """The arm's ``data_snapshot_hash`` token (see module docstring contract)."""
    found: set[str] = set()
    for path in sorted(root.rglob("*.json")):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue  # a non-JSON or unreadable sidecar carries no snapshot claim
        _harvest_snapshot_hashes(payload, found)
    if not found:
        return ""
    if len(found) > 1:
        return "INCONSISTENT"
    return found.pop()


# ── CLI (bash-consumable, line-oriented) ─────────────────────────────


def _field_lines(spec: FlagSpec, fieldname: str) -> list[str]:
    """Render one spec field as CLI output lines."""
    if fieldname == "artifact_globs":
        return list(spec.artifact_globs)
    if fieldname == "extra_env":
        return [f"{k}={v}" for k, v in spec.extra_env]
    if fieldname in {
        "flag_class",
        "surface_prefix",
        "engaged_marker",
        "bad_markers",
        "divergence_marker",
        "rss_ceiling_kib",
    }:
        return [str(getattr(spec, fieldname))]
    raise KeyError(f"unknown field {fieldname!r}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — see the module docstring for the contract."""
    parser = argparse.ArgumentParser(description=__doc__ or "flip-soak flag registry")
    parser.add_argument("--flag", default=None)
    parser.add_argument("--list-flags", action="store_true")
    parser.add_argument("--field", default=None)
    parser.add_argument("--render-twin-argv", action="store_true")
    parser.add_argument("--manifest-root", default=None)
    parser.add_argument("--snapshot-root", default=None)
    parser.add_argument("--lake-root", default="")
    parser.add_argument("--results-root", default="")
    parser.add_argument("--scale", default="default")
    args = parser.parse_args(argv)

    try:
        if args.list_flags:
            for spec in all_specs():
                print(spec.flag)
            return 0
        if args.flag is None:
            print("ERROR: --flag is required for every mode except --list-flags")
            return 2
        spec = spec_for(args.flag)
        if args.field is not None:
            for line in _field_lines(spec, args.field):
                print(line)
            return 0
        if args.render_twin_argv:
            for token in render_argv(
                spec,
                lake_root=args.lake_root,
                results_root=args.results_root,
                scale=args.scale,
            ):
                print(token)
            return 0
        if args.manifest_root is not None:
            for line in manifest_lines(spec, Path(args.manifest_root)):
                print(line)
            return 0
        if args.snapshot_root is not None:
            print(snapshot_token(Path(args.snapshot_root)))
            return 0
    except (KeyError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print("ERROR: no mode selected (see --help)")
    return 2


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    sys.exit(main())
