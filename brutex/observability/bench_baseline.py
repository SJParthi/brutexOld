"""Committed per-hardware ABSOLUTE benchmark baseline — the ONE source of truth.

DEC-BENCH-ABS-BASELINE-001 (MIGRATION.md #1533). Every perf gate before this
was a RATIO gate (``tests/bench/_o1_tiers.py``, Golden Rule #5) plus one
env-gated 50 M ops/s floor (``tests/bench/test_throughput_absolute.py``) — a
constant-factor 2-10x slowdown of a hot path passed every ratio gate. This
module is the pure comparator/schema/capture/governance core behind the
committed per-hardware absolute baselines at ``tests/bench/baselines/`` and
the ``scripts/ci/bench_baseline_gate.py`` CLI.

Contract (CLAUDE.md §11 COMMON · DYNAMIC · AUTOMATED):

* **Pure stdlib** — no structlog, no print, no subprocess, no third-party
  import at module scope. Functions return reports or raise typed
  exceptions; callers log/print. Loadable STANDALONE via
  ``importlib.util.spec_from_file_location`` (the ``scripts/ci/_textscan.py``
  sibling-contract idiom), so the shared-CI governance/validate steps need
  zero pip installs.
* **ZERO brutex imports** — module scope AND function scope. This module
  is the pure-stdlib LEAF of the hardware-key law:
  :mod:`brutex.config.hardware_envelope` imports :func:`slugify` /
  :func:`format_hardware_key` / :data:`KEY_SLUG_RE` FROM here (a strict
  one-direction import DAG — CodeQL py/cyclic-import 670/671), and the
  gate CLI's ``key`` subcommand resolves the runtime hardware key itself
  via a lazy ``brutex.config.hardware_envelope.hardware_key`` import
  (only on the bench runner, where brutex is installed).
* **DYNAMIC registry** — adding hot path #7 is one
  :data:`REQUIRED_ENTRY_KEYS` member + one :data:`ENTRY_WORKLOADS` row +
  one row in ``tests/bench/_abs_baseline_registry.py``; existing entries
  keep gating while the new one abstains per-entry until auto-blessed.

Honest envelope (Golden Rule #10): the gate is dormant until the m4pro
runner is online AND the first bless PR merges; until then compare ABSTAINS
green-but-loud (never a fake pass, never a false red). This gate catches
STRICTLY-greater-than-``tolerance_pct`` drift ONLY (the boundary law is a
strict ``>`` — a regression landing EXACTLY at the tolerance threshold
passes; boundary-pinned): at-or-below-tolerance drift passes and can
ACCUMULATE across merges until a ledgered re-bless tightens the number —
the complementary gates do NOT plug that hole (the 1.1x ratio tiers guard
complexity shape, not constant factors, and the 50 M ops/s floor covers
ONLY the mask-AND canary op — 5 of the 6 registry entries carry no
absolute floor below this gate's tolerance). The structural cap on
``tolerance_pct`` (failure multiplier ``1 + tolerance/100`` FLOAT-EXACTLY
below 2.0) bounds the worst case: a 2x single-step slowdown can NEVER
pass under any governable config. These are micro-benchmarks of the keyed
hot paths, not end-to-end sweep wall time.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

#: Baseline document schema version (bump only with a ledgered migration).
SCHEMA_VERSION: Final = 1

#: The ONE unit — nanoseconds per operation; lower is better. ops/s is a
#: derived DISPLAY quantity only, so a unit-mismatch is a schema error, not
#: a runtime branch.
UNIT: Final = "ns_per_op"

#: The ONE statistic — best-of-N minimum (DEC-CANARY-MINTIME-001 idiom).
#: All raw round samples are recorded so any other stat is re-derivable.
STAT: Final = "min"

#: The standing first-capture-per-cell authorization (ledger #1533). It can
#: bless an ADDED cell; it can NEVER authorize modifying an existing one.
BOOTSTRAP_DECISION_ID: Final = "DEC-BENCH-ABS-BASELINE-001"

#: Mirrors ``scripts/ci/validate_dec_ids.py`` DEC_ID_PATTERN (anchored).
DEC_ID_RE: Final = re.compile(r"^DEC-[A-Z][A-Z0-9_-]*-\d{3,}$")

#: Hardware-key slug law — lowercase alnum plus ``.`` and ``-``.
KEY_SLUG_RE: Final = re.compile(r"^[a-z0-9][a-z0-9.-]*$")

#: Workload-hash shape law.
WORKLOAD_HASH_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Provenance run-URL SHAPE law (a GitHub Actions run URL). The shape alone
#: does NOT pin the repository — govern() additionally requires the URL's
#: ``owner/repo`` slug to equal the injected *repo_slug* (THIS repository),
#: so citing another repo's run is a breach, never a verified provenance.
RUN_URL_RE: Final = re.compile(r"^https://github\.com/([^/]+/[^/]+)/actions/runs/(\d+)$")

#: The ``owner/repo`` slug shape govern() requires for its repo pin.
REPO_SLUG_RE: Final = re.compile(r"^[^/\s]+/[^/\s]+$")

#: Default per-entry regression tolerance — threshold multiplier 1.5x.
DEFAULT_TOLERANCE_PCT: Final = 50.0

#: STRUCTURAL cap (strict): ``tolerance_pct < 100`` bounds the failure
#: multiplier, and the schema ADDITIONALLY requires
#: :func:`tolerance_multiplier` strictly below
#: :data:`TOLERANCE_MULTIPLIER_MAX_EXCLUSIVE` FLOAT-EXACTLY (the raw range
#: alone admits ``math.nextafter(100, 0)``, whose multiplier rounds to
#: exactly 2.0 in IEEE double), so a 2x slowdown fails under EVERY
#: governable config (synthesis decision S6; review round 2).
TOLERANCE_PCT_MAX_EXCLUSIVE: Final = 100.0

#: The failure multiplier a schema-legal tolerance must stay strictly below
#: FLOAT-EXACTLY — the ACTUAL 2x-must-fail invariant the cap exists for.
TOLERANCE_MULTIPLIER_MAX_EXCLUSIVE: Final = 2.0

#: A measured speedup beyond this ratio WARNs "baseline stale" (S3 tier 1).
DEFAULT_IMPROVEMENT_WARN_RATIO: Final = 2.0

#: A measured speedup beyond this ratio FAILS — forces a conscious
#: ledgered re-bless (S3 tier 2; hollowing guard).
DEFAULT_SUSPICIOUS_SPEEDUP_RATIO: Final = 4.0

#: Capture folds at least this many PROCESS-FRESH measurement runs.
MIN_CAPTURE_RUNS: Final = 3

#: Timed rounds per entry per run (all samples recorded).
MEASURE_ROUNDS: Final = 7

#: Untimed warmup rounds per entry per run.
MEASURE_WARMUP_ROUNDS: Final = 2

#: A committed baseline cell must carry at least this many raw samples
#: (>= one full capture is 3 runs x 7 rounds; 3 is the honest floor that
#: still lets a hand-trimmed golden fixture stay legible).
MIN_BASELINE_SAMPLES: Final = 3

#: Wheels every fragment must prove imported (a pure-Python fallback would
#: not be the shipped stack — comparing it would be a silent lie).
REQUIRED_WHEELS: Final = ("fno_math", "trading_core")

#: The closed registry of gated hot-path entries (Golden Rule #5 set).
REQUIRED_ENTRY_KEYS: Final = frozenset(
    {
        "bar_lookup",
        "mask_eval_scalar",
        "mask_and_vector_1m",
        "dedup_bloom_probe",
        "dedup_lmdb_contains",
        "result_append",
    }
)

#: Pure DATA — the declared measured workload per entry. Changing ANY value
#: here changes ``workload_hash(entry)`` and STRUCTURAL-fails every blessed
#: baseline for that entry until re-blessed (the S11 recipe: the same PR
#: removes the affected baseline cells with a ledgered REMOVED note). The
#: tests/bench registry builds its ops FROM this mapping (test-pinned).
ENTRY_WORKLOADS: Final[Mapping[str, Mapping[str, int | str]]] = {
    "bar_lookup": {"dict_size": 100_000, "ops_per_round": 10_000, "seed": 42},
    "mask_eval_scalar": {"ops_per_round": 10_000, "mask_bits": "3,17,33,47"},
    "mask_and_vector_1m": {
        "array_len": 1_000_000,
        "ops_per_round": 1_000_000,
        "seed": 42,
        "mask_bits": "3,17,33,47",
    },
    "dedup_bloom_probe": {
        "capacity": 1_000_000,
        "fill": 100_000,
        "ops_per_round": 10_000,
        "seed": 42,
    },
    "dedup_lmdb_contains": {
        "fill": 100_000,
        "map_size_mb": 64,
        "key_bytes": 16,
        "ops_per_round": 10_000,
        "seed": 42,
    },
    "result_append": {"rows_per_round": 1_000, "ops_per_round": 1_000, "seed": 42},
}

#: The bench-m4pro workflow path a cited provenance run must belong to.
BENCH_WORKFLOW_PATH: Final = ".github/workflows/bench-m4pro.yml"


# ────────────────────────────────────────────────────────────────────────
# Exceptions
# ────────────────────────────────────────────────────────────────────────


class BenchBaselineError(ValueError):
    """Base error for every bench-baseline schema/compare/capture/govern fault."""


class BaselineSchemaError(BenchBaselineError):
    """A committed baseline file violates the schema laws (exit 2 tier)."""


class FragmentError(BenchBaselineError):
    """A measured fragment is malformed or carries invalid numbers."""


class NoiseRefusal(BenchBaselineError):
    """Capture refused — cross-run spread exceeds the guard band."""


class OverwriteRefusal(BenchBaselineError):
    """Capture refused — *replace_entries* is inconsistent with the blessed
    baseline (no existing baseline to replace into, or it names an unblessed
    entry). An already-blessed entry NOT listed for replacement is never a
    refusal — it is CARRIED field-for-field unchanged (byte-identical, so no
    silent-bump surface), keeping the full-registry measurement suite
    additive-safe (S4)."""


class GovernanceError(BenchBaselineError):
    """Re-baseline governance setup/verification failed (fail-closed)."""


# ────────────────────────────────────────────────────────────────────────
# Pure hardware-key formatting (consumed by brutex.config.hardware_envelope
# — the runtime detector — AND by parse-time re-derivation here; ONE law).
# ────────────────────────────────────────────────────────────────────────


def slugify(text: str) -> str:
    """Lowercase *text*, map every non-``[a-z0-9]`` run to ``-``, strip edges.

    An empty result degrades to the honest ``"unknown"`` — never a guess.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug if slug else "unknown"


def format_hardware_key(
    *,
    model_slug: str,
    physical_cores: int,
    performance_cores: int,
    efficiency_cores: int,
    total_ram_gib: int,
    arch: str,
) -> str:
    """Render the canonical hardware key, e.g. ``mac16-8-14c10p4e-48g-arm64``.

    ALL fields are KEY-DEFINING — a fingerprint difference always yields a
    different key (and therefore a different baseline file); numbers are
    never compared across keys. ``total_ram_gib == 0`` (the honest
    RAM-unknown sentinel) renders as ``unkg``.
    """
    ram = str(total_ram_gib) if total_ram_gib > 0 else "unk"
    raw = f"{model_slug}-{physical_cores}c{performance_cores}p{efficiency_cores}e-{ram}g-{arch}"
    return slugify(raw)


def tolerance_multiplier(tolerance_pct: float) -> float:
    """The exact IEEE-double failure multiplier ``1 + tolerance_pct / 100``.

    ONE source of truth (review round 2): the schema cap validates THIS
    value strictly below :data:`TOLERANCE_MULTIPLIER_MAX_EXCLUSIVE`, and the
    compare threshold is ``blessed * tolerance_multiplier(...)`` — so the
    float-exact rejection at parse time provably covers the exact arithmetic
    the verdict law runs (a raw ``tolerance < 100`` range check alone admits
    ``math.nextafter(100, 0)``, whose multiplier rounds to exactly 2.0).
    """
    return 1.0 + tolerance_pct / 100.0


def workload_hash(entry_id: str) -> str:
    """The ``sha256:``-prefixed canonical-JSON digest of an entry's workload.

    Changing any declared workload parameter moves this hash, which
    STRUCTURAL-fails every blessed baseline cell for the entry until it is
    re-blessed (S11).
    """
    if entry_id not in ENTRY_WORKLOADS:
        raise BenchBaselineError(f"unknown entry id {entry_id!r} — not in ENTRY_WORKLOADS")
    canonical = json.dumps(ENTRY_WORKLOADS[entry_id], sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ────────────────────────────────────────────────────────────────────────
# Frozen data model
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a blessed value was measured (live-verifiable)."""

    run_url: str
    commit: str
    captured_utc: str
    runner_name: str


@dataclass(frozen=True, slots=True)
class EntryRecord:
    """One blessed baseline cell (all thresholds are governed data)."""

    value_ns_per_op: float
    samples_ns_per_op: tuple[float, ...]
    spread_pct: float
    tolerance_pct: float
    improvement_warn_ratio: float
    suspicious_speedup_ratio: float
    workload_hash: str
    decision_id: str
    provenance: Provenance
    note: str = ""


@dataclass(frozen=True, slots=True)
class Baseline:
    """One parsed per-hardware baseline document."""

    schema_version: int
    hardware_key: str
    fingerprint: Mapping[str, int | str]
    context: Mapping[str, str]
    entries: Mapping[str, EntryRecord]


@dataclass(frozen=True, slots=True)
class Fragment:
    """One measured entry emitted by the bench runner's measurement step."""

    entry: str
    value_ns_per_op: float
    samples_ns_per_op: tuple[float, ...]
    spread_pct: float
    ops_per_round: int
    rounds: int
    workload_hash: str
    wheels: Mapping[str, bool]
    context: Mapping[str, str]


class Verdict(Enum):
    """Per-entry compare outcome."""

    OK = "OK"
    OK_IMPROVEMENT_WARN = "OK_IMPROVEMENT_WARN"
    REGRESSION = "REGRESSION"
    SUSPICIOUS_SPEEDUP = "SUSPICIOUS_SPEEDUP"
    STRUCTURAL = "STRUCTURAL"
    ABSTAIN_NO_BASELINE = "ABSTAIN_NO_BASELINE"
    ABSTAIN_NEW_ENTRY = "ABSTAIN_NEW_ENTRY"


#: Verdicts that fail the compare (rc 1 numeric / rc 2 structural).
_FAIL_VERDICTS: Final = frozenset(
    {Verdict.REGRESSION, Verdict.SUSPICIOUS_SPEEDUP, Verdict.STRUCTURAL}
)

#: Verdicts that count as a genuine measured-vs-blessed comparison.
_COMPARED_VERDICTS: Final = frozenset(
    {
        Verdict.OK,
        Verdict.OK_IMPROVEMENT_WARN,
        Verdict.REGRESSION,
        Verdict.SUSPICIOUS_SPEEDUP,
    }
)


@dataclass(frozen=True, slots=True)
class EntryVerdict:
    """One entry's compare verdict (measured/blessed may be ``None`` for abstains)."""

    entry: str
    verdict: Verdict
    measured_ns_per_op: float | None = None
    blessed_ns_per_op: float | None = None
    threshold_ns_per_op: float | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CompareReport:
    """The whole compare outcome — pure data; the caller renders/logs it."""

    verdicts: tuple[EntryVerdict, ...]
    ok: bool
    failed: tuple[str, ...]
    abstained_entries: tuple[str, ...]
    warnings: tuple[str, ...]
    attestation_line: str
    baseline_missing: bool
    has_structural: bool
    has_numeric_failure: bool
    n_compared: int


@dataclass(frozen=True, slots=True)
class GovernReport:
    """Re-baseline governance outcome (breaches = fail-closed reasons)."""

    ok: bool
    breaches: tuple[str, ...]
    notes: tuple[str, ...] = ()


# ────────────────────────────────────────────────────────────────────────
# Parsing (fail-loud; every error names file + field)
# ────────────────────────────────────────────────────────────────────────


def _reject_constant(token: str) -> float:
    """``json.loads`` hook — NaN/Infinity literals are schema errors."""
    raise BaselineSchemaError(f"non-finite JSON literal {token!r} rejected")


def _load_json_object(text: str, *, where: str) -> dict[str, object]:
    """Parse *text* as a JSON object; every failure names *where*."""
    try:
        doc = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise BaselineSchemaError(f"{where}: invalid JSON at offset {exc.pos}: {exc.msg}") from exc
    if not isinstance(doc, dict):
        raise BaselineSchemaError(f"{where}: JSON root must be an object, got {type(doc).__name__}")
    return doc


def _require_keys(
    doc: Mapping[str, object], *, required: frozenset[str], optional: frozenset[str], where: str
) -> None:
    """Closed-key law: *doc* keys == *required* (+ any subset of *optional*)."""
    keys = set(doc)
    missing = sorted(required - keys)
    extra = sorted(keys - required - optional)
    if missing or extra:
        raise BaselineSchemaError(f"{where}: missing keys {missing}, unknown keys {extra}")


def _str_field(doc: Mapping[str, object], key: str, *, where: str) -> str:
    """A required string field."""
    value = doc[key]
    if not isinstance(value, str):
        raise BaselineSchemaError(f"{where}: {key} must be a string, got {type(value).__name__}")
    return value


def _int_field(doc: Mapping[str, object], key: str, *, where: str, minimum: int) -> int:
    """A required integer field (bool rejected) with a floor."""
    value = doc[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise BaselineSchemaError(f"{where}: {key} must be an integer, got {value!r}")
    if value < minimum:
        raise BaselineSchemaError(f"{where}: {key} must be >= {minimum}, got {value}")
    return value


def _finite_float(doc: Mapping[str, object], key: str, *, where: str) -> float:
    """A required finite float field (bool rejected)."""
    value = doc[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BaselineSchemaError(f"{where}: {key} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise BaselineSchemaError(f"{where}: {key} must be finite, got {number!r}")
    return number


def _positive_float(doc: Mapping[str, object], key: str, *, where: str) -> float:
    """A required finite float field that must be > 0."""
    number = _finite_float(doc, key, where=where)
    if number <= 0:
        raise BaselineSchemaError(f"{where}: {key} must be > 0, got {number!r}")
    return number


def _samples_field(doc: Mapping[str, object], key: str, *, where: str) -> tuple[float, ...]:
    """A required nonempty list of finite positive numbers."""
    raw = doc[key]
    if not isinstance(raw, list) or not raw:
        raise BaselineSchemaError(f"{where}: {key} must be a nonempty list, got {raw!r}")
    samples: list[float] = []
    for i, item in enumerate(raw):
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise BaselineSchemaError(f"{where}: {key}[{i}] must be a number, got {item!r}")
        number = float(item)
        if not math.isfinite(number) or number <= 0:
            raise BaselineSchemaError(f"{where}: {key}[{i}] must be finite and > 0, got {number!r}")
        samples.append(number)
    return tuple(samples)


def _str_mapping(doc: Mapping[str, object], key: str, *, where: str) -> dict[str, str]:
    """A required ``str -> str`` object field."""
    raw = doc[key]
    if not isinstance(raw, dict):
        raise BaselineSchemaError(f"{where}: {key} must be an object, got {type(raw).__name__}")
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(v, str):
            raise BaselineSchemaError(f"{where}: {key}.{k} must be a string, got {v!r}")
        out[str(k)] = v
    return out


def _parse_captured_utc(raw: str, *, where: str) -> None:
    """captured_utc must be tz-aware UTC ISO-8601."""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BaselineSchemaError(f"{where}: captured_utc {raw!r} is not ISO-8601") from exc
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise BaselineSchemaError(f"{where}: captured_utc {raw!r} must be tz-aware UTC")


def _parse_provenance(doc: Mapping[str, object], *, where: str) -> Provenance:
    """Parse + validate one provenance object."""
    raw = doc["provenance"]
    if not isinstance(raw, dict):
        raise BaselineSchemaError(f"{where}: provenance must be an object, got {raw!r}")
    pwhere = f"{where}.provenance"
    _require_keys(
        raw,
        required=frozenset({"run_url", "commit", "captured_utc", "runner_name"}),
        optional=frozenset(),
        where=pwhere,
    )
    run_url = _str_field(raw, "run_url", where=pwhere)
    if RUN_URL_RE.fullmatch(run_url) is None:
        raise BaselineSchemaError(f"{pwhere}: run_url {run_url!r} is not a GitHub Actions run URL")
    commit = _str_field(raw, "commit", where=pwhere)
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise BaselineSchemaError(f"{pwhere}: commit {commit!r} is not a 40-hex SHA")
    captured = _str_field(raw, "captured_utc", where=pwhere)
    _parse_captured_utc(captured, where=pwhere)
    runner = _str_field(raw, "runner_name", where=pwhere)
    if not runner:
        raise BaselineSchemaError(f"{pwhere}: runner_name must be nonempty")
    return Provenance(run_url=run_url, commit=commit, captured_utc=captured, runner_name=runner)


_ENTRY_REQUIRED: Final = frozenset(
    {
        "unit",
        "stat",
        "value_ns_per_op",
        "samples_ns_per_op",
        "spread_pct",
        "tolerance_pct",
        "improvement_warn_ratio",
        "suspicious_speedup_ratio",
        "workload_hash",
        "decision_id",
        "provenance",
    }
)


def _parse_entry(entry_id: str, doc: object, *, where: str) -> EntryRecord:
    """Parse + validate one baseline entry cell."""
    if not isinstance(doc, dict):
        raise BaselineSchemaError(f"{where}: entry must be an object, got {doc!r}")
    _require_keys(doc, required=_ENTRY_REQUIRED, optional=frozenset({"note"}), where=where)
    unit = _str_field(doc, "unit", where=where)
    if unit != UNIT:
        raise BaselineSchemaError(f"{where}: unit must be {UNIT!r}, got {unit!r}")
    stat = _str_field(doc, "stat", where=where)
    if stat != STAT:
        raise BaselineSchemaError(f"{where}: stat must be {STAT!r}, got {stat!r}")
    value = _positive_float(doc, "value_ns_per_op", where=where)
    samples = _samples_field(doc, "samples_ns_per_op", where=where)
    if len(samples) < MIN_BASELINE_SAMPLES:
        raise BaselineSchemaError(
            f"{where}: samples_ns_per_op needs >= {MIN_BASELINE_SAMPLES} samples"
        )
    if value != min(samples):
        raise BaselineSchemaError(
            f"{where}: value_ns_per_op {value!r} != min(samples) {min(samples)!r}"
        )
    spread = _finite_float(doc, "spread_pct", where=where)
    if spread < 0:
        raise BaselineSchemaError(f"{where}: spread_pct must be >= 0, got {spread!r}")
    tolerance = _finite_float(doc, "tolerance_pct", where=where)
    # The raw [0, 100) range check alone is NOT float-exact: the largest
    # double below 100 (math.nextafter(100.0, 0.0)) has a failure multiplier
    # 1 + t/100 that ROUNDS to exactly 2.0 in IEEE double, which would let an
    # exact-2x slowdown pass the strict-> threshold. The multiplier itself is
    # therefore validated float-exactly (review round 2).
    if not (
        0.0 <= tolerance < TOLERANCE_PCT_MAX_EXCLUSIVE
        and tolerance_multiplier(tolerance) < TOLERANCE_MULTIPLIER_MAX_EXCLUSIVE
    ):
        raise BaselineSchemaError(
            f"{where}: tolerance_pct must be in [0, {TOLERANCE_PCT_MAX_EXCLUSIVE}) with "
            f"failure multiplier 1 + tolerance_pct/100 FLOAT-EXACTLY below 2.0 — the "
            f"structural 2x-must-fail cap — got {tolerance!r}"
        )
    warn_ratio = _finite_float(doc, "improvement_warn_ratio", where=where)
    if warn_ratio < 1.0:
        raise BaselineSchemaError(
            f"{where}: improvement_warn_ratio must be >= 1, got {warn_ratio!r}"
        )
    suspicious = _finite_float(doc, "suspicious_speedup_ratio", where=where)
    if suspicious <= warn_ratio:
        raise BaselineSchemaError(
            f"{where}: suspicious_speedup_ratio {suspicious!r} must be > "
            f"improvement_warn_ratio {warn_ratio!r}"
        )
    whash = _str_field(doc, "workload_hash", where=where)
    if WORKLOAD_HASH_RE.fullmatch(whash) is None:
        raise BaselineSchemaError(f"{where}: workload_hash {whash!r} malformed")
    decision_id = _str_field(doc, "decision_id", where=where)
    if DEC_ID_RE.fullmatch(decision_id) is None:
        raise BaselineSchemaError(f"{where}: decision_id {decision_id!r} malformed")
    note = doc.get("note", "")
    if not isinstance(note, str):
        raise BaselineSchemaError(f"{where}: note must be a string, got {note!r}")
    return EntryRecord(
        value_ns_per_op=value,
        samples_ns_per_op=samples,
        spread_pct=spread,
        tolerance_pct=tolerance,
        improvement_warn_ratio=warn_ratio,
        suspicious_speedup_ratio=suspicious,
        workload_hash=whash,
        decision_id=decision_id,
        provenance=_parse_provenance(doc, where=where),
        note=note,
    )


_FINGERPRINT_STR_FIELDS: Final = ("model_slug", "arch", "os_family")
_FINGERPRINT_INT_FIELDS: Final = (
    "physical_cores",
    "performance_cores",
    "efficiency_cores",
    "total_ram_gib",
)


def parse_baseline(text: str, *, filename: str) -> Baseline:
    """Parse + validate one committed baseline document (fail-loud).

    *filename* is the file NAME (e.g. ``mac16-8-14c10p4e-48g-arm64.json``);
    its stem must equal the embedded ``hardware_key``, which must itself
    re-derive from the embedded fingerprint fields — a hand-edited
    fingerprint/key mismatch is a schema error.
    """
    where = filename
    doc = _load_json_object(text, where=where)
    _require_keys(
        doc,
        required=frozenset({"schema_version", "hardware_key", "fingerprint", "context", "entries"}),
        optional=frozenset(),
        where=where,
    )
    version = _int_field(doc, "schema_version", where=where, minimum=0)
    if version != SCHEMA_VERSION:
        raise BaselineSchemaError(
            f"{where}: schema_version {version} != supported {SCHEMA_VERSION}"
        )
    key = _str_field(doc, "hardware_key", where=where)
    if KEY_SLUG_RE.fullmatch(key) is None:
        raise BaselineSchemaError(f"{where}: hardware_key {key!r} fails the slug law")
    stem = filename[: -len(".json")] if filename.endswith(".json") else filename
    if stem != key:
        raise BaselineSchemaError(f"{where}: filename stem {stem!r} != hardware_key {key!r}")
    raw_fp = doc["fingerprint"]
    if not isinstance(raw_fp, dict):
        raise BaselineSchemaError(f"{where}: fingerprint must be an object, got {raw_fp!r}")
    fwhere = f"{where}.fingerprint"
    _require_keys(
        raw_fp,
        required=frozenset(_FINGERPRINT_STR_FIELDS) | frozenset(_FINGERPRINT_INT_FIELDS),
        optional=frozenset(),
        where=fwhere,
    )
    fingerprint: dict[str, int | str] = {}
    for name in _FINGERPRINT_STR_FIELDS:
        fingerprint[name] = _str_field(raw_fp, name, where=fwhere)
    for name in _FINGERPRINT_INT_FIELDS:
        fingerprint[name] = _int_field(raw_fp, name, where=fwhere, minimum=0)
    derived = format_hardware_key(
        model_slug=str(fingerprint["model_slug"]),
        physical_cores=int(fingerprint["physical_cores"]),
        performance_cores=int(fingerprint["performance_cores"]),
        efficiency_cores=int(fingerprint["efficiency_cores"]),
        total_ram_gib=int(fingerprint["total_ram_gib"]),
        arch=str(fingerprint["arch"]),
    )
    if derived != key:
        raise BaselineSchemaError(
            f"{where}: hardware_key {key!r} does not re-derive from the embedded "
            f"fingerprint (derived {derived!r})"
        )
    context = _str_mapping(doc, "context", where=where)
    if "python" not in context:
        raise BaselineSchemaError(f"{where}: context.python is required")
    raw_entries = doc["entries"]
    if not isinstance(raw_entries, dict):
        raise BaselineSchemaError(f"{where}: entries must be an object, got {raw_entries!r}")
    orphans = sorted(set(raw_entries) - REQUIRED_ENTRY_KEYS)
    if orphans:
        raise BaselineSchemaError(f"{where}: orphan entries {orphans} not in REQUIRED_ENTRY_KEYS")
    entries = {
        entry_id: _parse_entry(entry_id, raw_entries[entry_id], where=f"{where}.entries.{entry_id}")
        for entry_id in sorted(raw_entries)
    }
    return Baseline(
        schema_version=version,
        hardware_key=key,
        fingerprint=fingerprint,
        context=context,
        entries=entries,
    )


_FRAGMENT_REQUIRED: Final = frozenset(
    {
        "entry",
        "unit",
        "stat",
        "value_ns_per_op",
        "samples_ns_per_op",
        "spread_pct",
        "ops_per_round",
        "rounds",
        "workload_hash",
        "wheels",
        "context",
    }
)


def parse_fragment(text: str, *, entry_hint: str) -> Fragment:
    """Parse + validate one measured fragment (fail-loud)."""
    where = f"fragment[{entry_hint}]"
    try:
        doc = _load_json_object(text, where=where)
    except BaselineSchemaError as exc:
        raise FragmentError(str(exc)) from exc
    try:
        _require_keys(doc, required=_FRAGMENT_REQUIRED, optional=frozenset(), where=where)
        entry = _str_field(doc, "entry", where=where)
        if entry != entry_hint:
            raise FragmentError(f"{where}: entry {entry!r} != expected {entry_hint!r}")
        unit = _str_field(doc, "unit", where=where)
        if unit != UNIT:
            raise FragmentError(f"{where}: unit must be {UNIT!r}, got {unit!r}")
        stat = _str_field(doc, "stat", where=where)
        if stat != STAT:
            raise FragmentError(f"{where}: stat must be {STAT!r}, got {stat!r}")
        value = _positive_float(doc, "value_ns_per_op", where=where)
        samples = _samples_field(doc, "samples_ns_per_op", where=where)
        if value != min(samples):
            raise FragmentError(
                f"{where}: value_ns_per_op {value!r} != min(samples) {min(samples)!r}"
            )
        spread = _finite_float(doc, "spread_pct", where=where)
        if spread < 0:
            raise FragmentError(f"{where}: spread_pct must be >= 0, got {spread!r}")
        ops = _int_field(doc, "ops_per_round", where=where, minimum=1)
        rounds = _int_field(doc, "rounds", where=where, minimum=1)
        whash = _str_field(doc, "workload_hash", where=where)
        if WORKLOAD_HASH_RE.fullmatch(whash) is None:
            raise FragmentError(f"{where}: workload_hash {whash!r} malformed")
        raw_wheels = doc["wheels"]
        if not isinstance(raw_wheels, dict):
            raise FragmentError(f"{where}: wheels must be an object, got {raw_wheels!r}")
        wheels: dict[str, bool] = {}
        for k, v in raw_wheels.items():
            if not isinstance(v, bool):
                raise FragmentError(f"{where}: wheels.{k} must be a boolean, got {v!r}")
            wheels[str(k)] = v
        context = _str_mapping(doc, "context", where=where)
    except BaselineSchemaError as exc:
        raise FragmentError(str(exc)) from exc
    return Fragment(
        entry=entry,
        value_ns_per_op=value,
        samples_ns_per_op=samples,
        spread_pct=spread,
        ops_per_round=ops,
        rounds=rounds,
        workload_hash=whash,
        wheels=wheels,
        context=context,
    )


# ────────────────────────────────────────────────────────────────────────
# Compare (pure verdict law)
# ────────────────────────────────────────────────────────────────────────


def _major_minor(version: str) -> str:
    """``"3.12.8" -> "3.12"`` (best-effort; a short string passes through)."""
    parts = version.split(".")
    return ".".join(parts[:2])


def _structural(entry: str, detail: str) -> EntryVerdict:
    """One STRUCTURAL failure verdict."""
    return EntryVerdict(entry=entry, verdict=Verdict.STRUCTURAL, detail=detail)


def _entry_verdict(entry: str, frag: Fragment, rec: EntryRecord) -> EntryVerdict:
    """The per-entry numeric/structural verdict law (S3/S6 boundaries)."""
    if frag.workload_hash != rec.workload_hash:
        return _structural(
            entry,
            f"workload hash drift: measured {frag.workload_hash} != blessed "
            f"{rec.workload_hash} — the workload-change PR must REMOVE this baseline "
            "entry with a ledgered note (S11 recipe, docs/runbooks/bench-baseline.md)",
        )
    for wheel in REQUIRED_WHEELS:
        if frag.wheels.get(wheel) is not True:
            return _structural(entry, f"required wheel {wheel!r} not imported on the runner")
    measured = frag.value_ns_per_op
    blessed = rec.value_ns_per_op
    threshold = blessed * tolerance_multiplier(rec.tolerance_pct)
    if measured > threshold:
        return EntryVerdict(
            entry=entry,
            verdict=Verdict.REGRESSION,
            measured_ns_per_op=measured,
            blessed_ns_per_op=blessed,
            threshold_ns_per_op=threshold,
            detail=(
                f"measured {measured:.3f} ns/op > threshold {threshold:.3f} ns/op "
                f"(blessed {blessed:.3f} x (1 + {rec.tolerance_pct}/100))"
            ),
        )
    if measured * rec.suspicious_speedup_ratio < blessed:
        return EntryVerdict(
            entry=entry,
            verdict=Verdict.SUSPICIOUS_SPEEDUP,
            measured_ns_per_op=measured,
            blessed_ns_per_op=blessed,
            threshold_ns_per_op=threshold,
            detail=(
                f"measured {measured:.3f} ns/op is > {rec.suspicious_speedup_ratio}x "
                f"faster than blessed {blessed:.3f} — a hollowed benchmark or a genuine "
                "win; either way a ledgered re-bless is REQUIRED"
            ),
        )
    verdict = Verdict.OK
    detail = ""
    if measured * rec.improvement_warn_ratio < blessed:
        verdict = Verdict.OK_IMPROVEMENT_WARN
        detail = (
            f"measured {measured:.3f} ns/op is > {rec.improvement_warn_ratio}x faster "
            f"than blessed {blessed:.3f} — baseline stale, a ledgered ratchet re-bless "
            "is recommended"
        )
    return EntryVerdict(
        entry=entry,
        verdict=verdict,
        measured_ns_per_op=measured,
        blessed_ns_per_op=blessed,
        threshold_ns_per_op=threshold,
        detail=detail,
    )


def _finish_report(
    verdicts: list[EntryVerdict],
    warnings: list[str],
    *,
    baseline: Baseline | None,
    baseline_missing: bool,
) -> CompareReport:
    """Fold verdicts into the frozen :class:`CompareReport`."""
    failed = tuple(v.entry for v in verdicts if v.verdict in _FAIL_VERDICTS)
    abstained = tuple(
        v.entry
        for v in verdicts
        if v.verdict in (Verdict.ABSTAIN_NO_BASELINE, Verdict.ABSTAIN_NEW_ENTRY)
    )
    has_structural = any(v.verdict is Verdict.STRUCTURAL for v in verdicts)
    has_numeric = any(
        v.verdict in (Verdict.REGRESSION, Verdict.SUSPICIOUS_SPEEDUP) for v in verdicts
    )
    compared = [v for v in verdicts if v.verdict in _COMPARED_VERDICTS]
    attestation = ""
    if compared and baseline is not None:
        cells = [baseline.entries[v.entry] for v in compared]
        oldest = min(cell.provenance.captured_utc for cell in cells)[:10]
        ids = ", ".join(sorted({cell.decision_id for cell in cells}))
        max_spread = max(cell.spread_pct for cell in cells)
        attestation = (
            f"BENCH-ABS GATE RAN: compared {len(compared)}/{len(REQUIRED_ENTRY_KEYS)} "
            f"entries against tests/bench/baselines/{baseline.hardware_key}.json "
            f"(captured {oldest}, decision ids {ids}, max blessed spread {max_spread}%)"
        )
    return CompareReport(
        verdicts=tuple(verdicts),
        ok=not failed,
        failed=failed,
        abstained_entries=abstained,
        warnings=tuple(warnings),
        attestation_line=attestation,
        baseline_missing=baseline_missing,
        has_structural=has_structural,
        has_numeric_failure=has_numeric,
        n_compared=len(compared),
    )


def compare(
    baseline: Baseline | None,
    fragments: Mapping[str, Fragment],
    *,
    runtime_key: str,
    runtime_python: str,
) -> CompareReport:
    """Compare measured *fragments* against the blessed *baseline* — pure.

    Verdict law (per entry, deterministic key order): the fragment set must
    EQUAL :data:`REQUIRED_ENTRY_KEYS` (a pytest skip/rename is a named
    STRUCTURAL fail — skip-honesty); ``baseline is None`` abstains every
    entry LOUDLY but green; a present baseline missing an entry abstains
    that entry only (S4 — the other cells keep gating); hash/wheel/python
    drift is STRUCTURAL; then the S6 boundary law — FAIL iff
    ``measured > blessed x (1 + tolerance_pct/100)`` (strict ``>``,
    equality passes) or the S3 suspicious-speedup bound trips.
    """
    verdicts: list[EntryVerdict] = []
    warnings: list[str] = []
    missing = sorted(REQUIRED_ENTRY_KEYS - set(fragments))
    extra = sorted(set(fragments) - REQUIRED_ENTRY_KEYS)
    if missing or extra:
        verdicts.append(
            _structural(
                "fragment-set",
                f"measured fragment set != registry: missing {missing}, extra {extra} "
                "(a skipped or renamed measurement test is a gate breach, never a pass)",
            )
        )
        return _finish_report(verdicts, warnings, baseline=None, baseline_missing=False)
    if baseline is None:
        for entry in sorted(REQUIRED_ENTRY_KEYS):
            verdicts.append(
                EntryVerdict(
                    entry=entry,
                    verdict=Verdict.ABSTAIN_NO_BASELINE,
                    measured_ns_per_op=fragments[entry].value_ns_per_op,
                    detail=f"no baseline file for hardware key {runtime_key!r}",
                )
            )
        warnings.append(
            f"NO BASELINE for {runtime_key!r} — compare ABSTAINS (green but loud); "
            "capture will run and a bless PR will open (docs/runbooks/bench-baseline.md)"
        )
        return _finish_report(verdicts, warnings, baseline=None, baseline_missing=True)
    if baseline.hardware_key != runtime_key:
        verdicts.append(
            _structural(
                "hardware-key",
                f"baseline hardware_key {baseline.hardware_key!r} != runtime key "
                f"{runtime_key!r} — foreign-hardware baselines are never compared",
            )
        )
        return _finish_report(verdicts, warnings, baseline=baseline, baseline_missing=False)
    if _major_minor(baseline.context["python"]) != _major_minor(runtime_python):
        verdicts.append(
            _structural(
                "context.python",
                f"python drift: baseline {baseline.context['python']} vs runtime "
                f"{runtime_python} — re-bless on the new interpreter",
            )
        )
        return _finish_report(verdicts, warnings, baseline=baseline, baseline_missing=False)
    for soft_key in ("numpy", "os_version"):
        blessed_value = baseline.context.get(soft_key)
        if blessed_value is None:
            continue
        runtime_values = {
            frag.context[soft_key] for frag in fragments.values() if soft_key in frag.context
        }
        drifted = sorted(runtime_values - {blessed_value})
        if drifted:
            warnings.append(
                f"soft context drift on {soft_key!r}: baseline {blessed_value!r} vs runtime "
                f"{drifted} — verdicts stay on the numbers (a stack perf regression IS a "
                "regression of the shipped stack)"
            )
    for entry in sorted(REQUIRED_ENTRY_KEYS):
        frag = fragments[entry]
        rec = baseline.entries.get(entry)
        if rec is None:
            verdicts.append(
                EntryVerdict(
                    entry=entry,
                    verdict=Verdict.ABSTAIN_NEW_ENTRY,
                    measured_ns_per_op=frag.value_ns_per_op,
                    detail="entry not yet blessed for this hardware key (additive bless)",
                )
            )
            continue
        result = _entry_verdict(entry, frag, rec)
        if result.verdict is Verdict.OK_IMPROVEMENT_WARN:
            warnings.append(f"{entry}: {result.detail}")
        verdicts.append(result)
    return _finish_report(verdicts, warnings, baseline=baseline, baseline_missing=False)


def _ops_per_sec(ns_per_op: float | None) -> str:
    """Derived display ops/s for one ns/op figure (``-`` when absent)."""
    if ns_per_op is None:
        return "-"
    return f"{1e9 / ns_per_op:,.0f}"


def render_report(report: CompareReport) -> str:
    """Render one compare report — table + GitHub annotations + remediation."""
    lines = [
        "entry | blessed ns/op | measured ns/op | blessed ops/s | measured ops/s | ratio | verdict"
    ]
    for v in report.verdicts:
        blessed = f"{v.blessed_ns_per_op:.3f}" if v.blessed_ns_per_op is not None else "-"
        measured = f"{v.measured_ns_per_op:.3f}" if v.measured_ns_per_op is not None else "-"
        ratio = (
            f"{v.measured_ns_per_op / v.blessed_ns_per_op:.3f}"
            if v.measured_ns_per_op is not None and v.blessed_ns_per_op is not None
            else "-"
        )
        lines.append(
            f"{v.entry} | {blessed} | {measured} | {_ops_per_sec(v.blessed_ns_per_op)} | "
            f"{_ops_per_sec(v.measured_ns_per_op)} | {ratio} | {v.verdict.value}"
        )
    for v in report.verdicts:
        if v.verdict in _FAIL_VERDICTS:
            lines.append(f"::error::bench-abs {v.entry}: {v.verdict.value} — {v.detail}")
    for warning in report.warnings:
        lines.append(f"::warning::bench-abs: {warning}")
    if report.abstained_entries and not report.baseline_missing:
        lines.append(
            "::warning::bench-abs: abstained entries "
            f"{', '.join(report.abstained_entries)} (additive bless pending)"
        )
    if not report.ok:
        lines.append(
            "remediation: see docs/runbooks/bench-baseline.md — a numeric red is "
            "re-measured once in a fresh process before failing; a re-bless is the "
            "workflow_dispatch capture (capture_candidate=true + decision_id + "
            "replace_entries) plus a human-written MIGRATION.md entry in the same PR. "
            "KNOWN LIMITATION: a STANDING red — numeric (e.g. a reproducing "
            "SUSPICIOUS_SPEEDUP) and STRUCTURAL alike — fails this compare step "
            "BEFORE capture can run, so that dispatch cannot mint a candidate; "
            "recover REMOVAL-FIRST (remove the affected cell(s)/file in a governed "
            "PR whose MIGRATION.md added lines name them, then the next scheduled "
            "run abstains and auto-blesses additively)"
        )
    if report.attestation_line:
        lines.append(report.attestation_line)
    else:
        lines.append("::notice::bench-abs: 0 entries compared this run")
    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────────────────────
# Capture (measured bless — fabrication-free by construction)
# ────────────────────────────────────────────────────────────────────────


def fold_capture(
    runs: Sequence[Mapping[str, Fragment]],
    *,
    existing: Baseline | None,
    decision_id: str,
    replace_entries: frozenset[str],
    fingerprint: Mapping[str, int | str],
    context: Mapping[str, str],
    provenance: Provenance,
) -> Baseline:
    """Fold >= :data:`MIN_CAPTURE_RUNS` process-fresh runs into a candidate baseline.

    Per NEW/replaced entry: value = min over ALL runs' samples; samples =
    every round of every run (order preserved); ``spread_pct`` = cross-run
    spread of the per-run minima. :class:`NoiseRefusal` when the spread
    exceeds ``tolerance_pct / 2`` (guard-band-coupled — a hot/loaded machine
    can never mint a baseline). An already-blessed entry NOT listed in
    *replace_entries* is CARRIED field-for-field unchanged (byte-identical —
    never a refusal, never a re-mint), because the measurement suite always
    emits the FULL registry: an additive bless on a partial baseline and a
    single-entry re-bless both fold full-registry runs. A nonempty
    *replace_entries* REQUIRES a fresh (non-bootstrap, non-current)
    *decision_id* — the bootstrap id authorizes first capture only, never a
    bump. The candidate's top-level ``context``/``fingerprint`` are carried
    from *existing* while any blessed cell is carried (their values were
    measured under that context), and refreshed from the FRESH measurement's
    *fingerprint*/*context* on a first capture OR a FULL re-bless
    (*replace_entries* covering every blessed cell) — so the removal-first
    python-drift recovery (a ledgered baseline delete, then the
    abstain-armed FIRST capture; ``docs/runbooks/bench-baseline.md``) and
    the proactive still-green FULL re-bless both yield a baseline whose
    next compare is OK instead of a permanent STRUCTURAL red.
    """
    if len(runs) < MIN_CAPTURE_RUNS:
        raise BenchBaselineError(
            f"capture requires >= {MIN_CAPTURE_RUNS} process-fresh runs, got {len(runs)}"
        )
    if DEC_ID_RE.fullmatch(decision_id) is None:
        raise GovernanceError(f"capture decision_id {decision_id!r} malformed")
    measured_keys = set(runs[0])
    for i, run in enumerate(runs[1:], start=2):
        if set(run) != measured_keys:
            raise FragmentError(
                f"run {i} measured entries {sorted(run)} != run 1 {sorted(measured_keys)}"
            )
    orphans = sorted(measured_keys - REQUIRED_ENTRY_KEYS)
    if orphans:
        raise FragmentError(f"measured entries {orphans} not in REQUIRED_ENTRY_KEYS")
    if replace_entries:
        if decision_id == BOOTSTRAP_DECISION_ID:
            raise GovernanceError(
                "replace_entries requires a NEW decision id — the bootstrap id "
                f"{BOOTSTRAP_DECISION_ID} authorizes first capture only"
            )
        if existing is None:
            raise OverwriteRefusal("replace_entries given but no existing baseline")
        for entry in sorted(replace_entries):
            current = existing.entries.get(entry)
            if current is None:
                raise OverwriteRefusal(f"replace_entries names {entry!r} which is not blessed")
            if current.decision_id == decision_id:
                raise GovernanceError(
                    f"replace decision_id {decision_id!r} equals the current blessed id "
                    f"for {entry!r} — a re-bless needs a NEW ledger entry"
                )
    entries: dict[str, EntryRecord] = {}
    if existing is not None:
        entries.update(existing.entries)
    for entry in sorted(measured_keys):
        current = existing.entries.get(entry) if existing is not None else None
        if current is not None and entry not in replace_entries:
            # Already blessed and not listed for replacement: CARRY the cell
            # field-for-field unchanged (it is already in `entries` via the
            # update above — byte-identical, so no silent bump is possible;
            # its fresh measurement is deliberately IGNORED, incl. its noise).
            continue
        frags = [run[entry] for run in runs]
        hashes = {f.workload_hash for f in frags}
        if len(hashes) > 1:
            raise FragmentError(f"{entry}: cross-run workload hash disagreement {sorted(hashes)}")
        for f in frags:
            for sample in (f.value_ns_per_op, *f.samples_ns_per_op):
                if not math.isfinite(sample) or sample <= 0:
                    raise FragmentError(f"{entry}: invalid sample {sample!r} in capture run")
        run_mins = [f.value_ns_per_op for f in frags]
        all_samples = tuple(s for f in frags for s in f.samples_ns_per_op)
        value = min(all_samples)
        spread_pct = (max(run_mins) - min(run_mins)) / min(run_mins) * 100.0
        if current is not None:
            tolerance = current.tolerance_pct
            warn_ratio = current.improvement_warn_ratio
            suspicious = current.suspicious_speedup_ratio
        else:
            tolerance = DEFAULT_TOLERANCE_PCT
            warn_ratio = DEFAULT_IMPROVEMENT_WARN_RATIO
            suspicious = DEFAULT_SUSPICIOUS_SPEEDUP_RATIO
        if spread_pct > tolerance / 2.0:
            raise NoiseRefusal(
                f"{entry}: cross-run spread {spread_pct:.2f}% > guard band "
                f"{tolerance / 2.0:.2f}% (tolerance/2) — quiet the machine and "
                "re-dispatch; a noisy run is never enshrined"
            )
        entries[entry] = EntryRecord(
            value_ns_per_op=value,
            samples_ns_per_op=all_samples,
            spread_pct=spread_pct,
            tolerance_pct=tolerance,
            improvement_warn_ratio=warn_ratio,
            suspicious_speedup_ratio=suspicious,
            workload_hash=frags[0].workload_hash,
            decision_id=decision_id,
            provenance=provenance,
            note=f"measured min over {len(runs)} process-fresh runs x {frags[0].rounds} rounds",
        )
    blessed_keys = frozenset(existing.entries) if existing is not None else frozenset()
    full_replace = existing is not None and blessed_keys <= replace_entries
    if existing is not None and not full_replace:
        # A blessed cell is being CARRIED: its value was measured under the
        # EXISTING context/fingerprint, so those stay (an interpreter drift
        # keeps an honest STRUCTURAL red until every cell is re-blessed).
        key = existing.hardware_key
        out_fingerprint: Mapping[str, int | str] = existing.fingerprint
        out_context: Mapping[str, str] = existing.context
    else:
        # First capture OR a FULL re-bless (every blessed cell replaced):
        # the candidate's context/fingerprint come from the FRESH
        # measurement, so the removal-first python-drift recovery (a first
        # capture after the ledgered baseline delete) refreshes the gate.
        key = format_hardware_key(
            model_slug=str(fingerprint["model_slug"]),
            physical_cores=int(fingerprint["physical_cores"]),
            performance_cores=int(fingerprint["performance_cores"]),
            efficiency_cores=int(fingerprint["efficiency_cores"]),
            total_ram_gib=int(fingerprint["total_ram_gib"]),
            arch=str(fingerprint["arch"]),
        )
        out_fingerprint = dict(fingerprint)
        out_context = dict(context)
    return Baseline(
        schema_version=SCHEMA_VERSION,
        hardware_key=key,
        fingerprint=out_fingerprint,
        context=out_context,
        entries=entries,
    )


def dump_baseline(baseline: Baseline) -> str:
    """Serialize deterministically (sort_keys, indent 2, trailing newline).

    The byte-stable diff surface the governance gate depends on — the
    literal value token a MODIFIED-cell ledger entry must quote is exactly
    ``json.dumps(value)`` of this document.
    """
    doc: dict[str, object] = {
        "schema_version": baseline.schema_version,
        "hardware_key": baseline.hardware_key,
        "fingerprint": dict(baseline.fingerprint),
        "context": dict(baseline.context),
        "entries": {
            entry_id: {
                "unit": UNIT,
                "stat": STAT,
                "value_ns_per_op": rec.value_ns_per_op,
                "samples_ns_per_op": list(rec.samples_ns_per_op),
                "spread_pct": rec.spread_pct,
                "tolerance_pct": rec.tolerance_pct,
                "improvement_warn_ratio": rec.improvement_warn_ratio,
                "suspicious_speedup_ratio": rec.suspicious_speedup_ratio,
                "workload_hash": rec.workload_hash,
                "decision_id": rec.decision_id,
                "provenance": {
                    "run_url": rec.provenance.run_url,
                    "commit": rec.provenance.commit,
                    "captured_utc": rec.provenance.captured_utc,
                    "runner_name": rec.provenance.runner_name,
                },
                "note": rec.note,
            }
            for entry_id, rec in baseline.entries.items()
        },
    }
    return json.dumps(doc, sort_keys=True, indent=2) + "\n"


def render_pr_body(
    baseline: Baseline,
    *,
    added: Sequence[str],
    replaced: Sequence[str],
    run_url: str,
    carried: Sequence[str] = (),
) -> str:
    """The bless PR body (machine-captured numbers, committed verbatim)."""
    rows = [
        f"| `{entry_id}` | {rec.value_ns_per_op} | {rec.spread_pct:.2f}% | {rec.decision_id} |"
        for entry_id, rec in sorted(baseline.entries.items())
    ]
    cited_ids = ", ".join(sorted({rec.decision_id for rec in baseline.entries.values()}))
    parts = [
        f"## Bless absolute benchmark baseline for `{baseline.hardware_key}`",
        "",
        # Honest headline: name the id(s) the cells ACTUALLY cite — a re-bless
        # under a NEW DEC id must not headline the bootstrap id alone.
        (
            f"Governing decision id(s): {cited_ids} (first captures ride the standing "
            + f"{BOOTSTRAP_DECISION_ID} authorization; a replaced cell cites its own "
            + "re-bless id — per cell in the table below)."
        ),
        "",
        # One PR-body LINE deliberately wrapped across source lines —
        # EXPLICIT concatenation so a missing list comma can never silently
        # merge two elements (CodeQL py/implicit-string-concatenation-in-list).
        (
            f"Every number below was MEASURED on the runner by run {run_url} and is "
            + "committed verbatim from the runner artifact — no edit surface exists "
            + "between measurement and this diff."
        ),
        "",
        "| entry | blessed ns/op | cross-run spread | decision id |",
        "|---|---|---|---|",
        *rows,
        "",
        f"Added entries: {', '.join(sorted(added)) if added else 'none'}.",
        f"Replaced entries: {', '.join(sorted(replaced)) if replaced else 'none'}.",
    ]
    if carried:
        parts.append(
            "Carried unchanged (already blessed, not replaced — byte-identical): "
            f"{', '.join(sorted(carried))}."
        )
    if replaced:
        parts += [
            "",
            # One PR-body LINE — explicit concatenation (see the note above).
            (
                "GOVERNANCE: this PR replaces blessed cells, so it must ALSO gain a "
                + "human-written docs/MIGRATION.md entry for the new decision id (naming "
                + "each replaced entry key and its literal new value token) before merge — "
                + "the bench-baseline governance gate enforces this mechanically."
            ),
        ]
    parts += [
        "",
        # One PR-body LINE — explicit concatenation (see the note above).
        (
            "NUDGE (bootstrap honesty): GITHUB_TOKEN-created PRs do not trigger CI: "
            + "close/reopen this PR (or push an empty commit) so the required "
            + "`CI OK (aggregate gate)` context reports, then merge."
        ),
        "",
    ]
    return "\n".join(parts)


# ────────────────────────────────────────────────────────────────────────
# Directory validation (the always-on shared-CI structural step)
# ────────────────────────────────────────────────────────────────────────


def validate_dir(files: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """Validate every committed baseline file — ``(errors, warnings)``.

    *files* maps file NAME to text for everything under the baselines dir.
    ``README.md`` is the one legal non-JSON resident. Workload-hash drift
    against the CURRENT :func:`workload_hash` is a WARNING, never an error
    (S11 — a workload-change PR may merge while the consequence is named).
    """
    errors: list[str] = []
    warnings: list[str] = []
    for name in sorted(files):
        if name == "README.md":
            continue
        if not name.endswith(".json"):
            errors.append(f"{name}: stray non-baseline file in tests/bench/baselines/")
            continue
        try:
            baseline = parse_baseline(files[name], filename=name)
        except BaselineSchemaError as exc:
            errors.append(str(exc))
            continue
        for entry_id, rec in baseline.entries.items():
            current = workload_hash(entry_id)
            if rec.workload_hash != current:
                warnings.append(
                    f"{name}: {entry_id} blessed against workload {rec.workload_hash} but "
                    f"the current declared workload is {current} — the next m4pro compare "
                    "will STRUCTURAL-fail until this entry is re-blessed (S11 recipe)"
                )
    return errors, warnings


# ────────────────────────────────────────────────────────────────────────
# Re-baseline governance (mechanical — a silent bump cannot reach main)
# ────────────────────────────────────────────────────────────────────────


def _entry_cells(baseline: Baseline) -> dict[str, EntryRecord]:
    """The per-entry cell mapping (plain dict for classification)."""
    return dict(baseline.entries)


def _value_token(value: float) -> str:
    """The literal value token as serialized by :func:`dump_baseline`."""
    return json.dumps(value)


def _ledgered_ids(migration_added_lines: str) -> set[str]:
    """Every DEC id token present in the PR's MIGRATION.md ADDED lines."""
    return set(re.findall(r"\bDEC-[A-Z][A-Z0-9_-]*-\d{3,}\b", migration_added_lines))


def _verify_cell_run(
    entry: str,
    cell: EntryRecord,
    *,
    repo_slug: str,
    run_verifier: Callable[[str], Mapping[str, object] | None],
    breaches: list[str],
) -> None:
    """Live-verify one cell's provenance run — a THIS-repo (*repo_slug*)
    bench run of the exact embedded commit that completed SUCCESSFULLY.

    The repo pin is the non-author-controlled leg: a run on a fork (which
    keeps ``bench-m4pro.yml`` at the required path with an attacker-chosen
    head_sha) is a breach BEFORE any API query."""
    match = RUN_URL_RE.fullmatch(cell.provenance.run_url)
    if match is None:
        breaches.append(f"{entry}: provenance run_url {cell.provenance.run_url!r} malformed")
        return
    if match.group(1) != repo_slug:
        breaches.append(
            f"{entry}: provenance run_url {cell.provenance.run_url} is on foreign repo "
            f"{match.group(1)!r}, not this repository ({repo_slug!r}) — a run you "
            "control on another repo can never bless a number here"
        )
        return
    run = run_verifier(cell.provenance.run_url)
    if run is None:
        breaches.append(
            f"{entry}: provenance run {cell.provenance.run_url} not found on this repo "
            "— a fabricated bump is refused"
        )
        return
    if run.get("path") != BENCH_WORKFLOW_PATH:
        breaches.append(
            f"{entry}: provenance run is {run.get('path')!r}, not {BENCH_WORKFLOW_PATH}"
        )
    if run.get("status") != "completed":
        breaches.append(f"{entry}: provenance run status {run.get('status')!r} != completed")
    if run.get("conclusion") != "success":
        breaches.append(
            f"{entry}: provenance run conclusion {run.get('conclusion')!r} != success "
            "— a failed/cancelled bench run is not valid provenance"
        )
    if run.get("head_sha") != cell.provenance.commit:
        breaches.append(
            f"{entry}: provenance run head_sha {run.get('head_sha')!r} != embedded "
            f"commit {cell.provenance.commit!r}"
        )


def _govern_added_cell(
    entry: str,
    cell: EntryRecord,
    *,
    ledgered: set[str],
    repo_slug: str,
    run_verifier: Callable[[str], Mapping[str, object] | None],
    breaches: list[str],
    notes: list[str],
) -> None:
    """ADDED-cell law: bootstrap id OR a DEC id ledgered in THIS PR; run verified."""
    if cell.decision_id != BOOTSTRAP_DECISION_ID and cell.decision_id not in ledgered:
        breaches.append(
            f"{entry}: added cell cites {cell.decision_id} which is neither the "
            f"bootstrap id nor ledgered in this PR's MIGRATION.md added lines"
        )
        return
    _verify_cell_run(entry, cell, repo_slug=repo_slug, run_verifier=run_verifier, breaches=breaches)
    notes.append(f"{entry}: ADDED under {cell.decision_id}")


def _govern_modified_cell(
    entry: str,
    base_cell: EntryRecord,
    head_cell: EntryRecord,
    *,
    ledgered: set[str],
    migration_added_lines: str,
    repo_slug: str,
    run_verifier: Callable[[str], Mapping[str, object] | None],
    breaches: list[str],
    notes: list[str],
) -> None:
    """MODIFIED-cell law (S7): new non-bootstrap id, ledgered with entry-key +
    literal value tokens, fresh live-verified run."""
    if head_cell.decision_id == BOOTSTRAP_DECISION_ID:
        breaches.append(
            f"{entry}: modified cell reuses the bootstrap id — bootstrap authorizes "
            "first capture only, never a bump"
        )
        return
    if head_cell.decision_id == base_cell.decision_id:
        breaches.append(
            f"{entry}: modified cell reuses the base decision id {base_cell.decision_id} "
            "— a re-bless requires a NEW human-written ledger entry"
        )
        return
    if head_cell.decision_id not in ledgered:
        breaches.append(
            f"{entry}: modified cell cites {head_cell.decision_id} which is not in this "
            "PR's MIGRATION.md added lines"
        )
        return
    value_token = _value_token(head_cell.value_ns_per_op)
    if entry not in migration_added_lines or value_token not in migration_added_lines:
        breaches.append(
            f"{entry}: the MIGRATION.md added lines must quote BOTH the entry key "
            f"'{entry}' and the literal new value token '{value_token}' (the measured "
            "justification, machine-checkable)"
        )
        return
    if head_cell.provenance.run_url == base_cell.provenance.run_url:
        breaches.append(
            f"{entry}: modified cell reuses the base provenance run "
            f"{base_cell.provenance.run_url} — a re-bless needs a FRESH measured run"
        )
        return
    _verify_cell_run(
        entry, head_cell, repo_slug=repo_slug, run_verifier=run_verifier, breaches=breaches
    )
    notes.append(f"{entry}: MODIFIED under {head_cell.decision_id}")


def _govern_modified_file(
    name: str,
    base_baseline: Baseline,
    head_baseline: Baseline,
    *,
    ledgered: set[str],
    migration_added_lines: str,
    repo_slug: str,
    run_verifier: Callable[[str], Mapping[str, object] | None],
    breaches: list[str],
    notes: list[str],
) -> None:
    """Classify a modified baseline file cell-by-cell against the base blob."""
    base_cells = _entry_cells(base_baseline)
    head_cells = _entry_cells(head_baseline)
    top_level_changed = (
        dict(base_baseline.fingerprint) != dict(head_baseline.fingerprint)
        or dict(base_baseline.context) != dict(head_baseline.context)
        or base_baseline.hardware_key != head_baseline.hardware_key
    )
    if top_level_changed:
        fresh = ledgered - {BOOTSTRAP_DECISION_ID}
        if not fresh:
            breaches.append(
                f"{name}: top-level fingerprint/context/hardware_key changed — this "
                "requires a NEW ledgered DEC id in the same PR's MIGRATION.md added lines"
            )
        else:
            notes.append(f"{name}: top-level change ledgered under {sorted(fresh)}")
    for entry in sorted(set(base_cells) | set(head_cells)):
        base_cell = base_cells.get(entry)
        head_cell = head_cells.get(entry)
        if head_cell is None:
            if entry not in migration_added_lines or not ledgered:
                breaches.append(
                    f"{entry}: cell REMOVED without a MIGRATION.md added line naming it "
                    "with a DEC id (the S11 workload-change recipe rides this)"
                )
            else:
                notes.append(f"{entry}: REMOVED with a ledgered note")
            continue
        if base_cell is None:
            _govern_added_cell(
                entry,
                head_cell,
                ledgered=ledgered,
                repo_slug=repo_slug,
                run_verifier=run_verifier,
                breaches=breaches,
                notes=notes,
            )
            continue
        if base_cell == head_cell:
            continue
        _govern_modified_cell(
            entry,
            base_cell,
            head_cell,
            ledgered=ledgered,
            migration_added_lines=migration_added_lines,
            repo_slug=repo_slug,
            run_verifier=run_verifier,
            breaches=breaches,
            notes=notes,
        )


def govern(
    *,
    changed: Sequence[tuple[str, str]],
    base_reader: Callable[[str], str | None],
    head_reader: Callable[[str], str | None],
    migration_added_lines: str,
    run_verifier: Callable[[str], Mapping[str, object] | None],
    repo_slug: str,
) -> GovernReport:
    """The no-silent-re-baseline law — pure, every I/O injected.

    *changed* is ``(status, name)`` per file under ``tests/bench/baselines/``
    (git ``--name-status`` letters; *name* is the file name). *base_reader*
    / *head_reader* return file text (``None`` = absent). *repo_slug* is
    THIS repository's ``owner/repo`` (``$GITHUB_REPOSITORY``): every cited
    provenance run_url must live on it — a completed run on ANY other repo
    (e.g. a fork keeping ``bench-m4pro.yml`` at the required path with an
    attacker-chosen head_sha) is a breach, and a missing/malformed slug
    raises :class:`GovernanceError` (fail-closed, mirroring the API-outage
    stance). The MODIFIED / REMOVED / top-level rules force a NEW
    human-written MIGRATION entry in the SAME PR carrying the entry key +
    literal value token, plus a live-verified fresh THIS-repo bench run —
    no author-controlled input satisfies them silently.
    """
    if REPO_SLUG_RE.fullmatch(repo_slug) is None:
        raise GovernanceError(
            f"repo_slug {repo_slug!r} malformed — the governance gate needs THIS "
            "repository's owner/repo (the $GITHUB_REPOSITORY env on CI) to pin "
            "provenance runs, and fails CLOSED without it"
        )
    breaches: list[str] = []
    notes: list[str] = []
    ledgered = _ledgered_ids(migration_added_lines)
    for status, name in changed:
        kind = status[:1]
        if name == "README.md":
            notes.append("README.md: documentation change (ungoverned)")
            continue
        if not name.endswith(".json"):
            breaches.append(f"{name}: stray non-baseline file change in tests/bench/baselines/")
            continue
        if kind == "D":
            if name not in migration_added_lines:
                breaches.append(
                    f"{name}: baseline file DELETED without a MIGRATION.md added line "
                    "naming it — retiring a hardware key is a ledgered decision"
                )
            else:
                notes.append(f"{name}: deletion carries a ledgered note")
            continue
        if kind not in ("A", "M"):
            breaches.append(
                f"{name}: unsupported git status {status!r} — the caller must expand "
                "renames/copies into delete + add"
            )
            continue
        head_text = head_reader(name)
        if head_text is None:
            breaches.append(f"{name}: changed file unreadable at HEAD")
            continue
        try:
            head_baseline = parse_baseline(head_text, filename=name)
        except BaselineSchemaError as exc:
            breaches.append(f"{name}: head baseline invalid — {exc}")
            continue
        if kind == "A":
            for entry in sorted(head_baseline.entries):
                _govern_added_cell(
                    entry,
                    head_baseline.entries[entry],
                    ledgered=ledgered,
                    repo_slug=repo_slug,
                    run_verifier=run_verifier,
                    breaches=breaches,
                    notes=notes,
                )
            continue
        base_text = base_reader(name)
        if base_text is None:
            breaches.append(f"{name}: modified file has no readable base blob")
            continue
        try:
            base_baseline = parse_baseline(base_text, filename=name)
        except BaselineSchemaError as exc:
            breaches.append(
                f"{name}: base baseline unparseable ({exc}) — refusing to classify; "
                "fix the base or re-add the file"
            )
            continue
        _govern_modified_file(
            name,
            base_baseline,
            head_baseline,
            ledgered=ledgered,
            migration_added_lines=migration_added_lines,
            repo_slug=repo_slug,
            run_verifier=run_verifier,
            breaches=breaches,
            notes=notes,
        )
    return GovernReport(ok=not breaches, breaches=tuple(breaches), notes=tuple(notes))


__all__ = [
    "BENCH_WORKFLOW_PATH",
    "BOOTSTRAP_DECISION_ID",
    "DEC_ID_RE",
    "DEFAULT_IMPROVEMENT_WARN_RATIO",
    "DEFAULT_SUSPICIOUS_SPEEDUP_RATIO",
    "DEFAULT_TOLERANCE_PCT",
    "ENTRY_WORKLOADS",
    "KEY_SLUG_RE",
    "MEASURE_ROUNDS",
    "MEASURE_WARMUP_ROUNDS",
    "MIN_BASELINE_SAMPLES",
    "MIN_CAPTURE_RUNS",
    "REPO_SLUG_RE",
    "REQUIRED_ENTRY_KEYS",
    "REQUIRED_WHEELS",
    "RUN_URL_RE",
    "SCHEMA_VERSION",
    "STAT",
    "TOLERANCE_MULTIPLIER_MAX_EXCLUSIVE",
    "TOLERANCE_PCT_MAX_EXCLUSIVE",
    "UNIT",
    "WORKLOAD_HASH_RE",
    "Baseline",
    "BaselineSchemaError",
    "BenchBaselineError",
    "CompareReport",
    "EntryRecord",
    "EntryVerdict",
    "Fragment",
    "FragmentError",
    "GovernReport",
    "GovernanceError",
    "NoiseRefusal",
    "OverwriteRefusal",
    "Provenance",
    "Verdict",
    "compare",
    "dump_baseline",
    "fold_capture",
    "format_hardware_key",
    "govern",
    "parse_baseline",
    "parse_fragment",
    "render_pr_body",
    "render_report",
    "slugify",
    "tolerance_multiplier",
    "validate_dir",
    "workload_hash",
]
