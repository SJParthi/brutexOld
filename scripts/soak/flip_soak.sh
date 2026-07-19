#!/usr/bin/env bash
# flip_soak.sh — the parameterized flip-soak TWIN-LEG orchestrator.
#
# DEC-FLIP-SOAK-HARNESS-001 (MIGRATION.md #1894). ONE
# flag-agnostic generalization of PR #1479's rust_enum_twin_run.sh: OFF arm
# then ON arm through the flag's registered twin command into two scratch
# results roots, relative-path sha256 manifests over the flag's artifact
# globs (audit .jsonl excluded — wall-clock timestamps), the ENGAGEMENT GATE
# (the ON arm's teed log must contain the flag's `<surface>.engaged` marker
# and ZERO bad-marker matches, BRUTEX_LOG_LEVEL=INFO pinned on both arms),
# the vacuity guard (a zero-artifact OFF arm is FAIL, never a hollow PASS),
# the data_snapshot_hash equality check (a moved lake is RETRY_ENV_MOVED,
# never a kernel FAIL), wall-clock A/B, then the in-process drift leg
# (flip_soak_driver.py) and the PASS.json fold (emit_pass.py).
#
# Usage (the operator terminal one-liner — the workflow calls the same):
#   bash scripts/soak/flip_soak.sh <FLAG> <LAKE_ROOT> \
#       [--minutes N] [--scale smoke|default|full] [--out-dir D] \
#       [--run-url URL] [--skip-drift] [--drift-dry]
#
# Refusal rails (all inherited from the precedents, generalized):
#   * ANY registry flag pre-set in the ambient env is refused — the
#     one-flag-at-a-time law made mechanical (a soak of X with Y ambiently
#     ON qualifies nothing; PR #1479's single-flag refusal, generalized).
#   * The lake root must exist (read-only source; all writes are scratch).
#
# Exit codes (test-pinned):
#   0 PASS · 1 arm/drift execution error · 2 refusal (usage / preset env /
#   missing lake / --minutes < 1) · 3 RETRY_ENV_MOVED (data_snapshot_hash
#   moved between/during arms, OR the drift leg hit a transient env move) ·
#   4 engagement gate (marker absent OR a bad marker matched — the masked-PASS
#   class) · 5 vacuous OFF arm (zero artifacts) · 6 byte-identity FAIL
#   (SWEEP_TWIN / SURFACE_GOLDEN) · 7 divergence FAIL (DIFFERENTIAL) ·
#   8 drift-leg hard-gate FAIL · 9 snapshot_unavailable (a byte-identity PASS
#   needs a non-empty data_snapshot_hash on BOTH arms — the safety rail may
#   not silently no-op).
#
# Test seams (CI dry mode — the fake-lake stub-CLI idiom from #1479 round 2):
#   FLIP_SOAK_ARM_CMD   — a shell command run per arm instead of the
#                         registry twin argv; sees FLIP_SOAK_ARM=off|on,
#                         FLIP_SOAK_RESULTS_ROOT, FLIP_SOAK_LAKE_ROOT,
#                         FLIP_SOAK_SCALE. Its output IS the arm log.
#   FLIP_SOAK_EXTRA_FLAGS — extra registry rows (see flag_registry.py).
#   FLIP_SOAK_PYTHON    — python interpreter (default: python3).
#
# macOS bash-3.2-compatible (the operator box ships 3.2): no readarray, no
# associative arrays, `${arr[@]+"${arr[@]}"}` for possibly-empty arrays.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="${FLIP_SOAK_PYTHON:-python3}"
REG="$SCRIPT_DIR/flag_registry.py"

usage() {
    sed -n '2,35p' "$0" >&2
}

if [ "$#" -lt 2 ]; then
    usage
    echo "ERROR: <FLAG> and <LAKE_ROOT> are required" >&2
    exit 2
fi

FLAG="$1"
LAKE_ROOT="$2"
shift 2

MINUTES="30"
SCALE="default"
OUT_DIR=""
RUN_URL="terminal"
SKIP_DRIFT="0"
DRIFT_DRY="0"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --minutes) MINUTES="$2"; shift 2 ;;
        --scale) SCALE="$2"; shift 2 ;;
        --out-dir) OUT_DIR="$2"; shift 2 ;;
        --run-url) RUN_URL="$2"; shift 2 ;;
        --skip-drift) SKIP_DRIFT="1"; shift ;;
        --drift-dry) DRIFT_DRY="1"; shift ;;
        *)
            echo "ERROR: unknown argument '$1'" >&2
            exit 2
            ;;
    esac
done

# Minimum-soak guard (MINOR-2): a real drift leg needs a >= 1 minute budget —
# the workflow plan job already refuses < 1 (a zero-minute dispatch fake-greens
# a soak that soaked nothing), so the terminal one-liner path matches. The
# CI-only --drift-dry seam is exempt (its synthetic windows finish in ms).
if [ "$DRIFT_DRY" != "1" ]; then
    if ! "$PY" -c 'import sys; sys.exit(0 if float(sys.argv[1]) >= 1 else 1)' "$MINUTES" \
        2>/dev/null; then
        echo "ERROR: --minutes must be >= 1 for a real drift leg (got '$MINUTES'); a" >&2
        echo "sub-minute soak proves nothing — use --drift-dry for a CI smoke run." >&2
        exit 2
    fi
fi

# ── Registry lookups (data, never hardcoded) ─────────────────────────
ALL_FLAGS="$("$PY" "$REG" --list-flags)"
FLAG_CLASS="$("$PY" "$REG" --flag "$FLAG" --field flag_class)" || {
    echo "ERROR: flag '$FLAG' is not in the registry" >&2
    exit 2
}
ENGAGED_RE="$("$PY" "$REG" --flag "$FLAG" --field engaged_marker)"
BAD_RE="$("$PY" "$REG" --flag "$FLAG" --field bad_markers)"
DIVERGENCE_RE="$("$PY" "$REG" --flag "$FLAG" --field divergence_marker)"

# ── Refusal rails ────────────────────────────────────────────────────
# One-flag-at-a-time law: refuse when ANY registry flag is pre-set — not
# just the flag under test (generalizes #1479's own-flag refusal; a stacked
# ambient flag would qualify NOTHING and PASS.json must record a clean map).
for f in $ALL_FLAGS; do
    if [ -n "${!f:-}" ]; then
        echo "REFUSED: registry flag $f is pre-set in the ambient environment —" >&2
        echo "the one-flag-at-a-time law requires a clean env (unset it and rerun)." >&2
        exit 2
    fi
done

if [ ! -d "$LAKE_ROOT" ]; then
    echo "REFUSED: lake root '$LAKE_ROOT' is not a directory" >&2
    exit 2
fi

if [ -z "$OUT_DIR" ]; then
    OUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/flip-soak.XXXXXX")"
fi
mkdir -p "$OUT_DIR/off-root" "$OUT_DIR/on-root"
OFF_ROOT="$OUT_DIR/off-root"
ON_ROOT="$OUT_DIR/on-root"
OFF_LOG="$OUT_DIR/off.log"
ON_LOG="$OUT_DIR/on.log"
echo "flip-soak: flag=$FLAG class=$FLAG_CLASS scale=$SCALE out=$OUT_DIR"

# ── Arm runner ───────────────────────────────────────────────────────
# Each arm runs with a PROCESS-CLEAN flag env (every registry flag unset,
# then FLAG=1 for the ON arm only — matching how production flips behave),
# the flag's extra_env false-diff suppressors, and BRUTEX_LOG_LEVEL=INFO
# pinned so the engagement markers are visible on both arms.
unset_args=()
for f in $ALL_FLAGS; do
    unset_args+=(-u "$f")
done

extra_env_args=()
while IFS= read -r kv; do
    [ -n "$kv" ] && extra_env_args+=("$kv")
done <<EOF
$("$PY" "$REG" --flag "$FLAG" --field extra_env)
EOF

now_s() {
    "$PY" -c 'import time; print(f"{time.time():.3f}")'
}

run_arm() {
    # $1 = off|on   $2 = results root   $3 = log file
    arm="$1"
    results_root="$2"
    log="$3"
    arm_env=("BRUTEX_LOG_LEVEL=INFO")
    if [ "$arm" = "on" ]; then
        arm_env+=("$FLAG=1")
    fi
    if [ -n "${FLIP_SOAK_ARM_CMD:-}" ]; then
        # DRY/fake-lake test seam: the stub CLI emits configurable markers +
        # artifacts (the #1479 round-2 TestTwinRunEngagementGate idiom).
        env "${unset_args[@]}" \
            ${extra_env_args[@]+"${extra_env_args[@]}"} \
            "${arm_env[@]}" \
            FLIP_SOAK_ARM="$arm" \
            FLIP_SOAK_RESULTS_ROOT="$results_root" \
            FLIP_SOAK_LAKE_ROOT="$LAKE_ROOT" \
            FLIP_SOAK_SCALE="$SCALE" \
            sh -c "$FLIP_SOAK_ARM_CMD" 2>&1 | tee "$log"
        return
    fi
    # Production path: render the registry twin argv and execute it.
    argv_file="$OUT_DIR/argv-$arm.txt"
    "$PY" "$REG" --flag "$FLAG" --render-twin-argv \
        --lake-root "$LAKE_ROOT" --results-root "$results_root" \
        --scale "$SCALE" > "$argv_file"
    argv=()
    while IFS= read -r tok; do
        argv+=("$tok")
    done < "$argv_file"
    env "${unset_args[@]}" \
        ${extra_env_args[@]+"${extra_env_args[@]}"} \
        "${arm_env[@]}" \
        "${argv[@]}" 2>&1 | tee "$log"
}

echo "flip-soak: OFF arm starting"
t0="$(now_s)"
# MINOR-1: capture the arm pipeline's status explicitly. Under `set -e` +
# `pipefail` a crashing sweep would otherwise abort the whole script with the
# child's RAW exit code (mis-read as a refusal/2); the `||` guard converts any
# arm failure into the harness's documented arm-error exit 1.
run_arm off "$OFF_ROOT" "$OFF_LOG" || {
    echo "ERROR: OFF arm execution failed (see $OFF_LOG)" >&2
    exit 1
}
t1="$(now_s)"
echo "flip-soak: ON arm starting"
run_arm on "$ON_ROOT" "$ON_LOG" || {
    echo "ERROR: ON arm execution failed (see $ON_LOG)" >&2
    exit 1
}
t2="$(now_s)"
OFF_WALL_S="$("$PY" -c "print(f'{$t1 - $t0:.3f}')")"
ON_WALL_S="$("$PY" -c "print(f'{$t2 - $t1:.3f}')")"

write_twin_fragment() {
    # $1 = verdict  $2 = detail
    TWIN_VERDICT="$1" TWIN_DETAIL="$2" TWIN_FLAG="$FLAG" \
    TWIN_CLASS="$FLAG_CLASS" TWIN_SCALE="$SCALE" \
    TWIN_ENGAGED_COUNT="${ENGAGED_COUNT:-0}" TWIN_BAD_COUNT="${BAD_COUNT:-0}" \
    TWIN_OFF_MANIFEST_SHA="${OFF_MANIFEST_SHA:-}" \
    TWIN_ON_MANIFEST_SHA="${ON_MANIFEST_SHA:-}" \
    TWIN_FILES_COMPARED="${FILES_COMPARED:-0}" \
    TWIN_BYTE_IDENTICAL="${BYTE_IDENTICAL:-false}" \
    TWIN_SNAP_OFF="${SNAP_OFF:-}" TWIN_SNAP_ON="${SNAP_ON:-}" \
    TWIN_SNAP_PRESENT="${SNAP_PRESENT:-false}" \
    TWIN_DIVERGENCE_COUNT="${DIVERGENCE_COUNT:-}" \
    TWIN_OFF_WALL_S="$OFF_WALL_S" TWIN_ON_WALL_S="$ON_WALL_S" \
    "$PY" - "$OUT_DIR/twin.json" <<'PYEOF'
import json, os, sys

def _int(name: str) -> int:
    return int(os.environ.get(name, "0") or "0")

divergence = os.environ.get("TWIN_DIVERGENCE_COUNT", "")
off_wall = float(os.environ["TWIN_OFF_WALL_S"])
on_wall = float(os.environ["TWIN_ON_WALL_S"])
fragment = {
    "flag": os.environ["TWIN_FLAG"],
    "flag_class": os.environ["TWIN_CLASS"],
    "scale": os.environ["TWIN_SCALE"],
    "verdict": os.environ["TWIN_VERDICT"],
    "detail": os.environ["TWIN_DETAIL"],
    "engagement": {
        "engaged_count": _int("TWIN_ENGAGED_COUNT"),
        "bad_markers_found": _int("TWIN_BAD_COUNT"),
    },
    "twin": {
        "off_manifest_sha256": os.environ.get("TWIN_OFF_MANIFEST_SHA", ""),
        "on_manifest_sha256": os.environ.get("TWIN_ON_MANIFEST_SHA", ""),
        "files_compared": _int("TWIN_FILES_COMPARED"),
        "byte_identical": os.environ.get("TWIN_BYTE_IDENTICAL", "false") == "true",
        "data_snapshot_hash_off": os.environ.get("TWIN_SNAP_OFF", ""),
        "data_snapshot_hash_on": os.environ.get("TWIN_SNAP_ON", ""),
        "snapshot_present": os.environ.get("TWIN_SNAP_PRESENT", "false") == "true",
    },
    "differential": None if divergence == "" else {"divergence_count": int(divergence)},
    "wall_clock": {
        "off_s": off_wall,
        "on_s": on_wall,
        "ratio": (on_wall / off_wall) if off_wall > 0 else None,
    },
}
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(fragment, fh, indent=2, sort_keys=True)
    fh.write("\n")
PYEOF
}

# ── Engagement gate (the #1479 law, generalized) ─────────────────────
ENGAGED_COUNT="$(grep -Ec "$ENGAGED_RE" "$ON_LOG" || true)"
BAD_COUNT="$(grep -Ec "$BAD_RE" "$ON_LOG" || true)"
if [ "$BAD_COUNT" -gt 0 ]; then
    write_twin_fragment FAIL "masked PASS: $BAD_COUNT bad-marker match(es) ($BAD_RE) in the ON-arm log"
    echo "FLIP-SOAK TWIN FAIL (engagement: bad markers present — the ON arm fell back / autoreverted)"
    exit 4
fi
if [ "$ENGAGED_COUNT" -lt 1 ]; then
    write_twin_fragment FAIL "no engagement: 0 matches of $ENGAGED_RE in the ON-arm log (byte identity would be VACUOUS)"
    echo "FLIP-SOAK TWIN FAIL (engagement: engaged marker absent)"
    exit 4
fi

# ── Manifests + vacuity guard ────────────────────────────────────────
OFF_MANIFEST="$OUT_DIR/off.manifest"
ON_MANIFEST="$OUT_DIR/on.manifest"
"$PY" "$REG" --flag "$FLAG" --manifest-root "$OFF_ROOT" > "$OFF_MANIFEST"
"$PY" "$REG" --flag "$FLAG" --manifest-root "$ON_ROOT" > "$ON_MANIFEST"
FILES_COMPARED="$(grep -c . "$OFF_MANIFEST" || true)"
if [ "$FILES_COMPARED" -lt 1 ]; then
    write_twin_fragment FAIL "vacuous: the OFF arm produced ZERO artifacts matching the registry globs"
    echo "FLIP-SOAK TWIN FAIL (vacuous OFF arm — zero artifacts, nothing was compared)"
    exit 5
fi
OFF_MANIFEST_SHA="$("$PY" -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$OFF_MANIFEST")"
ON_MANIFEST_SHA="$("$PY" -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$ON_MANIFEST")"
if cmp -s "$OFF_MANIFEST" "$ON_MANIFEST"; then
    BYTE_IDENTICAL="true"
else
    BYTE_IDENTICAL="false"
fi

# ── data_snapshot_hash equality (lake-mutation rail) ─────────────────
SNAP_OFF="$("$PY" "$REG" --flag "$FLAG" --snapshot-root "$OFF_ROOT")"
SNAP_ON="$("$PY" "$REG" --flag "$FLAG" --snapshot-root "$ON_ROOT")"
if [ "$SNAP_OFF" = "INCONSISTENT" ] || [ "$SNAP_ON" = "INCONSISTENT" ] || {
    [ -n "$SNAP_OFF" ] && [ -n "$SNAP_ON" ] && [ "$SNAP_OFF" != "$SNAP_ON" ];
}; then
    write_twin_fragment RETRY_ENV_MOVED "data_snapshot_hash moved (off=$SNAP_OFF on=$SNAP_ON) — the lake changed between/during arms; rerun outside the nightly window"
    echo "FLIP-SOAK RETRY_ENV_MOVED (data_snapshot_hash moved — not a kernel verdict)"
    exit 3
fi

# snapshot_present (MAJOR-2): BOTH arms produced a non-empty data_snapshot_hash.
# The rail can only equality-gate a lake move when it actually harvested a
# snapshot; an empty snapshot means the rail SILENTLY no-op'd, so a byte-identity
# PASS built on it is unsound — refuse below (exit 9) rather than emit a PASS.
SNAP_PRESENT="false"
if [ -n "$SNAP_OFF" ] && [ -n "$SNAP_ON" ]; then
    SNAP_PRESENT="true"
fi

# ── Class verdict ────────────────────────────────────────────────────
DIVERGENCE_COUNT=""
if [ "$FLAG_CLASS" = "DIFFERENTIAL" ]; then
    # Byte identity is trivially true BY DESIGN for this class (oracle bits
    # ship) — the load-bearing oracle is the zero-divergence counter, so the
    # snapshot-present rail is N/A here (mirrors emit_pass: no twin block).
    DIVERGENCE_COUNT="$(grep -Ec "$DIVERGENCE_RE" "$ON_LOG" || true)"
    if [ "$DIVERGENCE_COUNT" -gt 0 ]; then
        write_twin_fragment FAIL "differential: $DIVERGENCE_COUNT divergence marker(s) ($DIVERGENCE_RE) in the ON-arm log"
        echo "FLIP-SOAK TWIN FAIL (differential divergences: $DIVERGENCE_COUNT)"
        exit 7
    fi
else
    if [ "$BYTE_IDENTICAL" != "true" ]; then
        write_twin_fragment FAIL "byte identity: manifests differ (off=$OFF_MANIFEST_SHA on=$ON_MANIFEST_SHA over $FILES_COMPARED file(s))"
        echo "FLIP-SOAK TWIN FAIL (manifests differ — diff $OFF_MANIFEST $ON_MANIFEST)"
        exit 6
    fi
    if [ "$SNAP_PRESENT" != "true" ]; then
        write_twin_fragment FAIL "snapshot_unavailable: a byte-identity PASS requires a non-empty data_snapshot_hash on BOTH arms (off='$SNAP_OFF' on='$SNAP_ON') — the lake-mutation safety rail cannot silently no-op"
        echo "FLIP-SOAK TWIN FAIL (snapshot_unavailable — no data_snapshot_hash on one/both arms; the safety rail would silently vanish)"
        exit 9
    fi
fi

write_twin_fragment PASS "twin leg clean"
echo "FLIP-SOAK TWIN PASS (files=$FILES_COMPARED engaged=$ENGAGED_COUNT off=${OFF_WALL_S}s on=${ON_WALL_S}s)"

# ── Drift leg (in-process driver) + PASS.json fold ───────────────────
if [ "$SKIP_DRIFT" = "1" ]; then
    # Twin-only run: NOT flip-qualifying alone (a PASS.json requires the
    # drift leg's windows), so no PASS artifact is assembled — the twin
    # fragment + manifests under $OUT_DIR are the run's output.
    echo "FLIP-SOAK TWIN-ONLY (drift leg skipped — no PASS.json; not flip-qualifying alone)"
    exit 0
fi

DRIFT_RC=0
drift_cmd=("$PY" "$SCRIPT_DIR/flip_soak_driver.py"
    --flag "$FLAG" --minutes "$MINUTES" --lake-root "$LAKE_ROOT"
    --results-root "$OUT_DIR/drift" --scale "$SCALE"
    --out "$OUT_DIR/drift.json")
if [ "$DRIFT_DRY" = "1" ]; then
    drift_cmd+=(--dry)
fi
"${drift_cmd[@]}" || DRIFT_RC=$?
# rc 1 = a genuine hard-gate breach / kernel failure → FAIL (exit 8). rc 3 =
# RETRY_ENV_MOVED (the lake moved under the drift leg) → NOT a kernel FAIL:
# fold it through emit_pass, which computes the final RETRY verdict + exit 3.
if [ "$DRIFT_RC" -ne 0 ] && [ "$DRIFT_RC" -ne 3 ]; then
    echo "FLIP-SOAK FAIL (drift leg hard-gate breach or run error — see $OUT_DIR/drift.json)"
    exit 8
fi

"$PY" "$SCRIPT_DIR/emit_pass.py" \
    --flag "$FLAG" \
    --twin "$OUT_DIR/twin.json" \
    --drift "$OUT_DIR/drift.json" \
    --run-url "$RUN_URL" \
    --minutes "$MINUTES" \
    --out "$OUT_DIR/PASS.json"
