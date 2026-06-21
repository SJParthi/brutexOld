"""Unit tests for the instrument-master structural integrity audit (DEC-INSTRUMENT-MASTER-INTEGRITY-001).

Pure, offline, over hand-built `Instrument` lists (no store/lake/network). 100% line +
branch over `audit_master_integrity` + `report_to_dict`, covering every leg bucket
(resolved_both / symbol_only / token_only / orphan / ambiguous / disagreement /
empty_underlying), dedup, spots-with-no-legs, special chars, the alias bridge, and the
healthy path.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from brutex.data.master_integrity import (
    GATING_REASONS,
    ORPHAN_SAMPLE_CAP,
    REASON_AMBIGUOUS,
    REASON_DISAGREEMENT,
    REASON_EMPTY_UNDERLYING,
    REASON_ORPHAN,
    audit_master_integrity,
    report_from_dict,
    report_to_dict,
)
from brutex.models import Exchange, Instrument, InstrumentType, Segment

_NOW = datetime(2026, 6, 21, tzinfo=UTC)
_EXPIRY = date(2024, 1, 25)


def _spot(
    symbol: str,
    *,
    sid: int,
    kind: InstrumentType = InstrumentType.EQ,
    feed_token: str | None = None,
    exch: Exchange = Exchange.NSE,
) -> Instrument:
    """An IDX/EQ spot row. `feed_token` numeric ⇒ a genuine token-joinable security_id."""
    return Instrument(
        exch_id=exch,
        security_id=sid,
        symbol_name=symbol,
        display_name=symbol,
        instrument=kind,
        segment=Segment.INDEX if kind is InstrumentType.IDX else Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0 if kind is InstrumentType.IDX else 1,
        tick_size=Decimal("0") if kind is InstrumentType.IDX else Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
        feed_token=feed_token,
    )


def _leg(
    symbol: str,
    *,
    sid: int,
    kind: InstrumentType = InstrumentType.CE,
    under_sym: str | None,
    under_sid: int | None,
    exch: Exchange = Exchange.NSE,
) -> Instrument:
    """A FUT/CE/PE derivative leg pointing at an underlying by symbol and/or token."""
    return Instrument(
        exch_id=exch,
        security_id=sid,
        symbol_name=symbol,
        display_name=symbol,
        instrument=kind,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=under_sym,
        underlying_security_id=under_sid,
        lot_size=50,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=_EXPIRY,
        strike_price=Decimal("100") if kind is not InstrumentType.FUT else None,
        asm_gsm_flag=False,
    )


# ── healthy + buckets ─────────────────────────────────────────────────────────


def test_healthy_resolved_both_is_green() -> None:
    rep = audit_master_integrity(
        [
            _spot("RELIANCE", sid=100, feed_token="100"),
            _leg("RELIANCE24JAN3000CE", sid=900, under_sym="RELIANCE", under_sid=100),
        ],
        now=_NOW,
    )
    assert rep.is_green
    assert rep.resolved_both == 1
    assert rep.type_counts.spots == 1 and rep.type_counts.legs == 1
    assert rep.gating_failures == 0


def test_orphan_leg_is_red() -> None:
    rep = audit_master_integrity(
        [
            _spot("RELIANCE", sid=100, feed_token="100"),
            _leg("NOPE24JANCE", sid=901, under_sym="NOPE", under_sid=999),
        ],
        now=_NOW,
    )
    assert not rep.is_green
    assert rep.orphan_count == 1
    assert rep.orphan_legs[0].reason == REASON_ORPHAN


def test_leg_pointing_at_another_leg_is_orphan() -> None:
    # An option whose underlying names ANOTHER leg (not a spot) is an orphan — spot maps
    # only ever contain IDX/EQ rows.
    rep = audit_master_integrity(
        [
            _leg("AAA24JANFUT", sid=800, kind=InstrumentType.FUT, under_sym="ZZZ", under_sid=None),
            _leg("ZZZ24JANCE", sid=801, under_sym="AAA24JANFUT", under_sid=None),
        ],
        now=_NOW,
    )
    assert rep.orphan_count == 2  # neither leg names a spot
    assert not rep.is_green


def test_spot_with_no_legs_is_green() -> None:
    rep = audit_master_integrity([_spot("NIFTY", sid=1, kind=InstrumentType.IDX)], now=_NOW)
    assert rep.is_green
    assert rep.spots_with_no_legs == 1
    assert rep.type_counts.legs == 0


def test_empty_underlying_is_red() -> None:
    rep = audit_master_integrity(
        [_leg("X24JANFUT", sid=802, kind=InstrumentType.FUT, under_sym="", under_sid=None)],
        now=_NOW,
    )
    assert rep.empty_underlying_count == 1
    assert rep.orphan_legs[0].reason == REASON_EMPTY_UNDERLYING
    assert not rep.is_green


def test_token_only_resolution_special_char_symbol() -> None:
    # M&M: spot symbol "M&M" with a genuine numeric token; the leg's underlying_symbol does
    # NOT match the spot symbol, but its numeric token does → resolved_token_only, green.
    rep = audit_master_integrity(
        [
            _spot("M&M", sid=2031, feed_token="2031"),
            _leg("MAHINDRA24JANCE", sid=903, under_sym="MAHINDRA", under_sid=2031),
        ],
        now=_NOW,
    )
    assert rep.is_green
    assert rep.resolved_token_only == 1
    assert "M&M" not in rep.special_char_underlyings  # the SPOT symbol is not a leg underlying
    # The leg's own underlying ("MAHINDRA") has no special char; assert the bucket, not the set.


def test_symbol_only_resolution_nse_index_surrogate_token() -> None:
    # NSE index: non-numeric feed_token ⇒ surrogate security_id, NOT token-joinable. The leg
    # resolves by SYMBOL only (token miss) → resolved_symbol_only, green.
    rep = audit_master_integrity(
        [
            _spot("NIFTY", sid=99999, kind=InstrumentType.IDX, feed_token="NIFTY"),
            _leg("NIFTY24JANCE", sid=904, under_sym="NIFTY", under_sid=12345),
        ],
        now=_NOW,
    )
    assert rep.is_green
    assert rep.resolved_symbol_only == 1


def test_symbol_token_disagreement_is_red() -> None:
    rep = audit_master_integrity(
        [
            _spot("AAA", sid=1, feed_token="1"),
            _spot("BBB", sid=2, feed_token="2"),
            _leg("LEG24JANCE", sid=905, under_sym="AAA", under_sid=2),  # symbol→AAA, token→BBB
        ],
        now=_NOW,
    )
    assert rep.disagreement_count == 1
    assert rep.orphan_legs[0].reason == REASON_DISAGREEMENT
    assert not rep.is_green


def test_ambiguous_symbol_is_red() -> None:
    # Two distinct spots share (NSE, "DUP") → the symbol key is ambiguous; a leg naming it
    # cannot be resolved to one spot.
    rep = audit_master_integrity(
        [
            _spot("DUP", sid=10, feed_token="10"),
            _spot("DUP", sid=11, feed_token="11"),
            _leg("DUP24JANCE", sid=906, under_sym="DUP", under_sid=None),
        ],
        now=_NOW,
    )
    assert rep.ambiguous_count == 1
    assert rep.orphan_legs[0].reason == REASON_AMBIGUOUS
    assert not rep.is_green


def test_alias_bridge_resolves_leg_to_spot() -> None:
    # Spot symbol "NIFTYJR"; the leg's underlying is the F&O form "NIFTYNXT50". The alias map
    # {spot: fno} is reversed so the leg resolves to the spot by symbol.
    rep = audit_master_integrity(
        [
            _spot("NIFTYJR", sid=5, kind=InstrumentType.IDX, feed_token="NIFTYJR"),
            _leg("NIFTYNXT5024JANCE", sid=907, under_sym="NIFTYNXT50", under_sid=None),
        ],
        index_aliases={"NIFTYJR": "NIFTYNXT50"},
        now=_NOW,
    )
    assert rep.is_green
    assert rep.resolved_symbol_only == 1


def test_dedup_collision_counted() -> None:
    spot = _spot("RELIANCE", sid=100, feed_token="100")
    rep = audit_master_integrity([spot, spot], now=_NOW)  # identical row twice
    assert rep.dedup_collisions == 1


def test_special_char_underlyings_collected() -> None:
    rep = audit_master_integrity(
        [
            _spot("M&M", sid=2031, feed_token="2031"),
            _spot("BAJAJ-AUTO", sid=16669, feed_token="16669"),
            _leg("MM24JANCE", sid=910, under_sym="M&M", under_sid=2031),
            _leg("BAJ24JANCE", sid=911, under_sym="BAJAJ-AUTO", under_sid=16669),
        ],
        now=_NOW,
    )
    assert rep.special_char_underlyings == ("BAJAJ-AUTO", "M&M")
    assert rep.is_green  # both resolve both-ways


def test_orphan_sample_capped_but_count_exact() -> None:
    legs = [
        _leg(f"ORPH{i}CE", sid=5000 + i, under_sym=f"GHOST{i}", under_sid=None)
        for i in range(ORPHAN_SAMPLE_CAP + 25)
    ]
    rep = audit_master_integrity(legs, now=_NOW)
    assert rep.orphan_count == ORPHAN_SAMPLE_CAP + 25  # exact
    assert len(rep.orphan_legs) == ORPHAN_SAMPLE_CAP  # sample capped


def test_fut_and_options_of_same_underlying_all_resolve() -> None:
    rep = audit_master_integrity(
        [
            _spot("RELIANCE", sid=100, feed_token="100"),
            _leg(
                "R24JANFUT", sid=920, kind=InstrumentType.FUT, under_sym="RELIANCE", under_sid=100
            ),
            _leg(
                "R24JAN3000CE", sid=921, kind=InstrumentType.CE, under_sym="RELIANCE", under_sid=100
            ),
            _leg(
                "R24JAN3000PE", sid=922, kind=InstrumentType.PE, under_sym="RELIANCE", under_sid=100
            ),
        ],
        now=_NOW,
    )
    assert rep.is_green
    assert rep.resolved_both == 3
    assert rep.type_counts.fut == 1 and rep.type_counts.ce == 1 and rep.type_counts.pe == 1
    assert rep.spots_with_no_legs == 0


def test_empty_master() -> None:
    rep = audit_master_integrity([], now=_NOW)
    assert rep.is_green
    assert rep.total_rows == 0
    assert rep.type_counts.spots == 0 and rep.type_counts.legs == 0


# ── report_to_dict ────────────────────────────────────────────────────────────


def test_report_to_dict_shape() -> None:
    rep = audit_master_integrity(
        [
            _spot("RELIANCE", sid=100, feed_token="100"),
            _leg("NOPECE", sid=930, under_sym="NOPE", under_sid=None),
        ],
        now=_NOW,
        run_id="run-1",
        csv_source_sha256="abc",
    )
    d = report_to_dict(rep)
    assert d["is_green"] is False
    assert d["run_id"] == "run-1" and d["csv_source_sha256"] == "abc"
    assert d["type_counts"]["spots"] == 1 and d["type_counts"]["legs"] == 1
    assert d["gating_failures"] == 1
    assert d["orphan_legs"][0]["reason"] == REASON_ORPHAN
    assert d["orphan_legs"][0]["underlying_symbol"] == "NOPE"


def test_report_from_dict_round_trips() -> None:
    rep = audit_master_integrity(
        [
            _spot("M&M", sid=2031, feed_token="2031"),
            _leg("MM24JANCE", sid=903, under_sym="MAHINDRA", under_sid=2031),
            _leg("NOPECE", sid=930, under_sym="NOPE", under_sid=None),
        ],
        now=_NOW,
        run_id="r1",
        csv_source_sha256="abc",
    )
    restored = report_from_dict(report_to_dict(rep))
    assert restored == rep  # frozen dataclass equality — exact round-trip


def test_gating_reasons_constant() -> None:
    assert set(GATING_REASONS) == {
        REASON_ORPHAN,
        REASON_AMBIGUOUS,
        REASON_DISAGREEMENT,
        REASON_EMPTY_UNDERLYING,
    }
