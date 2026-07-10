"""Tests for `brutex.adapters.ingest_common` — the actionable pull verdict.

100% line+branch on the pure classifier: the four prioritised kinds
(auth_expired > transient_residual > known_gap_only > clean), the priority
ordering, ``pending_windows`` arithmetic, ``needs_operator``, ``auth_cause``
preservation, and the JSON ``to_dict`` shape (DEC-PULL-VERDICT-001).

Also the loud credential refusal + explicit synthetic labeling
(DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001): :func:`gate_pull_credentials` — the
ONE gate every REAL pull surface runs (explicit dry-run short-circuits
WITHOUT resolving credentials; broken credentials return the
``credentials_missing`` refusal verdict relaying the fix text verbatim) —
plus :func:`credentials_refusal_verdict` / :func:`verdict_message` /
:func:`label_synthetic` / :func:`mark_synthetic`. The cross-surface pins
(dashboard routes, CLI race, real AWS-chain permutations) live in
``tests/unit/test_pull_loud_credential_refusal.py``; the pure gate/label
units live HERE so the strict per-module coverage gates scope them.
"""

from __future__ import annotations

from typing import Any

import pytest
from brutex.adapters.ingest_common import (
    CREDENTIALS_REFUSAL_HEADLINE,
    SETTLED_SKIPPED_OPERATOR_THRESHOLD,
    SYNTHETIC_DRY_RUN_LABEL,
    PullVerdict,
    classify_pull,
    credentials_refusal_verdict,
    gate_pull_credentials,
    label_synthetic,
    mark_synthetic,
    verdict_message,
)
from brutex.config.credentials import CredentialsError, GrowwFileCredentials

_FIX_TEXT = "seed /tickvault/prod/groww/api-key + totp-secret in ap-south-1 (test fix text)"


def _good_loader() -> GrowwFileCredentials:
    """Fake AWS loader — credentials resolve (values are FAKE, never real)."""
    return GrowwFileCredentials(api_key="gw_key_123456", totp_secret="JBSWY3DPEHPK3PXP")


def _bad_loader() -> GrowwFileCredentials:
    """Fake AWS loader — fail-fast with an actionable fix message."""
    raise CredentialsError(_FIX_TEXT)


class TestClassifyPull:
    def test_auth_expired_is_highest_priority(self) -> None:
        # A dead token wins even when windows are also still failing.
        v = classify_pull(
            auth_aborted=True,
            still_failed_windows=7,
            failed_pairs=3,
            no_data=2,
            completed=0,
            auth_cause="HTTP 401 invalid TOTP",
        )
        assert v.kind == "auth_expired"
        assert v.needs_operator is True
        assert v.pending_windows == 7  # WINDOWS a re-pull retries (NOT the 3 quarantined pairs)
        assert v.failed_pairs == 3  # the quarantined pairs ride in their own field
        assert v.auth_cause == "HTTP 401 invalid TOTP"
        assert "Groww API key + TOTP secret" in v.action

    def test_quota_exhausted_ranks_just_under_auth(self) -> None:
        # The daily-quota brake (DEC-AUTO-RESUME-QUOTA-001): with residual but a
        # LIVE token, a spent quota outranks transient_residual (no retry lands a
        # bar today — only the IST-midnight reset can).
        v = classify_pull(
            auth_aborted=False,
            still_failed_windows=12,
            failed_pairs=0,
            no_data=0,
            completed=500,
            quota_exhausted=True,
        )
        assert v.kind == "quota_exhausted"
        assert v.needs_operator is True
        assert v.pending_windows == 12
        assert v.auth_cause is None  # quota is not an auth cause
        assert "IST-midnight reset" in v.action

    def test_auth_outranks_quota(self) -> None:
        # A dead token is still the headline even if the quota is also spent.
        v = classify_pull(
            auth_aborted=True,
            still_failed_windows=4,
            failed_pairs=0,
            no_data=0,
            completed=0,
            quota_exhausted=True,
            auth_cause="DH-906",
        )
        assert v.kind == "auth_expired"

    def test_quota_default_false_keeps_prior_behaviour(self) -> None:
        # Omitting quota_exhausted reproduces the pre-feature classification.
        v = classify_pull(
            auth_aborted=False, still_failed_windows=4, failed_pairs=0, no_data=0, completed=100
        )
        assert v.kind == "transient_residual"

    def test_transient_residual_on_still_failed_windows(self) -> None:
        v = classify_pull(
            auth_aborted=False, still_failed_windows=4, failed_pairs=0, no_data=0, completed=100
        )
        assert v.kind == "transient_residual"
        assert v.needs_operator is True
        assert v.pending_windows == 4
        assert v.auth_cause is None  # never carried off the auth path

    def test_quarantined_symbols_when_only_failed_pairs(self) -> None:
        # No retryable windows — the only "failures" are quarantined symbols (not on Groww).
        # A re-pull can NEVER recover them, so it's a COMPLETE pull (grey), NOT "re-pull"
        # (DEC-EMPTY-RESOLVED-REPORTING-001 — the BAJAJ-AUTO / NAM-INDIA headline case).
        v = classify_pull(
            auth_aborted=False, still_failed_windows=0, failed_pairs=2, no_data=0, completed=5
        )
        assert v.kind == "quarantined_symbols"
        assert v.needs_operator is False  # grey, not amber — no re-pull alarm
        assert v.pending_windows == 0  # zero WINDOWS to retry
        assert v.failed_pairs == 2
        assert "not on Groww" in v.action and "No re-pull needed" in v.action

    def test_transient_residual_outranks_quarantine_when_both(self) -> None:
        # Both retryable windows AND quarantined pairs → the re-pull verdict wins (there IS
        # retryable work); the quarantined pairs ride along in the failed_pairs field.
        v = classify_pull(
            auth_aborted=False, still_failed_windows=4, failed_pairs=2, no_data=0, completed=5
        )
        assert v.kind == "transient_residual"
        assert v.pending_windows == 4
        assert v.failed_pairs == 2

    def test_known_gap_only_when_nothing_completed_but_some_empty(self) -> None:
        v = classify_pull(
            auth_aborted=False, still_failed_windows=0, failed_pairs=0, no_data=15, completed=0
        )
        assert v.kind == "known_gap_only"
        assert v.needs_operator is False
        assert v.pending_windows == 0
        assert "No action needed" in v.action

    def test_clean_when_work_completed(self) -> None:
        # Completed work with some incidental no-data is still clean (not a gap-only).
        v = classify_pull(
            auth_aborted=False, still_failed_windows=0, failed_pairs=0, no_data=3, completed=780
        )
        assert v.kind == "clean"
        assert v.needs_operator is False

    def test_clean_when_everything_zero(self) -> None:
        # No completed, no failures, no empties → still clean (nothing to do).
        v = classify_pull(
            auth_aborted=False, still_failed_windows=0, failed_pairs=0, no_data=0, completed=0
        )
        assert v.kind == "clean"


class TestPullVerdictSerialisation:
    def test_to_dict_round_trips_every_field(self) -> None:
        v = PullVerdict(
            kind="auth_expired",
            headline="🔑 ...",
            action="Regenerate ...",
            pending_windows=10,
            failed_pairs=3,
            no_data=2,
            completed=0,
            auth_cause="DH-906",
        )
        d = v.to_dict()
        assert d == {
            "kind": "auth_expired",
            "headline": "🔑 ...",
            "action": "Regenerate ...",
            "needs_operator": True,
            "pending_windows": 10,
            "failed_pairs": 3,
            "no_data": 2,
            "completed": 0,
            "auth_cause": "DH-906",
            "settled_skipped": 0,
            "settled_exhausted": 0,
        }

    def test_needs_operator_false_for_clean_and_known_gap(self) -> None:
        clean = classify_pull(
            auth_aborted=False, still_failed_windows=0, failed_pairs=0, no_data=0, completed=1
        )
        gap = classify_pull(
            auth_aborted=False, still_failed_windows=0, failed_pairs=0, no_data=1, completed=0
        )
        assert clean.to_dict()["needs_operator"] is False
        assert gap.to_dict()["needs_operator"] is False


# ────────────────────────────────────────────────────────────────────────
# Loud credential refusal — the ONE shared gate + refusal verdict +
# synthetic labels (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001, audit finding R1)
# ────────────────────────────────────────────────────────────────────────


class TestGatePullCredentials:
    def test_explicit_dry_run_short_circuits_without_resolving(self) -> None:
        calls: list[bool] = []

        def _spy() -> str | None:
            calls.append(True)
            return _FIX_TEXT

        assert gate_pull_credentials(dry_run_requested=True, credentials_error=_spy) is None
        assert calls == []  # credentials are IRRELEVANT to an explicit dry-run

    def test_good_credentials_proceed(self) -> None:
        assert (
            gate_pull_credentials(dry_run_requested=False, credentials_error=lambda: None) is None
        )

    def test_broken_credentials_return_the_refusal_verdict(self) -> None:
        verdict = gate_pull_credentials(
            dry_run_requested=False, credentials_error=lambda: _FIX_TEXT
        )
        assert verdict is not None
        assert verdict.kind == "credentials_missing"
        assert verdict.needs_operator is True
        assert verdict.headline == CREDENTIALS_REFUSAL_HEADLINE
        assert _FIX_TEXT in verdict.action  # relayed VERBATIM (DYNAMIC)
        assert verdict.auth_cause == _FIX_TEXT
        assert (verdict.pending_windows, verdict.failed_pairs, verdict.no_data) == (0, 0, 0)
        assert verdict.completed == 0

    def test_default_resolver_refuses_via_the_real_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # credentials_error=None → the gate lazily imports the REAL resolver.
        monkeypatch.setattr("brutex.config.credentials_aws.load_groww_aws_credentials", _bad_loader)
        verdict = gate_pull_credentials(dry_run_requested=False)
        assert verdict is not None
        assert _FIX_TEXT in verdict.action

    def test_default_resolver_proceeds_via_the_real_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "brutex.config.credentials_aws.load_groww_aws_credentials", _good_loader
        )
        assert gate_pull_credentials(dry_run_requested=False) is None


class TestRefusalVerdictAndLabels:
    def test_verdict_message_joins_headline_and_action(self) -> None:
        verdict = credentials_refusal_verdict(_FIX_TEXT)
        message = verdict_message(verdict)
        assert message.startswith(CREDENTIALS_REFUSAL_HEADLINE)
        assert _FIX_TEXT in message

    def test_refusal_verdict_to_dict_is_needs_operator(self) -> None:
        payload = credentials_refusal_verdict(_FIX_TEXT).to_dict()
        assert payload["kind"] == "credentials_missing"
        assert payload["needs_operator"] is True
        assert _FIX_TEXT in payload["action"]

    def test_refusal_verdict_never_mentions_success(self) -> None:
        verdict = credentials_refusal_verdict(_FIX_TEXT)
        assert "REFUSED" in verdict.headline
        assert "NEVER silently degrades" in verdict.action

    def test_label_synthetic_prefixes_the_loud_label(self) -> None:
        assert label_synthetic("index spot") == f"{SYNTHETIC_DRY_RUN_LABEL} — index spot"

    def test_mark_synthetic_relabels_headline_and_action_preserving_kind(self) -> None:
        clean = classify_pull(
            auth_aborted=False,
            still_failed_windows=0,
            failed_pairs=0,
            no_data=0,
            completed=3,
        )
        marked = mark_synthetic(clean)
        assert marked.kind == clean.kind  # the classifier stays the ONE authority
        assert marked.headline.startswith(f"🧪 {SYNTHETIC_DRY_RUN_LABEL} — ")
        assert clean.headline in marked.headline
        assert "DISCARDED — zero real bars landed" in marked.action
        assert isinstance(marked, PullVerdict)


class TestMassEmptyHaltVerdict:
    """DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 — the mass-empty halt kind."""

    def test_mass_empty_halt_kind_and_needs_operator(self) -> None:
        verdict = classify_pull(
            auth_aborted=False,
            still_failed_windows=4,
            failed_pairs=0,
            no_data=0,
            completed=2,
            mass_empty_cause="Mass-empty circuit breaker TRIPPED (consecutive rule): 40 of 41",
        )
        assert verdict.kind == "mass_empty_halt"
        assert verdict.needs_operator is True
        assert "HALTED" in verdict.headline
        # The halt's own count-naming message is PREFIXED to the action…
        assert verdict.action.startswith("Mass-empty circuit breaker TRIPPED")
        # …and the static action names the causes + the re-verify path.
        assert "[R2]" in verdict.action and "[R3]" in verdict.action
        assert "reopen-skipped" in verdict.action
        assert verdict.to_dict()["kind"] == "mass_empty_halt"

    def test_auth_expired_outranks_mass_empty(self) -> None:
        verdict = classify_pull(
            auth_aborted=True,
            still_failed_windows=0,
            failed_pairs=0,
            no_data=0,
            completed=0,
            mass_empty_cause="tripped",
        )
        assert verdict.kind == "auth_expired"

    def test_mass_empty_outranks_quota_and_transient(self) -> None:
        verdict = classify_pull(
            auth_aborted=False,
            still_failed_windows=9,
            failed_pairs=1,
            no_data=0,
            completed=0,
            quota_exhausted=True,
            mass_empty_cause="tripped",
        )
        assert verdict.kind == "mass_empty_halt"


class TestSettledSkippedHonesty:
    """DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 — a settling pull may never read silent."""

    def test_clean_with_settles_never_says_no_action_needed(self) -> None:
        verdict = classify_pull(
            auth_aborted=False,
            still_failed_windows=0,
            failed_pairs=0,
            no_data=0,
            completed=5,
            settled_skipped=27,
        )
        assert verdict.kind == "clean"
        assert verdict.action != "No action needed."
        assert "27 window(s)" in verdict.action
        assert "reopen-skipped" in verdict.action
        assert verdict.settled_skipped == 27
        assert verdict.to_dict()["settled_skipped"] == 27

    def test_clean_without_settles_is_byte_identical(self) -> None:
        verdict = classify_pull(
            auth_aborted=False,
            still_failed_windows=0,
            failed_pairs=0,
            no_data=0,
            completed=5,
        )
        assert verdict.action == "No action needed."
        assert verdict.settled_skipped == 0

    def test_settle_note_appended_to_non_clean_kinds(self) -> None:
        # The primary instruction stays FIRST; the settle note rides after it.
        verdict = classify_pull(
            auth_aborted=False,
            still_failed_windows=3,
            failed_pairs=0,
            no_data=0,
            completed=1,
            settled_skipped=2,
        )
        assert verdict.kind == "transient_residual"
        assert verdict.action.startswith("Re-pull to finish")
        assert "2 window(s)" in verdict.action

    def test_settle_note_on_every_kind(self) -> None:
        # quarantined / known-gap / quota / auth / mass-empty all carry the note.
        cases: list[dict[str, Any]] = [
            {"auth_aborted": True},
            {"quota_exhausted": True},
            {"mass_empty_cause": "tripped"},
            {"failed_pairs": 1},
            {"no_data": 1},
        ]
        for extra in cases:
            kwargs: dict[str, Any] = {
                "auth_aborted": False,
                "still_failed_windows": 0,
                "failed_pairs": 0,
                "no_data": 0,
                "completed": 0,
                "settled_skipped": 7,
            }
            kwargs.update(extra)
            verdict = classify_pull(**kwargs)
            assert "7 window(s)" in verdict.action, verdict.kind


class TestSettledSkippedOperatorThreshold:
    """Minor M-4 — a mass write-off flips needs_operator even on a clean verdict."""

    def _clean(self, settled_skipped: int) -> PullVerdict:
        return classify_pull(
            auth_aborted=False,
            still_failed_windows=0,
            failed_pairs=0,
            no_data=0,
            completed=5,
            settled_skipped=settled_skipped,
        )

    def test_above_threshold_flips_needs_operator_on_clean(self) -> None:
        verdict = self._clean(SETTLED_SKIPPED_OPERATOR_THRESHOLD + 1)
        assert verdict.kind == "clean"
        assert verdict.needs_operator is True  # a mass write-off demands eyes
        assert verdict.to_dict()["needs_operator"] is True

    def test_at_threshold_stays_quiet(self) -> None:
        # The gate is STRICTLY above (> 50): exactly-at stays a quiet clean.
        verdict = self._clean(SETTLED_SKIPPED_OPERATOR_THRESHOLD)
        assert verdict.kind == "clean"
        assert verdict.needs_operator is False

    def test_threshold_applies_on_non_clean_kinds_too(self) -> None:
        verdict = classify_pull(
            auth_aborted=False,
            still_failed_windows=0,
            failed_pairs=2,  # quarantined_symbols — normally needs_operator=False
            no_data=0,
            completed=1,
            settled_skipped=SETTLED_SKIPPED_OPERATOR_THRESHOLD + 5,
        )
        assert verdict.kind == "quarantined_symbols"
        assert verdict.needs_operator is True


class TestReVerifyGuidanceTexts:
    """Minor M-1 — the halt + settle-note texts explain the bounded re-verify path."""

    def test_mass_empty_action_names_limit_and_envs(self) -> None:
        verdict = classify_pull(
            auth_aborted=False,
            still_failed_windows=0,
            failed_pairs=0,
            no_data=0,
            completed=0,
            mass_empty_cause="tripped",
        )
        assert "--limit N" in verdict.action  # the bounded re-verify sample
        assert "REOPENED" in verdict.action  # re-checks are breaker-exempt
        assert "BRUTEX_EMPTY_BREAKER" in verdict.action  # the env-tunable thresholds

    def test_settle_note_names_limit_and_exemption(self) -> None:
        verdict = classify_pull(
            auth_aborted=False,
            still_failed_windows=0,
            failed_pairs=0,
            no_data=0,
            completed=3,
            settled_skipped=2,
        )
        assert "--limit N" in verdict.action
        assert "breaker-exempt" in verdict.action


class TestSettledExhaustedHonesty:
    """Review round 1, MAJOR 1.4 — exhausted settles are never 'permanent no-data'."""

    def _verdict(self, *, settled_skipped: int, settled_exhausted: int) -> PullVerdict:
        return classify_pull(
            auth_aborted=False,
            still_failed_windows=0,
            failed_pairs=0,
            no_data=0,
            completed=5,
            settled_skipped=settled_skipped,
            settled_exhausted=settled_exhausted,
        )

    def test_mixed_settle_names_both_counts_correctly(self) -> None:
        # 5 settles, 2 of them exhausted: the no-data wording covers ONLY the
        # 3 genuine settles; the 2 exhausted ones are named as fetch failures.
        verdict = self._verdict(settled_skipped=5, settled_exhausted=2)
        assert "3 window(s) were settled as permanent no-data" in verdict.action
        assert "2 window(s) settled after repeated fetch failures" in verdict.action
        assert "every fetch ERRORED" in verdict.action
        assert "the vendor never confirmed no-data" in verdict.action
        assert "reopen-skipped" in verdict.action
        assert verdict.settled_skipped == 5
        assert verdict.settled_exhausted == 2
        assert verdict.to_dict()["settled_exhausted"] == 2

    def test_all_exhausted_settle_never_reads_no_data(self) -> None:
        # The pre-fix lie, pinned dead: an all-exhausted pull must carry ZERO
        # "permanent no-data" wording — every fetch errored.
        verdict = self._verdict(settled_skipped=2, settled_exhausted=2)
        assert "permanent no-data" not in verdict.action
        assert "2 window(s) settled after repeated fetch failures" in verdict.action

    def test_zero_exhausted_keeps_the_prior_wording(self) -> None:
        verdict = self._verdict(settled_skipped=4, settled_exhausted=0)
        assert "4 window(s) were settled as permanent no-data" in verdict.action
        assert "repeated fetch failures" not in verdict.action

    def test_exhausted_clamped_into_the_skipped_total(self) -> None:
        # Defensive: a violating caller (exhausted > skipped) never renders a
        # negative no-data count — the subset is clamped.
        verdict = self._verdict(settled_skipped=2, settled_exhausted=7)
        assert "-" not in verdict.action.split("NOTE:")[1].split("window(s)")[0]
        assert "2 window(s) settled after repeated fetch failures" in verdict.action
        assert "permanent no-data" not in verdict.action

    def test_exhausted_note_rides_non_clean_kinds_too(self) -> None:
        verdict = classify_pull(
            auth_aborted=False,
            still_failed_windows=3,
            failed_pairs=0,
            no_data=0,
            completed=1,
            settled_skipped=1,
            settled_exhausted=1,
        )
        assert verdict.kind == "transient_residual"
        assert verdict.action.startswith("Re-pull to finish")
        assert "settled after repeated fetch failures" in verdict.action

    def test_default_zero_is_byte_identical(self) -> None:
        # Omitting settled_exhausted reproduces the pre-feature action text.
        with_default = classify_pull(
            auth_aborted=False,
            still_failed_windows=0,
            failed_pairs=0,
            no_data=0,
            completed=5,
            settled_skipped=3,
        )
        explicit = self._verdict(settled_skipped=3, settled_exhausted=0)
        assert with_default.action == explicit.action
        assert with_default.settled_exhausted == 0
