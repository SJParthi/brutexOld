"""Unit test for the F&O spine's SINGLE-ADMISSION drop of the window pace.

DEC-RATE-SINGLE-ADMISSION-001 (forensic-audit Wave 1). Stem-matched to
``brutex/adapters/groww/option_ingest.py`` so the strict per-module coverage
discovery gate scopes this test to the branch it covers — a SELF-PACING live
client makes ``ingest_option_contract`` drop the per-window admission (exactly
one admission per physical fetch; pre-fix each fetch was admitted TWICE).
Moved out of the combined ``test_groww_single_admission.py``; the fakes are
imported from it (COMMON, no copy).
"""

# Shared single-admission fakes imported cross-test-module — sanctioned.
# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import date

from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.option_ingest import ingest_option_contract

from tests.unit.test_groww_single_admission import _Admissions, _ce, _FakeSDK, _Sink


class TestFnoSelfPacedSingleAdmission:
    def test_each_physical_fetch_admitted_exactly_once(self) -> None:
        """Production wiring: pace baked into the client AND passed to the spine (pre-fix 2×)."""
        admissions = _Admissions()
        sdk = _FakeSDK()
        # Mirrors historical_pull: build_live_client bakes controller.pace into the client
        # AND run_groww_option_ingest receives the SAME controller.pace per window.
        client = GrowwClient(sdk=sdk, sleep=lambda _s: None, pace=admissions.pace)
        report = ingest_option_contract(
            client,
            _Sink(),
            contract=_ce(),
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            pace=admissions.pace,
        )
        assert report.bars_appended == 1
        assert sdk.calls == 1
        assert admissions.count == sdk.calls  # exactly ONE admission per fetch (was 2)


class GrowwAPIRateLimitException(Exception):
    """SDK-named fake — maps by class name to the retryable ``GrowwRateLimitError``."""


class TestFnoSelfReportedFeedbackStandDown:
    """DEC-SUSPICION-SWEEP-FIXES-001 (D2): the spine's feedback hooks stand DOWN
    when the client self-hooks — the client already fires ``on_rate_limit`` on
    EVERY 429 occurrence (the final, budget-exhausting one included) and
    ``on_success`` per clean call, so the spine firing again on the propagated
    error double-counted the final occurrence (7 ladder hits per exhausted
    window instead of 6 → two+ step-downs from one window)."""

    def test_exhausted_429_counts_once_per_occurrence(self) -> None:
        hits: list[int] = []
        sdk = _FakeSDK(
            script=[GrowwAPIRateLimitException("429"), GrowwAPIRateLimitException("429")]
        )
        # Production wiring twin (historical_pull): the SAME controller hook is
        # baked into the client AND threaded to the spine.
        client = GrowwClient(
            sdk=sdk,
            sleep=lambda _s: None,
            max_attempts=2,
            on_rate_limit=lambda: hits.append(1),
        )
        ingest_option_contract(
            client,
            _Sink(),
            contract=_ce(),
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            on_rate_limit=lambda: hits.append(1),
            max_attempts=0,
        )
        assert sdk.calls == 2
        assert len(hits) == 2  # pre-fix: 3 (client's 2 occurrences + the spine's +1)

    def test_clean_window_success_credited_once(self) -> None:
        ok: list[int] = []
        client = GrowwClient(sdk=_FakeSDK(), sleep=lambda _s: None, on_success=lambda: ok.append(1))
        report = ingest_option_contract(
            client,
            _Sink(),
            contract=_ce(),
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            on_success=lambda: ok.append(1),
        )
        assert report.bars_appended == 1
        assert len(ok) == 1  # the client's per-call credit only — no spine double credit

    def test_unhooked_client_keeps_spine_fallback_feedback(self) -> None:
        hits: list[int] = []
        sdk = _FakeSDK(script=[GrowwAPIRateLimitException("429")])
        client = GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)
        ingest_option_contract(
            client,
            _Sink(),
            contract=_ce(),
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            on_rate_limit=lambda: hits.append(1),
            max_attempts=0,
        )
        assert len(hits) == 1  # the fallback (dry-run / fake) path still reports
