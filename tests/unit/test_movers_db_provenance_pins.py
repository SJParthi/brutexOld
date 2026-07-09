"""Substring-presence pins for the /movers source-provenance line + live /db
verify chip + the /db candle deep-link boot handler.

DEC-MOVERS-DB-PROVENANCE-VERIFY-001. The repo has NO executable-browser harness
(an honest, known limit — the DEC-PULL-UI-STALENESS-001 precedent), so this
suite pins the PRESENCE of the shipped JS, not its execution — the executed
teeth are the node goldens in ``test_bx_verify_golden.py`` over the REAL
``bx-verify.js`` + the REAL ``bx-format.js``.

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
