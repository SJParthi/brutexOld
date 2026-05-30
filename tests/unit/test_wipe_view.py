"""Unit tests for :mod:`brutex.dashboard.wipe_view` — the dashboard wipe guards.

Pure HTTP-side checks (no FastAPI): typed-``WIPE`` confirmation, refuse-while-a-
pull-runs, same-origin CSRF guard, and report shaping — 100% line + branch.
"""

from __future__ import annotations

from brutex.dashboard.wipe_view import (
    WIPE_CONFIRM_WORD,
    is_same_origin,
    parse_wipe_confirmation,
    wipe_refusal_reason,
    wipe_report_payload,
)
from brutex.data.lake_wipe import LakeWipeReport


def test_parse_wipe_confirmation_exact_token() -> None:
    """Only the exact ``WIPE`` token confirms."""
    assert parse_wipe_confirmation(WIPE_CONFIRM_WORD) is True


def test_parse_wipe_confirmation_rejects_others() -> None:
    """Anything else (wrong case, blank, None) does not confirm."""
    assert parse_wipe_confirmation("wipe") is False
    assert parse_wipe_confirmation("") is False
    assert parse_wipe_confirmation(None) is False


def test_wipe_refusal_reason_expiries_running() -> None:
    """A running expiries pull refuses the wipe."""
    reason = wipe_refusal_reason(expiries_running=True, contracts_running=False)
    assert reason is not None
    assert "expiries pull is running" in reason


def test_wipe_refusal_reason_contracts_running() -> None:
    """A running contracts pull refuses the wipe."""
    reason = wipe_refusal_reason(expiries_running=False, contracts_running=True)
    assert reason is not None
    assert "contracts pull is running" in reason


def test_wipe_refusal_reason_idle_allows() -> None:
    """No pull running → no refusal."""
    assert wipe_refusal_reason(expiries_running=False, contracts_running=False) is None


def test_is_same_origin_no_origin_header_allowed() -> None:
    """A request with no Origin (same-page fetch / curl) is allowed."""
    assert is_same_origin(None, "127.0.0.1:9080") is True


def test_is_same_origin_origin_without_host_refused() -> None:
    """An Origin present but no Host can't be matched → refused."""
    assert is_same_origin("http://127.0.0.1:9080", None) is False


def test_is_same_origin_matching() -> None:
    """A cross-site POST whose Origin host matches the Host is allowed."""
    assert is_same_origin("http://127.0.0.1:9080", "127.0.0.1:9080") is True


def test_is_same_origin_mismatch_refused() -> None:
    """A cross-origin POST (Origin host ≠ Host) is refused."""
    assert is_same_origin("http://evil.example", "127.0.0.1:9080") is False


def test_wipe_report_payload_shape() -> None:
    """The report serialises to the documented JSON shape."""
    report = LakeWipeReport(deleted=(("contracts", 249409),), failed=(("expiries", "locked"),))
    assert wipe_report_payload(report) == {
        "deleted": [{"table": "contracts", "rows": 249409}],
        "failed": [{"table": "expiries", "error": "locked"}],
    }
