"""Unit tests for :mod:`brutex.strategies.combo` — FR-BF-003 + NFR-UNIQ-002."""

# The depth-cap boundary test imports the private ``_MAX_CANONICALISE_DEPTH``
# constant deliberately (it is the contract under test); silence the
# import-time private-usage diagnostic file-wide (the repo convention).
# pyright: reportPrivateUsage=none
from __future__ import annotations

import array
import math
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from brutex.strategies.combo import (
    _MAX_CANONICALISE_DEPTH,
    UNIT_SEPARATOR,
    canonicalise_params,
    combo_hash,
    reject_unit_separator,
)
from brutex.strategies.errors import StrategyError


class TestCanonicaliseParams:
    def test_empty_dict(self) -> None:
        assert canonicalise_params({}) == "{}"

    def test_sorted_keys(self) -> None:
        """Key ordering is independent of insertion order."""
        a = canonicalise_params({"b": 1, "a": 2, "c": 3})
        b = canonicalise_params({"c": 3, "a": 2, "b": 1})
        assert a == b

    def test_nested_dict_recurses(self) -> None:
        out = canonicalise_params({"outer": {"inner_b": 2, "inner_a": 1}})
        # Inner dict keys must also be sorted.
        assert '"inner_a":1' in out
        assert out.index("inner_a") < out.index("inner_b")

    def test_list_preserves_order(self) -> None:
        """Lists are ordered — canonicalisation must preserve that."""
        a = canonicalise_params({"k": [1, 2, 3]})
        b = canonicalise_params({"k": [3, 2, 1]})
        assert a != b

    def test_tuple_treated_as_list(self) -> None:
        """Tuples round-trip identically to lists with the same content."""
        a = canonicalise_params({"k": (1, 2, 3)})
        b = canonicalise_params({"k": [1, 2, 3]})
        assert a == b

    def test_float_repr_stability(self) -> None:
        """1.0 and 1.00 canonicalise to the same string."""
        a = canonicalise_params({"k": 1.0})
        b = canonicalise_params({"k": 1.00})
        assert a == b

    def test_different_floats_hash_different(self) -> None:
        """Meaningful precision must not be collapsed (within IEEE-754 resolution)."""
        # 1.0 + 1e-16 == 1.0 in float64 (next representable is ~1 + 2.22e-16).
        # Use a gap that actually exists in the bit pattern.
        a = canonicalise_params({"k": 1.0})
        b = canonicalise_params({"k": 1.0 + 1e-12})
        assert a != b

    def test_nan_float_rejected(self) -> None:
        with pytest.raises(StrategyError, match="NaN"):
            canonicalise_params({"k": math.nan})

    def test_signed_zero_collapses_to_positive(self) -> None:
        """GAP #17 (DEC-SWEEP-DEDUP-LMDB-001): -0.0 and +0.0 are mathematically
        equal, so they MUST canonicalise to the same string — otherwise two
        equal params hash to different run-identities (breaks reproducibility /
        Golden Rule #4)."""
        assert canonicalise_params({"k": -0.0}) == canonicalise_params({"k": 0.0})

    def test_signed_zero_collapses_nested(self) -> None:
        """The -0.0 → +0.0 normalisation applies recursively (nested + in lists)."""
        assert canonicalise_params({"o": {"k": -0.0}}) == canonicalise_params({"o": {"k": 0.0}})
        assert canonicalise_params({"k": [-0.0, 1.5]}) == canonicalise_params({"k": [0.0, 1.5]})

    def test_signed_zero_run_identity_is_stable(self) -> None:
        """Two combos differing only by the sign of a zero param share one
        run-identity (the reproducibility intent of the fix)."""
        ctx = {
            "strategy_id": "mask:0000000000000001",
            "direction": "bull",
            "instrument": "NIFTY",
            "tf": "1m",
            "mode": "A",
            "data_snapshot_hash": "snap0001",
            "indicator_version": "ind-v1",
            "code_commit_sha": "deadbeef",
        }
        neg = combo_hash(params={"k": -0.0}, **ctx)
        pos = combo_hash(params={"k": 0.0}, **ctx)
        assert neg == pos

    def test_non_string_key_rejected(self) -> None:
        with pytest.raises(StrategyError, match="dict keys must be str"):
            canonicalise_params({1: "x"})  # type: ignore[dict-item]

    def test_unsupported_type_rejected(self) -> None:
        with pytest.raises(StrategyError, match="unsupported"):
            canonicalise_params({"k": {1, 2, 3}})

    def test_none_value(self) -> None:
        out = canonicalise_params({"k": None})
        assert out == '{"k":null}'

    def test_bool_values(self) -> None:
        """bool must round-trip as JSON ``true`` / ``false``."""
        out = canonicalise_params({"flag_a": True, "flag_b": False})
        assert '"flag_a":true' in out
        assert '"flag_b":false' in out

    def test_str_value(self) -> None:
        """String values (not just keys) flow through unchanged."""
        out = canonicalise_params({"label": "mean_revert"})
        assert out == '{"label":"mean_revert"}'

    def test_decimal_value(self) -> None:
        """Decimal values round-trip via ``str()`` for exact precision."""
        out = canonicalise_params({"price": Decimal("123.456")})
        assert out == '{"price":"123.456"}'

    def test_custom_sequence_value(self) -> None:
        """Non-list/tuple Sequence (e.g. ``array.array``) is serialised
        as a list so indicator params can use ``array`` storage without
        surprise rejections."""
        out = canonicalise_params({"k": array.array("i", [1, 2, 3])})
        assert out == '{"k":[1,2,3]}'


class TestComboHash:
    # DEC-P0-060 (PR-N+7): combo_hash migrated from 4-field SHA256 to 9-field
    # BLAKE3 (`strategy_id ‖ direction ‖ instrument ‖ tf ‖ mode ‖
    # canonical_params ‖ data_snapshot_hash ‖ indicator_version ‖
    # code_commit_sha`). Wire format unchanged (64-char hex / 32-byte raw);
    # these tests still exercise length, determinism, diff-input/diff-output,
    # and empty-input-rejection — now across the 9 inputs.
    _SNAPSHOT = "a" * 64
    _COMMIT = "b" * 12
    # Default-fill the 5 new fields with placeholders that the engine PR
    # (G-03 GAP) will replace with real values.
    _BASE: ClassVar[dict[str, str]] = {
        "direction": "bull",
        "instrument": "NIFTY",
        "tf": "15m",
        "mode": "A",
        "indicator_version": "v1",
    }

    def test_returns_64_hex_chars(self) -> None:
        h = combo_hash(
            strategy_id="ma_cross",
            **self._BASE,
            params={"fast": 5, "slow": 20},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        assert len(h) == 64
        int(h, 16)  # asserts every char is a valid hex digit

    def test_deterministic(self) -> None:
        h1 = combo_hash(
            strategy_id="ma_cross",
            **self._BASE,
            params={"fast": 5, "slow": 20},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        h2 = combo_hash(
            strategy_id="ma_cross",
            **self._BASE,
            params={"slow": 20, "fast": 5},  # reorder
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        assert h1 == h2

    def test_different_strategy_different_hash(self) -> None:
        params = {"fast": 5, "slow": 20}
        h_a = combo_hash(
            strategy_id="ma_cross",
            **self._BASE,
            params=params,
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        h_b = combo_hash(
            strategy_id="rsi_mr",
            **self._BASE,
            params=params,
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        assert h_a != h_b

    def test_different_params_different_hash(self) -> None:
        h_a = combo_hash(
            strategy_id="ma_cross",
            **self._BASE,
            params={"fast": 5},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        h_b = combo_hash(
            strategy_id="ma_cross",
            **self._BASE,
            params={"fast": 6},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        assert h_a != h_b

    def test_different_snapshot_different_hash(self) -> None:
        h_a = combo_hash(
            strategy_id="x",
            **self._BASE,
            params={},
            data_snapshot_hash="a" * 64,
            code_commit_sha=self._COMMIT,
        )
        h_b = combo_hash(
            strategy_id="x",
            **self._BASE,
            params={},
            data_snapshot_hash="b" * 64,
            code_commit_sha=self._COMMIT,
        )
        assert h_a != h_b

    def test_different_commit_different_hash(self) -> None:
        h_a = combo_hash(
            strategy_id="x",
            **self._BASE,
            params={},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha="aaa",
        )
        h_b = combo_hash(
            strategy_id="x",
            **self._BASE,
            params={},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha="bbb",
        )
        assert h_a != h_b

    def test_different_direction_different_hash(self) -> None:
        kwargs = dict(self._BASE)
        kwargs.pop("direction")
        h_a = combo_hash(
            strategy_id="x",
            direction="bull",
            **kwargs,
            params={},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        h_b = combo_hash(
            strategy_id="x",
            direction="bear",
            **kwargs,
            params={},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        assert h_a != h_b

    def test_different_instrument_different_hash(self) -> None:
        kwargs = dict(self._BASE)
        kwargs.pop("instrument")
        h_a = combo_hash(
            strategy_id="x",
            instrument="NIFTY",
            **kwargs,
            params={},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        h_b = combo_hash(
            strategy_id="x",
            instrument="BANKNIFTY",
            **kwargs,
            params={},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        assert h_a != h_b

    def test_different_tf_different_hash(self) -> None:
        kwargs = dict(self._BASE)
        kwargs.pop("tf")
        h_a = combo_hash(
            strategy_id="x",
            tf="15m",
            **kwargs,
            params={},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        h_b = combo_hash(
            strategy_id="x",
            tf="60m",
            **kwargs,
            params={},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        assert h_a != h_b

    def test_different_mode_different_hash(self) -> None:
        kwargs = dict(self._BASE)
        kwargs.pop("mode")
        h_a = combo_hash(
            strategy_id="x",
            mode="A",
            **kwargs,
            params={},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        h_b = combo_hash(
            strategy_id="x",
            mode="B",
            **kwargs,
            params={},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        assert h_a != h_b

    def test_different_indicator_version_different_hash(self) -> None:
        kwargs = dict(self._BASE)
        kwargs.pop("indicator_version")
        h_a = combo_hash(
            strategy_id="x",
            indicator_version="v1",
            **kwargs,
            params={},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        h_b = combo_hash(
            strategy_id="x",
            indicator_version="v2",
            **kwargs,
            params={},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        assert h_a != h_b

    def test_empty_strategy_id_rejected(self) -> None:
        with pytest.raises(StrategyError, match="strategy_id"):
            combo_hash(
                strategy_id="",
                **self._BASE,
                params={},
                data_snapshot_hash=self._SNAPSHOT,
                code_commit_sha=self._COMMIT,
            )

    def test_empty_direction_rejected(self) -> None:
        kwargs = dict(self._BASE)
        kwargs["direction"] = ""
        with pytest.raises(StrategyError, match="direction"):
            combo_hash(
                strategy_id="x",
                **kwargs,
                params={},
                data_snapshot_hash=self._SNAPSHOT,
                code_commit_sha=self._COMMIT,
            )

    def test_empty_instrument_rejected(self) -> None:
        kwargs = dict(self._BASE)
        kwargs["instrument"] = ""
        with pytest.raises(StrategyError, match="instrument"):
            combo_hash(
                strategy_id="x",
                **kwargs,
                params={},
                data_snapshot_hash=self._SNAPSHOT,
                code_commit_sha=self._COMMIT,
            )

    def test_empty_tf_rejected(self) -> None:
        kwargs = dict(self._BASE)
        kwargs["tf"] = ""
        with pytest.raises(StrategyError, match="tf"):
            combo_hash(
                strategy_id="x",
                **kwargs,
                params={},
                data_snapshot_hash=self._SNAPSHOT,
                code_commit_sha=self._COMMIT,
            )

    def test_empty_mode_rejected(self) -> None:
        kwargs = dict(self._BASE)
        kwargs["mode"] = ""
        with pytest.raises(StrategyError, match="mode"):
            combo_hash(
                strategy_id="x",
                **kwargs,
                params={},
                data_snapshot_hash=self._SNAPSHOT,
                code_commit_sha=self._COMMIT,
            )

    def test_empty_indicator_version_rejected(self) -> None:
        kwargs = dict(self._BASE)
        kwargs["indicator_version"] = ""
        with pytest.raises(StrategyError, match="indicator_version"):
            combo_hash(
                strategy_id="x",
                **kwargs,
                params={},
                data_snapshot_hash=self._SNAPSHOT,
                code_commit_sha=self._COMMIT,
            )

    def test_empty_snapshot_rejected(self) -> None:
        with pytest.raises(StrategyError, match="data_snapshot_hash"):
            combo_hash(
                strategy_id="x",
                **self._BASE,
                params={},
                data_snapshot_hash="",
                code_commit_sha=self._COMMIT,
            )

    def test_empty_commit_rejected(self) -> None:
        with pytest.raises(StrategyError, match="code_commit_sha"):
            combo_hash(
                strategy_id="x",
                **self._BASE,
                params={},
                data_snapshot_hash=self._SNAPSHOT,
                code_commit_sha="",
            )


# ────────────────────────────────────────────────────────────────────────
# (A) \x1f unit-separator injection rejection
# ────────────────────────────────────────────────────────────────────────


class TestComboHashSeparatorRejection:
    """The 9-field payload joins raw fields on ``\\x1f``; a field carrying
    that byte could forge a different combo's identity — it must be rejected
    BEFORE any digest byte is produced (REJECT-ONLY: no valid digest changes)."""

    _SNAPSHOT = "a" * 64
    _COMMIT = "b" * 12
    _BASE: ClassVar[dict[str, str]] = {
        "strategy_id": "x",
        "direction": "bull",
        "instrument": "NIFTY",
        "tf": "15m",
        "mode": "A",
        "data_snapshot_hash": _SNAPSHOT,
        "indicator_version": "v1",
        "code_commit_sha": _COMMIT,
    }

    @pytest.mark.parametrize(
        "field",
        [
            "strategy_id",
            "direction",
            "instrument",
            "tf",
            "mode",
            "data_snapshot_hash",
            "indicator_version",
            "code_commit_sha",
        ],
    )
    def test_combo_hash_rejects_unit_separator_in_each_field(self, field: str) -> None:
        kwargs = dict(self._BASE)
        kwargs[field] = "x\x1fy"
        with pytest.raises(StrategyError, match=field):
            combo_hash(**kwargs, params={})

    def test_combo_hash_injection_collision_now_rejected(self) -> None:
        """The classic split-injection pair (instrument=``NIFTY\\x1fX``,tf=``Y``
        vs instrument=``NIFTY``,tf=``X\\x1fY``) would have collided on the old
        join; both now raise instead of forging an identity."""
        kwargs = dict(self._BASE)
        kwargs.pop("instrument")
        kwargs.pop("tf")
        with pytest.raises(StrategyError, match="instrument"):
            combo_hash(instrument="NIFTY\x1fX", tf="Y", **kwargs, params={})
        with pytest.raises(StrategyError, match="tf"):
            combo_hash(instrument="NIFTY", tf="X\x1fY", **kwargs, params={})


# ────────────────────────────────────────────────────────────────────────
# (B) Non-finite float / Decimal rejection (NaN already covered above)
# ────────────────────────────────────────────────────────────────────────


class TestNonFiniteRejection:
    @pytest.mark.parametrize("value", [math.inf, -math.inf])
    def test_infinity_float_rejected(self, value: float) -> None:
        with pytest.raises(StrategyError, match="non-finite floats"):
            canonicalise_params({"k": value})

    @pytest.mark.parametrize("text", ["NaN", "sNaN"])
    def test_decimal_nan_rejected(self, text: str) -> None:
        with pytest.raises(StrategyError, match="non-finite Decimals"):
            canonicalise_params({"k": Decimal(text)})

    @pytest.mark.parametrize("text", ["Infinity", "-Infinity"])
    def test_decimal_infinity_rejected(self, text: str) -> None:
        with pytest.raises(StrategyError, match="non-finite Decimals"):
            canonicalise_params({"k": Decimal(text)})

    def test_nested_nan_in_list_rejected(self) -> None:
        with pytest.raises(StrategyError, match="non-finite floats"):
            canonicalise_params({"k": [1.0, math.nan, 2.0]})

    def test_nested_nan_in_nested_dict_rejected(self) -> None:
        with pytest.raises(StrategyError, match="non-finite floats"):
            canonicalise_params({"outer": {"inner": math.nan}})


# ────────────────────────────────────────────────────────────────────────
# (C) Top-level Mapping guard
# ────────────────────────────────────────────────────────────────────────


class TestTopLevelMappingGuard:
    @pytest.mark.parametrize(
        "value",
        [None, [1, 2], 5, "hi", (1, 2), 3.5, True],
    )
    def test_non_mapping_top_level_rejected(self, value: Any) -> None:
        with pytest.raises(StrategyError, match="must be a Mapping"):
            canonicalise_params(value)

    def test_empty_mapping_still_accepted(self) -> None:
        assert canonicalise_params({}) == "{}"


# ────────────────────────────────────────────────────────────────────────
# (D) numpy scalar / array rejection (float subclass + unsupported types)
# ────────────────────────────────────────────────────────────────────────


class TestNumpyRejection:
    def test_numpy_float64_rejected(self) -> None:
        np = pytest.importorskip("numpy")
        with pytest.raises(StrategyError, match="float subclasses"):
            canonicalise_params({"k": np.float64(1.5)})

    def test_numpy_int64_rejected(self) -> None:
        np = pytest.importorskip("numpy")
        with pytest.raises(StrategyError, match="unsupported value type"):
            canonicalise_params({"k": np.int64(5)})

    def test_numpy_bool_rejected(self) -> None:
        np = pytest.importorskip("numpy")
        with pytest.raises(StrategyError, match="unsupported value type"):
            canonicalise_params({"k": np.bool_(True)})

    def test_numpy_ndarray_rejected(self) -> None:
        np = pytest.importorskip("numpy")
        with pytest.raises(StrategyError, match="unsupported value type"):
            canonicalise_params({"k": np.array([1, 2, 3])})

    def test_builtin_float_unaffected(self) -> None:
        np = pytest.importorskip("numpy")
        del np  # only gating: a built-in float is never rejected
        assert canonicalise_params({"k": 1.5}) == '{"k":"1.5"}'


# ────────────────────────────────────────────────────────────────────────
# (I) Depth cap — no RecursionError, clean StrategyError
# ────────────────────────────────────────────────────────────────────────


class TestDepthCap:
    def test_deeply_nested_param_raises_strategyerror(self) -> None:
        value: Any = "leaf"
        for _ in range(200):
            value = {"k": value}
        with pytest.raises(StrategyError) as excinfo:
            canonicalise_params(value)
        assert "nesting deeper" in str(excinfo.value)
        # The guard must pre-empt the interpreter's RecursionError.
        assert not isinstance(excinfo.value, RecursionError)

    def test_depth_boundary(self) -> None:
        """A structure at the max accepted depth passes; one deeper raises.

        The top params dict is visited at ``_depth=0`` and recurses into its
        value at ``_depth=1``; a string leaf wrapped in ``_MAX_CANONICALISE_DEPTH``
        dict levels is visited at exactly that depth (``depth > MAX`` False), so
        it passes, while one more level raises (calibrated empirically)."""

        def nested(levels: int) -> Any:
            value: Any = "leaf"
            for _ in range(levels):
                value = {"k": value}
            return value

        # Exactly at the cap — accepted.
        canonicalise_params(nested(_MAX_CANONICALISE_DEPTH))
        # One level deeper — rejected.
        with pytest.raises(StrategyError, match="nesting deeper"):
            canonicalise_params(nested(_MAX_CANONICALISE_DEPTH + 1))


# ────────────────────────────────────────────────────────────────────────
# Part-5 type-acceptance pins (current behaviour, wire-locked)
# ────────────────────────────────────────────────────────────────────────


class TestTypeAcceptancePins:
    def test_bytes_value_rejected(self) -> None:
        with pytest.raises(StrategyError, match="unsupported value type bytes"):
            canonicalise_params({"k": b"x"})

    def test_bytearray_currently_accepted_as_array(self) -> None:
        # bytearray is a mutable Sequence of ints — pinned to the array form.
        assert canonicalise_params({"k": bytearray(b"x")}) == '{"k":[120]}'

    def test_memoryview_currently_accepted_as_array(self) -> None:
        assert canonicalise_params({"k": memoryview(b"x")}) == '{"k":[120]}'

    def test_set_rejected(self) -> None:
        with pytest.raises(StrategyError, match="unsupported value type set"):
            canonicalise_params({"k": {1, 2, 3}})


# ────────────────────────────────────────────────────────────────────────
# (H) Wire-locked alias / scale / signed-zero pins (canonical-string level)
# ────────────────────────────────────────────────────────────────────────


class TestWireLockedCanonical:
    def test_float_str_decimal_alias_same_canonical(self) -> None:
        """A float, str, and Decimal carrying the same text collapse to one
        token — wire-locked (mitigated by one-type-per-axis at ParamGrid)."""
        as_float = canonicalise_params({"sl": 1.5})
        as_str = canonicalise_params({"sl": "1.5"})
        as_decimal = canonicalise_params({"sl": Decimal("1.5")})
        assert as_float == as_str == as_decimal == '{"sl":"1.5"}'

    def test_int_does_not_alias_numeric_string(self) -> None:
        assert canonicalise_params({"x": 5}) == '{"x":5}'
        assert canonicalise_params({"x": 5}) != canonicalise_params({"x": "5.0"})
        assert canonicalise_params({"x": 5}) != canonicalise_params({"x": Decimal("5")})

    def test_decimal_scale_significant(self) -> None:
        one_0 = canonicalise_params({"x": Decimal("1.0")})
        one_00 = canonicalise_params({"x": Decimal("1.00")})
        one_e2 = canonicalise_params({"x": Decimal("1E+2")})
        hundred = canonicalise_params({"x": Decimal("100")})
        assert one_0 != one_00 != one_e2 != hundred
        assert len({one_0, one_00, one_e2, hundred}) == 4

    def test_signed_zero_collapses(self) -> None:
        # DEC-SWEEP-DEDUP-LMDB-001: -0.0 is normalised to +0.0 BEFORE repr so
        # two mathematically-equal zero params share ONE run-identity (Golden
        # Rule #4 reproducibility) — superseding the earlier split-zero pin.
        assert canonicalise_params({"x": 0.0}) == '{"x":"0.0"}'
        assert canonicalise_params({"x": -0.0}) == '{"x":"0.0"}'
        assert canonicalise_params({"x": 0.0}) == canonicalise_params({"x": -0.0})

    def test_tuple_list_range_canonicalise_identically(self) -> None:
        as_list = canonicalise_params({"k": [0, 1, 2]})
        as_tuple = canonicalise_params({"k": (0, 1, 2)})
        as_range = canonicalise_params({"k": range(3)})
        assert as_list == as_tuple == as_range == '{"k":[0,1,2]}'


class TestRejectUnitSeparatorHelper:
    """The COMMON reject-only guard itself (DEC-RUN-IDENTITY-GUARD-PARITY-001).

    ``combo_hash`` + the three engine run-identity mirrors all delegate here;
    these tests pin the helper's own contract: the injected error class fires
    (both the combo arm and an engine-style arm), clean fields pass silently,
    and the message reproduces the historical ``combo_hash`` bytes exactly.
    """

    def test_unit_separator_constant_is_x1f(self) -> None:
        assert UNIT_SEPARATOR == "\x1f"

    def test_raises_the_injected_strategy_error(self) -> None:
        with pytest.raises(StrategyError, match="bad_field"):
            reject_unit_separator("combo_hash", (("bad_field", "a\x1fb"),), error=StrategyError)

    def test_raises_the_injected_value_error(self) -> None:
        # The engine mirrors inject their OWN error classes — each call site
        # keeps its error contract; the helper never hardcodes StrategyError.
        with pytest.raises(ValueError, match="schedule_hash"):
            reject_unit_separator(
                "_walk_forward_run_hash",
                (("schedule_hash", "x\x1fy"),),
                error=ValueError,
            )

    def test_clean_fields_pass_silently(self) -> None:
        reject_unit_separator(
            "combo_hash",
            (("a", "clean"), ("b", ""), ("c", "also-clean")),
            error=StrategyError,
        )  # no raise — REJECT-ONLY, legal inputs untouched

    def test_first_offending_field_wins(self) -> None:
        with pytest.raises(StrategyError, match="first"):
            reject_unit_separator(
                "ctx",
                (("first", "x\x1fy"), ("second", "x\x1fy")),
                error=StrategyError,
            )

    def test_message_reproduces_the_historical_combo_hash_bytes(self) -> None:
        with pytest.raises(StrategyError) as exc_info:
            reject_unit_separator("combo_hash", (("tf", "x\x1fy"),), error=StrategyError)
        assert str(exc_info.value) == (
            "combo_hash: tf must not contain the \\x1f "
            "unit-separator byte (it is the field join delimiter)"
        )
