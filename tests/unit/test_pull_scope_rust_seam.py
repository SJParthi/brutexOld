"""The pull_scope ↔ ``brutex_pull_state`` Rust seam (DEC-RUST-PULL-STATE-001, Wave 1B).

Covers all four seam arcs on every consult site (flag off · flag on + served
· flag on + degrade · real-wheel parity), the facade accessors' refuse-poison
gates, and the CROSS-LANGUAGE ANCHOR DRIFT PIN: the Rust crate's anchor
literals must equal the Python module's must equal what the real
``pipeline_pull`` producers emit — a `_complete_note` wording refactor must
force a conscious re-pin on BOTH languages, never a silent desync.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from brutex import pull_state_rust
from brutex.dashboard import pull_scope as ps
from brutex.dashboard.pull_scope import (
    GREEKS_NOT_RUN_NOTE,
    SCOPE_FNO_ONLY,
    SCOPE_FULL,
    SCOPE_SPOT_ONLY,
    CrossverifyTailOutcome,
    build_fno_only_note,
    build_spot_only_note,
    crossverify_clause,
    normalize_pull_scope,
    rewrite_market_hours_note,
)
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Iterator

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUST_SRC = _REPO_ROOT / "rust" / "brutex-pull-state" / "src" / "pull_scope.rs"
_JSON_FIXTURE = _REPO_ROOT / "tests" / "golden" / "fixtures" / "pull_scope_note_surgery.json"

_TAIL = CrossverifyTailOutcome(status="started", human="producer started (poll x)")

_FLAG = "BRUTEX_PULL_STATE_RUST"


@pytest.fixture(autouse=True)
def _clean_warn_registry() -> Iterator[None]:
    pull_state_rust.reset_for_tests()
    yield
    pull_state_rust.reset_for_tests()


class _RecordingBomb:
    """A ``sys.modules`` plant that RECORDS every attribute touch.

    ``__getattr__`` raises :class:`AttributeError` (the special-method-
    appropriate type — CodeQL ``py/unexpected-raise-in-special-method``
    forbids an AssertionError here), so the TEETH are the recorded
    ``touched`` list: the flag-OFF pins assert it stays EMPTY, and
    :class:`TestBombTeeth` proves a flag-ON consult genuinely records
    (the spy cannot go silently blind).
    """

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> object:
        object.__getattribute__(self, "touched").append(name)
        raise AttributeError(name)


# ── flag OFF: byte-identical + never imports (the bomb spy) ──────────────────


class TestFlagOffNeverTouchesKernel:
    def test_all_four_sites_flag_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_FLAG, raising=False)
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        assert normalize_pull_scope("  spot-only  ") == SCOPE_SPOT_ONLY
        with pytest.raises(ValueError, match="unknown pull scope"):
            normalize_pull_scope("junk")
        drifted = "deferred — no anchors here"
        assert rewrite_market_hours_note(drifted) == drifted
        note = build_spot_only_note(
            "pipeline complete — spot clean (1 pair(s))"
            ", metadata 0 expiries + 0 contracts, historical 0 bars over 0 contract(s)",
            _TAIL,
        )
        assert note.startswith("spot-only pipeline complete — ")
        fno = build_fno_only_note("pipeline complete — spot clean (0 pair(s)), metadata 4")
        assert fno.startswith("F&O-only pipeline complete — ")
        assert bomb.touched == []

    def test_explicit_falsy_flag_also_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "0")
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        assert normalize_pull_scope(None) == SCOPE_FULL
        assert rewrite_market_hours_note("x") == "x"
        assert build_fno_only_note("y") == "y"
        assert bomb.touched == []


class TestBombTeeth:
    def test_flag_on_consult_records_the_touch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The spy has TEETH: a flag-ON consult genuinely records (so the
        flag-OFF empty-list assertions above can never pass vacuously)."""
        monkeypatch.setenv(_FLAG, "1")
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        # The AttributeError-raising bomb reads as a stale wheel → the
        # oracle still serves (never a crash) — AND the touch is recorded.
        assert normalize_pull_scope("spot-only") == SCOPE_SPOT_ONLY
        assert "scope_token" in bomb.touched


# ── flag ON: the kernel verdict is SERVED (deliberately-wrong fakes) ─────────


def _fake_kernel(**functions: object) -> SimpleNamespace:
    return SimpleNamespace(**functions)


class TestFlagOnServesKernel:
    def test_scope_token_verdict_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(scope_token=lambda token: 2)
        )
        # A deliberately-WRONG verdict is served — proving the kernel arm.
        assert normalize_pull_scope("full") == SCOPE_FNO_ONLY

    @pytest.mark.parametrize(
        ("code", "expected"), [(0, SCOPE_FULL), (1, SCOPE_SPOT_ONLY), (2, SCOPE_FNO_ONLY)]
    )
    def test_scope_every_code_maps(
        self, monkeypatch: pytest.MonkeyPatch, code: int, expected: str
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(scope_token=lambda token: code)
        )
        assert normalize_pull_scope("anything") == expected

    def test_scope_unknown_code_falls_to_oracle_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 255 verdict falls through to the ORACLE — the {token!r} refusal
        is composed Python-side, and a drifted kernel can never turn a KNOWN
        token into a raise (the oracle re-parses it)."""
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(scope_token=lambda token: 255)
        )
        with pytest.raises(ValueError, match=r"unknown pull scope 'junk—token'"):
            normalize_pull_scope("junk—token")
        # The drift-safe law: a known token mis-verdicted 255 still serves.
        assert normalize_pull_scope("spot-only") == SCOPE_SPOT_ONLY

    def test_market_hours_rewrite_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(market_hours_note_rewrite=lambda note, repl: f"KERNEL[{note}|{repl}]"),
        )
        out = rewrite_market_hours_note("n", phases_replacement="p")
        assert out == "KERNEL[n|p]"

    def test_spot_surgery_served_with_python_appends(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(spot_only_note_surgery=lambda note: ("SURGERY", True, True)),
        )
        with capture_logs() as logs:
            note = build_spot_only_note("whatever", _TAIL)
        # Step 3 (Greeks + crossverify) stays Python-side on the kernel arm.
        assert note == f"SURGERY, {GREEKS_NOT_RUN_NOTE}{crossverify_clause(_TAIL)}"
        assert [e for e in logs if e["event"] == "pull_scope.note_surgery_miss"] == []

    def test_spot_surgery_kernel_booleans_fire_the_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(spot_only_note_surgery=lambda note: ("SURGERY", True, False)),
        )
        with capture_logs() as logs:
            build_spot_only_note("half-drifted base", _TAIL)
        miss = [e for e in logs if e["event"] == "pull_scope.note_surgery_miss"]
        assert len(miss) == 1
        assert miss[0]["prefix_present"] is True
        assert miss[0]["fno_clause_present"] is False
        assert miss[0]["base_note"] == "half-drifted base"

    def test_fno_surgery_served_and_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(fno_only_note_surgery=lambda note: ("FNO-SURGERY", False, True)),
        )
        with capture_logs() as logs:
            note = build_fno_only_note("x")
        assert note == "FNO-SURGERY"  # no appends on the fno axis, ever
        miss = [e for e in logs if e["event"] == "pull_scope.note_surgery_miss"]
        assert len(miss) == 1
        assert miss[0]["prefix_present"] is False
        assert miss[0]["spot_clause_present"] is True


# ── flag ON: every degrade arm falls to the byte-identical oracle ────────────


class TestFlagOnDegrades:
    def test_missing_symbol_degrades_wheel_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(sys.modules, "brutex_pull_state", _fake_kernel())
        with capture_logs() as logs:
            assert normalize_pull_scope("fno-only") == SCOPE_FNO_ONLY
        events = [e for e in logs if e["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT]
        assert [e["reason"] for e in events] == ["wheel_stale"]
        assert events[0]["symbol"] == "scope_token"

    def test_kernel_raise_degrades_kernel_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")

        def _boom(note: str, repl: str) -> str:
            raise RuntimeError("kernel exploded")

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(market_hours_note_rewrite=_boom)
        )
        with capture_logs() as logs:
            assert rewrite_market_hours_note("no anchors") == "no anchors"
        events = [e for e in logs if e["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT]
        assert [e["reason"] for e in events] == ["kernel_error"]
        assert events[0]["log_level"] == "error"

    @pytest.mark.parametrize(
        "poison",
        ["spot-only", True, 7, -1, None, 2.0],
        ids=["str-pun", "bool-pun", "out-of-vocab", "negative", "none", "float"],
    )
    def test_scope_poisoned_verdict_degrades(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        """The refuse-poison gate: EXACT int + the exact 4-code vocabulary —
        notably ``True`` (an ``int`` subclass) must never pun as spot-only."""
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(scope_token=lambda token: poison)
        )
        with capture_logs() as logs:
            assert normalize_pull_scope("full") == SCOPE_FULL  # the oracle serves
        assert [
            e["reason"]
            for e in logs
            if e["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT
        ] == ["kernel_invalid"]

    def test_rewrite_poisoned_result_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(market_hours_note_rewrite=lambda note, repl: 5),
        )
        with capture_logs() as logs:
            assert rewrite_market_hours_note("pass-through") == "pass-through"
        assert [
            e["reason"]
            for e in logs
            if e["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT
        ] == ["kernel_invalid"]

    @pytest.mark.parametrize(
        "poison",
        [
            ["note", True, True],
            ("note", True),
            ("note", True, True, True),
            ("note", 1, True),
            ("note", True, 0),
            (b"note", True, True),
            None,
            "note",
        ],
        ids=[
            "list-pun",
            "arity-2",
            "arity-4",
            "int-for-bool-1",
            "int-for-bool-2",
            "bytes-note",
            "none",
            "bare-str",
        ],
    )
    def test_surgery_poisoned_triple_degrades(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(
                spot_only_note_surgery=lambda note: poison,
                fno_only_note_surgery=lambda note: poison,
                # The degraded builder's ORACLE arm re-consults the rewrite
                # seam; identity == the oracle for these anchor-free inputs
                # (keeps the captured degrade reasons to the one under test).
                market_hours_note_rewrite=lambda note, repl: note,
            ),
        )
        base = (
            "pipeline complete — spot clean (1 pair(s))"
            ", metadata 0 expiries + 0 contracts, historical 0 bars over 0 contract(s)"
        )
        with capture_logs() as logs:
            note = build_spot_only_note(base, _TAIL)
        assert note.startswith("spot-only pipeline complete — ")  # the oracle served
        assert [
            e["reason"]
            for e in logs
            if e["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT
        ] == ["kernel_invalid"]
        pull_state_rust.reset_for_tests()
        with capture_logs() as logs2:
            fno = build_fno_only_note("pipeline complete — spot clean (0 pair(s)), m")
        assert fno.startswith("F&O-only pipeline complete — ")
        assert [
            e["reason"]
            for e in logs2
            if e["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT
        ] == ["kernel_invalid"]

    def test_wheel_missing_degrades_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)  # import → ImportError
        with capture_logs() as logs:
            assert normalize_pull_scope("full") == SCOPE_FULL
            assert build_fno_only_note("x") == "x"
        assert [
            e["reason"]
            for e in logs
            if e["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT
        ] == ["wheel_missing"]  # warn-ONCE across consecutive consults


# ── the cross-language anchor drift pin ──────────────────────────────────────


def _rust_source_logical() -> str:
    """The Rust module source with string-literal line continuations folded
    (a ``\\``-newline in a Rust string literal skips the newline + leading
    whitespace), so full-literal containment checks work."""
    src = _RUST_SRC.read_text(encoding="utf-8")
    return re.sub(r"\\\n\s*", "", src)


class TestCrossLanguageAnchorDriftPin:
    def test_rust_literals_equal_python_anchors(self) -> None:
        """EVERY drift-pinned Python anchor literal appears VERBATIM as a
        Rust string literal in ``src/pull_scope.rs`` — the mandatory pin of
        the design §4.5: a `_complete_note` wording refactor must desync
        LOUDLY, on both languages, never silently."""
        rust = _rust_source_logical()
        from tests.golden._pull_scope_harness import ANCHOR_NAMES

        for name in ANCHOR_NAMES:
            literal = getattr(ps, name)
            assert isinstance(literal, str)
            assert f'"{literal}"' in rust, f"Rust literal drifted for {name}: {literal!r}"

    def test_python_anchors_equal_producer_output(self) -> None:
        """The producer coupling leg: the anchors appear in the REAL
        ``pipeline_pull`` renders (never a re-derived wording)."""
        from brutex.dashboard.pipeline_pull import _market_hours_stop_note

        deferral = _market_hours_stop_note()
        assert ps._MARKET_HOURS_WINDOW_CLAUSE in deferral
        assert ps._MARKET_HOURS_PHASES_CLAUSE in deferral
        assert ps._MARKET_HOURS_RESUME_CLAUSE in deferral
        # The completed-chain anchors ride the frozen golden (driven through
        # the real run_full_pipeline → _complete_note by the harness).
        table = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        clean_note = table["spot_only_surgery"][0]["base_note"]
        assert ps._FULL_PREFIX in clean_note
        assert ps._NOOP_FNO_CLAUSE in clean_note
        fno_note = table["fno_only_surgery"][0]["base_note"]
        assert ps._NOOP_SPOT_CLAUSE in fno_note

    def test_scope_vocabulary_pinned_in_rust(self) -> None:
        rust = _rust_source_logical()
        for token in sorted(ps.KNOWN_PULL_SCOPES):
            assert f'"{token}"' in rust


# ── real-wheel parity (skips until the integrator's merged wheel) ────────────


def _require_wave1b_wheel() -> object:
    wheel = pytest.importorskip(
        "brutex_pull_state", reason="brutex_pull_state wheel not installed"
    )
    if not hasattr(wheel, "scope_token"):
        pytest.skip("installed brutex_pull_state wheel predates Wave 1B (no scope_token)")
    return wheel


class TestRealWheelParity:
    """Flag-ON vs flag-OFF byte-equality over the frozen golden corpus +
    a hypothesis differential — importorskip'd + capability-probed, so these
    SKIP until the integrator ships the merged Wave-1B wheel."""

    def test_golden_corpus_parity_flag_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _require_wave1b_wheel()
        table = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        monkeypatch.setenv(_FLAG, "1")
        for row in table["scope"]:
            token = row["token"]
            if "error" in row:
                with pytest.raises(ValueError):
                    normalize_pull_scope(token)
            else:
                assert normalize_pull_scope(token) == row["ok"]
        for row in table["market_hours_rewrite"]:
            phases = row["phases"]
            replacement = {
                "SPOT": ps._MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE,
                "FNO": ps._MARKET_HOURS_PHASES_FNO_ONLY_CLAUSE,
            }.get(phases, phases[2:])
            assert (
                rewrite_market_hours_note(row["note"], phases_replacement=replacement)
                == row["expected"]
            )
        tail = CrossverifyTailOutcome(
            status=table["tail"]["status"], human=table["tail"]["human"]
        )
        for row in table["spot_only_surgery"]:
            with capture_logs() as logs:
                assert build_spot_only_note(row["base_note"], tail) == row["full"]
            missed = any(e["event"] == "pull_scope.note_surgery_miss" for e in logs)
            assert missed == row["miss_warned"]
        for row in table["fno_only_surgery"]:
            with capture_logs() as logs:
                assert build_fno_only_note(row["base_note"]) == row["note"]
            missed = any(e["event"] == "pull_scope.note_surgery_miss" for e in logs)
            assert missed == row["miss_warned"]

    def test_hypothesis_differential_flag_on_vs_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _require_wave1b_wheel()
        from hypothesis import given, settings
        from hypothesis import strategies as st

        fragments = st.sampled_from(
            [
                ps._FULL_PREFIX,
                ps._NOOP_FNO_CLAUSE,
                ps._NOOP_SPOT_CLAUSE,
                ps._STOP_FNO_CLAUSE,
                ps._MARKET_HOURS_WINDOW_CLAUSE,
                ps._MARKET_HOURS_PHASES_CLAUSE,
                ps._MARKET_HOURS_RESUME_CLAUSE,
                "spot clean (1 pair(s))",
                " — ",
                "x",
                "",
            ]
        )
        notes = st.lists(fragments, min_size=0, max_size=6).map("".join)

        @settings(max_examples=200, deadline=None)
        @given(note=notes, text=st.text(max_size=40))
        def _check(note: str, text: str) -> None:
            base = note + text
            monkeypatch.setenv(_FLAG, "0")
            with capture_logs():
                oracle_spot = build_spot_only_note(base, _TAIL)
                oracle_fno = build_fno_only_note(base)
                oracle_mh = rewrite_market_hours_note(base)
            monkeypatch.setenv(_FLAG, "1")
            with capture_logs():
                assert build_spot_only_note(base, _TAIL) == oracle_spot
                assert build_fno_only_note(base) == oracle_fno
                assert rewrite_market_hours_note(base) == oracle_mh

        _check()

    def test_scope_token_differential_flag_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _require_wave1b_wheel()
        from hypothesis import given, settings
        from hypothesis import strategies as st

        tokens = st.one_of(
            st.none(),
            st.text(max_size=20),
            st.sampled_from(
                ["full", "spot-only", "fno-only", "  spot-only  ", "\x1cfull\x1f", "junk"]
            ),
        )

        @settings(max_examples=300, deadline=None)
        @given(token=tokens)
        def _check(token: str | None) -> None:
            monkeypatch.setenv(_FLAG, "0")
            try:
                oracle: str | None = normalize_pull_scope(token)
                oracle_raised = False
            except ValueError:
                oracle = None
                oracle_raised = True
            monkeypatch.setenv(_FLAG, "1")
            if oracle_raised:
                with pytest.raises(ValueError):
                    normalize_pull_scope(token)
            else:
                assert normalize_pull_scope(token) == oracle

        _check()
