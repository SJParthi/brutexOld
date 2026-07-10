"""Tests for `brutex.dashboard.verdict_banner` — the pull-verdict banner helper.

100% line+branch on the pure presentation mapping (DEC-VERDICT-BANNER-001): every
contract verdict kind → colour + CSS modifier class, the verbatim pass-through of
``headline`` / ``action`` / ``pending_windows``, the ``show_retry_button`` flag
driven by ``needs_operator``, the fail-closed ``ValueError`` on an unknown kind,
the JSON ``to_dict`` shape — and the kind-set DRIFT PIN: ``_KIND_STYLE`` must
name exactly the ``PullVerdictKind`` contract values (2026-07-08 regression:
``mass_empty_halt`` was emittable by ``classify_pull`` but missing from
``_KIND_STYLE``, so a real halted pull 500'd the pull-verdict route).
"""

from __future__ import annotations

from typing import get_args

import pytest
from brutex.adapters.ingest_common import (
    PullVerdictKind,
    classify_pull,
    credentials_refusal_verdict,
)
from brutex.dashboard.verdict_banner import _KIND_STYLE, VerdictBanner, banner_for


def _verdict_dict(
    *,
    auth_aborted: bool = False,
    still_failed_windows: int = 0,
    failed_pairs: int = 0,
    no_data: int = 0,
    completed: int = 0,
    auth_cause: str | None = None,
    quota_exhausted: bool = False,
) -> dict[str, object]:
    """Build a real PullVerdict.to_dict() so the helper is tested against the live contract."""
    return classify_pull(
        auth_aborted=auth_aborted,
        still_failed_windows=still_failed_windows,
        failed_pairs=failed_pairs,
        no_data=no_data,
        completed=completed,
        auth_cause=auth_cause,
        quota_exhausted=quota_exhausted,
    ).to_dict()


class TestBannerForEachKind:
    def test_auth_expired_is_red_with_retry(self) -> None:
        b = banner_for(
            _verdict_dict(
                auth_aborted=True, still_failed_windows=7, failed_pairs=3, auth_cause="DH-906"
            )
        )
        assert b.kind == "auth_expired"
        assert b.colour == "red"
        assert b.css_class == "verdict--red"
        assert b.show_retry_button is True  # needs_operator
        assert b.pending_windows == 7  # WINDOWS a re-pull retries (NOT the 3 quarantined pairs)
        assert "auth" in b.headline.lower()
        assert "TOTP secret" in b.action

    def test_mass_empty_halt_is_red_with_retry(self) -> None:
        # DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001: the breaker HALTED the pull —
        # red (operator must triage before re-pulling), needs_operator. Driven
        # through the REAL classify_pull so the live contract is what's tested.
        # Regression (2026-07-08): this kind was emittable by classify_pull but
        # MISSING from _KIND_STYLE — banner_for raised ValueError and the
        # /api/v1/universe/pull-verdict route 500'd on a real halted pull.
        v = classify_pull(
            auth_aborted=False,
            still_failed_windows=9,
            failed_pairs=0,
            no_data=0,
            completed=10,
            mass_empty_cause="Mass-empty halt: 40/40 empty (12 consecutive).",
        )
        b = banner_for(v.to_dict())
        assert b.kind == "mass_empty_halt"
        assert b.colour == "red"
        assert b.css_class == "verdict--red"
        assert b.show_retry_button is True  # needs_operator — triage first
        assert b.pending_windows == 9
        assert "HALTED" in b.headline
        assert b.action.startswith("Mass-empty halt: 40/40 empty (12 consecutive).")

    def test_credentials_missing_is_red_with_retry(self) -> None:
        # DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001: the PRE-pull refusal verdict,
        # built by its REAL producer (credentials_refusal_verdict — the only
        # writer of this kind), maps red with the retry control shown.
        v = credentials_refusal_verdict("BRUTEX_GROWW_API_KEY did not resolve")
        b = banner_for(v.to_dict())
        assert b.kind == "credentials_missing"
        assert b.colour == "red"
        assert b.css_class == "verdict--red"
        assert b.show_retry_button is True  # needs_operator by construction
        assert b.pending_windows == 0
        assert "BRUTEX_GROWW_API_KEY did not resolve" in b.action

    def test_transient_residual_is_amber_with_retry(self) -> None:
        b = banner_for(_verdict_dict(still_failed_windows=4, completed=100))
        assert b.kind == "transient_residual"
        assert b.colour == "amber"
        assert b.css_class == "verdict--amber"
        assert b.show_retry_button is True
        assert b.pending_windows == 4

    def test_quota_exhausted_is_amber_with_retry(self) -> None:
        # DEC-AUTO-RESUME-QUOTA-001: the auto-resume paused on the daily quota —
        # amber "paused, resume later" lane (not red "broken"), needs_operator.
        b = banner_for(_verdict_dict(still_failed_windows=12, completed=500, quota_exhausted=True))
        assert b.kind == "quota_exhausted"
        assert b.colour == "amber"
        assert b.css_class == "verdict--amber"
        assert b.show_retry_button is True  # needs_operator (resume after reset)
        assert b.pending_windows == 12
        assert "quota" in b.headline.lower()

    def test_quarantined_symbols_is_grey_no_retry(self) -> None:
        # DEC-EMPTY-RESOLVED-REPORTING-001: a pull whose only "failures" are quarantined
        # symbols (not on Groww) is COMPLETE — grey, no re-pull alarm, no retry button.
        b = banner_for(_verdict_dict(failed_pairs=2, completed=5))
        assert b.kind == "quarantined_symbols"
        assert b.colour == "grey"
        assert b.css_class == "verdict--grey"
        assert b.show_retry_button is False  # not needs_operator — re-pull can't help
        assert b.pending_windows == 0  # zero WINDOWS to retry

    def test_known_gap_only_is_grey_no_retry(self) -> None:
        b = banner_for(_verdict_dict(no_data=5, completed=0))
        assert b.kind == "known_gap_only"
        assert b.colour == "grey"
        assert b.css_class == "verdict--grey"
        assert b.show_retry_button is False  # not needs_operator
        assert b.pending_windows == 0

    def test_clean_is_green_no_retry(self) -> None:
        b = banner_for(_verdict_dict(completed=200))
        assert b.kind == "clean"
        assert b.colour == "green"
        assert b.css_class == "verdict--green"
        assert b.show_retry_button is False
        assert b.pending_windows == 0


class TestKindSetDriftPin:
    def test_style_map_covers_exactly_the_contract_kinds(self) -> None:
        # THE drift pin (2026-07-08): `_KIND_STYLE` must name EXACTLY the
        # `PullVerdictKind` Literal values. A kind added to the contract
        # without a style (the `mass_empty_halt` regression — a real halted
        # pull 500'd the pull-verdict route) or a style with no contract kind
        # fails HERE by name, before any route or hypothesis run can find it.
        assert set(_KIND_STYLE) == set(get_args(PullVerdictKind))


class TestBannerForGuards:
    def test_unknown_kind_raises_value_error(self) -> None:
        # Fail-closed: an unrecognised kind is never rendered uncoloured.
        with pytest.raises(ValueError, match="unknown verdict kind 'bogus'"):
            banner_for(
                {
                    "kind": "bogus",
                    "headline": "h",
                    "action": "a",
                    "pending_windows": 0,
                    "needs_operator": False,
                }
            )

    def test_passes_through_headline_action_verbatim(self) -> None:
        # COMMON: no re-derivation — the helper copies the verdict strings exactly.
        raw = {
            "kind": "clean",
            "headline": "HEAD",
            "action": "DO X",
            "pending_windows": 0,
            "needs_operator": False,
        }
        b = banner_for(raw)
        assert b.headline == "HEAD"
        assert b.action == "DO X"

    def test_pending_windows_coerced_to_int(self) -> None:
        b = banner_for(
            {
                "kind": "clean",
                "headline": "h",
                "action": "a",
                "pending_windows": "0",
                "needs_operator": False,
            }
        )
        assert b.pending_windows == 0
        assert isinstance(b.pending_windows, int)

    def test_show_retry_button_coerced_to_bool(self) -> None:
        b = banner_for(
            {
                "kind": "auth_expired",
                "headline": "h",
                "action": "a",
                "pending_windows": 0,
                "needs_operator": 1,
            }
        )
        assert b.show_retry_button is True


class TestToDict:
    def test_to_dict_shape(self) -> None:
        b = VerdictBanner(
            kind="clean",
            css_class="verdict--green",
            colour="green",
            headline="✅ Pull complete.",
            action="No action needed.",
            pending_windows=0,
            show_retry_button=False,
        )
        assert b.to_dict() == {
            "kind": "clean",
            "css_class": "verdict--green",
            "colour": "green",
            "headline": "✅ Pull complete.",
            "action": "No action needed.",
            "pending_windows": 0,
            "show_retry_button": False,
        }


class TestProperties:
    @pytest.mark.parametrize("kind", sorted(get_args(PullVerdictKind)))
    def test_every_kind_maps_to_a_verdict_css_modifier(self, kind: PullVerdictKind) -> None:
        # Property: each contract kind yields a `verdict--*` class + a non-empty
        # colour, and `show_retry_button` mirrors `needs_operator` exactly (the
        # COMMON pass-through, not a re-derivation).
        needs_operator = kind in (
            "credentials_missing",
            "auth_expired",
            "mass_empty_halt",
            "quota_exhausted",
            "transient_residual",
        )
        b = banner_for(
            {
                "kind": kind,
                "headline": "h",
                "action": "a",
                "pending_windows": 0,
                "needs_operator": needs_operator,
            }
        )
        assert b.css_class.startswith("verdict--")
        assert b.colour
        assert b.show_retry_button is needs_operator
