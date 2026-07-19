"""The per-feed universe scope law — DEC-UNIVERSE-INDEX-ONLY-001 (design §2/§6).

The full §2.3 grammar matrix, the memo law (per-feed + the composite-keyed
read union), the §2.5 ACTIVE-feed registration seam (primed-memo +
refused-registration arms), the two mirror pins, the A2 monotonicity
property, and the §2.1 EMPIRICAL closure import pin.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.config import feed_scope
from brutex.config.feed_scope import (
    DEFAULT_FEED,
    DEFAULT_FEED_SCOPES,
    FEED_SCOPE_ENV,
    INDEX4_FNO_UNDERLYINGS,
    INDEX4_UNDERLYINGS,
    READ_UNION_FEED,
    SCOPE_FULL,
    SCOPE_INDEX4,
    FeedScope,
    FeedScopeError,
    active_feeds,
    note_feed_registered,
    read_scope_snapshot,
    resolve_feed_scope,
    resolve_read_scope,
    scope_revision,
    validate_feed_scope_env,
)
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from structlog.testing import capture_logs

# ────────────────────────────────────────────────────────────────────────
# Instrument helpers (the test_universe.py idiom)
# ────────────────────────────────────────────────────────────────────────


def _instrument(
    symbol: str,
    instrument_type: InstrumentType,
    exchange: Exchange = Exchange.NSE,
    *,
    underlying: str | None = None,
) -> Instrument:
    return Instrument(
        exch_id=exchange,
        security_id=abs(hash((symbol, instrument_type, exchange))) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=instrument_type,
        segment=Segment.FNO if instrument_type.value in ("FUT", "CE", "PE") else Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=underlying if underlying is not None else symbol,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=date(2026, 9, 24) if instrument_type.value in ("FUT", "CE", "PE") else None,
        strike_price=Decimal("100") if instrument_type.value in ("CE", "PE") else None,
        asm_gsm_flag=False,
    )


# ────────────────────────────────────────────────────────────────────────
# §2.3 grammar matrix
# ────────────────────────────────────────────────────────────────────────


class TestEnvGrammar:
    """The full ``BRUTEX_FEED_SCOPE`` grammar (design §2.3)."""

    def test_unset_resolves_defaults_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(FEED_SCOPE_ENV, raising=False)
        assert resolve_feed_scope("groww").token == SCOPE_INDEX4
        assert resolve_feed_scope("dhan").token == SCOPE_INDEX4

    @pytest.mark.parametrize("raw", ["", "   ", "\t "])
    def test_blank_env_is_unset_never_a_refusal(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Blank ≡ UNSET → the defaults map (§2.3 Rev 5 — never exit-1s a box)."""
        monkeypatch.setenv(FEED_SCOPE_ENV, raw)
        scope = resolve_feed_scope("groww")
        assert scope.token == SCOPE_INDEX4
        assert validate_feed_scope_env().token == SCOPE_INDEX4

    @pytest.mark.parametrize("raw", ["full", " FULL ", "Full"])
    def test_bare_full_normalized(self, raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FEED_SCOPE_ENV, raw)
        scope = resolve_feed_scope("groww")
        assert scope.token == SCOPE_FULL
        assert not scope.bounded()
        assert scope.admits_stocks()

    def test_bare_index4(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FEED_SCOPE_ENV, "index4")
        scope = resolve_feed_scope("groww")
        assert scope.token == SCOPE_INDEX4
        assert scope.bounded()
        assert scope.admitted_underlyings == INDEX4_UNDERLYINGS
        assert scope.fno_underlyings == INDEX4_FNO_UNDERLYINGS

    def test_per_feed_csv_with_key_normalization(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FEED_SCOPE_ENV, " GROWW = Full , gdfl=index4 ")
        assert resolve_feed_scope("groww").token == SCOPE_FULL
        assert resolve_feed_scope("gdfl").token == SCOPE_INDEX4
        # dhan absent from the CSV → the defaults map (scope-known).
        assert resolve_feed_scope("dhan").token == SCOPE_INDEX4

    @pytest.mark.parametrize(
        "raw",
        [
            "banana",  # unknown bare token
            "groww=banana",  # unknown token in a pair
            "full,",  # empty entry within a non-blank value
            "=x",  # empty key
            "=",  # bare =
            "a==b",  # malformed (two =)
            "groww=index4,groww=full",  # duplicate key — never last-wins
            "GROWW=index4,groww=full",  # duplicate AFTER normalization
            "full,gdfl=index4",  # mixing bare with key= pairs
            "index4,full",  # two bare tokens — not a valid form
        ],
    )
    def test_junk_refused_loudly(self, raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FEED_SCOPE_ENV, raw)
        with pytest.raises(FeedScopeError, match="BRUTEX_FEED_SCOPE"):
            resolve_feed_scope("groww")
        with pytest.raises(FeedScopeError):
            validate_feed_scope_env()
        with pytest.raises(FeedScopeError):
            resolve_read_scope()

    def test_unknown_feed_absent_everywhere_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(FEED_SCOPE_ENV, raising=False)
        scope = resolve_feed_scope("gdfl")
        assert scope.token == SCOPE_INDEX4
        assert scope.bounded()

    def test_bare_token_covers_scope_known_feeds_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare ``full`` widens groww+dhan (scope-KNOWN) but NOT an unknown feed."""
        monkeypatch.setenv(FEED_SCOPE_ENV, "full")
        assert resolve_feed_scope("groww").token == SCOPE_FULL
        assert resolve_feed_scope("dhan").token == SCOPE_FULL
        assert resolve_feed_scope("gdfl").token == SCOPE_INDEX4  # fail-closed

    def test_defaults_map_is_the_operator_2026_07_17_map(self) -> None:
        assert dict(DEFAULT_FEED_SCOPES) == {"groww": SCOPE_INDEX4, "dhan": SCOPE_INDEX4}
        assert DEFAULT_FEED == "groww"


class TestFailClosedDefaultWarn:
    """§2.3 Rev 5 — the ACTIVE-feed fail-closed-default warn tier."""

    def test_active_unknown_feed_under_bare_full_warns_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(FEED_SCOPE_ENV, "full")
        note_feed_registered("gdfl")
        with capture_logs() as logs:
            first = resolve_feed_scope("gdfl")
            second = resolve_feed_scope("gdfl")
        assert first.token == SCOPE_INDEX4  # fail-closed despite bare full
        assert second.token == SCOPE_INDEX4
        events = [e for e in logs if e["event"] == "feed_scope.active_feed_fail_closed_default"]
        assert len(events) == 1
        assert events[0]["feed"] == "gdfl"

    def test_no_warn_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(FEED_SCOPE_ENV, raising=False)
        note_feed_registered("gdfl")
        with capture_logs() as logs:
            assert resolve_feed_scope("gdfl").token == SCOPE_INDEX4
        assert not [e for e in logs if e["event"] == "feed_scope.active_feed_fail_closed_default"]

    def test_no_warn_for_inactive_unknown_feed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FEED_SCOPE_ENV, "full")
        with capture_logs() as logs:
            assert resolve_feed_scope("gdfl").token == SCOPE_INDEX4
        assert not [e for e in logs if e["event"] == "feed_scope.active_feed_fail_closed_default"]


class TestNonActiveKeyLaw:
    """§2.3 Rev 3 — the two-tier non-ACTIVE-key law (typo'd/premature keys inert)."""

    def test_unknown_key_accepted_warned_and_union_inert(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The typo'd ``grow=full`` is fully inert on BOTH admission and reads."""
        monkeypatch.setenv(FEED_SCOPE_ENV, "grow=full")
        with capture_logs() as logs:
            groww = resolve_feed_scope("groww")
            union = resolve_read_scope()
        assert groww.token == SCOPE_INDEX4  # admission unchanged
        assert union.bounded()  # read union unchanged — grow is not ACTIVE
        warned = [e for e in logs if e["event"] == "feed_scope.unregistered_feed_binding"]
        assert len(warned) == 1
        assert warned[0]["feed"] == "grow"

    def test_dhan_full_scope_known_but_adapterless_excluded_and_warned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``dhan=full`` — scope-known but adapterless: excluded + warned (the

        half-widened split state closed)."""
        monkeypatch.setenv(FEED_SCOPE_ENV, "dhan=full")
        with capture_logs() as logs:
            groww = resolve_feed_scope("groww")
            union = resolve_read_scope()
        assert groww.token == SCOPE_INDEX4  # admission unchanged
        assert union.bounded()  # read union unchanged — dhan is NOT active
        assert union.admitted_underlyings == INDEX4_UNDERLYINGS
        wide = [e for e in logs if e["event"] == "feed_scope.inactive_feed_wide_binding"]
        assert len(wide) == 1
        assert wide[0]["feed"] == "dhan"
        # dhan is scope-KNOWN, so NO unregistered_feed_binding warn.
        assert not [e for e in logs if e["event"] == "feed_scope.unregistered_feed_binding"]

    def test_wide_warn_fires_once_per_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FEED_SCOPE_ENV, "dhan=full")
        with capture_logs() as logs:
            resolve_read_scope()
            resolve_read_scope()
            resolve_feed_scope("dhan")
        assert len([e for e in logs if e["event"] == "feed_scope.inactive_feed_wide_binding"]) == 1


# ────────────────────────────────────────────────────────────────────────
# §2.4/§2.5 — the read union + the ACTIVE-feed registration seam
# ────────────────────────────────────────────────────────────────────────


class TestActiveFeedsAndReadUnion:
    def test_active_seeded_groww(self) -> None:
        assert active_feeds() == frozenset({"groww"})

    def test_note_feed_registered_grows_and_normalizes(self) -> None:
        note_feed_registered("  GDFL ")
        assert active_feeds() == frozenset({"groww", "gdfl"})
        note_feed_registered("")  # defensive no-op
        assert active_feeds() == frozenset({"groww", "gdfl"})

    def test_default_union_is_groww_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(FEED_SCOPE_ENV, raising=False)
        union = resolve_read_scope()
        assert union.feed == READ_UNION_FEED
        assert union.bounded()
        assert union.admitted_underlyings == INDEX4_UNDERLYINGS
        assert union.fno_underlyings == INDEX4_FNO_UNDERLYINGS

    def test_any_active_full_makes_union_unbounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FEED_SCOPE_ENV, "gdfl=full")
        note_feed_registered("gdfl")
        union = resolve_read_scope()
        assert not union.bounded()
        assert union.admits_stocks()

    def test_primed_memo_widens_on_registration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The §2.3 memo law's teeth: prime the union memo BEFORE registering.

        An env-string-only union memo key would serve the stale narrow union
        until restart, killing the §2.4 "takes effect with zero further
        changes" promise (the Rev-4 primed-memo arm).
        """
        monkeypatch.setenv(FEED_SCOPE_ENV, "gdfl=full")
        primed = resolve_read_scope()  # memo primed while gdfl is NOT active
        assert primed.bounded()
        rev_before = scope_revision()
        note_feed_registered("gdfl")  # the §2.5 registration event — no env flip
        widened = resolve_read_scope()
        assert not widened.bounded()  # the pre-laid wide binding folds in
        assert scope_revision() != rev_before  # the cache-key token moved

    def test_scope_revision_stable_without_changes(self) -> None:
        assert scope_revision() == scope_revision()

    def test_unbounded_scope_admits_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The ``None``-set arms: a ``full`` scope admits any pair (pass-through)."""
        monkeypatch.setenv(FEED_SCOPE_ENV, "full")
        scope = resolve_feed_scope("groww")
        assert scope.admits_underlying(Exchange.NSE, "RELIANCE")
        assert scope.admits_fno_underlying(Exchange.BSE, "ANYTHING")


class TestReadScopeSnapshot:
    """The §2.5 PAIR LAW — ``read_scope_snapshot`` (DEC-UNIVERSE-INDEX-ONLY-001).

    A memo-key site that threads the read-union scope AND folds the revision
    token must derive BOTH from ONE ``(env, active)`` snapshot: two separate
    ``resolve_read_scope()`` / ``scope_revision()`` reads racing a §2.5
    registration could pair the pre-widening narrow scope with the
    post-widening token — caching the narrow payload UNDER the post-widening
    key, so post-registration requests keep hitting stale narrow data until
    an unrelated generation bump."""

    def test_parity_with_split_reads_in_quiescent_state(self) -> None:
        scope, token = read_scope_snapshot()
        assert scope == resolve_read_scope()
        assert token == scope_revision()

    def test_one_active_set_read_binds_both_halves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The coherence mechanism itself: the PAIR is derived from the FIRST
        ``active_feeds()`` snapshot — a mutating stub that widens on every
        LATER read (a registration landing mid-pair) proves the token still
        matches the scope's own snapshot. A split
        ``(resolve_read_scope(), scope_revision())`` implementation pairs the
        narrow scope with the POST-widening token here — the exact stale-key
        interleaving the pre-fix call sites allowed."""
        import hashlib

        monkeypatch.setenv(FEED_SCOPE_ENV, "gdfl=full")
        calls: list[int] = []

        def _mutating() -> frozenset[str]:
            calls.append(1)
            if len(calls) == 1:
                return frozenset({"groww"})
            return frozenset({"groww", "gdfl"})  # a registration mid-pair

        monkeypatch.setattr(feed_scope, "active_feeds", _mutating)
        scope, token = read_scope_snapshot()
        # The scope is the FIRST snapshot's union (groww-only → bounded)...
        assert scope.bounded()
        # ... and the token is the SAME snapshot's token — never the widened
        # set's (independent oracle: the documented (raw, active) basis).
        narrow = hashlib.sha256(f"{'gdfl=full'!r}|groww".encode()).hexdigest()[:16]
        wide = hashlib.sha256(f"{'gdfl=full'!r}|gdfl,groww".encode()).hexdigest()[:16]
        assert token == narrow
        assert token != wide

    def test_registration_between_snapshots_moves_both(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(FEED_SCOPE_ENV, "gdfl=full")
        narrow_scope, narrow_token = read_scope_snapshot()
        assert narrow_scope.bounded()
        note_feed_registered("gdfl")  # the §2.5 widening event — no env flip
        wide_scope, wide_token = read_scope_snapshot()
        assert not wide_scope.bounded()
        assert wide_token != narrow_token

    def test_memo_key_sites_bind_the_snapshot_pair(self) -> None:
        """Source pin (the #1645 idiom): every dashboard site that threads the
        read scope AND folds the token binds ``read_scope_snapshot()`` — the
        split adjacent-pair shape may not reappear. Lone token folds whose
        compute resolves the scope lazily stay legal (benign ordering)."""
        root = Path(__file__).resolve().parents[2] / "brutex" / "dashboard"
        app_src = (root / "app.py").read_text(encoding="utf-8")
        warm_src = (root / "discovery_warm.py").read_text(encoding="utf-8")
        for src in (app_src, warm_src):
            assert "scope_token = scope_revision()" not in src
            assert "scope_token=scope_revision()" not in src
        assert app_src.count("read_scope_snapshot()") == 5  # 3 warm + movers + preopen
        assert warm_src.count("read_scope_snapshot()") == 1


class TestRegistrationSeam:
    """The REAL ``VendorProfileRegistry.register`` fires the §2.5 seam."""

    def test_register_fires_seam_and_folds_pre_laid_binding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from brutex.feeds.vendor_profile import VendorProfileRegistry

        from tests.fakes.vendor_profiles import synthetic_feed_profile

        monkeypatch.setenv(FEED_SCOPE_ENV, "gdfl=full")
        primed = resolve_read_scope()
        assert primed.bounded()
        registry = VendorProfileRegistry()
        registry.register(synthetic_feed_profile(tmp_path, vendor="gdfl"))
        assert "gdfl" in active_feeds()
        assert not resolve_read_scope().bounded()

    def test_refused_registration_never_grows_active(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The §2.5 seam-ordering law: a REFUSED registration leaves ACTIVE,

        the union AND ``scope_revision()`` unchanged."""
        from brutex.feeds.vendor_profile import VendorProfileError, VendorProfileRegistry

        from tests.fakes.vendor_profiles import synthetic_feed_profile

        monkeypatch.setenv(FEED_SCOPE_ENV, "gdfl=full")
        registry = VendorProfileRegistry()
        registry.register(synthetic_feed_profile(tmp_path, vendor="testfeed"))
        active_before = active_feeds()
        rev_before = scope_revision()
        union_before = resolve_read_scope()
        # A DUPLICATE registration is refused BEFORE state lands.
        with pytest.raises(VendorProfileError, match="already registered"):
            registry.register(synthetic_feed_profile(tmp_path, vendor="testfeed"))
        assert active_feeds() == active_before
        assert scope_revision() == rev_before
        assert resolve_read_scope() == union_before


# ────────────────────────────────────────────────────────────────────────
# Mirror pins (§2.1)
# ────────────────────────────────────────────────────────────────────────


class TestMirrorPins:
    def test_index4_underlyings_mirror_universe(self) -> None:
        """INDEX4_UNDERLYINGS ↔ universe.INDEX_UNDERLYINGS (the literal-mirror pin)."""
        from brutex.universe import INDEX_UNDERLYINGS

        assert INDEX4_UNDERLYINGS == INDEX_UNDERLYINGS

    def test_index4_fno_underlyings_mirror_non_reference_v1(self) -> None:
        """INDEX4_FNO ↔ the NON-reference V1_INDICES pairs (its OWN pin — §2.1).

        Deliberately NOT ``BRUTE_FORCE_INDEX_UNDERLYINGS`` equality-by-name:
        the pin derives from V1_INDICES so a fifth F&O index / a
        reference_only flip moves it.
        """
        from brutex.universe import V1_INDICES

        assert (
            frozenset((u.exchange, u.underlying_symbol) for u in V1_INDICES if not u.reference_only)
            == INDEX4_FNO_UNDERLYINGS
        )

    def test_trio_token_banned_in_scope_modules(self) -> None:
        """The C8 VIX-killer ban: no BRUTE_FORCE_INDEX_UNDERLYINGS in the scope leaves."""
        for rel in ("brutex/config/feed_scope.py", "brutex/data/scope_filter.py"):
            source = (Path(__file__).resolve().parents[2] / rel).read_text(encoding="utf-8")
            assert "BRUTE_FORCE_INDEX_UNDERLYINGS" not in source, rel


# ────────────────────────────────────────────────────────────────────────
# The A2 monotonicity property: admits(index4) ⇒ admits(full)
# ────────────────────────────────────────────────────────────────────────


class TestMonotonicity:
    _FULL = FeedScope(
        feed="groww", token=SCOPE_FULL, admitted_underlyings=None, fno_underlyings=None
    )
    _INDEX4 = FeedScope(
        feed="groww",
        token=SCOPE_INDEX4,
        admitted_underlyings=INDEX4_UNDERLYINGS,
        fno_underlyings=INDEX4_FNO_UNDERLYINGS,
    )

    @pytest.mark.parametrize("exchange", [Exchange.NSE, Exchange.BSE])
    @pytest.mark.parametrize(
        "instrument_type",
        [InstrumentType.IDX, InstrumentType.EQ, InstrumentType.FUT, InstrumentType.CE],
    )
    @pytest.mark.parametrize(
        "symbol", ["NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX", "RELIANCE", "NIFTYIT"]
    )
    def test_admits_index4_implies_admits_full(
        self, exchange: Exchange, instrument_type: InstrumentType, symbol: str
    ) -> None:
        """index4 ⊆ full over BOTH predicates — incl. the BSE-derivative arm

        (the A2 conscious correction: verbatim-legacy `full` refused BSE legs
        while index4 admits the (BSE, SENSEX) legs — an inverted lattice)."""
        from brutex.universe import is_in_fetch_scope, is_universe_underlying

        inst = _instrument(symbol, instrument_type, exchange)
        fno = frozenset({(Exchange.NSE, "RELIANCE"), (Exchange.NSE, "NIFTY")})
        if is_universe_underlying(inst, fno, scope=self._INDEX4):
            assert is_universe_underlying(inst, fno, scope=self._FULL)
        if is_in_fetch_scope(inst, fno, scope=self._INDEX4):
            assert is_in_fetch_scope(inst, fno, scope=self._FULL)


# ────────────────────────────────────────────────────────────────────────
# §2.1 — the EMPIRICAL closure import pin
# ────────────────────────────────────────────────────────────────────────


class TestClosureImportPin:
    def test_leaf_imports_are_stdlib_models_structlog_only(self) -> None:
        """(a) the AST half: the leaf's own imports ⊆ {stdlib, brutex.models, structlog}."""
        leaf = Path(feed_scope.__file__).read_text(encoding="utf-8")
        allowed_brutex = {"brutex.models"}
        roots: set[str] = set()
        for node in ast.walk(ast.parse(leaf)):
            if isinstance(node, ast.Import):
                roots.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module is not None
                roots.add(node.module)
        for module in roots:
            top = module.split(".")[0]
            ok = module in allowed_brutex or top == "structlog" or top in sys.stdlib_module_names
            assert ok, f"feed_scope.py imports {module!r} — outside the §2.1 allowed set"
        assert "brutex.models" in roots  # anti-vacuous: the walk saw the imports

    def test_config_package_closure_excludes_universe_and_dashboard(self) -> None:
        """(b) the SUBPROCESS half — the empirical pin: any future config/-,

        ports- or data-init edit that re-creates the universe/dashboard cycle
        fails this regardless of which file carried it (§2.1)."""
        code = (
            "import sys\n"
            "import brutex.config\n"
            "assert 'brutex.universe' not in sys.modules, 'universe in config closure'\n"
            "bad = [m for m in sys.modules if m.startswith('brutex.dashboard')]\n"
            "assert not bad, f'dashboard modules in config closure: {bad}'\n"
            "print('closure clean')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "closure clean" in result.stdout
