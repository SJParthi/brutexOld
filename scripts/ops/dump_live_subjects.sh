#!/usr/bin/env bash
# Dump the live LMDB dedup-state to a newline-separated subjects file,
# ready to feed into the L3 audit-log replay oracle
# (DEC-L3-REPLAY-ORACLE-001 + DEC-STANDARD-MONITOR-001).
#
# Wire to crontab for periodic refresh:
#
#     */15 * * * * /path/to/scripts/ops/dump_live_subjects.sh
#
# The output file path defaults to ${BRUTEX_LIVE_SUBJECTS_FILE} —
# the same env var the production monitor reads — so no operator
# config is required after wiring this cron.
#
# Decision: DEC-DUMP-LIVE-SUBJECTS-001 (MIGRATION entry #244).

set -uo pipefail

# ────────── config ──────────
LMDB_PATH="${BRUTEX_LMDB_PATH:-${HOME}/.brutex/lmdb}"
LIVE_FILE="${BRUTEX_LIVE_SUBJECTS_FILE:-${HOME}/.brutex/live-subjects.txt}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

# Find a Python with brutex installed (prefer venv).
if [[ -x .venv/bin/python ]]; then
  PY=.venv/bin/python
elif command -v python3.12 >/dev/null 2>&1; then
  PY=python3.12
else
  echo "✗ no Python 3.12 or .venv found; bootstrap first" >&2
  exit 1
fi

# Skip silently if LMDB doesn't exist (fresh box, no ingest yet).
if [[ ! -d "${LMDB_PATH}" ]]; then
  echo "(no LMDB store at ${LMDB_PATH}; writing empty subjects file)"
  mkdir -p "$(dirname "${LIVE_FILE}")"
  : > "${LIVE_FILE}"
  exit 0
fi

# Atomic-write: dump to .tmp, then rename. Avoids the L3 oracle
# reading a half-written file.
TMP_FILE="${LIVE_FILE}.tmp.$$"
mkdir -p "$(dirname "${LIVE_FILE}")"

"${PY}" - <<EOF > "${TMP_FILE}"
import sys
from pathlib import Path

try:
    import lmdb
except ImportError:
    print("✗ lmdb not installed; run: pip install -e '.[data]'", file=sys.stderr)
    sys.exit(1)

LMDB_PATH = Path("${LMDB_PATH}")
env = lmdb.open(str(LMDB_PATH), readonly=True, lock=False, max_dbs=8)
try:
    with env.begin() as txn:
        cursor = txn.cursor()
        for key, _value in cursor:
            try:
                key_str = key.decode("utf-8")
            except UnicodeDecodeError:
                # Binary keys: hex-encode for safe inclusion
                key_str = key.hex()
            print(key_str)
finally:
    env.close()
EOF
PY_RC=$?

if [[ ${PY_RC} -ne 0 ]]; then
  rm -f "${TMP_FILE}"
  echo "✗ Python dump failed (exit ${PY_RC})" >&2
  exit ${PY_RC}
fi

# Atomic rename
mv "${TMP_FILE}" "${LIVE_FILE}"

COUNT=$(wc -l < "${LIVE_FILE}" | tr -d ' ')
echo "✓ Dumped ${COUNT} live subjects to ${LIVE_FILE}"
