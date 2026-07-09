"""Golden locks for :mod:`brutex.engine.twin_detect` (DEC-SWEEP-RESULTS-DEPTH-001 WP4).

Two frozen oracles:

1. **The fingerprint law** — a fixed, fully hand-specified 3-trade list is
   locked THREE ways: the exact canonical serialization string (the ``bxtwin1``
   law, spelled out byte-for-byte), the BLAKE3 digest of that string, and the
   :func:`trade_fingerprint` output — all must agree. The empty-list digest is
   locked too (every zero-trade combo shares it).
2. **A fixed family fixture** — a 6-candidate pool spanning three behaviors
   (an exact-twin pair + its near-twin, a loner, and a CROSS-BUCKET exact-twin
   pair) is grouped and the resulting partition is locked as literal mappings
   plus a sha256 of its canonical JSON — and re-grouped from reversed input
   to pin re-run/order stability of every ``family_id``.

Any change to the serialization law, the hash, the grouping rules or the
representative law moves a lock and MUST be a deliberate, ledgered re-lock
(the ``bxtwin2`` version-bump discipline — never a silent drift).

Regenerate (deterministic) after a DELIBERATE law change::

    .venv/bin/python -c "import tests.golden.test_twin_fingerprint_golden as g; g.print_locks()"
"""

from __future__ import annotations

import hashlib
import json

import blake3
from brutex.engine.trade_capture import CapturedTrade
from brutex.engine.twin_detect import TwinCandidate, group_twins, trade_fingerprint
from brutex.strategies.types import Side

from tests.fakes.wp4_twin_fixtures import ist_ts, make_captured_trade

# --- lock 1: the fingerprint law ------------------------------------------

#: The EXACT canonical serialization of the fixed 3-trade list below
#: (2024-01-02; entries 09:17 / 10:05 / 13:45 IST as UTC epoch µs).
_GOLDEN_SERIALIZATION = (
    "bxtwin1|"
    "1704167220000000|1704168720000000|BUY|1250\n"
    "1704170100000000|1704175200000000|SELL|-475\n"
    "1704183300000000|1704189000000000|BUY|0"
)

#: Locked BLAKE3 hex of :data:`_GOLDEN_SERIALIZATION`.
_GOLDEN_FINGERPRINT = "6ebcd9594fc1a23bff5b38ee799e9c537ad95cf76356925bcbb44e8166d3361c"

#: Locked BLAKE3 hex of the bare ``bxtwin1|`` prefix (the zero-trade combo).
_EMPTY_FINGERPRINT = "96f7a1310661bd4ddfa86916ab7579087c0ed7912f6cc0911022b74b872a8779"

# --- lock 2: the family fixture -------------------------------------------

_EXPECTED_FAMILY_BY_RUN_ID = {
    "run-a": "run-b",
    "run-b": "run-b",
    "run-c": "run-b",
    "run-d": "run-d",
    "run-e": "run-f",
    "run-f": "run-f",
}
_EXPECTED_MEMBERS_BY_FAMILY = {
    "run-b": ("run-a", "run-b", "run-c"),
    "run-d": ("run-d",),
    "run-f": ("run-e", "run-f"),
}

#: sha256 of the partition's canonical JSON (sorted keys, compact separators).
_FAMILY_JSON_SHA256 = "2718f2523f9e6dce8dc5ad507bfe8ee06e23df1ae81f6945343b2a06df7261b8"


def _golden_trades() -> list[CapturedTrade]:
    """The fixed 3-trade identity fixture (fresh objects per call)."""
    return [
        make_captured_trade(
            entry_ts=ist_ts(9, 17), exit_ts=ist_ts(9, 42), direction=Side.BUY, pnl_paisa=1_250
        ),
        make_captured_trade(
            entry_ts=ist_ts(10, 5), exit_ts=ist_ts(11, 30), direction=Side.SELL, pnl_paisa=-475
        ),
        make_captured_trade(
            entry_ts=ist_ts(13, 45), exit_ts=ist_ts(15, 20), direction=Side.BUY, pnl_paisa=0
        ),
    ]


def _near_twin_trades() -> list[CapturedTrade]:
    """The golden list with ONE pnl moved by a tick — same entries, tier-2 twin."""
    trades = _golden_trades()
    middle = trades[1]
    trades[1] = make_captured_trade(
        entry_ts=middle.entry_ts,
        exit_ts=middle.exit_ts,
        direction=middle.direction,
        pnl_paisa=-474,
    )
    return trades


def _other_bucket_trades() -> list[CapturedTrade]:
    """A second behavior, shared verbatim by two candidates from DIFFERENT buckets."""
    return [
        make_captured_trade(
            entry_ts=ist_ts(10, 0), exit_ts=ist_ts(10, 30), direction=Side.BUY, pnl_paisa=300
        ),
        make_captured_trade(
            entry_ts=ist_ts(14, 0), exit_ts=ist_ts(14, 45), direction=Side.SELL, pnl_paisa=-100
        ),
    ]


def _loner_trades() -> list[CapturedTrade]:
    return [
        make_captured_trade(
            entry_ts=ist_ts(9, 30), exit_ts=ist_ts(9, 55), direction=Side.SELL, pnl_paisa=42
        )
    ]


def _fixture_pool() -> list[TwinCandidate]:
    """Six candidates → three behavior families (design D golden fixture).

    * ``run-a``/``run-b`` — exact twins (identical trade lists; b is simpler);
    * ``run-c`` — their tier-2 near-twin (same entries, one pnl off a tick);
    * ``run-d`` — a loner;
    * ``run-e``/``run-f`` — exact twins pooled from a DIFFERENT bucket
      (cross-bucket by construction; f is simpler).
    """
    return [
        TwinCandidate.from_trades(run_id="run-a", mask_bits=0b111, trades=_golden_trades()),
        TwinCandidate.from_trades(run_id="run-b", mask_bits=0b11, trades=_golden_trades()),
        TwinCandidate.from_trades(run_id="run-c", mask_bits=0b11111, trades=_near_twin_trades()),
        TwinCandidate.from_trades(run_id="run-d", mask_bits=0b1, trades=_loner_trades()),
        TwinCandidate.from_trades(run_id="run-e", mask_bits=0b1111, trades=_other_bucket_trades()),
        TwinCandidate.from_trades(
            run_id="run-f", mask_bits=0b110000, trades=_other_bucket_trades()
        ),
    ]


def _family_canonical_json() -> str:
    families = group_twins(_fixture_pool())
    return json.dumps(
        {
            "family_by_run_id": dict(families.family_by_run_id),
            "members_by_family": {k: list(v) for k, v in families.members_by_family.items()},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


class TestFingerprintGolden:
    def test_serialization_law_and_digest_locked(self) -> None:
        # The three-way agreement: law text → blake3 → trade_fingerprint.
        assert (
            blake3.blake3(_GOLDEN_SERIALIZATION.encode("utf-8")).hexdigest() == _GOLDEN_FINGERPRINT
        )
        assert trade_fingerprint(_golden_trades()) == _GOLDEN_FINGERPRINT

    def test_empty_list_digest_locked(self) -> None:
        assert trade_fingerprint([]) == _EMPTY_FINGERPRINT
        assert blake3.blake3(b"bxtwin1|").hexdigest() == _EMPTY_FINGERPRINT


class TestFamilyFixtureGolden:
    def test_partition_locked(self) -> None:
        families = group_twins(_fixture_pool())
        assert dict(families.family_by_run_id) == _EXPECTED_FAMILY_BY_RUN_ID
        assert dict(families.members_by_family) == _EXPECTED_MEMBERS_BY_FAMILY

    def test_partition_canonical_json_sha256_locked(self) -> None:
        digest = hashlib.sha256(_family_canonical_json().encode("utf-8")).hexdigest()
        assert digest == _FAMILY_JSON_SHA256

    def test_family_ids_stable_across_rebuild_and_input_order(self) -> None:
        # The R34 stability pin: rebuilding from fresh objects AND feeding the
        # pool in reversed order both land on the IDENTICAL locked partition.
        first = group_twins(_fixture_pool())
        rebuilt = group_twins(_fixture_pool())
        reversed_input = group_twins(list(reversed(_fixture_pool())))
        assert first == rebuilt == reversed_input
        assert dict(first.family_by_run_id) == _EXPECTED_FAMILY_BY_RUN_ID


def print_locks() -> None:  # pragma: no cover - manual regeneration helper
    """Print the current lock values (run manually after a DELIBERATE change)."""
    print("_GOLDEN_FINGERPRINT =", trade_fingerprint(_golden_trades()))
    print("_EMPTY_FINGERPRINT =", trade_fingerprint([]))
    families = group_twins(_fixture_pool())
    print("family_by_run_id =", dict(families.family_by_run_id))
    print("members_by_family =", dict(families.members_by_family))
    digest = hashlib.sha256(_family_canonical_json().encode("utf-8")).hexdigest()
    print("_FAMILY_JSON_SHA256 =", digest)
