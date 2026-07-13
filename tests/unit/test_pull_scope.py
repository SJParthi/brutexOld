"""``brutex.dashboard.pull_scope`` — the scoped-ALL spot-pull primitives.

DEC-PULL-SCOPED-ALL-ONECLICK-001 (MIGRATION.md #1779): the scope-token
registry, the spot-only no-op F&O reports (driven through the REAL
``pipeline_pull`` gates — never re-implemented predicates), the note SURGERY
goldens + negative pins (B H1 — a designed no-op must never read like a real
empty vendor result), and the fail-soft crossverify-tail fence. 100%
line+branch standalone (the ci.yml module gate).
"""

from __future__ import annotations

import pytest
from brutex.dashboard.pipeline_pull import (
    historical_phase_ok,
    metadata_phase_ok,
    run_full_pipeline,
)
from brutex.dashboard.pull_controller import BulkSpotReport, SymbolPullResult
from brutex.dashboard.pull_scope import (
    FNO_SKIPPED_CLAUSE,
    GREEKS_NOT_RUN_NOTE,
    KNOWN_PULL_SCOPES,
    SCOPE_FULL,
    SCOPE_SPOT_ONLY,
    SPOT_ONLY_HISTORICAL_NOTE,
    SPOT_ONLY_METADATA_NOTE,
    TAIL_ALERT_STATUSES,
    CrossverifyTailOutcome,
    build_spot_only_note,
    crossverify_clause,
    normalize_pull_scope,
    rewrite_market_hours_note,
    run_crossverify_tail,
    spot_only_historical_report,
    spot_only_metadata_report,
)
from brutex.data.errors import LakeWriteHalt
from structlog.testing import capture_logs

# ── fixtures ─────────────────────────────────────────────────────────────


def _spot_report(
    *,
    symbols: list[SymbolPullResult] | None = None,
    ledger_load_failed: bool = False,
) -> BulkSpotReport:
    return BulkSpotReport(
        dry_run=False,
        start_date="2020-01-01",
        end_date="2026-07-12",
        symbols=(
            [SymbolPullResult(symbol="NIFTY", completed=1, failed=0, no_data=0)]
            if symbols is None
            else symbols
        ),
        ledger_load_failed=ledger_load_failed,
    )


def _sequencer_note(spot: BulkSpotReport) -> str:
    """The REAL sequencer's composed note over the spot-only no-op reports.

    Drives the actual ``run_full_pipeline`` → ``_complete_note`` chain (the
    public entry — never a re-derived wording), exactly as the spot-only
    launcher arm binds it.
    """
    report = run_full_pipeline(
        run_spot=lambda: spot,
        run_metadata=spot_only_metadata_report,
        run_historical=lambda _prior: spot_only_historical_report(),
    )
    assert report.stopped_after is None
    return report.note


_TAIL_OK = CrossverifyTailOutcome(
    status="started", human="producer started (poll /api/v1/crossverify/progress)"
)


# ── scope registry ───────────────────────────────────────────────────────


@pytest.mark.parametrize("token", [None, "", "   ", "\t"])
def test_normalize_pull_scope_default_blank_none(token: str | None) -> None:
    assert normalize_pull_scope(token) == SCOPE_FULL


def test_normalize_pull_scope_known_verbatim() -> None:
    assert normalize_pull_scope(SCOPE_FULL) == SCOPE_FULL
    assert normalize_pull_scope(SCOPE_SPOT_ONLY) == SCOPE_SPOT_ONLY
    assert normalize_pull_scope("  spot-only  ") == SCOPE_SPOT_ONLY
    assert frozenset({SCOPE_FULL, SCOPE_SPOT_ONLY}) == KNOWN_PULL_SCOPES


@pytest.mark.parametrize("token", ["spotonly", "SPOT-ONLY", "spot+options", "all"])
def test_normalize_pull_scope_unknown_raises(token: str) -> None:
    """A typo'd scope must never widen or narrow a vendor pull (B H9)."""
    with pytest.raises(ValueError, match="unknown pull scope"):
        normalize_pull_scope(token)


# ── the no-op reports pass the REAL gates ────────────────────────────────


def test_spot_only_metadata_report_passes_real_gate() -> None:
    assert metadata_phase_ok(spot_only_metadata_report()) is True


def test_spot_only_historical_report_passes_real_gate() -> None:
    """``historical=None`` is the documented no-op arm — the gate returns True."""
    assert historical_phase_ok(spot_only_historical_report()) is True


def test_noop_reports_carry_scope_note_and_empty_tuples() -> None:
    """B H1 machine tell-aparts: by-design zeros, never a fake vendor result."""
    meta = spot_only_metadata_report()
    assert meta.note == SPOT_ONLY_METADATA_NOTE
    assert meta.years == ()
    assert meta.underlyings == ()
    assert meta.dry_run is False
    assert meta.expiries_persisted == 0
    assert meta.contracts_persisted == 0
    hist = spot_only_historical_report()
    assert hist.note == SPOT_ONLY_HISTORICAL_NOTE
    assert hist.requested == ()
    assert hist.tradable == ()
    assert hist.metadata is None
    assert hist.historical is None


def test_noop_reports_echo_dry_run_flag() -> None:
    """[refuter-r1 r2 NIT-6] the no-op builders thread the LAUNCH's dry_run —
    a dry-run payload's nested sub-reports never read ``dry_run: false``;
    the zero-arg default stays False (back-compat)."""
    assert spot_only_metadata_report(dry_run=True).dry_run is True
    assert spot_only_historical_report(dry_run=True).dry_run is True
    assert spot_only_metadata_report(dry_run=False).dry_run is False
    assert spot_only_historical_report(dry_run=False).dry_run is False
    # The dry_run flag changes NOTHING else (the designed-no-op invariant),
    # and the gates still pass on the dry-run shape.
    assert metadata_phase_ok(spot_only_metadata_report(dry_run=True)) is True
    assert historical_phase_ok(spot_only_historical_report(dry_run=True)) is True
    wet, dry = spot_only_metadata_report(), spot_only_metadata_report(dry_run=True)
    assert wet.note == dry.note
    assert wet.years == dry.years


# ── note surgery goldens + negative pins ─────────────────────────────────


def test_note_surgery_replaces_fno_clause_exact() -> None:
    """[gold] The exact no-op F&O wording becomes the by-design clause."""
    note = build_spot_only_note(_sequencer_note(_spot_report()), _TAIL_OK)
    assert note == (
        "spot-only pipeline complete — spot clean (1 pair(s))"
        ", F&O skipped by design (spot-only scope)"
        ", Greeks: not run — no options in this pull"
        ", cross-verify: producer started (poll /api/v1/crossverify/progress)"
    )
    assert FNO_SKIPPED_CLAUSE in note


def test_note_surgery_prefix_becomes_spot_only_ledger_fail_stays_leading() -> None:
    """[gold] The ledger-failure prefix survives LEADING through the surgery."""
    note = build_spot_only_note(_sequencer_note(_spot_report(ledger_load_failed=True)), _TAIL_OK)
    assert note.startswith(
        "window ledger reload FAILED — running full re-verify pull (no skip); "
        "spot-only pipeline complete — "
    )
    assert "pipeline complete — " in note  # the rewritten prefix, once
    assert note.count("spot-only pipeline complete") == 1


def test_note_no_zero_expiries_substring() -> None:
    """[neg — B H1] The '0 expiries' no-op wording must NOT survive surgery."""
    note = build_spot_only_note(_sequencer_note(_spot_report()), _TAIL_OK)
    assert "0 expiries" not in note
    assert "0 contracts" not in note
    assert "0 bars over 0 contract(s)" not in note


def test_note_keeps_quarantine_clause_leading() -> None:
    """B R3/H5: the quarantine headline still LEADS the rewritten note."""
    spot = _spot_report(
        symbols=[
            SymbolPullResult(symbol="NIFTY", completed=1, failed=0, no_data=0),
            SymbolPullResult(symbol="BADSYM", completed=0, failed=1, no_data=0),
        ]
    )
    note = build_spot_only_note(_sequencer_note(spot), _TAIL_OK)
    assert note.startswith("spot-only pipeline complete — 1 symbol(s) quarantined: BADSYM — ")
    assert FNO_SKIPPED_CLAUSE in note


def test_note_surgery_miss_warns_and_still_appends() -> None:
    """A HALF-drifted ``_complete_note`` wording warns once + still appends."""
    # prefix present, F&O clause drifted away → WARN + step-3 clauses appended.
    drifted = "pipeline complete — spot clean (1 pair(s)), metadata went sideways"
    with capture_logs() as logs:
        note = build_spot_only_note(drifted, _TAIL_OK)
    assert note.startswith("spot-only pipeline complete — ")
    assert GREEKS_NOT_RUN_NOTE in note
    assert crossverify_clause(_TAIL_OK) in note
    miss = [e for e in logs if e["event"] == "pull_scope.note_surgery_miss"]
    assert len(miss) == 1
    assert miss[0]["prefix_present"] is True
    assert miss[0]["fno_clause_present"] is False
    # The mirror drift (clause present, prefix drifted) also warns.
    drifted2 = (
        "pipeline done, metadata 0 expiries + 0 contracts, historical 0 bars over 0 contract(s)"
    )
    with capture_logs() as logs2:
        note2 = build_spot_only_note(drifted2, _TAIL_OK)
    assert FNO_SKIPPED_CLAUSE in note2
    assert "0 expiries" not in note2
    assert [e["event"] for e in logs2] == ["pull_scope.note_surgery_miss"]


def test_stopped_note_appends_without_surgery_or_warn() -> None:
    """A stopped/deferred note (no completed-note anchors) gets clauses only —
    no prefix/F&O surgery, no WARN — while the spot-stop note's generic
    "metadata + historical NOT started" trailer reads spot-only-aware
    (r2 #3: those phases were designed no-ops here), driven through the REAL
    sequencer stop note (never a re-derived wording)."""
    failing_spot = BulkSpotReport(
        dry_run=False,
        start_date="2020-01-01",
        end_date="2026-07-12",
        symbols=[SymbolPullResult(symbol="NIFTY", completed=0, failed=0, no_data=0)],
        still_failed_windows=3,
    )
    report = run_full_pipeline(
        run_spot=lambda: failing_spot,
        run_metadata=spot_only_metadata_report,
        run_historical=lambda _prior: spot_only_historical_report(),
    )
    assert report.stopped_after == "spot"
    assert "metadata + historical NOT started" in report.note  # the REAL trailer
    tail = CrossverifyTailOutcome(
        status="skipped_pipeline_stopped", human="skipped (pipeline stopped: spot)"
    )
    with capture_logs() as logs:
        note = build_spot_only_note(report.note, tail)
    assert note.startswith("stopped after spot — ")
    assert "F&O not applicable (spot-only scope)" in note
    assert "metadata + historical NOT started" not in note
    assert note.endswith(f", {GREEKS_NOT_RUN_NOTE}, cross-verify: skipped (pipeline stopped: spot)")
    assert not [e for e in logs if e["event"] == "pull_scope.note_surgery_miss"]
    # A stopped note WITHOUT the trailer (e.g. a market-hours deferral shape)
    # passes through untouched — clauses only.
    plain = "stopped after spot — auth_expired (3 window(s) still failing); ..."
    with capture_logs() as logs2:
        note2 = build_spot_only_note(plain, tail)
    assert note2 == (
        f"{plain}, {GREEKS_NOT_RUN_NOTE}, cross-verify: skipped (pipeline stopped: spot)"
    )
    assert not [e for e in logs2 if e["event"] == "pull_scope.note_surgery_miss"]


def test_rewrite_market_hours_note_real_wording_both_arms() -> None:
    """[refuter-r1 r2 MINOR-1] the REAL ``_market_hours_stop_note`` wording
    (imported, never re-derived) is rewritten to the honest BOTH-ARMS text:
    the bare "inside 08:45-15:35" clock claim (FALSE during an evening
    Muhurat deferral) is gone, the generic three-phase list reads spot-only
    (r1 NIT-A), and the self-contradictory resume advice is replaced."""
    from brutex.dashboard.pipeline_pull import _market_hours_stop_note

    rewritten = rewrite_market_hours_note(_market_hours_stop_note())
    assert (
        "market-session protection window active "
        "(NSE hours 08:45-15:35 IST or a Muhurat session)" in rewritten
    )
    assert "inside NSE market-hours window (08:45-15:35 IST)" not in rewritten
    assert "F&O not applicable — spot-only scope" in rewritten
    assert "spot / metadata / historical skipped" not in rewritten
    assert "resume once the protection window clears" in rewritten
    assert "resume on the next out-of-hours run" not in rewritten
    assert rewritten.startswith("deferred — ")
    # Idempotent: a second pass changes nothing (no anchor in the replacements).
    assert rewrite_market_hours_note(rewritten) == rewritten


def test_rewrite_market_hours_note_drifted_passthrough() -> None:
    """A drifted sequencer wording passes through UNCHANGED — the clause is
    diagnostic, never load-bearing (the ``_STOP_FNO_CLAUSE`` posture)."""
    drifted = "deferred — the market gate wording moved; resume later"
    assert rewrite_market_hours_note(drifted) == drifted


def test_build_spot_only_note_applies_market_hours_rewrite() -> None:
    """[refuter-r1 r2 MINOR-1 — the dashboard finish-detail arm] the surgery
    law folds the market-hours rewrite in, with the honest clauses appended
    and NO surgery-miss WARN (the deferral note carries no F&O anchors)."""
    from brutex.dashboard.pipeline_pull import _market_hours_stop_note

    tail = CrossverifyTailOutcome(
        status="skipped_pipeline_stopped", human="skipped (pipeline stopped: market_hours)"
    )
    with capture_logs() as logs:
        note = build_spot_only_note(_market_hours_stop_note(), tail)
    assert "market-session protection window active" in note
    assert "inside NSE market-hours window (08:45-15:35 IST)" not in note
    assert note.endswith(
        f", {GREEKS_NOT_RUN_NOTE}, cross-verify: skipped (pipeline stopped: market_hours)"
    )
    assert not [e for e in logs if e["event"] == "pull_scope.note_surgery_miss"]


def test_greeks_clause_on_every_variant() -> None:
    """The explicit Greeks constant rides EVERY variant; the enrichment-failure
    family wording never appears (C 5 — this pull structurally has no pass)."""
    variants = [
        build_spot_only_note(_sequencer_note(_spot_report()), _TAIL_OK),
        build_spot_only_note("stopped after spot — x", _TAIL_OK),
        build_spot_only_note(
            _sequencer_note(_spot_report(ledger_load_failed=True)),
            CrossverifyTailOutcome(status="disabled", human="disabled (BRUTEX_CROSSVERIFY=off)"),
        ),
    ]
    for note in variants:
        assert GREEKS_NOT_RUN_NOTE in note
        assert "Greeks enrichment" not in note  # [neg] the failure-family wording


def test_crossverify_clause_all_status_vocab() -> None:
    """[gold] The ONE clause renderer over the pinned status vocabulary."""
    cases = {
        "started": "producer started (poll /api/v1/crossverify/progress)",
        "already_running": "already running",
        "disabled": "disabled (BRUTEX_CROSSVERIFY=off)",
        "not_configured": "not configured (ValueError: junk)",
        "skipped_dry_run": "skipped (dry-run)",
        "skipped_pipeline_stopped": "skipped (pipeline stopped: spot)",
        "failed": "FAILED (RuntimeError: boom) — pull unaffected",
        "published": "published (published 4 · withheld 0 · failed 0)",
        "staged_only": "staged_only (published 0 · withheld 0 · failed 0)",
        "nothing_to_do": "nothing_to_do (published 0 · withheld 0 · failed 0)",
        "skipped_non_trading": "skipped_non_trading (published 0 · withheld 0 · failed 0)",
        "skipped_muhurat": "skipped_muhurat (published 0 · withheld 0 · failed 0)",
        "deferred_market_hours": "deferred_market_hours (published 0 · withheld 0 · failed 0)",
        "refused": "refused (published 0 · withheld 0 · failed 0)",
        "withheld_partial": "withheld_partial (published 3 · withheld 1 · failed 0)",
        "blocked": "blocked (published 0 · withheld 0 · failed 0)",
        "publish_failed": "publish_failed (published 1 · withheld 0 · failed 2)",
        "suspect": "suspect (published 0 · withheld 0 · failed 0)",
    }
    for status, human in cases.items():
        clause = crossverify_clause(CrossverifyTailOutcome(status=status, human=human))
        assert clause == f", cross-verify: {human}"
    # The strict-exit escalation family — named by alert STATUS, not by exit code.
    assert (
        frozenset({"failed", "refused", "withheld_partial", "blocked", "publish_failed", "suspect"})
        == TAIL_ALERT_STATUSES
    )


# ── the fail-soft tail fence ─────────────────────────────────────────────


def test_tail_none_not_configured() -> None:
    out = run_crossverify_tail(None, dry_run=False, stopped_after=None)
    assert out == CrossverifyTailOutcome(
        status="not_configured", human="not configured (no tail bound)"
    )


def test_tail_dry_run_never_invoked() -> None:
    calls: list[str] = []

    def _tail() -> CrossverifyTailOutcome:
        calls.append("invoked")
        return _TAIL_OK

    out = run_crossverify_tail(_tail, dry_run=True, stopped_after=None)
    assert out == CrossverifyTailOutcome(status="skipped_dry_run", human="skipped (dry-run)")
    assert calls == []  # [spy] the tail is NEVER invoked on a dry-run


def test_tail_stopped_pipeline_skipped() -> None:
    calls: list[str] = []

    def _tail() -> CrossverifyTailOutcome:
        calls.append("invoked")
        return _TAIL_OK

    out = run_crossverify_tail(_tail, dry_run=False, stopped_after="spot")
    assert out == CrossverifyTailOutcome(
        status="skipped_pipeline_stopped", human="skipped (pipeline stopped: spot)"
    )
    assert calls == []


def test_tail_raising_fenced_failed_pull_unaffected() -> None:
    def _tail() -> CrossverifyTailOutcome:
        raise RuntimeError("boom")

    with capture_logs() as logs:
        out = run_crossverify_tail(_tail, dry_run=False, stopped_after=None)
    assert out.status == "failed"
    assert out.human == "FAILED (RuntimeError: boom) — pull unaffected"
    warns = [e for e in logs if e["event"] == "pull_scope.crossverify_tail_failed"]
    assert len(warns) == 1
    assert warns[0]["error_type"] == "RuntimeError"


def test_tail_lakewritehalt_reraises() -> None:
    """B M6: a disk-full/global-stop stays LOUD — never folded into fail-soft."""

    def _tail() -> CrossverifyTailOutcome:
        raise LakeWriteHalt("disk_full")

    with pytest.raises(LakeWriteHalt):
        run_crossverify_tail(_tail, dry_run=False, stopped_after=None)


def test_tail_happy_path_passthrough() -> None:
    out = run_crossverify_tail(lambda: _TAIL_OK, dry_run=False, stopped_after=None)
    assert out is _TAIL_OK


def test_note_truncation_quarantine_and_clauses_inside_500(  # C 7 worst-case
) -> None:
    """The 500-char finish-detail cap: on a realistic worst-case note the leading
    quarantine headline AND the appended Greeks + cross-verify clauses all sit
    inside the first 500 chars (the job's ``_FINISH_DETAIL_MAX`` truncation)."""
    symbols = [
        SymbolPullResult(symbol="NIFTY", completed=1, failed=0, no_data=0),
    ] + [
        SymbolPullResult(symbol=f"STOCK{i:02d}", completed=0, failed=1, no_data=0)
        for i in range(12)  # 12 quarantined → 10 named + "(+2 more)"
    ]
    tail = CrossverifyTailOutcome(
        status="failed",
        human="FAILED (RuntimeError: transient publish socket reset) — pull unaffected",
    )
    note = build_spot_only_note(_sequencer_note(_spot_report(symbols=symbols)), tail)
    capped = note[:500]
    assert "12 symbol(s) quarantined: STOCK00" in capped
    assert "(+2 more)" in capped
    assert GREEKS_NOT_RUN_NOTE in capped
    assert crossverify_clause(tail) in capped
    assert "0 expiries" not in note


def test_tail_outcome_is_frozen() -> None:
    with pytest.raises((AttributeError, TypeError)):
        _TAIL_OK.status = "x"  # type: ignore[misc]
