"""Substring-presence pins for the /movers source-provenance line + live /db
verify chip + the /db candle deep-link boot handler.

DEC-MOVERS-DB-PROVENANCE-VERIFY-001. This suite pins the PRESENCE of the
shipped JS (the fast unit layer), not its execution. The EXECUTED layers are
elsewhere and real: the node goldens in ``test_bx_verify_golden.py`` run the
REAL ``bx-verify.js`` + the REAL ``bx-format.js``, and the repo's Chromium
browser truth suite (``tests/browser`` — DEC-DASHBOARD-BROWSER-TRUTH-001, the
CI ``browser`` job) renders /movers with a real payload and executes the full
chip flow end-to-end (``test_movers_gate.py::test_verify_chip_green_execution``
pins the green state; its console-clean teardown pins the zero-400 probe
discipline). The round-1 claim here that "the repo has NO executable-browser
harness" was WRONG — corrected by the round-2 refuter.

What is pinned here:

* **movers.html** — the static ``#src`` skeleton (source line + click-through
  link + verify chip), the ``bx-verify.js`` include ordered AFTER
  ``bx-format.js`` (the comparator resolves the shared ``bxFixed2`` global),
  the sorted-view stash (``visibleData``) inside ``renderRows`` (the "top-K
  visible rows" law — ``rowsData`` stays server-ordered), the bounded fetch
  discipline (ONE ``AbortController`` per pass · 10 s timeout ·
  ``Promise.allSettled`` · ``document.hidden`` skip), the honest chip states
  (F&O ``LTP only`` envelope · ``never a fake green``), and the ABSENCE of any
  innerHTML write on the three provenance nodes (CodeQL XSS gate).
* **db.html** — the additive ``?candles=`` deep-link boot handler: gated on the
  ``candles=`` param (a bare /db boots byte-identically), format-validated
  ``date``/``interval`` (invalid values DROP the param, never block boot), and
  the unstored-TF fall-back to the ``1minute`` base.
"""

from __future__ import annotations

from pathlib import Path

_DASHBOARD = Path(__file__).resolve().parents[2] / "brutex" / "dashboard"
_MOVERS = _DASHBOARD / "templates" / "movers.html"
_DB = _DASHBOARD / "templates" / "db.html"
_VERIFY_JS = _DASHBOARD / "static" / "bx-verify.js"


class TestMoversSourceLine:
    def test_static_skeleton_is_present(self) -> None:
        html = _MOVERS.read_text(encoding="utf-8")
        assert 'id="src" hidden' in html
        assert '<span id="srcText"></span>' in html
        assert '<a id="srcLink" class="srclink"></a>' in html
        assert '<span id="verifyChip" class="muted"></span>' in html

    def test_source_line_writes_are_textcontent_only(self) -> None:
        # The three provenance nodes must NEVER be innerHTML sinks — runtime
        # writes are .textContent / .hidden / .className / the href attribute
        # (DEC-DASHBOARD-XSS-HARDENING-001).
        html = _MOVERS.read_text(encoding="utf-8")
        assert '$("srcText").textContent' in html
        assert '$("srcLink").textContent' in html
        assert 'srcText").innerHTML' not in html
        assert 'verifyChip").innerHTML' not in html
        assert 'srcLink").innerHTML' not in html

    def test_spot_source_line_surfaces_the_1145_read_provenance(self) -> None:
        # The EXISTING stored_reads / resampled_reads payload counts
        # (DEC-PIPELINE-AUTOPREBUILD-001) are surfaced verbatim — no new
        # payload key was fabricated for the source line.
        html = _MOVERS.read_text(encoding="utf-8")
        assert "j.stored_reads" in html
        assert "j.resampled_reads" in html
        assert "stored-TF" in html
        assert "derived-from-1m" in html

    def test_fno_source_line_states_the_constant_read_mode(self) -> None:
        # The F&O read mode is the DEC-FNO-TF-DERIVE-OI-001 documented constant.
        html = _MOVERS.read_text(encoding="utf-8")
        assert "1-minute-native session + stored Greeks overlay" in html

    def test_provenance_refreshes_on_the_skip_render_fast_path_too(self) -> None:
        html = _MOVERS.read_text(encoding="utf-8")
        assert html.count("refreshProvenance()") >= 3  # renderRows both arms + skip path


class TestMoversClickThrough:
    def test_deep_link_is_built_with_urlsearchparams(self) -> None:
        html = _MOVERS.read_text(encoding="utf-8")
        assert '"/db?" + q.toString()' in html
        assert 'q.set("candles", "spot")' in html
        assert 'q.set("candles", "fno")' in html

    def test_fno_link_pins_the_1minute_partition(self) -> None:
        # The F&O tabs READ 1m-native — the link lands on the partition they
        # actually read (always stored for a pulled contract).
        html = _MOVERS.read_text(encoding="utf-8")
        assert 'q.set("interval", "1minute")' in html

    def test_href_is_set_as_an_attribute_never_markup(self) -> None:
        html = _MOVERS.read_text(encoding="utf-8")
        assert '$("srcLink").href = "/db?" + q.toString();' in html


class TestMoversVerifyChip:
    def test_verify_module_loads_after_bx_format(self) -> None:
        html = _MOVERS.read_text(encoding="utf-8")
        fmt = html.index('<script src="/static/bx-format.js"></script>')
        ver = html.index('<script src="/static/bx-verify.js"></script>')
        inline = html.index("<script>\n")
        assert fmt < ver < inline, (
            "bx-verify.js must load (non-defer) AFTER bx-format.js (it resolves the "
            "shared bxFixed2 global) and BEFORE the inline script that calls BXVerify"
        )

    def test_render_rows_stashes_the_sorted_view(self) -> None:
        # "top-K visible rows" = the rows the operator SEES: renderRows sorts a
        # LOCAL copy (rowsData stays server-ordered) and must stash it.
        html = _MOVERS.read_text(encoding="utf-8")
        assert "visibleData = data;" in html
        assert "visibleData.slice(0, VERIFY_TOP_K)" in html

    def test_bounded_fetch_discipline(self) -> None:
        html = _MOVERS.read_text(encoding="utf-8")
        assert "new AbortController()" in html
        assert "VERIFY_TIMEOUT_MS = 10000" in html
        assert "Promise.allSettled" in html
        assert "document.hidden" in html
        # single-flight: a newer render supersedes the in-flight pass
        assert "verifyGen" in html
        assert "gen !== verifyGen" in html

    def test_probes_ride_the_existing_db_candles_api(self) -> None:
        html = _MOVERS.read_text(encoding="utf-8")
        assert '"/api/v1/db/candles?" + q.toString()' in html
        # the probe always reads the 1-minute base, one desc day page
        assert 'interval: "1minute"' in html
        assert 'desc: "true", limit: "500"' in html

    def test_honest_chip_states(self) -> None:
        html = _MOVERS.read_text(encoding="utf-8")
        # F&O envelope disclosed in the green copy
        assert "F&O LTP only" in html
        # a probe failure is an honest unavailable — never a fake green
        assert "never a fake green" in html
        assert "verify unavailable" in html
        # a red names the exact cell + both values
        assert "✗ /db MISMATCH" in html
        # the actionable stale-TF remediation suffix on a non-1minute display TF
        assert "displayed TF may be stale vs the 1-minute base" in html

    def test_chip_writes_are_textcontent_only(self) -> None:
        html = _MOVERS.read_text(encoding="utf-8")
        assert "chip.textContent = text;" in html

    def test_verify_module_has_no_innerhtml_sink(self) -> None:
        assert ".innerHTML" not in _VERIFY_JS.read_text(encoding="utf-8")

    # ── round-2 refuter folds ─────────────────────────────────────────────

    def test_asset_is_captured_at_schedule_time(self) -> None:
        # Refuter S2 (tab-switch false-red race): the pass must verify the
        # asset it was SCHEDULED for — a fire-time state.asset read probes
        # spot rows as segment=FNO after a tab click.
        html = _MOVERS.read_text(encoding="utf-8")
        assert "const asset = state.asset;" in html
        assert "runVerify(gen, asset)" in html
        assert "async function runVerify(gen, asset)" in html
        assert "const isSpot = asset ===" in html
        assert "const isSpot = state.asset ===" not in html

    def test_tab_switch_supersedes_the_pending_pass(self) -> None:
        # Belt on top of the schedule-time capture: the asset-tab handler
        # cancels BEFORE mutating state.asset.
        html = _MOVERS.read_text(encoding="utf-8")
        assert "function cancelVerify()" in html
        idx_cancel = html.index("cancelVerify();")
        idx_mutate = html.index("state.asset = t.dataset.asset;")
        assert idx_cancel < idx_mutate

    def test_spot_latest_cut_anchors_at_the_payloads_own_latest_time(self) -> None:
        # Refuter S3 (live-pull false red): the spot Latest cut anchors at the
        # payload's own latest_time echo (DEC-MOVERS-ASOF-LATEST-DEFAULT-001),
        # not the newest /db row at PROBE time; F&O/daily stay un-anchored
        # (latest_time is a SPOT-derived echo) with the residual race disclosed.
        html = _MOVERS.read_text(encoding="utf-8")
        assert "j.time || j.latest_time || null" in html
        assert "or a live pull advanced /db past this snapshot" in html

    def test_db_unreadable_rows_are_a_disclosed_skip_never_a_400_probe(self) -> None:
        # The /db READ allowlist (db_view._SYMBOL_RE) is narrower than the lake
        # WRITE allowlist — an unreadable row must never burn a guaranteed-400
        # probe (browser console-clean law; one wasted request per render).
        html = _MOVERS.read_text(encoding="utf-8")
        assert "BXVerify.bxDbProbeOk(" in html
        assert "not /db-readable" in html
        js = _VERIFY_JS.read_text(encoding="utf-8")
        assert "function bxDbProbeOk(" in js
        assert r"/^[A-Za-z0-9][A-Za-z0-9_.&\- ]*$/" in js  # the _SYMBOL_RE mirror

    def test_missing_row_wording_distinguishes_empty_page_from_cut_miss(self) -> None:
        # Refuter S4: "no /db 1-minute rows for <date>" was claimed even when
        # rows existed but none sat at/before the pick — now a distinct truth.
        html = _MOVERS.read_text(encoding="utf-8")
        assert "no /db 1-minute rows for" in html
        assert "but none at/before" in html

    def test_hidden_pause_resumes_on_visibilitychange(self) -> None:
        # Refuter S5: a render landing on a hidden tab pauses verify — the
        # visibilitychange listener must re-schedule on return, never leaving
        # a stale "paused (hidden tab)" chip on a visible tab.
        html = _MOVERS.read_text(encoding="utf-8")
        assert "verifyHiddenPaused" in html
        assert "else if (!document.hidden && verifyHiddenPaused) scheduleVerify();" in html


class TestDbDeepLink:
    def test_boot_handler_is_gated_on_the_candles_param(self) -> None:
        # A bare /db (no ?candles=) must boot byte-identically.
        html = _DB.read_text(encoding="utf-8")
        assert "new URLSearchParams(location.search)" in html
        assert 'q.get("candles") === "spot" || q.get("candles") === "fno"' in html

    def test_date_and_interval_are_format_validated(self) -> None:
        # An invalid value DROPS that param (field falls to its default);
        # boot never blocks.
        html = _DB.read_text(encoding="utf-8")
        assert r"/^\d{4}-\d{2}-\d{2}$/" in html
        assert r"/^\d+(minute|hour)$|^1day$/" in html

    def test_deep_link_reads_the_named_params(self) -> None:
        html = _DB.read_text(encoding="utf-8")
        for param in ("candles", "exchange", "segment", "symbol", "interval", "date"):
            assert f'q.get("{param}")' in html, param

    def test_unstored_interval_falls_back_to_the_1minute_base(self) -> None:
        html = _DB.read_text(encoding="utf-8")
        assert "x.interval === wantIv" in html
        assert 'x.interval === "1minute"' in html  # the spot fall-back chain
        assert 'wantIv || "1minute"' in html  # the fno single-descriptor default

    def test_deep_link_adds_no_new_innerhtml_path(self) -> None:
        # exchange/segment/symbol are DATA-only: the handler assigns cstate
        # fields and calls the pre-existing pipeline (buildCandleUrl's
        # encodeURIComponent + candleControlsHtml/spotCascadeHtml's esc()).
        html = _DB.read_text(encoding="utf-8")
        start = html.index("async function deepLinkCandles(")
        end = html.index("function boot()")
        handler = html[start:end]
        assert ".innerHTML" not in handler
        assert "insertAdjacentHTML" not in handler
        assert "document.write" not in handler

    def test_spot_deep_link_resolves_through_the_real_cascade(self) -> None:
        # The symbol must resolve via the cascade's own _spotKey composite —
        # no match ⇒ the cascade's honest prompt stays (no fabrication).
        html = _DB.read_text(encoding="utf-8")
        assert "cstate.spot.syms.find(x => _spotKey(x) === key)" in html
