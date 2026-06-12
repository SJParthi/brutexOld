"""Tests for `brutex.dashboard.verdict_banner` — the pull-verdict banner helper.

100% line+branch on the pure presentation mapping (DEC-VERDICT-BANNER-001): the
four verdict kinds → colour + CSS modifier class, the verbatim pass-through of
``headline`` / ``action`` / ``pending_windows``, the ``show_retry_button`` flag
driven by ``needs_operator``, the fail-closed ``ValueError`` on an unknown kind,
and the JSON ``to_dict`` shape.
"""

from __future__ import annotations

import pytest
from brutex.adapters.ingest_common import PullVerdictKind, classify_pull
from brutex.dashboard.verdict_banner import VerdictBanner, banner_for


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
        assert b.pending_windows == 10  # 7 + 3 verbatim from the verdict
        assert "token" in b.headline.lower()
        assert "web.dhan.co" in b.action

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
    @pytest.mark.parametrize(
        "kind",
        ["auth_expired", "quota_exhausted", "transient_residual", "known_gap_only", "clean"],
    )
    def test_every_kind_maps_to_a_verdict_css_modifier(self, kind: PullVerdictKind) -> None:
        # Property: each contract kind yields a `verdict--*` class + a non-empty
        # colour, and `show_retry_button` mirrors `needs_operator` exactly (the
        # COMMON pass-through, not a re-derivation).
        needs_operator = kind in ("auth_expired", "quota_exhausted", "transient_residual")
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
