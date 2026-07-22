#!/usr/bin/env bash
# up.sh — BruteX ONE-COMMAND bootstrap: ANY state → a RUNNING dashboard, idempotently.
#
# DEC-ONE-COMMAND-UP-001 (docs/MIGRATION.md). `./up.sh` takes the operator from ANY
# starting state — a fresh clone, a tenth clone, a months-stale checkout, a
# half-broken .venv — to a running dashboard ready to pull:
#
#   FAST PATH — a dashboard already running (live PID in ~/.brutex/runtime.json)
#               prints "already ready" + the URL and exits 0 in under a second.
#   Step 1/6  — find python3.12 (exact per-OS install hint if absent); create or
#               SELF-HEAL the .venv (a broken/stale/wrong-python venv is rebuilt —
#               ONLY the in-repo .venv build artifact is ever removed; user data
#               under ~/.brutex and the lake are NEVER touched).
#   Step 2/6  — pip install -e ".[dev,data,ui,groww,token-broker]" when the import
#               probe fails; then verify the pull-critical deps (boto3 / growwapi /
#               pyotp + the dashboard runtime), one PASS/FAIL line each with the
#               exact fix.
#   Step 3/6  — build the REQUIRED PyO3 wheels via maturin when not importable:
#               fno_math + trading_core (brutex.indicators.* / brutex.strategies
#               .combo import them at module top) + brutex_identity
#               (DEC-RUST-RUNID-STORES-001 — module-top import on the CLI/
#               dashboard boot chain via engine/results_store.py) + brutex_refine
#               (Stage-3 is Rust-only, DEC-RUST-STAGE3-REFINE-001) +
#               brutex_pull_state (the pull-state decision plane is Rust-only,
#               fail-closed). Required-wheels-missing + cargo-absent is an HONEST
#               hard stop with the rustup one-liner, not a silent skip. The
#               brutex_feed + brutex_lake wheels build BEST-EFFORT (warn-only —
#               mirrors scripts/setup/bootstrap.sh Steps 6a/6a3).
#   Step 4/6  — ZERO-DECISION lake location: a persisted config is reused
#               silently; else an EXISTING lake is auto-detected (the app
#               default ~/.brutex/lake, or an environment-pointed path) and
#               recorded permanently; else the default is created and recorded.
#               The operator NEVER types a path. The auto-resume gates are
#               recorded the same way — the legacy sub-gate pre-armed
#               (BRUTEX_AUTO_RESUME_ON_BOOT=1) and the BRUTEX_AUTO_RESUME
#               master opt-in DOCUMENTED as a commented line, never armed
#               (boot auto-resume is OFF by default,
#               DEC-AUTO-RESUME-DEFAULT-OFF-001) — all in ~/.brutex/.env
#               (brutex loads that file at every boot via
#               brutex/config/dotenv.py). No per-session exports ever again.
#   Step 5/6  — self-check chain, each line PASS or exact-cause+fix, never a stack
#               trace: deps importable → AWS identity (sts get-caller-identity via
#               boto3) → Groww daily access token READABLE from AWS SSM (read-only
#               GetParameter — this script NEVER mints a token) → lake root
#               writable. AWS/SSM failures DEGRADE honestly (the dashboard still
#               starts; a live pull will fail until fixed); an unwritable lake
#               root is a hard stop.
#   Step 6/6  — hand off to the existing one-click entry
#               `.venv/bin/python -m brutex run` (DEC-ONECLICK-RUN-001), which
#               prints the URL + the rotating log-file location, opens the
#               browser, and boots the dashboard at http://127.0.0.1:9080.
#
# THIS SCRIPT IS THE ONE ENTRY POINT (DEC-ONE-ENTRY-UNIFY-001): `make run`,
# `./up.sh`, and the committed IntelliJ ▶ "BruteX" run configuration ALL
# execute this same self-check → self-heal → start path — the operator never
# chooses between entries. (`make run-raw` keeps the raw direct
# `python -m brutex run` chain for CI reproduction / debugging.)
#
# Idempotent + re-run-safe: every step probes before it mutates, so a re-run with
# everything ready reaches "already ready" / the dashboard in seconds — including
# a re-run mid-setup after a Ctrl-C. Written for bash 3.2 (the macOS default):
# no arrays, no mapfile/readarray, no associative arrays, no ${var,,}.
#
# Flags:
#   --no-start           do everything except booting the dashboard (CI / cron)
#   --print-lake-detect  run ONLY the zero-decision lake detection + recording,
#                        print LAKE_DECISION=<source> <path> datasets=<n>, exit
#                        (internal — exercised by tests/deploy hermetically)
#   -h, --help           print usage
set -u

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"

VENV="$REPO_ROOT/.venv"
VPY="$VENV/bin/python"
BRUTEX_HOME="${HOME}/.brutex"
CONF="$BRUTEX_HOME/.env"
EXTRAS=".[dev,data,ui,groww,token-broker]"
DASHBOARD_URL="http://127.0.0.1:9080"
DEGRADED=0
NO_START=0

usage() {
  cat <<'EOF'
usage: ./up.sh [--no-start]

One-command BruteX bootstrap (DEC-ONE-COMMAND-UP-001): from ANY state (fresh
clone, stale checkout, half-broken venv) to a RUNNING dashboard ready to pull.
Idempotent — re-run any time; when everything is already up it prints
"already ready" and exits in seconds. Zero decisions: the lake location is
auto-detected and recorded — you never type a path.

  --no-start           run every setup + self-check step, do not boot the server
  --print-lake-detect  internal: run only the lake detection (used by tests)
  --link-logs          internal: run only the <repo>/logs symlink refresh (tests)
  -h, --help           this message
EOF
}

PRINT_LAKE_DETECT=0
LINK_LOGS_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-start) NO_START=1 ;;
    --print-lake-detect) PRINT_LAKE_DETECT=1 ;;
    --link-logs) LINK_LOGS_ONLY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'up.sh: unknown flag %s (supported: --no-start, --help)\n' "$1" >&2; exit 1 ;;
  esac
  shift
done

# ── helpers (loud, precise; a hard failure always exits non-zero) ────────────
say()     { printf '\n==> %s\n' "$*"; }
note()    { printf '        %s\n' "$*"; }
pass()    { printf '  PASS  %s\n' "$*"; }
degrade() { printf '  FAIL  %s\n' "$*" >&2; DEGRADED=1; }
failhard() {
  printf '  FAIL  %s\n' "$*" >&2
  printf 'up.sh: cannot continue — apply the fix above, then re-run ./up.sh\n' >&2
  exit 1
}

# ── zero-decision lake detection (the operator NEVER types a path) ───────────
# "Real data" = at least one Parquet partition under <root>/bars (the lake
# layout bars/<exch>/<segment>/<symbol>/<interval>/<yyyy>/<mm>.parquet).
lake_has_data() {
  [ -d "$1/bars" ] || return 1
  find "$1/bars" -name '*.parquet' -print 2>/dev/null | head -1 | grep -q .
}

# Datasets = (exchange, segment, symbol, interval) directories — depth 4 under
# bars/ — the same definition the dashboard's /db discovery uses.
lake_dataset_count() {
  if [ -d "$1/bars" ]; then
    find "$1/bars" -mindepth 4 -maxdepth 4 -type d 2>/dev/null | wc -l | tr -d ' '
  else
    printf '0'
  fi
}

# Decide + permanently record the lake location with ZERO operator input.
# Sets LAKE_ROOT / LAKE_SOURCE / LAKE_DATASETS. Order:
#   1. a persisted config line       → reuse silently (an exported env var that
#      differs wins for THIS run — brutex/config/dotenv.py real-env-wins — and
#      is reported, never asked about);
#   2. an EXISTING lake with real data → auto-detect (an environment-pointed
#      path first, then the app default ~/.brutex/lake) and record it;
#   3. an environment-pointed path (no data yet) → record it;
#   4. nothing anywhere → create + record the app default ~/.brutex/lake.
resolve_and_record_lake() {
  persisted_lake="$(sed -n 's/^BRUTEX_LAKE_ROOT=//p' "$CONF" | head -1)"
  if [ -n "$persisted_lake" ]; then
    if [ -n "${BRUTEX_LAKE_ROOT:-}" ] && [ "${BRUTEX_LAKE_ROOT}" != "$persisted_lake" ]; then
      LAKE_ROOT="${BRUTEX_LAKE_ROOT}"
      LAKE_SOURCE="environment-override"
    else
      LAKE_ROOT="$persisted_lake"
      LAKE_SOURCE="persisted"
    fi
  elif [ -n "${BRUTEX_LAKE_ROOT:-}" ] && lake_has_data "${BRUTEX_LAKE_ROOT}"; then
    LAKE_ROOT="${BRUTEX_LAKE_ROOT}"
    LAKE_SOURCE="auto-detected"
  elif lake_has_data "$BRUTEX_HOME/lake"; then
    LAKE_ROOT="$BRUTEX_HOME/lake"
    LAKE_SOURCE="auto-detected"
  elif [ -n "${BRUTEX_LAKE_ROOT:-}" ]; then
    LAKE_ROOT="${BRUTEX_LAKE_ROOT}"
    LAKE_SOURCE="from-environment"
  else
    LAKE_ROOT="$BRUTEX_HOME/lake"
    LAKE_SOURCE="created"
  fi
  LAKE_DATASETS="$(lake_dataset_count "$LAKE_ROOT")"
  # Record ONCE (presence-guarded) — never rewrite an existing operator line.
  if ! grep -q '^BRUTEX_LAKE_ROOT=' "$CONF"; then
    printf 'BRUTEX_LAKE_ROOT=%s\n' "$LAKE_ROOT" >> "$CONF"
  fi
}

# Print the rotating log-file path the runtime will use (or is using),
# mirroring brutex/observability/logging.py resolve_log_file exactly:
# $BRUTEX_LOG_FILE (full path) → $BRUTEX_LOG_DIR/brutex.log → the persisted
# ~/.brutex/.env BRUTEX_LOG_DIR line (dotenv is loaded at every brutex boot)
# → the default ~/.brutex/logs/brutex.log. Pure computation — never mkdirs.
resolve_log_file() {
  if [ -n "${BRUTEX_LOG_FILE:-}" ]; then
    printf '%s' "$BRUTEX_LOG_FILE"
    return 0
  fi
  log_dir="${BRUTEX_LOG_DIR:-}"
  if [ -z "$log_dir" ] && [ -f "$CONF" ]; then
    log_dir="$(sed -n 's/^BRUTEX_LOG_DIR=//p' "$CONF" | head -1)"
  fi
  [ -n "$log_dir" ] || log_dir="$BRUTEX_HOME/logs"
  printf '%s/brutex.log' "$log_dir"
}

# Make the log visible INSIDE the project tree (DEC-LOGS-IN-PROJECT-001): create
# or refresh an IDEMPOTENT symlink `<repo>/logs` → the resolved log DIRECTORY, so
# clicking ▶ BruteX in IntelliJ shows the live log folder + file in the project
# (the default log home ~/.brutex/logs is invisible to the IDE tree). Rules:
#   * a fresh link is created (the log dir is mkdir'd first so it never dangles);
#   * an existing symlink pointing elsewhere (a stale BRUTEX_LOG_DIR change) is
#     REPOINTED; an already-correct one is left as-is (re-run = no-op);
#   * a REAL directory/file named `logs` is NEVER clobbered — warn + leave it;
#   * a failed ln/rm degrades to the plain path line — the boot NEVER breaks.
# Prints ONE honest line either way. `<repo>/logs` is .gitignored. (`make run-raw`
# needs no link — its LOG_DIR default ./.brutex-logs is already inside the repo.)
link_repo_logs() {
  log_file="$(resolve_log_file)"
  log_dir="$(dirname "$log_file")"
  link="$REPO_ROOT/logs"
  mkdir -p "$log_dir" 2>/dev/null || true
  if [ -L "$link" ]; then
    if [ "$(readlink "$link")" != "$log_dir" ]; then
      rm -f "$link" 2>/dev/null && ln -s "$log_dir" "$link" 2>/dev/null || {
        printf 'logs → %s  (could not repoint the %s symlink)\n' "$log_file" "$link"
        return 0
      }
    fi
  elif [ -e "$link" ]; then
    printf 'WARNING: %s exists and is NOT a symlink — left untouched (the log stays at %s)\n' \
      "$link" "$log_file"
    printf 'logs → %s\n' "$log_file"
    return 0
  else
    ln -s "$log_dir" "$link" 2>/dev/null || {
      printf 'logs → %s  (could not create the %s symlink)\n' "$log_file" "$link"
      return 0
    }
  fi
  printf 'logs → %s → %s\n' "$link" "$log_dir"
}

# Return 0 and print "<pid> <url>" when a live dashboard is found via the
# service-discovery runtime file (brutex/config/runtime.py — PID + URLs written
# on startup, PID-liveness is the staleness oracle). Return 1 otherwise.
runtime_pid_alive() {
  rt="${BRUTEX_RUNTIME_FILE:-$BRUTEX_HOME/runtime.json}"
  [ -f "$rt" ] || return 1
  live_pid="$(sed -n 's/.*"pid":[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$rt" | head -1)"
  [ -n "$live_pid" ] || return 1
  kill -0 "$live_pid" 2>/dev/null || return 1
  live_url="$(sed -n 's/.*"\(http:\/\/[^"]*\)".*/\1/p' "$rt" | head -1)"
  printf '%s %s\n' "$live_pid" "${live_url:-$DASHBOARD_URL}"
}

# ── internal test hook — run ONLY the zero-decision lake detection ───────────
if [ "$PRINT_LAKE_DETECT" -eq 1 ]; then
  mkdir -p "$BRUTEX_HOME" || { printf 'up.sh: cannot create %s\n' "$BRUTEX_HOME" >&2; exit 1; }
  [ -f "$CONF" ] || : > "$CONF"
  resolve_and_record_lake
  printf 'LAKE_DECISION=%s %s datasets=%s\n' "$LAKE_SOURCE" "$LAKE_ROOT" "$LAKE_DATASETS"
  exit 0
fi

# ── internal test hook — run ONLY the <repo>/logs symlink refresh ────────────
if [ "$LINK_LOGS_ONLY" -eq 1 ]; then
  link_repo_logs
  exit 0
fi

# ── FAST PATH — re-run while a dashboard is up = no-op in under a second ─────
# The dashboard URL + the rotating log-file location each get ONE clear line
# (the running process printed its own on boot; this re-run exits before the
# runtime prints anything, so the fast path repeats both for visibility). The
# <repo>/logs symlink is refreshed on EVERY path (DEC-LOGS-IN-PROJECT-001).
if live="$(runtime_pid_alive)"; then
  printf 'already ready — BruteX dashboard is running (pid %s) at %s\n' \
    "${live%% *}" "${live#* }"
  link_repo_logs
  exit 0
fi

# ── Step 1/6 — Python 3.12 + virtualenv (create or self-heal) ────────────────
say "Step 1/6 — Python 3.12 + virtualenv"
PYBIN=""
if command -v python3.12 >/dev/null 2>&1; then
  PYBIN="python3.12"
elif command -v python3 >/dev/null 2>&1 \
  && python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)' \
    2>/dev/null; then
  PYBIN="python3"
else
  case "$(uname -s 2>/dev/null || echo unknown)" in
    Darwin) py_fix="brew install python@3.12" ;;
    Linux)  py_fix="sudo apt-get install -y python3.12 python3.12-venv" ;;
    *)      py_fix="install Python 3.12 from https://www.python.org/downloads/" ;;
  esac
  failhard "python3.12 NOT found — fix: $py_fix"
fi
pass "python: $("$PYBIN" --version 2>&1)  ($(command -v "$PYBIN"))"

# Self-heal: a .venv whose python is missing, broken, or older than 3.12 (a
# stale .pth, a deleted system interpreter, a venv built from python3.11) is
# rebuilt. This removes ONLY the in-repo .venv build artifact — user data under
# ~/.brutex (lake, ledger, credentials cache, logs) is NEVER touched.
venv_ok=0
if [ -x "$VPY" ] \
  && "$VPY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)' \
    2>/dev/null \
  && "$VPY" -m pip --version >/dev/null 2>&1; then
  venv_ok=1
fi
if [ -d "$VENV" ] && [ "$venv_ok" -eq 0 ]; then
  note "existing .venv is broken or stale (missing/old python or broken pip) — rebuilding"
  note "(self-heal removes ONLY .venv; ~/.brutex user data and the lake are never touched)"
  rm -rf "$VENV"
fi
if [ ! -d "$VENV" ]; then
  "$PYBIN" -m venv "$VENV" \
    || failhard "could not create .venv — fix: ensure the venv module works ($PYBIN -m venv)"
  pass "created .venv  ($("$VPY" --version 2>&1))"
else
  pass ".venv OK — reusing"
fi

# ── Step 2/6 — Python dependencies (idempotent install + per-dep verify) ─────
say "Step 2/6 — Python dependencies  (pip install -e \"$EXTRAS\")"
DEPS="brutex boto3 growwapi pyotp fastapi uvicorn polars duckdb"
probe_deps() {
  "$VPY" -c "import brutex, boto3, growwapi, pyotp, fastapi, uvicorn, polars, duckdb" \
    >/dev/null 2>&1
}
if probe_deps; then
  pass "all dependencies already importable — skipping pip install"
else
  note "installing (first run, or a dep is missing — this can take a few minutes)"
  "$VPY" -m pip install --quiet --upgrade pip \
    || failhard "pip self-upgrade failed — fix: check network/proxy, then re-run ./up.sh"
  "$VPY" -m pip install --quiet -e "$EXTRAS" \
    || failhard "pip install -e \"$EXTRAS\" failed — read the pip error above (network / build toolchain), fix it, then re-run ./up.sh"
fi
dep_failed=0
for dep in $DEPS; do
  if "$VPY" -c "import $dep" >/dev/null 2>&1; then
    pass "import $dep"
  else
    printf '  FAIL  import %s — fix: %s -m pip install -e "%s"\n' "$dep" "$VPY" "$EXTRAS" >&2
    dep_failed=1
  fi
done
[ "$dep_failed" -eq 0 ] || failhard "dependency verification failed (see the FAIL lines above)"

# ── Step 3/6 — Rust PyO3 wheels (build only when missing) ────────────────────
say "Step 3/6 — Rust PyO3 wheels (required: fno_math + trading_core + brutex_identity + brutex_refine + brutex_pull_state; best-effort: brutex_feed + brutex_lake)"
# HONEST requirement check — the REQUIRED set mirrors the canonical one in
# scripts/setup/bootstrap.sh (Steps 5/6/6b/6a2/6c) + the Makefile run-raw:
#   * fno_math + trading_core — brutex.indicators.{pivots,fib_levels,ema}
#     import trading_core at module top; brutex.strategies.combo imports
#     fno_math — the dashboard cannot even import without them;
#   * brutex_identity — DEC-RUST-RUNID-STORES-001 (MIGRATION.md #2065): a
#     MODULE-TOP import of brutex/engine/{results_store,snapshot_hash}.py +
#     brutex/data/window_ledger_store.py, all on the CLI/dashboard boot chain
#     (cli → engine/__init__ → results_report → results_store), so a fresh
#     clone crashes with ModuleNotFoundError at boot without it;
#   * brutex_refine — DEC-RUST-STAGE3-REFINE-001: Stage-3 is Rust-only (the
#     pycma Python math is deleted; call-time fail-closed, no oracle);
#   * brutex_pull_state — the pull-state decision plane is Rust-only and
#     fail-closed (call-time; every dashboard pull surface consumes it).
# There is no "wheels not required" skip to offer for the REQUIRED set.
# BEST-EFFORT wheels (a build failure WARNS, never aborts — mirrors
# bootstrap.sh Steps 6a/6a3): brutex_feed (BRUTEX_FEED_RUST is default-OFF)
# and brutex_lake (DEC-BRUTEX-LAKE-BOX-WHEEL-001 — warn-only in the box path;
# absent = the fail-loud LakeRustWavedUnavailableError at USE time; a cold
# build compiles the pinned polars graph — minutes + ~12 GB of target/).
# Probe-first (idempotent re-run): every wheel is import-probed BEFORE any
# maturin build. bash-3.2-safe (plain space-separated strings, no arrays).
REQUIRED_IMPORTS="fno_math, trading_core, brutex_identity, brutex_refine, brutex_pull_state"
wheels_missing=""
"$VPY" -c "import fno_math" >/dev/null 2>&1 || wheels_missing="rust/fno-math"
"$VPY" -c "import trading_core" >/dev/null 2>&1 \
  || wheels_missing="$wheels_missing rust/trading-core"
"$VPY" -c "import brutex_identity" >/dev/null 2>&1 \
  || wheels_missing="$wheels_missing rust/brutex-identity"
"$VPY" -c "import brutex_refine" >/dev/null 2>&1 \
  || wheels_missing="$wheels_missing rust/brutex-refine"
"$VPY" -c "import brutex_pull_state" >/dev/null 2>&1 \
  || wheels_missing="$wheels_missing rust/brutex-pull-state"
wheels_missing="${wheels_missing# }"
optional_missing=""
"$VPY" -c "import brutex_feed" >/dev/null 2>&1 || optional_missing="rust/brutex-feed"
"$VPY" -c "import brutex_lake" >/dev/null 2>&1 \
  || optional_missing="$optional_missing rust/brutex-lake"
optional_missing="${optional_missing# }"
if [ -z "$wheels_missing" ] && [ -z "$optional_missing" ]; then
  pass "all Rust wheels already importable — skipping build"
else
  CARGO_BIN=""
  if command -v cargo >/dev/null 2>&1; then
    CARGO_BIN="$(command -v cargo)"
  elif [ -x "$HOME/.cargo/bin/cargo" ]; then
    CARGO_BIN="$HOME/.cargo/bin/cargo"
  fi
  if [ -z "$CARGO_BIN" ] && [ -n "$wheels_missing" ]; then
    failhard "REQUIRED wheels missing ($wheels_missing) and cargo NOT found — the dashboard cannot import without them (brutex.indicators.* import trading_core and brutex.engine.results_store imports brutex_identity at module top, DEC-RUST-RUNID-STORES-001). fix: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh   — then restart the shell (or: source \$HOME/.cargo/env) and re-run ./up.sh"
  fi
  if [ -z "$CARGO_BIN" ]; then
    # Only best-effort wheels are missing — never a hard stop without cargo.
    printf '  WARN  optional wheels not importable (%s) and cargo NOT found — skipping (best-effort; the affected Rust ops refuse loudly at USE time until built)\n' \
      "$optional_missing"
  else
    [ -x "$VENV/bin/maturin" ] || "$VPY" -m pip install --quiet maturin \
      || failhard "could not install maturin — fix: check network/proxy, then re-run ./up.sh"
    for crate in $wheels_missing; do
      note "building $crate (one-time; REQUIRED; uses $CARGO_BIN)"
      ( cd "$REPO_ROOT/$crate" \
        && PATH="$HOME/.cargo/bin:$PATH" VIRTUAL_ENV="$VENV" \
          "$VENV/bin/maturin" develop --release ) \
        || failhard "maturin develop --release failed for $crate — read the cargo error above, then re-run ./up.sh"
    done
    if [ -n "$wheels_missing" ]; then
      "$VPY" -c "import $REQUIRED_IMPORTS" >/dev/null 2>&1 \
        || failhard "required wheels built but still not importable — re-run ./up.sh (fallback: make run)"
      pass "required wheels built + importable ($REQUIRED_IMPORTS)"
    fi
    for crate in $optional_missing; do
      note "building $crate (one-time; best-effort; uses $CARGO_BIN)"
      ( cd "$REPO_ROOT/$crate" \
        && PATH="$HOME/.cargo/bin:$PATH" VIRTUAL_ENV="$VENV" \
          "$VENV/bin/maturin" develop --release ) \
        || printf '  WARN  maturin develop --release failed for %s — best-effort wheel (the dashboard still boots; the affected Rust ops refuse loudly at USE time). fix later: cd %s && maturin develop --release\n' \
          "$crate" "$crate"
    done
    if [ -z "$wheels_missing" ]; then
      pass "required wheels already importable; best-effort builds attempted"
    fi
  fi
fi

# ── Step 4/6 — zero-decision lake + persisted config (recorded ONCE) ─────────
say "Step 4/6 — lake location + persisted config ($CONF — loaded by brutex at every boot; nothing to type, ever)"
mkdir -p "$BRUTEX_HOME" \
  || failhard "cannot create $BRUTEX_HOME — fix: check \$HOME permissions"
[ -f "$CONF" ] || : > "$CONF"

# The lake location is decided WITHOUT any operator input: persisted config →
# an existing lake auto-detected (environment-pointed path, then the app
# default ~/.brutex/lake) → environment-pointed path → create the default.
# brutex/config/dotenv.py loads $CONF at every CLI + dashboard boot, so the
# recording is a one-time act — no per-session exports ever again.
resolve_and_record_lake
case "$LAKE_SOURCE" in
  persisted)
    pass "lake: $LAKE_ROOT (persisted, $LAKE_DATASETS datasets)"
    ;;
  environment-override)
    pass "lake: $LAKE_ROOT (this session's environment overrides the persisted location for this run, $LAKE_DATASETS datasets)"
    ;;
  auto-detected)
    pass "lake: $LAKE_ROOT (auto-detected existing data, $LAKE_DATASETS datasets — recorded permanently, nothing to type)"
    ;;
  from-environment)
    pass "lake: $LAKE_ROOT (recorded permanently from this session's environment — nothing to type)"
    ;;
  *)
    pass "lake: $LAKE_ROOT (created — your market data will live here; recorded permanently, nothing to type)"
    ;;
esac
mkdir -p "$LAKE_ROOT" 2>/dev/null \
  || failhard "cannot create the lake directory $LAKE_ROOT — fix: check the disk mount/permissions, then re-run ./up.sh"

# Auto-resume gates (DEC-AUTO-RESUME-DEFAULT-OFF-001): boot auto-resume is OFF
# by default by operator directive — an auto-pull must NEVER launch without an
# explicit opt-in. up.sh pre-arms ONLY the legacy SUB-gate
# (BRUTEX_AUTO_RESUME_ON_BOOT=1, inert on its own) so that ONE flip — the
# BRUTEX_AUTO_RESUME=1 master gate — restores the full prior behaviour; the
# master gate itself is written as a COMMENTED documentation line, never armed
# here. Honest wiring: even opted in, the resume fires ONLY when the durable
# window ledger shows unfinished work (brutex/orchestrator/auto_resume.py,
# DEC-AUTO-RESUME-ON-BOOT-001) — a clean ledger boots idle, never a surprise
# pull. An operator's explicit setting in $CONF is respected, never rewritten.
if grep -q '^BRUTEX_AUTO_RESUME_ON_BOOT=' "$CONF"; then
  pass "auto-resume sub-gate already configured: $(grep '^BRUTEX_AUTO_RESUME_ON_BOOT=' "$CONF" | head -1)"
else
  printf 'BRUTEX_AUTO_RESUME_ON_BOOT=1\n' >> "$CONF"
  pass "auto-resume sub-gate pre-armed (BRUTEX_AUTO_RESUME_ON_BOOT=1 in $CONF) — inert until the master gate is enabled"
fi
if grep -q 'BRUTEX_AUTO_RESUME=' "$CONF"; then
  pass "auto-resume master gate already configured: $(grep 'BRUTEX_AUTO_RESUME=' "$CONF" | head -1)"
else
  printf '# BRUTEX_AUTO_RESUME=1   # uncomment to enable boot auto-resume (master opt-in; default OFF)\n' >> "$CONF"
  pass "auto-resume is OFF by default (operator directive): to enable crash-recovery auto-resume set BRUTEX_AUTO_RESUME=1 in $CONF — the legacy sub-gate is pre-armed, so that one flip suffices"
fi

# ── Step 5/6 — self-check chain (PASS or exact-cause+fix, never a stack trace) ─
say "Step 5/6 — self-check chain"

# 5a — deps importable (re-verified so the chain is complete on a fast re-run).
if probe_deps; then
  pass "deps importable (brutex + boto3 + growwapi + pyotp + dashboard runtime)"
else
  failhard "deps no longer importable — re-run ./up.sh to reinstall"
fi

# 5b — AWS credentials usable (sts get-caller-identity via boto3). BruteX reads
# the Groww credentials + daily token ONLY from AWS SSM (DEC-AWS-ONLY-CREDS-001),
# so without working AWS credentials a live pull will fail. DEGRADE, not stop.
aws_out="$("$VPY" - <<'PY' 2>/dev/null
import sys
try:
    import boto3
    ident = boto3.client("sts").get_caller_identity()
    print("OK " + str(ident.get("Account", "?")))
except Exception as exc:  # one honest line, never a stack trace
    print("ERR " + type(exc).__name__)
    sys.exit(1)
PY
)"
case "$aws_out" in
  OK*)
    pass "AWS credentials usable (sts get-caller-identity → account ${aws_out#OK })"
    ;;
  *)
    degrade "AWS credentials NOT usable (${aws_out#ERR }) — fix: run 'aws configure' (or export AWS_PROFILE, or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY). BruteX reads the Groww credentials + daily token ONLY from AWS SSM (DEC-AWS-ONLY-CREDS-001), so a live pull will fail until this is fixed."
    ;;
esac

# 5c — Groww daily access token readable from SSM. STRICTLY READ-ONLY: one
# GetParameter (WithDecryption) on the access-token path. up.sh NEVER mints a
# token and never writes SSM — the dedicated Lambda (deploy/aws_token_minter/)
# is the only minter/writer.
UP_SSM_REGION="${BRUTEX_TOKEN_AWS_REGION:-${AWS_REGION:-ap-south-1}}"
UP_SSM_PARAM="${BRUTEX_TOKEN_PARAM_ACCESS_TOKEN:-/tickvault/prod/groww/access-token}"
ssm_out="$(UP_SSM_REGION="$UP_SSM_REGION" UP_SSM_PARAM="$UP_SSM_PARAM" "$VPY" - <<'PY' 2>/dev/null
import os
import sys
try:
    import boto3
    ssm = boto3.client("ssm", region_name=os.environ["UP_SSM_REGION"])
    resp = ssm.get_parameter(Name=os.environ["UP_SSM_PARAM"], WithDecryption=True)
    value = resp.get("Parameter", {}).get("Value") or ""
    if not value.strip():
        print("ERR EmptyParameter")
        sys.exit(1)
    print("OK %d" % len(value))  # LENGTH only — the token value is NEVER printed
except Exception as exc:  # classify; one honest line, never a stack trace
    code = ""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = str(resp.get("Error", {}).get("Code", ""))
    print("ERR " + (code or type(exc).__name__))
    sys.exit(1)
PY
)"
case "$ssm_out" in
  OK*)
    pass "Groww access token readable from SSM $UP_SSM_PARAM (read-only GetParameter, region $UP_SSM_REGION; length ${ssm_out#OK } — the value is never printed)"
    ;;
  "ERR ParameterNotFound")
    degrade "Groww access token NOT found at $UP_SSM_PARAM (region $UP_SSM_REGION) — fix: the daily minter has not written it yet — deploy it once (deploy/aws_token_minter/deploy.sh) or wait for the ~06:05 IST scheduled mint. up.sh NEVER mints a token (read-only by design)."
    ;;
  "ERR AccessDenied"|"ERR AccessDeniedException")
    degrade "SSM read DENIED for $UP_SSM_PARAM — fix: attach the read-only consumer IAM policy (ssm:GetParameter on the access-token + kms:Decrypt — see deploy/aws_token_minter/). up.sh only READS the token, never mints."
    ;;
  *)
    degrade "could not read $UP_SSM_PARAM from SSM (${ssm_out#ERR }) — fix: resolve the AWS credential/region issue above (region in use: $UP_SSM_REGION). up.sh only READS the token, never mints."
    ;;
esac

# 5d — lake root writable (hard stop: nothing can be pulled or stored without it).
probe="$LAKE_ROOT/.up-write-probe.$$"
if ( : > "$probe" ) 2>/dev/null; then
  rm -f "$probe"
  pass "lake: $LAKE_ROOT ($LAKE_SOURCE, $LAKE_DATASETS datasets) — writable"
else
  failhard "lake root NOT writable: $LAKE_ROOT — fix: check the disk mount/permissions, then re-run ./up.sh"
fi

# ── Step 6/6 — start the dashboard (reuse the existing one-click entry) ──────
say "Step 6/6 — dashboard"
if [ "$DEGRADED" -ne 0 ]; then
  note "DEGRADED: an AWS/SSM check failed above — the dashboard will run, but a LIVE pull will fail until it is fixed (each FAIL line names the exact fix)."
fi
# Re-check the fast path: a concurrent ./up.sh may have started the dashboard
# while this run was setting up — never boot a second instance.
if live="$(runtime_pid_alive)"; then
  printf 'already ready — BruteX dashboard is running (pid %s) at %s\n' \
    "${live%% *}" "${live#* }"
  link_repo_logs
  exit 0
fi
# In-project log visibility (DEC-LOGS-IN-PROJECT-001): refresh the <repo>/logs
# symlink BEFORE the boot/no-start handoff so the IDE project tree shows the
# live log folder the moment the dashboard starts (one honest mapping line;
# the runtime itself then prints its own `[brutex] logs → <file>` on boot).
link_repo_logs
if [ "$NO_START" -eq 1 ]; then
  printf 'setup complete — start the dashboard with ./up.sh (or: %s -m brutex run) → %s\n' \
    "$VPY" "$DASHBOARD_URL"
  exit 0
fi
printf 'starting the BruteX dashboard → %s  (Ctrl-C stops it; re-run ./up.sh any time — it fast-paths to "already ready")\n' \
  "$DASHBOARD_URL"
exec "$VPY" -m brutex run
