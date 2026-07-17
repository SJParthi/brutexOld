"""Unit tests for :mod:`brutex.strategies.spec` — DEC-SPEC-IDENTITY-WIRING-001.

Covers the symbol registry invariants (constructed, not asserted), the
``StrategySpec`` validation matrix, the Python↔Rust rejection-PARITY pins,
the ``canonical_spec_params`` builder shape, the ``spec_run_id`` wiring
(including the two-anchors→two-run_ids regression), the ``StrategyMeta``
reserved-prefix refusal, and the declaration→identity→dedup integration
chain on the real modules.
"""

# The registry-invariant tests drive the private ``_build_registry`` builder
# deliberately (its duplicate/range refusals are the contract under test);
# repo convention for whitebox suites.
# pyright: reportPrivateUsage=none
from __future__ import annotations

import dataclasses
from typing import Any

import pytest
import trading_core
from brutex.data.option_dedup import OptionDedupIndex
from brutex.engine.sweep_run import ComboDedup
from brutex.strategies.errors import StrategyError
from brutex.strategies.fib_definitions import PREV_DAY, Direction
from brutex.strategies.spec import (
    MAX_PRIOR_N,
    MAX_PROXIMITY_BPS,
    SPEC_STRATEGY_ID_PREFIX,
    SYMBOL_IDS,
    StrategySpec,
    _build_registry,
    canonical_spec_params,
    spec_run_id,
    symbol_id_for,
    symbol_label_for,
)
from brutex.strategies.types import FibAnchorKind
from brutex.vocabulary.bits import IMPLEMENTED_MASK


def make_spec(**overrides: Any) -> StrategySpec:
    """A valid baseline spec; override any field per test."""
    fields: dict[str, Any] = {
        "condition_mask": 0x1F,
        "direction": Direction.LONG,
        "pivot_proximity_bps": 50,
        "pdh_pdl_proximity_bps": 10,
        "fib_proximity_bps": 25,
        "prior_n": 3,
        "symbol_id": 1,
    }
    fields.update(overrides)
    return StrategySpec(**fields)


RUN_CTX: dict[str, Any] = {
    "tf": "15m",
    "mode": "B",
    "sl_paisa": None,
    "tp_paisa": None,
    "start_equity_paisa": 10_000_000_00,
    "fib_anchor": FibAnchorKind.PREV_DAY,
    "data_snapshot_hash": "a" * 64,
    "indicator_version": "ind-v1",
    "code_commit_sha": "b" * 40,
}


class TestSymbolRegistry:
    def test_registered_pairs_roundtrip_both_directions(self) -> None:
        for label, symbol_id in SYMBOL_IDS.items():
            assert symbol_id_for(label) == symbol_id
            assert symbol_label_for(symbol_id) == label

    def test_table_pinned_literally(self) -> None:
        # The full assignment table, pinned in the MUTATION-gated unit file
        # (the golden pins it too): a renumber, rename, or drift fails here.
        # Row 4 appended by DEC-FUTURES-ADDRESSING-PIN-001 (ADR-0012).
        assert dict(SYMBOL_IDS) == {
            "NSE-NIFTY": 1,
            "NSE-BANKNIFTY": 2,
            "BSE-SENSEX": 3,
            "NSE-NIFTY-FUT": 4,
        }

    def test_unregistered_label_raises(self) -> None:
        # EXACT message equality (not a substring) so a wording drift — or a
        # mutated segment — is caught, not just the raise.
        with pytest.raises(StrategyError) as exc:
            symbol_id_for("NSE-NOSUCH")
        assert str(exc.value) == (
            "symbol_id_for: unregistered instrument label 'NSE-NOSUCH' "
            "(registered: ['BSE-SENSEX', 'NSE-BANKNIFTY', 'NSE-NIFTY', 'NSE-NIFTY-FUT'])"
        )

    def test_unregistered_id_raises(self) -> None:
        with pytest.raises(StrategyError) as exc:
            symbol_label_for(999)
        assert str(exc.value) == (
            "symbol_label_for: unregistered symbol_id 999 (registered: [1, 2, 3, 4])"
        )

    def test_registry_is_readonly(self) -> None:
        with pytest.raises(TypeError):
            SYMBOL_IDS["NSE-HACK"] = 42  # type: ignore[index]

    def test_futures_row_import_identity_with_the_universe_alias(self) -> None:
        # Row 4's label IS brutex.universe.NIFTY_FUTURES_SERIES_SYMBOL —
        # import-identity-pinned so the registry literal can never drift from
        # the ONE pinned addressing constant (DEC-FUTURES-ADDRESSING-PIN-001).
        from brutex.universe import NIFTY_FUTURES_SERIES_SYMBOL

        assert symbol_id_for(NIFTY_FUTURES_SERIES_SYMBOL) == 4
        assert symbol_label_for(4) == NIFTY_FUTURES_SERIES_SYMBOL

    def test_build_rejects_duplicate_label(self) -> None:
        with pytest.raises(StrategyError) as exc:
            _build_registry((("A", 1), ("A", 2)))
        assert str(exc.value) == "symbol registry: duplicate label 'A'"

    def test_build_rejects_duplicate_id(self) -> None:
        with pytest.raises(StrategyError) as exc:
            _build_registry((("A", 1), ("B", 1)))
        assert str(exc.value) == "symbol registry: id 1 assigned to both 'A' and 'B'"

    def test_build_rejects_reserved_id_zero(self) -> None:
        with pytest.raises(StrategyError) as exc:
            _build_registry((("A", 0),))
        assert str(exc.value) == (
            "symbol registry: id 0 for 'A' outside [1, 4294967295] "
            "(0 is the RESERVED unassigned sentinel)"
        )

    def test_build_rejects_id_beyond_u32(self) -> None:
        with pytest.raises(StrategyError) as exc:
            _build_registry((("A", 2**32),))
        assert str(exc.value) == (
            "symbol registry: id 4294967296 for 'A' outside [1, 4294967295] "
            "(0 is the RESERVED unassigned sentinel)"
        )

    def test_build_rejects_negative_id(self) -> None:
        # The lower boundary's negative side (the 0 < symbol_id arm) — a
        # forged negative id can never mint a registry row.
        with pytest.raises(StrategyError) as exc:
            _build_registry((("A", -1),))
        assert str(exc.value) == (
            "symbol registry: id -1 for 'A' outside [1, 4294967295] "
            "(0 is the RESERVED unassigned sentinel)"
        )

    def test_build_accepts_id_at_u32_max(self) -> None:
        # Literal boundary probe: 2**32 - 1 is IN range (kills a <= → <
        # boundary mutant + a max-constant drift).
        forward, reverse = _build_registry((("A", 2**32 - 1),))
        assert forward["A"] == 4294967295
        assert reverse[4294967295] == "A"

    def test_build_rejects_blank_label(self) -> None:
        with pytest.raises(StrategyError) as exc:
            _build_registry((("", 1),))
        assert str(exc.value) == "symbol registry: label must be non-empty"


class TestStrategySpecValidation:
    def test_valid_spec_constructs(self) -> None:
        spec = make_spec()
        assert spec.condition_mask == 0x1F
        assert spec.symbol_id == 1

    def test_frozen_dataclass_rejects_assignment(self) -> None:
        spec = make_spec()
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.prior_n = 9  # type: ignore[misc]

    def test_slots_no_instance_dict(self) -> None:
        assert not hasattr(make_spec(), "__dict__")

    def test_negative_mask_rejected(self) -> None:
        with pytest.raises(StrategyError) as exc:
            make_spec(condition_mask=-1)
        assert str(exc.value) == "StrategySpec.condition_mask must be non-negative, got -1"

    def test_reserved_bit_rejected_exact_message(self) -> None:
        with pytest.raises(StrategyError) as exc:
            make_spec(condition_mask=1 << 127)
        assert str(exc.value) == (
            "StrategySpec.condition_mask requires unimplemented/reserved bits "
            f"({1 << 127:#034x}); only bits in IMPLEMENTED_MASK "
            f"({IMPLEMENTED_MASK:#034x}) may be required"
        )

    def test_mixed_valid_and_reserved_bits_rejected(self) -> None:
        with pytest.raises(StrategyError, match="unimplemented/reserved"):
            make_spec(condition_mask=IMPLEMENTED_MASK | (1 << 100))

    def test_full_implemented_mask_accepted(self) -> None:
        assert make_spec(condition_mask=IMPLEMENTED_MASK).condition_mask == IMPLEMENTED_MASK

    @pytest.mark.parametrize(
        "bit",
        [
            pytest.param(52, id="bit52-retired-vwap-above"),
            pytest.param(53, id="bit53-retired-vwap-below"),
        ],
    )
    def test_retired_bits_rejected(self, bit: int) -> None:
        """The vocab-66 refusal pins (the Rust twin's Python mirror).

        The RETIRED VWAP tombstones (52/53) are outside ``IMPLEMENTED_MASK``
        — a mask requiring either must refuse at construction, alone AND
        mixed with valid bits. (The SuperTrend bits 64/65 left this refusal
        set at the vocab-66 implemented-flip — see
        :meth:`test_live_supertrend_bits_accepted`.)
        """
        assert not (IMPLEMENTED_MASK >> bit) & 1  # genuinely unimplemented
        with pytest.raises(StrategyError, match="unimplemented/reserved"):
            make_spec(condition_mask=1 << bit)
        with pytest.raises(StrategyError, match="unimplemented/reserved"):
            make_spec(condition_mask=(1 << bit) | 0x1F)

    @pytest.mark.parametrize(
        "bit",
        [
            pytest.param(64, id="bit64-live-supertrend-above"),
            pytest.param(65, id="bit65-live-supertrend-below"),
        ],
    )
    def test_live_supertrend_bits_accepted(self, bit: int) -> None:
        """The vocab-66 SuperTrend implemented-flip ACCEPTANCE pins (crate
        0.5.0): bits 64/65 joined ``IMPLEMENTED_MASK``, so a mask requiring
        them constructs — alone AND mixed with valid bits (the former
        declared-only refusal is consciously retired)."""
        assert (IMPLEMENTED_MASK >> bit) & 1  # genuinely implemented now
        mask = (1 << bit) | 0b101
        assert make_spec(condition_mask=mask).condition_mask == mask

    @pytest.mark.parametrize(
        "bit",
        [
            pytest.param(60, id="bit60-live-above-cpr-tc"),
            pytest.param(63, id="bit63-live-narrow-cpr-day-sign-bit"),
        ],
    )
    def test_live_cpr_bits_accepted(self, bit: int) -> None:
        """The LIVE CPR bits construct — incl. bit 63, the u64 sign bit."""
        mask = (1 << bit) | 0b101
        assert make_spec(condition_mask=mask).condition_mask == mask

    def test_non_direction_rejected(self) -> None:
        with pytest.raises(StrategyError) as exc:
            make_spec(direction="long")
        assert str(exc.value) == "StrategySpec.direction must be a Direction, got str"

    @pytest.mark.parametrize(
        "field",
        ["pivot_proximity_bps", "pdh_pdl_proximity_bps", "fib_proximity_bps"],
    )
    def test_negative_bps_rejected(self, field: str) -> None:
        with pytest.raises(StrategyError) as exc:
            make_spec(**{field: -1})
        assert str(exc.value) == f"StrategySpec.{field} (-1) outside [0, 1000000]"

    @pytest.mark.parametrize(
        "field",
        ["pivot_proximity_bps", "pdh_pdl_proximity_bps", "fib_proximity_bps"],
    )
    def test_bps_beyond_max_rejected(self, field: str) -> None:
        # Literal 1_000_001 (not MAX+1) so a mutated Python-side constant
        # cannot self-consistently dodge the probe.
        with pytest.raises(StrategyError) as exc:
            make_spec(**{field: 1_000_001})
        assert str(exc.value) == f"StrategySpec.{field} (1000001) outside [0, 1000000]"

    def test_bps_at_max_accepted(self) -> None:
        # Literal boundary: exactly 1_000_000 is IN range.
        spec = make_spec(pivot_proximity_bps=1_000_000)
        assert spec.pivot_proximity_bps == MAX_PROXIMITY_BPS == 1_000_000

    def test_negative_prior_n_rejected(self) -> None:
        with pytest.raises(StrategyError) as exc:
            make_spec(prior_n=-1)
        assert str(exc.value) == (
            "StrategySpec.prior_n (-1) outside [0, 18446744073709551615] (encoded as a fixed u64)"
        )

    def test_prior_n_at_u64_max_accepted(self) -> None:
        # Literal boundary: 2**64 - 1 is IN range (kills a <= → < mutant +
        # a max-constant drift) — and it ENCODES through the real binding.
        spec = make_spec(prior_n=2**64 - 1)
        assert spec.prior_n == MAX_PRIOR_N == 18446744073709551615
        assert len(spec.canonical_bytes()) == 57

    def test_prior_n_beyond_u64_rejected(self) -> None:
        with pytest.raises(StrategyError, match="prior_n"):
            make_spec(prior_n=2**64)

    def test_symbol_id_zero_rejected_policy_exact_message(self) -> None:
        # BruteX POLICY: 0 is the reserved unassigned sentinel (the shared
        # crate itself accepts 0 — see the parity test below).
        with pytest.raises(StrategyError) as exc:
            make_spec(symbol_id=0)
        assert str(exc.value) == (
            "StrategySpec.symbol_id (0) outside [1, 4294967295] "
            "(0 is the RESERVED unassigned sentinel — BruteX policy)"
        )

    def test_symbol_id_beyond_u32_rejected(self) -> None:
        with pytest.raises(StrategyError) as exc:
            make_spec(symbol_id=2**32)
        assert str(exc.value) == (
            "StrategySpec.symbol_id (4294967296) outside [1, 4294967295] "
            "(0 is the RESERVED unassigned sentinel — BruteX policy)"
        )

    def test_symbol_id_at_u32_max_reaches_registry_check(self) -> None:
        # 2**32 - 1 PASSES the range check (kills a <= → < mutant) and fails
        # only at the REGISTRY layer — pinned by the unregistered message.
        with pytest.raises(StrategyError, match="^symbol_label_for: unregistered symbol_id"):
            make_spec(symbol_id=2**32 - 1)

    def test_unregistered_symbol_id_rejected(self) -> None:
        with pytest.raises(StrategyError, match="unregistered symbol_id"):
            make_spec(symbol_id=77)


class TestRejectionParity:
    """Each SHARED validation rule raises on BOTH layers (design §1.1/§1.3).

    Python ``__post_init__`` raises :class:`StrategyError`; the raw Rust
    binding raises ``ValueError`` (or PyO3 ``OverflowError`` for the width
    rows) — pinned pairwise so the layers cannot drift apart silently.
    """

    def test_reserved_bit_parity(self) -> None:
        with pytest.raises(StrategyError):
            make_spec(condition_mask=1 << 127)
        with pytest.raises(ValueError, match="unimplemented/reserved"):
            trading_core.spec_canonical_bytes(f"{1 << 127:032x}", 0, 50, 10, 25, 3, 1)

    def test_non_hex_mask_binding_rejects(self) -> None:
        # Python-side the mask is an int (unrepresentable state); the binding
        # still refuses garbage hex loudly.
        with pytest.raises(ValueError, match="not valid hex"):
            trading_core.spec_canonical_bytes("zz", 0, 50, 10, 25, 3, 1)

    def test_direction_parity(self) -> None:
        with pytest.raises(StrategyError):
            make_spec(direction="sideways")
        with pytest.raises(ValueError, match="direction"):
            trading_core.spec_canonical_bytes("1f", 2, 50, 10, 25, 3, 1)

    def test_bps_range_parity_at_the_shared_boundary(self) -> None:
        # The SAME constant on both sides: MAX accepted, MAX+1 rejected —
        # proves MAX_PROXIMITY_BPS mirrors the Rust MAX_BPS by probe.
        assert make_spec(fib_proximity_bps=MAX_PROXIMITY_BPS).fib_proximity_bps
        ok = trading_core.spec_canonical_bytes("1f", 0, 50, 10, MAX_PROXIMITY_BPS, 3, 1)
        assert len(bytes(ok)) == 57
        with pytest.raises(StrategyError):
            make_spec(fib_proximity_bps=MAX_PROXIMITY_BPS + 1)
        with pytest.raises(ValueError, match="fib_bps"):
            trading_core.spec_canonical_bytes("1f", 0, 50, 10, MAX_PROXIMITY_BPS + 1, 3, 1)
        with pytest.raises(StrategyError):
            make_spec(pivot_proximity_bps=-1)
        with pytest.raises(ValueError, match="pivot_bps"):
            trading_core.spec_canonical_bytes("1f", 0, -1, 10, 25, 3, 1)

    def test_prior_n_width_backstop(self) -> None:
        # Python refuses > u64 with StrategyError; a RAW binding call raises
        # PyO3 OverflowError — the documented backstop for a forged field.
        with pytest.raises(StrategyError):
            make_spec(prior_n=2**64)
        with pytest.raises(OverflowError):
            trading_core.spec_canonical_bytes("1f", 0, 50, 10, 25, 2**64, 1)

    def test_symbol_id_width_backstop(self) -> None:
        with pytest.raises(StrategyError):
            make_spec(symbol_id=2**32)
        with pytest.raises(OverflowError):
            trading_core.spec_canonical_bytes("1f", 0, 50, 10, 25, 3, 2**32)

    def test_symbol_id_negative_backstop(self) -> None:
        # The u32 boundary's NEGATIVE side (the FFI-contract twin of the
        # width backstop above, ADR-0012 launch pin): Python policy refuses
        # a negative id; a raw binding call raises PyO3 OverflowError — a
        # forged negative key can never reach the canonical-bytes encoder.
        with pytest.raises(StrategyError):
            make_spec(symbol_id=-1)
        with pytest.raises(OverflowError):
            trading_core.spec_canonical_bytes("1f", 0, 50, 10, 25, 3, -1)

    def test_symbol_id_zero_is_policy_not_shared_rule(self) -> None:
        # The shared crate ACCEPTS id 0 (spec.rs "zero is valid"); only the
        # BruteX registry layer reserves it — the ONE deliberate asymmetry.
        raw = bytes(trading_core.spec_canonical_bytes("0", 1, 0, 0, 0, 0, 0))
        assert len(raw) == 57
        with pytest.raises(StrategyError):
            make_spec(symbol_id=0)


class TestCanonicalBytes:
    def test_returns_57_bytes_with_v1_prefix(self) -> None:
        raw = make_spec().canonical_bytes()
        assert isinstance(raw, bytes)
        assert len(raw) == 57
        assert raw[:4] == (1).to_bytes(4, "little")

    def test_deterministic(self) -> None:
        assert make_spec().canonical_bytes() == make_spec().canonical_bytes()

    @pytest.mark.parametrize(
        "overrides",
        [
            {"condition_mask": 0x1E},
            {"direction": Direction.SHORT},
            {"pivot_proximity_bps": 51},
            {"pdh_pdl_proximity_bps": 11},
            {"fib_proximity_bps": 26},
            {"prior_n": 4},
            {"symbol_id": 2},
        ],
    )
    def test_each_field_moves_the_encode(self, overrides: dict[str, Any]) -> None:
        assert make_spec(**overrides).canonical_bytes() != make_spec().canonical_bytes()


class TestFromDefinition:
    def test_valid_projection(self) -> None:
        spec = StrategySpec.from_definition(
            PREV_DAY,
            condition_mask=0x1F,
            direction=Direction.SHORT,
            pivot_proximity_bps=50,
            pdh_pdl_proximity_bps=10,
            fib_proximity_bps=25,
            prior_n=3,
            symbol_label="NSE-NIFTY",
        )
        assert spec.direction is Direction.SHORT
        assert spec.symbol_id == symbol_id_for("NSE-NIFTY")

    def test_out_of_scope_mask_rejected_via_definition(self) -> None:
        narrow = dataclasses.replace(
            PREV_DAY,
            name="fib_prev_day_narrow",
            condition_scope=frozenset({"ema20_above_ema200"}),
        )
        with pytest.raises(ValueError, match="outside strategy"):
            StrategySpec.from_definition(
                narrow,
                condition_mask=IMPLEMENTED_MASK,
                direction=Direction.LONG,
                pivot_proximity_bps=50,
                pdh_pdl_proximity_bps=10,
                fib_proximity_bps=25,
                prior_n=3,
                symbol_label="NSE-NIFTY",
            )

    def test_unswept_direction_rejected_via_definition(self) -> None:
        long_only = dataclasses.replace(
            PREV_DAY, name="fib_prev_day_long", directions=(Direction.LONG,)
        )
        with pytest.raises(ValueError, match="not swept"):
            StrategySpec.from_definition(
                long_only,
                condition_mask=0x1,
                direction=Direction.SHORT,
                pivot_proximity_bps=50,
                pdh_pdl_proximity_bps=10,
                fib_proximity_bps=25,
                prior_n=3,
                symbol_label="NSE-NIFTY",
            )

    def test_unregistered_symbol_label_rejected(self) -> None:
        with pytest.raises(StrategyError, match="unregistered instrument label"):
            StrategySpec.from_definition(
                PREV_DAY,
                condition_mask=0x1,
                direction=Direction.LONG,
                pivot_proximity_bps=50,
                pdh_pdl_proximity_bps=10,
                fib_proximity_bps=25,
                prior_n=3,
                symbol_label="NSE-NOSUCH",
            )


class TestCanonicalSpecParams:
    def test_exact_shape(self) -> None:
        params = canonical_spec_params(
            sl_paisa=100,
            tp_paisa=None,
            start_equity_paisa=5_00_000_00,
            fib_anchor=FibAnchorKind.ROLLING_WEEKLY,
        )
        assert params == {
            "sl_paisa": 100,
            "tp_paisa": None,
            "start_equity_paisa": 5_00_000_00,
            "fib_anchor": "rolling_weekly",
        }

    def test_spec_carried_axes_structurally_absent(self) -> None:
        # The C1/C2 closure: the builder HAS no channel for a spec-carried
        # axis, so a double-carry key can never appear.
        params = canonical_spec_params(
            sl_paisa=None,
            tp_paisa=None,
            start_equity_paisa=1,
            fib_anchor=FibAnchorKind.PREV_DAY,
        )
        forbidden = {
            "prior_n",
            "proximity_bps",
            "pivot_proximity_bps",
            "pdh_pdl_proximity_bps",
            "fib_proximity_bps",
            "condition_mask",
            "mask",
            "direction",
            "symbol_id",
        }
        assert forbidden.isdisjoint(params)

    @pytest.mark.parametrize("anchor", list(FibAnchorKind))
    def test_fib_anchor_emitted_unconditionally(self, anchor: FibAnchorKind) -> None:
        params = canonical_spec_params(
            sl_paisa=None, tp_paisa=None, start_equity_paisa=1, fib_anchor=anchor
        )
        assert params["fib_anchor"] == anchor.value


class TestSpecRunId:
    def test_shape_and_determinism(self) -> None:
        rid = spec_run_id(make_spec(), **RUN_CTX)
        assert len(rid) == 64
        assert set(rid) <= set("0123456789abcdef")
        assert rid == spec_run_id(make_spec(), **RUN_CTX)

    def test_two_anchors_two_run_ids(self) -> None:
        # THE C1 regression: the anchor changes the Group-4 levels a spec
        # evaluates against — two anchors are two computations, two ids.
        base = spec_run_id(make_spec(), **RUN_CTX)
        other_ctx = dict(RUN_CTX, fib_anchor=FibAnchorKind.WEEKLY_STATIC)
        assert spec_run_id(make_spec(), **other_ctx) != base

    @pytest.mark.parametrize(
        "overrides",
        [
            {"condition_mask": 0x1E},
            {"direction": Direction.SHORT},
            {"pivot_proximity_bps": 51},
            {"prior_n": 4},
            {"symbol_id": 2},
        ],
    )
    def test_each_spec_field_moves_the_id(self, overrides: dict[str, Any]) -> None:
        assert spec_run_id(make_spec(**overrides), **RUN_CTX) != spec_run_id(make_spec(), **RUN_CTX)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("tf", "5m"),
            ("mode", "A"),
            ("sl_paisa", 500),
            ("tp_paisa", 900),
            ("start_equity_paisa", 42),
            ("data_snapshot_hash", "c" * 64),
            ("indicator_version", "ind-v2"),
            ("code_commit_sha", "d" * 40),
        ],
    )
    def test_each_context_field_moves_the_id(self, key: str, value: Any) -> None:
        ctx = dict(RUN_CTX, **{key: value})
        assert spec_run_id(make_spec(), **ctx) != spec_run_id(make_spec(), **RUN_CTX)

    def test_direction_and_instrument_derived_from_spec(self) -> None:
        # Consistency by construction: the projection reads the spec — there
        # is no direction/instrument parameter to contradict it. Proven by
        # equality against a manual combo_hash with the derived fields.
        from brutex.strategies.combo import combo_hash

        spec = make_spec(direction=Direction.SHORT, symbol_id=3)
        expected = combo_hash(
            strategy_id=SPEC_STRATEGY_ID_PREFIX + spec.canonical_bytes().hex(),
            direction="short",
            instrument="BSE-SENSEX",
            tf=RUN_CTX["tf"],
            mode=RUN_CTX["mode"],
            params=canonical_spec_params(
                sl_paisa=RUN_CTX["sl_paisa"],
                tp_paisa=RUN_CTX["tp_paisa"],
                start_equity_paisa=RUN_CTX["start_equity_paisa"],
                fib_anchor=RUN_CTX["fib_anchor"],
            ),
            data_snapshot_hash=RUN_CTX["data_snapshot_hash"],
            indicator_version=RUN_CTX["indicator_version"],
            code_commit_sha=RUN_CTX["code_commit_sha"],
        )
        assert spec_run_id(spec, **RUN_CTX) == expected

    def test_namespace_prefixes_disjoint(self) -> None:
        assert SPEC_STRATEGY_ID_PREFIX == "spec:"
        assert not SPEC_STRATEGY_ID_PREFIX.startswith("mask:")
        assert not "mask:".startswith(SPEC_STRATEGY_ID_PREFIX)


class TestSpecIdentityDedupChain:
    """Integration: declaration → spec → evaluate → identity → dedup, all real.

    The EVALUATION half (mask → fires) is already deploy-golden-proven 400×2
    (#1027); here ONE real ``evaluate_spec`` call anchors the chain, then the
    identity flows through the real BLAKE3 ``combo_hash`` into the real
    Bloom+exact ``ComboDedup``.
    """

    def test_end_to_end_chain(self) -> None:
        spec = StrategySpec.from_definition(
            PREV_DAY,
            condition_mask=0,
            direction=Direction.LONG,
            pivot_proximity_bps=0,
            pdh_pdl_proximity_bps=0,
            fib_proximity_bps=0,
            prior_n=0,
            symbol_label="NSE-NIFTY",
        )
        # One REAL evaluate_spec call (the 23-arg pre-built-levels ABI —
        # the retired vwap arg is gone at crate 0.4.0): the
        # empty mask is a subset of every bar's bits, so it always fires.
        bar_bits, fires, direction_u8 = trading_core.evaluate_spec(
            f"{spec.condition_mask:032x}",
            spec.direction.as_u8(),
            10 * 3600,  # tod_seconds
            100_00,  # open
            110_00,  # high
            90_00,  # low
            105_00,  # close
            None,  # ema20
            None,  # ema200
            [100_00] * 12,  # pivot levels (p,r1..r3,s1..s3,pdh,pdl,pdc,r5,s5)
            [100_00] * 15,  # fib levels
            110_00,  # day_high
            90_00,  # day_low
            100_00,  # open_of_day
            spec.pivot_proximity_bps,
            spec.pdh_pdl_proximity_bps,
            spec.fib_proximity_bps,
            spec.prior_n,
            [],  # prior flags
            False,
            False,
            False,
            False,
        )
        assert fires is True
        assert direction_u8 == 0
        assert bar_bits & spec.condition_mask == spec.condition_mask

        rid = spec_run_id(spec, **RUN_CTX)
        dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
        assert not dedup.seen(rid)
        dedup.mark(rid)
        assert dedup.seen(rid)
        # A different anchor is a DIFFERENT computation — never deduped away.
        other = spec_run_id(spec, **dict(RUN_CTX, fib_anchor=FibAnchorKind.ROLLING_WEEKLY))
        assert not dedup.seen(other)
