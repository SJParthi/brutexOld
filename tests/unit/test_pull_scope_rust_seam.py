"""The pull_scope ↔ ``brutex_pull_state`` Rust kernel (DEC-RUST-PULL-STATE-001, flip phase).

The kernel is THE ONLY decision path for the scope-token verdict, the
market-hours rewrite and both note surgeries — the Python oracle arms were
deleted with the flip.  This suite pins: the removed-flag refusal, kernel
AUTHORITY on every consult site (a deliberately-wrong fake verdict is
SERVED), the fail-closed ``PullStateKernelError`` raise on every wheel
fault, the CROSS-LANGUAGE ANCHOR DRIFT PIN (the Rust crate's anchor
literals must equal the Python module's must equal what the real
``pipeline_pull`` producers emit — a ``_complete_note`` wording refactor
must force a conscious re-pin on BOTH languages, never a silent desync),
and the real-wheel golden replay + hypothesis differentials against IN-TEST
reimplementations of the frozen string laws (test code may reimplement the
law; production may not).
"""

# Whitebox reads of module-private anchor constants (sanctioned — the frozen
# drift-pin surface).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from brutex.dashboard import pull_scope as ps
from brutex.dashboard.pull_scope import (
    FNO_SKIPPED_CLAUSE,
    GREEKS_NOT_RUN_NOTE,
    SCOPE_FNO_ONLY,
    SCOPE_FULL,
    SCOPE_SPOT_ONLY,
    SPOT_SKIPPED_CLAUSE,
    CrossverifyTailOutcome,
    build_fno_only_note,
    build_spot_only_note,
    crossverify_clause,
    normalize_pull_scope,
    rewrite_market_hours_note,
)
from brutex.pull_state_rust import PullStateFlagRemovedError, PullStateKernelError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUST_SRC = _REPO_ROOT / "rust" / "brutex-pull-state" / "src" / "pull_scope.rs"
_JSON_FIXTURE = _REPO_ROOT / "tests" / "golden" / "fixtures" / "pull_scope_note_surgery.json"

_TAIL = CrossverifyTailOutcome(status="started", human="producer started (poll x)")

_FLAG = "BRUTEX_PULL_STATE_RUST"

#: Anchor-fragment note strategy for the real-wheel differential (module
#: level so the ``@given``-decorated methods can reference it).
_NOTE_FRAGMENT_LISTS = st.lists(
    st.sampled_from(
        [
            ps._MARKET_HOURS_WINDOW_CLAUSE,
            ps._MARKET_HOURS_PHASES_CLAUSE,
            ps._MARKET_HOURS_RESUME_CLAUSE,
            ps._FULL_PREFIX,
            ps._NOOP_FNO_CLAUSE,
            ps._NOOP_SPOT_CLAUSE,
            ps._STOP_FNO_CLAUSE,
            "spot clean (1 pair(s))",
            " — ",
            "x",
            "",
        ]
    ),
    min_size=0,
    max_size=6,
).map("".join)


class _RecordingBomb:
    """A ``sys.modules`` plant that RECORDS every attribute touch.

    ``__getattr__`` raises :class:`AttributeError` (the special-method-
    appropriate type — CodeQL ``py/unexpected-raise-in-special-method``
    forbids an AssertionError here) — the shape the facade's resolver reads
    as a wheel LACKING the symbol (``reason=wheel_stale``).  The teeth are
    BOTH the typed raise and the recorded ``touched`` list.
    """

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> object:
        object.__getattribute__(self, "touched").append(name)
        raise AttributeError(name)


def _fake_kernel(**functions: object) -> SimpleNamespace:
    """A fake ``brutex_pull_state`` module carrying the given kernels."""
    return SimpleNamespace(**functions)


# ── the removed opt-in knob refuses LOUDLY (any spelling) ────────────────────


class TestRemovedFlagRefusal:
    """A SET ``BRUTEX_PULL_STATE_RUST`` is the typed refusal at every site."""

    @pytest.mark.parametrize("value", ["1", "0", "yes", "off"])
    def test_every_site_refuses_a_set_flag(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Truthy AND falsy spellings — the knob itself is removed."""
        monkeypatch.setenv(_FLAG, value)
        with pytest.raises(PullStateFlagRemovedError, match=_FLAG):
            normalize_pull_scope("spot-only")
        with pytest.raises(PullStateFlagRemovedError, match=_FLAG):
            rewrite_market_hours_note("x")
        with pytest.raises(PullStateFlagRemovedError, match=_FLAG):
            build_spot_only_note("y", _TAIL)
        with pytest.raises(PullStateFlagRemovedError, match=_FLAG):
            build_fno_only_note("z")


# ── kernel AUTHORITY: the verdict is SERVED (deliberately-wrong fakes) ───────


class TestKernelVerdictServed:
    def test_scope_token_verdict_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A deliberately-WRONG verdict is served — proving the consult."""
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(scope_token=lambda token: 2)
        )
        assert normalize_pull_scope("full") == SCOPE_FNO_ONLY

    @pytest.mark.parametrize(
        ("code", "expected"), [(0, SCOPE_FULL), (1, SCOPE_SPOT_ONLY), (2, SCOPE_FNO_ONLY)]
    )
    def test_scope_every_code_maps(
        self, monkeypatch: pytest.MonkeyPatch, code: int, expected: str
    ) -> None:
        """Every vocabulary code maps through the ONE Python code→token table."""
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(scope_token=lambda token: code)
        )
        assert normalize_pull_scope("anything") == expected

    def test_scope_unknown_code_composes_the_python_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """255 is the kernel's HONEST unknown-token verdict — a real
        vocabulary answer, never a degrade: Python composes the ``{token!r}``
        refusal for it (CPython repr is never re-minted in Rust).  The
        kernel is the AUTHORITY, so even a known token mis-verdicted 255 by
        a fake refuses (the pre-flip oracle-re-parse rescue died with the
        oracle — the real kernel is drift-pinned by the golden corpus)."""
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(scope_token=lambda token: 255)
        )
        with pytest.raises(ValueError, match=r"unknown pull scope 'junk—token'"):
            normalize_pull_scope("junk—token")
        with pytest.raises(ValueError, match=r"unknown pull scope 'spot-only'"):
            normalize_pull_scope("spot-only")

    def test_market_hours_rewrite_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The rewrite output is the kernel's, verbatim."""
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(market_hours_note_rewrite=lambda note, repl: f"KERNEL[{note}|{repl}]"),
        )
        out = rewrite_market_hours_note("n", phases_replacement="p")
        assert out == "KERNEL[n|p]"

    def test_spot_surgery_served_with_python_appends(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Step 3 (Greeks + crossverify) stays Python-side composition."""
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(spot_only_note_surgery=lambda note: ("SURGERY", True, True)),
        )
        with capture_logs() as logs:
            note = build_spot_only_note("whatever", _TAIL)
        assert note == f"SURGERY, {GREEKS_NOT_RUN_NOTE}{crossverify_clause(_TAIL)}"
        assert [e for e in logs if e["event"] == "pull_scope.note_surgery_miss"] == []

    def test_spot_surgery_kernel_booleans_fire_the_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``note_surgery_miss`` WARN stays Python, fed by the kernel booleans."""
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

    def test_fno_surgery_served_clean_no_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(fno_only_note_surgery=lambda note: ("CLEAN", True, True)),
        )
        with capture_logs() as logs:
            assert build_fno_only_note("whatever") == "CLEAN"
        assert [e for e in logs if e["event"] == "pull_scope.note_surgery_miss"] == []

    def test_fno_surgery_served_and_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No appends on the fno axis, ever — and the WARN keys spot_clause."""
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(fno_only_note_surgery=lambda note: ("FNO-SURGERY", False, True)),
        )
        with capture_logs() as logs:
            note = build_fno_only_note("x")
        assert note == "FNO-SURGERY"
        miss = [e for e in logs if e["event"] == "pull_scope.note_surgery_miss"]
        assert len(miss) == 1
        assert miss[0]["prefix_present"] is False
        assert miss[0]["spot_clause_present"] is True


# ── kernel faults are FAIL-CLOSED typed raises (no oracle exists) ────────────


class TestKernelFaultsFailClosed:
    def test_missing_wheel_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)  # import → ImportError
        with pytest.raises(PullStateKernelError, match="wheel_missing"):
            normalize_pull_scope("full")
        with pytest.raises(PullStateKernelError, match="wheel_missing"):
            build_fno_only_note("x")

    def test_stale_wheel_missing_symbol_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", _fake_kernel())
        with pytest.raises(PullStateKernelError, match="wheel_stale"):
            normalize_pull_scope("fno-only")

    def test_bomb_module_is_touched_and_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The recording bomb proves the consult AND the typed refusal."""
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        with pytest.raises(PullStateKernelError, match="wheel_stale"):
            normalize_pull_scope("spot-only")
        assert "scope_token" in bomb.touched

    def test_kernel_raise_raises_kernel_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(note: str, repl: str) -> str:
            raise RuntimeError("kernel exploded")

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(market_hours_note_rewrite=_boom)
        )
        with pytest.raises(PullStateKernelError, match="kernel_error"):
            rewrite_market_hours_note("no anchors")

    @pytest.mark.parametrize(
        "poison",
        ["spot-only", True, 7, -1, None, 2.0],
        ids=["str-pun", "bool-pun", "out-of-vocab", "negative", "none", "float"],
    )
    def test_scope_poisoned_verdict_raises(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        """The refuse-poison gate: EXACT int + the exact 4-code vocabulary —
        notably ``True`` (an ``int`` subclass) must never pun as spot-only."""
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(scope_token=lambda token: poison)
        )
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            normalize_pull_scope("full")

    def test_rewrite_poisoned_result_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(market_hours_note_rewrite=lambda note, repl: 5),
        )
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            rewrite_market_hours_note("pass-through")

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
    def test_surgery_poisoned_triple_raises(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        """The shared surgery-triple gate refuses every malformed shape."""
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(
                spot_only_note_surgery=lambda note: poison,
                fno_only_note_surgery=lambda note: poison,
            ),
        )
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            build_spot_only_note("base", _TAIL)
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            build_fno_only_note("base")

    def test_spot_surgery_kernel_raise_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(note: str) -> tuple[str, bool, bool]:
            raise ValueError("surgery kernel exploded")

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(spot_only_note_surgery=_boom)
        )
        with pytest.raises(PullStateKernelError, match="kernel_error"):
            build_spot_only_note("base", _TAIL)


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
        """Every scope token appears verbatim in the Rust source."""
        rust = _rust_source_logical()
        for token in sorted(ps.KNOWN_PULL_SCOPES):
            assert f'"{token}"' in rust


# ── the REAL wheel: golden replay + kernel-vs-inline-law differentials ───────


def _inline_rewrite(note: str, phases_replacement: str) -> str:
    """The frozen market-hours rewrite LAW, reimplemented in test code
    (test-only — the production copy lives in the Rust kernel)."""
    note = note.replace(ps._MARKET_HOURS_WINDOW_CLAUSE, ps._MARKET_HOURS_WINDOW_SPOT_ONLY_CLAUSE, 1)
    note = note.replace(ps._MARKET_HOURS_PHASES_CLAUSE, phases_replacement, 1)
    return note.replace(ps._MARKET_HOURS_RESUME_CLAUSE, ps._MARKET_HOURS_RESUME_SPOT_ONLY_CLAUSE, 1)


def _inline_spot_note(base_note: str, tail: CrossverifyTailOutcome) -> str:
    """The frozen spot-only note-surgery LAW, reimplemented in test code."""
    note = _inline_rewrite(base_note, ps._MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE)
    if ps._STOP_FNO_CLAUSE in note:
        note = note.replace(ps._STOP_FNO_CLAUSE, ps._STOP_FNO_SPOT_ONLY_CLAUSE, 1)
    prefix_present = ps._FULL_PREFIX in note
    clause_present = ps._NOOP_FNO_CLAUSE in note
    if clause_present:
        note = note.replace(ps._NOOP_FNO_CLAUSE, FNO_SKIPPED_CLAUSE, 1)
    if prefix_present:
        note = note.replace(ps._FULL_PREFIX, ps._SPOT_ONLY_PREFIX, 1)
    return f"{note}, {GREEKS_NOT_RUN_NOTE}{crossverify_clause(tail)}"


def _inline_fno_note(base_note: str) -> str:
    """The frozen fno-only note-surgery LAW, reimplemented in test code."""
    note = _inline_rewrite(base_note, ps._MARKET_HOURS_PHASES_FNO_ONLY_CLAUSE)
    prefix_present = ps._FULL_PREFIX in note
    clause_present = ps._NOOP_SPOT_CLAUSE in note
    if clause_present:
        note = note.replace(ps._NOOP_SPOT_CLAUSE, SPOT_SKIPPED_CLAUSE, 1)
    if prefix_present:
        note = note.replace(ps._FULL_PREFIX, ps._FNO_ONLY_PREFIX, 1)
    return note


class TestRealWheelLaw:
    """Kernel-backed output ≡ the frozen golden + the inline law (the wheel
    is REQUIRED — a missing wheel fails loudly, never skips green)."""

    def test_golden_corpus_replay(self) -> None:
        """Every frozen corpus row replays byte-identically through the kernel."""
        table = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        for row in table["scope"]:
            token = row["token"]
            if "error" in row:
                with pytest.raises(ValueError):
                    normalize_pull_scope(token)
            else:
                assert normalize_pull_scope(token) == row["ok"]
        for row in table["market_hours_rewrite"]:
            phases = str(row["phases"])
            if phases == "SPOT":
                replacement = ps._MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE
            elif phases == "FNO":
                replacement = ps._MARKET_HOURS_PHASES_FNO_ONLY_CLAUSE
            else:
                replacement = phases[2:]
            assert (
                rewrite_market_hours_note(row["note"], phases_replacement=replacement)
                == row["expected"]
            )
        tail = CrossverifyTailOutcome(status=table["tail"]["status"], human=table["tail"]["human"])
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

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(note=_NOTE_FRAGMENT_LISTS, text=st.text(max_size=40))
    def test_hypothesis_surgeries_match_the_inline_law(self, note: str, text: str) -> None:
        """Arbitrary anchor-fragment notes: kernel output ≡ the inline law
        on all three string-surgery surfaces."""
        base = note + text
        with capture_logs():
            assert build_spot_only_note(base, _TAIL) == _inline_spot_note(base, _TAIL)
            assert build_fno_only_note(base) == _inline_fno_note(base)
            assert rewrite_market_hours_note(base) == _inline_rewrite(
                base, ps._MARKET_HOURS_PHASES_SPOT_ONLY_CLAUSE
            )

    @settings(max_examples=300, deadline=None)
    @given(
        token=st.one_of(
            st.none(),
            st.text(max_size=20),
            st.sampled_from(
                ["full", "spot-only", "fno-only", "  spot-only  ", "\x1cfull\x1f", "junk"]
            ),
        )
    )
    def test_scope_token_matches_the_inline_law(self, token: str | None) -> None:
        """Kernel scope verdict ≡ the strip/default/membership law."""
        stripped = (token or "").strip()
        if not stripped:
            assert normalize_pull_scope(token) == SCOPE_FULL
        elif stripped in ps.KNOWN_PULL_SCOPES:
            assert normalize_pull_scope(token) == stripped
        else:
            with pytest.raises(ValueError, match="unknown pull scope"):
                normalize_pull_scope(token)

    def test_kernel_strip_parity_serves_the_canonical_constant(self) -> None:
        """A padded known token serves its canonical constant (the kernel's
        CPython-``str.strip``-parity trim)."""
        assert normalize_pull_scope("  spot-only  ") == SCOPE_SPOT_ONLY
        assert normalize_pull_scope("\tfno-only\n") == SCOPE_FNO_ONLY
        assert normalize_pull_scope(None) == SCOPE_FULL
