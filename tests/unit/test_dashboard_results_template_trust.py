"""Static-template pins — /results trust + honesty surfaces (DEC-RESULTS-TRUST-HONESTY-001).

The ``test_dashboard_results_template_era.py`` convention: static-template
substring assertions (no JS engine — the honest envelope: these pin PRESENCE
of the markup/JS, not execution; the route integration suites drive the
payloads). This file owns the PR-B trust pack's template pins: the risk /
rank-axis columns (audit BLOCKER 6) and their honest formatters.
"""

# The template renderer is module-private by design (the era-pin convention).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from brutex.dashboard.app import _render_results_html


def test_results_ships_the_risk_columns() -> None:
    """BLOCKER 6: Calmar / Max DD % / Expectancy — rank axes the dropdown
    already offered — are visible columns, not invisible sort keys."""
    html = _render_results_html()
    assert '["calmar_ratio", "Calmar"' in html
    assert '["max_drawdown_pct", "Max DD %", (r) => pctVal(r.max_drawdown_pct)]' in html
    assert '["expectancy_paisa", "Expectancy (paisa)", (r) => num(r.expectancy_paisa)]' in html


def test_max_dd_pct_is_never_rescaled() -> None:
    """max_drawdown_pct arrives PERCENT-valued (the metrics fold ×100) — the
    cell appends the sign only; the fraction-scaling pct() is not used on it."""
    html = _render_results_html()
    assert "function pctVal(v)" in html
    # pctVal appends "%" without the ×100 the fraction formatter applies.
    assert 'return n.toLocaleString("en-US", { maximumFractionDigits: 2 }) + "%";' in html
    assert "pct(r.max_drawdown_pct)" not in html


def test_risk_columns_sort_numerically() -> None:
    html = _render_results_html()
    assert '"calmar_ratio", "max_drawdown_pct", "expectancy_paisa"' in html


def test_results_ships_the_serve_filter_controls() -> None:
    """B2a: the four serving-side floor inputs exist and are sent only when
    non-blank (blank = the byte-identical legacy request)."""
    html = _render_results_html()
    for input_id in ("fMinTrades", "fMinPf", "fMinDsr", "fMaxDd"):
        assert f'id="{input_id}"' in html
    assert 'params.set("min_trades", fMinTrades.value.trim())' in html
    assert 'params.set("min_pf", fMinPf.value.trim())' in html
    assert 'params.set("min_dsr", fMinDsr.value.trim())' in html
    assert 'params.set("max_abs_dd_pct", fMaxDd.value.trim())' in html
    assert "Min DSR (tuple-N)" in html


def test_hard_gates_recheck_marks_never_hides() -> None:
    """The † marker renders on an exact string compare — MARK, never drop."""
    html = _render_results_html()
    assert 'r.hard_gates_recheck === "marked"' in html
    assert "fails a persisted hard door on re-check" in html
    assert "marked, never dropped" in html
