"""Guards the ``/universe`` O(1) render fixes (DEC-UNIVERSE-RENDER-O1-001).

The operator hit a slow, click-eating list: every row click rebuilt all ~243
instruments, and each rebuild restarted a staggered ``fadeInUp`` entrance
animation (``animationDelay = ix * 0.03s`` → ~7.3 s for 243 rows) whose
not-yet-started rows sit at ``opacity:0`` (invisible, unclickable). Symptoms:
"slow", "click the checkbox multiple times", "again and again loading".

The fix makes the hot paths O(1)-per-interaction (CLAUDE.md §5, applied to the
frontend): the entrance animation plays ONCE per fresh load, row selection swaps
a single CSS class instead of rebuilding the DOM, and select-all / clear patch
the visible checkboxes in place. These substring guards pin the markup + wiring
in CI (the dashboard JS has no unit harness — same established pattern as
``test_dashboard_universe_ux.py``).
"""
# pyright: reportPrivateUsage=none
# (whitebox: these guards render the dashboard's own ``_render_universe_html``.)

from __future__ import annotations

from brutex.dashboard.app import _render_universe_html


def test_entrance_animation_plays_once_per_load() -> None:
    """The entrance animation is gated by ``listAnimated`` so re-renders don't re-cascade."""
    html = _render_universe_html()
    assert "let listAnimated" in html, "the one-shot animation flag is missing"
    # gated: animate only when NOT yet animated; otherwise disable the animation.
    assert "if (listAnimated)" in html, "the animation must be gated on re-render"
    assert 'd.style.animation = "none"' in html, (
        "re-renders must disable the entrance animation (no 7-second cascade)"
    )
    # a fresh instrument load resets the flag so it animates exactly once.
    assert "listAnimated = false" in html, "a fresh load must re-arm the one-shot animation"
    assert "listAnimated = true" in html, "the flag must latch after the first render"


def test_entrance_stagger_is_capped() -> None:
    """The per-row stagger is capped so even a full 243-row load finishes well under a second."""
    html = _render_universe_html()
    assert "Math.min(ix, 12) * 0.03" in html, (
        "the animationDelay stagger must be capped (was ix*0.03 → 7.3s for 243 rows)"
    )


def test_row_selection_is_o1_class_swap_not_full_rebuild() -> None:
    """Selecting a row swaps the ``.sel`` class on just the affected rows — no 243-row rebuild."""
    html = _render_universe_html()
    # rows carry their symbol so selection can target them without rebuilding.
    assert "d.dataset.symbol = i.symbol" in html, "rows must carry data-symbol for O(1) selection"
    # the old selected row is un-selected, the new one selected, via classList — not innerHTML.
    assert '.inst.sel").forEach(n => n.classList.remove("sel"))' in html, (
        "selectInstrument must clear the previous .sel via classList, not a rebuild"
    )
    assert 'n.dataset.symbol === i.symbol) n.classList.add("sel")' in html, (
        "selectInstrument must set the new .sel via classList, not a rebuild"
    )


def test_select_all_and_clear_patch_checkboxes_in_place() -> None:
    """Select-all / clear toggle the visible checkboxes directly — no full re-render."""
    html = _render_universe_html()
    assert '#instruments .inst .msel").forEach(cb => { cb.checked = true; })' in html, (
        "select-all must tick the visible checkboxes in place"
    )
    assert '#instruments .inst .msel").forEach(cb => { cb.checked = false; })' in html, (
        "clear must untick the visible checkboxes in place"
    )
