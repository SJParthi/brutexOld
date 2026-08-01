"""EXTREME-permutation hardening for the strict-Rust gate + the census.

DEC-RUST-STRICT-NO-FALLBACK-001 / DEC-RUST-ENGAGEMENT-CENSUS-001.

``tests/unit/test_rust_strict.py`` pins the gate's HAPPY SHAPES — five falsy
literals, five truthy, three blanks, five junk values — and its posture pin
walks the live census. This file is the adversarial complement: it attacks the
same two modules along the axes the shape tests do not reach, and every claim
below was MEASURED on this tree before it was asserted.

What is here that is nowhere else:

* **The whole env plane, not a sample.** A hypothesis ``@given`` over
  ``st.text()`` pins the ONLY-ROUTE-TO-FALSE invariant — ``strict_mode()`` is
  ``False`` *if and only if* ``env_flag_tristate(raw)`` is ``False``. Every
  other shape (blank, junk, unicode digit, 10 KB, embedded newline) collapses
  to ``True`` by construction rather than by enumeration, so a future edit that
  invents a sixth falsy spelling fails here even if nobody thinks to add it to
  a parametrize list.
* **The whitespace-padded kill switch.** ``"\\n0\\t"`` and ``" off "`` resolve
  ``False`` (``env_flag_tristate`` strips before matching). Nothing pinned
  that, and it is the shape an operator actually produces — a value pasted
  into a shell heredoc or a YAML block scalar.
* **The ``_CONTEXT_REPR_CAP`` boundary at the exact byte.** The existing suite
  proves a 100 000-char value is cut; it never proves WHERE. A ``repr`` of
  exactly 200 chars must survive intact and 201 must lose exactly one — an
  off-by-one in either direction is invisible to a huge-value test.
* **Hostile ``__repr__``.** A context value whose ``__repr__`` raises, or
  returns a non-``str``, propagates that raw error INSTEAD of
  ``RustFallbackRefused``. Pinned as CURRENT behaviour with the honest reading
  spelled out in :class:`TestRefuseHostileRepr` — the DEC's core guarantee
  (Python does not serve) still holds because the call still raises, but the
  message naming the facade, the reason and the kill switch is LOST. Changing
  that is a behaviour change and needs its own DEC; pinning it stops the change
  happening by accident.
* **The absorption hazard, mechanized.** ``rust_strict``'s own module docstring
  documents that ``RustFallbackRefused`` derives from ``Exception`` and is
  therefore swallowed by a broad ``except Exception``. That is prose. Here it
  is a test — and its mirror, that the ``BaseException``-only classes stay
  disjoint, so a future "make it un-absorbable" edit cannot quietly reparent it
  under ``BaseException`` and break every ``except BruteXError`` boundary.
* **Concurrency and reentrancy.** ``strict_mode()`` is documented "one
  ``os.environ`` probe, no memo, no global state" — 32 threads reading while a
  33rd flips the var pins that there is no torn read and no crash. ``refuse``
  called from inside an ``except`` handling a prior refusal pins the
  ``__context__`` chain, so a facade's second choke point cannot mask the
  first.
* **The census over synthetic rows.** Every verdict permutation, duplicate
  module names, the empty list, an unimportable module and a source-less
  module — the shapes a real 39-facade walk never produces, so a regression in
  ``summarize``/``strict_failures``/``prove``/``render_table`` on any of them
  would otherwise surface only on the day it actually happens.

Env hygiene: every env write goes through ``monkeypatch`` (the fixture) or
``pytest.MonkeyPatch.context()`` (inside ``@given``, where a function-scoped
fixture is a hypothesis health-check error), so ``tests/conftest.py``'s
``_isolate_rust_strict_env`` belt has nothing to undo. The ONE test that writes
``os.environ`` raw — the concurrency probe, which must mutate from a background
thread — restores in a ``finally`` and is the reason that belt exists.
"""

from __future__ import annotations

import os
import sys
import threading
import types
from typing import TYPE_CHECKING, Final, Literal

import pytest
from brutex.config.env_flags import FALSY_LITERALS, TRUTHY_LITERALS, env_flag_tristate
from brutex.config.rust_strict import (
    _CONTEXT_REPR_CAP,  # pyright: ignore[reportPrivateUsage]
    RUST_STRICT_DEFAULT,
    RUST_STRICT_ENV,
    RustFallbackRefused,
    refuse,
    refuse_if_strict,
    strict_mode,
)
from brutex.models import BruteXError
from brutex.observability.rust_census import (
    CONFIGURED,
    DEGRADED,
    ENGAGED,
    OFF,
    REQUIRED,
    UNKNOWN,
    FacadeStatus,
    census,
    prove,
    render_table,
    strict_failures,
    summarize,
)
from hypothesis import given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path


# ──────────────────────────────────────────────────────────────────────
# 1. strict_mode() — the whole env plane
# ──────────────────────────────────────────────────────────────────────
#
# The table below is MEASURED, not reasoned: each row was executed against the
# shipped ``strict_mode()`` before it was written down. The four laws it
# enumerates, in the order ``strict_mode`` applies them:
#
#   explicit falsy  -> False   (the ONLY route to False)
#   explicit truthy -> True
#   blank/unset     -> RUST_STRICT_DEFAULT (True)
#   anything else   -> True    (the inverted junk polarity)
#
# Both "blank" and "junk" land on True today, but through DIFFERENT branches
# (``raw.strip()`` falsy vs truthy), so both arms are enumerated — collapsing
# them would leave one branch pinned only by coverage, not by intent.

#: ``(raw, expected)`` — the falsy plane. Case, padding and vocabulary.
# ``st.characters`` types ``exclude_categories`` as a ``Collection[Literal[...]]``
# of Unicode general-category names, so a bare ``("Cs",)`` infers as
# ``tuple[str]`` and fails ``mypy --strict``. Naming it once, typed, keeps the
# three call sites honest AND keeps the surrogate exclusion in ONE place: a
# lone surrogate cannot round-trip through ``os.environ``, so generating one
# would test the OS, not the gate.
_NO_SURROGATES: Final[tuple[Literal["Cs"], ...]] = ("Cs",)

_FALSY_CASES: Final[tuple[str, ...]] = (
    "0",
    "false",
    "FALSE",
    "False",
    "fAlSe",
    "no",
    "NO",
    "nO",
    "off",
    "OFF",
    "Off",
    "oFf",
    " 0 ",
    "\n0\t",
    " off ",
    "\toff\n",
)

#: The truthy plane — same axes, mirrored.
_TRUTHY_CASES: Final[tuple[str, ...]] = (
    "1",
    "true",
    "TRUE",
    "True",
    "tRuE",
    "yes",
    "YES",
    "yEs",
    "on",
    "ON",
    "On",
    "oN",
    " 1 ",
    "\n1\t",
    " on ",
)

#: Blank — ``raw.strip()`` is empty, so the DEFAULT applies (not the junk arm).
_BLANK_CASES: Final[tuple[str, ...]] = ("", " ", "\t", "\n", "\r\n", "   \t\n  ")

#: Junk — recognised by nothing, so strict stays ON. Includes the near-misses
#: (a truthy/falsy PREFIX, a numeric neighbour) and the unicode traps: the
#: Arabic-Indic and fullwidth digit ones are NOT ``"1"`` (the literal sets are
#: ASCII and ``str.lower()`` does not fold them), so they are junk, not truthy.
_JUNK_CASES: Final[tuple[str, ...]] = (
    "00",
    "01",
    "2",
    "-1",
    "1.0",
    "y",
    "n",
    "t",
    "f",
    "yes!",
    "ON!",
    "offf",
    "0x0",
    "None",
    "null",
    "garbage",
    "١",  # ARABIC-INDIC DIGIT ONE — not ASCII "1"
    "１",  # FULLWIDTH DIGIT ONE — not ASCII "1"
    "1\n2",  # internal newline: strip() does not remove it
    "0 1",
    "ünïcödé",  # valid utf-8, not ascii
    "x" * 10_240,  # 10 KB
)


class TestStrictModeEnvPermutations:
    """The measured table: every env shape, its verdict, and which law fired."""

    def test_unset_is_the_shipped_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)
        assert strict_mode() is RUST_STRICT_DEFAULT is True

    @pytest.mark.parametrize("raw", _FALSY_CASES)
    def test_explicit_falsy_is_the_only_kill_switch(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(RUST_STRICT_ENV, raw)
        assert strict_mode() is False
        assert env_flag_tristate(raw) is False, "the verdict must come from the tristate law"

    @pytest.mark.parametrize("raw", _TRUTHY_CASES)
    def test_explicit_truthy_is_on(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(RUST_STRICT_ENV, raw)
        assert strict_mode() is True
        assert env_flag_tristate(raw) is True

    @pytest.mark.parametrize("raw", _BLANK_CASES)
    def test_blank_takes_the_default_branch_not_the_junk_branch(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """Blank and junk both yield True — via DIFFERENT arms. Pin both."""
        monkeypatch.setenv(RUST_STRICT_ENV, raw)
        assert env_flag_tristate(raw) is None
        assert not raw.strip(), "precondition: this is the blank arm, not the junk arm"
        assert strict_mode() is RUST_STRICT_DEFAULT

    @pytest.mark.parametrize("raw", _JUNK_CASES)
    def test_junk_keeps_strict_on(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        """The deliberate inversion of the house fail-safe-OFF junk law.

        Elsewhere a typo must never arm an experimental kernel. Here the
        dangerous outcome points the other way — a typo'd kill switch must not
        silently hand the work back to Python — so junk stays ON.
        """
        monkeypatch.setenv(RUST_STRICT_ENV, raw)
        assert env_flag_tristate(raw) is None
        assert raw.strip(), "precondition: this is the junk arm, not the blank arm"
        assert strict_mode() is True

    def test_the_case_permutation_set_is_complete_over_the_vocabulary(self) -> None:
        """Anti-drift: every shipped literal appears in the tables above.

        A sixth falsy spelling added to ``FALSY_LITERALS`` without a row here
        would leave the new kill-switch spelling unpinned; this fails instead.
        """
        assert {raw.strip().lower() for raw in _FALSY_CASES} == set(FALSY_LITERALS)
        assert {raw.strip().lower() for raw in _TRUTHY_CASES} == set(TRUTHY_LITERALS)

    def test_the_verdict_is_read_per_call_never_memoized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cached verdict would leak the first caller's env into every later one."""
        for expected, raw in ((False, "0"), (True, "1"), (False, "off"), (True, "garbage")):
            monkeypatch.setenv(RUST_STRICT_ENV, raw)
            assert strict_mode() is expected


class TestStrictModeUnreachableEnvShapes:
    """Shapes the OS refuses — pinned so the docs cannot claim them as covered.

    MEASURED: ``os.environ`` rejects an embedded NUL (``ValueError``) and a lone
    surrogate (``UnicodeEncodeError``) before ``strict_mode`` is ever reached.
    They are therefore *unreachable through the env*, and the honest statement
    is that the gate never sees them — not that it handles them. The pure
    classifier is still exercised directly, so the layer that CAN see such a
    string is pinned.
    """

    @pytest.mark.parametrize("raw", ["\x00", "0\x00", "a\x00b"])
    def test_nul_cannot_reach_the_gate_through_the_environment(self, raw: str) -> None:
        with pytest.raises(ValueError, match="null byte"), pytest.MonkeyPatch.context() as mp:
            mp.setenv(RUST_STRICT_ENV, raw)

    def test_a_lone_surrogate_cannot_reach_the_gate_either(self) -> None:
        with pytest.raises(UnicodeEncodeError), pytest.MonkeyPatch.context() as mp:
            mp.setenv(RUST_STRICT_ENV, "\ud800")

    @pytest.mark.parametrize("raw", ["\x00", "0\x00", "\ud800"])
    def test_the_pure_classifier_still_calls_them_junk(self, raw: str) -> None:
        """A NUL does not strip, so ``"0\\x00"`` is NOT the kill switch."""
        assert env_flag_tristate(raw) is None


class TestStrictModeProperty:
    """The ONLY-ROUTE-TO-FALSE invariant over the whole text plane."""

    @settings(max_examples=300)
    @given(
        raw=st.text(
            alphabet=st.characters(exclude_categories=_NO_SURROGATES, exclude_characters="\x00"),
            max_size=40,
        )
    )
    def test_strict_mode_is_false_iff_the_tristate_is_false(self, raw: str) -> None:
        """``strict_mode()`` is ``False`` IFF ``env_flag_tristate(raw)`` is ``False``.

        The biconditional is the whole guarantee: the left-to-right half says
        no junk, blank or unicode shape can ever turn the gate off; the
        right-to-left half says the documented kill switch always works.
        Enumeration cannot state that — this can.

        The alphabet excludes the two shapes ``os.environ`` itself refuses
        (surrogates, NUL — pinned separately above), so every generated value
        is genuinely reachable in production.
        """
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv(RUST_STRICT_ENV, raw)
            assert (strict_mode() is False) == (env_flag_tristate(raw) is False)

    @settings(max_examples=200)
    @given(
        raw=st.text(
            alphabet=st.characters(exclude_categories=_NO_SURROGATES, exclude_characters="\x00"),
            max_size=40,
        )
    )
    def test_the_verdict_is_always_a_real_bool(self, raw: str) -> None:
        """Never a truthy non-bool: callers do ``if strict_mode():`` AND ``is True``."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv(RUST_STRICT_ENV, raw)
            verdict = strict_mode()
        assert type(verdict) is bool


# ──────────────────────────────────────────────────────────────────────
# 2. refuse() — context bounding at the exact byte
# ──────────────────────────────────────────────────────────────────────


def _message(**context: object) -> str:
    """The refusal text for *context*, or fail loudly if nothing was raised."""
    with pytest.raises(RustFallbackRefused) as excinfo:
        refuse("brutex.some_rust", "wheel_missing", **context)
    return str(excinfo.value)


def _emitted(text: str, key: str) -> str:
    """The rendered ``key=<repr>`` segment, sliced out of the parenthetical."""
    start = text.index(f"{key}=") + len(key) + 1
    return text[start : text.index(").")]


class TestRefuseContextBounding:
    """``_CONTEXT_REPR_CAP`` bites at exactly 200 characters — no more, no less."""

    @pytest.mark.parametrize("repr_len", [_CONTEXT_REPR_CAP - 1, _CONTEXT_REPR_CAP])
    def test_a_repr_at_or_below_the_cap_survives_intact(self, repr_len: int) -> None:
        # repr("x" * n) is n + 2 chars (the two quotes).
        value = "x" * (repr_len - 2)
        emitted = _emitted(_message(blob=value), "blob")
        assert emitted == repr(value)
        assert len(emitted) == repr_len

    @pytest.mark.parametrize("overshoot", [1, 2, 100, 100_000])
    def test_a_repr_above_the_cap_is_cut_to_exactly_the_cap(self, overshoot: int) -> None:
        value = "x" * (_CONTEXT_REPR_CAP - 2 + overshoot)
        emitted = _emitted(_message(blob=value), "blob")
        assert len(emitted) == _CONTEXT_REPR_CAP
        assert emitted == repr(value)[:_CONTEXT_REPR_CAP]

    def test_the_surrounding_message_is_never_truncated_with_the_value(self) -> None:
        """The cap bounds the CONTEXT; the operator still gets the escape hatch."""
        text = _message(blob="x" * 1_000_000)
        assert "brutex.some_rust" in text
        assert "reason: wheel_missing" in text
        assert "DEC-RUST-STRICT-NO-FALLBACK-001" in text
        assert f"{RUST_STRICT_ENV}=0" in text

    def test_every_value_is_capped_independently(self) -> None:
        """N oversized values cost N x cap, never one shared budget."""
        text = _message(a="x" * 5_000, b="y" * 5_000)
        assert len(_emitted(text, "a").split(",")[0]) == _CONTEXT_REPR_CAP
        assert len(_emitted(text, "b")) == _CONTEXT_REPR_CAP

    def test_a_500_key_dict_is_bounded_by_the_same_cap(self) -> None:
        """A whole frame handed over as one value cannot mint an unbounded line."""
        emitted = _emitted(_message(frame={f"k{i}": i for i in range(500)}), "frame")
        assert len(emitted) == _CONTEXT_REPR_CAP

    def test_a_self_referential_value_does_not_recurse(self) -> None:
        """CPython's repr guard renders ``[[...]]``; no RecursionError escapes."""
        loop: list[object] = []
        loop.append(loop)
        assert "loop=[[...]]" in _message(loop=loop)

    def test_bytes_are_repred_not_decoded(self) -> None:
        """A non-utf-8 byte cannot forge a line or blow up the formatter."""
        assert r"b'\x00\xff'" in _message(payload=b"\x00\xff")

    def test_a_newline_in_a_value_is_escaped_by_repr(self) -> None:
        assert "note='a\\nb'" in _message(note="a\nb")

    def test_zero_kwargs_emits_no_parenthetical(self) -> None:
        with pytest.raises(RustFallbackRefused) as excinfo:
            refuse("f", "r")
        text = str(excinfo.value)
        assert "()" not in text
        assert "(reason: r). " in text, "the reason parenthetical is the only one"

    def test_context_is_sorted_by_plain_string_order(self) -> None:
        """Deterministic message text — ASCII ordinal, so upper sorts before lower."""
        text = _message(b=1, a=2, C=3)
        assert "(C=3, a=2, b=1)" in text

    def test_a_kwarg_named_context_is_ordinary_context(self) -> None:
        """``**context`` swallows it; only the two POSITIONAL names collide."""
        assert "context='ctx'" in _message(context="ctx")

    @pytest.mark.parametrize("name", ["facade", "reason"])
    def test_a_kwarg_colliding_with_a_positional_is_a_loud_type_error(self, name: str) -> None:
        """Not silently dropped and not a refusal — a plain Python TypeError."""
        with pytest.raises(TypeError, match=f"multiple values for argument '{name}'"):
            refuse("f", "r", **{name: "dup"})


class TestRefuseHostileRepr:
    """A context value whose ``repr`` misbehaves — CURRENT behaviour, pinned.

    HONEST READING (Golden Rule #10). ``refuse`` does not guard the ``repr``
    call, so a raising or non-``str`` ``__repr__`` propagates ITS error instead
    of :class:`RustFallbackRefused`. The DEC's core guarantee survives — the
    call still raises, so the Python arm still does not serve — but the message
    naming the facade, the reason and the kill switch is LOST, and a caller
    catching ``RustFallbackRefused`` specifically will not catch this.

    Pinned rather than fixed: wrapping the ``repr`` would change what every
    facade's refusal says on a hostile value, which is a behaviour change owed
    its own DEC. The value of the pin is that the change cannot now happen by
    accident, in either direction.
    """

    def test_a_raising_repr_propagates_its_own_error(self) -> None:
        class RaisingRepr:
            def __repr__(self) -> str:
                raise RuntimeError("repr boom")

        with pytest.raises(RuntimeError, match="repr boom"):
            refuse("f", "r", bad=RaisingRepr())

    def test_a_non_str_repr_raises_the_interpreters_type_error(self) -> None:
        class NonStrRepr:
            def __repr__(self) -> str:
                return 42  # type: ignore[return-value]

        with pytest.raises(TypeError, match="__repr__ returned non-string"):
            refuse("f", "r", bad=NonStrRepr())

    def test_the_hostile_value_still_blocks_the_python_arm(self) -> None:
        """The guarantee that DOES hold: the call never returns."""

        class RaisingRepr:
            def __repr__(self) -> str:
                raise RuntimeError("repr boom")

        with pytest.raises(BaseException):
            refuse("f", "r", bad=RaisingRepr())


# ──────────────────────────────────────────────────────────────────────
# 3. refuse_if_strict() — the one-line insert
# ──────────────────────────────────────────────────────────────────────


class TestRefuseIfStrict:
    """Returns ``None`` exactly when the kill switch is thrown; raises otherwise."""

    @pytest.mark.parametrize("raw", _FALSY_CASES)
    def test_returns_none_only_under_an_explicit_falsy(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(RUST_STRICT_ENV, raw)
        # ``refuse_if_strict`` is annotated ``-> None``, so asserting its
        # return value is itself a type error. The absence of a raise IS
        # the assertion: under an explicit falsy the gate must let the
        # call through silently.
        refuse_if_strict("brutex.some_rust", "wheel_missing", n=3)

    @pytest.mark.parametrize("raw", [*_TRUTHY_CASES, *_BLANK_CASES, *_JUNK_CASES])
    def test_raises_for_every_other_env_shape(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(RUST_STRICT_ENV, raw)
        with pytest.raises(RustFallbackRefused, match="burn_in_mismatch"):
            refuse_if_strict("brutex.some_rust", "burn_in_mismatch")

    @settings(max_examples=200)
    @given(
        raw=st.text(
            alphabet=st.characters(exclude_categories=_NO_SURROGATES, exclude_characters="\x00"),
            max_size=24,
        ),
        facade=st.text(max_size=24),
        reason=st.text(max_size=24),
    )
    def test_the_gate_and_the_predicate_never_disagree(
        self, raw: str, facade: str, reason: str
    ) -> None:
        """For ANY env x facade x reason: raises IFF ``strict_mode()`` is True."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv(RUST_STRICT_ENV, raw)
            expected_strict = strict_mode()
            raised: RustFallbackRefused | None = None
            returned = False
            try:
                # Annotated ``-> None``; binding the call would be a
                # ``func-returns-value`` type error, so RETURNING AT ALL
                # is what we record. That is the property under test --
                # the gate either raises or falls through, never both.
                refuse_if_strict(facade, reason)
                returned = True
            except RustFallbackRefused as exc:
                raised = exc

        if expected_strict:
            assert raised is not None, "strict was on but the gate let the call through"
            assert facade in str(raised)
            assert f"reason: {reason}" in str(raised)
        else:
            assert raised is None, "the kill switch was thrown but the gate still raised"
            assert returned, "the gate neither raised nor returned"


# ──────────────────────────────────────────────────────────────────────
# 4. RustFallbackRefused — the exception taxonomy + the absorption hazard
# ──────────────────────────────────────────────────────────────────────


class TestRustFallbackRefusedTaxonomy:
    """Where the refusal sits in the hierarchy — both what it IS and is NOT."""

    def test_it_is_a_brutex_error_and_an_exception(self) -> None:
        assert issubclass(RustFallbackRefused, BruteXError)
        assert issubclass(RustFallbackRefused, Exception)

    def test_it_is_absorbed_by_a_broad_except_exception(self) -> None:
        """The DOCUMENTED hazard, mechanized (``rust_strict`` module docstring).

        Three call sites were verified reachable-and-absorbing and grew an
        ``except RustFallbackRefused: raise`` escape; ONE (``StartupSweeper.
        run_sync``) is kept absorbing on purpose. Both dispositions depend on
        this being an ``Exception`` — so the fact is pinned here rather than
        left as prose an edit could silently invalidate.
        """
        absorbed = False
        try:
            refuse("f", "r")
        except Exception:
            absorbed = True
        assert absorbed

    @pytest.mark.parametrize("other", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_it_is_disjoint_from_the_escape_classes(self, other: type[BaseException]) -> None:
        """``KeyboardInterrupt``/``SystemExit`` must stay outside ``except Exception``.

        ``MemoryError`` is an ``Exception`` (so it IS absorbable) but must never
        be confused with a refusal in either direction — the facades re-raise it
        raw.
        """
        assert not issubclass(RustFallbackRefused, other)
        assert not issubclass(other, RustFallbackRefused)

    def test_the_two_base_exception_classes_survive_an_except_exception(self) -> None:
        for escaping in (KeyboardInterrupt, SystemExit):
            assert not issubclass(escaping, Exception), (
                f"{escaping.__name__} must stay BaseException-only — a facade's "
                "broad guard would otherwise swallow an operator's Ctrl-C"
            )

    def test_str_is_the_full_message_not_a_redacted_safe_message(self) -> None:
        """``BruteXError`` supports ``safe_message`` redaction; ``refuse`` uses none.

        An operator debugging a refusal needs the facade, the reason and the
        kill switch — redacting them would defeat the message's whole purpose.
        """
        with pytest.raises(RustFallbackRefused) as excinfo:
            refuse("brutex.some_rust", "wheel_missing", n=1)
        assert excinfo.value.safe_message is None
        assert "brutex.some_rust" in str(excinfo.value)


class TestRefuseReentrancy:
    """A second refusal raised while handling the first."""

    def test_refuse_inside_an_except_chains_and_names_the_second_facade(self) -> None:
        first: RustFallbackRefused | None = None
        second: RustFallbackRefused | None = None
        try:
            refuse("brutex.first_rust", "wheel_missing")
        except RustFallbackRefused as caught_first:
            first = caught_first
            try:
                refuse("brutex.second_rust", "kernel_error", prior=type(caught_first).__name__)
            except RustFallbackRefused as caught_second:
                second = caught_second

        assert first is not None, "the first refuse() must raise"
        assert second is not None, "a refusal raised while handling one must still raise"
        assert second is not first
        assert second.__context__ is first, "the implicit chain must survive"
        assert "brutex.second_rust" in str(second)
        assert "reason: kernel_error" in str(second)
        assert "prior='RustFallbackRefused'" in str(second)

    def test_a_refusal_carrying_a_prior_refusal_as_context_is_bounded(self) -> None:
        """Even a giant prior message cannot mint an unbounded second message."""
        try:
            refuse("brutex.first_rust", "wheel_missing", blob="x" * 50_000)
        except RustFallbackRefused as first:
            emitted = _emitted(_message(prior=str(first)), "prior")
            assert len(emitted) == _CONTEXT_REPR_CAP


# ──────────────────────────────────────────────────────────────────────
# 5. Concurrency — documented "one probe, no memo, no global state"
# ──────────────────────────────────────────────────────────────────────


class TestStrictModeConcurrency:
    """32 readers against a live mutator: no crash, no torn read, no non-bool."""

    def test_concurrent_reads_during_env_mutation_are_always_a_clean_bool(self) -> None:
        original = os.environ.get(RUST_STRICT_ENV)
        stop = threading.Event()
        observed: set[object] = set()
        failures: list[str] = []
        lock = threading.Lock()

        def _flip() -> None:
            i = 0
            while not stop.is_set():
                os.environ[RUST_STRICT_ENV] = "0" if i % 2 else "1"
                i += 1

        def _read() -> None:
            local: set[object] = set()
            try:
                # 32 x 400 = 12 800 reads interleaved with a free-running
                # mutator. The loop count buys OVERLAP with the flipper, not
                # statistical power — a torn read or a non-bool would surface in
                # the first handful — so it is deliberately modest: under
                # ``--cov`` every call is traced and a 2 000-iteration loop cost
                # 22 s of the file's 26 s (MEASURED), which would dominate the
                # per-module CI gate for no added detection.
                for _ in range(400):
                    local.add(strict_mode())
            except BaseException as exc:
                with lock:
                    failures.append(repr(exc))
            with lock:
                observed.update(local)

        mutator = threading.Thread(target=_flip, name="strict-env-mutator")
        readers = [threading.Thread(target=_read, name=f"strict-reader-{i}") for i in range(32)]
        try:
            mutator.start()
            for thread in readers:
                thread.start()
            for thread in readers:
                thread.join(timeout=60)
        finally:
            stop.set()
            mutator.join(timeout=60)
            # RAW restore: the mutator writes os.environ directly (a thread
            # cannot use the test's monkeypatch), so this file owns the undo.
            # tests/conftest.py's _isolate_rust_strict_env belt is the backstop.
            if original is None:
                os.environ.pop(RUST_STRICT_ENV, None)
            else:
                os.environ[RUST_STRICT_ENV] = original

        assert not failures, f"strict_mode() raised under concurrency: {failures}"
        assert not any(thread.is_alive() for thread in readers), "a reader thread hung"
        assert observed <= {True, False}, f"torn read: {observed!r}"
        assert all(type(value) is bool for value in observed)


# ──────────────────────────────────────────────────────────────────────
# 6. The census over synthetic rows
# ──────────────────────────────────────────────────────────────────────


_ALL_VERDICTS: Final[tuple[str, ...]] = (ENGAGED, REQUIRED, DEGRADED, OFF, UNKNOWN)

#: The verdicts ``strict_failures`` must reject — the mirror of the module's
#: private ``_STRICT_FAILING``, restated here so a widening of that set without
#: a deliberate edit fails this file.
_MUST_FAIL_STRICT: Final[frozenset[str]] = frozenset({DEGRADED, OFF, UNKNOWN})


def _row(module: str, verdict: str) -> FacadeStatus:
    return FacadeStatus(
        module=module,
        env_var="BRUTEX_X",
        flag_on=True,
        crate="some_crate",
        crate_present=True,
        verdict=verdict,
    )


class TestCensusSyntheticPermutations:
    """Shapes a real 39-facade walk never produces."""

    @pytest.mark.parametrize("verdict", _ALL_VERDICTS)
    def test_strict_failures_partitions_every_verdict(self, verdict: str) -> None:
        failed = strict_failures([_row("m", verdict)])
        assert bool(failed) is (verdict in _MUST_FAIL_STRICT)

    @pytest.mark.parametrize("verdict", _ALL_VERDICTS)
    def test_is_rust_running_is_true_only_for_engaged_and_required(self, verdict: str) -> None:
        assert _row("m", verdict).is_rust_running is (verdict in (ENGAGED, REQUIRED))

    def test_the_empty_set_is_a_clean_pass_not_a_crash(self) -> None:
        assert census([]) == []
        assert summarize([]) == {}
        assert strict_failures([]) == []
        assert "NO FACADES DISCOVERED" in render_table([])

    def test_duplicate_module_names_are_counted_not_collapsed(self) -> None:
        """A duplicated row is a discovery BUG; silently deduping would hide it."""
        rows = [_row("brutex.dup", ENGAGED), _row("brutex.dup", ENGAGED)]
        assert summarize(rows) == {ENGAGED: 2}
        assert render_table(rows).count("brutex.dup") == 2

    def test_every_verdict_renders_without_a_width_error(self) -> None:
        rows = [_row(f"brutex.m{i}", verdict) for i, verdict in enumerate(_ALL_VERDICTS)]
        table = render_table(rows)
        for verdict in _ALL_VERDICTS:
            assert verdict in table
        assert "DEGRADED — Python is running where Rust was expected:" in table

    def test_a_module_that_cannot_be_imported_is_an_unknown_row_never_an_absence(
        self,
    ) -> None:
        """ "I cannot tell" must be a ROW: an absent row reads as a clean audit."""
        rows = census(["brutex.__definitely_not_a_module__"])
        assert len(rows) == 1
        assert rows[0].verdict == UNKNOWN
        assert "import failed: ModuleNotFoundError" in rows[0].detail

    def test_a_module_whose_import_raises_a_non_import_error_is_also_unknown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A module-scope raise must not abort the whole census.

        ``except Exception`` at :file:`rust_census.py:999` is deliberately wider
        than ``ImportError``: a facade that raises ``RuntimeError`` while its
        module body runs is exactly the shape an operator most needs to SEE, and
        a narrower catch would let one broken facade blank the whole audit.
        """
        name = "brutex___census_raiser"
        (tmp_path / f"{name}.py").write_text('raise RuntimeError("module scope exploded")\n')
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.delitem(sys.modules, name, raising=False)

        rows = census([name])

        assert len(rows) == 1
        assert rows[0].verdict == UNKNOWN
        assert rows[0].detail == "import failed: RuntimeError"

    def test_the_import_guard_is_import_time_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HONEST LIMIT, pinned so a future reader does not over-trust the guard.

        The ``try`` at :file:`rust_census.py:997` wraps ``import_module`` and
        nothing after it, so a module that imports cleanly and then explodes on
        attribute ACCESS propagates out of :func:`census` rather than becoming
        an UNKNOWN row. That is not a production shape — every real facade is a
        plain module — but the boundary is real and is documented here rather
        than left for someone to discover by surprise.
        """
        name = "brutex.__census_lazy_boom__"

        class _Boom(types.ModuleType):
            def __getattr__(self, attr: str) -> object:
                raise RuntimeError("module scope exploded")

        monkeypatch.setitem(sys.modules, name, _Boom(name))
        with pytest.raises(RuntimeError, match="module scope exploded"):
            census([name])

    def test_a_source_less_module_reports_unknown_with_the_honest_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No reset fn, no flag, no crate, no readable source — a row, not a drop."""
        name = "brutex.__census_bare__"
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        rows = census([name])
        assert len(rows) == 1
        assert rows[0].verdict == UNKNOWN
        assert rows[0].env_var is None
        assert rows[0].crate is None
        assert "source unreadable" in rows[0].detail

    @pytest.mark.parametrize("verdict", [REQUIRED, DEGRADED, OFF, UNKNOWN])
    def test_prove_passes_non_engaged_rows_through_untouched(self, verdict: str) -> None:
        row = _row("brutex.money_rust", verdict)
        assert prove(lambda: None, [row]) == [row]

    def test_prove_downgrades_an_uncalled_engaged_row_to_configured(self) -> None:
        """CONFIGURED is an untested seam, not a defect — never conflated."""
        out = prove(lambda: None, [_row("brutex.money_rust", ENGAGED)])
        assert out[0].verdict == CONFIGURED
        assert out[0].detail == "flag on, crate imports, uncalled"

    def test_prove_over_an_empty_row_set_still_runs_the_workload(self) -> None:
        ran: list[int] = []
        assert prove(lambda: ran.append(1), []) == []
        assert ran == [1], "the workload must run even with nothing to re-verdict"


# ──────────────────────────────────────────────────────────────────────
# 7. Anti-vacuity — this file must be able to FAIL
# ──────────────────────────────────────────────────────────────────────


class TestAntiVacuity:
    """Each mechanism above is provably capable of rejecting something."""

    def test_the_permutation_tables_are_disjoint_and_non_empty(self) -> None:
        tables = (_FALSY_CASES, _TRUTHY_CASES, _BLANK_CASES, _JUNK_CASES)
        for table in tables:
            assert table, "an empty table would make its parametrize a green no-op"
        seen: set[str] = set()
        for table in tables:
            overlap = seen & set(table)
            assert not overlap, f"a value cannot be in two verdict classes: {overlap}"
            seen.update(table)

    def test_the_cap_constant_is_the_documented_value(self) -> None:
        """If the cap moves, the boundary tests above must move with it."""
        assert _CONTEXT_REPR_CAP == 200

    def test_the_strict_failing_mirror_matches_the_module(self) -> None:
        """The local mirror is re-derived from behaviour, never trusted."""
        derived = {v for v in _ALL_VERDICTS if strict_failures([_row("m", v)])}
        assert derived == set(_MUST_FAIL_STRICT)
