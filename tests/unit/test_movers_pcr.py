"""Unit tests for the /movers PCR adapter (DEC-MOVERS-OPTIONS-PCR-001).

Pure, in-memory — no lake, no FastAPI. Proves :func:`compute_pcr_rows`'s
grouping/emission law (one row per ``legs_total`` key, sorted by the PARSED
underlying, including the all-skipped ``empty`` chain — the #919 silent-vanish
lesson read-side), its fail-loud caller contracts, the rename/collision
structural safety, and the :func:`pcr_row_json` wire serialization — with one
byte-pinned golden fixture.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from brutex.dashboard.movers_pcr import PcrLeg, UnderlyingPcr, compute_pcr_rows, pcr_row_json
from brutex.data.pcr import RatioStatus

_EXP = date(2025, 6, 26)
_EXP_B = date(2025, 7, 3)


def _leg(underlying: str, opt_type: str, *, oi: int | None, volume: int) -> PcrLeg:
    return PcrLeg(underlying=underlying, opt_type=opt_type, oi=oi, volume=volume)


class TestComputePcrRows:
    def test_multi_underlying_grouping_sorted_by_parsed_underlying(self) -> None:
        legs = [
            _leg("NIFTY", "PE", oi=300, volume=30),
            _leg("BANKNIFTY", "CE", oi=100, volume=10),
            _leg("NIFTY", "CE", oi=200, volume=20),
            _leg("BANKNIFTY", "PE", oi=50, volume=5),
        ]
        rows = compute_pcr_rows(
            legs,
            legs_total={"NIFTY": 2, "BANKNIFTY": 2},
            target_expiry={"NIFTY": _EXP, "BANKNIFTY": _EXP_B},
        )
        assert [r.underlying for r in rows] == ["BANKNIFTY", "NIFTY"]  # parsed-sorted
        bank, nifty = rows
        assert bank.expiry == _EXP_B
        assert nifty.expiry == _EXP
        assert nifty.pcr.put_oi == 300
        assert nifty.pcr.call_oi == 200
        assert nifty.pcr.oi_pcr == Decimal("1.5")
        assert nifty.pcr.vol_pcr == Decimal("1.5")
        assert bank.pcr.oi_pcr == Decimal("0.5")

    def test_all_skipped_underlying_still_emits_an_empty_row(self) -> None:
        # A chain that exists but was unreadable is REPORTED, never silently
        # absent: zero sums, both statuses zero_denominator, coverage "empty".
        rows = compute_pcr_rows(
            [_leg("NIFTY", "CE", oi=10, volume=1)],
            legs_total={"NIFTY": 1, "GHOST": 4},
            target_expiry={"NIFTY": _EXP, "GHOST": _EXP_B},
        )
        assert [r.underlying for r in rows] == ["GHOST", "NIFTY"]
        ghost = rows[0]
        assert ghost.pcr.coverage.legs_read == 0
        assert ghost.pcr.coverage.legs_total == 4
        assert ghost.pcr.oi.status is RatioStatus.ZERO_DENOMINATOR
        assert ghost.pcr.volume.status is RatioStatus.ZERO_DENOMINATOR
        assert (ghost.pcr.put_oi, ghost.pcr.call_oi) == (0, 0)
        assert ghost.pcr.coverage_label == "empty"
        assert ghost.pcr.coverage_note() == "0/4 legs read"

    def test_leg_outside_legs_total_raises_loudly(self) -> None:
        with pytest.raises(ValueError, match="not a legs_total key"):
            compute_pcr_rows(
                [_leg("STRAY", "CE", oi=1, volume=1)],
                legs_total={"NIFTY": 1},
                target_expiry={"NIFTY": _EXP},
            )

    def test_legs_total_undercount_raises_via_coverage(self) -> None:
        with pytest.raises(ValueError, match="exceeds legs_total"):
            compute_pcr_rows(
                [_leg("NIFTY", "CE", oi=1, volume=1), _leg("NIFTY", "PE", oi=1, volume=1)],
                legs_total={"NIFTY": 1},
                target_expiry={"NIFTY": _EXP},
            )

    def test_missing_target_expiry_key_propagates_keyerror(self) -> None:
        with pytest.raises(KeyError):
            compute_pcr_rows(
                [_leg("NIFTY", "CE", oi=1, volume=1)],
                legs_total={"NIFTY": 1},
                target_expiry={},
            )

    def test_display_names_none_is_identity(self) -> None:
        rows = compute_pcr_rows(
            [_leg("NIFTY", "CE", oi=1, volume=1)],
            legs_total={"NIFTY": 1},
            target_expiry={"NIFTY": _EXP},
        )
        assert rows[0].display_underlying == rows[0].underlying == "NIFTY"

    def test_rename_keeps_the_parsed_group_key(self) -> None:
        rows = compute_pcr_rows(
            [_leg("NIFTY", "CE", oi=1, volume=1)],
            legs_total={"NIFTY": 1},
            target_expiry={"NIFTY": _EXP},
            display_names={"NIFTY": "NIFTY 50"},
        )
        assert rows[0].underlying == "NIFTY"
        assert rows[0].display_underlying == "NIFTY 50"

    def test_display_collision_never_merges_the_sums(self) -> None:
        # Two parsed underlyings sharing ONE display name = TWO rows with the
        # same display label and distinct parsed keys — sums never merged.
        rows = compute_pcr_rows(
            [
                _leg("AAA", "PE", oi=10, volume=1),
                _leg("BBB", "PE", oi=90, volume=9),
            ],
            legs_total={"AAA": 1, "BBB": 1},
            target_expiry={"AAA": _EXP, "BBB": _EXP},
            display_names={"AAA": "SAME", "BBB": "SAME"},
        )
        assert [r.display_underlying for r in rows] == ["SAME", "SAME"]
        assert [r.underlying for r in rows] == ["AAA", "BBB"]
        assert [r.pcr.put_oi for r in rows] == [10, 90]

    def test_empty_inputs_yield_no_rows(self) -> None:
        assert compute_pcr_rows([], legs_total={}, target_expiry={}) == ()


class TestPcrRowJson:
    def test_full_precision_string_and_null_serialization(self) -> None:
        rows = compute_pcr_rows(
            [
                _leg("NIFTY", "PE", oi=117_000, volume=46_000),
                _leg("NIFTY", "CE", oi=134_000, volume=0),
                _leg("NIFTY", "PE", oi=None, volume=100),
            ],
            legs_total={"NIFTY": 5},
            target_expiry={"NIFTY": _EXP},
        )
        payload = pcr_row_json(rows[0])
        assert payload["underlying"] == "NIFTY"
        assert payload["display_underlying"] == "NIFTY"
        assert payload["expiry"] == "2025-06-26"
        # FULL-precision Decimal string — never rounded server-side.
        assert payload["oi_pcr"] == "0.8731343283582089552238805970"
        assert payload["oi_status"] == "ok"
        # Zero call-volume denominator → honest null + the typed sentinel string.
        assert payload["vol_pcr"] is None
        assert payload["vol_status"] == "zero_denominator"
        assert payload["put_oi"] == 117_000
        assert payload["call_oi"] == 134_000
        assert payload["put_volume"] == 46_100
        assert payload["call_volume"] == 0
        assert payload["oi_missing_legs"] == 1
        assert payload["volume_missing_legs"] == 0
        assert payload["legs_read"] == 3
        assert payload["legs_total"] == 5
        assert payload["coverage"] == "partial"
        assert payload["coverage_note"] == "3/5 legs read · 1 leg(s) missing OI"

    def test_ok_vol_and_null_oi_are_independent(self) -> None:
        rows = compute_pcr_rows(
            [
                _leg("NIFTY", "PE", oi=None, volume=30),
                _leg("NIFTY", "CE", oi=None, volume=20),
            ],
            legs_total={"NIFTY": 2},
            target_expiry={"NIFTY": _EXP},
        )
        payload = pcr_row_json(rows[0])
        assert payload["oi_pcr"] is None
        assert payload["oi_status"] == "zero_denominator"
        assert payload["vol_pcr"] == "1.5"
        assert payload["vol_status"] == "ok"

    def test_golden_serialized_row_is_byte_pinned(self) -> None:
        # The full wire shape, byte-pinned (ordering + strings + statuses) — a
        # serialization drift fails HERE before it ships (golden flavour).
        rows = compute_pcr_rows(
            [
                _leg("NIFTY", "PE", oi=300, volume=30),
                _leg("NIFTY", "CE", oi=200, volume=20),
            ],
            legs_total={"NIFTY": 2},
            target_expiry={"NIFTY": _EXP},
            display_names={"NIFTY": "NIFTY 50"},
        )
        assert json.dumps(pcr_row_json(rows[0]), sort_keys=False) == (
            '{"underlying": "NIFTY", "display_underlying": "NIFTY 50", '
            '"expiry": "2025-06-26", "oi_pcr": "1.5", "oi_status": "ok", '
            '"vol_pcr": "1.5", "vol_status": "ok", "put_oi": 300, "call_oi": 200, '
            '"put_volume": 30, "call_volume": 20, "oi_missing_legs": 0, '
            '"volume_missing_legs": 0, "legs_read": 2, "legs_total": 2, '
            '"coverage": "full", "coverage_note": null}'
        )

    def test_dataclasses_are_frozen(self) -> None:
        leg = _leg("NIFTY", "CE", oi=1, volume=1)
        with pytest.raises(AttributeError):
            leg.oi = 2  # type: ignore[misc]
        row = compute_pcr_rows([leg], legs_total={"NIFTY": 1}, target_expiry={"NIFTY": _EXP})[0]
        assert isinstance(row, UnderlyingPcr)
        with pytest.raises(AttributeError):
            row.underlying = "X"  # type: ignore[misc]
