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

from brutex.adapters.ingest_common import classify_pull, credentials_refusal_verdict
from brutex.dashboard.verdict_banner import banner_for


def _banner_json(**kwargs: object) -> str:
    verdict = classify_pull(**kwargs).to_dict()  # type: ignore[arg-type]
    return json.dumps(banner_for(verdict).to_dict(), sort_keys=True)


# ── Frozen golden bytes: one verdict per kind (the exact pull-verdict route payload) ──

_GOLDEN = {
    "auth_expired": (
        '{"action": "Re-check the Groww API key + TOTP secret, then re-pull. '
        'A retry can NEVER succeed on bad credentials \\u2014 only valid credentials can.", '
        '"colour": "red", "css_class": "verdict--red", '
        '"headline": "\\ud83d\\udd11 Groww auth failed mid-pull \\u2014 the pull aborted.", '
        '"kind": "auth_expired", "pending_windows": 7, "show_retry_button": true}'
    ),
    "quarantined_symbols": (
        '{"action": "No re-pull needed \\u2014 these symbols are not on Groww (e.g. a '
        "renamed/delisted ticker or a non-Groww constituent), so a retry can NEVER land a "
        'bar. Verify the symbol list; every resolvable symbol completed.", '
        '"colour": "grey", "css_class": "verdict--grey", '
        '"headline": "\\u2705 Pull complete \\u2014 some symbols were quarantined (not on Groww).", '
        '"kind": "quarantined_symbols", "pending_windows": 0, "show_retry_button": false}'
    ),
    "transient_residual": (
        '{"action": "Re-pull to finish \\u2014 already-pulled windows are skipped and only the '
        'still-pending windows are re-fetched (auto-retry mops up transient 429s).", '
        '"colour": "amber", "css_class": "verdict--amber", '
        '"headline": "\\u23f3 Some windows are still pending after retries.", '
        '"kind": "transient_residual", "pending_windows": 4, "show_retry_button": true}'
    ),
    "known_gap_only": (
        '{"action": "No action needed \\u2014 these are reference-only symbols Groww does not serve '
        'intraday (e.g. broad-market indices); they are not brute-force inputs.", '
        '"colour": "grey", "css_class": "verdict--grey", '
        '"headline": "\\u2139\\ufe0f No new bars \\u2014 these symbols have no intraday on Groww.", '
        '"kind": "known_gap_only", "pending_windows": 0, "show_retry_button": false}'
    ),
    "clean": (
        '{"action": "No action needed.", "colour": "green", "css_class": "verdict--green", '
        '"headline": "\\u2705 Pull complete.", '
        '"kind": "clean", "pending_windows": 0, "show_retry_button": false}'
    ),
    # 2026-07-08 regression golden: `mass_empty_halt` was emittable by
    # classify_pull but MISSING from `_KIND_STYLE`, so banner_for raised on a
    # real halted pull (the pull-verdict route 500'd). Frozen red + retry.
    "mass_empty_halt": (
        '{"action": "Mass-empty halt: 40/40 empty (12 consecutive). The vendor answered a '
        "mass of windows with clean-but-EMPTY responses \\u2014 either a soft-throttle serving "
        "empty 200s [R2], mis-addressed requests [R3], or a genuine pre-listing gap; the code "
        "cannot tell which, so NOTHING more was settled as SKIPPED and every un-settled window "
        "stays retryable. Wait out a possible throttle, verify the symbol addressing, then "
        "re-pull; if windows were settled before the halt, `brutex reopen-skipped` re-opens "
        "them for re-verification (`--limit N` bounds the re-open to a sample). Re-opened "
        "(REOPENED) windows are deliberate re-checks the breaker never counts, so reopen + "
        "re-pull cannot re-trip endlessly; if a genuine no-data universe keeps tripping, the "
        "thresholds are env-tunable (BRUTEX_EMPTY_BREAKER_*) and BRUTEX_EMPTY_BREAKER=off is "
        'the explicit kill switch.", '
        '"colour": "red", "css_class": "verdict--red", '
        '"headline": "\\ud83d\\udea8 Pull HALTED \\u2014 mass empty-response circuit breaker '
        'tripped (suspected vendor soft-throttle or mis-addressing).", '
        '"kind": "mass_empty_halt", "pending_windows": 9, "show_retry_button": true}'
    ),
    # The operator's cooperative Stop (DEC-JOB-COOPERATIVE-CANCEL-001
    # continuation-v3 regression golden): `cancelled` was previously NOT a
    # verdict kind at all — a cancelled pull fell through to the GREEN `clean`
    # "\u2705 Pull complete. / No action needed." banner. Frozen amber + retry.
    "cancelled": (
        '{"action": "Re-run the pull to resume the remainder \\u2014 the pull stopped at a '
        "window boundary on the operator's Stop, so every settled window is durably in the "
        'ledger and only the un-pulled windows are re-fetched on the next pull.", '
        '"colour": "amber", "css_class": "verdict--amber", '
        '"headline": "\\u23f9\\ufe0f Pull CANCELLED at operator request \\u2014 an honest '
        'partial, stopped cleanly.", '
        '"kind": "cancelled", "pending_windows": 6, "show_retry_button": true}'
    ),
    # The PRE-pull credentials refusal (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001),
    # built by its one real producer `credentials_refusal_verdict`.
    "credentials_missing": (
        '{"action": "Groww credentials did not resolve: missing SSM parameter. (Synthetic '
        "data runs ONLY on an explicit dry-run request \\u2014 a real pull NEVER silently "
        'degrades to synthetic.)", '
        '"colour": "red", "css_class": "verdict--red", '
        '"headline": "\\ud83d\\udd12 Pull REFUSED \\u2014 Groww credentials unavailable '
        '(nothing was fetched, nothing was written).", '
        '"kind": "credentials_missing", "pending_windows": 0, "show_retry_button": true}'
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


def test_quarantined_symbols_golden() -> None:
    assert (
        _banner_json(
            auth_aborted=False,
            still_failed_windows=0,
            failed_pairs=2,
            no_data=0,
            completed=5,
        )
        == _GOLDEN["quarantined_symbols"]
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


def test_mass_empty_halt_golden() -> None:
    assert (
        _banner_json(
            auth_aborted=False,
            still_failed_windows=9,
            failed_pairs=0,
            no_data=0,
            completed=10,
            mass_empty_cause="Mass-empty halt: 40/40 empty (12 consecutive).",
        )
        == _GOLDEN["mass_empty_halt"]
    )


def test_credentials_missing_golden() -> None:
    verdict = credentials_refusal_verdict(
        "Groww credentials did not resolve: missing SSM parameter."
    ).to_dict()
    assert (
        json.dumps(banner_for(verdict).to_dict(), sort_keys=True) == _GOLDEN["credentials_missing"]
    )


def test_cancelled_golden() -> None:
    assert (
        _banner_json(
            auth_aborted=False,
            still_failed_windows=6,
            failed_pairs=0,
            no_data=0,
            completed=42,
            cancelled=True,
        )
        == _GOLDEN["cancelled"]
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
