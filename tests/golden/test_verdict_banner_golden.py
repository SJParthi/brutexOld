"""Golden byte-equality for the verdict-banner JSON (DEC-VERDICT-BANNER-001).

The ``/api/v1/universe/pull-verdict`` route serves
``banner_for(verdict).to_dict()`` and ``/universe`` paints the banner directly
from those keys (``css_class`` for the colour, ``show_retry_button`` for the
retry control). Any drift in a key name, the colour/class mapping, or a scalar
type would silently mis-paint the banner, so this golden **freezes the exact
canonical JSON bytes** for one hand-picked verdict per kind.

Where ``tests/unit/test_verdict_banner.py`` asserts the mapping field-by-field
and the property file asserts it generatively, this pins the
``json.dumps(..., sort_keys=True)`` bytes byte-for-byte.
"""

from __future__ import annotations

import json

from brutex.adapters.dhan.pull_verdict import classify_pull
from brutex.dashboard.verdict_banner import banner_for


def _banner_json(**kwargs: object) -> str:
    verdict = classify_pull(**kwargs).to_dict()  # type: ignore[arg-type]
    return json.dumps(banner_for(verdict).to_dict(), sort_keys=True)


# ── Frozen golden bytes: one verdict per kind (the exact pull-verdict route payload) ──

_GOLDEN = {
    "auth_expired": (
        '{"action": "Regenerate the 24-hour token at web.dhan.co, then re-pull. '
        'A retry can NEVER succeed on a dead token \\u2014 only a fresh token can.", '
        '"colour": "red", "css_class": "verdict--red", '
        '"headline": "\\ud83d\\udd11 Dhan token expired mid-pull \\u2014 the pull aborted.", '
        '"kind": "auth_expired", "pending_windows": 10, "show_retry_button": true}'
    ),
    "transient_residual": (
        '{"action": "Re-pull to finish \\u2014 already-pulled windows are skipped and only the '
        'still-pending windows are re-fetched (auto-retry mops up transient 429s).", '
        '"colour": "amber", "css_class": "verdict--amber", '
        '"headline": "\\u23f3 Some windows are still pending after retries.", '
        '"kind": "transient_residual", "pending_windows": 4, "show_retry_button": true}'
    ),
    "known_gap_only": (
        '{"action": "No action needed \\u2014 these are reference-only symbols Dhan does not serve '
        'intraday (e.g. broad-market indices); they are not brute-force inputs.", '
        '"colour": "grey", "css_class": "verdict--grey", '
        '"headline": "\\u2139\\ufe0f No new bars \\u2014 these symbols have no intraday on Dhan.", '
        '"kind": "known_gap_only", "pending_windows": 0, "show_retry_button": false}'
    ),
    "clean": (
        '{"action": "No action needed.", "colour": "green", "css_class": "verdict--green", '
        '"headline": "\\u2705 Pull complete.", '
        '"kind": "clean", "pending_windows": 0, "show_retry_button": false}'
    ),
}


def test_auth_expired_golden() -> None:
    assert (
        _banner_json(
            auth_aborted=True,
            still_failed_windows=7,
            failed_pairs=3,
            no_data=0,
            completed=0,
            auth_cause="HTTP 400 DH-906 Invalid Token",
        )
        == _GOLDEN["auth_expired"]
    )


def test_transient_residual_golden() -> None:
    assert (
        _banner_json(
            auth_aborted=False,
            still_failed_windows=4,
            failed_pairs=0,
            no_data=0,
            completed=100,
        )
        == _GOLDEN["transient_residual"]
    )


def test_known_gap_only_golden() -> None:
    assert (
        _banner_json(
            auth_aborted=False,
            still_failed_windows=0,
            failed_pairs=0,
            no_data=5,
            completed=0,
        )
        == _GOLDEN["known_gap_only"]
    )


def test_clean_golden() -> None:
    assert (
        _banner_json(
            auth_aborted=False,
            still_failed_windows=0,
            failed_pairs=0,
            no_data=0,
            completed=200,
        )
        == _GOLDEN["clean"]
    )
