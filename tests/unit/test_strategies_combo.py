"""Unit tests for :mod:`brutex.strategies.combo` — FR-BF-003 + NFR-UNIQ-002."""

from __future__ import annotations

import array
import math
from decimal import Decimal

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
    _SNAPSHOT = "a" * 64
    _COMMIT = "b" * 12

    def test_returns_64_hex_chars(self) -> None:
        h = combo_hash(
            strategy_id="ma_cross",
            params={"fast": 5, "slow": 20},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        assert len(h) == 64
        int(h, 16)  # asserts every char is a valid hex digit

    def test_deterministic(self) -> None:
        h1 = combo_hash(
            strategy_id="ma_cross",
            params={"fast": 5, "slow": 20},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        h2 = combo_hash(
            strategy_id="ma_cross",
            params={"slow": 20, "fast": 5},  # reorder
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        assert h1 == h2

    def test_different_strategy_different_hash(self) -> None:
        params = {"fast": 5, "slow": 20}
        h_a = combo_hash(
            strategy_id="ma_cross",
            params=params,
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        h_b = combo_hash(
            strategy_id="rsi_mr",
            params=params,
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        assert h_a != h_b

    def test_different_params_different_hash(self) -> None:
        h_a = combo_hash(
            strategy_id="ma_cross",
            params={"fast": 5},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        h_b = combo_hash(
            strategy_id="ma_cross",
            params={"fast": 6},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha=self._COMMIT,
        )
        assert h_a != h_b

    def test_different_snapshot_different_hash(self) -> None:
        h_a = combo_hash(
            strategy_id="x",
            params={},
            data_snapshot_hash="a" * 64,
            code_commit_sha=self._COMMIT,
        )
        h_b = combo_hash(
            strategy_id="x",
            params={},
            data_snapshot_hash="b" * 64,
            code_commit_sha=self._COMMIT,
        )
        assert h_a != h_b

    def test_different_commit_different_hash(self) -> None:
        h_a = combo_hash(
            strategy_id="x",
            params={},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha="aaa",
        )
        h_b = combo_hash(
            strategy_id="x",
            params={},
            data_snapshot_hash=self._SNAPSHOT,
            code_commit_sha="bbb",
        )
        assert h_a != h_b

    def test_empty_strategy_id_rejected(self) -> None:
        with pytest.raises(StrategyError, match="strategy_id"):
            combo_hash(
                strategy_id="",
                params={},
                data_snapshot_hash=self._SNAPSHOT,
                code_commit_sha=self._COMMIT,
            )

    def test_empty_snapshot_rejected(self) -> None:
        with pytest.raises(StrategyError, match="data_snapshot_hash"):
            combo_hash(
                strategy_id="x",
                params={},
                data_snapshot_hash="",
                code_commit_sha=self._COMMIT,
            )

    def test_empty_commit_rejected(self) -> None:
        with pytest.raises(StrategyError, match="code_commit_sha"):
            combo_hash(
                strategy_id="x",
                params={},
                data_snapshot_hash=self._SNAPSHOT,
                code_commit_sha="",
            )
