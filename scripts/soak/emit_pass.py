#!/usr/bin/env python3
"""emit_pass.py — assemble + schema-validate the flip-soak PASS.json artifact.

DEC-FLIP-SOAK-HARNESS-001 (number pending coordinator reservation). The
durable evidence home for a flip-qualifying soak is a COMMITTED transcript
at ``docs/soak/<FLAG>/<YYYY-MM-DD>-PASS.json`` (the
``tests/bench/baselines/<hardware_key>.json`` + ``docs/groww/probe/
settle_evidence.json`` governance precedents — measured-on-the-box numbers
committed via a reviewed PR, schema-validated in CI, citable by a later
flip ADR). PR-body-paste-only evidence is the demonstrated failure mode
(PR #1479's slot still reads *pending*); workflow artifacts expire.

Schema v1 (see ``docs/soak/README.md`` for the field-by-field contract):
top-level keys ``schema_version · flag · verdict · date_utc · git_sha ·
branch · runner_name · hardware · run_url · grid_scale · engagement ·
soak · wall_clock · ambient_flags`` plus EXACTLY ONE of ``twin`` (SWEEP_TWIN
/ SURFACE_GOLDEN classes) or ``differential`` (DIFFERENTIAL class — byte
identity is trivially true BY DESIGN there, so the block records the
zero-divergence counter instead; a deliberate, documented deviation from
the design sketch's ``comparisons`` field — engagement counts prove the
differential ran, the divergence counter is the load-bearing oracle).

Governance laws enforced here (and by the repo test over docs/soak/):

* Only ``verdict: "PASS"`` files are committable — this script REFUSES to
  write a FAIL / RETRY_ENV_MOVED artifact under ``docs/soak/`` (a red soak
  lives in the issue tracker, not the evidence home).
* Provenance is MANDATORY: ``run_url`` (an actions run URL, or the literal
  ``"terminal"`` for the operator one-liner path) + ``git_sha`` + ``branch``
  + ``runner_name`` + a ``hardware`` block. A venue-less PASS is invalid.
* ``ambient_flags`` must record EVERY registry flag (the one-flag-at-a-time
  proof — all values null except the flag under test).

Honest envelope (Golden Rule #10): a determined operator can still fabricate
a PASS file — the control is the reviewed-PR diff + this validator + the
flip-ADR law of citing BOTH the PASS.json path AND the run/transcript, the
same posture as the bench baselines.

Exit codes: 0 = PASS assembled/validated · 1 = FAIL verdict ·
2 = schema/usage error · 3 = RETRY_ENV_MOVED, or a non-PASS artifact
refused under docs/soak/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = 1

VERDICT_PASS = "PASS"  # noqa: S105 — a verdict token, not a credential
VERDICT_FAIL = "FAIL"
VERDICT_RETRY = "RETRY_ENV_MOVED"
VERDICTS = frozenset({VERDICT_PASS, VERDICT_FAIL, VERDICT_RETRY})

#: Top-level keys every PASS.json must carry (plus exactly one of twin /
#: differential — validated structurally below).
REQUIRED_TOP_KEYS = (
    "schema_version",
    "flag",
    "verdict",
    "date_utc",
    "git_sha",
    "branch",
    "runner_name",
    "hardware",
    "run_url",
    "grid_scale",
    "grid",
    "engagement",
    "soak",
    "wall_clock",
    "ambient_flags",
)

#: The evidence home whose contents are PASS-only by law.
EVIDENCE_HOME = Path("docs") / "soak"


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _registry_flags() -> set[str]:
    """The full registry flag set (built-ins + any test-seam rows).

    Imported lazily so the validator stays importable beside the registry
    without the brutex package installed; mirrors :func:`assemble`'s sys.path
    insert. The ``ambient_flags`` one-flag-at-a-time proof (§c) is checked
    against this set.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from flag_registry import all_specs

    return {spec.flag for spec in all_specs()}


def validate_pass_obj(obj: object) -> list[str]:
    """Return schema violations for *obj* (empty list = valid)."""
    errors: list[str] = []
    if not isinstance(obj, dict):
        return [f"top level must be a JSON object, got {type(obj).__name__}"]
    for key in REQUIRED_TOP_KEYS:
        if key not in obj:
            errors.append(f"missing required key {key!r}")
    if errors:
        return errors
    if obj["schema_version"] != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}, got {obj['schema_version']!r}")
    if obj["verdict"] not in VERDICTS:
        errors.append(f"verdict must be one of {sorted(VERDICTS)}, got {obj['verdict']!r}")
    for key in ("flag", "date_utc", "git_sha", "branch", "runner_name", "run_url", "grid_scale"):
        if not _is_nonempty_str(obj[key]):
            errors.append(f"{key} must be a non-empty string (provenance is mandatory)")
    hardware = obj["hardware"]
    if not isinstance(hardware, dict) or not _is_nonempty_str(hardware.get("model")):
        errors.append("hardware must be an object with a non-empty 'model'")
    has_twin = isinstance(obj.get("twin"), dict)
    has_diff = isinstance(obj.get("differential"), dict)
    if has_twin == has_diff:
        errors.append(
            "exactly ONE of 'twin' (SWEEP_TWIN/SURFACE_GOLDEN) or 'differential' "
            "(DIFFERENTIAL) must be present"
        )
    if has_twin:
        twin = obj["twin"]
        for key in ("off_manifest_sha256", "on_manifest_sha256"):
            if not _is_nonempty_str(twin.get(key)):
                errors.append(f"twin.{key} must be a non-empty string")
        if not isinstance(twin.get("files_compared"), int) or twin["files_compared"] < 1:
            errors.append("twin.files_compared must be an int >= 1 (the vacuity guard)")
        if not isinstance(twin.get("byte_identical"), bool):
            errors.append("twin.byte_identical must be a bool")
        if obj["verdict"] == VERDICT_PASS:
            # A byte-identity PASS is self-inconsistent unless the manifests
            # actually matched (a) and the lake-mutation safety rail actually
            # produced a stable snapshot on BOTH arms (b) — otherwise a
            # forged/bit-rotted PASS with byte_identical:false, or one whose
            # snapshot rail silently no-op'd, would validate (MAJOR-1/MAJOR-2).
            if twin.get("byte_identical") is not True:
                errors.append("a PASS twin requires byte_identical == true (self-consistency)")
            snap_off = twin.get("data_snapshot_hash_off")
            snap_on = twin.get("data_snapshot_hash_on")
            if not _is_nonempty_str(snap_off) or not _is_nonempty_str(snap_on):
                errors.append(
                    "a PASS twin requires a non-empty data_snapshot_hash on BOTH arms "
                    "(the lake-mutation safety rail must not silently no-op)"
                )
            elif snap_off != snap_on:
                errors.append("a PASS twin requires equal data_snapshot_hash on both arms")
    if has_diff:
        diff = obj["differential"]
        if not isinstance(diff.get("divergence_count"), int):
            errors.append("differential.divergence_count must be an int")
        elif obj["verdict"] == VERDICT_PASS and diff["divergence_count"] != 0:
            errors.append("a PASS differential must record divergence_count == 0")
    engagement = obj["engagement"]
    if not isinstance(engagement, dict):
        errors.append("engagement must be an object")
    else:
        if not isinstance(engagement.get("engaged_count"), int):
            errors.append("engagement.engaged_count must be an int")
        elif obj["verdict"] == VERDICT_PASS and engagement["engaged_count"] < 1:
            errors.append("a PASS requires engagement.engaged_count >= 1 (never vacuous)")
        if engagement.get("bad_markers_found") != 0 and obj["verdict"] == VERDICT_PASS:
            errors.append("a PASS requires engagement.bad_markers_found == 0 (never masked)")
    soak = obj["soak"]
    if not isinstance(soak, dict):
        errors.append("soak must be an object")
    elif obj["verdict"] == VERDICT_PASS:
        if soak.get("hard_gate_breaches") != 0:
            errors.append("a PASS requires soak.hard_gate_breaches == 0")
        if not isinstance(soak.get("windows"), int) or soak["windows"] < 1:
            errors.append("a PASS requires soak.windows >= 1")
    ambient = obj["ambient_flags"]
    if not isinstance(ambient, dict) or not ambient:
        errors.append("ambient_flags must be a non-empty object recording EVERY registry flag")
    elif obj["verdict"] == VERDICT_PASS:
        # The one-flag-at-a-time proof (§c): ambient_flags must be EXACTLY the
        # registry flag set with EXACTLY ONE non-null value — the flag under
        # test. A stacked/ambient second flag qualifies nothing.
        registry_flags = _registry_flags()
        if set(ambient) != registry_flags:
            errors.append(
                "a PASS ambient_flags must record EXACTLY the registry flag set "
                f"(missing/extra: {sorted(set(ambient) ^ registry_flags)})"
            )
        non_null = {k for k, v in ambient.items() if v is not None}
        if non_null != {obj["flag"]}:
            errors.append(
                "a PASS requires EXACTLY ONE non-null ambient flag equal to the flag under "
                f"test (flag={obj['flag']!r}, non-null={sorted(non_null)})"
            )
    grid = obj.get("grid")
    if not isinstance(grid, dict) or not grid:
        errors.append("grid must be a non-empty object recording the RESOLVED grid dimensions")
    return errors


def _under_evidence_home(path: Path) -> bool:
    """True when *path* resolves under the repo's docs/soak/ evidence home."""
    resolved = path.resolve()
    return any(
        parent.name == EVIDENCE_HOME.name and parent.parent.name == EVIDENCE_HOME.parent.name
        for parent in resolved.parents
    )


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 — fixed constant argv, no shell/user input
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _git_branch() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S607 — fixed constant argv
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def assemble(
    *,
    flag: str,
    twin: dict[str, object],
    drift: dict[str, object] | None,
    run_url: str,
    minutes: str,
    git_sha: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    """Fold the twin + drift fragments into the PASS.json object."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from flag_registry import DIFFERENTIAL, GRID_SCALES, all_specs, spec_for

    spec = spec_for(flag)
    twin_verdict = str(twin.get("verdict", VERDICT_FAIL))
    drift_clean = drift is None or (
        drift.get("hard_gate_breaches") == 0 and drift.get("run_failures") == 0
    )
    if twin_verdict == VERDICT_RETRY:
        verdict = VERDICT_RETRY
    elif twin_verdict == VERDICT_PASS and drift_clean:
        verdict = VERDICT_PASS
    else:
        verdict = VERDICT_FAIL

    soak_block: dict[str, object] = {
        "minutes": float(minutes),
        "windows": 0,
        "rss_peak_kib": None,
        "rss_ceiling_kib": spec.rss_ceiling_kib,
        "fd_baseline": None,
        "fd_final": None,
        "p99_ratio_final": None,
        "hard_gate_breaches": 0,
        "mode": "skipped",
    }
    if drift is not None:
        for key in (
            "windows",
            "rss_peak_kib",
            "rss_ceiling_kib",
            "fd_baseline",
            "fd_final",
            "p99_ratio_final",
            "hard_gate_breaches",
            "mode",
        ):
            if key in drift:
                soak_block[key] = drift[key]

    ambient: dict[str, object] = {s.flag: None for s in all_specs()}
    ambient[flag] = "1"  # the flag under test — the only non-null entry

    scale = str(twin.get("scale", "default"))
    # NIT-1 grid provenance: record the RESOLVED grid dimensions beside the
    # scale NAME so an old evidence file is not ambiguous if GRID_SCALES is
    # later edited (the settle_evidence measured-beside-transcripts posture).
    resolved_grid: dict[str, object] = dict(GRID_SCALES.get(scale, {"scale": scale}))

    obj: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "flag": flag,
        "verdict": verdict,
        "date_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_sha": git_sha or _git_sha(),
        "branch": branch or _git_branch(),
        "runner_name": platform.node() or "unknown",
        "hardware": {
            "model": platform.machine() or "unknown",
            "system": platform.system(),
            "ncpu": os.cpu_count(),
        },
        "run_url": run_url,
        "grid_scale": scale,
        "grid": resolved_grid,
        "engagement": twin.get("engagement", {}),
        "soak": soak_block,
        "wall_clock": twin.get("wall_clock", {}),
        "ambient_flags": ambient,
    }
    if spec.flag_class == DIFFERENTIAL:
        obj["differential"] = twin.get("differential") or {"divergence_count": 0}
    else:
        obj["twin"] = twin.get("twin", {})
    return obj


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — see the module docstring for the contract."""
    parser = argparse.ArgumentParser(description="flip-soak PASS.json assembler/validator")
    parser.add_argument("--validate", default=None, help="validate an existing artifact and exit")
    parser.add_argument("--flag", default=None)
    parser.add_argument("--twin", default=None)
    parser.add_argument("--drift", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--run-url", default="terminal")
    parser.add_argument("--minutes", default="0")
    parser.add_argument("--git-sha", default=None)
    parser.add_argument("--branch", default=None)
    args = parser.parse_args(argv)

    if args.validate is not None:
        path = Path(args.validate)
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"INVALID: {path}: unreadable/malformed JSON: {exc}")
            return 2
        errors = validate_pass_obj(obj)
        if (
            _under_evidence_home(path)
            and isinstance(obj, dict)
            and obj.get("verdict") != VERDICT_PASS
        ):
            errors.append(
                "docs/soak/ is PASS-only by law — a FAIL/RETRY artifact lives in the "
                "issue tracker, never the evidence home"
            )
        if errors:
            for err in errors:
                print(f"INVALID: {path}: {err}")
            return 2
        print(f"VALID: {path}")
        return 0

    if args.flag is None or args.twin is None or args.out is None:
        print("ERROR: --flag, --twin and --out are required (or use --validate)")
        return 2
    try:
        twin = json.loads(Path(args.twin).read_text(encoding="utf-8"))
        drift = (
            json.loads(Path(args.drift).read_text(encoding="utf-8"))
            if args.drift is not None
            else None
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"ERROR: unreadable fragment: {exc}")
        return 2

    obj = assemble(
        flag=args.flag,
        twin=twin,
        drift=drift,
        run_url=args.run_url,
        minutes=args.minutes,
        git_sha=args.git_sha,
        branch=args.branch,
    )
    errors = validate_pass_obj(obj)
    if errors:
        for err in errors:
            print(f"ERROR: assembled artifact invalid: {err}")
        return 2

    out_path = Path(args.out)
    verdict = str(obj["verdict"])
    if verdict != VERDICT_PASS and _under_evidence_home(out_path):
        print(
            f"REFUSED: verdict {verdict} may not be written under docs/soak/ — "
            "the evidence home is PASS-only by law"
        )
        return 3
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"FLIP-SOAK {verdict}")
    if verdict == VERDICT_PASS:
        return 0
    return 3 if verdict == VERDICT_RETRY else 1


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    sys.exit(main())
