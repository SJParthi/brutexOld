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
#   Step 3/6  — build the fno_math + trading_core PyO3 wheels via maturin when not
#               importable. They ARE required for the dashboard + pull path
#               (brutex.indicators.{pivots,fib_levels,ema} import trading_core at
#               module top; brutex.strategies.combo imports fno_math — the same
#               reason the Makefile `run` target builds both), so wheels-missing +
#               cargo-absent is an HONEST hard stop with the rustup one-liner, not
#               a silent skip.
#   Step 4/6  — persist BRUTEX_LAKE_ROOT + BRUTEX_AUTO_RESUME_ON_BOOT=1 ONCE in
#               ~/.brutex/.env (brutex loads that file at every boot via
#               brutex/config/dotenv.py; an exported env var always wins). No
#               per-session exports ever again.
#   Step 5/6  — self-check chain, each line PASS or exact-cause+fix, never a stack
#               trace: deps importable → AWS identity (sts get-caller-identity via
#               boto3) → Groww daily access token READABLE from AWS SSM (read-only
#               GetParameter — this script NEVER mints a token) → lake root
#               writable. AWS/SSM failures DEGRADE honestly (the dashboard still
#               starts; a live pull will fail until fixed); an unwritable lake
#               root is a hard stop.
#   Step 6/6  — hand off to the existing one-click entry
#               `.venv/bin/python -m brutex run` (the same entry `make run`
#               invokes — DEC-ONECLICK-RUN-001), which prints the URL, opens the
#               browser, and boots the dashboard at http://127.0.0.1:9080.
#
# Idempotent + re-run-safe: every step probes before it mutates, so a re-run with
# everything ready reaches "already ready" / the dashboard in seconds — including
# a re-run mid-setup after a Ctrl-C. Written for bash 3.2 (the macOS default):
# no arrays, no mapfile/readarray, no associative arrays, no ${var,,}.
#
# Flags:
#   --no-start   do everything except booting the dashboard (CI / cron / tests)
#   -h, --help   print usage
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
"already ready" and exits in seconds.

  --no-start   run every setup + self-check step but do not boot the dashboard
  -h, --help   this message
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --no-start) NO_START=1 ;;
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

# ── FAST PATH — re-run while a dashboard is up = no-op in under a second ─────
if live="$(runtime_pid_alive)"; then
  printf 'already ready — BruteX dashboard is running (pid %s) at %s\n' \
    "${live%% *}" "${live#* }"
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
say "Step 3/6 — Rust PyO3 wheels (fno_math + trading_core)"
# HONEST requirement check: both wheels ARE required for the dashboard + pull
# path — brutex.indicators.{pivots,fib_levels,ema} import trading_core at module
# top and brutex.strategies.combo imports fno_math, so the dashboard cannot even
# import without them (mirrors the Makefile `run` target, which builds both).
# There is no "wheels not required" skip to offer.
wheels_missing=""
"$VPY" -c "import fno_math" >/dev/null 2>&1 || wheels_missing="rust/fno-math"
"$VPY" -c "import trading_core" >/dev/null 2>&1 \
  || wheels_missing="$wheels_missing rust/trading-core"
if [ -z "$wheels_missing" ]; then
  pass "fno_math + trading_core already importable — skipping build"
else
  CARGO_BIN=""
  if command -v cargo >/dev/null 2>&1; then
    CARGO_BIN="$(command -v cargo)"
  elif [ -x "$HOME/.cargo/bin/cargo" ]; then
    CARGO_BIN="$HOME/.cargo/bin/cargo"
  fi
  if [ -z "$CARGO_BIN" ]; then
    failhard "wheels missing ($wheels_missing) and cargo NOT found — the dashboard cannot import without them (brutex.indicators.* import trading_core at module top). fix: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh   — then restart the shell (or: source \$HOME/.cargo/env) and re-run ./up.sh"
  fi
  [ -x "$VENV/bin/maturin" ] || "$VPY" -m pip install --quiet maturin \
    || failhard "could not install maturin — fix: check network/proxy, then re-run ./up.sh"
  for crate in $wheels_missing; do
    note "building $crate (one-time; uses $CARGO_BIN)"
    ( cd "$REPO_ROOT/$crate" \
      && PATH="$HOME/.cargo/bin:$PATH" VIRTUAL_ENV="$VENV" \
        "$VENV/bin/maturin" develop --release ) \
      || failhard "maturin develop --release failed for $crate — read the cargo error above, then re-run ./up.sh"
  done
  "$VPY" -c "import fno_math, trading_core" >/dev/null 2>&1 \
    || failhard "wheels built but still not importable — re-run ./up.sh (fallback: make run)"
  pass "wheels built + importable"
fi

# ── Step 4/6 — persisted config (~/.brutex/.env — recorded ONCE) ─────────────
say "Step 4/6 — persisted config ($CONF — loaded by brutex at every boot; no per-session exports)"
mkdir -p "$BRUTEX_HOME" \
  || failhard "cannot create $BRUTEX_HOME — fix: check \$HOME permissions"
[ -f "$CONF" ] || : > "$CONF"

# Lake root: persisted value → current exported env → default ~/.brutex/lake.
# brutex/config/dotenv.py loads $CONF at every CLI + dashboard boot and a real
# exported env var always wins, so recording here is a one-time act.
persisted_lake="$(sed -n 's/^BRUTEX_LAKE_ROOT=//p' "$CONF" | head -1)"
if [ -n "$persisted_lake" ]; then
  LAKE_ROOT="${BRUTEX_LAKE_ROOT:-$persisted_lake}"
  if [ -n "${BRUTEX_LAKE_ROOT:-}" ] && [ "${BRUTEX_LAKE_ROOT}" != "$persisted_lake" ]; then
    note "NOTE: exported BRUTEX_LAKE_ROOT=${BRUTEX_LAKE_ROOT} overrides the persisted ${persisted_lake} for THIS run (an exported env var always wins over $CONF)"
  fi
  pass "lake root (already persisted): $LAKE_ROOT"
elif [ -n "${BRUTEX_LAKE_ROOT:-}" ]; then
  LAKE_ROOT="$BRUTEX_LAKE_ROOT"
  printf 'BRUTEX_LAKE_ROOT=%s\n' "$LAKE_ROOT" >> "$CONF"
  pass "lake root recorded from \$BRUTEX_LAKE_ROOT: $LAKE_ROOT (persisted in $CONF — you never need to export it again)"
else
  LAKE_ROOT="$BRUTEX_HOME/lake"
  printf 'BRUTEX_LAKE_ROOT=%s\n' "$LAKE_ROOT" >> "$CONF"
  pass "lake root defaulted to $LAKE_ROOT (persisted in $CONF — change it any time by editing the BRUTEX_LAKE_ROOT= line there)"
fi
mkdir -p "$LAKE_ROOT" 2>/dev/null \
  || failhard "cannot create lake root $LAKE_ROOT — fix: check the disk mount/permissions, or point BRUTEX_LAKE_ROOT in $CONF at a writable disk"

# Auto-resume-on-boot: enable by default, ONCE. Honest wiring: the flag makes
# the dashboard boot resume an interrupted pull ONLY when the durable window
# ledger shows unfinished work (brutex/orchestrator/auto_resume.py,
# DEC-AUTO-RESUME-ON-BOOT-001) — a clean ledger boots idle, never a surprise
# pull. An operator's explicit setting in $CONF is respected, never rewritten.
if grep -q '^BRUTEX_AUTO_RESUME_ON_BOOT=' "$CONF"; then
  pass "auto-resume-on-boot already configured: $(grep '^BRUTEX_AUTO_RESUME_ON_BOOT=' "$CONF" | head -1)"
else
  printf 'BRUTEX_AUTO_RESUME_ON_BOOT=1\n' >> "$CONF"
  pass "auto-resume-on-boot enabled (BRUTEX_AUTO_RESUME_ON_BOOT=1 in $CONF): an interrupted pull resumes when the dashboard boots — only when the window ledger shows unfinished work; set it to 0 there to disable"
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
  pass "lake root writable: $LAKE_ROOT"
else
  failhard "lake root NOT writable: $LAKE_ROOT — fix: check the disk mount/permissions, or point BRUTEX_LAKE_ROOT in $CONF at a writable disk"
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
  exit 0
fi
if [ "$NO_START" -eq 1 ]; then
  printf 'setup complete — start the dashboard with ./up.sh (or: %s -m brutex run) → %s\n' \
    "$VPY" "$DASHBOARD_URL"
  exit 0
fi
printf 'starting the BruteX dashboard → %s  (Ctrl-C stops it; re-run ./up.sh any time — it fast-paths to "already ready")\n' \
  "$DASHBOARD_URL"
exec "$VPY" -m brutex run
