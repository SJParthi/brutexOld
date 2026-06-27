"""Unit tests for :mod:`brutex.strategies.combo` — FR-BF-003 + NFR-UNIQ-002."""

from __future__ import annotations

import array
import math
from decimal import Decimal
from typing import ClassVar

import pytest
from brutex.strategies.combo import canonicalise_params, combo_hash
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
            canonicalise_params({"k": {1, 2, 3}})  # type: ignore[dict-item]

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
