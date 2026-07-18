"""Tests for `brutex.engine.sweep_preflight` (DEC-SWEEP-PREFLIGHT-001).

The loud no-bars refusal core: every requested (symbol, interval) pair with
zero bars is NAMED in deterministic order, with the per-class remediation
(derived TFs → materialize / opt-in; fetched intervals → pull).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.data.session_density import SESSION_DENSITY_ERROR_EVENT
from brutex.engine import sweep_preflight as sweep_preflight_mod
from brutex.engine.sweep_preflight import (
    PreflightReport,
    SessionDensityNote,
    preflight_session_density,
    preflight_slices,
)
from structlog.testing import capture_logs


class TestPreflightSlices:
    def test_ok_when_every_pair_has_bars(self) -> None:
        report = preflight_slices(
            {("NIFTY", "1minute"): 100, ("NIFTY", "5minute"): 20},
            symbols=["NIFTY"],
            intervals=["1minute", "5minute"],
        )
        assert report.ok
        assert report.missing == ()
        assert report.requested == 2

    def test_missing_pairs_named_in_request_order(self) -> None:
        report = preflight_slices(
            {
                ("NIFTY", "1minute"): 100,
                ("NIFTY", "5minute"): 0,  # explicit zero
                ("SENSEX", "1minute"): 50,
                # ("SENSEX", "5minute") absent entirely — counts as zero
            },
            symbols=["NIFTY", "SENSEX"],
            intervals=["1minute", "5minute"],
        )
        assert not report.ok
        assert report.missing == (("NIFTY", "5minute"), ("SENSEX", "5minute"))
        assert report.requested == 4

    def test_empty_request_is_trivially_ok(self) -> None:
        report = preflight_slices({}, symbols=[], intervals=[])
        assert report.ok
        assert report.requested == 0


class TestRemediation:
    def test_derived_only_points_at_pipeline_prebuild(self) -> None:
        # DEC-PIPELINE-AUTOPREBUILD-001: the manual derive-timeframes route + the
        # BRUTEX_AUTO_DERIVE_TFS opt-in are removed — the remedy is the pipeline's
        # automatic prebuild stage.
        report = PreflightReport(missing=(("NIFTY", "5minute"), ("NIFTY", "2hour")), requested=4)
        remedy = report.remediation()
        assert "full pipeline" in remedy
        assert "prebuild" in remedy
        assert "2hour, 5minute" in remedy
        assert "derive-timeframes" not in remedy  # the deleted route is never suggested
        assert "BRUTEX_AUTO_DERIVE_TFS" not in remedy  # the removed opt-in is never suggested
        assert "pull the" not in remedy  # derived-only ⇒ no fetch clause

    def test_fetched_only_points_at_pull(self) -> None:
        # 1minute is the ONLY fetched interval (DEC-1MINUTE-ONLY-DERIVE-1DAY-001
        # #853; the stale {"1minute", "1day"} taxonomy corrected by
        # DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001).
        report = PreflightReport(missing=(("NIFTY", "1minute"),), requested=4)
        remedy = report.remediation()
        assert "pull the" in remedy
        assert "1minute" in remedy
        assert "prebuild" not in remedy

    def test_1day_classifies_derived_not_fetched(self) -> None:
        # 1day is DERIVED from the stored 1-minute — a missing 1day slice
        # remediates via the pipeline prebuild, never a re-pull (#853/#1145).
        report = PreflightReport(missing=(("SENSEX", "1day"),), requested=4)
        remedy = report.remediation()
        assert "prebuild" in remedy
        assert "1day" in remedy
        assert "pull the" not in remedy

    def test_spot_fetched_wording_byte_pinned(self) -> None:
        # The spot remedy wording is BYTE-pinned unchanged by the F&O-aware
        # branch (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001 — spot wording preserved).
        report = PreflightReport(missing=(("NIFTY", "1minute"),), requested=1)
        assert report.remediation() == (
            "fetched intervals with no bars (1minute): pull the "
            "symbol's spot history first (dashboard Pull / `brutex fetch`)"
        )

    def test_futures_alias_fetched_points_at_fno_pull(self) -> None:
        # The NIFTY-futures series alias (ADR-0012 /
        # DEC-FUTURES-ADDRESSING-PIN-001) pulls through the F&O phase — the
        # spot-history remedy would be WRONG (Golden Rule #1).
        report = PreflightReport(missing=(("NSE-NIFTY-FUT", "1minute"),), requested=1)
        remedy = report.remediation()
        assert "F&O history" in remedy
        assert "F&O phase" in remedy
        assert "spot history" not in remedy

    def test_fno_contract_fetched_points_at_fno_pull(self) -> None:
        # A parseable per-contract groww_symbol likewise names the F&O pull.
        report = PreflightReport(missing=(("NSE-NIFTY-26Jun25-FUT", "1minute"),), requested=1)
        remedy = report.remediation()
        assert "F&O history" in remedy
        assert "spot history" not in remedy

    def test_mixed_spot_and_fno_fetched_names_both_pulls(self) -> None:
        report = PreflightReport(
            missing=(("NIFTY", "1minute"), ("NSE-NIFTY-FUT", "1minute")), requested=2
        )
        remedy = report.remediation()
        assert "F&O history" in remedy
        assert "spot history" in remedy

    def test_mixed_names_both_remedies(self) -> None:
        report = PreflightReport(missing=(("NIFTY", "1minute"), ("NIFTY", "5minute")), requested=2)
        remedy = report.remediation()
        assert "prebuild" in remedy
        assert "pull the" in remedy

    def test_ok_report_remediation_is_empty(self) -> None:
        assert PreflightReport(missing=(), requested=2).remediation() == ""


class TestFreshnessPreflight:
    """DEC-SUSPICION-SWEEP-FIXES-001 (G1) — the derived-TF freshness gate core."""

    def test_all_fresh_is_ok(self) -> None:
        from brutex.engine.sweep_preflight import preflight_derived_freshness

        report = preflight_derived_freshness(
            {("NIFTY", "5minute"): "fresh", ("SENSEX", "15minute"): "fresh"}
        )
        assert report.ok is True
        assert report.stale == ()
        assert report.checked == 2

    def test_stale_pairs_named_sorted_deterministic(self) -> None:
        from brutex.engine.sweep_preflight import preflight_derived_freshness

        report = preflight_derived_freshness(
            {
                ("SENSEX", "5minute"): "stale",
                ("NIFTY", "15minute"): "stale",
                ("NIFTY", "5minute"): "fresh",
            }
        )
        assert report.ok is False
        assert report.stale == (("NIFTY", "15minute"), ("SENSEX", "5minute"))
        assert report.checked == 3
        assert "prebuild" in report.remediation()
        assert "DEC-PIPELINE-AUTOPREBUILD-001" in report.remediation()

    def test_remediation_names_the_ghost_bucket_escape(self) -> None:
        """DEC-DERIVED-FRESHNESS-SCREEN-PARITY-001: the gate has NO escape flag by
        design, so the printed remedy must name the ONE stale shape a prebuild
        re-run can NEVER clear — a stored derived bucket whose sole 1-minute
        source is screened away (the latest-wins derived writer never deletes;
        the prune is the named follow-up DEC-DERIVED-GHOST-BUCKET-PRUNE-001) —
        with its working manual escape, or the refusal is a permanent wedge."""
        from brutex.engine.sweep_preflight import preflight_derived_freshness

        remedy = preflight_derived_freshness({("NIFTY", "5minute"): "stale"}).remediation()
        assert "ghost bucket" in remedy
        assert "DEC-DERIVED-GHOST-BUCKET-PRUNE-001" in remedy
        assert "delete the symbol's derived TF partition dirs" in remedy
        assert "re-run the prebuild" in remedy

    def test_unexpected_verdict_fails_closed(self) -> None:
        """Anything not EXACTLY 'fresh' counts stale — an unknown verdict string
        can never silently pass the gate (fail-closed)."""
        from brutex.engine.sweep_preflight import preflight_derived_freshness

        report = preflight_derived_freshness({("NIFTY", "5minute"): "weird"})
        assert report.ok is False
        assert report.stale == (("NIFTY", "5minute"),)

    def test_empty_verdicts_ok(self) -> None:
        """No verifiable pairs (all abstained) → the gate never blocks a launch."""
        from brutex.engine.sweep_preflight import preflight_derived_freshness

        report = preflight_derived_freshness({})
        assert report.ok is True
        assert report.checked == 0


class TestOneDayTradeLawNote:
    """`one_day_trade_law_note` — the honest 1day-grid launch label.

    Review round-8 finding 1 (Golden Rule #10): under the intraday-only trade
    law the 1day grid is structurally trade-less (midnight-IST stamps → the
    next-bar fill lands on a later IST session date and the overnight-gap
    rule drops it; no 1day bar reaches the 15:20 forced exit), so every mask
    ranks nothing and MIN_TRADES gates all combos out. The launch is NOT
    refused (bar_bits consumers are real — the D12 mask); it is NAMED.
    """

    def test_one_day_interval_yields_the_note(self) -> None:
        from brutex.engine.sweep_preflight import one_day_trade_law_note

        note = one_day_trade_law_note(["1minute", "1day"])
        assert note is not None
        assert "intraday-only trade law" in note
        assert "overnight-gap rule" in note
        assert "MIN_TRADES" in note
        assert "DEC-FUTURES-LAUNCH-REVIEW-FIXES-008" in note  # the named follow-up
        assert "\n" not in note  # one line — never a forged log shape

    def test_intraday_only_launch_gets_no_note(self) -> None:
        from brutex.engine.sweep_preflight import one_day_trade_law_note

        assert one_day_trade_law_note(["1minute", "5minute", "15minute"]) is None
        assert one_day_trade_law_note([]) is None


class TestSessionDensityNote:
    """DEC-SESSION-DENSITY-CENSUS-001 — the WARN-only preflight density note."""

    @staticmethod
    def _minute_bars(d: date, *, count: int = 375) -> list[CandleBar]:
        bars: list[CandleBar] = []
        for i in range(count):
            hh, mm = divmod(9 * 60 + 15 + i, 60)
            ts = datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).astimezone(UTC)
            bars.append(
                CandleBar(
                    timestamp=ts,
                    open=Decimal("100"),
                    high=Decimal("100"),
                    low=Decimal("100"),
                    close=Decimal("100"),
                    volume=0,
                    open_interest=None,
                )
            )
        return bars

    @classmethod
    def _holey_slice(cls) -> list[CandleBar]:
        # Tue + Thu full; Wed 2024-01-03 missing entirely (an interior hole).
        return cls._minute_bars(date(2024, 1, 2)) + cls._minute_bars(date(2024, 1, 4))

    def test_clean_slices_read_ok(self) -> None:
        slices = {
            ("NIFTY", "1minute"): (
                self._minute_bars(date(2024, 1, 2))
                + self._minute_bars(date(2024, 1, 3))
                + self._minute_bars(date(2024, 1, 4))
            )
        }
        note = preflight_session_density(slices, today_ist=date(2026, 12, 31))
        assert note.ok
        assert note.flagged == ()
        assert note.slices_checked == 1
        assert note.lines() == ()

    def test_holey_slice_is_named_with_shape_and_gap(self) -> None:
        note = preflight_session_density(
            {("NIFTY", "1minute"): self._holey_slice()}, today_ist=date(2026, 12, 31)
        )
        assert not note.ok
        assert len(note.flagged) == 1
        symbol, interval, flag = note.flagged[0]
        assert (symbol, interval) == ("NIFTY", "1minute")
        assert flag.session == date(2024, 1, 3)
        assert flag.shape == "missing_session"
        (line,) = note.lines()
        assert "NIFTY/1minute 2024-01-03: 0/375 bars" in line
        assert "missing_session" in line

    def test_delegates_to_the_one_common_classifier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # §11 COMMON: the note DELEGATES — no second copy of the deficiency law.
        calls = {"n": 0}
        real = sweep_preflight_mod.classify_session_density

        def _counting(*args: object, **kwargs: object) -> object:
            calls["n"] += 1
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(sweep_preflight_mod, "classify_session_density", _counting)
        preflight_session_density(
            {("NIFTY", "1minute"): self._holey_slice()}, today_ist=date(2026, 12, 31)
        )
        assert calls["n"] == 1

    def test_derived_tf_slices_are_skipped(self) -> None:
        # A derived TF's density follows its 1-minute base — judging it would
        # double-report every hole.
        note = preflight_session_density(
            {("NIFTY", "5minute"): self._holey_slice()}, today_ist=date(2026, 12, 31)
        )
        assert note.ok
        assert note.slices_checked == 0

    def test_empty_slices_are_skipped(self) -> None:
        note = preflight_session_density({("NIFTY", "1minute"): []}, today_ist=date(2026, 12, 31))
        assert note.ok
        assert note.slices_checked == 0

    def test_lines_cap_with_honest_overflow_tail(self) -> None:
        note = preflight_session_density(
            {("NIFTY", "1minute"): self._holey_slice()}, today_ist=date(2026, 12, 31)
        )
        capped = note.lines(cap=0)
        assert capped == ("... +1 more low-density session(s)",)

    def _two_flag_note(self) -> SessionDensityNote:
        # Two symbols, each with the same interior hole -> exactly 2 flags
        # in symbol-major order (AAA before BBB). Review round-6 (the #1594
        # R4-2 anti-order precedent): BBB is deliberately inserted FIRST so
        # the symbol-major contract is proven by `sorted(slices)`, never by
        # dict insertion-order luck — the sorted-removal mutant renders the
        # BBB line first and fails the AAA-first pin below.
        return preflight_session_density(
            {
                ("BBB", "1minute"): self._holey_slice(),
                ("AAA", "1minute"): self._holey_slice(),
            },
            today_ist=date(2026, 12, 31),
        )

    def test_lines_cap_one_of_two_shows_first_flag_plus_honest_tail(self) -> None:
        # Review round-3 mutant killer (`[:cap]` -> `[:cap-1]`): at cap=1 with
        # 2 flags the output must be the FIRST flag line + a "+1 more" tail —
        # the mutant renders ZERO flag lines while the tail still claims only
        # 1 was suppressed (a lying count line at any real cap).
        note = self._two_flag_note()
        lines = note.lines(cap=1)
        assert len(lines) == 2
        assert lines[0].startswith("AAA/1minute 2024-01-03: 0/375 bars")
        assert lines[1] == "... +1 more low-density session(s)"

    def test_lines_at_exactly_cap_have_no_tail(self) -> None:
        # Review round-3 mutant killer (`>` -> `>=` at the tail gate): at
        # cap == len(flagged) every flag line renders and NO overflow tail is
        # appended (a "+0 more" tail would lie — nothing was suppressed).
        note = self._two_flag_note()
        lines = note.lines(cap=2)
        assert len(lines) == 2
        assert all("more low-density" not in line for line in lines)

    def test_observed_overlay_day_is_not_flagged(self) -> None:
        # Review round-3 (fails before): the preflight arm now threads the
        # observed-holiday overlay exactly like BOTH ingest wirings — an
        # overlaid interior date abstains non_trading instead of
        # false-flagging missing_session at every sweep launch.
        slices = {("NIFTY", "1minute"): self._holey_slice()}
        unoverlaid = preflight_session_density(slices, today_ist=date(2026, 12, 31))
        assert not unoverlaid.ok  # the un-overlaid twin still flags
        overlaid = preflight_session_density(
            slices,
            today_ist=date(2026, 12, 31),
            observed=frozenset({date(2024, 1, 3)}),
        )
        assert overlaid.ok
        assert overlaid.flagged == ()
        assert overlaid.slices_checked == 1

    def test_belt_swallows_a_raising_slice_and_continues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Review round-3 belt: a slice whose classify raises logs ONE error
        # event and is SKIPPED — the remaining slices are still censused and
        # the WARN-only note can never crash the launch it decorates (the
        # ingest twin's swallow-and-continue discipline).
        real = sweep_preflight_mod.classify_session_density

        def _selective(stamps: list[datetime], **kwargs: object) -> object:
            if len(stamps) == 1:
                raise RuntimeError("injected classify crash")
            return real(stamps, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(sweep_preflight_mod, "classify_session_density", _selective)
        slices = {
            # AAA sorts first -> the RAISING slice; continuation is proven by
            # the later healthy slice still being censused + flagged. Review
            # round-6 anti-order (the #1594 R4-2 precedent): NIFTY is
            # deliberately inserted BEFORE AAA so the sorted-order claim never
            # rides dict insertion-order luck.
            ("NIFTY", "1minute"): self._holey_slice(),
            ("AAA", "1minute"): self._minute_bars(date(2024, 1, 2), count=1),
        }
        with capture_logs() as logs:
            note = preflight_session_density(slices, today_ist=date(2026, 12, 31))
        assert note.slices_checked == 1  # the healthy slice still censused
        assert len(note.flagged) == 1  # NIFTY's hole still named
        assert note.flagged[0][0] == "NIFTY"
        errors = [e for e in logs if e["event"] == SESSION_DENSITY_ERROR_EVENT]
        assert len(errors) == 1
        assert "injected classify crash" in errors[0]["error"]

    def test_note_is_pure_data_never_a_refusal(self) -> None:
        # The note carries NO refusal surface — no remediation() gate method,
        # no exit code, nothing but printable facts (the one_day_trade_law_note
        # posture: the CLI prints and PROCEEDS; there is no escape flag to add
        # because nothing is ever refused).
        note = preflight_session_density(
            {("NIFTY", "1minute"): self._holey_slice()}, today_ist=date(2026, 12, 31)
        )
        assert isinstance(note.flagged, tuple)
        assert not hasattr(note, "remediation")
