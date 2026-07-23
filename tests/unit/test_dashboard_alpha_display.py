"""Display-surface pins for the alpha-vs-buy-&-hold metric.

DEC-ALPHA-BUYHOLD-METRIC-001. Source-string pins (the #1557 honest envelope —
presence of the wiring, not browser execution; the node-DOM strategy-panel
harness executes the setter itself) over the four display sites:

* /screener — the ``metrics.alpha_pct`` column with its explain token;
* the operator rank-1 card — the ``r1_alpha`` hook fed by ``metricText``
  (absent key → "—", never a fabricated 0);
* the strategy story card — the ``stAlpha`` cell in BOTH markup copies
  (strategy.html + v29's drawer — the T-N2 hook-parity fence) + the panel
  setter with the undefined→"—" law;
* bx-explain — the operator-English explain line.
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path("brutex/dashboard/static")
_TEMPLATES = Path("brutex/dashboard/templates")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestScreenerColumn:
    def test_alpha_column_registered_with_explain(self) -> None:
        js = _read(_STATIC / "bx-screener.js")
        assert (
            '{ key: "metrics.alpha_pct", label: "Alpha %", type: "num", explain: "alpha_pct" }'
            in js
        )


class TestOperatorRankOneCard:
    def test_r1_alpha_fed_by_metric_text(self) -> None:
        js = _read(_STATIC / "bx-oneclick.js")
        assert 'setOc("r1_alpha", metricText(m.alpha_pct));' in js

    def test_v29_carries_the_r1_alpha_hook(self) -> None:
        html = _read(_TEMPLATES / "v29.html")
        assert 'data-oc="r1_alpha" data-explain="alpha_pct"' in html


class TestStrategyStoryCard:
    def test_both_markup_copies_carry_st_alpha(self) -> None:
        cell = '<div class="v" id="stAlpha" data-explain="alpha_pct">—</div>'
        assert cell in _read(_TEMPLATES / "strategy.html")
        assert cell in _read(_TEMPLATES / "v29.html")

    def test_panel_setter_renders_dash_on_absent_key(self) -> None:
        js = _read(_STATIC / "bx-strategy-panel.js")
        assert (
            "byId('stAlpha').textContent = m.alpha_pct === undefined ? '—' : String(m.alpha_pct);"
            in js
        )

    def test_panel_registers_st_alpha_in_reset_and_pop(self) -> None:
        js = _read(_STATIC / "bx-strategy-panel.js")
        # setter byId + popRealValues + resetPanelDom dash list — all three.
        assert js.count("'stAlpha'") == 3


class TestExplainLine:
    def test_alpha_pct_explain_present(self) -> None:
        js = _read(_STATIC / "bx-explain.js")
        assert "alpha_pct:" in js
        assert "buy-&-hold" in js
