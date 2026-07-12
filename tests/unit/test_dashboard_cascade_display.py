"""Frontend pins for the cascade-verdict display (DEC-RESULTS-CASCADE-VERDICT-JOIN-001).

Source-string pins + node syntax gates (the house #1529/#1557/#1561 envelope
— presence, not browser execution): the /screener Cascade column, the
operator ``r1_cascade`` badge + the ``oc-cascade-fail`` red-flag toggle (a
cascade-FAILED rank-1 can never read silently green), the /strategy identity
+ deep-stats cells, the /results badge/columns/meta line, the BX_EXPLAIN
lines, and the killed-token law that NO client composes a "PASS" default —
every verdict text is SERVER-built (``oneclick_view.cascade_status_text``).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

# The ONE house inline-<script> extractor (stdlib HTMLParser — CodeQL
# py/bad-tag-filter hardening, alert 1011; §11 COMMON: imported from the
# frontend-latency suite that owns it, never a second copy).
from tests.unit.test_dashboard_frontend_latency import _inline_script_bodies

_DASH = Path(__file__).resolve().parents[2] / "brutex" / "dashboard"
_STATIC = _DASH / "static"
_TEMPLATES = _DASH / "templates"

_SCREENER = (_STATIC / "bx-screener.js").read_text(encoding="utf-8")
_ONECLICK = (_STATIC / "bx-oneclick.js").read_text(encoding="utf-8")
_EXPLAIN = (_STATIC / "bx-explain.js").read_text(encoding="utf-8")
_RESULTS = (_TEMPLATES / "results.html").read_text(encoding="utf-8")
_STRATEGY = (_TEMPLATES / "strategy.html").read_text(encoding="utf-8")
_V29 = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")


class TestScreenerColumn:
    def test_cascade_column_present(self) -> None:
        assert 'key: "cascade.status_text"' in _SCREENER
        assert 'label: "Cascade"' in _SCREENER
        assert 'explain: "cascade_status"' in _SCREENER

    def test_explain_lines_exist(self) -> None:
        assert "cascade_status:" in _EXPLAIN
        # The reworded CPCV provenance (measured at cascade filter #3).
        assert "cascade filter #3" in _EXPLAIN


class TestOperatorBadge:
    def test_r1_cascade_fill_and_red_flag_toggle(self) -> None:
        assert 'setOc("r1_cascade"' in _ONECLICK
        assert 'setOcClass("r1_cascade", "oc-cascade-fail"' in _ONECLICK
        # The toggle keys on the RAW status token, never on display text.
        assert 'casc.status === "fail"' in _ONECLICK

    def test_v29_hosts_and_style(self) -> None:
        assert _V29.count('data-oc="r1_cascade"') >= 2
        assert ".oc-cascade-fail" in _V29


class TestStrategyPage:
    def test_identity_and_deep_cells(self) -> None:
        assert 'id="stCascade"' in _STRATEGY
        assert 'id="stCascadeDeep"' in _STRATEGY
        assert "cascade-fail" in _STRATEGY

    def test_fill_reads_server_text(self) -> None:
        assert "casc.status_text" in _STRATEGY
        assert "classList.toggle('cascade-fail'" in _STRATEGY


class TestResultsPage:
    def test_columns_and_badge(self) -> None:
        assert '["cascade_status", "Cascade"' in _RESULTS
        assert '["cpcv_pass_pct", "CPCV %"' in _RESULTS
        assert '["composite_score", "Composite"' in _RESULTS
        assert "cascadeBadge" in _RESULTS
        assert ".cascbadge.fail" in _RESULTS

    def test_meta_law_and_flag_chip(self) -> None:
        # Client law: meta joined + row lacks cascade_status ⇒ "not validated";
        # any other/absent state ⇒ "—" — plus the flagged-rows header chip.
        assert 'CASC_META && CASC_META.state === "joined"' in _RESULTS
        assert "cascade-FAILED row(s) flagged" in _RESULTS
        assert "board.cascade_join" in _RESULTS

    def test_null_counts_render_question_marks(self) -> None:
        # A failed load's null counts read "?" — never a fabricated 0.
        assert 'CASC_META.n_input == null ? "?"' in _RESULTS


class TestNoClientComposedVerdict:
    """The killed-token law: verdict text is SERVER-built, never JS prose."""

    @pytest.mark.parametrize(
        ("name", "body"),
        [
            ("bx-screener.js", _SCREENER),
            ("bx-oneclick.js", _ONECLICK),
            ("results.html", _RESULTS),
            ("strategy.html", _STRATEGY),
            ("v29.html", _V29),
        ],
    )
    def test_no_pass_literal_composed(self, name: str, body: str) -> None:
        # A bare "PASS" / 'PASS' JS string literal would be a client-composed
        # verdict — the ONE builder lives in oneclick_view.cascade_status_text.
        assert not re.search(r"""["']PASS["']""", body), name

    def test_the_one_builder_is_server_side(self) -> None:
        view = (
            Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "oneclick_view.py"
        ).read_text(encoding="utf-8")
        assert "def cascade_status_text" in view
        assert '"PASS"' in view
        assert '"PASS (provisional)"' in view


class TestNodeSyntax:
    @pytest.mark.parametrize("name", ["bx-screener.js", "bx-oneclick.js", "bx-explain.js"])
    def test_touched_js_parses(self, name: str, tmp_path: Path) -> None:
        node = shutil.which("node")
        if node is None:
            pytest.skip("node not installed locally (CI installs it)")
        subprocess.run([node, "--check", str(_STATIC / name)], check=True, capture_output=True)

    @pytest.mark.parametrize("name", ["results.html", "strategy.html", "v29.html"])
    def test_touched_template_inline_scripts_parse(self, name: str, tmp_path: Path) -> None:
        node = shutil.which("node")
        if node is None:
            pytest.skip("node not installed locally (CI installs it)")
        body = (_TEMPLATES / name).read_text(encoding="utf-8")
        blocks = _inline_script_bodies(body)
        assert blocks, f"{name} carries no inline scripts?"
        for index, block in enumerate(blocks):
            target = tmp_path / f"{name}.{index}.js"
            target.write_text(block, encoding="utf-8")
            proc = subprocess.run(
                [node, "--check", str(target)], capture_output=True, text=True, check=False
            )
            assert proc.returncode == 0, f"{name} inline script {index}: {proc.stderr}"


class TestInlineScriptExtractorTeeth:
    """CodeQL py/bad-tag-filter hardening (code-scanning alert 1011).

    The retired extractor regex
    ``<script(?![^>]*\\bsrc=)[^>]*>(.*?)</script>`` spelled BOTH tags
    lowercase-exact, so (a) an upper/mixed-case ``<SCRIPT>`` block was
    silently MISSED — its inline script escaped the ``node --check`` gate
    entirely — and (b) a variant-spelled end tag (``</script >``,
    ``</SCRIPT\\t>``) made the non-greedy capture run on to the NEXT real
    ``</script>``, swallowing intervening MARKUP into the "script body"
    (the db940c42 / alert-1010 evasion class one suite over). The scan now
    rides the ONE house extractor — ``_inline_script_bodies`` over the
    stdlib ``html.parser.HTMLParser`` (the frontend-latency suite's
    NIT-1 precedent; ``tests.unit.test_dashboard_frontend_latency``, §11
    COMMON — imported, never a second copy): real tag grammar incl. the
    script-element CDATA content mode, every browser-honoured tag
    spelling. TEST-side reader of our own trusted templates, never a
    sanitizer. The variant matrix below pins exactly the spellings CodeQL
    names (case + end-tag whitespace) on the stdlib-version-STABLE set
    (the divergent ``</script foo>`` / ``</script/>`` arms are pinned
    with a tolerant law in the db940c42 suite, not re-pinned here)."""

    @pytest.mark.parametrize(
        "specimen",
        [
            "<script>var a = 1;</script>",
            "<SCRIPT>var a = 1;</SCRIPT>",
            "<ScRiPt>var a = 1;</sCrIpT>",
            "<script>var a = 1;</script >",
            "<script>var a = 1;</script\t\n>",
            "<SCRIPT type='text/javascript'>var a = 1;</Script >",
        ],
    )
    def test_case_and_end_tag_variants_are_extracted(self, specimen: str) -> None:
        # The retired lowercase-exact regex returned [] for every non-
        # lowercase spelling here — the block escaped the node gate.
        assert _inline_script_bodies(specimen) == ["var a = 1;"], specimen

    @pytest.mark.parametrize(
        "specimen",
        [
            '<script src="/static/x.js"></script>',
            '<SCRIPT SRC="/static/x.js"></SCRIPT>',
            "<script\tSrc='/static/x.js'></script >",
        ],
    )
    def test_src_carrying_scripts_are_excluded_any_case(self, specimen: str) -> None:
        # HTMLParser lowercases attribute names, so SRC=/Src= are excluded
        # too (the retired regex's case-sensitive lookahead admitted them).
        assert _inline_script_bodies(specimen) == [], specimen

    def test_variant_end_tag_never_swallows_following_markup(self) -> None:
        # The alert-1010 evasion shape: with the retired regex, the
        # variant-spelled end tag made `(.*?)</script>` capture straight
        # through the <div> markup into the NEXT block's end tag.
        specimen = '<script>var a = 1;</script ><div id="k">markup</div><script>var b = 2;</script>'
        assert _inline_script_bodies(specimen) == ["var a = 1;", "var b = 2;"]
