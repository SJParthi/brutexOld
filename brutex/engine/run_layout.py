"""The single-source run-directory layout leaf (DEC-SWEEP-UNKILLABLE-CORE-001).

Coordinator Ruling 4 / Register Row 4 + Row 15: ONE place owns the sweep
run's on-disk layout so every observability + engine module derives its
artifact paths identically, the eventual physical relocation is a ONE-constant
change, and the R40 forensics can be mirrored off the results SSD.

The run's artifacts (flight-recorder.jsonl · manifest.json · exit-status.json ·
verdicts.json · chain-map.json · diag bundle · stage2 sidecar · the G12
crash-loop counter) all live under :func:`run_dir`. ``run_id`` is
``grid_identity(grid)`` (BLAKE3, timestamp-free, D-1) so a resume of the SAME
grid lands in the SAME directory.

HONEST ENVELOPE (Golden Rule #10): the physical subdir is still ``runs/`` today
(:data:`RUN_DIR_SUBDIR`). Coordinator Ruling 4 wants the eventual
``sweep_runs/`` SIBLING root that P3's ``trade_store.sweep_run_dir`` will ship —
a JOINT P2/P3 layout that lands at merge-integration once P3's seam is on the
branch (P3 is NOT merged here, so unilaterally guessing its exact path would
risk a fresh mismatch). This leaf makes that relocation a one-line change here.
The ``**/*.parquet`` glob hazard the sibling-root prevents is ALREADY pinned
dead independently: the run core writes only JSON/JSONL/extensionless artifacts
under :func:`run_dir` (``test_runcore_writes_no_parquet_under_runs_dir``), so
the ``/results`` glob is structurally untouched wherever the subdir sits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

# DEC-RUST-SWEEP-OPS-001 (slice 1): the default-OFF Rust routing facade. The
# wired functions gate on `sweep_ops_rust_enabled()` FIRST so the flag-OFF
# default path is byte-identical (the Python law below is the live oracle).
from brutex.engine.sweep_ops_rust import routed_run_path, sweep_ops_rust_enabled

__all__ = [
    "MIRROR_ROOT_ENV",
    "RUN_DIR_SUBDIR",
    "mirror_run_dir",
    "run_dir",
]

#: The results-root subdirectory hosting every per-run artifact. The SINGLE
#: source of the run-layout root (Ruling 4). Relocation to the ``sweep_runs/``
#: sibling root (the joint P2/P3 target) is a change of THIS one constant at
#: merge-integration — see the module docstring's honest envelope.
RUN_DIR_SUBDIR: Final[str] = "runs"

#: Optional env override naming the INTERNAL-disk mirror root (Row 15). Defaults
#: to ``~/.brutex/runs`` — the ``~/.brutex/logs`` precedent. Set empty to DISABLE
#: mirroring (a box with no writable home / a test that wants no home writes).
MIRROR_ROOT_ENV: Final[str] = "BRUTEX_RUN_MIRROR_ROOT"


def run_dir(results_root: Path, run_id: str) -> Path:
    """Return the run's artifact directory — ``<results_root>/<RUN_DIR_SUBDIR>/<run_id>``.

    Parameters
    ----------
    results_root
        The sweep results root.
    run_id
        The run identity (``grid_identity`` BLAKE3 hex, D-1).

    Returns
    -------
    Path
        The single directory every per-run artifact lives under.
    """
    if sweep_ops_rust_enabled():
        return routed_run_path(
            str(results_root),
            (RUN_DIR_SUBDIR, run_id),
            python_law=lambda: _run_dir_python(results_root, run_id),
        )
    return _run_dir_python(results_root, run_id)


def _run_dir_python(results_root: Path, run_id: str) -> Path:
    """The verbatim Python ``run_dir`` law — the live parity oracle
    (DEC-RUST-SWEEP-OPS-001; retained until the final slice's default flip)."""
    return results_root / RUN_DIR_SUBDIR / run_id


def mirror_run_dir(run_id: str, *, env: dict[str, str] | None = None) -> Path | None:
    """Return the INTERNAL-disk forensics mirror dir for *run_id*, or ``None``.

    Coordinator Ruling 15 / Register Row 15: the flight recorder + diagnostic
    bundle exist to diagnose an SSD unplug/death, so their sole copy must NOT
    live only on that same results SSD. This resolves the INTERNAL-disk mirror
    root — ``$BRUTEX_RUN_MIRROR_ROOT`` when set (empty ⇒ mirroring DISABLED,
    returns ``None``), else ``~/.brutex/runs`` (the ``~/.brutex/logs``
    precedent). Returns ``None`` when the home directory cannot be resolved
    (``RuntimeError`` from :meth:`Path.home`) so a headless/home-less box never
    crashes — the results-root copy is always written regardless (the mirror is
    a best-effort DUPLICATE, never the primary, I-3).

    Parameters
    ----------
    run_id
        The run identity (D-1).
    env
        Environment mapping (defaults to :data:`os.environ`); injected in tests.

    Returns
    -------
    Path | None
        The mirror directory, or ``None`` when mirroring is disabled/unavailable.
    """
    import os

    environ = os.environ if env is None else env
    override = environ.get(MIRROR_ROOT_ENV)
    if override is not None:
        stripped = override.strip()
        if not stripped:
            return None  # explicitly disabled
        if sweep_ops_rust_enabled():
            return routed_run_path(
                stripped,
                (run_id,),
                python_law=lambda: Path(stripped) / run_id,
            )
        return Path(stripped) / run_id
    try:
        home = Path.home()
    except RuntimeError:
        return None
    if sweep_ops_rust_enabled():
        return routed_run_path(
            str(home),
            (".brutex", "runs", run_id),
            python_law=lambda: home / ".brutex" / "runs" / run_id,
        )
    return home / ".brutex" / "runs" / run_id
