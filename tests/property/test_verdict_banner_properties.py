"""Property tests for `brutex.dashboard.verdict_banner` (DEC-VERDICT-BANNER-001).

Hypothesis invariants over arbitrary verdict dicts:

* **kind ↔ colour bijection** — each of the four contract kinds always maps to
  its one fixed ``(css_class, colour)`` and back, for ANY headline / action /
  pending count;
* **retry button mirrors needs_operator** — ``show_retry_button`` is exactly the
  verdict's own ``needs_operator`` flag (the COMMON pass-through, never re-derived);
* **verbatim pass-through** — ``headline`` / ``action`` are copied unchanged and
  ``pending_windows`` round-trips through ``int(...)``;
* **fail-closed** — any kind outside the four ALWAYS raises ``ValueError``.

The helper is pure (no Rust wheel, no lake), so this runs on a bare checkout.
"""

from __future__ import annotations

from brutex.dashboard.verdict_banner import banner_for
from hypothesis import given
from hypothesis import strategies as st

_KINDS = ("auth_expired", "transient_residual", "known_gap_only", "clean")
_EXPECTED = {
    "auth_expired": ("verdict--red", "red"),
    "transient_residual": ("verdict--amber", "amber"),
    "known_gap_only": ("verdict--grey", "grey"),
    "clean": ("verdict--green", "green"),
}
_NEEDS_OPERATOR = {"auth_expired", "transient_residual"}


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
