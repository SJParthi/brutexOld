"""Engine-entry symbol scope guard (DEC-CORP-ACTION-GATE-001 / ADR-0016).

The corporate-action future-blocker gate: the engine has ZERO corporate-action
adjustment code (correct for the 3-index scope; catastrophic for single
stocks), so every production sweep entry must refuse an out-of-scope symbol
LOUDLY. These tests pin the allowed set, the normalization, the refusal
message contract (the literal phrase + decision id + ADR number + every
offender named), the structlog WARNING, and the set-agreement with the
lake-side truth ``BRUTE_FORCE_INDEX_SYMBOLS`` so the two can never drift.
"""

from __future__ import annotations

import pytest
import structlog
from brutex.engine.scope_guard import (
    ENGINE_SCOPE_SYMBOLS,
    EngineScopeError,
    ensure_engine_scope,
)


class TestAllowedSymbols:
    def test_three_index_symbols_admitted(self) -> None:
        assert ensure_engine_scope(["NIFTY", "BANKNIFTY", "SENSEX"]) == (
            "NIFTY",
            "BANKNIFTY",
            "SENSEX",
        )

    def test_single_symbol_admitted(self) -> None:
        assert ensure_engine_scope(["NIFTY"]) == ("NIFTY",)

    def test_case_normalization(self) -> None:
        assert ensure_engine_scope(["nifty", "BankNifty"]) == ("NIFTY", "BANKNIFTY")

    def test_whitespace_normalization(self) -> None:
        assert ensure_engine_scope(["  SENSEX  ", "\tNIFTY\n"]) == ("SENSEX", "NIFTY")

    def test_input_order_and_duplicates_preserved(self) -> None:
        # De-dup belongs to the downstream axes (SweepGridConfig), not here.
        assert ensure_engine_scope(["NIFTY", "NIFTY"]) == ("NIFTY", "NIFTY")

    def test_allowed_set_is_exactly_the_three_indexes(self) -> None:
        assert frozenset({"NIFTY", "BANKNIFTY", "SENSEX"}) == ENGINE_SCOPE_SYMBOLS


class TestRefusals:
    def test_reference_only_stock_refused(self) -> None:
        with pytest.raises(EngineScopeError) as excinfo:
            ensure_engine_scope(["RELIANCE"])
        msg = str(excinfo.value)
        assert "corporate-action adjustment layer missing" in msg
        assert "DEC-CORP-ACTION-GATE-001" in msg
        assert "ADR-0016" in msg
        assert "'RELIANCE'" in msg
        assert "NIFTY / BANKNIFTY / SENSEX" in msg

    def test_india_vix_refused(self) -> None:
        # India VIX is REFERENCE ONLY (CLAUDE.md §2 Golden Rule #3) — never
        # a brute-force instrument.
        with pytest.raises(EngineScopeError, match="INDIAVIX"):
            ensure_engine_scope(["INDIAVIX"])

    def test_groww_key_form_refused(self) -> None:
        # The engine takes bare LAKE symbols, not EXCH-SYMBOL groww keys.
        with pytest.raises(EngineScopeError, match="NSE-NIFTY"):
            ensure_engine_scope(["NSE-NIFTY"])

    def test_blank_symbol_refused(self) -> None:
        with pytest.raises(EngineScopeError, match="corporate-action adjustment layer missing"):
            ensure_engine_scope([""])

    def test_whitespace_only_symbol_refused(self) -> None:
        with pytest.raises(EngineScopeError, match="corporate-action adjustment layer missing"):
            ensure_engine_scope(["   "])

    def test_empty_iterable_refused(self) -> None:
        with pytest.raises(EngineScopeError, match="<no symbols>"):
            ensure_engine_scope([])

    def test_mixed_refusal_names_every_offender(self) -> None:
        with pytest.raises(EngineScopeError) as excinfo:
            ensure_engine_scope(["NIFTY", "RELIANCE", "BANKNIFTY", "TCS"])
        msg = str(excinfo.value)
        assert "'RELIANCE'" in msg
        assert "'TCS'" in msg
        # The in-scope symbols are NOT named as refused.
        assert "'NIFTY'" not in msg
        assert "'BANKNIFTY'" not in msg

    def test_refusal_is_a_value_error(self) -> None:
        # The house config-validation pattern (sweep_orchestrator raises
        # ValueError) — existing except ValueError handling keeps working.
        assert issubclass(EngineScopeError, ValueError)

    def test_refusal_emits_one_scope_refused_warning(self) -> None:
        with structlog.testing.capture_logs() as logs, pytest.raises(EngineScopeError):
            ensure_engine_scope(["RELIANCE"])
        events = [entry for entry in logs if entry["event"] == "engine.scope_refused"]
        assert len(events) == 1
        assert events[0]["refused"] == ["'RELIANCE'"]
        assert events[0]["decision"] == "DEC-CORP-ACTION-GATE-001"


class TestLakeSideAgreement:
    def test_engine_scope_agrees_with_lake_side_truth(self) -> None:
        """The engine set == the lake-side groww keys, exchange prefix stripped.

        ``BRUTE_FORCE_INDEX_SYMBOLS`` (adapters/groww/constants.py) is the
        lake-side truth ``{"NSE-NIFTY", "NSE-BANKNIFTY", "BSE-SENSEX"}``; the
        engine guard holds the bare trading symbols. This pin makes a drift
        between the two sets a test failure, since the engine deliberately
        does not import adapters (layering).
        """
        from brutex.adapters.groww.constants import BRUTE_FORCE_INDEX_SYMBOLS

        bare = frozenset(key.split("-", 1)[1] for key in BRUTE_FORCE_INDEX_SYMBOLS)
        assert bare == ENGINE_SCOPE_SYMBOLS


class TestAdversarialCorpus:
    """Hard-coded homoglyph/encoding vectors with independent expectations.

    The property suite's oracle mirrors the implementation's own
    ``strip().upper()`` normalization, so a normalization-semantics bug shared
    by both sides would be unfalsifiable there. This corpus pins concrete
    expectations derived by hand (escape-spelled, never re-computed through
    the guard's own expression).
    """

    @pytest.mark.parametrize(
        "symbol",
        [
            "NIFTY\u200b",  # trailing ZERO WIDTH SPACE — not str.strip() whitespace
            "\u200bNIFTY",  # leading ZERO WIDTH SPACE
            "NIF TY",  # interior ASCII space survives strip()
            "\uff2e\uff29\uff26\uff34\uff39",  # fullwidth NIFTY — upper() keeps fullwidth
            "B\u0410NKNIFTY",  # Cyrillic A homoglyph for Latin A
            "\u0130NDIAVIX",  # Turkish dotted capital I — still not in scope
        ],
    )
    def test_homoglyph_and_encoding_variants_refused(self, symbol: str) -> None:
        with pytest.raises(EngineScopeError, match="corporate-action adjustment layer missing"):
            ensure_engine_scope([symbol])

    def test_nbsp_padding_is_stripped_whitespace_and_admitted(self) -> None:
        # U+00A0 IS str.strip() whitespace (str.isspace() -> True), so an
        # NBSP-padded allowed symbol normalizes to itself — a true positive.
        assert ensure_engine_scope(["\u00a0NIFTY\u00a0"]) == ("NIFTY",)

    def test_turkish_dotless_i_uppercases_to_ascii_i_and_is_admitted(self) -> None:
        # '\u0131' ("i" without a dot).upper() == "I" under Python's
        # locale-independent str casing, so "n\u0131fty" normalizes to the
        # genuine "NIFTY" — documented normalization behavior (the normalized
        # form IS the in-scope symbol), not a bypass: the engine sweeps the
        # normalized name only.
        assert ensure_engine_scope(["n\u0131fty"]) == ("NIFTY",)
