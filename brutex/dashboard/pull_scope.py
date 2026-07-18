"""Pull-scope registry + scoped-arm no-op reports + note surgery + crossverify tail.

DEC-PULL-SCOPED-ALL-ONECLICK-001 (MIGRATION.md #1779). The ONE home for the
``scope=`` token vocabulary the ``/pull-pipeline`` launcher and the headless
nightly binding share, the scoped arms' no-op phase reports (the spot-only
arm's no-op F&O pair and — since the round-6 two-button split — the fno-only
arm's no-op SPOT report; each passes the unchanged
:mod:`~brutex.dashboard.pipeline_pull` gates vacuously — the sequencer itself
is never edited), the deterministic NOTE SURGERY that keeps a scoped
completion note honest (a designed no-op phase must never read like a real
empty vendor result — the B H1 negative pin, both axes), and the fail-soft
CROSSVERIFY TAIL seam + fence both SPOT-ONLY surfaces (dashboard launcher +
the ``brutex pull-all-spot`` CLI) run strictly AFTER the pull chain settled —
the tail is Spot-EXCLUSIVE by the round-6 ruling (an fno-only run never fires
it, exactly like the full-scope arm).

Leaf discipline: imports ONLY the three report dataclasses + the lake-halt
error + the :mod:`brutex.pull_state_rust` LEAF (stdlib+structlog only) —
never ``app``/``pipeline_pull`` (no cycle; the sequencer stays
byte-untouched).

DEC-RUST-PULL-STATE-001 (lane Wave 1B): behind the default-OFF
``BRUTEX_PULL_STATE_RUST`` flag the PURE surface here — the scope-token
verdict + the market-hours rewrite + both note surgeries — is served by the
``brutex_pull_state`` Rust kernel (byte-exact string laws, golden- +
differential-pinned); the Python arms below are RETAINED VERBATIM as the
live parity oracle and serve every flag-OFF call byte-identically, and
every flag-ON degrade (wheel missing/stale, kernel raise, poisoned result —
each a named warn-once in :mod:`brutex.pull_state_rust`). The
``note_surgery_miss`` WARN, the step-3 appends, the no-op report builders
and the crossverify tail fence stay Python permanently.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import structlog

from brutex import pull_state_rust as _pull_state_rust
from brutex.dashboard.composite_pull import CompositePullReport
from brutex.dashboard.metadata_pull import MetadataPullReport
from brutex.dashboard.pull_controller import BulkSpotReport
from brutex.data.errors import LakeWriteHalt

_LOG = structlog.get_logger("brutex.dashboard.pull_scope")

__all__ = [
    "FNO_SKIPPED_CLAUSE",
    "GREEKS_NOT_RUN_NOTE",
    "KNOWN_PULL_SCOPES",
    "SCOPE_FNO_ONLY",
    "SCOPE_FULL",
    "SCOPE_SPOT_ONLY",
    "SPOT_ONLY_HISTORICAL_NOTE",
    "SPOT_ONLY_METADATA_NOTE",
    "SPOT_ONLY_SESSION_RESUME_HINT",
    "SPOT_SKIPPED_CLAUSE",
    "TAIL_ALERT_STATUSES",
    "CrossverifyTail",
    "CrossverifyTailOutcome",
    "build_fno_only_note",
    "build_spot_only_note",
    "crossverify_clause",
    "fno_only_spot_report",
    "normalize_pull_scope",
    "rewrite_market_hours_note",
    "run_crossverify_tail",
    "spot_only_historical_report",
    "spot_only_metadata_report",
]

#: The default scope — the full spot→metadata→historical chain, byte-identical
#: to every pre-scope ``/pull-pipeline`` request.
SCOPE_FULL: Final[str] = "full"

#: The scoped-ALL spot pull (DEC-PULL-SCOPED-ALL-ONECLICK-001): every admitted
#: index + stock, 1-minute, 2020-01-01 → prev day, F&O phases structurally
#: replaced by the no-op reports below. NO options, NO Greeks. That whole-
#: universe full-range shape is what the button + the CLI always send; the
#: ``/pull-pipeline`` ROUTE may additionally combine the token with a
#: ``symbols=``/``symbol=`` selection and/or a ``from_date=``/``to_date=``
#: range — a SUPPORTED NARROWING (a smaller spot-only pull; the token only
#: swaps the two F&O phases, so the combination can never widen a vendor
#: pull — refuter round 3 F-D2, route-pinned).
SCOPE_SPOT_ONLY: Final[str] = "spot-only"

#: The F&O-only pull (DEC-PULL-SCOPED-ALL-ONECLICK-001 round-6 two-button
#: split — the coordinator-ruled EXACT MIRROR of :data:`SCOPE_SPOT_ONLY`):
#: the SPOT phase is structurally replaced by the honest no-op
#: :func:`fno_only_spot_report` (it passes the unchanged ``spot_phase_ok``
#: gate vacuously), while METADATA + F&O HISTORICAL run for REAL over the
#: same whole-universe selection the full pipeline uses; the prebuild runs
#: the F&O cascade only (``include_spot=False`` — the spot-exclusion mirror
#: of the spot-only empty-cascade arm) and only the futures + options movers
#: warmers are injected. MANUAL only: the crossverify tail never fires
#: (Spot-exclusive by ruling) and the auto-pull scheduler never arms/fires
#: this scope (``auto_arm`` stays a 400 on it). The route may combine the
#: token with ``symbols=``/``from_date=``/``to_date=`` — a SUPPORTED
#: NARROWING exactly like spot-only (the token only swaps the spot phase, so
#: the combination can never widen a vendor pull).
SCOPE_FNO_ONLY: Final[str] = "fno-only"

#: The whole scope vocabulary. Phase 2 adds e.g. ``"spot+options"`` HERE — one
#: token, no launcher rewrite (the launcher inherits every rail either way).
KNOWN_PULL_SCOPES: Final[frozenset[str]] = frozenset({SCOPE_FULL, SCOPE_SPOT_ONLY, SCOPE_FNO_ONLY})

#: The Rust kernel's ``scope_token`` verdict-code → scope-token map (Wave 1B;
#: a known token's stripped form IS its canonical constant, so serving the
#: constant is byte-identical to the oracle's ``stripped`` return).
_SCOPE_BY_CODE: Final[dict[int, str]] = {0: SCOPE_FULL, 1: SCOPE_SPOT_ONLY, 2: SCOPE_FNO_ONLY}


def normalize_pull_scope(token: str | None) -> str:
    """Normalize a ``scope=`` query/CLI token — fail-loud on the unknown.

    ``None`` / blank / whitespace-only → :data:`SCOPE_FULL` (the byte-identical
    default); a known token passes verbatim; anything else raises
    :class:`ValueError` (the route maps it to a clean 400, the CLI to exit 1).
    A typo'd scope must never silently widen OR narrow a vendor pull (B H9).

    DEC-RUST-PULL-STATE-001 (Wave 1B): flag-ON, the KNOWN-token verdict is
    served by the Rust kernel (CPython-``str.strip``-parity trim, golden- +
    differential-pinned); an UNKNOWN verdict falls through to the oracle
    below, which composes the ``{token!r}`` refusal itself (CPython repr is
    never re-minted in Rust) — so the raise path is oracle-served on BOTH
    arms, and a kernel degrade can never change a verdict.
    """
    if _pull_state_rust.pull_state_rust_enabled():
        verdict = _pull_state_rust.rust_scope_token(token)
        if verdict is not None and verdict != _pull_state_rust.SCOPE_CODE_UNKNOWN:
            return _SCOPE_BY_CODE[verdict]
    # The Python oracle — the byte-identical retained arm: flag OFF takes it
    # untouched; a flag-ON kernel degrade OR unknown-token verdict falls
    # through to it (never a raise from the seam itself).
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


def spot_only_metadata_report(dry_run: bool = False) -> MetadataPullReport:
    """The all-zeros METADATA no-op the spot-only arm binds as ``run_metadata``.

    Zero failures + zero deferrals pass the REAL
    :func:`~brutex.dashboard.pipeline_pull.metadata_phase_ok` gate vacuously;
    ``years=()`` / ``underlyings=()`` are the machine tell-apart from a real
    empty enumeration (which always carries the requested year grid — B H1).
    *dry_run* mirrors the LAUNCH's flag (refuter-r1-round r2 NIT-6: a dry-run
    payload's nested sub-report must never read ``dry_run: false`` inside an
    overall ``dry_run: true`` job result); the report is a designed no-op
    either way — the flag changes NOTHING but the echoed field.
    """
    return MetadataPullReport(
        dry_run=dry_run,
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


def spot_only_historical_report(dry_run: bool = False) -> CompositePullReport:
    """The HISTORICAL no-op the spot-only arm binds as ``run_historical``.

    ``historical=None`` is the documented no-op arm of
    :func:`~brutex.dashboard.pipeline_pull.historical_phase_ok` ("a zero-call
    no-op, NOT a failure" — it returns ``True``), and ``metadata=None`` skips
    the inner gate — so the completed-chain path (prebuild + movers) fires
    with ZERO F&O vendor calls, structurally. *dry_run* mirrors the LAUNCH's
    flag (refuter-r1-round r2 NIT-6 — see :func:`spot_only_metadata_report`).
    """
    return CompositePullReport(
        dry_run=dry_run,
        requested=(),
        tradable=(),
        skipped_reference_only=(),
        metadata=None,
        historical=None,
        note=SPOT_ONLY_HISTORICAL_NOTE,
    )


def fno_only_spot_report(dry_run: bool = False) -> BulkSpotReport:
    """The all-zeros SPOT no-op the fno-only arm binds as ``run_spot``.

    The exact mirror of :func:`spot_only_metadata_report` (round-6 two-button
    split): every failure signal at its zero default, so the REAL
    :func:`~brutex.dashboard.pipeline_pull.spot_phase_ok` gate passes
    VACUOUSLY (``auth_aborted=False`` · ``quota_exhausted=False`` ·
    ``still_failed_windows=0`` · ``mass_empty_cause=None``) and
    ``_spot_quarantine_names`` reads the empty set (``symbols=[]`` — the
    ``PriorStepsVerdict.spot_clean`` arm is vacuously True too). The machine
    tell-apart from a real empty pull: EMPTY ``start_date``/``end_date`` +
    zero symbols (a real pull always stamps the resolved range and one row
    per target); the classifier folds the all-zeros tallies to the ``clean``
    verdict kind, whose rendered ``"spot clean (0 pair(s))"`` clause is then
    surgically replaced by :func:`build_fno_only_note` — the B H1 honesty law
    on the spot axis. *dry_run* mirrors the LAUNCH's flag (the r2 NIT-6 law);
    the report is a designed no-op either way.
    """
    return BulkSpotReport(dry_run=dry_run, start_date="", end_date="", symbols=[])


# ── note surgery (B negative pins + C wording; SURGERY, not a suffix-append) ──

#: The explicit Greeks honesty constant (C 5): the sequencer note renders
#: NOTHING for a Greeks pass that never ran (``ok=None``), so the spot-only
#: note must SAY it — a structurally-skipped stage is named, never implied.
GREEKS_NOT_RUN_NOTE: Final[str] = "Greeks: not run — no options in this pull"

#: What the no-op F&O clause becomes: an honest by-design statement.
FNO_SKIPPED_CLAUSE: Final[str] = ", F&O skipped by design (spot-only scope)"

#: The fno-only mirror (round-6 two-button split): what the no-op SPOT clause
#: becomes — the coordinator-ruled exact wording.
SPOT_SKIPPED_CLAUSE: Final[str] = "spot skipped by design (F&O-only scope)"

#: The EXACT spot clause ``pipeline_pull._complete_note`` renders for the
#: all-zeros :func:`fno_only_spot_report` (verdict kind ``clean``, zero
#: completed pairs). Surgically REPLACED — a "spot clean (0 pair(s))" wording
#: on an fno-only pull would read like a real empty spot pull (the B H1 law's
#: spot axis); the same exact-string-coupling posture as
#: :data:`_NOOP_FNO_CLAUSE` (the e2e negative pin + the surgery-miss WARN
#: guard a wording refactor).
_NOOP_SPOT_CLAUSE: Final[str] = "spot clean (0 pair(s))"

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

#: The fno-only replacement prefix (round-6 two-button split).
_FNO_ONLY_PREFIX: Final[str] = "F&O-only pipeline complete — "

# ── market-hours deferral honesty (refuter round 1, r2 MINOR-1 + r1 NIT-A) ───
#
# ``pipeline_pull._market_hours_stop_note`` renders EXACTLY ONE deferral note
# for a truthy ``market_closed`` — but the SPOT-ONLY predicate is the COMPOSED
# belt (market hours OR a Muhurat session), so during an EVENING Muhurat
# session (executed repro: frozen 2024-11-01 18:30 IST) the note's bare clock
# claim "inside NSE market-hours window (08:45-15:35 IST)" is FALSE, and
# "resume on the next out-of-hours run" is self-contradictory (18:30 IS
# out-of-hours by that window). The stop INSTANT is not available to the
# surgery (the note is a finished string), so the replacement is the honest
# BOTH-ARMS wording. Exact-string coupled to ``_market_hours_stop_note`` by
# design — a drifted wording passes through unchanged (diagnostic, never
# load-bearing; the ``_STOP_FNO_CLAUSE`` posture).

#: The exact clock-claim clause of the sequencer's market-hours deferral note.
_MARKET_HOURS_WINDOW_CLAUSE: Final[str] = "inside NSE market-hours window (08:45-15:35 IST)"

#: The both-arms replacement — true for the guard's window AND the Muhurat belt.
_MARKET_HOURS_WINDOW_SPOT_ONLY_CLAUSE: Final[str] = (
    "market-session protection window active (NSE hours 08:45-15:35 IST or a Muhurat session)"
)

#: The generic three-phase list — on spot-only the F&O phases were designed
#: no-ops that were never going to run (the ``_STOP_FNO_CLAUSE`` family).
_MARKET_HOURS_PHASES_CLAUSE: Final[str] = (
    "(spot / metadata / historical skipped to keep the shared Groww account free for the live feed)"
)

#: The spot-only replacement for :data:`_MARKET_HOURS_PHASES_CLAUSE`.
_MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE: Final[str] = (
    "(spot deferred to keep the shared Groww account free for the live feed; "
    "F&O not applicable — spot-only scope)"
)

#: The fno-only replacement for :data:`_MARKET_HOURS_PHASES_CLAUSE` (round-6
#: two-button split — the metadata/historical phases ARE real there while
#: spot is the designed no-op).
_MARKET_HOURS_PHASES_FNO_ONLY_CLAUSE: Final[str] = (
    "(metadata / historical deferred to keep the shared Groww account free for "
    "the live feed; spot not applicable — F&O-only scope)"
)

#: The self-contradictory-during-Muhurat resume advice.
_MARKET_HOURS_RESUME_CLAUSE: Final[str] = "resume on the next out-of-hours run"

#: The spot-only replacement for :data:`_MARKET_HOURS_RESUME_CLAUSE`.
_MARKET_HOURS_RESUME_SPOT_ONLY_CLAUSE: Final[str] = "resume once the protection window clears"

#: The CLI mid-run-halt mirror (``MarketHoursHalt`` caught in
#: ``cmd_pull_all_spot`` — the exception message carries no clock claim, so
#: only the RESUME advice needs the both-arms wording there).
SPOT_ONLY_SESSION_RESUME_HINT: Final[str] = (
    "resume once the market-session protection window clears "
    "(NSE hours 08:45-15:35 IST or a Muhurat session)"
)


def rewrite_market_hours_note(
    note: str, *, phases_replacement: str = _MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE
) -> str:
    """Scoped-arm surgery on the sequencer's market-hours deferral note.

    Refuter round 1 (r2 MINOR-1 + r1 NIT-A): the composed scoped-arm belt also
    defers during a Muhurat session, where the sequencer's single deferral
    note makes a FALSE clock claim and self-contradictory resume advice.
    Replaces the three exact clauses with wording true for BOTH arms and
    scope-aware for the phase list (*phases_replacement* — the spot-only
    wording by default, the CALLER's default-arm back-compat; the round-6
    fno-only builder passes its own); a drifted note (anchors absent) passes
    through UNCHANGED — the clause is diagnostic, never load-bearing, so no
    WARN (the ``_STOP_FNO_CLAUSE`` posture). Pure; idempotent on an
    already-rewritten note (the replacement strings contain no anchor).

    DEC-RUST-PULL-STATE-001 (Wave 1B): flag-ON the rewrite is served by the
    Rust kernel (byte-exact, golden-pinned); the Python arm below is the
    retained oracle serving every flag-OFF call + every kernel degrade.
    """
    if _pull_state_rust.pull_state_rust_enabled():
        rewritten = _pull_state_rust.rust_market_hours_note_rewrite(note, phases_replacement)
        if rewritten is not None:
            return rewritten
    note = note.replace(_MARKET_HOURS_WINDOW_CLAUSE, _MARKET_HOURS_WINDOW_SPOT_ONLY_CLAUSE, 1)
    note = note.replace(_MARKET_HOURS_PHASES_CLAUSE, phases_replacement, 1)
    return note.replace(_MARKET_HOURS_RESUME_CLAUSE, _MARKET_HOURS_RESUME_SPOT_ONLY_CLAUSE, 1)


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


#: The tail statuses ``--strict-exit`` escalates (the crossverify ALERT-status
#: verdict family + the fence's own ``failed`` — named by alert STATUS, never
#: by exit code: ``refused`` exits 1 on a lock refusal on the direct
#: ``brutex crossverify run`` surface, so it is not uniformly exit-2).
#: ``disabled`` / ``not_configured`` / the skip family never escalate.
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
    (hostile-review r2 #3; those phases were designed no-ops here), and the
    market-hours deferral note, whose bare clock claim is rewritten to the
    both-arms wording via :func:`rewrite_market_hours_note` (refuter round 1
    r2 MINOR-1 — during an evening Muhurat session "inside 08:45-15:35 IST"
    is FALSE). A note
    carrying exactly ONE anchor is a ``_complete_note`` wording DRIFT — log
    ONE ``pull_scope.note_surgery_miss`` WARN and still append step 3; the
    e2e negative pin (``test_e2e_no_zero_expiries_wording…``) fails loudly on
    a full drift, forcing a conscious re-pin.

    DEC-RUST-PULL-STATE-001 (Wave 1B): flag-ON, steps 1-2 (+ the market-hours
    and stop-clause rewrites) are served by the Rust kernel as
    ``(note, prefix_present, clause_present)``; the ``note_surgery_miss``
    WARN and the step-3 append stay HERE on both arms (Python composes the
    tail outcome). The Python arm below is the retained byte-identical
    oracle — every flag-OFF call and every kernel degrade.
    """
    if _pull_state_rust.pull_state_rust_enabled():
        surgery = _pull_state_rust.rust_spot_only_note_surgery(base_note)
        if surgery is not None:
            rust_note, prefix_present, clause_present = surgery
            if prefix_present != clause_present:
                _LOG.warning(
                    "pull_scope.note_surgery_miss",
                    prefix_present=prefix_present,
                    fno_clause_present=clause_present,
                    base_note=base_note[:200],
                )
            return f"{rust_note}, {GREEKS_NOT_RUN_NOTE}{crossverify_clause(tail)}"
    note = rewrite_market_hours_note(base_note)
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


def build_fno_only_note(base_note: str) -> str:
    """Deterministic SURGERY on the sequencer's note for an fno-only run.

    The exact mirror of :func:`build_spot_only_note` on the spot axis
    (round-6 two-button split), MINUS its unconditional appends — an fno-only
    run pulls options for real, so the Greeks pass genuinely runs (no
    "Greeks: not run" claim), and the crossverify tail is Spot-EXCLUSIVE by
    ruling (never fired here, exactly like the full-scope arm — no clause).

    1. Rewrite a market-hours deferral note's clauses through
       :func:`rewrite_market_hours_note` with the fno-only phases wording
       (the composed Muhurat belt is inherited by every non-full scope, so
       the evening-Muhurat FALSE-clock-claim class applies here too).
    2. Replace the exact :data:`_NOOP_SPOT_CLAUSE` (what the all-zeros
       :func:`fno_only_spot_report` renders through ``_complete_note``) with
       :data:`SPOT_SKIPPED_CLAUSE` — a by-design spot no-op must never read
       like a real empty spot pull (the B H1 law's spot axis).
    3. Replace the first :data:`_FULL_PREFIX` with :data:`_FNO_ONLY_PREFIX`
       (a ledger-failure prefix stays LEADING — the B R3/H5 posture).

    A STOPPED/deferred note (neither anchor — e.g. a metadata stop, whose
    "historical NOT started" trailer is TRUE here) gets no surgery and no
    WARN; a note carrying exactly ONE anchor is a ``_complete_note`` wording
    DRIFT — ONE ``pull_scope.note_surgery_miss`` WARN (the e2e negative pin
    fails loudly on a full drift, forcing a conscious re-pin). Pure beyond
    the WARN.

    DEC-RUST-PULL-STATE-001 (Wave 1B): flag-ON the surgery is served by the
    Rust kernel (the spot-axis mirror seam of :func:`build_spot_only_note` —
    the WARN stays HERE on both arms); the Python arm below is the retained
    byte-identical oracle.
    """
    if _pull_state_rust.pull_state_rust_enabled():
        surgery = _pull_state_rust.rust_fno_only_note_surgery(base_note)
        if surgery is not None:
            rust_note, prefix_present, clause_present = surgery
            if prefix_present != clause_present:
                _LOG.warning(
                    "pull_scope.note_surgery_miss",
                    prefix_present=prefix_present,
                    spot_clause_present=clause_present,
                    base_note=base_note[:200],
                )
            return rust_note
    note = rewrite_market_hours_note(
        base_note, phases_replacement=_MARKET_HOURS_PHASES_FNO_ONLY_CLAUSE
    )
    prefix_present = _FULL_PREFIX in note
    clause_present = _NOOP_SPOT_CLAUSE in note
    if clause_present:
        note = note.replace(_NOOP_SPOT_CLAUSE, SPOT_SKIPPED_CLAUSE, 1)
    if prefix_present:
        note = note.replace(_FULL_PREFIX, _FNO_ONLY_PREFIX, 1)
    if prefix_present != clause_present:
        _LOG.warning(
            "pull_scope.note_surgery_miss",
            prefix_present=prefix_present,
            spot_clause_present=clause_present,
            base_note=base_note[:200],
        )
    return note
