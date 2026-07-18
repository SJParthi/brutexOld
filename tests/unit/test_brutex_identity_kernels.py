"""Live-CPython differential + contract tests for the ``brutex_identity`` kernel.

DEC-RUST-RUNID-STORES-001. The frozen pre-conversion oracle
(``tests/golden/test_runid_stores_oracle_golden.py``) proves the conversion
changed no CAPTURED value; this suite proves the kernel's CPython-law twins
against the LIVE interpreter over generated corpora — so a CPython minor-bump
drift (repr shortest-round-trip, ``json.dumps`` escaping, ``isoformat``) or a
kernel regression fails loudly on ANY input, not only the captured ones:

* ``SnapshotHasher`` ≡ the retired blake3 streaming body (verbatim in-test
  oracle) over hostile timestamp shapes;
* the store laws (run-id validation, limb codec, top-k) against in-test
  oracles of the retired Python bodies;
* the wheel-shim handshake: ``EXPECTED_BUILD.json`` mirrors the shim's
  ``_EXPECTED_COMPILED_SYMBOLS`` (the brutex_feed convention).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import blake3
import brutex_identity
import pytest
from hypothesis import given
from hypothesis import strategies as st

# pyright: reportPrivateUsage=none

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ── the retired-Python oracles (verbatim law bodies, test-local) ─────────────


#: One (symbol, interval) → bars slice map — annotated at each literal build
#: site too, so a homogeneous literal (all-None / all-int OI) does not infer
#: an invariant-incompatible dict value type under pyright strict.
type _SliceMap = dict[tuple[str, str], list[tuple[datetime, str, str, str, str, int, int | None]]]


def _snapshot_oracle(
    exchange: str,
    segment: str,
    start: str,
    end: str,
    slices: _SliceMap,
) -> str:
    """The retired ``data_snapshot_hash`` streaming body, verbatim."""
    hasher = blake3.blake3()
    hasher.update(f"bxsnap2|{exchange}|{segment}|{start}|{end}".encode())
    for (symbol, interval), bars in sorted(slices.items()):
        hasher.update(f"\n{symbol}|{interval}|{len(bars)}".encode())
        for ts, o, h, lo, c, v, oi in bars:
            hasher.update(f"\n{ts.isoformat()}|{o}|{h}|{lo}|{c}|{v}|{oi}".encode())
    return str(hasher.hexdigest())


# ── RETIRED: the canonical-params differentials (the 2026-07-18 crate-ownership
# ruling) ────────────────────────────────────────────────────────────────────
#
# The former ``TestCanonicalDifferential`` + ``TestFloatReprEdge`` classes and
# the ``_canonicalise_value_oracle``/``_canonicalise_params_oracle`` bodies
# drove the kernel's ``canonicalise_params_json``/``combo_hash``/
# ``find_unit_separator_field`` entry points against the retired combo.py law.
# Per the coordinator-relayed crate-ownership ruling of 2026-07-18, combo
# CONSTRUCTION is a STRATEGIES concern (DEC-STRATEGIES-RUST-COMBO-001 #1955 —
# ``brutex/strategies/combo.py`` + ``rust/brutex-strategies`` own the param
# canonicaliser, the ``\x1f`` scan and the 9-field hash assembly; identity
# only CONSUMES run-id bytes), so those kernel entry points were REMOVED and
# their differentials retire WITH them. The laws stay covered by strategies'
# own suites (``test_strategies_combo.py`` · ``test_strategies_combo_rust.py``
# · ``test_combo_rust_differential.py`` · ``test_combo_rust_flag_on_golden.py``)
# and the end-to-end run-identity byte proof stays EXECUTABLE in
# ``tests/golden/test_runid_stores_oracle_golden.py`` (its canonical/combo
# sections drive the PUBLIC ``brutex.strategies.combo`` path against the
# frozen pre-conversion fixture).

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
                slices: _SliceMap = {
                    ("SYM", "1minute"): [(ts, "1.00", "2.00", "0.50", "1.50", i, None)]
                }
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
        slices: _SliceMap = {("S", "1minute"): [(ts, "1", "1", "1", "1", 1, 7)]}
        h = brutex_identity.SnapshotHasher("E", "S", "a", "b")
        h.add_slice("S", "1minute", 1)
        h.add_bar(ts, "1", "1", "1", "1", 1, 7)
        assert h.hexdigest() == _snapshot_oracle("E", "S", "a", "b", slices)


# ── hostile FFI input: the utcoffset checked-arithmetic fold ─────────────────────

#: The subprocess proof body: a hostile ``utcoffset().days`` fold must raise
#: a TYPED ValueError — never a process abort. Run out-of-process because the
#: pre-fix failure mode (an i64 overflow panic under the since-retired
#: ``panic = "abort"`` release profile) killed the WHOLE interpreter with
#: SIGABRT — an in-process regression could never report it.
_HOSTILE_UTCOFFSET_CHILD = """
import brutex_identity


class FakeOffset:
    microseconds = 0
    days = 1 << 60
    seconds = 0


class FakeTs:
    year = 2024
    month = 1
    day = 2
    hour = 9
    minute = 15
    second = 0
    microsecond = 0

    def utcoffset(self):
        return FakeOffset()


h = brutex_identity.SnapshotHasher("NSE", "INDEX", "2024-01-01", "2024-01-02")
try:
    h.add_bar(FakeTs(), "1", "1", "1", "1", 0, None)
except ValueError as exc:
    print("TYPED_VALUE_ERROR:", exc)
"""


class _HostileOffset:
    """A duck-typed ``utcoffset()`` result with arbitrary integer attributes."""

    def __init__(self, days: int, seconds: int = 0, microseconds: int = 0) -> None:
        self.days = days
        self.seconds = seconds
        self.microseconds = microseconds


class _HostileTs:
    """A duck-typed timestamp whose ``utcoffset()`` is attacker-shaped."""

    year = 2024
    month = 1
    day = 2
    hour = 9
    minute = 15
    second = 0
    microsecond = 0

    def __init__(self, offset: _HostileOffset) -> None:
        self._offset = offset

    def utcoffset(self) -> _HostileOffset:
        return self._offset


class TestHostileUtcoffsetOverflow:
    """Independent refuter round (DEC-RUST-RUNID-STORES-001): a hostile object
    whose ``utcoffset().days`` is huge overflowed the kernel's i64
    ``days * 86_400 + seconds`` fold — with the crate's
    ``overflow-checks = true`` that PANICKED, and under the since-retired
    ``panic = "abort"`` release profile the panic was an uncatchable process
    SIGABRT (reproduced pre-fix: exit 134 at python.rs's fold). The fold is
    now checked-arithmetic total: overflow = the SAME typed bad-shape
    ValueError as every other malformed timestamp. Production-unreachable
    (real ``datetime`` offsets have ``days`` ∈ {−1, 0}) — this pins the FFI
    boundary's totality, not a live pull path.
    """

    def test_subprocess_proof_no_abort(self) -> None:
        # The abort class kills the PROCESS, so the definitive regression
        # runs the hostile call in a subprocess: pre-fix this died with
        # returncode -6 (SIGABRT); post-fix it exits 0 having printed the
        # typed-ValueError marker.
        proc = subprocess.run(
            [sys.executable, "-c", _HOSTILE_UTCOFFSET_CHILD],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert proc.returncode == 0, (
            f"hostile utcoffset fold killed the process (rc={proc.returncode}; "
            f"-6/134 = SIGABRT — the overflow panic escaped): {proc.stderr[-500:]}"
        )
        assert "TYPED_VALUE_ERROR:" in proc.stdout

    @pytest.mark.parametrize(
        ("days", "seconds"),
        [
            (1 << 60, 0),
            (-(1 << 60), 0),
            (0, 1 << 62),
            (1 << 62, -(1 << 62)),
        ],
        ids=["huge-days", "huge-negative-days", "huge-seconds", "mul-overflow-only"],
    )
    def test_overflowing_offsets_raise_typed_value_error(self, days: int, seconds: int) -> None:
        h = brutex_identity.SnapshotHasher("NSE", "INDEX", "2024-01-01", "2024-01-02")
        # cast: the stub types `ts` as datetime; feeding the duck-typed hostile
        # shape IS the point (the FFI extracts attributes, not the class).
        hostile = cast(datetime, _HostileTs(_HostileOffset(days, seconds)))
        with pytest.raises(ValueError, match="bar timestamp must be a datetime"):
            h.add_bar(hostile, "1", "1", "1", "1", 0, None)

    def test_real_offset_shapes_still_accepted(self) -> None:
        # The checked fold changes NOTHING for real offsets — an IST-offset
        # aware datetime still folds identically to the streaming oracle.
        ts = datetime(2024, 1, 2, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        h = brutex_identity.SnapshotHasher("NSE", "INDEX", "2024-01-01", "2024-01-02")
        h.add_slice("S", "1minute", 1)
        h.add_bar(ts, "1", "1", "1", "1", 0, None)
        slices: _SliceMap = {("S", "1minute"): [(ts, "1", "1", "1", "1", 0, None)]}
        assert h.hexdigest() == _snapshot_oracle("NSE", "INDEX", "2024-01-01", "2024-01-02", slices)


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
        # The doctor freshness stage fingerprints the compiled .so surface as
        # {non-underscore names} ∪ {"__version__"} (the trading_core/fno_math
        # convention), so EXPECTED_BUILD.json carries __version__ too
        # (DEC-RUST-RUNID-STORES-001, independent refuter round 2 — the
        # brutex_identity doctor stages).
        shim_symbols = {
            name for name in brutex_identity._EXPECTED_COMPILED_SYMBOLS if not name.startswith("__")
        }
        assert set(expected_build["symbols"]) == shim_symbols | {"__version__"}

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
        def keys_for(ids: set[str], market: str) -> frozenset[tuple[str, str, str, str, str]]:
            return frozenset((market, sid, "1minute", "2024-01-01", "2024-01-31") for sid in ids)

        pruned = keys_for(pruned_ids, "groww")
        reopened = keys_for(reopened_ids, "groww")
        degraded = keys_for(degraded_ids, "groww")
        got = brutex_identity.merge_ledger_rows(memory_rows, disk_rows, pruned, reopened, degraded)
        assert got == _merge_oracle(memory_rows, disk_rows, pruned, reopened, degraded)
