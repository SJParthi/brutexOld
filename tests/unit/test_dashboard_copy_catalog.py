"""Node-executed goldens for the plain-English copy catalog ``bx-copy.js``
(DEC-OPERATOR-PAGE-CLEANVIEW-001).

The catalog is the ONE render-time remap layer between the frozen
``/api/v1/*`` payload strings and the operator's screen (Law 1: the wire is
frozen — every rewording happens client-side; Law 2: honesty is auditable —
a remapped string keeps the verbatim server string in ``title``, an unknown
string passes through VERBATIM, never suppressed). This suite:

* byte-pins EVERY catalog entry (token maps + pattern rules, one
  representative server string each, captures included);
* pins the UNKNOWN pass-through rule (matched=False, text unchanged, tier
  null) for both ``lookup`` and ``token``;
* pins the tier vocabulary to exactly ``{neutral, notice, alert, null}``;
* runs the MECHANICAL anti-jargon pin over every display text the catalog
  can emit (``displayTexts()`` — no entry can hide);
* scans the v29 template's USER-VISIBLE text (comments / scripts / styles /
  attributes stripped — they never render) for the banned jargon tokens,
  plus targeted raw-source absences for the killed display literals.

The repo has NO executable-JS harness, so the catalog half executes the REAL
shipped ``bx-copy.js`` under node — the bx-format.js/bx-explain.js precedent.
Skipped (visibly) where node is absent; the CI node-presence guard lives in
``test_dashboard_oneclick_frontend.py``. The template half is pure Python.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
from pathlib import Path
from typing import Any

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"
_TEMPLATES = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "templates"
_COPY_JS = _STATIC / "bx-copy.js"

_NODE = shutil.which("node")


def _node_call(expr: str, *argv: str) -> Any:
    """Run the REAL shipped bx-copy.js under node."""
    script = (
        f"const C = require({json.dumps(str(_COPY_JS))});"
        "const ARGS = process.argv.slice(1).map((a) => JSON.parse(a));"
        f"console.log(JSON.stringify({expr}));"
    )
    proc = subprocess.run(  # nosec B603 — fixed argv, test-only
        ["node", "-e", script, *argv],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# The forbidden-jargon vocabulary (mechanical — §1.3 of the clean-view spec)
# ---------------------------------------------------------------------------

#: (name, regex) — no catalog display text and no v29 VISIBLE text may match.
_JARGON = (
    ("rupee-amount", re.compile(r"₹\s?\d")),
    ("env-var", re.compile(r"BRUTEX_")),
    ("ledger-ref", re.compile(r"#1275")),
    ("phase-token", re.compile(r"\bP[1-6]\b")),
    ("requirement-token", re.compile(r"\bR\d+\b")),
    ("merge-integration", re.compile(r"merge-integration")),
    ("not-wired-prose", re.compile(r"not wired")),
    ("sidecar", re.compile(r"sidecar")),
    ("manifest-json", re.compile(r"manifest\.json")),
    ("top100-json", re.compile(r"top100\.json")),
    ("events-jsonl", re.compile(r"events\.jsonl")),
    ("o-file", re.compile(r"O\(file\)")),
)


def _assert_no_jargon(text: str, context: str) -> None:
    for name, pattern in _JARGON:
        assert pattern.search(text) is None, f"{context}: jargon [{name}] in {text!r}"


# ---------------------------------------------------------------------------
# Node-executed catalog goldens
# ---------------------------------------------------------------------------

pytestmark_node = pytest.mark.skipif(_NODE is None, reason="node not installed")


#: (kind, server token) -> (display, tier) — EVERY token-map entry, byte-pinned.
_TOKEN_GOLDEN: tuple[tuple[str, str, str, str], ...] = (
    ("pill", "awaiting", "no run yet", "neutral"),
    ("pill", "pending", "preparing results", "neutral"),
    ("pill", "artifacts partial", "partial results", "notice"),
    ("pill", "complete", "complete", "neutral"),
    ("pill", "suspect", "needs review", "alert"),
    ("verify", "pass", "passed", "neutral"),
    ("verify", "fail", "FAILED", "alert"),
    ("verify", "ready", "your call", "neutral"),
    ("verify", "awaiting", "waiting on data", "neutral"),
    ("verify", "not_wired", "not available yet", "neutral"),
    ("source", "results_store", "real run data", "neutral"),
    ("source", "fixture", "TEST DATA", "notice"),
    ("source", "none", "—", "neutral"),
    ("granularity", "1m", " · per-minute", "neutral"),
    ("granularity", "tf", " · per-candle", "neutral"),
)

#: (server string, display, tier) — one representative per exact entry + rule.
_LOOKUP_GOLDEN: tuple[tuple[str, str, str], ...] = (
    # Portal-panel strings (the absorbed screener/strategy copy clean —
    # merge-up round): the panels' pure helpers keep these literals for
    # main's node goldens; the DOM render remaps them via the catalog.
    (
        "minute-path equity — the stored real series (R27)",
        "equity curve — the stored minute-by-minute series",
        "neutral",
    ),
    (
        "no trades yet — awaiting real run artifact",
        "no trades yet — they appear after the first completed run",
        "neutral",
    ),
    # Health-hub surfaces (merge-up round — the #1747 widget's copy routed
    # through the catalog; the wire stays frozen).
    (
        "OK — 3 ok · 2 not wired",
        "OK — 3 ok · 2 not available yet",
        "neutral",
    ),
    (
        "not wired — no nightly report on this box",
        "Not set up on this machine yet — no nightly report on this box.",
        "neutral",
    ),
    (
        "BRUTEX_BACKUP_REPO is unset and no backup state file exists on this box",
        "A required setting is missing on this machine — hover for the exact name.",
        "neutral",
    ),
    (
        "awaiting real run artifact — no lake root wired for trades",
        "Trade detail isn't connected on this server yet.",
        "neutral",
    ),
    (
        "awaiting real run artifact — top100 entry carries no results-depth reference",
        "This run is from an older version — re-run the sweep to record trade detail.",
        "neutral",
    ),
    (
        "awaiting real run artifact — results-depth sidecars absent for r-x rank 1",
        "Trade detail wasn't written for this strategy yet.",
        "neutral",
    ),
    (
        "awaiting real run artifact — no leaderboard entry at rank 7",
        "No stored winner at position 7.",
        "neutral",
    ),
    (
        "awaiting real run artifact — events.jsonl absent",
        "No activity log written for this run yet.",
        "neutral",
    ),
    (
        "awaiting real run artifact — manifest.json absent",
        "Run record not written yet.",
        "neutral",
    ),
    (
        "awaiting real run artifact — top100.json absent",
        "Winner list not written yet.",
        "neutral",
    ),
    (
        # Review round 1: a run IS selected — the fixture just lacks the rank;
        # the generic "waiting for a sweep run" wording would be false.
        "awaiting real run artifact — no trades fixture for rank 3",
        "No test-data trades stored for strategy #3.",
        "neutral",
    ),
    (
        "awaiting real run artifact",
        "Waiting for a sweep run — nothing to show yet.",
        "neutral",
    ),
    (
        "invalid run_id/rank — nothing to read",
        "That run/strategy reference isn't valid.",
        "notice",
    ),
    (
        "results-depth read failed: boom",
        "Couldn't read the stored trade file — it may be damaged.",
        "alert",
    ),
    (
        "top100 entry tuple ref '../evil' is not three safe path segments",
        "This entry's trade reference is malformed — trade detail unavailable.",
        "alert",
    ),
    (
        "limit clamped to 500 (the server page cap)",
        "showing the first 500 trades (server page limit)",
        "neutral",
    ),
    (
        "trades fixture unreadable/corrupt: boom",
        "Test-data file is damaged.",
        "alert",
    ),
    (
        "top100.json is not valid JSON",
        "The winner list is damaged and couldn't be read.",
        "alert",
    ),
    (
        # Review round 2: the THIRD corrupt-top100 sibling — the shared
        # reader's OSError/UnicodeDecodeError arm ("<artifact>
        # unreadable/corrupt: <Exc>"), which also propagates into the trades
        # note — no longer renders filename jargon.
        "top100.json unreadable/corrupt: OSError",
        "The winner list is damaged and couldn't be read.",
        "alert",
    ),
    (
        "row 3 is not an object — rows kept up to it",
        "Part of the winner list couldn't be read — showing the readable entries.",
        "notice",
    ),
    (
        # Review round 1: the trades partial twin no longer renders verbatim.
        "trade row 4 is not an object — rows kept up to it",
        "Part of the trade list couldn't be read — showing the readable entries.",
        "notice",
    ),
    (
        "0 winners stored for this run",
        "the run finished with no surviving strategies",
        "neutral",
    ),
    (
        "manifest.json is not valid JSON",
        "Run record damaged — couldn't be read.",
        "alert",
    ),
    (
        # Review round 1: the FOURTH corrupt-manifest sibling
        # (oneclick_artifacts "is not a JSON object") joins the rule.
        "manifest.json is not a JSON object",
        "Run record damaged — couldn't be read.",
        "alert",
    ),
    (
        "chain verified: 118 records (algorithm assumed until P2 merges)",
        "Activity log intact — 118 records checked. Verification method is "
        "provisional until the audit component ships.",
        "neutral",
    ),
    (
        "chain verified: 5000 records (bounded at 5000)",
        "Activity log intact for the first 5000 records checked. Verification "
        "method is provisional until the audit component ships.",
        "neutral",
    ),
    (
        # Review round 1: the provisional caveat rides the ALERT verdicts too
        # (under the ASSUMED algorithm a reported break could itself be a
        # false alarm of the assumed algorithm — T-M1 covers EVERY verdict).
        "chain break at line 42",
        "Activity log BROKEN at record 42 — treat this run as unverified. "
        "Verification method is provisional until the audit component ships.",
        "alert",
    ),
    (
        "unparseable record at line 9",
        "Activity log damaged partway through — treat this run as unverified. "
        "Verification method is provisional until the audit component ships.",
        "alert",
    ),
    (
        "record at line 9 carries no hash",
        "Activity log damaged partway through — treat this run as unverified. "
        "Verification method is provisional until the audit component ships.",
        "alert",
    ),
    (
        "events.jsonl absent — nothing to verify",
        "No activity log written for this run yet.",
        "neutral",
    ),
    (
        "events.jsonl unreadable: [Errno 13]",
        "Activity log file couldn't be read. Verification method is "
        "provisional until the audit component ships.",
        "alert",
    ),
    (
        "chain not verified yet — click Verify (algorithm assumed until P2 merges)",
        "Not checked yet — press Replay to verify. Verification method is "
        "provisional until the audit component ships.",
        "neutral",
    ),
    (
        "chain intact but 0 records — an empty events file verifies nothing",
        "Activity log is empty — there's nothing to verify yet. Verification "
        "method is provisional until the audit component ships.",
        "neutral",
    ),
    (
        "not wired yet — lands at merge-integration (backend exists)",
        "Not available yet — this check ships in a later update.",
        "neutral",
    ),
    (
        "wired — operator eyeballs the chart vs the broker terminal",
        "Ready — compare the chart against your broker and judge it yourself.",
        "neutral",
    ),
    (
        "wired — the expand sums the trade pages client-side",
        "Ready — open the trades below and check the totals yourself.",
        "neutral",
    ),
    (
        "awaiting trades — no results-depth sidecars for this rank",
        "Waiting for this run's trade detail.",
        "neutral",
    ),
    (
        "funnel reconciles to the digit",
        "All counts add up exactly.",
        "neutral",
    ),
    (
        "funnel does NOT reconcile — see the lines",
        "Counts do NOT add up — see the arithmetic below.",
        "alert",
    ),
    (
        "manifest funnel counts awaiting",
        "Counts not available yet.",
        "neutral",
    ),
    (
        # Review round 3: the ONE deterministic CascadeWireError for a lake
        # never swept via sweep-all (cascade_wire.load_manifest) — the raw
        # string rendered an absolute filesystem path + a backticked CLI
        # command in #cascade-msg, one click deep on the operator page.
        "no cascade manifest at /tmp/lake/sweeps/cascade/manifest.json — run "
        "`brutex sweep-all` first (the sweep writes the manifest its winners "
        "are cascaded from)",
        "No completed sweep to verify yet — run a sweep first (the Run Sweep button above).",
        "neutral",
    ),
)


@pytest.mark.skipif(_NODE is None, reason="node not installed")
class TestCopyCatalogGolden:
    """Every catalog entry byte-pinned against the REAL shipped JS."""

    @pytest.mark.parametrize(("kind", "raw", "display", "tier"), _TOKEN_GOLDEN)
    def test_token_map_entries(self, kind: str, raw: str, display: str, tier: str) -> None:
        out = _node_call("C.token(ARGS[0][0], ARGS[0][1])", json.dumps([kind, raw]))
        assert out == {"text": display, "tier": tier, "matched": True}

    @pytest.mark.parametrize(("raw", "display", "tier"), _LOOKUP_GOLDEN)
    def test_lookup_entries(self, raw: str, display: str, tier: str) -> None:
        out = _node_call("C.lookup(ARGS[0][0])", json.dumps([raw]))
        assert out == {"text": display, "tier": tier, "matched": True}

    def test_unknown_string_passes_through_verbatim(self) -> None:
        """Law 2: fallback = pass-through, never suppression."""
        weird = "some entirely novel server detail 42 — never suppressed"
        out = _node_call("C.lookup(ARGS[0][0])", json.dumps([weird]))
        assert out == {"text": weird, "tier": None, "matched": False}

    def test_unknown_token_passes_through_verbatim(self) -> None:
        out = _node_call('C.token("pill", "brand_new_state")')
        assert out == {"text": "brand_new_state", "tier": None, "matched": False}
        out2 = _node_call('C.token("nonexistent_kind", "x")')
        assert out2 == {"text": "x", "tier": None, "matched": False}

    def test_tier_vocabulary_is_exactly_the_three_tiers_or_null(self) -> None:
        raws = [row[0] for row in _LOOKUP_GOLDEN] + ["unknown passthrough"]
        tiers = _node_call(
            "ARGS[0].map((s) => C.lookup(s).tier)",
            json.dumps(raws),
        )
        assert set(tiers) <= {"neutral", "notice", "alert", None}
        token_tiers = _node_call(
            "ARGS[0].map((p) => C.token(p[0], p[1]).tier)",
            json.dumps([[k, r] for k, r, _d, _t in _TOKEN_GOLDEN]),
        )
        assert set(token_tiers) <= {"neutral", "notice", "alert"}

    def test_capture_fill_is_exact(self) -> None:
        """$1/$2 placeholders fill from the server string's own digits."""
        out = _node_call('C.lookup("awaiting real run artifact — no leaderboard entry at rank 42")')
        assert out["text"] == "No stored winner at position 42."

    def test_every_display_text_is_jargon_free(self) -> None:
        """The MECHANICAL anti-jargon pin — over displayTexts(), so no entry
        (token map, exact map or rule template) can hide from the scan."""
        texts = _node_call("C.displayTexts()")
        assert len(texts) >= len(_TOKEN_GOLDEN) + 20  # the whole catalog
        for text in texts:
            _assert_no_jargon(text, "bx-copy display text")

    def test_apply_stamps_the_server_title_only_when_remapped(self) -> None:
        """Law 2 mechanics via a minimal element stand-in under node."""
        out = _node_call(
            "(() => { const el = {textContent: '', title: undefined};"
            "  const r = C.apply(el, ARGS[0][0], {});"
            "  return {text: el.textContent, title: el.title === undefined ? null : el.title,"
            "          matched: r.matched}; })()",
            json.dumps(["awaiting real run artifact"]),
        )
        assert out == {
            "text": "Waiting for a sweep run — nothing to show yet.",
            "title": "server: awaiting real run artifact",
            "matched": True,
        }
        passthrough = _node_call(
            "(() => { const el = {textContent: '', title: undefined};"
            "  const r = C.apply(el, ARGS[0][0], {});"
            "  return {text: el.textContent, title: el.title === undefined ? null : el.title,"
            "          matched: r.matched}; })()",
            json.dumps(["novel verbatim detail"]),
        )
        assert passthrough == {"text": "novel verbatim detail", "title": None, "matched": False}

    def test_apply_fallback_covers_only_the_empty_string(self) -> None:
        out = _node_call('C.apply(null, "", {fallback: "waiting for run data"})')
        assert out == {"text": "waiting for run data", "tier": "neutral", "matched": False}
        out2 = _node_call('C.apply(null, null, {fallback: "waiting for run data"})')
        assert out2["text"] == "waiting for run data"

    def test_apply_clears_a_stale_server_title_on_a_reused_element(self) -> None:
        """Review round 1: a matched→unmatched transition on a REUSED element
        must never keep the previous state's 'server: …' attribution — a
        wrong audit trail is worse than none (Law 2)."""
        out = _node_call(
            "(() => { const el = {textContent: '', title: undefined,"
            "  removeAttribute(name) { if (name === 'title') { this.title = undefined; } }};"
            "  C.apply(el, ARGS[0][0], {});"
            "  const mid = el.title;"
            "  C.apply(el, ARGS[0][1], {});"
            "  return {mid: mid === undefined ? null : mid,"
            "          text: el.textContent,"
            "          title: el.title === undefined ? null : el.title}; })()",
            json.dumps(["awaiting real run artifact", "novel verbatim detail"]),
        )
        assert out == {
            "mid": "server: awaiting real run artifact",
            "text": "novel verbatim detail",
            "title": None,
        }


# ---------------------------------------------------------------------------
# v29 template: user-visible text is jargon-free (pure Python — no node)
# ---------------------------------------------------------------------------


def _visible_text(html: str) -> str:
    """Approximate the RENDERED text: strip comments, scripts, styles and all
    tags (attributes — incl. the sanctioned ``title="server: …"`` escape
    hatches — vanish with the tags; they never render as page text)."""
    text = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.S)
    return re.sub(r"<[^>]*>", " ", text)


def _strip_portal_static(html: str) -> str:
    """Remove the two PORTAL-folded blocks (DEC-OPERATOR-PORTAL-REFOLD-001
    #1796: the winners section + the strategy drawer) before the jargon scan.

    Their STATIC default strings are main's, pinned verbatim by the landed
    ``test_dashboard_operator_portal.py`` suite — the first-lander-wins rule
    the merge-up ruling applied to the ticker static text. The RUNTIME sinks
    that overwrite them route through the BXCopy catalog (pinned by
    ``TestPortalPanelCopyRouting`` below), so the literals are a pre-poll
    flash inside a collapsed/hidden container, never a steady-state render.
    """
    text = re.sub(r'<section[^>]*id="ocWinnersSection".*?</section>', " ", html, flags=re.S)
    text = re.sub(r'<aside[^>]*id="ocStrategyDrawer".*?</aside>', " ", text, flags=re.S)
    # The ticker strip's pre-first-poll STATIC default is likewise main's
    # wording (the merge-up first-lander rule); renderTicker rewrites it with
    # the plain-English strip on the first poll (integration-pinned).
    return re.sub(r'<span id="bxTickerStrip">[^<]*</span>', " ", text)


class TestOperatorPageVisibleTextClean:
    """The operator page's visible prose carries no developer jargon."""

    def test_v29_visible_text_is_jargon_free(self) -> None:
        html = _strip_portal_static((_TEMPLATES / "v29.html").read_text(encoding="utf-8"))
        visible = _visible_text(html)
        for name, pattern in _JARGON:
            hits = pattern.findall(visible)
            assert not hits, f"v29 visible text: jargon [{name}] survives: {hits[:3]!r}"
        # The clean-view specific bans (visible prose only — engineering
        # comments legitimately keep their vocabulary and never render).
        for needle in (
            "awaiting real run artifact",
            "tuples",
            "PROVISIONAL",
            "not_wired",
            "results_store",
            "results-depth",
        ):
            assert needle not in visible, f"v29 visible text still says {needle!r}"

    def test_v29_raw_source_display_literal_absences(self) -> None:
        """The killed display literals are gone from the WHOLE file — JS
        string literals included (comments were swept where they carried the
        exact display wording). Post-merge-up exemption: the portal-folded
        winners/drawer static blocks (#1796) carry main's pinned defaults —
        stripped here, with the containment asserted separately below."""
        raw = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        # Every remaining "awaiting real run artifact" literal must live
        # INSIDE the two portal-folded blocks (main's pinned static text).
        assert "awaiting real run artifact" not in _strip_portal_static(raw)
        html = _strip_portal_static(raw)
        for needle in (
            "awaiting real run artifact",
            "tuples",
            "not wired yet",
            "merge-integration",
            "PROVISIONAL",
            "O(file)",
            "Sweep refused",
            "Cascade error",
            "nine-filter cascade",
        ):
            assert needle not in html, f"v29 raw source still carries {needle!r}"

    def test_fixture_env_var_survives_only_inside_a_title_attribute(self) -> None:
        """The badge stays LOUD but the env-var name lives in title= only."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert 'title="server: BRUTEX_ONECLICK_FIXTURE_ROOT active"' in html
        stripped = _visible_text(html)
        assert "BRUTEX_ONECLICK_FIXTURE_ROOT" not in stripped
        # The badge itself stays purple/uppercase-loud (class + wording).
        assert "TEST DATA — fixture override active (not real market results)" in html

    def test_awaiting_banner_is_calm_not_amber(self) -> None:
        """Empty ≠ error: the .oc-awaiting rule carries no gold gradient or
        yellow text — amber is reserved for genuine NOTICE states."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        rule = re.search(r"\.oc-awaiting \{([^}]*)\}", html)
        assert rule is not None, ".oc-awaiting rule must exist"
        body = rule.group(1)
        assert "255,183,77" not in body, "gold border/gradient survived on .oc-awaiting"
        assert "--yellow" not in body, "yellow text survived on .oc-awaiting"
        assert "var(--muted)" in body
        assert "var(--surface-2)" in body

    def test_awaiting_banner_hidden_attribute_actually_hides(self) -> None:
        """Review round 1: the author-origin ``display: flex`` on
        ``.oc-awaiting`` beats the UA ``[hidden]{display:none}`` rule — the
        explicit override must exist or ``awaiting.hidden = true`` never
        hides the banner (it rendered beside a live run's own pill/hero)."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert ".oc-awaiting[hidden] { display: none; }" in html

    def test_heartbeat_pointer_and_hero_collapse_labels_are_accurate(self) -> None:
        """Review round 1 copy accuracy: the Run button sits ABOVE the
        diagnostics block ("below" pointed the wrong way), and the
        hero-collapse line binds the ENUMERATED count — "generated", never
        the page's other meaning of "tested"."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert "start one from the Run panel above" in html
        assert "start one below" not in html
        assert "ideas generated" in html
        assert "ideas tested" not in html

    def test_directions_options_submit_buy_sell(self) -> None:
        """Folded bugfix: the backend accepts BUY/SELL only — long/short
        values 400'd every non-default pick (sweep_runner._normalise_directions)."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert '<option value="BUY">long (buy)</option>' in html
        assert '<option value="SELL">short (sell)</option>' in html
        assert '<option value="">both (default)</option>' in html
        assert '<option value="long">' not in html
        assert '<option value="short">' not in html

    def test_one_primary_run_button_cascade_demoted_under_advanced(self) -> None:
        """Exactly ONE primary button pre-fold; the cascade + condition filter
        live inside <details class="oc-advanced">."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert html.count('id="run-sweep"') == 1
        assert html.count('id="run-cascade"') == 1
        advanced = re.search(r'<details class="oc-advanced">(.*?)</details>', html, re.S)
        assert advanced is not None, "the Advanced collapse must exist"
        assert 'id="run-cascade"' in advanced.group(1)
        assert 'id="swConditions"' in advanced.group(1)
        assert 'id="run-sweep"' not in advanced.group(1)
        assert "↻ Re-verify latest run" in advanced.group(1)

    def test_diagnostics_details_wraps_the_demoted_strips(self) -> None:
        """The sweep-id strip + progress panel + heartbeat live inside the
        collapsed System-details block; VERIFY panel + timeline stay outside."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        diag = re.search(r'<details id="bxDiagnostics"[^>]*>(.*?)</details>', html, re.S)
        assert diag is not None, "the bxDiagnostics collapse must exist"
        body = diag.group(1)
        for anchor in ('id="sweepProgressPanel"', 'id="bxHeartbeat"', 'class="sweep-id'):
            assert anchor in body, f"{anchor} must be inside bxDiagnostics"
        assert 'id="bxDiagBadge"' in html
        assert 'id="ocVerifyPanel"' not in body, "VERIFY panel must stay outside (unmoved)"
        assert 'id="ocTimeline"' not in body, "the timeline must stay outside (unmoved)"
        # Never auto-opened: the details carries no `open` attribute.
        assert re.search(r'<details id="bxDiagnostics"[^>]*\bopen\b', html) is None

    def test_hero_and_heartbeat_handle_the_failing_and_local_verdicts(self) -> None:
        """Review round 2: the DISTINCT server verdicts flow_state='failing'
        (FLOW_FAILING — an active pull whose every call fails) and 'local'
        (FLOW_LOCAL — an active no-vendor-calls stage) must never fall
        through to the false-calm idle arms of the hero or heartbeat card."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        # Hero row 3 red covers 'failing' with honest wording.
        assert "const pullFailing = S.pull === 'failing';" in html
        assert "'the data pull is failing every call'" in html
        # Hero row 2 sub never invents a fetch during a local stage.
        assert "processing pulled data locally — no vendor calls right now" in html
        # A pull's FIRST stalled tick reads paused-but-active, never Idle
        # (the >=2-tick hysteresis still owns the red escalation).
        assert "(S.pull === 'stalled' && S.pullStalledTicks < 2)" in html
        assert "data flow paused — waiting on the data vendor" in html
        # The heartbeat pull card renders both states as ACTIVE — in
        # agreement with its own pullWasActive() classifier.
        # Post-merge-up the heartbeat card renders main's #1747 classified
        # truth arms (DEC-OPERATOR-HEALTH-TRUTH-001 — their strings win the
        # structure per the coordinator ruling); the intent pinned here is
        # unchanged: failing/local never fall through to a false-calm idle.
        assert "'pull active — calls FAILING'" in html
        assert "'pull active — local processing'" in html

    def test_hero_suspect_row_claims_latest_only_when_true(self) -> None:
        """Review round 2: the suspect flag rides the SELECTED run's
        manifest, so 'Latest' is claimed only when that run IS the runs
        feed's latest_run_id; an older selection reads 'Selected'."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert "(isLatest ? 'Latest' : 'Selected')" in html
        assert "' run needs review — a self-check flagged its numbers'" in html

    def test_inline_msg_sinks_clear_stale_error_titles(self) -> None:
        """Review round 2: the round-1 stale-title law reaches the two v29
        inline message sinks — a healthy #sweep-msg / #cascade-msg write
        clears the previous blip's raw-error tooltip."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert html.count("sweepMsg.removeAttribute('title')") >= 2
        assert "cascadeMsg.removeAttribute('title')" in html

    def test_advanced_condition_filter_names_itself_a_future_sweep_option(self) -> None:
        """Review round 2: the Re-verify explainer sits directly above its
        own button and the disabled condition filter carries its own note
        naming it a FUTURE SWEEP option — never read as a re-verify knob."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert "A future sweep option — picks which market conditions a sweep" in html
        assert "Re-verify — re-runs the 9-filter statistical verification" in html
        # Order inside the disclosure: filter row first, then the re-verify
        # explainer, then the button it describes.
        advanced = re.search(r'<details class="oc-advanced">(.*?)</details>', html, re.S)
        assert advanced is not None
        body = advanced.group(1)
        assert (
            body.index("A future sweep option")
            < body.index("Re-verify — re-runs")
            < body.index("↻ Re-verify latest run")
        )

    def test_status_hero_present_with_the_idle_default(self) -> None:
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert 'id="bxStatusHero"' in html
        assert 'id="bxHeroLine"' in html
        assert 'id="bxHeroSub"' in html
        assert "Idle — ready to run" in html
        # The hero carries no ₹ (the zero-₹ pin owns the whole template; this
        # is the hero-scoped restatement).
        hero = re.search(r'<section[^>]*id="bxStatusHero".*?</section>', html, re.S)
        assert hero is not None
        assert "₹" not in hero.group(0)

    def test_sweep_progress_hidden_attribute_actually_hides(self) -> None:
        """Review round 3: the same author-``display:flex``-beats-UA-[hidden]
        defect class fixed for ``.oc-awaiting`` in round 1, on its sibling —
        without the override the idle template-default zeros row ("0/0
        tested · 0 survivors") renders inside System details before any
        sweep ever ran, colliding with the empty-≠-zero doctrine."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert ".sweep-progress[hidden] { display: none; }" in html

    def test_diag_badge_counts_attention_never_a_feed_diagnosis(self) -> None:
        """Review round 3: ALERT-tier ≠ feed outage — a sweep ERROR, a
        FAILING pull and FAILING health checks all set 'bad' over a HEALTHY
        feed, so "N feed(s) failing" was a factually wrong diagnosis that
        pointed the operator at connectivity. The badge counts what needs
        attention; the cause lives in the slots (and the hero row 3)."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert "' feed' + (failing > 1 ? 's' : '') + ' failing'" not in html
        assert "(failing > 1 ? ' need attention' : ' needs attention')" in html

    def test_hero_degrades_active_rows_on_a_feed_outage(self) -> None:
        """Review round 3: unknown is never green — a feed outage over a
        last-known-ACTIVE state must not re-paint stale numbers green
        indefinitely. Both active rows degrade to the warn "unreachable —
        reconnecting" treatment at the heartbeat's own >=2-fail threshold,
        and the pull feed gains the outage fan-out its success path already
        had (S.pull previously froze at 'flowing' through an outage)."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert "if (S.sweepFeedFails >= 2) {" in html
        assert "'Sweep status unreachable — reconnecting'" in html
        assert "if (S.pullFeedFails >= 2) {" in html
        assert "'Data pull status unreachable — reconnecting'" in html
        # The fan-out exists, is exported, and the shared-poll failure
        # subscriber fires it (post-#1720 there is ONE failure path — the
        # __bxPullRateFailSubs subscriber — not the old two pollPull arms).
        assert "window.__bxHeroPullErr = window.BXHero.pullErr;" in html
        assert html.count("window.__bxHeroPullErr && window.__bxHeroPullErr();") == 1
        # A successful poll resets both counters (the degrade never latches).
        assert "S.sweepFeedFails = 0;" in html
        assert "S.pullFeedFails = 0;" in html

    def test_cascade_error_sink_routes_known_refusals_through_the_catalog(self) -> None:
        """Review round 3: the known no-manifest CascadeWireError rendered an
        absolute server path + a backticked CLI command in #cascade-msg. The
        sink now classifies via BXCopy — a KNOWN refusal renders the catalog
        text (no "failed" prefix: nothing failed, there is nothing to verify
        yet) with the verbatim string in title="server: …"; an UNKNOWN error
        keeps the verbatim pass-through under the honest prefix (Law 2)."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert "if (window.BXCopy && BXCopy.lookup(rawErr).matched) {" in html
        assert "BXCopy.apply(cascadeMsg, rawErr, {});" in html
        assert "BXCopy.apply(cascadeMsg, rawErr, { prefix: 'Verification failed: ' });" in html
        assert "cascadeMsg.textContent = 'Verification failed: ' + rawErr;" in html

    def test_fabricated_live_trade_marquee_is_gone(self) -> None:
        """Review round 3: the hardcoded v24.4 "LIVE" ticker marquee —
        invented trades, percentages and TP/SL verdicts pulsing above this
        pass's "nothing is ever invented" banner — is deleted (it survived
        the DEC-OPERATOR-PAGE-REAL-001 sweep because the guard pin keyed on
        ₹-digits). Track 1 has no live surface that could feed it
        (DEC-LIVE-REMOVE-001)."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert 'id="liveTickerTrack"' not in html
        assert "TP hit" not in html
        assert "SL hit" not in html
        assert '<div class="hook-banner">' not in html

    def test_run_outcome_copy_pluralizes_the_one_survivor_case(self) -> None:
        """Review round 3 copy edits: a 1-survivor run is a realistic outcome
        and its headline lines must read grammatically ("1 winning
        strategy", "1 winner", "1 strategy passed"); a finished sweep's
        heartbeat reads "done · 100%", never the doubled "done · 100% done"."""
        html = (_TEMPLATES / "v29.html").read_text(encoding="utf-8")
        assert "(sp.state === 'done' ? '' : ' done')" in html
        assert "(nWin === 1 ? ' winner' : ' winners')" in html
        assert "((p.survivors || 0) === 1 ? ' winner' : ' winners')" in html
        assert "((p.survivors || 0) === 1 ? ' winning strategy.' : ' winning strategies.')" in html
        assert "(sp.survivors === 1 ? ' winner so far' : ' winners so far')" in html
        assert "(S.doneSurvivors === 1 ? ' strategy passed' : ' strategies passed')" in html
        # Idle-capitalization outlier: heartbeat idle cards read lowercase.
        assert "'Not yet run'" not in html


class TestPortalPanelCopyRouting:
    """The portal panels (DEC-OPERATOR-PORTAL-REFOLD-001 #1796) route their
    VISIBLE runtime copy through the ONE BXCopy catalog — the absorbed
    screener/strategy copy clean (coordinator-approved at the merge-up).

    Source pins only: the panels keep their node-goldened pure-helper
    literals (main's suite executes them without BXCopy), so the guarded
    ``window.BXCopy ? … : <literal>`` shape is exactly what is asserted —
    catalog render in the browser, literal fallback in the node harness.
    Panel logic/structure untouched — copy + rendering hooks only.
    """

    def _js(self, name: str) -> str:
        text = (_STATIC / name).read_text(encoding="utf-8")
        assert text
        return text

    def test_screener_detail_sinks_route_through_the_catalog(self) -> None:
        js = self._js("bx-screener-panel.js")
        # The two awaiting/detail sinks ride BXCopy.apply with the calm
        # fallback; the literal stays for the node harness.
        assert js.count("window.BXCopy.apply(byId('scDetail')") >= 2
        assert js.count("{ fallback: 'waiting for run data' }") >= 2
        # The loaded/partial arm renders catalog text (verbatim server string
        # demoted to title="server: …" by bxCopyApply).
        assert "'Stored list partly readable — '" in js
        assert "'winner list loaded'" in js
        # The node-harness fallback arm keeps main's pinned literal.
        assert "'PARTIAL artifact: ' + String(payload.detail)" in js

    def test_strategy_panel_sinks_route_through_the_catalog(self) -> None:
        js = self._js("bx-strategy-panel.js")
        # Equity label DOM write remaps (the pure chooser keeps its literals).
        assert "if (window.BXCopy) { window.BXCopy.apply(label, text, {}); }" in js
        # Trades note / stName awaiting sinks ride the catalog.
        assert js.count("{ fallback: 'waiting for run data' }") >= 3
        # The trades source badge mirrors the bx-oneclick token pattern.
        assert "window.BXCopy.token('source', rawSource)" in js
        assert "'server: ' + rawSource" in js
        # The two static no-trades notes are remapped at render.
        assert js.count("window.BXCopy.apply(") >= 5

    def test_panel_visible_string_literals_carry_no_new_jargon(self) -> None:
        """Every NEW display string this routing introduces is jargon-free
        (the catalog's own displayTexts() pin covers the remap targets)."""
        for s in (
            "waiting for run data",
            "Stored list partly readable — ",
            "winner list loaded",
        ):
            _assert_no_jargon(s, "portal panel copy")
