"""Node-executed DOM-RENDER goldens for the resilience surfaces (FIX 3).

The sibling ``bxResilienceCells`` / screener ``cellText`` builders are already
execution-tested (``test_dashboard_resilience_panel.py`` /
``test_dashboard_screener_math.py``); the ONE remaining unexecuted link was the
template ``textContent`` render law itself. These tests close it HONESTLY: the
REAL inline template ``<script>`` is loaded under node against a minimal DOM
shim (no jsdom in the toolchain) and the actual render functions run —

* ``/strategy``: the panel's ``fillResilience(row)`` fills the real
  ``id="stRes*"`` cells; a NULL/absent record renders every cell "—", a LIVE
  record renders the locked labels/values INCLUDING the money path (through the
  page's real ``bx-explain`` paisa→₹ converter) and the coloured underwater
  badge class;
* ``/screener``: the table's ``render()`` builds a ``<tr>``/``<td>`` DOM through
  ``document.createElement`` + ``textContent`` — a null-resilience row renders
  its resilience cells "—", a live row renders the money-formatted worst-streak
  cell.

The DOM shim supports only the exact subset both templates touch
(createElement / getElementById / appendChild / textContent / setAttribute);
``document.readyState='loading'`` so the templates' ``init()`` never fires (the
private render functions are returned instead). Large fixtures ride a temp file
(the surfacing harness's MAX_ARG_STRLEN pattern).
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "brutex" / "dashboard" / "static"
_TEMPLATES = _ROOT / "brutex" / "dashboard" / "templates"
_ONECLICK_JS = _STATIC / "bx-oneclick.js"
_EXPLAIN_JS = _STATIC / "bx-explain.js"
_SCREENER_JS = _STATIC / "bx-screener.js"
_STRATEGY_HTML = _TEMPLATES / "strategy.html"
_SCREENER_HTML = _TEMPLATES / "screener.html"

_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="node not installed")

_UNAVAILABLE = "Resilience stats unavailable (re-simulation parity mismatch or no trades)."

#: The all-None NULL resilience block (``resilience_payload(None)`` shape).
_NULL_RESILIENCE: dict[str, Any] = {
    "stats_live": False,
    "n_trades": None,
    "losing_streak_p50": None,
    "losing_streak_p95": None,
    "losing_streak_count": None,
    "worst_streak_pnl_paisa": None,
    "worst_streak_pnl_pct": None,
    "worst_streak_len": None,
    "worst_streak_start": None,
    "worst_streak_end": None,
    "longest_underwater_trades": None,
    "longest_underwater_days": None,
    "still_underwater_at_end": None,
    "worst_loss_month": None,
    "worst_loss_month_count": None,
    "worst_loss_week": None,
    "worst_loss_week_count": None,
    "win_rate_by_year": {},
    "win_rate_year_spread": None,
    "losses_by_month": {},
    "fingerprint": None,
}

#: A fully-populated LIVE resilience block (worst streak cost -₹1250.00).
_LIVE_RESILIENCE: dict[str, Any] = {
    "stats_live": True,
    "n_trades": 50,
    "losing_streak_p50": 2,
    "losing_streak_p95": 4,
    "losing_streak_count": 8,
    "worst_streak_pnl_paisa": -125000,
    "worst_streak_pnl_pct": -12.5,
    "worst_streak_len": 4,
    "worst_streak_start": "2024-03-01T04:15:00+00:00",
    "worst_streak_end": "2024-03-05T09:20:00+00:00",
    "longest_underwater_trades": 7,
    "longest_underwater_days": 30,
    "still_underwater_at_end": False,
    "worst_loss_month": "2024-03",
    "worst_loss_month_count": 6,
    "worst_loss_week": "2024-W10",
    "worst_loss_week_count": 3,
    "win_rate_by_year": {"2024": [30, 50]},
    "win_rate_year_spread": 0.15,
    "losses_by_month": {"2024-03": 6},
    "fingerprint": "fp",
}


def _live_row() -> dict[str, Any]:
    return {"rank": 1, "metrics": {"max_consecutive_losses": 5}, "resilience": _LIVE_RESILIENCE}


def _null_row() -> dict[str, Any]:
    return {"rank": 1, "metrics": {"max_consecutive_losses": 0}, "resilience": _NULL_RESILIENCE}


#: The node driver — loads the REAL template inline script against a minimal DOM
#: shim and runs the actual render function. Paths are injected verbatim as JSON
#: string literals; the fixture rides ``argfile`` (temp file).
_DRIVER = r"""
const fs = require('fs');
const OC = require(%(oneclick)s);
const EX = require(%(explain)s);
const SC = require(%(screener)s);
const ARGS = JSON.parse(fs.readFileSync(%(argfile)s, 'utf8'));
const KIND = ARGS.kind;
const ROW = ARGS.row;

function mkNode(tag) {
  const node = {
    tagName: tag, children: [], attrs: {}, className: '', hidden: false, value: '',
    style: {}, _text: '',
  };
  Object.defineProperty(node, 'firstChild', {
    get() { return this.children.length ? this.children[0] : null; },
  });
  node.appendChild = function (c) { this.children.push(c); return c; };
  node.removeChild = function (c) {
    const i = this.children.indexOf(c); if (i >= 0) { this.children.splice(i, 1); } return c;
  };
  node.setAttribute = function (k, v) { this.attrs[k] = String(v); };
  node.getAttribute = function (k) {
    return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
  };
  node.addEventListener = function () {};
  Object.defineProperty(node, 'textContent', {
    get() { return this._text; },
    set(t) { this._text = String(t); this.children = []; },
  });
  return node;
}

function makeDom() {
  const registry = {};
  const document = {
    readyState: 'loading',
    body: mkNode('body'),
    createElement: (t) => mkNode(t),
    createElementNS: (_ns, t) => mkNode(t),
    getElementById: (id) => (registry[id] || (registry[id] = mkNode('div'))),
    addEventListener: () => {},
    querySelectorAll: () => [],
  };
  return { registry, document };
}

// bx-oneclick.js exports the bx-prefixed PURE builders; the browser wires them
// under window.BXOneclick.* — mirror the exact names the templates read.
const BXOneclick = {
  resilienceCells: OC.bxResilienceCells,
  resilienceSelfLocate: OC.bxResilienceSelfLocate,
  resilienceGuide: OC.bxResilienceGuide,
  vixBandText: OC.bxVixBandText,
  vixBandCellText: OC.bxVixBandCellText,
  istDateTime: OC.bxIstDateTime,
  RESILIENCE_UNAVAILABLE: OC.BX_RESILIENCE_UNAVAILABLE,
  VIX_BAND_NOT_STAMPED: OC.BX_VIX_BAND_NOT_STAMPED,
};

function loadInline(file, marker, exportsExpr) {
  const html = fs.readFileSync(file, 'utf8');
  const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  const body0 = blocks.find((b) => b.indexOf(marker) !== -1);
  if (!body0) { throw new Error('inline <script> not found for marker ' + marker); }
  const head = '(function () {';
  let body = body0.slice(body0.indexOf(head) + head.length);
  body = body.slice(0, body.lastIndexOf('})();'));
  const dom = makeDom();
  const win = { BXOneclick, BXExplain: EX, BXScreener: SC, location: { search: '' } };
  const factory = new Function('document', 'window', body + '\n;return (' + exportsExpr + ');');
  return { api: factory(dom.document, win), dom };
}

let OUT;
if (KIND === 'strategy') {
  const { api, dom } = loadInline(
    %(strategy)s, 'function fillResilience', '{ fillResilience: fillResilience }'
  );
  api.fillResilience(ROW);
  const g = (id) => (dom.registry[id] ? dom.registry[id].textContent : null);
  const badge = dom.registry['stResUnderwaterBadge'];
  OUT = {
    worstStreak: g('stResWorstStreak'),
    worstStreakSub: g('stResWorstStreakSub'),
    cost: g('stResCost'),
    costSub: g('stResCostSub'),
    underwater: g('stResUnderwater'),
    underwaterBadge: g('stResUnderwaterBadge'),
    badgeClass: badge ? badge.className : null,
    worstMonth: g('stResWorstMonth'),
    worstMonthSub: g('stResWorstMonthSub'),
    winSpread: g('stResWinSpread'),
    guide: g('stResGuide'),
  };
} else {
  const { api, dom } = loadInline(
    %(screener_html)s, 'function cellText', '{ render: render, state: state }'
  );
  api.state.rows = [ROW];
  api.state.rowCount = 1;
  api.render();
  const tbody = dom.registry['scRows'];
  const cells = {};
  let rendered = false;
  if (tbody && tbody.children.length) {
    rendered = true;
    for (const td of tbody.children[0].children) {
      cells[td.getAttribute('data-explain')] = td.textContent;
    }
  }
  OUT = {
    cells,
    rendered,
    count: dom.registry['scCount'] ? dom.registry['scCount'].textContent : null,
  };
}
console.log(JSON.stringify(OUT));
"""


def _render(kind: str, row: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    argfile = tmp_path / "arg.json"
    argfile.write_text(json.dumps({"kind": kind, "row": row}), encoding="utf-8")
    script = _DRIVER % {
        "oneclick": json.dumps(str(_ONECLICK_JS)),
        "explain": json.dumps(str(_EXPLAIN_JS)),
        "screener": json.dumps(str(_SCREENER_JS)),
        "strategy": json.dumps(str(_STRATEGY_HTML)),
        "screener_html": json.dumps(str(_SCREENER_HTML)),
        "argfile": json.dumps(str(argfile)),
    }
    proc = subprocess.run(  # nosec B603 — fixed argv, test-only
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)  # type: ignore[no-any-return]


class TestStrategyPanelDomRender:
    """The REAL /strategy ``fillResilience`` fills the panel cells via textContent."""

    def test_live_row_renders_locked_labels_and_money_path(self, tmp_path: Path) -> None:
        out = _render("strategy", _live_row(), tmp_path)
        assert out["worstStreak"] == "5 in a row"
        assert out["worstStreakSub"] == "typical 2 · P95 4"
        # The money path runs through the page's REAL bx-explain paisa→₹ converter.
        assert out["cost"] == "−₹1250.00 (-12.5%)"
        assert out["costSub"] == "4 losses · 2024-03-01 09:45 IST → 2024-03-05 14:50 IST"
        assert out["underwater"] == "7 trades · 30 days"
        assert out["underwaterBadge"] == "● recovered"
        assert "badge-ok" in out["badgeClass"]  # coloured status (FIX 5)
        assert out["worstMonth"] == "2024-03 (6 losses)"
        assert out["winSpread"] == "swing 0.15"

    def test_null_row_renders_every_cell_as_dash(self, tmp_path: Path) -> None:
        out = _render("strategy", _null_row(), tmp_path)
        for cell in (
            "worstStreak",
            "worstStreakSub",
            "cost",
            "costSub",
            "underwater",
            "worstMonth",
            "winSpread",
        ):
            assert out[cell] == "—", cell
        # Sub-lines with no companion value stay EMPTY (never a fabricated "—0").
        assert out["worstMonthSub"] == ""
        assert out["guide"] == _UNAVAILABLE  # honest unavailable copy, never a 0

    def test_still_underwater_renders_red_badge_class(self, tmp_path: Path) -> None:
        row = _live_row()
        row["resilience"] = {**_LIVE_RESILIENCE, "still_underwater_at_end": True}
        out = _render("strategy", row, tmp_path)
        assert out["underwaterBadge"] == "STILL UNDERWATER at backtest end"
        assert "badge-bad" in out["badgeClass"]  # loud coloured truth state (FIX 5)


class TestScreenerCellDomRender:
    """The REAL /screener ``render()`` builds the resilience ``<td>`` via textContent."""

    def test_null_resilience_row_renders_dashes(self, tmp_path: Path) -> None:
        out = _render("screener", _null_row(), tmp_path)
        assert out["rendered"] is True
        cells = out["cells"]
        # Every resilience.* column abstains to "—" (never a fabricated 0).
        assert cells["worst_streak_pnl"] == "—"
        assert cells["losing_streak_p95"] == "—"
        assert cells["longest_underwater"] == "—"
        assert cells["worst_loss_month"] == "—"
        assert cells["win_rate_year_spread"] == "—"

    def test_live_resilience_row_renders_money_and_values(self, tmp_path: Path) -> None:
        out = _render("screener", _live_row(), tmp_path)
        assert out["rendered"] is True
        cells = out["cells"]
        # The money column runs through the shipped money() → bx-explain path.
        assert cells["worst_streak_pnl"] == "−₹1250.00"
        assert cells["losing_streak_p95"] == "4"
        assert cells["longest_underwater"] == "30"
        assert cells["win_rate_year_spread"] == "0.15"
        # The MAX cold streak reads from metrics (no rival resilience column).
        assert cells["max_consecutive_losses"] == "5"
