"""Property tests for `brutex.dashboard.verdict_banner` (DEC-VERDICT-BANNER-001).

Hypothesis invariants over arbitrary verdict dicts:

* **kind ↔ colour bijection** — each contract kind always maps to its one fixed
  ``(css_class, colour)`` and back, for ANY headline / action / pending count;
* **retry button mirrors needs_operator** — ``show_retry_button`` is exactly the
  verdict's own ``needs_operator`` flag (the COMMON pass-through, never re-derived);
* **verbatim pass-through** — ``headline`` / ``action`` are copied unchanged and
  ``pending_windows`` round-trips through ``int(...)``;
* **fail-closed** — any kind outside the contract set ALWAYS raises ``ValueError``.

``_KINDS`` below is this file's INDEPENDENT hand-written oracle of the contract
kind set. ``test_kinds_oracle_matches_production`` pins it against the
production ``_KIND_STYLE`` keys, so a kind added on one side without the other
fails LOUDLY here instead of surfacing as a hypothesis falsifying example
(2026-07-08: hypothesis's constant injection generated the then-missing
``'credentials_missing'`` in ``test_unknown_kind_always_raises`` — this drift
pin makes that failure mode structural, not probabilistic).

The helper is pure (no Rust wheel, no lake), so this runs on a bare checkout.
"""

from __future__ import annotations

from brutex.dashboard.verdict_banner import _KIND_STYLE, banner_for
from hypothesis import given
from hypothesis import strategies as st

_KINDS = (
    "credentials_missing",
    "auth_expired",
    "mass_empty_halt",
    "cancelled",
    "transient_residual",
    "known_gap_only",
    "clean",
    "quota_exhausted",
    "quarantined_symbols",
)
_EXPECTED = {
    "credentials_missing": ("verdict--red", "red"),
    "auth_expired": ("verdict--red", "red"),
    "mass_empty_halt": ("verdict--red", "red"),
    # The operator's cooperative Stop (DEC-JOB-COOPERATIVE-CANCEL-001
    # continuation-v3): an honest amber "re-run to resume" partial — never green.
    "cancelled": ("verdict--amber", "amber"),
    "transient_residual": ("verdict--amber", "amber"),
    "known_gap_only": ("verdict--grey", "grey"),
    "clean": ("verdict--green", "green"),
    "quota_exhausted": ("verdict--amber", "amber"),
    "quarantined_symbols": ("verdict--grey", "grey"),
}
_NEEDS_OPERATOR = {
    "credentials_missing",
    "auth_expired",
    "mass_empty_halt",
    "quota_exhausted",
    "cancelled",
    "transient_residual",
}


def test_kinds_oracle_matches_production() -> None:
    # The drift pin: this file's independent kind oracle must name EXACTLY the
    # production style-map kinds — a kind added to `_KIND_STYLE` (or removed)
    # without updating this oracle fails here by name, never as a
    # constant-injected hypothesis falsifying example.
    assert set(_KINDS) == set(_KIND_STYLE)
    assert dict(_KIND_STYLE) == _EXPECTED


@given(
    kind=st.sampled_from(_KINDS),
    headline=st.text(),
    action=st.text(),
    pending=st.integers(min_value=0, max_value=10**6),
)
def test_known_kind_maps_deterministically(
    kind: str, headline: str, action: str, pending: int
) -> None:
    needs_operator = kind in _NEEDS_OPERATOR
    b = banner_for(
        {
            "kind": kind,
            "headline": headline,
            "action": action,
            "pending_windows": pending,
            "needs_operator": needs_operator,
        }
    )
    assert (b.css_class, b.colour) == _EXPECTED[kind]
    assert b.show_retry_button is needs_operator
    assert b.headline == headline  # verbatim
    assert b.action == action  # verbatim
    assert b.pending_windows == pending


@given(
    kind=st.text().filter(lambda s: s not in _KINDS),
    needs_operator=st.booleans(),
)
def test_unknown_kind_always_raises(kind: str, needs_operator: bool) -> None:
    try:
        banner_for(
            {
                "kind": kind,
                "headline": "h",
                "action": "a",
                "pending_windows": 0,
                "needs_operator": needs_operator,
            }
        )
    except ValueError:
        return
    raise AssertionError(f"expected ValueError for unknown kind {kind!r}")
