"""Pull-scope registry + spot-only no-op reports + note surgery + crossverify tail.

DEC-PULL-SCOPED-ALL-ONECLICK-001 (MIGRATION.md #1779). The ONE home for the
``scope=`` token vocabulary the ``/pull-pipeline`` launcher and the headless
nightly binding share, the spot-only arm's no-op F&O phase reports (they pass
the unchanged :mod:`~brutex.dashboard.pipeline_pull` gates vacuously — the
sequencer itself is never edited), the deterministic NOTE SURGERY that keeps a
spot-only completion note honest (a designed F&O no-op must never read like a
real empty vendor enumeration — the B H1 negative pin), and the fail-soft
CROSSVERIFY TAIL seam + fence both surfaces (dashboard launcher + the
``brutex pull-all-spot`` CLI) run strictly AFTER the pull chain settled.

Leaf discipline: imports ONLY the two report dataclasses + the lake-halt error
— never ``app``/``pipeline_pull`` (no cycle; the sequencer stays byte-untouched).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import structlog

from brutex.dashboard.composite_pull import CompositePullReport
from brutex.dashboard.metadata_pull import MetadataPullReport
from brutex.data.errors import LakeWriteHalt

_LOG = structlog.get_logger("brutex.dashboard.pull_scope")

__all__ = [
    "FNO_SKIPPED_CLAUSE",
    "GREEKS_NOT_RUN_NOTE",
    "KNOWN_PULL_SCOPES",
    "SCOPE_FULL",
    "SCOPE_SPOT_ONLY",
    "SPOT_ONLY_HISTORICAL_NOTE",
    "SPOT_ONLY_METADATA_NOTE",
    "TAIL_ALERT_STATUSES",
    "CrossverifyTail",
    "CrossverifyTailOutcome",
    "build_spot_only_note",
    "crossverify_clause",
    "normalize_pull_scope",
    "run_crossverify_tail",
    "spot_only_historical_report",
    "spot_only_metadata_report",
]

#: The default scope — the full spot→metadata→historical chain, byte-identical
#: to every pre-scope ``/pull-pipeline`` request.
SCOPE_FULL: Final[str] = "full"

#: The scoped-ALL spot pull (DEC-PULL-SCOPED-ALL-ONECLICK-001): every admitted
#: index + stock, 1-minute, 2020-01-01 → prev day, F&O phases structurally
#: replaced by the no-op reports below. NO options, NO Greeks.
SCOPE_SPOT_ONLY: Final[str] = "spot-only"

#: The whole scope vocabulary. Phase 2 adds e.g. ``"spot+options"`` HERE — one
#: token, no launcher rewrite (the launcher inherits every rail either way).
KNOWN_PULL_SCOPES: Final[frozenset[str]] = frozenset({SCOPE_FULL, SCOPE_SPOT_ONLY})


def normalize_pull_scope(token: str | None) -> str:
    """Normalize a ``scope=`` query/CLI token — fail-loud on the unknown.

    ``None`` / blank / whitespace-only → :data:`SCOPE_FULL` (the byte-identical
    default); a known token passes verbatim; anything else raises
    :class:`ValueError` (the route maps it to a clean 400, the CLI to exit 1).
    A typo'd scope must never silently widen OR narrow a vendor pull (B H9).
    """
    stripped = (token or "").strip()
    if not stripped:
        return SCOPE_FULL
    if stripped in KNOWN_PULL_SCOPES:
        return stripped
    known = ", ".join(sorted(KNOWN_PULL_SCOPES))
    raise ValueError(f"unknown pull scope {token!r}; expected one of: {known}")


#: The machine tell-apart notes (B H1): a spot-only no-op report is BY DESIGN,
#: never a real empty vendor result — the note + the empty ``years=()`` /
#: ``underlyings=()`` tuples distinguish it from a genuine empty enumeration.
SPOT_ONLY_METADATA_NOTE: Final[str] = (
    "spot-only scope — F&O metadata NOT pulled (by design, not an empty vendor result)"
)
SPOT_ONLY_HISTORICAL_NOTE: Final[str] = (
    "spot-only scope — F&O historical NOT pulled (by design, not an empty vendor result)"
)


def spot_only_metadata_report() -> MetadataPullReport:
    """The all-zeros METADATA no-op the spot-only arm binds as ``run_metadata``.

    Zero failures + zero deferrals pass the REAL
    :func:`~brutex.dashboard.pipeline_pull.metadata_phase_ok` gate vacuously;
    ``years=()`` / ``underlyings=()`` are the machine tell-apart from a real
    empty enumeration (which always carries the requested year grid — B H1).
    """
    return MetadataPullReport(
        dry_run=False,
        years=(),
        underlyings=(),
        expiry_cells=0,
        cells_skipped=0,
        expiry_failures=0,
        expiries_persisted=0,
        contract_jobs=0,
        contract_failures=0,
        contracts_persisted=0,
        note=SPOT_ONLY_METADATA_NOTE,
        store_write_deferrals=0,
    )


def spot_only_historical_report() -> CompositePullReport:
    """The HISTORICAL no-op the spot-only arm binds as ``run_historical``.

    ``historical=None`` is the documented no-op arm of
    :func:`~brutex.dashboard.pipeline_pull.historical_phase_ok` ("a zero-call
    no-op, NOT a failure" — it returns ``True``), and ``metadata=None`` skips
    the inner gate — so the completed-chain path (prebuild + movers) fires
    with ZERO F&O vendor calls, structurally.
    """
    return CompositePullReport(
        dry_run=False,
        requested=(),
        tradable=(),
        skipped_reference_only=(),
        metadata=None,
        historical=None,
        note=SPOT_ONLY_HISTORICAL_NOTE,
    )


# ── note surgery (B negative pins + C wording; SURGERY, not a suffix-append) ──

#: The explicit Greeks honesty constant (C 5): the sequencer note renders
#: NOTHING for a Greeks pass that never ran (``ok=None``), so the spot-only
#: note must SAY it — a structurally-skipped stage is named, never implied.
GREEKS_NOT_RUN_NOTE: Final[str] = "Greeks: not run — no options in this pull"

#: What the no-op F&O clause becomes: an honest by-design statement.
FNO_SKIPPED_CLAUSE: Final[str] = ", F&O skipped by design (spot-only scope)"

#: The EXACT wording the all-zeros no-op reports produce inside
#: ``pipeline_pull._complete_note`` (pipeline_pull.py — ``metadata
#: {expiries_persisted} expiries + {contracts_persisted} contracts, historical
#: {bars} bars over {contracts} contract(s)`` with every count 0). Surgically
#: REPLACED — a "0 expiries" wording on a spot-only pull would read like a
#: real empty vendor result (B H1). Exact-string coupled to ``_complete_note``
#: by design; the e2e negative drift pin + the surgery-miss WARN below guard a
#: future wording refactor (it must force a conscious re-pin, never a silent lie).
_NOOP_FNO_CLAUSE: Final[str] = (
    ", metadata 0 expiries + 0 contracts, historical 0 bars over 0 contract(s)"
)

#: The EXACT trailing clause ``pipeline_pull._spot_stop_note`` renders on a
#: spot-gate stop ("stopped after spot — … ; metadata + historical NOT
#: started"). On a SPOT-ONLY run those phases were designed no-ops that were
#: never going to run, so the generic wording is replaced by the honest
#: spot-only clause below (hostile-review r2 #3 — cosmetic accuracy; a drifted
#: wording simply passes through unchanged, no WARN — the clause is
#: diagnostic, never load-bearing).
_STOP_FNO_CLAUSE: Final[str] = "metadata + historical NOT started"

#: The spot-only replacement for :data:`_STOP_FNO_CLAUSE`.
_STOP_FNO_SPOT_ONLY_CLAUSE: Final[str] = "F&O not applicable (spot-only scope)"

#: The completed-chain note prefix ``_complete_note`` composes — the ledger
#: failure prefix, when present, sits BEFORE it and stays LEADING (B R3/H5).
_FULL_PREFIX: Final[str] = "pipeline complete — "

#: The spot-only replacement prefix.
_SPOT_ONLY_PREFIX: Final[str] = "spot-only pipeline complete — "


# ── crossverify tail seam (production default = REAL; fail-soft fence) ───────


@dataclass(frozen=True, slots=True)
class CrossverifyTailOutcome:
    """One tail invocation's machine status + the exact human clause tail.

    ``status`` vocabulary (pinned): ``"started"`` / ``"already_running"`` /
    ``"disabled"`` / ``"not_configured"`` / ``"skipped_dry_run"`` /
    ``"skipped_pipeline_stopped"`` / ``"failed"`` — plus, on the CLI arm, any
    :data:`brutex.crossverify.run.Verdict` token passed through verbatim
    (``"published"``, ``"staged_only"``, ``"nothing_to_do"``,
    ``"skipped_non_trading"``, ``"skipped_muhurat"``,
    ``"deferred_market_hours"``, ``"refused"``, ``"withheld_partial"``,
    ``"blocked"``, ``"publish_failed"``, ``"suspect"``).
    """

    status: str
    human: str  # the exact clause tail (never empty)


#: The tail statuses ``--strict-exit`` escalates (the crossverify exit-2
#: verdict family + the fence's own ``failed``). ``disabled`` /
#: ``not_configured`` / the skip family never escalate.
TAIL_ALERT_STATUSES: Final[frozenset[str]] = frozenset(
    {"failed", "refused", "withheld_partial", "blocked", "publish_failed", "suspect"}
)

#: A zero-arg tail — closures capture their own deps (config/seams/job).
CrossverifyTail = Callable[[], CrossverifyTailOutcome]


def crossverify_clause(outcome: CrossverifyTailOutcome) -> str:
    """The ONE ``", cross-verify: …"`` note-clause renderer both surfaces use."""
    return f", cross-verify: {outcome.human}"


def run_crossverify_tail(
    tail: CrossverifyTail | None,
    *,
    dry_run: bool,
    stopped_after: str | None,
) -> CrossverifyTailOutcome:
    """The fail-soft crossverify-tail fence — run strictly AFTER the pull settled.

    Invoked once the sequencer has RETURNED (fetch done + ledger durable) and
    — on both surfaces — strictly OUTSIDE our own held pull single-flight (the
    crossverify runner acquires the SAME account→lake→lease trio itself with a
    bounded wait-retry; nesting would be guaranteed contention — ruling 8).

    Order of arms:

    * ``dry_run``            → ``skipped_dry_run`` (the tail is NEVER invoked);
    * ``stopped_after`` set  → ``skipped_pipeline_stopped`` (a deferred/stopped
      pull has nothing settled to verify);
    * ``tail is None``       → ``not_configured`` (reachable only via injection
      — production binds the real tail on both surfaces);
    * the tail raising :class:`~brutex.data.errors.LakeWriteHalt` RE-RAISES
      (B M6 — a disk-full stays loud; structurally near-unreachable since
      ``run_crossverify`` never raises for data problems);
    * any other raise → ``failed`` + ONE ``pull_scope.crossverify_tail_failed``
      WARN — the spot pull is NEVER failed by a broken verify tail (fail-soft
      decree);
    * else the tail's own outcome.
    """
    if dry_run:
        return CrossverifyTailOutcome(status="skipped_dry_run", human="skipped (dry-run)")
    if stopped_after is not None:
        return CrossverifyTailOutcome(
            status="skipped_pipeline_stopped",
            human=f"skipped (pipeline stopped: {stopped_after})",
        )
    if tail is None:
        return CrossverifyTailOutcome(
            status="not_configured", human="not configured (no tail bound)"
        )
    try:
        return tail()
    except LakeWriteHalt:
        raise
    except Exception as exc:
        _LOG.warning(
            "pull_scope.crossverify_tail_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return CrossverifyTailOutcome(
            status="failed",
            human=f"FAILED ({type(exc).__name__}: {exc}) — pull unaffected",
        )


def build_spot_only_note(base_note: str, tail: CrossverifyTailOutcome) -> str:
    """Deterministic SURGERY on the sequencer's composed note (never a bare append).

    1. Replace the exact :data:`_NOOP_FNO_CLAUSE` (what the all-zeros no-op
       reports render through ``_complete_note``) with
       :data:`FNO_SKIPPED_CLAUSE` — the B H1 honesty law: a by-design no-op
       must never read like a real empty vendor result.
    2. Replace the first :data:`_FULL_PREFIX` with :data:`_SPOT_ONLY_PREFIX`
       (the ledger-failure prefix, when present, stays LEADING; the quarantine
       clause lives INSIDE *base_note* after the prefix and still leads —
       B R3/H5).
    3. Append ``", " + GREEKS_NOT_RUN_NOTE + crossverify_clause(tail)``.

    A STOPPED/deferred note (neither anchor present — e.g. "stopped after
    spot — …" / the market-hours deferral) gets NO surgery and no WARN: it
    makes no F&O claim, so only step 3's honest clauses are appended — except
    the spot-stop note's generic "metadata + historical NOT started" trailer,
    which reads spot-only-aware as ``F&O not applicable (spot-only scope)``
    (hostile-review r2 #3; those phases were designed no-ops here). A note
    carrying exactly ONE anchor is a ``_complete_note`` wording DRIFT — log
    ONE ``pull_scope.note_surgery_miss`` WARN and still append step 3; the
    e2e negative pin (``test_e2e_no_zero_expiries_wording…``) fails loudly on
    a full drift, forcing a conscious re-pin.
    """
    note = base_note
    if _STOP_FNO_CLAUSE in note:
        note = note.replace(_STOP_FNO_CLAUSE, _STOP_FNO_SPOT_ONLY_CLAUSE, 1)
    prefix_present = _FULL_PREFIX in note
    clause_present = _NOOP_FNO_CLAUSE in note
    if clause_present:
        note = note.replace(_NOOP_FNO_CLAUSE, FNO_SKIPPED_CLAUSE, 1)
    if prefix_present:
        note = note.replace(_FULL_PREFIX, _SPOT_ONLY_PREFIX, 1)
    if prefix_present != clause_present:
        _LOG.warning(
            "pull_scope.note_surgery_miss",
            prefix_present=prefix_present,
            fno_clause_present=clause_present,
            base_note=base_note[:200],
        )
    return f"{note}, {GREEKS_NOT_RUN_NOTE}{crossverify_clause(tail)}"
