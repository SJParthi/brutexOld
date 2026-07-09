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
