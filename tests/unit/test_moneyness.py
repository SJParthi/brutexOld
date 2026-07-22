"""Unit + property + golden tests for :mod:`brutex.data.moneyness`.

The ONE source of truth for option moneyness (ITM / ATM / OTM) + the signed ATM±N strike
offset (DEC-FNO-MONEYNESS-001), shared by the ``/db`` F&O candle browser (per-bar) and the
options-movers screen (as-of snapshot). Moneyness is **derived, time-varying** — the strike
is fixed but the spot moves, so the same ``24000 CE`` is OTM then ITM as spot climbs.

Coverage intent: **100% line + 100% branch**. Every classify branch (CE/PE × ITM/ATM/OTM,
future → NA, missing/invalid spot|strike → UNKNOWN), the DYNAMIC grid derivation (smallest
positive gap; single-strike → ``None``), the signed offset (ROUND_HALF_UP both signs), the
display label (every shape), plus Hypothesis properties that the engine **never raises** on
arbitrary input (Golden Rule #1) and the CALL/PUT mirror invariant holds.
"""

# Whitebox: the private guard `_is_positive_finite` + the display helper `_format_label`
# are imported directly so each is pinned by a focused table (the public API also exercises
# them, but the direct tests document the contract byte-exact for the mutation gate).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from brutex.data.moneyness import (
    OPTION_CALL,
    OPTION_PUT,
    Moneyness,
    MoneynessView,
    StepConfidence,
    StrikeStepResult,
    _atm_offset_python,
    _classify_graded_python,
    _derive_strike_step_python,
    _format_label,
    _is_positive_finite,
    atm_offset,
    classify,
    derive_strike_step,
    moneyness_view,
    resolve_strike_step,
)
from hypothesis import given
from hypothesis import strategies as st

# A reusable positive-Decimal strategy for strike / spot / step.
_POS = st.decimals(min_value=Decimal("0.05"), max_value=Decimal("100000"), places=2)
# Arbitrary junk for the never-raises property: None, ints, floats, strings, Decimals,
# non-finite Decimals — anything a caller might hand us.
_ANYTHING = st.one_of(
    st.none(),
    st.integers(min_value=-5, max_value=5),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=4),
    st.decimals(allow_nan=True, allow_infinity=True),
    _POS,
)


class TestModuleConstants:
    """The module-level vocabulary mirrors the documented F&O option tokens."""

    def test_option_tokens(self) -> None:
        assert OPTION_CALL == "CE"
        assert OPTION_PUT == "PE"

    def test_moneyness_values(self) -> None:
        assert Moneyness.ITM == "ITM"
        assert Moneyness.ATM == "ATM"
        assert Moneyness.OTM == "OTM"
        assert Moneyness.NA == "NA"
        assert Moneyness.UNKNOWN == "UNKNOWN"

    def test_moneyness_is_str_enum(self) -> None:
        # A StrEnum member IS its value string — JSON-serialises with no adapter.
        import json

        assert json.dumps({"m": Moneyness.OTM}) == '{"m": "OTM"}'
        assert str(Moneyness.ITM) == "ITM"


class TestIsPositiveFinite:
    """The private guard rejects everything that is not a finite, strictly-positive Decimal."""

    def test_positive_decimal_is_true(self) -> None:
        assert _is_positive_finite(Decimal("100")) is True

    @pytest.mark.parametrize(
        "value",
        [
            None,  # not a Decimal (first short-circuit)
            100,  # int, not a Decimal
            100.0,  # float, not a Decimal
            "100",  # str, not a Decimal
            Decimal("NaN"),  # Decimal but not finite (second short-circuit)
            Decimal("Infinity"),  # Decimal but not finite
            Decimal("0"),  # finite Decimal but not > 0 (third short-circuit)
            Decimal("-5"),  # finite Decimal but negative
        ],
    )
    def test_non_positive_finite_is_false(self, value: object) -> None:
        assert _is_positive_finite(value) is False


class TestClassifyOptionType:
    """A non-option type is moneyness-N/A — moneyness only applies to CE / PE."""

    @pytest.mark.parametrize("option_type", [None, "FUT", "XX", "", "ce", "pe"])
    def test_non_option_is_na(self, option_type: str | None) -> None:
        assert classify(option_type, Decimal("100"), Decimal("120")) is Moneyness.NA


class TestClassifyInvalidInputs:
    """Missing / non-positive / non-finite strike or spot surfaces honestly as UNKNOWN."""

    @pytest.mark.parametrize("strike", [None, Decimal("0"), Decimal("-5"), Decimal("NaN")])
    def test_bad_strike_is_unknown(self, strike: Decimal | None) -> None:
        assert classify(OPTION_CALL, strike, Decimal("120")) is Moneyness.UNKNOWN

    @pytest.mark.parametrize("spot", [None, Decimal("0"), Decimal("-5"), Decimal("Infinity")])
    def test_bad_spot_is_unknown(self, spot: Decimal | None) -> None:
        # Strike is valid here, so the OR short-circuits on the spot side.
        assert classify(OPTION_CALL, Decimal("100"), spot) is Moneyness.UNKNOWN


class TestClassifyCall:
    """CALL: spot above strike → ITM, below → OTM, equal → ATM (band 0)."""

    def test_call_itm(self) -> None:
        assert classify(OPTION_CALL, Decimal("100"), Decimal("120")) is Moneyness.ITM

    def test_call_otm(self) -> None:
        assert classify(OPTION_CALL, Decimal("120"), Decimal("100")) is Moneyness.OTM

    def test_call_atm_exact(self) -> None:
        assert classify(OPTION_CALL, Decimal("100"), Decimal("100")) is Moneyness.ATM


class TestClassifyPut:
    """PUT: the CALL mirror — spot below strike → ITM, above → OTM, equal → ATM."""

    def test_put_itm(self) -> None:
        assert classify(OPTION_PUT, Decimal("120"), Decimal("100")) is Moneyness.ITM

    def test_put_otm(self) -> None:
        assert classify(OPTION_PUT, Decimal("100"), Decimal("120")) is Moneyness.OTM

    def test_put_atm_exact(self) -> None:
        assert classify(OPTION_PUT, Decimal("100"), Decimal("100")) is Moneyness.ATM


class TestClassifyAtmBand:
    """The ATM band widens the at-the-money window; a negative / None band means exact."""

    def test_within_band_is_atm_call(self) -> None:
        # |spot - strike| = 10 <= band 25 → ATM even though spot != strike.
        assert (
            classify(OPTION_CALL, Decimal("100"), Decimal("110"), atm_band=Decimal("25"))
            is Moneyness.ATM
        )

    def test_within_band_is_atm_put(self) -> None:
        assert (
            classify(OPTION_PUT, Decimal("100"), Decimal("110"), atm_band=Decimal("25"))
            is Moneyness.ATM
        )

    def test_at_band_boundary_is_atm(self) -> None:
        # Exactly == band → ATM (the <= boundary is inclusive).
        assert (
            classify(OPTION_CALL, Decimal("100"), Decimal("125"), atm_band=Decimal("25"))
            is Moneyness.ATM
        )

    def test_outside_band_is_graded(self) -> None:
        # |diff| = 30 > band 25 → real ITM/OTM, not ATM.
        assert (
            classify(OPTION_CALL, Decimal("100"), Decimal("130"), atm_band=Decimal("25"))
            is Moneyness.ITM
        )

    def test_none_band_is_exact(self) -> None:
        # Default band None → Decimal(0) → only exact equality is ATM.
        assert classify(OPTION_CALL, Decimal("100"), Decimal("101")) is Moneyness.ITM

    def test_negative_band_is_treated_as_zero(self) -> None:
        # A junk negative band falls back to exact-equality ATM (band clamped to 0).
        assert (
            classify(OPTION_CALL, Decimal("100"), Decimal("100"), atm_band=Decimal("-9"))
            is Moneyness.ATM
        )
        assert (
            classify(OPTION_CALL, Decimal("100"), Decimal("101"), atm_band=Decimal("-9"))
            is Moneyness.ITM
        )


class TestDeriveStrikeStep:
    """DYNAMIC grid: the smallest positive gap of the distinct, sorted strike ladder."""

    def test_even_ladder(self) -> None:
        assert derive_strike_step([Decimal("100"), Decimal("150"), Decimal("200")]) == Decimal("50")

    def test_unsorted_input(self) -> None:
        assert derive_strike_step([Decimal("200"), Decimal("100"), Decimal("150")]) == Decimal("50")

    def test_duplicates_collapse(self) -> None:
        assert derive_strike_step([Decimal("100"), Decimal("100"), Decimal("150")]) == Decimal("50")

    def test_missing_rung_keeps_base_step(self) -> None:
        # A gap of 100 (a missing 150 rung) must NOT widen the inferred step.
        assert derive_strike_step([Decimal("100"), Decimal("150"), Decimal("250")]) == Decimal("50")

    def test_fractional_grid(self) -> None:
        assert derive_strike_step([Decimal("2.5"), Decimal("5.0"), Decimal("7.5")]) == Decimal(
            "2.5"
        )

    def test_single_strike_is_none(self) -> None:
        assert derive_strike_step([Decimal("100")]) is None

    def test_empty_is_none(self) -> None:
        assert derive_strike_step([]) is None

    def test_invalids_filtered_then_none(self) -> None:
        # Only one valid strike survives the filter → no inferable grid.
        assert derive_strike_step([None, Decimal("-5"), Decimal("NaN"), Decimal("100")]) is None

    def test_invalids_filtered_then_step(self) -> None:
        assert derive_strike_step([None, Decimal("100"), Decimal("0"), Decimal("150")]) == Decimal(
            "50"
        )

    def test_authoritative_param_prefers_the_registry_chain(self) -> None:
        # The back-compat facade over resolve_strike_step: a sparse observed ladder
        # (gap 100) is corrected by the full authoritative chain (gap 50).
        assert derive_strike_step(
            [Decimal("100"), Decimal("200")],
            authoritative_strikes=[Decimal("100"), Decimal("150"), Decimal("200")],
        ) == Decimal("50")

    def test_authoritative_param_falls_back_when_chain_degenerate(self) -> None:
        assert derive_strike_step(
            [Decimal("100"), Decimal("200")],
            authoritative_strikes=[Decimal("100")],
        ) == Decimal("100")


class TestResolveStrikeStep:
    """DEC-MONEYNESS-REGISTRY-STEP-001 — the confidence-aware step resolver."""

    def test_registry_chain_corrects_a_sparse_observed_ladder(self) -> None:
        # REGRESSION (fails before the fix, when only the observed ladder existed):
        # only alternate strikes were pulled → observed min gap 100 = 2× the true base
        # step; the FULL vendor chain restores the 50 grid, labelled high-confidence.
        observed = [Decimal("24000"), Decimal("24100")]
        chain = [Decimal("24000"), Decimal("24050"), Decimal("24100")]
        result = resolve_strike_step(observed, authoritative_strikes=chain)
        assert result.step == Decimal("50")
        assert result.confidence is StepConfidence.REGISTRY
        # The uncorrected derivation really does overestimate (the bug being fixed).
        assert derive_strike_step(observed) == Decimal("100")

    def test_no_authoritative_chain_is_observed_low_confidence(self) -> None:
        result = resolve_strike_step([Decimal("100"), Decimal("150")])
        assert result.step == Decimal("50")
        assert result.confidence is StepConfidence.OBSERVED

    def test_degenerate_chain_falls_back_to_observed(self) -> None:
        # A single-strike (or junk-only) chain cannot size the grid → observed wins,
        # still honestly labelled low-confidence.
        result = resolve_strike_step(
            [Decimal("100"), Decimal("150")], authoritative_strikes=[Decimal("100")]
        )
        assert result.step == Decimal("50")
        assert result.confidence is StepConfidence.OBSERVED

    def test_registry_chain_works_even_with_an_empty_observed_ladder(self) -> None:
        result = resolve_strike_step([], authoritative_strikes=[Decimal("100"), Decimal("150")])
        assert result.step == Decimal("50")
        assert result.confidence is StepConfidence.REGISTRY

    def test_stale_registry_chain_is_refined_by_union_with_observed(self) -> None:
        # UNION refinement (DEC-FNO-HONESTY-PACK-001): a lake partition is itself
        # vendor-grounded proof a strike exists, so a STALE/SPARSER registry chain
        # (metadata pull predating a newly-listed strike) must not overestimate.
        # Registry {24000, 24100} alone says 100; the observed 24050 partition only
        # surfaces via the UNION (min(reg, obs) would miss the cross-ladder gap).
        result = resolve_strike_step(
            [Decimal("24050")],
            authoritative_strikes=[Decimal("24000"), Decimal("24100")],
        )
        assert result.step == Decimal("50")
        assert result.confidence is StepConfidence.REGISTRY  # both ladders vendor evidence

    def test_union_never_launders_a_pure_observed_step_into_registry(self) -> None:
        # A 1-strike chain contributes NO pair of its own → the REGISTRY label is
        # never claimed for a step that only the observed ladder produced.
        result = resolve_strike_step(
            [Decimal("24000"), Decimal("24050")],
            authoritative_strikes=[Decimal("24000")],
        )
        assert result.step == Decimal("50")
        assert result.confidence is StepConfidence.OBSERVED

    def test_no_step_from_either_ladder_is_none_none(self) -> None:
        result = resolve_strike_step([Decimal("100")], authoritative_strikes=[Decimal("50")])
        assert result.step is None
        assert result.confidence is None

    def test_no_step_and_no_chain_is_none_none(self) -> None:
        result = resolve_strike_step([])
        assert result.step is None
        assert result.confidence is None

    def test_confidence_tokens_are_json_native(self) -> None:
        # StrEnum → the payload strings the frontends match on.
        assert StepConfidence.REGISTRY == "registry"
        assert StepConfidence.OBSERVED == "observed"

    def test_result_is_frozen(self) -> None:
        result = resolve_strike_step([Decimal("100"), Decimal("150")])
        with pytest.raises(FrozenInstanceError):
            result.step = Decimal("1")  # type: ignore[misc]

    @given(
        st.lists(_POS, max_size=6),
        st.one_of(st.none(), st.lists(_POS, max_size=6)),
    )
    def test_resolve_agrees_with_derive_and_labels_honestly(
        self, observed: list[Decimal], chain: list[Decimal] | None
    ) -> None:
        # Property: the step ALWAYS equals what derive_strike_step would say for the
        # winning ladder — the UNION of chain+observed when the chain itself yields a
        # step (DEC-FNO-HONESTY-PACK-001), else the observed ladder — and the
        # confidence names exactly that source (no step ⇒ no confidence): the two can
        # never disagree (one gap-math source of truth).
        result = resolve_strike_step(observed, authoritative_strikes=chain)
        registry_step = derive_strike_step(chain) if chain is not None else None
        if registry_step is not None:
            assert chain is not None
            union_step = derive_strike_step([*chain, *observed])
            assert result == StrikeStepResult(union_step, StepConfidence.REGISTRY)
            assert union_step is not None and union_step <= registry_step  # never coarser
        elif derive_strike_step(observed) is not None:
            assert result == StrikeStepResult(derive_strike_step(observed), StepConfidence.OBSERVED)
        else:
            assert result == StrikeStepResult(None, None)


class TestAtmOffset:
    """Signed ATM±N = round((strike - spot) / step), ROUND_HALF_UP, anchor-independent."""

    def test_positive_offset(self) -> None:
        assert atm_offset(Decimal("200"), Decimal("100"), Decimal("50")) == 2

    def test_negative_offset(self) -> None:
        assert atm_offset(Decimal("100"), Decimal("200"), Decimal("50")) == -2

    def test_zero_offset(self) -> None:
        assert atm_offset(Decimal("105"), Decimal("100"), Decimal("50")) == 0

    def test_half_toward_zero_positive(self) -> None:
        # 25 / 50 = 0.5 → rounds HALF TOWARD ZERO to 0 (the ATM-band edge agrees).
        assert atm_offset(Decimal("125"), Decimal("100"), Decimal("50")) == 0

    def test_half_toward_zero_negative(self) -> None:
        # -25 / 50 = -0.5 → rounds half toward zero to 0.
        assert atm_offset(Decimal("75"), Decimal("100"), Decimal("50")) == 0

    def test_above_half_rounds_out(self) -> None:
        # 26 / 50 = 0.52 → past the half-step, rounds out to 1 (consistent with OTM/ITM).
        assert atm_offset(Decimal("126"), Decimal("100"), Decimal("50")) == 1
        assert atm_offset(Decimal("74"), Decimal("100"), Decimal("50")) == -1

    @pytest.mark.parametrize("step", [None, Decimal("0"), Decimal("-5"), Decimal("NaN")])
    def test_bad_step_is_none(self, step: Decimal | None) -> None:
        assert atm_offset(Decimal("100"), Decimal("120"), step) is None

    def test_bad_strike_is_none(self) -> None:
        assert atm_offset(None, Decimal("120"), Decimal("50")) is None

    def test_bad_spot_is_none(self) -> None:
        # Step + strike valid; the spot side trips the OR.
        assert atm_offset(Decimal("100"), None, Decimal("50")) is None


class TestFormatLabel:
    """The compact display label — every shape (the view layer, never logic)."""

    def test_na_dash(self) -> None:
        assert _format_label(Moneyness.NA, None) == "—"

    def test_unknown_question(self) -> None:
        assert _format_label(Moneyness.UNKNOWN, None) == "?"

    def test_no_grid_falls_back_to_bucket(self) -> None:
        assert _format_label(Moneyness.OTM, None) == "OTM"
        assert _format_label(Moneyness.ITM, None) == "ITM"

    def test_atm_zero(self) -> None:
        assert _format_label(Moneyness.ATM, 0) == "ATM"

    def test_otm_is_bucket_plus_depth(self) -> None:
        assert _format_label(Moneyness.OTM, 2) == "OTM +2"

    def test_itm_is_bucket_minus_depth(self) -> None:
        # The sign tracks the BUCKET (ITM negative), not the raw offset — a raw +offset
        # ITM (a put) still reads "ITM -N".
        assert _format_label(Moneyness.ITM, -1) == "ITM -1"
        assert _format_label(Moneyness.ITM, 1) == "ITM -1"


class TestMoneynessView:
    """The bundled view + the frozen dataclass."""

    def test_call_itm_with_grid(self) -> None:
        v = moneyness_view(OPTION_CALL, Decimal("100"), Decimal("180"), Decimal("50"))
        assert v.moneyness is Moneyness.ITM
        assert v.atm_offset == -2
        assert v.label == "ITM -2"

    def test_nearest_rung_reads_atm(self) -> None:
        # spot 120, strike 100, step 50 → band 25, |20| <= 25 → ATM, offset 0.
        v = moneyness_view(OPTION_CALL, Decimal("100"), Decimal("120"), Decimal("50"))
        assert v.moneyness is Moneyness.ATM
        assert v.atm_offset == 0
        assert v.label == "ATM"

    def test_no_grid_classifies_but_no_offset(self) -> None:
        v = moneyness_view(OPTION_CALL, Decimal("120"), Decimal("100"), None)
        assert v.moneyness is Moneyness.OTM
        assert v.atm_offset is None
        assert v.label == "OTM"

    @pytest.mark.parametrize("option_type", ["CE", "PE"])
    def test_strike_midpoint_bucket_and_offset_agree(self, option_type: str) -> None:
        # The reconciliation regression (hostile review MAJOR): at EXACTLY the inclusive
        # ATM-band edge |spot - strike| == step/2 (a reachable on-grid point — NIFTY 50 →
        # 25.00), the bucket (ATM) and the offset (0) must AGREE, never "ATM" beside ±1.
        v = moneyness_view(option_type, Decimal("100"), Decimal("125"), Decimal("50"))
        assert v.moneyness is Moneyness.ATM
        assert v.atm_offset == 0
        assert v.label == "ATM"
        # And one step BEYOND the edge grades + offsets consistently (never ATM + 0).
        beyond = moneyness_view(option_type, Decimal("100"), Decimal("130"), Decimal("50"))
        assert beyond.moneyness is not Moneyness.ATM
        assert beyond.atm_offset != 0

    def test_atm_bucket_always_carries_zero_offset(self) -> None:
        # moneyness_view never emits the contradictory (ATM, non-zero) pair — so the
        # label is always a bare "ATM", never "ITM -…"/"OTM +…" for an ATM bar.
        v = moneyness_view(OPTION_CALL, Decimal("100"), Decimal("124.99"), Decimal("50"))
        assert v.moneyness is Moneyness.ATM
        assert v.atm_offset == 0
        assert v.label == "ATM"

    def test_future_is_na(self) -> None:
        v = moneyness_view("FUT", Decimal("100"), Decimal("120"), Decimal("50"))
        assert v.moneyness is Moneyness.NA
        assert v.atm_offset is None
        assert v.label == "—"

    def test_missing_spot_is_unknown(self) -> None:
        v = moneyness_view(OPTION_CALL, Decimal("100"), None, Decimal("50"))
        assert v.moneyness is Moneyness.UNKNOWN
        assert v.atm_offset is None
        assert v.label == "?"

    def test_frozen_dataclass(self) -> None:
        v = MoneynessView(Moneyness.ATM, 0, "ATM")
        with pytest.raises(FrozenInstanceError):
            v.atm_offset = 5  # type: ignore[misc]


class TestGoldenTable:
    """A frozen decode table — the contract the surfaces render."""

    @pytest.mark.parametrize(
        ("option_type", "strike", "spot", "step", "bucket", "offset", "label"),
        [
            # CALL across the grid (step 50, spot 1000) — ITM minus, OTM plus.
            ("CE", "800", "1000", "50", Moneyness.ITM, -4, "ITM -4"),
            ("CE", "1000", "1000", "50", Moneyness.ATM, 0, "ATM"),
            ("CE", "1200", "1000", "50", Moneyness.OTM, 4, "OTM +4"),
            # PUT mirror — raw offset sign flips, but the label sign tracks the BUCKET,
            # so a deep-ITM put still reads "ITM -4" (consistent with the call).
            ("PE", "1200", "1000", "50", Moneyness.ITM, 4, "ITM -4"),
            ("PE", "1000", "1000", "50", Moneyness.ATM, 0, "ATM"),
            ("PE", "800", "1000", "50", Moneyness.OTM, -4, "OTM +4"),
            # Future → N/A regardless of strike/spot/step.
            ("FUT", "1000", "1000", "50", Moneyness.NA, None, "—"),
            # No grid → bucket only.
            ("CE", "900", "1000", None, Moneyness.ITM, None, "ITM"),
            # No spot → UNKNOWN.
            ("CE", "1000", None, "50", Moneyness.UNKNOWN, None, "?"),
        ],
    )
    def test_golden(
        self,
        option_type: str,
        strike: str,
        spot: str | None,
        step: str | None,
        bucket: Moneyness,
        offset: int | None,
        label: str,
    ) -> None:
        v = moneyness_view(
            option_type,
            Decimal(strike),
            Decimal(spot) if spot is not None else None,
            Decimal(step) if step is not None else None,
        )
        assert v.moneyness is bucket
        assert v.atm_offset == offset
        assert v.label == label


class TestProperties:
    """Hypothesis invariants — the never-raises guarantee + the CALL/PUT mirror."""

    @given(_ANYTHING, _ANYTHING, _ANYTHING, _ANYTHING)
    def test_view_never_raises(
        self, option_type: object, strike: object, spot: object, step: object
    ) -> None:
        # moneyness_view must survive ANY junk — it is on a per-bar render hot path.
        v = moneyness_view(option_type, strike, spot, step)  # type: ignore[arg-type]
        assert isinstance(v, MoneynessView)
        assert isinstance(v.label, str) and v.label
        assert isinstance(v.moneyness, Moneyness)
        assert v.atm_offset is None or isinstance(v.atm_offset, int)

    @given(_ANYTHING, _ANYTHING, _ANYTHING)
    def test_classify_never_raises_and_is_valid(
        self, option_type: object, strike: object, spot: object
    ) -> None:
        result = classify(option_type, strike, spot)  # type: ignore[arg-type]
        assert isinstance(result, Moneyness)

    @given(_POS, _POS)
    def test_call_put_mirror(self, strike: Decimal, spot: Decimal) -> None:
        # With band 0, a non-ATM strike is exactly opposite for CALL vs PUT.
        call = classify(OPTION_CALL, strike, spot)
        put = classify(OPTION_PUT, strike, spot)
        if strike == spot:
            assert call is Moneyness.ATM and put is Moneyness.ATM
        else:
            opposite = {Moneyness.ITM: Moneyness.OTM, Moneyness.OTM: Moneyness.ITM}
            assert put is opposite[call]

    @given(st.lists(_POS, min_size=2, max_size=20))
    def test_step_is_positive_and_minimal(self, strikes: list[Decimal]) -> None:
        step = derive_strike_step(strikes)
        unique = sorted(set(strikes))
        if len(unique) < 2:
            assert step is None
        else:
            assert step is not None and step > 0
            gaps = [unique[i + 1] - unique[i] for i in range(len(unique) - 1)]
            assert step == min(gaps)

    @given(_POS, _POS, _POS)
    def test_offset_sign_tracks_strike_vs_spot(
        self, strike: Decimal, spot: Decimal, step: Decimal
    ) -> None:
        off = atm_offset(strike, spot, step)
        assert off is not None
        if strike > spot:
            assert off >= 0
        elif strike < spot:
            assert off <= 0


# ── DEC-OPTCOST-TAILS-RUST-001: the flag-ON seam (ON == OFF, never raises) ──


class TestOptcostRustSeamOn:
    """The graded classify / atm_offset / derive_strike_step decisions under
    `BRUTEX_OPTCOST_TAILS_RUST=1` answer identically to the Python oracle
    (byte-exact Decimal steps included) and preserve the module's
    never-raises contract."""

    @pytest.fixture(autouse=True)
    def _seam_on(self, monkeypatch: pytest.MonkeyPatch):
        import brutex.optcost_rust as ocr

        pytest.importorskip("fno_math")
        monkeypatch.setenv(ocr.OPTCOST_TAILS_RUST_ENV, "1")
        ocr.reset_optcost_rust_for_tests()  # burn-in armed — oracle runs beside
        yield
        ocr.reset_optcost_rust_for_tests()

    def test_classify_graded_matches_the_oracle(self) -> None:
        import brutex.optcost_rust as ocr

        for option_type, strike, spot, band in (
            ("CE", Decimal("24000"), Decimal("24100"), None),
            ("PE", Decimal("24000"), Decimal("24100"), None),
            ("CE", Decimal("24025"), Decimal("24000"), Decimal("25")),  # inclusive band edge
            ("PE", Decimal("24000.005"), Decimal("24100"), None),  # inexact paisa → oracle
        ):
            assert classify(option_type, strike, spot, atm_band=band) is _classify_graded_python(
                option_type,
                strike,
                spot,
                band if (band is not None and band >= 0) else Decimal(0),
            )
        # NA / UNKNOWN stay pre-FFI decisions
        assert classify("FUT", Decimal("1"), Decimal("1")) is Moneyness.NA
        assert classify("CE", None, Decimal("1")) is Moneyness.UNKNOWN
        assert not ocr.optcost_rust_disabled()

    def test_atm_offset_half_toward_zero_matches_the_oracle(self) -> None:
        step = Decimal("50")
        for strike, spot in (
            (Decimal("24025"), Decimal("24000")),  # +half step → 0 (toward zero)
            (Decimal("23975"), Decimal("24000")),  # -half step → 0 (toward zero)
            (Decimal("24100"), Decimal("24000")),
            (Decimal("23900"), Decimal("24000")),
            (Decimal("24000.005"), Decimal("24000")),  # inexact paisa → oracle
        ):
            assert atm_offset(strike, spot, step) == _atm_offset_python(strike, spot, step)

    def test_derive_strike_step_is_byte_exact_including_exponent(self) -> None:
        int_ladder = [Decimal("24000"), Decimal("24050"), Decimal("24150")]
        two_dp_ladder = [Decimal("24000.00"), Decimal("24050.00")]
        mixed_ladder = [Decimal("24000"), Decimal("24050.0")]
        for ladder in (int_ladder, two_dp_ladder, mixed_ladder):
            seam = derive_strike_step(list(ladder))
            oracle = _derive_strike_step_python(tuple(ladder))
            assert seam is not None and oracle is not None
            assert seam.as_tuple() == oracle.as_tuple()  # exponent-exact
        assert derive_strike_step([Decimal("24000")]) is None

    def test_resolve_strike_step_rides_the_gated_derive(self) -> None:
        result = resolve_strike_step(
            [Decimal("24050")], authoritative_strikes=[Decimal("24000"), Decimal("24100")]
        )
        assert result.step == Decimal("50")
        assert result.confidence is StepConfidence.REGISTRY
