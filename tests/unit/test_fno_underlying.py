"""Tests for the spot→F&O underlying-symbol resolver (DEC-FNO-UNDERLYING-RESOLVE-001).

The COMMON single source of truth for the Groww F&O ``underlying_symbol`` form(s) to
hand ``get_expiries`` / ``get_contracts`` for an F&O-eligible spot row. The bug it
closes: the metadata pull keyed the chain on the SPOT ``trading_symbol``
(``BAJAJ-AUTO`` / ``NIFTYJR``) where Groww wants the F&O underlying spelling
(``BAJAJAUTO`` / ``NIFTYNXT50``), so ``get_expiries`` returned a clean-but-EMPTY 200
→ 0 expiries → the whole underlying silently missed.

Pure functions, no I/O — :func:`fno_underlying_forms` is tested with an injected
alias map; :func:`resolve_fno_underlying_forms` is tested against the production map
(monkeypatching the operator override env). 100% line+branch on
``brutex.dashboard.fno_underlying``.
"""

from __future__ import annotations

import pytest
from brutex.dashboard.fno_underlying import (
    FNO_INDEX_ALIASES_OVERRIDE_ENV,
    fno_underlying_forms,
    primary_fno_underlying,
    resolve_fno_underlying_forms,
)

# A small fixed alias map mirroring the sourced production defaults.
_ALIASES = {"NIFTYJR": "NIFTYNXT50", "NIFTYMIDSELECT": "MIDCPNIFTY"}


# ── fno_underlying_forms ─────────────────────────────────────────────────────


def test_hyphenated_stock_normalizes_to_fno_underlying() -> None:
    """``BAJAJ-AUTO`` → first form ``BAJAJAUTO`` (the case/space/hyphen bridge)."""
    forms = fno_underlying_forms("BAJAJ-AUTO", aliases=_ALIASES)
    assert forms[0] == "BAJAJAUTO"
    # The original spelling is appended as a try-also fallback (distinct from the
    # normalized form, so both appear).
    assert forms == ("BAJAJAUTO", "BAJAJ-AUTO")


def test_spaced_index_normalizes() -> None:
    """``NIFTY NEXT 50`` (spaced, no alias) → ``NIFTYNEXT50`` first."""
    forms = fno_underlying_forms("NIFTY NEXT 50", aliases={})
    assert forms == ("NIFTYNEXT50", "NIFTY NEXT 50")


def test_alias_takes_precedence_over_normalization() -> None:
    """A genuine-abbreviation alias (``NIFTYJR``→``NIFTYNXT50``) wins position one.

    Normalization alone could not bridge ``NIFTYJR`` → ``NIFTYNXT50`` (different
    abbreviation), so the alias is the head form; the normalized + original spellings
    follow as fallbacks.
    """
    forms = fno_underlying_forms("NIFTYJR", aliases=_ALIASES)
    assert forms[0] == "NIFTYNXT50"
    # NIFTYJR normalizes to itself (no space/hyphen) and is the original too, so the
    # full ordered, de-duped tuple is (alias, NIFTYJR).
    assert forms == ("NIFTYNXT50", "NIFTYJR")


def test_alias_keyed_case_insensitively() -> None:
    """The alias key is matched on the UPPER-cased, trimmed spot spelling."""
    assert fno_underlying_forms("  niftyjr  ", aliases=_ALIASES)[0] == "NIFTYNXT50"


def test_canonical_symbol_collapses_to_one_form() -> None:
    """An alias-free symbol with no space/hyphen yields exactly ONE form (no extra calls).

    NIFTY / BANKNIFTY / SENSEX normalize to themselves and equal the original, so the
    de-dup collapses all three candidates to a single form — byte-identical to the
    pre-fix single-symbol behaviour.
    """
    assert fno_underlying_forms("NIFTY", aliases=_ALIASES) == ("NIFTY",)
    assert fno_underlying_forms("BANKNIFTY", aliases={}) == ("BANKNIFTY",)
    assert fno_underlying_forms("SENSEX", aliases={}) == ("SENSEX",)


def test_never_empty_and_alias_equal_to_normalized_dedups() -> None:
    """When the alias EQUALS the normalized form, the result de-dups to one+original.

    Guards the inner ``_add`` de-dup branch: an alias whose value already equals the
    normalized spelling must not appear twice.
    """
    # spot "FOO-BAR" normalizes to "FOOBAR"; alias also maps to "FOOBAR".
    forms = fno_underlying_forms("FOO-BAR", aliases={"FOO-BAR": "FOOBAR"})
    assert forms == ("FOOBAR", "FOO-BAR")


def test_blank_alias_value_is_ignored() -> None:
    """A blank alias value is not added (the ``if form`` guard in ``_add``)."""
    forms = fno_underlying_forms("ABC", aliases={"ABC": "   "})
    # The stripped-blank alias contributes nothing; normalization == original == "ABC".
    assert forms == ("ABC",)


# ── primary_fno_underlying ───────────────────────────────────────────────────


def test_primary_is_head_of_forms() -> None:
    assert primary_fno_underlying("BAJAJ-AUTO", aliases=_ALIASES) == "BAJAJAUTO"
    assert primary_fno_underlying("NIFTYJR", aliases=_ALIASES) == "NIFTYNXT50"
    assert primary_fno_underlying("NIFTY", aliases={}) == "NIFTY"


# ── DEC-FNO-UNDERLYING-EMPTY-GUARD-001: the blank-symbol degenerate ───────────


def test_primary_fno_underlying_empty_symbol_returns_itself_never_raises() -> None:
    """The guarantee-audit BUG-3 repro: ``primary_fno_underlying("")`` must be TOTAL.

    Pre-fix, ``fno_underlying_forms("")`` returned ``()`` (the ``_add`` truthiness
    gate drops every falsy form) and ``forms[0]`` raised ``IndexError`` — one blank
    vendor row poisoned every whole-map consumer. The empty string now resolves to
    itself verbatim — an inert key no coverage / registry / kind row ever matches,
    never a fabricated form (Golden Rule #1).
    """
    assert primary_fno_underlying("", aliases={}) == ""
    assert primary_fno_underlying("", aliases=_ALIASES) == ""


def test_fno_underlying_forms_empty_symbol_yields_no_forms() -> None:
    """``fno_underlying_forms("")`` DELIBERATELY stays ``()`` (the corrected docstring law).

    No vendor form can be derived from nothing — a forms-iterating consumer (the
    metadata fetch loop, the mapping-integrity fold) then issues ZERO SDK calls for
    a blank symbol; ``("",)`` would issue a vendor query with an empty symbol.
    """
    assert fno_underlying_forms("", aliases={}) == ()
    assert fno_underlying_forms("", aliases=_ALIASES) == ()


def test_primary_fno_underlying_whitespace_only_unchanged() -> None:
    """Whitespace-only symbols were NEVER crashing and stay byte-identical.

    ``"  "`` is truthy, so ``_add("  ")`` appends it → forms ``("  ",)`` → primary
    ``"  "`` (a junk-but-inert key). The BUG-3 fallback branch must NOT fire for it
    — the guard is scoped to the literal empty string only.
    """
    assert fno_underlying_forms("  ", aliases={}) == ("  ",)
    assert primary_fno_underlying("  ", aliases={}) == "  "


def test_primary_fno_underlying_nonempty_byte_identity() -> None:
    """Every NON-empty symbol is byte-identical to the pre-guard behaviour.

    Structural: the ``forms[0] if forms else`` fallback fires only for the literal
    ``""`` (``_add(spot_symbol)`` always appends a truthy string). The table kills a
    ``forms[-1]`` / always-fallback mutant on the guarded return.
    """
    assert primary_fno_underlying("BAJAJ-AUTO", aliases=_ALIASES) == "BAJAJAUTO"
    assert primary_fno_underlying("M&M", aliases={}) == "M&M"
    assert primary_fno_underlying("NIFTY", aliases={}) == "NIFTY"
    assert primary_fno_underlying("NIFTYJR", aliases=_ALIASES) == "NIFTYNXT50"


# ── resolve_fno_underlying_forms (production alias map) ──────────────────────


def test_resolve_uses_sourced_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no override env, the sourced defaults bridge NIFTYJR → NIFTYNXT50."""
    monkeypatch.delenv(FNO_INDEX_ALIASES_OVERRIDE_ENV, raising=False)
    assert resolve_fno_underlying_forms("NIFTYJR")[0] == "NIFTYNXT50"
    assert resolve_fno_underlying_forms("BAJAJ-AUTO")[0] == "BAJAJAUTO"
    assert resolve_fno_underlying_forms("NIFTY") == ("NIFTY",)


def test_resolve_honours_operator_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator JSON override env is merged into the resolved map."""
    monkeypatch.setenv(FNO_INDEX_ALIASES_OVERRIDE_ENV, '{"NEWSPOT": "NEWFNO"}')
    assert resolve_fno_underlying_forms("NEWSPOT")[0] == "NEWFNO"
