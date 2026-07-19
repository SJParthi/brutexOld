"""Frontend pins for DEC-OPERATOR-HEALTH-TRUTH-001 (MIGRATION.md #1747).

* the v29 heartbeat pull slot's unconditional green "last pull completed
  cleanly" on ``flow_state === 'done'`` is KILLED — the slot now joins the
  additive ``last_pull`` outcome block (and the formerly-swallowed ``failing``
  / ``local`` flow states render honestly instead of "idle");
* the health-hub widget rides ONE ``BXPoll.createPoller`` (the #1720
  one-poller-per-endpoint law — no bare ``setInterval``), fans out via the
  house ``__bxHealthHubSubs`` arrays, and writes DOM via ``textContent`` only;
* node-EXECUTED goldens for the widget's pure helpers (verdict → dot state,
  lane labels, age text, fold text).

HONEST envelope (the house #1529/#1561/#1720 rule): source-string pins +
node-executed pure helpers — the stateful widget is browser-only and is NOT
executed here (no headless JS runner in CI); ``node --check`` syntax gates for
bx-health-hub.js and the v29 inline scripts ride the existing
``test_dashboard_frontend_latency.py`` glob/extractor tests.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from pathlib import Path

import pytest

_DASHBOARD = Path(__file__).resolve().parents[2] / "brutex" / "dashboard"
_TEMPLATES = _DASHBOARD / "templates"
_STATIC = _DASHBOARD / "static"

_NODE = shutil.which("node")


def _v29() -> str:
    text = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
    assert text
    return text


def _widget() -> str:
    text = (_STATIC / "bx-health-hub.js").read_text(encoding="utf-8")
    assert text
    return text


class TestHeartbeatPullSlotHonesty:
    """Part 1 — the operator page's 'done' render joins the honest outcome."""

    def test_unconditional_clean_green_token_is_killed(self) -> None:
        html = _v29()
        assert "setSlot('pull', 'ok', 'pull finished', 'last pull completed cleanly');" not in html

    def test_done_render_joins_the_last_pull_outcome_block(self) -> None:
        html = _v29()
        assert "d.last_pull" in html
        # green ONLY on the classified clean outcome…
        assert "lp.outcome === 'clean'" in html
        assert "'pull finished — completed cleanly'" in html
        # …and the degraded outcomes render their backend headline verbatim.
        assert "lp.tone === 'warn' || lp.tone === 'bad'" in html

    def test_done_without_outcome_evidence_reads_neutral_not_green(self) -> None:
        html = _v29()
        assert "'outcome unknown — no classified verdict recorded'" in html

    def test_failing_and_local_states_no_longer_fall_through_to_idle(self) -> None:
        html = _v29()
        assert "state === 'failing'" in html
        assert "'pull active — calls FAILING'" in html
        assert "state === 'local'" in html
        assert "'pull active — local processing'" in html

    def test_idle_render_keeps_a_degraded_last_outcome_visible(self) -> None:
        html = _v29()
        assert "'no pull running now'" in html


class TestHealthHubWiring:
    """Part 2 — the panel + the ONE BXPoll-disciplined widget poller."""

    def test_v29_ships_the_panel_and_the_widget_include(self) -> None:
        html = _v29()
        assert 'id="bxHealthHub"' in html
        assert 'id="bxHealthHubBody"' in html
        assert 'id="bxHealthFold"' in html
        assert 'id="bxHealthUpdated"' in html
        assert '<script defer src="/static/bx-health-hub.js"></script>' in html

    def test_widget_loads_after_the_shared_poll_core(self) -> None:
        """defer preserves document order — bx-poll.js must come first."""
        html = _v29()
        poll_at = html.index('src="/static/bx-poll.js"')
        hub_at = html.index('src="/static/bx-health-hub.js"')
        assert poll_at < hub_at

    def test_widget_rides_one_bxpoll_poller_never_a_bare_interval(self) -> None:
        js = _widget()
        assert "BXPoll.createPoller({" in js
        assert 'url: "/api/v1/health/hub"' in js
        assert "setInterval(" not in js  # the #1720 law — no bare interval
        # No un-disciplined fallback poller when the core is missing — an
        # honest degraded note instead.
        assert "shared poll core (bx-poll.js) did not load" in js

    def test_widget_ships_the_house_fanout_arrays(self) -> None:
        js = _widget()
        assert "__bxHealthHubSubs" in js
        assert "__bxHealthHubFailSubs" in js
        assert "subscriber isolation" in js

    def test_widget_dom_writes_are_textcontent_only(self) -> None:
        js = _widget()
        assert not re.search(r"\.innerHTML\s*\+?=", js), (
            "bx-health-hub.js must never assign innerHTML — textContent/createElement only"
        )
        assert ".textContent" in js
        assert "document.createElement" in js

    def test_v29_panel_markup_adds_no_innerhtml_sink(self) -> None:
        html = _v29()
        # The health-hub additions introduce no new innerHTML assignment; the
        # template-wide sink budget is pinned by test_dashboard_js_perf_frontend.
        section = html[html.index('id="bxHealthHub"') : html.index('id="bxHealthHubBody"')]
        assert "innerHTML" not in section


@pytest.mark.skipif(_NODE is None, reason="node not installed")
class TestWidgetPureHelpersNodeGoldens:
    """Node-EXECUTED goldens over the CommonJS export (real code, not a copy)."""

    def _run(self, script: str) -> str:
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            [str(_NODE), "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            cwd=str(_STATIC),
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    def test_dot_state_golden(self) -> None:
        out = self._run(
            'const h = require("./bx-health-hub.js");'
            'console.log(JSON.stringify(["ok","warn","bad","unknown","idle","not_wired","junk"]'
            ".map(h.dotState)));"
        )
        assert json.loads(out) == ["ok", "warn", "down", "warn", "", "", ""]

    def test_lane_label_golden(self) -> None:
        out = self._run(
            'const h = require("./bx-health-hub.js");'
            'console.log(JSON.stringify(["pull","nightly","backup_deadman","doctor_daily",'
            '"mutation","future_lane"].map(h.laneLabel)));'
        )
        assert json.loads(out) == [
            "Pull outcome",
            "Nightly chain",
            "Backup dead-man",
            "Doctor daily",
            "Mutation gate",
            "future_lane",  # an unknown lane passes through — never hidden
        ]

    def test_badge_text_golden(self) -> None:
        out = self._run(
            'const h = require("./bx-health-hub.js");'
            'console.log(JSON.stringify(["not_wired","ok","bad"].map(h.badgeText)));'
        )
        assert json.loads(out) == ["not wired", "ok", "bad"]

    def test_age_text_golden(self) -> None:
        out = self._run(
            'const h = require("./bx-health-hub.js");'
            "console.log(JSON.stringify([null, -1, 5, 300, 7200, 200000].map(h.ageText)));"
        )
        assert json.loads(out) == ["", "", "5s ago", "5m ago", "2.0h ago", "2.3d ago"]

    def test_fold_text_golden(self) -> None:
        out = self._run(
            'const h = require("./bx-health-hub.js");'
            "console.log(JSON.stringify(["
            "h.foldText(null),"
            'h.foldText({verdict: "ok", headline: "3 ok · 2 not wired"}),'
            'h.foldText({verdict: "warn", headline: ""}),'
            'h.foldText({verdict: "bad", headline: "1 bad"})'
            "]));"
        )
        assert json.loads(out) == ["—", "OK — 3 ok · 2 not wired", "AMBER", "RED — 1 bad"]
