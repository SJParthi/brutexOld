"""Unit gate for :mod:`brutex.meta.abstain` (DEC-ABSTAIN-GATE-STREAM-001).

The gate_reasons-template laws: the stable ordered vocabulary, every door
judged independently, ``passes ⇔ reasons == ()``, the EXACT ``>=`` τ
boundary, the NaN-normalisation law, the bitmask oracle's parity with the
tuple function, and the dormant in-sim knob's fail-loud refusal (the
``BRUTEX_FILL_MODE`` precedent).
"""

from __future__ import annotations

import math

import pytest
from brutex.meta.abstain import (
    ABSTAIN_EVENT_DAY,
    ABSTAIN_GATE_INSIM_ENV,
    ABSTAIN_LOW_CONFIDENCE,
    ABSTAIN_REASON_ORDER,
    ABSTAIN_REGIME_MISMATCH,
    ABSTAIN_VIX_MISSING,
    ABSTAIN_VIX_OUT_OF_BAND,
    DEFAULT_TAU,
    REGIME_CODES,
    AbstainGateConfig,
    AbstainGateInSimRefusedError,
    abstain_masks_python,
    abstain_passes,
    abstain_reasons,
    default_abstain_config,
    reasons_from_mask,
    require_abstain_gate_insim_unset,
)

NAN = float("nan")


def config(
    *,
    allowed: frozenset[str] | None = None,
    vix_lo: float | None = None,
    vix_hi: float | None = None,
    block_event_days: bool = False,
    tau: float | None = None,
) -> AbstainGateConfig:
    return AbstainGateConfig(
        allowed_regimes=allowed if allowed is not None else frozenset(REGIME_CODES),
        vix_lo=vix_lo,
        vix_hi=vix_hi,
        block_event_days=block_event_days,
        tau=tau,
    )


class TestVocabulary:
    def test_reason_order_is_the_pinned_wire_tuple(self) -> None:
        # The stable wire vocabulary — a rename/reorder is a breaking change.
        assert ABSTAIN_REASON_ORDER == (
            "regime_mismatch",
            "vix_out_of_band",
            "vix_missing",
            "event_day",
            "low_confidence",
        )
        assert ABSTAIN_REASON_ORDER == (
            ABSTAIN_REGIME_MISMATCH,
            ABSTAIN_VIX_OUT_OF_BAND,
            ABSTAIN_VIX_MISSING,
            ABSTAIN_EVENT_DAY,
            ABSTAIN_LOW_CONFIDENCE,
        )

    def test_regime_codes_are_the_pinned_ffi_map(self) -> None:
        # Mirror-pinned against rust/brutex-meta/src/abstain.rs's doc map.
        assert REGIME_CODES == {"up": 0, "down": 1, "sideways": 2, "unknown": 3}

    def test_default_tau_is_mid_grid(self) -> None:
        assert DEFAULT_TAU == 0.7


class TestConfigValidation:
    def test_valid_config_constructs(self) -> None:
        cfg = config(vix_lo=12.0, vix_hi=20.0, tau=0.7)
        assert cfg.vix_lo == 12.0

    def test_unknown_regime_token_refuses(self) -> None:
        with pytest.raises(ValueError, match="unknown regime token"):
            config(allowed=frozenset({"up", "bogus"}))

    def test_empty_allowed_regimes_refuses(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            config(allowed=frozenset())

    @pytest.mark.parametrize("bad", [NAN, math.inf, -math.inf])
    def test_non_finite_band_refuses(self, bad: float) -> None:
        with pytest.raises(ValueError, match="vix_lo"):
            config(vix_lo=bad)
        with pytest.raises(ValueError, match="vix_hi"):
            config(vix_hi=bad)

    def test_inverted_band_refuses(self) -> None:
        with pytest.raises(ValueError, match="vix_lo"):
            config(vix_lo=20.0, vix_hi=12.0)

    @pytest.mark.parametrize("bad", [-0.1, 1.1, NAN, math.inf])
    def test_bad_tau_refuses(self, bad: float) -> None:
        with pytest.raises(ValueError, match="tau"):
            config(tau=bad)

    def test_tau_boundaries_accepted(self) -> None:
        assert config(tau=0.0).tau == 0.0
        assert config(tau=1.0).tau == 1.0


class TestDefaultConfig:
    def test_default_is_unrestricted_except_events_and_tau(self) -> None:
        cfg = default_abstain_config()
        assert cfg.allowed_regimes == frozenset(REGIME_CODES)
        assert cfg.vix_lo is None
        assert cfg.vix_hi is None
        assert cfg.block_event_days is True
        assert cfg.tau == DEFAULT_TAU

    def test_full_band_adopted(self) -> None:
        cfg = default_abstain_config(12.0, 20.0)
        assert (cfg.vix_lo, cfg.vix_hi) == (12.0, 20.0)

    @pytest.mark.parametrize(
        ("lo", "hi"),
        [
            (None, 20.0),  # partial stamp — never a half-invented band
            (12.0, None),
            (None, None),
            (NAN, 20.0),  # non-finite stamp
            (12.0, NAN),
            (20.0, 12.0),  # inverted stamp
        ],
    )
    def test_partial_or_junk_band_yields_no_vix_gate(
        self, lo: float | None, hi: float | None
    ) -> None:
        cfg = default_abstain_config(lo, hi)
        assert cfg.vix_lo is None
        assert cfg.vix_hi is None


class TestAbstainReasons:
    def test_unrestricted_config_passes_everything(self) -> None:
        cfg = config()
        assert (
            abstain_reasons(regime="unknown", vix=None, event_day=True, confidence=None, config=cfg)
            == ()
        )

    def test_regime_mismatch_door(self) -> None:
        cfg = config(allowed=frozenset({"up"}))
        assert abstain_reasons(
            regime="down", vix=None, event_day=False, confidence=None, config=cfg
        ) == (ABSTAIN_REGIME_MISMATCH,)
        assert (
            abstain_reasons(regime="up", vix=None, event_day=False, confidence=None, config=cfg)
            == ()
        )

    def test_vix_band_is_inclusive_both_ends(self) -> None:
        cfg = config(vix_lo=12.0, vix_hi=20.0)

        def reasons(vix: float) -> tuple[str, ...]:
            return abstain_reasons(
                regime="up", vix=vix, event_day=False, confidence=None, config=cfg
            )

        assert reasons(12.0) == ()
        assert reasons(20.0) == ()
        assert reasons(11.999) == (ABSTAIN_VIX_OUT_OF_BAND,)
        assert reasons(20.001) == (ABSTAIN_VIX_OUT_OF_BAND,)

    def test_vix_half_bounded_bands(self) -> None:
        floor_only = config(vix_lo=12.0)
        assert (
            abstain_reasons(
                regime="up", vix=1e9, event_day=False, confidence=None, config=floor_only
            )
            == ()
        )
        assert abstain_reasons(
            regime="up", vix=1.0, event_day=False, confidence=None, config=floor_only
        ) == (ABSTAIN_VIX_OUT_OF_BAND,)
        ceil_only = config(vix_hi=20.0)
        assert abstain_reasons(
            regime="up", vix=25.0, event_day=False, confidence=None, config=ceil_only
        ) == (ABSTAIN_VIX_OUT_OF_BAND,)

    def test_vix_missing_only_under_a_configured_gate(self) -> None:
        gated = config(vix_lo=12.0, vix_hi=20.0)
        assert abstain_reasons(
            regime="up", vix=None, event_day=False, confidence=None, config=gated
        ) == (ABSTAIN_VIX_MISSING,)
        ungated = config()
        assert (
            abstain_reasons(regime="up", vix=None, event_day=False, confidence=None, config=ungated)
            == ()
        )

    def test_nan_vix_normalises_to_missing(self) -> None:
        gated = config(vix_lo=12.0, vix_hi=20.0)
        assert abstain_reasons(
            regime="up", vix=NAN, event_day=False, confidence=None, config=gated
        ) == (ABSTAIN_VIX_MISSING,)

    def test_event_day_door_judged_only_when_blocking(self) -> None:
        blocking = config(block_event_days=True)
        assert abstain_reasons(
            regime="up", vix=None, event_day=True, confidence=None, config=blocking
        ) == (ABSTAIN_EVENT_DAY,)
        assert (
            abstain_reasons(
                regime="up", vix=None, event_day=False, confidence=None, config=blocking
            )
            == ()
        )
        off = config(block_event_days=False)
        assert (
            abstain_reasons(regime="up", vix=None, event_day=True, confidence=None, config=off)
            == ()
        )

    def test_tau_boundary_is_exact_gte(self) -> None:
        cfg = config(tau=0.7)
        # confidence == tau PASSES (the pinned >= law).
        assert (
            abstain_reasons(regime="up", vix=None, event_day=False, confidence=0.7, config=cfg)
            == ()
        )
        below = math.nextafter(0.7, 0.0)
        assert abstain_reasons(
            regime="up", vix=None, event_day=False, confidence=below, config=cfg
        ) == (ABSTAIN_LOW_CONFIDENCE,)

    def test_missing_or_nan_confidence_under_tau_is_low_confidence(self) -> None:
        cfg = config(tau=0.7)
        for confidence in (None, NAN):
            assert abstain_reasons(
                regime="up", vix=None, event_day=False, confidence=confidence, config=cfg
            ) == (ABSTAIN_LOW_CONFIDENCE,)

    def test_all_doors_judged_independently_in_order(self) -> None:
        cfg = AbstainGateConfig(
            allowed_regimes=frozenset({"up"}),
            vix_lo=12.0,
            vix_hi=20.0,
            block_event_days=True,
            tau=0.7,
        )
        reasons = abstain_reasons(
            regime="down", vix=50.0, event_day=True, confidence=0.1, config=cfg
        )
        assert reasons == (
            ABSTAIN_REGIME_MISMATCH,
            ABSTAIN_VIX_OUT_OF_BAND,
            ABSTAIN_EVENT_DAY,
            ABSTAIN_LOW_CONFIDENCE,
        )
        # The missing-vix variant swaps door 2 for door 3, same order.
        reasons = abstain_reasons(
            regime="down", vix=None, event_day=True, confidence=None, config=cfg
        )
        assert reasons == (
            ABSTAIN_REGIME_MISMATCH,
            ABSTAIN_VIX_MISSING,
            ABSTAIN_EVENT_DAY,
            ABSTAIN_LOW_CONFIDENCE,
        )

    def test_passes_iff_reasons_empty(self) -> None:
        cfg = config(tau=0.7)
        assert abstain_passes(regime="up", vix=None, event_day=False, confidence=0.9, config=cfg)
        assert not abstain_passes(
            regime="up", vix=None, event_day=False, confidence=0.1, config=cfg
        )


class TestReasonsFromMask:
    def test_decodes_every_single_bit(self) -> None:
        for i, reason in enumerate(ABSTAIN_REASON_ORDER):
            assert reasons_from_mask(1 << i) == (reason,)

    def test_zero_is_pass(self) -> None:
        assert reasons_from_mask(0) == ()

    def test_combined_mask_decodes_in_order(self) -> None:
        assert reasons_from_mask(0b11011) == (
            ABSTAIN_REGIME_MISMATCH,
            ABSTAIN_VIX_OUT_OF_BAND,
            ABSTAIN_EVENT_DAY,
            ABSTAIN_LOW_CONFIDENCE,
        )

    @pytest.mark.parametrize("bad", [-1, 32, 1 << 8])
    def test_out_of_vocabulary_mask_refuses(self, bad: int) -> None:
        with pytest.raises(ValueError, match="reason vocabulary"):
            reasons_from_mask(bad)


class TestAbstainMasksPython:
    def test_empty_inputs_are_first_class(self) -> None:
        assert abstain_masks_python([], 0b1111, [], NAN, NAN, [], True, [], NAN) == []

    def test_mismatched_lengths_refuse(self) -> None:
        with pytest.raises(ValueError, match="column lengths"):
            abstain_masks_python([0], 0b1111, [], NAN, NAN, [0], True, [NAN], NAN)
        with pytest.raises(ValueError, match="column lengths"):
            abstain_masks_python([0], 0b1111, [NAN], NAN, NAN, [], True, [NAN], NAN)
        with pytest.raises(ValueError, match="column lengths"):
            abstain_masks_python([0], 0b1111, [NAN], NAN, NAN, [0], True, [], NAN)

    def test_out_of_range_regime_codes_always_mismatch(self) -> None:
        masks = abstain_masks_python(
            [-1, 64, 63], (1 << 63) | 0b1111, [NAN] * 3, NAN, NAN, [0] * 3, False, [NAN] * 3, NAN
        )
        assert masks == [1, 1, 0]

    def test_matches_the_tuple_function_bit_for_bit(self) -> None:
        # The oracle's bitmask semantics == abstain_reasons over the same
        # snapshot, for every door combination (the mirror-parity law).
        cfg = AbstainGateConfig(
            allowed_regimes=frozenset({"up", "sideways"}),
            vix_lo=12.0,
            vix_hi=20.0,
            block_event_days=True,
            tau=0.7,
        )
        allowed_mask = 0
        for token in cfg.allowed_regimes:
            allowed_mask |= 1 << REGIME_CODES[token]
        snapshots: list[tuple[str, float | None, bool, float | None]] = [
            ("up", 15.0, False, 0.9),
            ("down", 15.0, False, 0.9),
            ("up", 25.0, True, 0.1),
            ("sideways", None, True, None),
            ("unknown", 11.0, False, 0.7),
        ]
        masks = abstain_masks_python(
            [REGIME_CODES[r] for r, _, _, _ in snapshots],
            allowed_mask,
            [v if v is not None else NAN for _, v, _, _ in snapshots],
            12.0,
            20.0,
            [1 if e else 0 for _, _, e, _ in snapshots],
            True,
            [c if c is not None else NAN for _, _, _, c in snapshots],
            0.7,
        )
        for (regime, vix, event, conf), mask in zip(snapshots, masks, strict=True):
            assert reasons_from_mask(mask) == abstain_reasons(
                regime=regime, vix=vix, event_day=event, confidence=conf, config=cfg
            )


class TestInSimRefusal:
    def test_unset_passes(self) -> None:
        require_abstain_gate_insim_unset(env={})

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_passes(self, blank: str) -> None:
        # The common `VAR= cmd` unset idiom — nothing was configured.
        require_abstain_gate_insim_unset(env={ABSTAIN_GATE_INSIM_ENV: blank})

    @pytest.mark.parametrize("value", ["1", "true", "on", "0", "off", "junk", "worst_case"])
    def test_any_non_blank_value_refuses(self, value: str) -> None:
        with pytest.raises(AbstainGateInSimRefusedError, match="DEC-ABSTAIN-GATE-STREAM-001"):
            require_abstain_gate_insim_unset(env={ABSTAIN_GATE_INSIM_ENV: value})

    def test_refusal_names_the_gate_flip_ruling(self) -> None:
        with pytest.raises(AbstainGateInSimRefusedError, match="gate-flip ruling"):
            require_abstain_gate_insim_unset(env={ABSTAIN_GATE_INSIM_ENV: "1"})

    def test_default_env_source_is_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ABSTAIN_GATE_INSIM_ENV, "1")
        with pytest.raises(AbstainGateInSimRefusedError):
            require_abstain_gate_insim_unset()
        monkeypatch.delenv(ABSTAIN_GATE_INSIM_ENV)
        require_abstain_gate_insim_unset()

    def test_error_is_a_runtime_error(self) -> None:
        assert issubclass(AbstainGateInSimRefusedError, RuntimeError)
