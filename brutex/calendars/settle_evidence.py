"""Committed measured-settle EVIDENCE — schema validator + hardened loader.

DEC-SETTLE-EVIDENCE-ADOPTION-001 (MIGRATION.md #1793). The #1775 post-close
settle probe's ``--summarize --emit-evidence`` folds ≥ 5 REGULAR trading days
of transcripts into ONE machine-readable artifact
(``docs/groww/probe/settle_evidence.json``, schema 1, committed BESIDE its
transcripts and reviewed like any other machine evidence — the
``tests/bench/baselines`` precedent). This module is the validated ADOPTION
channel: :func:`evidence_settle` is the ONE seam
:func:`~brutex.calendars.auto_pull_gate.resolve_settle_resolution` (tier 2)
and the two CLI eager-validation probes consume.

Failure posture (the DEC-BENCH-ABS-BASELINE-001 two-tier law):

* an operator-typed EXPLICIT path (:data:`SETTLE_EVIDENCE_PATH_ENV`) fails
  LOUD — absent/not-regular/oversized/unreadable/invalid all raise
  :class:`ValueError` naming the env var (the ``resolve_*`` house law);
* the DEFAULT repo path degrades SOFT — a present-but-bad file is ONE
  ``settle_evidence.invalid`` WARN per stat-token then the conservative
  17:30 fallback (the r6/r7 "never crash OR hang the boot" posture; the
  fail-loud tier for COMMITTED content is CI's committed-file test + the
  emitter's self-validation — a raising runtime resolver would let one bad
  commit brick every dashboard boot fleet-wide via ``git pull``, strictly
  worse than the local env-typo class; the fallback direction is SAFE —
  17:30 is the latest plausible value).

Reads ride the COMMON regular-file-only loader
(:func:`brutex.data.sidecar_read.read_sidecar_bytes` — ``O_NONBLOCK`` +
``S_ISREG`` + the fstat-on-fd size cap), so a planted FIFO / device symlink /
socket / directory can never hang a boot or a poll. Wheel-install honesty:
:func:`default_evidence_path` derives the repo root as
``parents[2]`` (the ``config.dotenv._default_repo_root`` precedent) — under a
non-editable site-packages install that path reads ABSENT and the resolver
silently serves the tier-3 default (safe-by-absence; editable installs and
checkouts resolve the committed file).

Staleness clock: :func:`evidence_settle` reads ``datetime.now(UTC)`` at
fresh-compute only — an observability input for the >180-day WARN, never a
decision input (no injectable clock at a 180-day threshold; a process running
past the boundary with an unchanged file re-warns only after restart).
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Final, NoReturn

import structlog

from brutex.calendars.calendar_rust import (
    _LAW_SETTLE,
    LawOutcome,
    Raise,
    Value,
    routed,
    unwrap,
    warn_fallback,
)
from brutex.calendars.market_hours import BLOCK_END
from brutex.data.sidecar_read import read_sidecar_bytes

_LOG = structlog.get_logger("brutex.calendars.settle_evidence")

__all__ = [
    "DEFAULT_EVIDENCE_RELPATH",
    "MAX_EVIDENCE_BYTES",
    "MAX_MARGIN_MINUTES",
    "MIN_EVIDENCE_DAYS",
    "MIN_MARGIN_MINUTES",
    "SETTLE_EVIDENCE_KIND",
    "SETTLE_EVIDENCE_OFF",
    "SETTLE_EVIDENCE_PATH_ENV",
    "SETTLE_EVIDENCE_SCHEMA",
    "STALE_EVIDENCE_AFTER_DAYS",
    "SettleEvidence",
    "default_evidence_path",
    "evidence_settle",
    "resolve_evidence_path",
    "validate_evidence_payload",
]

#: The evidence artifact's schema stamp — exact int match (a FUTURE schema is
#: refused, never half-parsed; unknown EXTRA keys within schema 1 are ignored).
SETTLE_EVIDENCE_SCHEMA: Final[int] = 1

#: The exact ``kind`` token the artifact must carry.
SETTLE_EVIDENCE_KIND: Final[str] = "groww-postclose-settle-evidence"

#: Path override / kill switch. Unset/blank → the repo-committed default path;
#: ``off`` (case-insensitive) → the channel is DISABLED (the conftest
#: hermeticity belt's value); any other value is an EXPLICIT operator path —
#: junk behind it fails LOUD (the resolve_* law).
SETTLE_EVIDENCE_PATH_ENV: Final[str] = "BRUTEX_SETTLE_EVIDENCE_PATH"

#: The case-insensitive disable token.
SETTLE_EVIDENCE_OFF: Final[str] = "off"

#: The repo-committed default artifact location (NOT committed by
#: DEC-SETTLE-EVIDENCE-ADOPTION-001 itself — the measured value is operator
#: work; see docs/groww/probe/POSTCLOSE_SETTLE_RUNBOOK.md).
DEFAULT_EVIDENCE_RELPATH: Final[str] = "docs/groww/probe/settle_evidence.json"

#: The runbook's ≥5-REGULAR-trading-days law, validated on the artifact.
MIN_EVIDENCE_DAYS: Final[int] = 5

#: Margin floor = the smallest inter-slot gap (15:35 → 15:45); a sub-gap
#: margin would claim finer resolution than the probe grid measured.
MIN_MARGIN_MINUTES: Final[int] = 10

#: Margin sanity ceiling (4 h past the earliest all-pass slot).
MAX_MARGIN_MINUTES: Final[int] = 240

#: WARN-only staleness threshold — a stale measured value still dominates a
#: guess, so the evidence is ADOPTED with one ``settle_evidence.stale`` WARN.
STALE_EVIDENCE_AFTER_DAYS: Final[int] = 180

#: Size cap on the artifact (fstat-on-fd) — a real file is ~2 KiB, so 1 MiB
#: is ~500× headroom; a planted multi-GB file reads as invalid, never whole.
MAX_EVIDENCE_BYTES: Final[int] = 1 * 1024 * 1024

#: Strict ASCII ``HH:MM`` (``re.ASCII`` per the DEC-LAKE-STATS-MANIFEST-001
#: round-3 precedent). The former auto_pull_gate ``_SETTLE_SHAPE`` Python
#: twin was DELETED at the pull-state flip — the env-tier settle token's
#: shape law now lives in the Rust kernel (``settle_token_verdict``, the
#: crate's byte-digit check; auto_pull_gate consults it fail-closed and
#: composes the byte-identical ValueErrors Python-side). Consumed via
#: ``fullmatch`` ONLY: under ``.match`` the ``$`` anchor matches BEFORE a
#: single trailing newline, so ``"15:45\n"`` would validate and the raw
#: newline would reach the ``settle_evidence.valid`` INFO + the gate
#: provenance un-repr'd (refuter r1 F-R1-1 — executed repro; fullmatch
#: refuses it). The kernel's shape law has no such quirk (its token is
#: ``.strip()``'d pre-consult, so a trailing newline never reaches it).
_HHMM_SHAPE: Final[re.Pattern[str]] = re.compile(r"^(\d{2}):(\d{2})$", re.ASCII)

#: Strict ASCII ISO calendar-day shape for ``per_day`` keys (``fullmatch``
#: ONLY — the same ``$``-before-trailing-newline refusal as ``_HHMM_SHAPE``).
_ISO_DAY_SHAPE: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}$", re.ASCII)

#: The settle override env var's LITERAL name, mirrored from
#: ``auto_pull_gate.PULL_ALL_SETTLE_ENV`` for the ``settle_evidence.valid``
#: detail wording — a literal (not an import) because auto_pull_gate imports
#: THIS module (tier 2); drift-pinned by test_settle_evidence.py.
_SETTLE_OVERRIDE_ENV: Final[str] = "BRUTEX_PULL_ALL_SETTLE_IST"


@dataclass(frozen=True, slots=True)
class SettleEvidence:
    """One VALIDATED committed settle-evidence artifact (load-bearing fields)."""

    adopted: time
    earliest_all_pass: str
    margin_minutes: int
    distinct_trading_days: int
    generated_at_utc: str
    probe_script_version: str
    path: str


def default_evidence_path() -> Path:
    """The repo-committed default artifact path — ``<repo>/docs/groww/probe/...``.

    PURE path arithmetic — ``parents[2]`` up from this file's package
    (parents[0] = ``calendars``, [1] = ``brutex``, [2] = the repo root; the
    ``brutex.config.dotenv._default_repo_root`` precedent MINUS its
    ``.resolve()`` — the module ``__file__`` is already absolute at import
    on this Python, and symlink canonicalization is unnecessary for a
    repo-relative default that every consumer addresses through THIS one
    function, so :func:`resolve_evidence_path` performs genuinely zero
    filesystem work; hostile-review r1 MINOR-1). Under a non-editable wheel
    install parents[2] lands in site-packages' parent — the path reads
    ABSENT and the loader silently serves the tier-3 default
    (safe-by-absence, documented in the module docstring). MUST be looked up
    as a module global by :func:`resolve_evidence_path` at CALL time so
    default-tier tests can monkeypatch this seam (the ``now_ist``-lookup
    idiom in ``auto_pull_gate.compose_market_closed``).
    """
    return Path(__file__).parents[2] / DEFAULT_EVIDENCE_RELPATH


def resolve_evidence_path(
    env: Mapping[str, str] | None = None,
) -> tuple[Path | None, bool]:
    """Resolve ``(path, explicit)`` from the env — pure, never raises, no I/O
    (the default branch is pure path arithmetic on this module's absolute
    ``__file__`` — no ``.resolve()`` lstat walk; r1 MINOR-1).

    Unset / blank → ``(default_evidence_path(), False)``; the ``off`` token
    (casefold) → ``(None, False)`` — the channel disabled; any other value →
    ``(Path(value), True)`` — an explicit operator path (junk behind it fails
    loud downstream, the resolve_* law).
    """
    mapping = os.environ if env is None else env
    token = mapping.get(SETTLE_EVIDENCE_PATH_ENV, "").strip()
    if not token:
        return default_evidence_path(), False
    if token.casefold() == SETTLE_EVIDENCE_OFF:
        return None, False
    return Path(token), True


def _fail(path: str, reason: str) -> NoReturn:
    """Raise the validator's uniform :class:`ValueError` naming path + reason.

    The *path* is ``!r``'d like ``_absent_error``'s (the #907 source-repr
    convention — every explicit-tier ValueError reprs the path consistently;
    hostile-review r1 security F2).
    """
    raise ValueError(f"settle evidence {path!r}: {reason}")


def _require_int(
    value: object,
    *,
    path: str,
    field: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """An exact-int field — ``bool`` REFUSED first (True == 1 would sail through)."""
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, f"{field} must be an int (bool refused), got {value!r}")
    if minimum is not None and value < minimum:
        _fail(path, f"{field}={value} is below the minimum {minimum}")
    if maximum is not None and value > maximum:
        _fail(path, f"{field}={value} is above the maximum {maximum}")
    return value


def _require_hhmm(value: object, *, path: str, field: str) -> tuple[time, int]:
    """A strict ASCII ``HH:MM`` real wall-clock field → ``(time, minute-of-day)``."""
    if not isinstance(value, str):
        _fail(path, f"{field} must be a string 'HH:MM', got {value!r}")
    match = _HHMM_SHAPE.fullmatch(value)
    if match is None:
        _fail(path, f"{field}={value!r} is not strict ASCII HH:MM")
    hour, minute = int(match.group(1)), int(match.group(2))
    try:
        parsed = time(hour, minute)
    except ValueError:
        _fail(path, f"{field}={value!r} is not a real wall-clock time")
    return parsed, hour * 60 + minute


def _require_str_list(value: object, *, path: str, field: str) -> None:
    """An informational list-of-strings field (shape-checked when present)."""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _fail(path, f"{field} must be a list of strings, got {value!r}")


def _validate_per_day(per_day: object, *, path: str) -> dict[str, dict[str, dict[str, object]]]:
    """The ``per_day`` matrix — ISO day → slot ``HH:MM`` → the check cell."""
    if not isinstance(per_day, dict):
        _fail(path, f"per_day must be an object, got {type(per_day).__name__}")
    if not per_day:
        _fail(path, "per_day is empty — no observations")
    for day, slots in per_day.items():
        if not isinstance(day, str) or _ISO_DAY_SHAPE.fullmatch(day) is None:
            _fail(path, f"per_day key {day!r} is not an ISO calendar day")
        try:
            datetime.strptime(day, "%Y-%m-%d")  # date-only realness check
        except ValueError:
            _fail(path, f"per_day key {day!r} is not a real calendar day")
        if not isinstance(slots, dict) or not slots:
            _fail(path, f"per_day[{day!r}] must be a non-empty object of slots")
        for slot, cell in slots.items():
            if not isinstance(slot, str) or _HHMM_SHAPE.fullmatch(slot) is None:
                _fail(path, f"per_day[{day!r}] slot key {slot!r} is not HH:MM")
            if not isinstance(cell, dict):
                _fail(path, f"per_day[{day!r}][{slot!r}] must be an object")
            _require_int(
                cell.get("records"),
                path=path,
                field=f"per_day[{day!r}][{slot!r}].records",
                minimum=1,
            )
            if not isinstance(cell.get("all_pass"), bool):
                _fail(path, f"per_day[{day!r}][{slot!r}].all_pass must be a bool")
            _require_str_list(
                cell.get("failed_checks"),
                path=path,
                field=f"per_day[{day!r}][{slot!r}].failed_checks",
            )
    return per_day


def validate_evidence_payload(payload: object, *, path: str) -> SettleEvidence:
    """The pure truth-table validator — :class:`ValueError` names path + reason.

    Load-bearing fields are validated STRICTLY (schema/kind exact; strict
    ASCII ``HH:MM``; ``bool`` refused where an int is required; the
    ``adopted == earliest + margin`` minute arithmetic with NO midnight wrap;
    the ``adopted >= BLOCK_END`` 15:35 floor with 15:35-exact ALLOWED — the
    env-tier boundary mirrored; the per-day support law). Informational
    fields (``checks`` / ``slots_observed`` / ``transcripts`` /
    ``excluded_days`` / ``ignored_records``) are shape-checked when present,
    never load-bearing; unknown EXTRA keys are ignored (forward-compat within
    schema 1).
    """
    if not isinstance(payload, dict):
        _fail(path, f"root is not a JSON object (got {type(payload).__name__})")
    schema = payload.get("schema")
    # Exact-INT schema (refuter r3 F-R3a-1): bool refused FIRST (True == 1),
    # then the type gate — a JSON float ``1.0`` == 1 numerically and would
    # sail through bare equality (the ``_require_int`` law mirrored) — then
    # the exact value.
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != SETTLE_EVIDENCE_SCHEMA:
        _fail(path, f"schema must be exactly {SETTLE_EVIDENCE_SCHEMA}, got {schema!r}")
    kind = payload.get("kind")
    if kind != SETTLE_EVIDENCE_KIND:
        _fail(path, f"kind must be {SETTLE_EVIDENCE_KIND!r}, got {kind!r}")
    earliest_raw = payload.get("earliest_all_pass_ist")
    _, earliest_minutes = _require_hhmm(earliest_raw, path=path, field="earliest_all_pass_ist")
    adopted, adopted_minutes = _require_hhmm(
        payload.get("adopted_settle_ist"), path=path, field="adopted_settle_ist"
    )
    margin = _require_int(
        payload.get("margin_minutes"),
        path=path,
        field="margin_minutes",
        minimum=MIN_MARGIN_MINUTES,
        maximum=MAX_MARGIN_MINUTES,
    )
    if earliest_minutes + margin >= 24 * 60:
        _fail(
            path,
            f"earliest_all_pass_ist + margin_minutes wraps past midnight "
            f"({earliest_raw!r} + {margin} min)",
        )
    if earliest_minutes + margin != adopted_minutes:
        _fail(
            path,
            f"adopted_settle_ist must equal earliest_all_pass_ist + margin_minutes "
            f"({earliest_raw!r} + {margin} min != {payload.get('adopted_settle_ist')!r})",
        )
    if adopted < BLOCK_END:
        _fail(
            path,
            f"adopted_settle_ist {payload.get('adopted_settle_ist')!r} is before the "
            f"market-hours guard's BLOCK_END {BLOCK_END:%H:%M} IST "
            f"(15:35-exact is allowed — the env-tier floor mirrored)",
        )
    days = _require_int(
        payload.get("distinct_trading_days"),
        path=path,
        field="distinct_trading_days",
        minimum=MIN_EVIDENCE_DAYS,
    )
    per_day = _validate_per_day(payload.get("per_day"), path=path)
    earliest_key = str(earliest_raw)  # _require_hhmm proved it is a str
    support = sum(
        1
        for slots in per_day.values()
        if earliest_key in slots and slots[earliest_key].get("all_pass") is True
    )
    if support != days:
        _fail(
            path,
            f"per_day support for the earliest all-pass slot {earliest_raw!r} is "
            f"{support} day(s) but distinct_trading_days claims {days}",
        )
    generated = payload.get("generated_at_utc")
    if not isinstance(generated, str):
        _fail(path, f"generated_at_utc must be an ISO-8601 string, got {generated!r}")
    try:
        generated_dt = datetime.fromisoformat(generated)
    except ValueError:
        _fail(path, f"generated_at_utc={generated!r} is not ISO-8601")
    if generated_dt.tzinfo is None:
        _fail(path, f"generated_at_utc={generated!r} is tz-naive (must be tz-aware)")
    if generated_dt.utcoffset() != timedelta(0):
        # The field NAME promises UTC and the emitter always writes UTC — an
        # aware-but-non-UTC offset is refused (r1 correctness NIT-1; the
        # staleness math would still be correct, the tightening is semantic).
        _fail(
            path,
            f"generated_at_utc={generated!r} must be UTC (got offset {generated_dt.utcoffset()})",
        )
    version = payload.get("probe_script_version")
    if not isinstance(version, str) or not version:
        # NOT exact-matched — informational provenance, but it must exist.
        _fail(path, f"probe_script_version must be a non-empty string, got {version!r}")
    for field in ("checks", "slots_observed", "transcripts"):
        if field in payload:
            _require_str_list(payload[field], path=path, field=field)
    if "excluded_days" in payload:
        excluded = payload["excluded_days"]
        if not isinstance(excluded, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in excluded.items()
        ):
            _fail(path, f"excluded_days must be an object of strings, got {excluded!r}")
    if "ignored_records" in payload:
        _require_int(payload["ignored_records"], path=path, field="ignored_records", minimum=0)
    return SettleEvidence(
        adopted=adopted,
        earliest_all_pass=earliest_key,
        margin_minutes=margin,
        distinct_trading_days=days,
        generated_at_utc=generated,
        probe_script_version=version,
        path=path,
    )


# ── Rust routing (DEC-RUST-CALENDAR-FACADE-FLIP-001) ──────────────────────

#: The ``brutex_calendar.validate_settle_evidence`` return arity —
#: ``(valid, message, adopted_hhmm, generated_raw)``.
_SETTLE_CRATE_TUPLE_LEN: Final[int] = 4


def _materialize_settle_valid(
    payload: object,
    adopted_hhmm: object,
    generated_raw: object,
    *,
    path: str,
) -> LawOutcome[SettleEvidence] | None:
    """Re-hydrate a crate ``valid=True`` verdict into :class:`SettleEvidence`.

    The crate DECIDED the payload valid + the two Rust-owned fields
    (``adopted_hhmm`` / ``generated_raw``); Python PLUCKS the four non-decided
    fields from the crate-validated payload (cheap ``dict.get`` + type-check —
    NOT a re-validation, or the oracle would run every call). Any structural
    surprise (a foreign / broken crate saying ``valid`` over a non-conforming
    payload — structurally unreachable through the real crate) is REFUSED to the
    oracle (``None`` + one loud WARN); a malformed ``adopted_hhmm`` reaching
    :class:`~datetime.time` raises ``ValueError`` UP into the facade degrade set.
    """
    if (
        not isinstance(payload, dict)
        or not isinstance(adopted_hhmm, str)
        or not isinstance(generated_raw, str)
    ):
        warn_fallback("settle_valid_shape", got=repr((adopted_hhmm, generated_raw)))
        return None
    earliest_raw = payload.get("earliest_all_pass_ist")
    margin = payload.get("margin_minutes")
    days = payload.get("distinct_trading_days")
    version = payload.get("probe_script_version")
    if (
        earliest_raw is None
        or isinstance(margin, bool)
        or not isinstance(margin, int)
        or isinstance(days, bool)
        or not isinstance(days, int)
        or not isinstance(version, str)
    ):
        warn_fallback("settle_valid_pluck")
        return None
    match = _HHMM_SHAPE.fullmatch(adopted_hhmm)
    if match is None:
        warn_fallback("settle_valid_adopted_shape", got=repr(adopted_hhmm))
        return None
    adopted = time(int(match.group(1)), int(match.group(2)))
    return Value(
        SettleEvidence(
            adopted=adopted,
            earliest_all_pass=str(earliest_raw),
            margin_minutes=margin,
            distinct_trading_days=days,
            generated_at_utc=generated_raw,
            probe_script_version=version,
            path=path,
        )
    )


def _materialize_settle(
    raw_return: object, *, payload: object, path: str
) -> LawOutcome[SettleEvidence] | None:
    """Re-hydrate the crate's ``validate_settle_evidence`` return.

    ``None`` = the crate's serde ABSTAIN (bytes CPython's ``json.loads``
    accepted but the Rust engine refused — NaN/Infinity/lone-surrogate/deep
    nesting) → serve the Python validator (silent, designed degrade). Otherwise a
    ``(valid, message, adopted_hhmm, generated_raw)`` 4-tuple: ``valid=False``
    defers the byte-identical ``ValueError`` (``path_repr`` was passed
    pre-``repr()``'d, so ``message`` matches the oracle text); ``valid=True``
    materializes :class:`SettleEvidence`. A wrong SHAPE / leg type is refuse-poison.
    """
    if raw_return is None:
        return None
    if not isinstance(raw_return, tuple) or len(raw_return) != _SETTLE_CRATE_TUPLE_LEN:
        warn_fallback("settle_shape", got=repr(raw_return))
        return None
    valid, message, adopted_hhmm, generated_raw = raw_return
    if not isinstance(valid, bool) or not isinstance(message, str):
        warn_fallback("settle_leg_types", got=repr(raw_return))
        return None
    if not valid:
        return Raise(message)
    return _materialize_settle_valid(payload, adopted_hhmm, generated_raw, path=path)


def routed_validate_evidence_payload(payload: object, *, raw: bytes, path: str) -> SettleEvidence:
    """Rust-first :func:`validate_evidence_payload` — byte-identical on every input.

    DEC-RUST-CALENDAR-FACADE-FLIP-001. Routes the settle-evidence truth table
    through the ``brutex_calendar`` crate behind the default-ON
    ``BRUTEX_CALENDAR_RUST`` flag; the verbatim Python validator is the LIVE
    parity oracle every degrade / abstain / burn-in-mismatch path serves. Raises
    the SAME :class:`ValueError` (byte-identical message) on an invalid payload.

    Parameters
    ----------
    payload
        The already-``json.loads``-decoded object (the Python oracle's input).
    raw
        The RAW artifact bytes the crate re-parses with serde (never a second
        Python decode — the crate owns the parse-engine divergence ABSTAIN).
    path
        The already-``str()``-ified sidecar path; ``repr(path)`` is handed to
        the crate pre-computed so every refusal ``message`` embeds it identically.
    """

    def _oracle() -> LawOutcome[SettleEvidence]:
        try:
            return Value(validate_evidence_payload(payload, path=path))
        except ValueError as exc:
            return Raise(str(exc))

    outcome = routed(
        _LAW_SETTLE,
        rust_call=lambda crate: crate.validate_settle_evidence(raw, repr(path)),
        materialize=lambda raw_return: _materialize_settle(raw_return, payload=payload, path=path),
        oracle=_oracle,
    )
    return unwrap(outcome)


# ── the memoized loader ──────────────────────────────────────────────────


class _EvidenceMemo:
    """Single-slot process memo (production has ONE evidence file; the token
    folds the path, so a different path is simply a fresh compute — alternating
    paths evict each other, a bounded recompute, never staleness). Guarded by
    its lock; "once per stat-token" = the AutoPullStateStore "once per
    corruption sighting" semantics — a replaced file re-validates + re-logs
    (truth-table row 25). An attribute-mutated holder, not module-global
    rebinds (§4 no-global-mutable-state discipline, the injectable seam being
    :func:`_reset_evidence_memo_for_tests`).

    COSMETIC-LOG residual (r1 correctness NIT-4): if an operator sets
    :data:`SETTLE_EVIDENCE_PATH_ENV` to EXACTLY the repo default path AND a
    corrupt-EXPLICIT call precedes a default (env-unset) call in the same
    process, the explicit RAISE pre-empts the ``settle_evidence.invalid``
    WARN and the later default call serves the memoized outcome
    (``fresh=False``) → ``None`` with no WARN. The served VALUE is still
    correct (``None`` → the 17:30 tier-3 fallback) — log-only, and it
    requires the explicit path to equal the default path."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.token: tuple[str, int, int] | None = None
        self.outcome: SettleEvidence | str | None = None


_MEMO: Final[_EvidenceMemo] = _EvidenceMemo()


def _reset_evidence_memo_for_tests() -> None:
    """Reset the process memo (the ``hardware_envelope`` test-reset precedent)."""
    with _MEMO.lock:
        _MEMO.token = None
        _MEMO.outcome = None


def _absent_error(path: Path) -> ValueError:
    """The explicit-tier ABSENT refusal — names the env var + the path."""
    return ValueError(
        f"{SETTLE_EVIDENCE_PATH_ENV}={str(path)!r}: settle-evidence file is absent "
        f"— fix the path, or unset the env to use the repo default "
        f"({DEFAULT_EVIDENCE_RELPATH}), or set it to 'off'"
    )


def _compute_outcome(path: Path) -> SettleEvidence | str | None:
    """Read + decode + validate — ``None`` = raced-away-absent, str = invalid."""
    result = read_sidecar_bytes(path, max_bytes=MAX_EVIDENCE_BYTES)
    if result.shape == "absent":
        return None
    if result.raw is None:
        # not_regular / oversized / read_error — the detail names the shape.
        return result.detail
    try:
        payload: object = json.loads(result.raw.decode("utf-8"))
    except (ValueError, RecursionError, OverflowError) as exc:
        # The r6 N-1 decode-guard set: ValueError covers junk JSON AND
        # UnicodeDecodeError (its subclass); RecursionError the nesting bomb;
        # OverflowError an out-of-range numeric.
        return f"undecodable ({str(exc) or type(exc).__name__})"
    try:
        return routed_validate_evidence_payload(payload, raw=result.raw, path=str(path))
    except ValueError as exc:
        return str(exc)


def evidence_settle(env: Mapping[str, str] | None = None) -> SettleEvidence | None:
    """The ONE adoption seam — validated evidence or ``None`` (never a bad value).

    ``off`` / an absent DEFAULT path → ``None`` silently (the per-poll path —
    no log). An EXPLICIT path (:data:`SETTLE_EVIDENCE_PATH_ENV` set to a
    path) fails LOUD on absent / not-regular / oversized / unreadable /
    invalid — :class:`ValueError` naming the env var. A DEFAULT-path
    present-but-bad file is ONE ``settle_evidence.invalid`` WARN per
    stat-token then ``None`` (tier-3 17:30 fallback). A valid file returns
    the :class:`SettleEvidence` with ONE ``settle_evidence.valid`` INFO per
    stat-token (truthful whether or not the settle env overrides — §3.3
    naming) plus ONE ``settle_evidence.stale`` WARN when older than
    :data:`STALE_EVIDENCE_AFTER_DAYS` (still adopted — measured-then-margined
    dominates a guess). Memoized on ``(path, st_mtime_ns, st_size)`` under a
    module lock; ABSENT is re-probed every call — the per-call filesystem
    cost is exactly ONE ``os.stat`` of the evidence path (dentry-cached; the
    path derivation itself is pure — r1 MINOR-2), no log; a later
    commit/deploy is picked up on the next poll.
    """
    path, explicit = resolve_evidence_path(env)
    if path is None:
        return None
    with _MEMO.lock:
        return _evidence_settle_locked(path, explicit)


def _evidence_settle_locked(path: Path, explicit: bool) -> SettleEvidence | None:
    """The lock-held body of :func:`evidence_settle` (memo + logs + raises)."""
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        # ABSENT (incl. a dangling symlink) — never memoized, never logged.
        if explicit:
            raise _absent_error(path) from None
        return None
    except (OSError, ValueError) as exc:
        # A stat-level fault — an unreadable parent (OSError) or an
        # un-stat-able PATH VALUE (``os.stat`` raises raw ValueError on an
        # embedded NUL byte; only an explicit operator path can carry one —
        # r1 correctness NIT-2). No content token exists; fold into the
        # read_error family under a degenerate token so the default-tier WARN
        # still fires once per sighting and the explicit tier re-raises the
        # module's uniform ValueError naming the env var (via ``_serve`` —
        # the fail-loud direction preserved, never an unwrapped raw raise).
        token = (str(path), -1, -1)
        pre_outcome: SettleEvidence | str | None = f"unreadable ({type(exc).__name__}: {exc})"
    else:
        token = (str(path), st.st_mtime_ns, st.st_size)
        pre_outcome = None
    if token == _MEMO.token and _MEMO.outcome is not None:
        return _serve(path, explicit, _MEMO.outcome, fresh=False)
    outcome = _compute_outcome(path) if pre_outcome is None else pre_outcome
    if outcome is None:
        # Raced away between the stat and the open — the ABSENT answer.
        if explicit:
            raise _absent_error(path) from None
        return None
    _MEMO.token = token
    _MEMO.outcome = outcome
    return _serve(path, explicit, outcome, fresh=True)


def _serve(
    path: Path, explicit: bool, outcome: SettleEvidence | str, *, fresh: bool
) -> SettleEvidence | None:
    """Serve a memoized/fresh outcome — logs fire on FRESH computes only."""
    if isinstance(outcome, SettleEvidence):
        if fresh:
            _LOG.info(
                "settle_evidence.valid",
                adopted_settle_ist=f"{outcome.adopted:%H:%M}",
                earliest_all_pass_ist=outcome.earliest_all_pass,
                margin_minutes=outcome.margin_minutes,
                distinct_trading_days=outcome.distinct_trading_days,
                path=outcome.path,
                detail=(
                    "committed settle evidence validated — adopted whenever "
                    f"{_SETTLE_OVERRIDE_ENV} is unset"
                ),
            )
            generated = datetime.fromisoformat(outcome.generated_at_utc)
            if datetime.now(UTC) - generated > timedelta(days=STALE_EVIDENCE_AFTER_DAYS):
                # generated_at_utc is FILE CONTENT — repr()'d at source (the
                # #907 convention; ``fromisoformat`` accepts ANY single char
                # as the separator, so a VALIDATED value can still carry e.g.
                # a raw newline — r1 security F1).
                _LOG.warning(
                    "settle_evidence.stale",
                    generated_at_utc=repr(outcome.generated_at_utc),
                    stale_after_days=STALE_EVIDENCE_AFTER_DAYS,
                    path=outcome.path,
                    detail=(
                        "settle evidence is older than the staleness threshold — "
                        "still ADOPTED (measured-then-margined dominates a guess); "
                        "re-probe per docs/groww/probe/POSTCLOSE_SETTLE_RUNBOOK.md"
                    ),
                )
        return outcome
    if explicit:
        raise ValueError(f"{SETTLE_EVIDENCE_PATH_ENV}={str(path)!r}: {outcome}")
    if fresh:
        _LOG.warning(
            "settle_evidence.invalid",
            path=str(path),
            detail=outcome,
            fallback="the conservative 17:30 default (tier 3)",
        )
    return None
