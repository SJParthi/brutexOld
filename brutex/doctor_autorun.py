"""Zero-command auto-run of the Mac live-proof kit (DEC-DOCTOR-LIVE-AUTORUN-001).

The operator NEVER runs ``brutex doctor --live`` by hand (operator law,
2026-07-02: he starts BruteX as always — IDE ▶ / ``./up.sh`` / ``brutex
dashboard`` — and the health proof must run ITSELF, with the verdict in the
dashboard UI). This module is that auto-run engine; the kit itself
(:mod:`brutex.doctor_live`, DEC-DOCTOR-LIVE-PROOF-KIT-001) is UNCHANGED and
remains available for manual debugging.

Two bounded auto-runs, both driven by :class:`LiveProofAutorun` (owned by the
dashboard app instance — no module-global mutable state, CLAUDE.md §4):

1. **Startup preflight** — when the dashboard boots, the CHEAP kit stages
   (:data:`PREFLIGHT_STAGES`: env-identity incl. lake-root/SSD-mount [G1] +
   SDK version [G2], creds presence [R1], TOTP/clock [M3/N1], token READ —
   never a mint — + ONE authenticated smoke call [R7/M1/G2], instrument-master
   reachability [M17/R8]) run in a background daemon thread. Cost: ≤ 2
   network fetches total (1 authenticated vendor call + 1 keyless master CSV).
   The verdict lands in memory + on disk and the dashboard shows it as a
   persistent status element (``/api/v1/liveproof`` + ``bx-liveproof.js``).
2. **First-real-pull probes** — the one-time expensive probes
   (:data:`PROBE_STAGES`: 2020 1-minute depth [R6], VIX availability [R5 —
   the ADR-0009 §3.2 hard gate], 1-day derivation + NSE bhavcopy cross-check
   [G7], riding the creds/token/spot-1m prerequisites) auto-run ONCE around
   the first REAL pull attempt (the loud-refusal gate's success path —
   DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001). The verdict PERSISTS under
   ``<lake_root>/_liveproof/autorun/``; later startups/pulls read the cached
   verdict instead of re-probing. A failed/corrupt cache re-probes on the
   next pull — bounded: single-flight, at most one attempt per pull trigger,
   and every kit run is capped by the kit's own :class:`~brutex.doctor_live.\
CallBudget`. Probe runs inject a **1 r/s** controller ceiling
   (:data:`AUTORUN_PROBE_CEILING_RPS` — the ladder's deep floor) **under a
   dedicated 16/min hard slice** (:data:`AUTORUN_PROBE_CEILING_PER_MIN`); the
   probes share NO state with the dashboard's pull job.

Rate-budget composition (CLAUDE.md §2 Rule 6 — honest arithmetic, not a
"shared governor"): the probe controller is a SEPARATE
:class:`~brutex.adapters.rate_common.RateController` instance from the pull
job's, and rolling-window governors do NOT share state across instances, so
the account-level **500 req/min** cap (``GROWW_RATE_LIMIT_PER_MIN``,
operator-confirmed) must hold by BUDGET SPLIT: the production pull ceiling is
8 r/s (``GROWW_RATE_LIMIT_RPS_CEILING``, DEC-GROWW-RATE-CEILING-8-001) → at
most **480 issues/min** worst-case, and the probes' own per-minute governor is
capped at **16/min**, so ``480 + 16 = 496 ≤ 500`` even when a probe run stacks
on a full-throttle in-flight pull. The 4/min headroom is deliberate — NOT a
knife edge: the two rolling windows are independent, so fencepost counting
against a fixed wall-clock minute can observe up to one extra issue per
stream (``481 + 17 = 498 ≤ 500``); the earlier 20/min slice summed to a
boundary-countable 502 worst case. Both probe bounds only ever LOWER the
issue rate (a cap, never a raise — Rule 6 safe direction).

Honesty (Golden Rules #1/#10 — this repo just killed the R1 silent degrade;
do NOT reintroduce one):

* The verdict is **never PASS unless the stages truly ran against the real
  vendor** — ``pass`` requires the run's vendor-proof stages
  (:data:`PREFLIGHT_REQUIRED` / :data:`PROBES_REQUIRED`) to have genuinely
  PASSed.
* With **no credentials** in the NEVER-ATTEMPTED context (the sandbox / a
  dry-run setup with no live-proof history) the verdict is
  ``not_applicable`` — headline ``live-proof: not applicable (no
  credentials)`` carrying the :class:`~brutex.config.credentials_types.\
CredentialsError` fix text VERBATIM from the SAME seam the pull refusal
  uses (:func:`~brutex.config.credentials.groww_credentials_error`), so the
  startup banner and the HTTP-503 pull refusal always agree. **No kit stage
  runs and no vendor call is made** in that mode. But grey never papers
  over BROKEN credentials: a refused REAL pull attempt
  (:meth:`LiveProofAutorun.note_credentials_refusal`) and a creds-less
  startup on a machine whose cache shows genuine live-proof history both
  render an honest RED ``fail`` (:func:`credentials_failure_verdict`) with
  the same relayed fix text — a red verdict is never downgraded to grey.
* Every diagnostic line shown is the kit's own catalog line VERBATIM
  (:data:`~brutex.doctor_live.DIAGNOSTICS` stays the single golden-tested
  source — CLAUDE.md §11 COMMON; this module never re-words a diagnosis).
* ``BRUTEX_AUTORUN_LIVEPROOF=0`` is a code-level escape hatch (default ON —
  zero new operator surface; the operator never needs to set anything).

The autorun cannot VERIFY the live path from the sandbox — that remains
honestly UNVERIFIED until the operator's next normal start on the Mac; the
autorun exists precisely so that start needs zero extra commands.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.adapters.rate_common import RateController
from brutex.config.credentials import groww_credentials_error
from brutex.doctor_live import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_WARN,
    KitDeps,
    KitOptions,
    KitReport,
    build_real_deps,
    run_live_doctor,
)

_LOG = structlog.get_logger("brutex.doctor_autorun")

# ────────────────────────────────────────────────────────────────────────
# Configuration + stage selections
# ────────────────────────────────────────────────────────────────────────

#: The code-level escape hatch (DEFAULT ON — the operator never sets it).
#: Only an explicit falsy value disables the autorun.
AUTORUN_ENV: Final[str] = "BRUTEX_AUTORUN_LIVEPROOF"

#: Values (case-insensitive, stripped) that DISABLE the autorun. Anything
#: else — unset, blank, "1", garbage — leaves it ON (default-on is the
#: operator law; the mirror-image of the sibling default-OFF gates).
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: The CHEAP startup stages (kit spec §4): env / creds / totp / token-read +
#: ONE authenticated smoke call / instrument master. ≤ 2 network fetches.
PREFLIGHT_STAGES: Final[frozenset[int]] = frozenset({0, 1, 2, 3, 4})

#: The one-time expensive probes + their prerequisites: creds (1) → token (3)
#: → spot-1m (5) → depth (6) + VIX (7) + bhavcopy (8). ≤ ~10 vendor calls,
#: all inside the kit's default 40-call budget.
PROBE_STAGES: Final[frozenset[int]] = frozenset({1, 3, 5, 6, 7, 8})

#: Stage names whose genuine PASS/WARN is REQUIRED before a preflight verdict
#: may read better than ``fail`` — ``token`` is the stage whose authenticated
#: smoke call proves the run touched the real vendor.
PREFLIGHT_REQUIRED: Final[frozenset[str]] = frozenset({"token"})

#: Stage names whose genuine PASS/WARN is REQUIRED for a probes verdict —
#: the three questions the probes exist to answer.
PROBES_REQUIRED: Final[frozenset[str]] = frozenset({"depth", "vix", "bhavcopy"})

#: Per-second ceiling injected into PROBE runs (the rate ladder's deep floor)
#: so probe traffic stacked on an in-flight pull adds ≤ 1 r/s. Overriding the
#: controller here can only LOWER the issue rate vs the production 8 r/s
#: ceiling — never raise it (CLAUDE.md §2 Rule 6).
AUTORUN_PROBE_CEILING_RPS: Final[float] = 1.0

#: Per-MINUTE hard slice injected into PROBE runs — the probes' share of the
#: account-level 500/min cap. The probe controller is a SEPARATE instance
#: from the pull job's (rolling windows do not share state), so composition
#: is guaranteed by budget split: pull ≤ 8 r/s → 480/min worst-case, probes
#: ≤ this 16/min slice, 480 + 16 = 496 ≤ ``GROWW_RATE_LIMIT_PER_MIN`` — with
#: 4/min headroom so window-boundary fencepost counting (≤ 1 extra per
#: stream, 481 + 17 = 498) still fits; the earlier 20/min slice was a
#: knife-edge 500 that boundary counting could push to 502.
#: Only ever LOWER than the account cap (Rule 6 — a cap, never a raise).
AUTORUN_PROBE_CEILING_PER_MIN: Final[int] = 16

#: A ``warn`` probes verdict is settled for this many days, then re-probes on
#: the next real pull: a WARN (e.g. the NSE archive unreachable) is expected
#: to CLEAR, so it earns a cheap periodic re-check instead of being cached
#: forever. Code-level default — no new operator surface.
AUTORUN_WARN_RECHECK_DAYS: Final[int] = 7

#: On-disk verdict schema version — bump on shape change; a mismatched cache
#: is treated as absent (never mis-parsed into a fake verdict).
VERDICT_SCHEMA: Final[int] = 1

# ── verdict kinds + their operator-facing headlines ─────────────────────

KIND_PASS: Final[str] = "pass"  # noqa: S105 — a verdict-kind literal, not a credential
KIND_WARN: Final[str] = "warn"
KIND_FAIL: Final[str] = "fail"
KIND_ERROR: Final[str] = "error"
KIND_NOT_APPLICABLE: Final[str] = "not_applicable"

#: Kind → headline. The chrome around the kit's verbatim diagnostic lines;
#: the ``not_applicable`` wording is the operator-decreed sandbox sentence.
HEADLINES: Final[dict[str, str]] = {
    KIND_PASS: "live-proof: PASS",
    KIND_WARN: "live-proof: PASS with warnings",
    KIND_FAIL: "live-proof: FAIL",
    KIND_ERROR: "live-proof: autorun error",
    KIND_NOT_APPLICABLE: "live-proof: not applicable (no credentials)",
}

# ── run states ──────────────────────────────────────────────────────────

STATE_IDLE: Final[str] = "idle"
STATE_RUNNING: Final[str] = "running"
STATE_DONE: Final[str] = "done"


def autorun_enabled(env: Mapping[str, str]) -> bool:
    """Resolve the zero-command autorun toggle from *env* — **default ON**.

    Reads :data:`AUTORUN_ENV`. Returns ``False`` ONLY for an explicit falsy
    value (``"0"`` / ``"false"`` / ``"no"`` / ``"off"``, case-insensitive);
    unset, blank, or anything else keeps the autorun ON. Default-on is the
    operator law (zero new operator surface); the env var exists purely as a
    code-level escape hatch. O(1).

    Parameters
    ----------
    env
        The environment mapping (production passes ``os.environ``).

    Returns
    -------
    bool
        ``True`` when the autorun should run.
    """
    raw = env.get(AUTORUN_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSY


# ────────────────────────────────────────────────────────────────────────
# Verdict model + persistence
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AutorunVerdict:
    """One auto-run's display-ready outcome (immutable + JSON round-trippable).

    ``lines`` are the kit's catalog diagnostics VERBATIM (or the relayed
    CredentialsError fix text for ``not_applicable``) — never re-worded here
    (CLAUDE.md §11 COMMON). ``stages`` is the per-stage status matrix
    ``(stage, name, status)`` for the expanded UI view.
    """

    kind: str
    headline: str
    checked_at_utc: str
    run_id: str | None
    lines: tuple[str, ...]
    stages: tuple[tuple[int, str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-friendly dict (the on-disk + API shape)."""
        return {
            "schema": VERDICT_SCHEMA,
            "kind": self.kind,
            "headline": self.headline,
            "checked_at_utc": self.checked_at_utc,
            "run_id": self.run_id,
            "lines": list(self.lines),
            "stages": [
                {"stage": stage, "name": name, "status": status}
                for stage, name, status in self.stages
            ],
        }


def cache_usable(verdict: AutorunVerdict, *, now: datetime | None = None) -> bool:
    """Return ``True`` iff a cached *verdict* answers the probes without re-running.

    ``pass`` is a settled answer. ``warn`` is settled for
    :data:`AUTORUN_WARN_RECHECK_DAYS` days and then re-probes on the next
    real pull — a WARN (e.g. the NSE archive being unreachable) is expected
    to CLEAR, so it earns a cheap periodic re-check instead of being cached
    forever. ``fail`` / ``error`` / ``not_applicable`` re-probe on the next
    real pull (bounded by the single-flight guard). A ``warn`` whose
    timestamp cannot be parsed reads as stale (fail-closed → re-probe,
    still bounded).

    Parameters
    ----------
    verdict
        The cached verdict under consideration.
    now
        The current UTC time for the ``warn`` age check (``None`` → real
        clock; the holder injects its own clock).
    """
    if verdict.kind == KIND_PASS:
        return True
    if verdict.kind != KIND_WARN:
        return False
    try:
        checked = datetime.fromisoformat(verdict.checked_at_utc)
    except ValueError:
        return False
    if checked.tzinfo is None:
        return False
    current = _utc_now() if now is None else now
    return (current - checked) <= timedelta(days=AUTORUN_WARN_RECHECK_DAYS)


def _stage_rows(data: object) -> tuple[tuple[int, str, str], ...] | None:
    """Parse the ``stages`` payload rows, or ``None`` when malformed."""
    if not isinstance(data, list):
        return None
    rows: list[tuple[int, str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            return None
        stage, name, status = item.get("stage"), item.get("name"), item.get("status")
        if not isinstance(stage, int) or not isinstance(name, str) or not isinstance(status, str):
            return None
        rows.append((stage, name, status))
    return tuple(rows)


def verdict_from_payload(data: object) -> AutorunVerdict | None:
    """Rebuild an :class:`AutorunVerdict` from a decoded JSON payload, or ``None``.

    Fail-closed on ANY shape/schema mismatch (wrong type, unknown ``kind``,
    schema drift, malformed rows) — a damaged cache is treated as absent,
    never mis-parsed into a fake verdict (Golden Rule #1).
    """
    if not isinstance(data, dict):
        return None
    if data.get("schema") != VERDICT_SCHEMA:
        return None
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in HEADLINES:
        return None
    headline = data.get("headline")
    checked_at = data.get("checked_at_utc")
    run_id = data.get("run_id")
    lines = data.get("lines")
    if not isinstance(headline, str) or not isinstance(checked_at, str):
        return None
    if headline != HEADLINES[kind]:
        # Cross-check headline-vs-kind: every constructor stamps the kind's
        # own catalog headline, so a mismatch means a tampered/foreign cache
        # — fail-closed, never render a headline that contradicts the kind.
        return None
    if run_id is not None and not isinstance(run_id, str):
        return None
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        return None
    stages = _stage_rows(data.get("stages"))
    if stages is None:
        return None
    return AutorunVerdict(
        kind=kind,
        headline=headline,
        checked_at_utc=checked_at,
        run_id=run_id,
        lines=tuple(lines),
        stages=stages,
    )


def autorun_dir(lake_root: Path) -> Path:
    """The autorun's persistence directory under the kit's ``_liveproof`` area."""
    return lake_root / "_liveproof" / "autorun"


def preflight_cache_path(lake_root: Path) -> Path:
    """Where the startup preflight verdict persists."""
    return autorun_dir(lake_root) / "preflight.json"


def probes_cache_path(lake_root: Path) -> Path:
    """Where the one-time probe verdict persists (read instead of re-probing)."""
    return autorun_dir(lake_root) / "probes.json"


def save_verdict(path: Path, verdict: AutorunVerdict) -> None:
    """Persist *verdict* as canonical JSON at *path* — ATOMICALLY (parents created).

    The payload lands in a same-directory ``.tmp`` sibling first and is then
    ``os.replace``-d over *path* (atomic within one filesystem), so a crash
    mid-write can never leave a torn cache: *path* either still holds the
    previous verdict byte-complete or the new one byte-complete.

    Raises ``OSError`` on an unwritable volume — the caller decides whether
    that is fatal (the in-app holder logs and keeps the in-memory verdict).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(verdict.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_verdict(path: Path) -> AutorunVerdict | None:
    """Load a cached verdict from *path*, or ``None`` for missing/corrupt/drifted.

    Never raises: an unreadable file, invalid BYTES (``UnicodeDecodeError``
    is a ``ValueError`` — a torn write / disk corruption must fail-closed to
    "no cache", never crash the holder), invalid JSON
    (``json.JSONDecodeError`` is a ``ValueError`` too), pathologically DEEP
    nesting (the JSON parser raises ``RecursionError``, which is neither —
    it must fail-closed the same way, not kill the holder), or a
    schema/shape mismatch all read as "no cache" (the next run re-computes
    — bounded).
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return verdict_from_payload(payload)


# ────────────────────────────────────────────────────────────────────────
# Report summarization (pure)
# ────────────────────────────────────────────────────────────────────────


def summarize_report(
    report: KitReport, *, required: frozenset[str], now: datetime
) -> AutorunVerdict:
    """Map one kit *report* onto its display verdict — pure, O(stages).

    Kind policy (honest — never a fake green):

    * any stage ``FAIL`` → ``fail``;
    * a *required* stage missing or not PASS/WARN (e.g. SKIPped because a
      prerequisite fell over) → ``fail`` — the proof did not happen;
    * else any ``WARN`` → ``warn``;
    * else ``pass`` — and only then, because every required vendor-proof
      stage genuinely PASSed.

    ``lines`` carry every non-PASS stage's kit lines VERBATIM (the catalog is
    the single wording source); a green verdict has no diagnostic lines.

    Parameters
    ----------
    report
        The finished :class:`~brutex.doctor_live.KitReport`.
    required
        Stage NAMES whose genuine PASS/WARN the verdict requires
        (:data:`PREFLIGHT_REQUIRED` / :data:`PROBES_REQUIRED`).
    now
        The verdict timestamp (UTC).

    Returns
    -------
    AutorunVerdict
        The display-ready verdict.
    """
    statuses = {outcome.name: outcome.status for outcome in report.outcomes}
    lines: list[str] = []
    for outcome in report.outcomes:
        if outcome.status != STATUS_PASS:
            lines.extend(outcome.lines)
    required_ok = all(statuses.get(name) in (STATUS_PASS, STATUS_WARN) for name in required)
    if any(outcome.status == STATUS_FAIL for outcome in report.outcomes) or not required_ok:
        kind = KIND_FAIL
    elif any(outcome.status == STATUS_WARN for outcome in report.outcomes):
        kind = KIND_WARN
    else:
        kind = KIND_PASS
    return AutorunVerdict(
        kind=kind,
        headline=HEADLINES[kind],
        checked_at_utc=now.isoformat(),
        run_id=report.run_id,
        lines=tuple(lines),
        stages=tuple((outcome.stage, outcome.name, outcome.status) for outcome in report.outcomes),
    )


def not_applicable_verdict(fix_text: str, *, now: datetime) -> AutorunVerdict:
    """The honest no-credentials verdict — the NEVER-ATTEMPTED sandbox state.

    *fix_text* is the :class:`~brutex.config.credentials_types.CredentialsError`
    message VERBATIM from the SAME seam the pull refusal relays
    (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001), so the startup banner and the 503
    refusal can never disagree. No kit stage ran; no vendor call was made.
    Reserved for the context where the live pipe was never genuinely
    exercised on this machine — a refused REAL pull attempt, or credentials
    vanishing AFTER real live-proof history, renders
    :func:`credentials_failure_verdict` instead (never grey-over-broken).
    """
    return AutorunVerdict(
        kind=KIND_NOT_APPLICABLE,
        headline=HEADLINES[KIND_NOT_APPLICABLE],
        checked_at_utc=now.isoformat(),
        run_id=None,
        lines=(fix_text,),
        stages=(),
    )


def credentials_failure_verdict(fix_text: str, *, now: datetime) -> AutorunVerdict:
    """Broken credentials rendered HONESTLY RED — a ``fail``, never grey.

    Used when broken credentials are a FAILURE of the live pipe rather than
    the sandbox's not-applicable context: (a) a REAL pull attempt was refused
    by the loud-refusal gate (:meth:`LiveProofAutorun.note_credentials_refusal`
    — the operator tried to pull and could not), and (b) the creds pre-check
    finds no credentials on a machine whose cache shows genuine live-proof
    history (credentials VANISHING after having been exercised is a breakage,
    not a fresh sandbox). *fix_text* stays the relayed CredentialsError text
    VERBATIM — same seam as the 503, so banner and refusal always agree.
    """
    return AutorunVerdict(
        kind=KIND_FAIL,
        headline=HEADLINES[KIND_FAIL],
        checked_at_utc=now.isoformat(),
        run_id=None,
        lines=(fix_text,),
        stages=(),
    )


#: Env vars whose VALUES are credential material — any occurrence of a set
#: value is redacted from autorun-internal error text before persist/UI.
_SECRET_ENV_KEYS: Final[tuple[str, ...]] = (
    "BRUTEX_GROWW_API_KEY",
    "BRUTEX_GROWW_TOTP_SECRET",
)

#: Values shorter than this are never redacted (a 1-3 char env value would
#: false-positive all over ordinary prose; real keys/secrets are far longer).
_MIN_SECRET_LEN: Final[int] = 6

#: The redaction marker.
REDACTED: Final[str] = "[REDACTED]"

#: ``key=value`` / ``key: value`` / ``key='value'`` shapes whose key names
#: credential material — the classic repr-leak (e.g. a raised exception
#: embedding ``GrowwFileCredentials(api_key='…', totp_secret='…')``).
_SECRET_ASSIGNMENT: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(api[_-]?key|totp[_-]?secret|secret|token|password)"
    r"(\s*[=:]\s*)(['\"]?)([^'\"\s,)\]}]+)(\3)"
)


def scrub_secrets(text: str, *, env: Mapping[str, str] | None = None) -> str:
    """Redact obvious secret substrings from *text* (before persist/UI).

    Two belts, both display-safe (over-redaction of an error line is fine):

    1. any occurrence of a KNOWN credential env value
       (:data:`_SECRET_ENV_KEYS`, length ≥ :data:`_MIN_SECRET_LEN`) → the
       :data:`REDACTED` marker;
    2. ``api_key= / totp_secret= / secret= / token= / password=``-shaped
       assignments (incl. quoted repr forms) have their VALUE redacted.

    This is a hygiene belt for autorun-INTERNAL error text (raw exception
    messages, whose content is unbounded), not an anti-exfiltration
    guarantee — the kit's own catalog lines remain the golden-tested primary
    defence and are never re-worded here.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    scrubbed = text
    for key in _SECRET_ENV_KEYS:
        value = (source.get(key) or "").strip()
        if len(value) >= _MIN_SECRET_LEN:
            scrubbed = scrubbed.replace(value, REDACTED)
    return _SECRET_ASSIGNMENT.sub(rf"\g<1>\g<2>\g<3>{REDACTED}\g<5>", scrubbed)


def error_verdict(
    detail: str, *, now: datetime, env: Mapping[str, str] | None = None
) -> AutorunVerdict:
    """An autorun-internal failure verdict (the kit itself crashed) — never green.

    *detail* is the raised exception rendered ``TypeName: message``, passed
    through :func:`scrub_secrets` before it can reach the cache or the UI
    (an exception message is unbounded text and may echo credential
    material). This is the ONE non-catalog line the autorun emits, because
    it diagnoses the autorun/kit machinery, not the pull path (the kit's
    exit-code-3 class).
    """
    return AutorunVerdict(
        kind=KIND_ERROR,
        headline=HEADLINES[KIND_ERROR],
        checked_at_utc=now.isoformat(),
        run_id=None,
        lines=(scrub_secrets(detail, env=env),),
        stages=(),
    )


# ────────────────────────────────────────────────────────────────────────
# Kit composition
# ────────────────────────────────────────────────────────────────────────


def probe_controller_factory() -> RateController:
    """The bounded controller PROBE runs inject — 1 r/s under a 16/min hard slice.

    HONEST composition with the account-level 500/min cap: this is a
    SEPARATE :class:`RateController` instance from the pull job's — rolling
    per-minute governors do NOT share state across instances — so the cap
    must hold by BUDGET SPLIT, not by a shared governor. The split: the
    production pull ceiling is 8 r/s (``GROWW_RATE_LIMIT_RPS_CEILING``) →
    ≤ 480 issues/min worst-case; probes get the dedicated
    :data:`AUTORUN_PROBE_CEILING_PER_MIN` = 16/min slice → ``480 + 16 = 496
    ≤ GROWW_RATE_LIMIT_PER_MIN`` even when a probe run stacks on a
    full-throttle in-flight pull, with 4/min headroom absorbing
    window-boundary fencepost counting (see the constant's note). Both
    bounds only ever LOWER the issue rate (CLAUDE.md §2 Rule 6 — a cap,
    never a raise).
    """
    return RateController(
        ceiling_rps=AUTORUN_PROBE_CEILING_RPS,
        ceiling_per_min=AUTORUN_PROBE_CEILING_PER_MIN,
    )


def run_kit_stages(
    stages: frozenset[int],
    *,
    lake_root: Path,
    deps_factory: Callable[[], KitDeps] | None = None,
    controller_factory: Callable[[], RateController] | None = None,
) -> KitReport:
    """Run the live-proof kit over *stages* with ``keep_going`` (the autorun mode).

    ``keep_going=True`` so one failing cheap stage still lets its siblings
    report (the banner shows the WHOLE cheap picture); the kit's own
    fail-fast dependency skips (no creds → token SKIPs, no client → probes
    SKIP) still bound the vendor spend. Every run stays inside the kit's
    default 40-call budget; writes stay quarantined under
    ``<lake_root>/_liveproof/<run_id>/`` exactly as the manual CLI run.

    Parameters
    ----------
    stages
        The stage-id subset (:data:`PREFLIGHT_STAGES` / :data:`PROBE_STAGES`).
    lake_root
        The resolved lake root (transcripts + quarantined writes land here).
    deps_factory
        The :class:`~brutex.doctor_live.KitDeps` composition; tests inject
        the offline fake deps. ``None`` (the production default) composes
        :func:`~brutex.doctor_live.build_real_deps`, resolved LATE — a
        module-global lookup at CALL time, not a default bound at import —
        so a test-belt stub of ``build_real_deps`` genuinely intercepts the
        real network composition.
    controller_factory
        Optional pacing override (:func:`probe_controller_factory` for probe
        runs). ``None`` keeps the deps' own controller.

    Returns
    -------
    KitReport
        The finished kit report.
    """
    deps = (build_real_deps if deps_factory is None else deps_factory)()
    if controller_factory is not None:
        deps.controller_factory = controller_factory
    options = KitOptions(lake_root=lake_root, stages=stages, keep_going=True)
    return run_live_doctor(deps, options)


def start_daemon_thread(fn: Callable[[], None], name: str) -> None:
    """Run *fn* on a named daemon thread (the production thread starter).

    Mirrors the dashboard's instant-launch seams (DEC-DASHBOARD-INSTANT-
    LAUNCH-001): the caller returns immediately; the server bind is never
    blocked. Tests inject a synchronous starter instead.
    """
    threading.Thread(target=fn, name=name, daemon=True).start()


# ────────────────────────────────────────────────────────────────────────
# The in-app autorun holder
# ────────────────────────────────────────────────────────────────────────


class LiveProofAutorun:
    """The dashboard-owned zero-command autorun state machine.

    One instance per dashboard app (injected via ``build_app`` — no
    module-global mutable state, CLAUDE.md §4). Thread-safe: a single lock
    guards the tiny state (states + verdict references); it is NEVER held
    across a kit run or disk I/O. All heavy work runs on daemon threads via
    the injected *thread_starter*, so :meth:`start_preflight` /
    :meth:`on_real_pull` return in O(1) and the server keeps serving.
    """

    def __init__(
        self,
        lake_root: Path,
        *,
        env: Mapping[str, str] | None = None,
        credentials_error: Callable[[], str | None] = groww_credentials_error,
        deps_factory: Callable[[], KitDeps] | None = None,
        preflight_runner: Callable[[], KitReport] | None = None,
        probes_runner: Callable[[], KitReport] | None = None,
        probe_controller: Callable[[], RateController] = probe_controller_factory,
        now_utc: Callable[[], datetime] | None = None,
        thread_starter: Callable[[Callable[[], None], str], None] = start_daemon_thread,
    ) -> None:
        """Build the holder and load any cached verdicts (cheap, offline).

        Parameters
        ----------
        lake_root
            The resolved lake root — verdict cache + kit quarantine area.
        env
            Environment for the :data:`AUTORUN_ENV` toggle + the
            :func:`scrub_secrets` hygiene belt (default ``os.environ``).
        credentials_error
            The reason-carrying creds seam — DEFAULT the same
            :func:`~brutex.config.credentials.groww_credentials_error` the
            pull refusal uses, so banner and refusal agree by construction.
        deps_factory
            Kit deps composition for the default runners (tests inject the
            offline fakes). ``None`` (the production default) lets
            :func:`run_kit_stages` compose the real network deps LATE.
        preflight_runner, probes_runner
            Full runner overrides for tests; ``None`` builds the default
            :func:`run_kit_stages` compositions (probes with the bounded
            *probe_controller*).
        probe_controller
            The pacing factory PROBE runs inject (default the bounded
            1 r/s + 16/min :func:`probe_controller_factory`); tests inject a
            fake-clock controller so the default probes composition runs
            instantly.
        now_utc
            Injected clock (default: real UTC now).
        thread_starter
            Injected thread seam (default: :func:`start_daemon_thread`).
        """
        self._lake_root = lake_root
        self._env: Mapping[str, str] = os.environ if env is None else env
        self._enabled = autorun_enabled(self._env)
        self._credentials_error = credentials_error
        self._deps_factory = deps_factory
        self._preflight_runner = (
            preflight_runner if preflight_runner is not None else self._run_preflight_kit
        )
        self._probes_runner = probes_runner if probes_runner is not None else self._run_probes_kit
        self._probe_controller = probe_controller
        self._now_utc = now_utc if now_utc is not None else _utc_now
        self._start_thread = thread_starter
        self._lock = threading.Lock()
        # Serializes the DISK writers (worker threads + the refusal mirror on
        # a request thread) so two saves can never interleave; never held
        # together with self._lock and never across a kit run.
        self._io_lock = threading.Lock()
        self._preflight_state = STATE_IDLE
        self._probes_state = STATE_IDLE
        self._preflight_verdict = load_verdict(preflight_cache_path(lake_root))
        self._probes_verdict = load_verdict(probes_cache_path(lake_root))

    # ── default kit compositions (offline-testable via deps_factory) ────

    def _run_preflight_kit(self) -> KitReport:
        """The default preflight run — the cheap stages, deps' own pacing."""
        return run_kit_stages(
            PREFLIGHT_STAGES, lake_root=self._lake_root, deps_factory=self._deps_factory
        )

    def _run_probes_kit(self) -> KitReport:
        """The default probes run — bounded to the injected probe controller."""
        return run_kit_stages(
            PROBE_STAGES,
            lake_root=self._lake_root,
            deps_factory=self._deps_factory,
            controller_factory=self._probe_controller,
        )

    # ── triggers ─────────────────────────────────────────────────────────

    def start_preflight(self) -> bool:
        """Kick the startup preflight on a background thread (single-flight).

        Returns ``True`` iff a run was started; ``False`` when disabled or
        already running. Never blocks and NEVER RAISES into the caller: a
        thread-spawn failure rolls the state back to ``idle`` (never a
        wedged ``running``), records an honest ``error`` verdict, and
        returns ``False`` — a later trigger may retry. The worker converts
        every in-run failure into an honest verdict.
        """
        with self._lock:
            if not self._enabled or self._preflight_state == STATE_RUNNING:
                return False
            self._preflight_state = STATE_RUNNING
        try:
            self._start_thread(self._preflight_worker, "brutex-liveproof-preflight")
        except Exception as exc:  # pylint: disable=broad-exception-caught  # spawn boundary
            self._spawn_failed("preflight", exc)
            return False
        return True

    def on_real_pull(self) -> bool:
        """A REAL pull was admitted — run the one-time probes if still needed.

        Skips (returns ``False``) when disabled, already running, or a cached
        settled verdict exists (:func:`cache_usable`, warn-aged against the
        holder's clock). A failed/absent cache re-probes — at most once per
        pull trigger (bounded), never concurrently. NEVER RAISES into the
        pull path: a thread-spawn failure rolls the state back to ``idle``,
        records an honest ``error`` verdict, and returns ``False`` — the
        NEXT real pull retries. The probes share NO state with the pull job
        and pace at the 1 r/s + 16/min probe budget.
        """
        with self._lock:
            if not self._enabled or self._probes_state == STATE_RUNNING:
                return False
            if self._probes_verdict is not None and cache_usable(
                self._probes_verdict, now=self._now_utc()
            ):
                return False
            self._probes_state = STATE_RUNNING
        try:
            self._start_thread(self._probes_worker, "brutex-liveproof-probes")
        except Exception as exc:  # pylint: disable=broad-exception-caught  # spawn boundary
            self._spawn_failed("probes", exc)
            return False
        return True

    def note_credentials_refusal(self, fix_text: str) -> None:
        """A REAL pull attempt was refused for broken credentials — RED, never grey.

        Called by the loud-refusal gate with the SAME relayed fix text its
        HTTP 503 carries, so the persistent status element and the refusal
        agree even when credentials broke AFTER startup. The stamped verdict
        is a ``fail`` (:func:`credentials_failure_verdict`): an ATTEMPTED
        live pull that could not proceed IS a failure of the live pipe, so
        this can never downgrade a red verdict to the grey
        ``not_applicable`` (that kind stays reserved for the
        never-attempted sandbox context — see :meth:`_compute`). No-op when
        disabled.
        """
        if not self._enabled:
            return
        verdict = credentials_failure_verdict(fix_text, now=self._now_utc())
        with self._lock:
            self._preflight_verdict = verdict
            if self._preflight_state != STATE_RUNNING:
                self._preflight_state = STATE_DONE
        self._persist(preflight_cache_path(self._lake_root), verdict, label="preflight")

    def _spawn_failed(self, label: str, exc: Exception) -> None:
        """A worker thread could not be SPAWNED — roll back + report honestly.

        The flow's state returns to ``idle`` FIRST — BEFORE the error verdict
        is even constructed: the construction touches the injected clock and
        the scrub belt, and a raise inside it must never re-wedge the state
        at ``running`` (which would permanently refuse every later trigger).
        Then an honest ``error`` verdict lands in memory + on disk, so the
        banner shows the machinery failure instead of silent nothing. The
        NEXT trigger may retry (an ``error`` cache is never
        :func:`cache_usable`).
        """
        _LOG.error("liveproof.thread_spawn_failed", label=label, error=str(exc))
        with self._lock:
            if label == "preflight":
                self._preflight_state = STATE_IDLE
            else:
                self._probes_state = STATE_IDLE
        verdict = error_verdict(f"{type(exc).__name__}: {exc}", now=self._now_utc(), env=self._env)
        if label == "preflight":
            with self._lock:
                self._preflight_verdict = verdict
            self._persist(preflight_cache_path(self._lake_root), verdict, label=label)
        else:
            with self._lock:
                self._probes_verdict = verdict
            self._persist(probes_cache_path(self._lake_root), verdict, label=label)

    # ── workers ──────────────────────────────────────────────────────────

    def _preflight_worker(self) -> None:
        """Compute + publish + persist the preflight verdict (thread body)."""
        with self._lock:
            prior = self._preflight_verdict
        verdict = self._compute(self._preflight_runner, required=PREFLIGHT_REQUIRED, prior=prior)
        with self._lock:
            self._preflight_verdict = verdict
            self._preflight_state = STATE_DONE
        self._persist(preflight_cache_path(self._lake_root), verdict, label="preflight")
        _LOG.info("liveproof.preflight.done", kind=verdict.kind, run_id=verdict.run_id)

    def _probes_worker(self) -> None:
        """Compute + publish + persist the probes verdict (thread body)."""
        with self._lock:
            prior = self._probes_verdict
        verdict = self._compute(self._probes_runner, required=PROBES_REQUIRED, prior=prior)
        with self._lock:
            self._probes_verdict = verdict
            self._probes_state = STATE_DONE
        self._persist(probes_cache_path(self._lake_root), verdict, label="probes")
        _LOG.info("liveproof.probes.done", kind=verdict.kind, run_id=verdict.run_id)

    def _compute(
        self,
        runner: Callable[[], KitReport],
        *,
        required: frozenset[str],
        prior: AutorunVerdict | None,
    ) -> AutorunVerdict:
        """One guarded verdict computation: creds pre-check → kit run → summary.

        The credentials pre-check SHORT-CIRCUITS before any kit stage, so the
        no-credentials mode makes ZERO vendor/network calls (the spy-tested
        invariant). With no credentials the verdict depends on HISTORY: on a
        machine whose *prior* verdict shows this flow genuinely ran before
        (any kind other than ``not_applicable``), credentials VANISHING is a
        breakage → an honest RED :func:`credentials_failure_verdict`; only
        the never-attempted context (no prior, or a prior grey) stays
        ``not_applicable`` — grey must never paper over broken credentials.
        Any exception — kit-internal or unexpected — becomes an honest
        ``error`` verdict (secret-scrubbed); the thread never dies silently.
        """
        try:
            fix_text = self._credentials_error()
        except Exception as exc:  # pylint: disable=broad-exception-caught  # thread boundary
            _LOG.error("liveproof.creds_check.error", error=str(exc))
            return error_verdict(f"{type(exc).__name__}: {exc}", now=self._now_utc(), env=self._env)
        if fix_text is not None:
            if prior is not None and prior.kind != KIND_NOT_APPLICABLE:
                return credentials_failure_verdict(fix_text, now=self._now_utc())
            return not_applicable_verdict(fix_text, now=self._now_utc())
        try:
            report = runner()
        except Exception as exc:  # pylint: disable=broad-exception-caught  # thread boundary
            _LOG.error("liveproof.kit.error", error=str(exc), error_type=type(exc).__name__)
            return error_verdict(f"{type(exc).__name__}: {exc}", now=self._now_utc(), env=self._env)
        return summarize_report(report, required=required, now=self._now_utc())

    def _persist(self, path: Path, verdict: AutorunVerdict, *, label: str) -> None:
        """Serialized, atomic, best-effort disk cache write — a failure logs.

        The io-lock serializes the writers (the worker threads + the refusal
        mirror on a request thread — two of them target ``preflight.json``),
        so saves can never interleave; :func:`save_verdict`'s tmp+``replace``
        additionally keeps each single write atomic against a crash.
        """
        try:
            with self._io_lock:
                save_verdict(path, verdict)
        except OSError as exc:
            _LOG.warning("liveproof.cache_write_failed", label=label, error=str(exc))

    # ── read surface ─────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """The O(1) JSON status for ``/api/v1/liveproof`` (lock-guarded snapshot)."""
        with self._lock:
            preflight = self._preflight_verdict
            probes = self._probes_verdict
            return {
                "enabled": self._enabled,
                "preflight": {
                    "state": self._preflight_state,
                    "verdict": preflight.to_dict() if preflight is not None else None,
                },
                "probes": {
                    "state": self._probes_state,
                    "verdict": probes.to_dict() if probes is not None else None,
                },
            }


def _utc_now() -> datetime:
    """The default injected clock — real UTC now."""
    return datetime.now(UTC)


__all__ = [
    "AUTORUN_ENV",
    "AUTORUN_PROBE_CEILING_PER_MIN",
    "AUTORUN_PROBE_CEILING_RPS",
    "AUTORUN_WARN_RECHECK_DAYS",
    "HEADLINES",
    "KIND_ERROR",
    "KIND_FAIL",
    "KIND_NOT_APPLICABLE",
    "KIND_PASS",
    "KIND_WARN",
    "PREFLIGHT_REQUIRED",
    "PREFLIGHT_STAGES",
    "PROBES_REQUIRED",
    "PROBE_STAGES",
    "REDACTED",
    "STATE_DONE",
    "STATE_IDLE",
    "STATE_RUNNING",
    "VERDICT_SCHEMA",
    "AutorunVerdict",
    "LiveProofAutorun",
    "autorun_dir",
    "autorun_enabled",
    "cache_usable",
    "credentials_failure_verdict",
    "error_verdict",
    "load_verdict",
    "not_applicable_verdict",
    "preflight_cache_path",
    "probe_controller_factory",
    "probes_cache_path",
    "run_kit_stages",
    "save_verdict",
    "scrub_secrets",
    "start_daemon_thread",
    "summarize_report",
    "verdict_from_payload",
]
