"""Substring-presence pins for the /movers header chips + ⓘ explainer +
expiry tabs + buildup pills (DEC-MOVERS-HEADER-CHIPS-001).

HONEST ENVELOPE: these pin the PRESENCE of the shipped JS/DOM skeleton (the
fast unit layer), not its execution. The EXECUTED layers are elsewhere and
real: the node goldens in ``test_bx_movers_chips_golden.py`` run the REAL
``bx-movers-chips.js`` + the REAL ``bx-format.js``, and the Chromium browser
truth suite (``tests/browser/test_movers_header_chips.py``) renders the chips
against a real payload, clicks the Coverage badge / PCR chips / expiry tabs,
and asserts the revealed DOM.

What is pinned here:

* the static ``#chips`` / ``#chipDetail`` skeleton (hidden by default) + the
  ``<details id="explain">`` explainer WITHOUT the ``open`` attribute
  (default COLLAPSED);
* the relocated all-exchanges folding phrase (moved from the pcrPanel
  header into the options explainer — the phrase itself stays whole-html
  pinned by ``test_dashboard_movers_template_xss.py``);
* the ONE-move chips invalidation line at the top of ``load()`` beside the
  pinned pcrPanel invalidation;
* the chip renderers are DOM-sink-free (the FULL 4-sink sweep the sibling
  renderPcr pin uses — CodeQL XSS gate);
* the expiry/track TABS write the RETAINED hidden selects, so the
  ``?expiry=``/``?track=`` query tokens are byte-identical;
* the buildup branch is gated on ``BXMoversChips.bxBuildup`` with the strict
  ``=== false`` " *" contiguity gate;
* ``bx-movers-chips.js`` loads synchronously AFTER ``bx-format.js`` (the
  shared-``bxFixed2`` order law) and BEFORE the inline script.
"""

from __future__ import annotations

import re
from pathlib import Path

_DASHBOARD = Path(__file__).resolve().parents[2] / "brutex" / "dashboard"
_MOVERS = _DASHBOARD / "templates" / "movers.html"


def _html() -> str:
    return _MOVERS.read_text(encoding="utf-8")


class TestChipsSkeleton:
    def test_static_skeleton_present_and_hidden(self) -> None:
        html = _html()
        assert '<div class="chiprow" id="chips" hidden></div>' in html
        assert '<div class="note" id="chipDetail" hidden></div>' in html

    def test_explainer_ships_default_collapsed(self) -> None:
        html = _html()
        assert '<details class="note explain" id="explain">' in html
        # Default COLLAPSED: no `open` attribute on the details element.
        details_tag = re.search(r"<details[^>]*id=\"explain\"[^>]*>", html)
        assert details_tag is not None
        assert "open" not in details_tag.group(0)
        assert "ⓘ What am I looking at?" in html
        # One per-tab body each, futures/options hidden by default (spot is
        # the initial tab).
        assert '<div id="explainSpot">' in html
        assert '<div id="explainFutures" hidden>' in html
        assert '<div id="explainOptions" hidden>' in html
        assert "function syncExplain()" in html

    def test_folding_phrase_moved_into_the_options_explainer(self) -> None:
        html = _html()
        start = html.index('<div id="explainOptions" hidden>')
        end = html.index("</details>", start)
        explainer = html[start:end]
        assert "all-exchanges view folds both listings of an underlying into one row" in explainer
        # …and the pcrPanel header is the short detail title now (the jargon
        # line is gone from it).
        assert "<b>PCR detail — per-underlying Put/Call Ratios at the tier expiry</b>" in html
        assert "<b>PCR (put/call) — tier expiry · OI at the as-of instant" not in html

    def test_chips_invalidation_rides_beside_the_pinned_pcr_invalidation(self) -> None:
        html = _html()
        # The pinned pcrPanel invalidation line survives byte-exact…
        pcr_line = '$("pcrPanel").hidden = true; $("pcrRows").textContent = "";'
        chips_line = (
            '$("chips").hidden = true; $("chipDetail").hidden = true; '
            '$("chipDetail").textContent = "";'
        )
        assert pcr_line in html
        assert chips_line in html
        # …and the chips invalidation sits right after it (same ONE-move law).
        assert html.index(pcr_line) < html.index(chips_line)

    def test_render_chips_called_on_both_success_paths(self) -> None:
        # The full-render path AND the skip-render-when-unchanged fast path
        # (load() hides the chips at its top — an identical repeat fetch must
        # restore them). Call sites only (the definition is excluded).
        html = _html()
        call_sites = re.findall(r"(?<!function )renderChips\(j\)", html)
        assert len(call_sites) >= 2, f"call sites found: {len(call_sites)}"


class TestChipRenderersAreSinkFree:
    def test_chip_functions_never_touch_a_dom_string_sink(self) -> None:
        # The FULL 4-sink sweep over the makeChip → renderChips block (the
        # renderPcr pin's law): payload values reach the DOM via
        # createElement / textContent / the title DOM property ONLY.
        html = _html()
        match = re.search(
            r"function makeChip\(label, value, opts\).*?function renderChips\(j\).*?\n}",
            html,
            re.DOTALL,
        )
        assert match is not None, "makeChip/renderMissingOi/renderChips must be defined"
        source = match.group(0)
        for sink in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write"):
            assert sink not in source, f"forbidden DOM sink {sink!r} in the chip renderers"
        assert "textContent" in source
        assert "document.createElement" in source

    def test_missing_oi_reveal_disclosures_ship(self) -> None:
        html = _html()
        # The unread-legs honest tail + the capped-names "+K more" wording.
        assert (
            "leg(s) unread (no session bar / read error) — counted in the total, not named." in html
        )
        assert "more not listed" in html
        # The chip labels the operator asked for.
        assert "Put/Call Ratio (OI)" in html
        assert "Put/Call Ratio (Vol)" in html
        assert '"Coverage"' in html
        # The ratio-of-sums honesty is stated on the chip title.
        assert "ratio of sums" in html


class TestExpiryTabs:
    def test_tabs_write_the_retained_hidden_selects(self) -> None:
        html = _html()
        # The selects STAY in the DOM, permanently display:none…
        assert (
            '<select id="track" title="Expiry cadence (Options)" style="display:none;"></select>'
            in html
        )
        assert '<select id="expiry" title="Expiry" style="display:none;"></select>' in html
        # …renderPills no longer unhides them (the tabs are the visible control)…
        assert '$("expiry").style.display' not in html
        assert '$("track").style.display' not in html
        assert '$("expiryTabs").hidden = !derivAsset;' in html
        # …and the delegated tab click WRITES the select then loads.
        assert "$(SEG_SELECT[boxId]).value = btn.dataset.val;" in html
        assert "function initSegTabs()" in html
        assert "function syncSegTabs()" in html

    def test_query_param_semantics_are_byte_identical(self) -> None:
        html = _html()
        # load() still reads the selects with the same fallbacks + tokens.
        assert 'if (state.asset!=="spot") p.set("expiry", $("expiry").value||"Near");' in html
        assert 'if (state.asset==="options") p.set("track", $("track").value||"All");' in html
        # The select population tokens are unchanged (Near/Next/Far · All/Monthly/Weekly).
        # Fold note (2026-07-22 PR #1358 hand-port): the consolidated body's populate
        # carries the body's own TIER_TIP/TRACK_TIP option tooltips — the OPTION VALUE
        # tokens (what the tabs write and load() reads) are byte-identical.
        assert '["Near","Next","Far"].map(e=>`<option title="${TIER_TIP[e]}">${e}</option>`)' in html
        assert '["All","Monthly","Weekly"].map(e=>`<option title="${TRACK_TIP[e]}">${e}</option>`)' in html

    def test_provenance_reads_of_the_selects_survive(self) -> None:
        html = _html()
        assert 'const tier = ($("expiry").value || "Near").toLowerCase();' in html


class TestBuildupPills:
    def test_buildup_branch_rides_the_pure_module_and_the_strict_star_gate(self) -> None:
        html = _html()
        assert 'if (type === "buildup") {' in html
        assert "BXMoversChips.bxBuildup(" in html
        # Options uses the raw oi_chg int; futures the sign-faithful oi_chg_pct.
        assert 'typeof row.oi_chg === "number" ? row.oi_chg : null' in html
        assert "numify(row.oi_chg_pct)" in html
        # The strict `=== false` " *" gate (an abstained null contiguity NEVER
        # marks) + the value rides the same esc() as every cell.
        assert 'if (b && row.oi_baseline_contiguous === false) bv += " *";' in html
        assert "${esc(bv)}" in html

    def test_buildup_columns_registered_for_futures_and_options(self) -> None:
        html = _html()
        assert html.count('["Buildup","buildup","buildup"]') == 2


class TestModuleInclude:
    def test_chips_module_loads_non_defer_after_bx_format(self) -> None:
        html = _html()
        fmt = html.index('<script src="/static/bx-format.js"></script>')
        chips = html.index('<script src="/static/bx-movers-chips.js"></script>')
        inline = html.index("<script>\n")
        assert fmt < chips < inline, (
            "bx-movers-chips.js must load (non-defer) AFTER bx-format.js (it "
            "resolves the shared bxFixed2 global) and BEFORE the inline script"
        )


class TestLoadSupersedeGuard:
    """The loadGen re-entrancy guard (round 2 — the executed S1 repro).

    A STALLED response for a superseded coordinate must never repaint a newer
    paint: pre-guard, a stalled ``expiry=Next`` response landing after a fast
    Far paint rendered next-tier rows under the Far selection with the Expiry
    chip cross-labeled "25-Jul-2024 (far tier)". The EXECUTED regression pin
    is the Chromium probe ``tests/browser/test_refuter_r2_chips_probes.py``
    (S1); these pin the guard's presence at every await resume point.
    """

    def test_load_generation_declared_and_taken(self) -> None:
        html = _html()
        assert "let loadGen = 0;" in html
        assert "const gen = ++loadGen;" in html

    def test_every_await_resume_point_rechecks_generation(self) -> None:
        html = _html()
        # One check per await in load(): the fetch, the 5xx-detail body read,
        # the success body read — plus the catch arm (a superseded failure
        # must not clobber a newer paint with the preview/error shell).
        assert html.count("if (gen !== loadGen) return;") == 4
        fetch_at = html.index('const r = await fetch("/api/v1/movers?"')
        first_check = html.index("if (gen !== loadGen) return;")
        assert fetch_at < first_check, "the first re-check must follow the fetch await"
        body_read = html.index("const bodyText = await r.text();")
        assert html.index("if (gen !== loadGen) return;", body_read) - body_read < 120, (
            "the success body read must re-check the generation before JSON.parse/paint"
        )
