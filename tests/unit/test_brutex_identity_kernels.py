"""Live-CPython differential + contract tests for the ``brutex_identity`` kernel.

DEC-RUST-RUNID-STORES-001. The frozen pre-conversion oracle
(``tests/golden/test_runid_stores_oracle_golden.py``) proves the conversion
changed no CAPTURED value; this suite proves the kernel's CPython-law twins
against the LIVE interpreter over generated corpora — so a CPython minor-bump
drift (repr shortest-round-trip, ``json.dumps`` escaping, ``isoformat``) or a
kernel regression fails loudly on ANY input, not only the captured ones:

* ``canonicalise_params_json`` ≡ the retired composition
  ``json.dumps(canonical-walk, sort_keys=True, separators=(",", ":"),
  ensure_ascii=True)`` — floats via ``repr``, Decimals via ``str`` (hypothesis
  + directed boundary corpora);
* ``SnapshotHasher`` ≡ the retired blake3 streaming body (verbatim in-test
  oracle) over hostile timestamp shapes;
* the store laws (run-id validation, limb codec, top-k) against in-test
  oracles of the retired Python bodies;
* the wheel-shim handshake: ``EXPECTED_BUILD.json`` mirrors the shim's
  ``_EXPECTED_COMPILED_SYMBOLS`` (the brutex_feed convention).
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import blake3
import brutex_identity
import pytest
from hypothesis import given
from hypothesis import strategies as st

# pyright: reportPrivateUsage=none

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ── the retired-Python oracles (verbatim law bodies, test-local) ─────────────


def _canonicalise_value_oracle(value: Any) -> Any:
    """The retired ``_canonicalise_value`` walk, verbatim (test-local oracle)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return repr(value + 0.0)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {k: _canonicalise_value_oracle(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_canonicalise_value_oracle(v) for v in value]
    raise AssertionError(f"oracle: unsupported {type(value)}")


def _canonicalise_params_oracle(params: dict[str, Any]) -> str:
    return json.dumps(
        _canonicalise_value_oracle(params),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _snapshot_oracle(
    exchange: str,
    segment: str,
    start: str,
    end: str,
    slices: dict[tuple[str, str], list[tuple[datetime, str, str, str, str, int, int | None]]],
) -> str:
    """The retired ``data_snapshot_hash`` streaming body, verbatim."""
    hasher = blake3.blake3()
    hasher.update(f"bxsnap2|{exchange}|{segment}|{start}|{end}".encode())
    for (symbol, interval), bars in sorted(slices.items()):
        hasher.update(f"\n{symbol}|{interval}|{len(bars)}".encode())
        for ts, o, h, lo, c, v, oi in bars:
            hasher.update(f"\n{ts.isoformat()}|{o}|{h}|{lo}|{c}|{v}|{oi}".encode())
    return str(hasher.hexdigest())


# ── canonical params: CPython repr/json.dumps differentials ──────────────────

#: Directed float boundary corpus — the shortest-repr law's hardest cases.
_FLOAT_BOUNDARIES = [
    0.0,
    -0.0,
    0.5,
    0.1,
    1e15,
    1e16,
    1e-4,
    1e-5,
    5e-324,
    2.2250738585072014e-308,
    1.7976931348623157e308,
    9007199254740993.0,
    123456789012345.6,
    2.5e-10,
    1e100,
    1.2345678901234567,
    -1.5,
    3.0000000000000004,
]


class TestCanonicalDifferential:
    def test_directed_float_boundaries(self) -> None:
        params = {f"f{i}": v for i, v in enumerate(_FLOAT_BOUNDARIES)}
        assert brutex_identity.canonicalise_params_json(params) == _canonicalise_params_oracle(
            params
        )

    @given(st.floats(allow_nan=False, allow_infinity=False))
    def test_float_repr_law_over_the_full_plane(self, value: float) -> None:
        params = {"x": value}
        assert brutex_identity.canonicalise_params_json(params) == _canonicalise_params_oracle(
            params
        )

    @given(st.text(alphabet=st.characters(codec="utf-8"), max_size=64))
    def test_string_escape_law_over_hostile_text(self, text: str) -> None:
        params = {"s": text, text or "k": 1}
        assert brutex_identity.canonicalise_params_json(params) == _canonicalise_params_oracle(
            params
        )

    @given(
        st.dictionaries(
            st.text(alphabet=st.characters(codec="utf-8"), max_size=16),
            st.recursive(
                st.none()
                | st.booleans()
                | st.integers()
                | st.floats(allow_nan=False, allow_infinity=False)
                | st.text(max_size=16)
                | st.decimals(allow_nan=False, allow_infinity=False),
                lambda inner: (
                    st.lists(inner, max_size=3)
                    | st.dictionaries(st.text(max_size=8), inner, max_size=3)
                ),
                max_leaves=12,
            ),
            max_size=6,
        )
    )
    def test_nested_structures_differential(self, params: dict[str, Any]) -> None:
        assert brutex_identity.canonicalise_params_json(params) == _canonicalise_params_oracle(
            params
        )

    def test_int_bignum_and_decimal_scale(self) -> None:
        params = {
            "big": 2**200,
            "neg": -(3**150),
            "i64": 2**63 - 1,
            "d1": Decimal("1.00"),
            "d2": Decimal("1E+5"),
            "d3": Decimal("-0"),
        }
        assert brutex_identity.canonicalise_params_json(params) == _canonicalise_params_oracle(
            params
        )

    def test_duplicate_mapping_keys_keep_last(self) -> None:
        # A generic Mapping whose items() yields a duplicate key — the retired
        # dict-comprehension semantics keep the LAST value.
        class Dupes:
            def keys(self) -> list[str]:  # pragma: no cover - Mapping shape
                return ["k", "k"]

            def __getitem__(self, key: str) -> int:  # pragma: no cover
                return 0

            def items(self) -> list[tuple[str, int]]:
                return [("k", 1), ("k", 2)]

            def __len__(self) -> int:  # pragma: no cover - Mapping shape
                return 1

            def __iter__(self) -> Any:  # pragma: no cover - Mapping shape
                return iter(["k"])

        from collections.abc import Mapping

        Mapping.register(Dupes)
        assert brutex_identity.canonicalise_params_json(Dupes()) == '{"k":2}'

    def test_lone_surrogate_is_rejected_typed(self) -> None:
        # The DISCLOSED narrowing: the retired body hashed the raw code point;
        # the kernel rejects invalid Unicode with a typed error.
        bad = "a\udc80b"
        with pytest.raises(ValueError, match="lone surrogate"):
            brutex_identity.canonicalise_params_json({"s": bad})
        with pytest.raises(ValueError, match="lone surrogate"):
            brutex_identity.canonicalise_params_json({bad: 1})

    def test_depth_cap_boundary(self) -> None:
        def nested(depth: int) -> dict[str, Any]:
            leaf: dict[str, Any] = {"leaf": "x"}
            for _ in range(depth):
                leaf = {"d": leaf}
            return leaf

        brutex_identity.canonicalise_params_json(nested(63))
        with pytest.raises(ValueError, match="nesting deeper than 64"):
            brutex_identity.canonicalise_params_json(nested(64))


# ── snapshot hasher: isoformat + framing differential ────────────────────────

_TZ_SHAPES = [
    UTC,
    timezone(timedelta(hours=5, minutes=30)),
    timezone(timedelta(hours=-9, minutes=-30)),
    timezone(timedelta(hours=1, minutes=2, seconds=3)),
    timezone(timedelta(0)),
    None,  # naive
]


class TestSnapshotDifferential:
    def test_timestamp_shapes_match_isoformat_law(self) -> None:
        for i, tz in enumerate(_TZ_SHAPES):
            for micro in (0, 1, 123456):
                ts = datetime(2024, 3, 5, 9, 15, 7, micro, tzinfo=tz)
                slices = {("SYM", "1minute"): [(ts, "1.00", "2.00", "0.50", "1.50", i, None)]}
                hasher = brutex_identity.SnapshotHasher("NSE", "INDEX", "2024-01-01", "2024-12-31")
                hasher.add_slice("SYM", "1minute", 1)
                hasher.add_bar(ts, "1.00", "2.00", "0.50", "1.50", i, None)
                assert hasher.hexdigest() == _snapshot_oracle(
                    "NSE", "INDEX", "2024-01-01", "2024-12-31", slices
                ), (tz, micro)

    def test_none_oi_and_zero_oi_are_distinct(self) -> None:
        ts = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)

        def digest(oi: int | None) -> str:
            h = brutex_identity.SnapshotHasher("NSE", "FNO", "a", "b")
            h.add_slice("S", "1minute", 1)
            h.add_bar(ts, "1", "1", "1", "1", 0, oi)
            return h.hexdigest()

        assert digest(None) != digest(0)

    @given(
        st.datetimes(
            min_value=datetime(1970, 1, 1),
            max_value=datetime(9999, 12, 30),
            timezones=st.none() | st.timezones(),
        )
    )
    def test_isoformat_differential_over_hostile_datetimes(self, ts: datetime) -> None:
        # Whole-second-offset zones only (the CandleBar reality; chrono
        # FixedOffset is second-granular). st.timezones() yields real IANA
        # zones — all whole-second offsets.
        slices = {("S", "1minute"): [(ts, "1", "1", "1", "1", 1, 7)]}
        h = brutex_identity.SnapshotHasher("E", "S", "a", "b")
        h.add_slice("S", "1minute", 1)
        h.add_bar(ts, "1", "1", "1", "1", 1, 7)
        assert h.hexdigest() == _snapshot_oracle("E", "S", "a", "b", slices)


# ── store laws: retired-body oracles ─────────────────────────────────────────


class TestStoreLawDifferential:
    @given(st.integers(min_value=0, max_value=(1 << 128) - 1))
    def test_limb_codec_round_trips_and_matches_the_offset_law(self, mask: int) -> None:
        lo, hi = brutex_identity.mask_to_limbs(mask)
        # The retired _encode_limb law: limb - 2**63, per u64 limb.
        assert lo == (mask & ((1 << 64) - 1)) - (1 << 63)
        assert hi == (mask >> 64) - (1 << 63)
        assert -(2**63) <= lo <= 2**63 - 1
        assert -(2**63) <= hi <= 2**63 - 1
        assert brutex_identity.mask_from_limbs(lo, hi) == mask

    def test_run_id_validation_decision_matches_the_retired_law(self) -> None:
        cases = [
            ("ab" * 32, None),
            ("abc", "length"),
            ("AB" * 32, "case"),
            ("zz" * 32, "hex"),
            ("g" + "a" * 63, "hex"),
        ]
        for run_id, expected in cases:
            message = brutex_identity.validate_run_id_error(run_id, 64)
            if expected is None:
                assert message is None, run_id
            elif expected == "length":
                assert message is not None and "64-char BLAKE3 hex" in message
            elif expected == "case":
                assert message is not None and "lowercase hex" in message
            else:
                assert message is not None and "not valid hex" in message

    @given(
        st.lists(
            st.tuples(
                st.integers(min_value=-(2**62), max_value=2**62),
                st.text("abcdef0123456789", min_size=4, max_size=4),
            ),
            min_size=1,
            max_size=16,
        ),
        st.integers(min_value=1, max_value=8),
        st.booleans(),
    )
    def test_top_k_matches_the_retired_two_pass_sort(
        self, rows: list[tuple[int, str]], k: int, descending: bool
    ) -> None:
        run_ids = [f"{suffix}{i:02d}" for i, (_, suffix) in enumerate(rows)]
        keys = [value for value, _ in rows]
        got = brutex_identity.rank_top_k(run_ids, keys, k, descending)
        # The retired law: run_id asc, then stable sort by key.
        order = sorted(range(len(rows)), key=lambda i: run_ids[i])
        order.sort(key=lambda i: keys[i], reverse=descending)
        assert got == order[:k]

    def test_exact_int_ranking_above_float_precision(self) -> None:
        run_ids = ["a", "b"]
        keys = [2**60, 2**60 + 1]  # float-equal, int-distinct
        assert float(keys[0]) == float(keys[1])
        assert brutex_identity.rank_top_k(run_ids, keys, 2, True) == [1, 0]


# ── the wheel-shim handshake pins (the brutex_feed convention) ───────────────


class TestWheelHandshake:
    def test_expected_build_mirrors_the_shim_symbol_set(self) -> None:
        expected_build = json.loads(
            (
                _REPO_ROOT
                / "rust"
                / "brutex-identity"
                / "python"
                / "brutex_identity"
                / "EXPECTED_BUILD.json"
            ).read_text(encoding="utf-8")
        )
        shim_symbols = {
            name for name in brutex_identity._EXPECTED_COMPILED_SYMBOLS if not name.startswith("__")
        }
        assert set(expected_build["symbols"]) == shim_symbols

    def test_expected_build_crate_version_matches_the_wheel(self) -> None:
        expected_build = json.loads(
            (
                _REPO_ROOT
                / "rust"
                / "brutex-identity"
                / "python"
                / "brutex_identity"
                / "EXPECTED_BUILD.json"
            ).read_text(encoding="utf-8")
        )
        assert expected_build["crate_version"] == brutex_identity.version()

    def test_source_digest_is_baked_and_hex(self) -> None:
        digest = brutex_identity.SOURCE_DIGEST
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")
        assert digest == brutex_identity.__source_digest__

    def test_ledger_column_vocabulary(self) -> None:
        assert len(brutex_identity.LEDGER_COLUMNS) == 16
        assert len(brutex_identity.LEDGER_CORE_COLUMNS) == 11
        assert tuple(brutex_identity.LEDGER_COLUMNS[:11]) == tuple(
            brutex_identity.LEDGER_CORE_COLUMNS
        )


# ── the ledger merge law: retired-body oracle differential ───────────────────


def _row_preference_oracle(row: tuple[Any, ...]) -> tuple[int, str, int, tuple[str, ...]]:
    """The retired ``_row_preference`` law, verbatim (test-local oracle)."""
    rank = {"SUCCESS": 3, "SKIPPED": 3, "EMPTY": 2, "FAILED": 1, "PLANNED": 0}
    return (
        rank.get(row[5], -1),
        row[10] or "",
        row[7],
        tuple("" if v is None else str(v) for v in row),
    )


def _merge_oracle(
    memory_rows: list[tuple[Any, ...]],
    disk_rows: list[tuple[Any, ...]],
    pruned: frozenset[tuple[str, ...]],
    reopened: frozenset[tuple[str, ...]],
    degraded: frozenset[tuple[str, ...]],
) -> list[tuple[Any, ...]]:
    key = lambda r: (r[0], r[1], r[2], r[3], r[4])  # noqa: E731 - oracle brevity
    merged = {key(r): r for r in memory_rows}
    for row in disk_rows:
        k = key(row)
        mine = merged.get(k)
        if mine is None:
            if k not in pruned:
                merged[k] = row
            continue
        if k in reopened or k in degraded:
            continue
        if _row_preference_oracle(row) > _row_preference_oracle(mine):
            merged[k] = row
    return sorted(merged.values(), key=key)


_ROW_STRATEGY = st.tuples(
    st.sampled_from(["groww", "dhan"]),
    st.sampled_from(["NSE-A", "NSE-B", "NSE-C"]),
    st.sampled_from(["1minute", "1day"]),
    st.just("2024-01-01"),
    st.just("2024-01-31"),
    st.sampled_from(["SUCCESS", "SKIPPED", "EMPTY", "FAILED", "PLANNED", "JUNK"]),
    st.integers(min_value=0, max_value=500),
    st.integers(min_value=0, max_value=9),
    st.none() | st.sampled_from(["err-a", "err-b"]),
    st.just("2024-01-31T16:00:00+00:00"),
    st.none() | st.sampled_from(["2024-01-30T16:00:00+00:00", "2024-01-31T16:00:00+00:00"]),
    st.integers(min_value=0, max_value=5),
    st.integers(min_value=0, max_value=5),
    st.none() | st.integers(min_value=0, max_value=500),
    st.none() | st.integers(min_value=0, max_value=5),
    st.none() | st.integers(min_value=0, max_value=5),
)


class TestMergeLawDifferential:
    @given(
        st.lists(_ROW_STRATEGY, max_size=8),
        st.lists(_ROW_STRATEGY, max_size=8),
        st.sets(st.sampled_from(["NSE-A", "NSE-B", "NSE-C"]), max_size=2),
        st.sets(st.sampled_from(["NSE-A", "NSE-B", "NSE-C"]), max_size=2),
        st.sets(st.sampled_from(["NSE-A", "NSE-B", "NSE-C"]), max_size=2),
    )
    def test_merge_matches_the_retired_law_over_hostile_corpora(
        self,
        memory_rows: list[tuple[Any, ...]],
        disk_rows: list[tuple[Any, ...]],
        pruned_ids: set[str],
        reopened_ids: set[str],
        degraded_ids: set[str],
    ) -> None:
        def keys_for(ids: set[str], market: str) -> frozenset[tuple[str, ...]]:
            return frozenset((market, sid, "1minute", "2024-01-01", "2024-01-31") for sid in ids)

        pruned = keys_for(pruned_ids, "groww")
        reopened = keys_for(reopened_ids, "groww")
        degraded = keys_for(degraded_ids, "groww")
        got = brutex_identity.merge_ledger_rows(memory_rows, disk_rows, pruned, reopened, degraded)
        assert got == _merge_oracle(memory_rows, disk_rows, pruned, reopened, degraded)


# ── the float repr twin exercised directly through canonical output ──────────


class TestFloatReprEdge:
    @given(st.integers(min_value=0, max_value=2**64 - 1))
    def test_bit_pattern_floats_round_trip_the_repr_law(self, bits: int) -> None:
        import struct

        value = struct.unpack("<d", struct.pack("<Q", bits))[0]
        if not math.isfinite(value):
            return  # rejected by both sides — covered elsewhere
        params = {"x": value}
        assert brutex_identity.canonicalise_params_json(params) == _canonicalise_params_oracle(
            params
        )
