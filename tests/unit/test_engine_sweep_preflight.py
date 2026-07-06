"""Tests for `brutex.engine.sweep_preflight` (DEC-SWEEP-PREFLIGHT-001).

The loud no-bars refusal core: every requested (symbol, interval) pair with
zero bars is NAMED in deterministic order, with the per-class remediation
(derived TFs → materialize / opt-in; fetched intervals → pull).
"""

from __future__ import annotations

from brutex.engine.sweep_preflight import PreflightReport, preflight_slices


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
        report = PreflightReport(missing=(("NIFTY", "1minute"), ("SENSEX", "1day")), requested=4)
        remedy = report.remediation()
        assert "pull the" in remedy
        assert "1day, 1minute" in remedy
        assert "prebuild" not in remedy

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
