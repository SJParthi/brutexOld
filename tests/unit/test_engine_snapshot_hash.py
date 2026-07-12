"""Unit tests — full-content ``data_snapshot_hash`` (DEC-SNAPSHOT-HASH-FULL-001).

THE fails-before/passes-after regression + the per-field sensitivity matrix for
the v1→v2 upgrade: the retired v1 derivation sampled only ``count +
first/last timestamp+close`` per slice, so an INTERIOR-bar re-pull edit (or any
first/last O/H/L/volume/OI change) preserved the hash and the whole identity
chain (``mask_run_id`` / ``tuple_cache_key`` / ``_tuple_digest`` /
``grid_identity``) aliased two different lakes — Bloom/LMDB dedup + the tuple
cache then served STALE results as fresh (Golden Rules #4/#7). v2 streams ALL
SEVEN CandleBar fields per bar under the ``bxsnap2|`` domain tag.

Deliberately in its own module WITHOUT the ``pytest.importorskip("cma")`` that
gates ``test_engine_stage3_wire.py`` — the hash needs no optimizer, so these
teeth bite even where the ``optimize`` extra is absent.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar
from brutex.engine.snapshot_hash import data_snapshot_hash
from brutex.engine.stage3_wire import (
    _data_snapshot_hash_v1,
    train_slice_bars,
)

_IST = timezone(timedelta(hours=5, minutes=30))

_SCOPE: dict[str, str] = {
    "exchange": "NSE",
    "segment": "INDEX",
    "start": "2026-01-01",
    "end": "2026-12-31",
}


def _bar(
    minute: int,
    *,
    day: int = 0,
    o: str = "100.00",
    h: str = "102.00",
    lo: str = "99.00",
    c: str = "101.00",
    volume: int = 1000,
    oi: int | None = None,
) -> CandleBar:
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=volume,
        open_interest=oi,
    )


def _bars(n: int = 5) -> list[CandleBar]:
    return [_bar(i) for i in range(n)]


class TestInteriorEditRegression:
    """THE bug: v1 collides on an interior edit; v2 must not."""

    def test_interior_close_edit_collides_under_v1_but_not_v2(self) -> None:
        base = _bars(5)
        # Same count, same first/last timestamp+close — ONLY bar[2]'s close
        # moves (the interior re-pull edit v1 was structurally blind to).
        edited = list(base)
        edited[2] = dataclasses.replace(base[2], close=Decimal("100.50"))
        a = {("NIFTY", "1minute"): base}
        b = {("NIFTY", "1minute"): edited}
        # Fails-before proof: the retained v1 derivation ALIASES the two lakes.
        assert _data_snapshot_hash_v1(**_SCOPE, slices=a) == _data_snapshot_hash_v1(
            **_SCOPE, slices=b
        )
        # Passes-after: the full-content v2 distinguishes them.
        assert data_snapshot_hash(**_SCOPE, slices=a) != data_snapshot_hash(**_SCOPE, slices=b)

    def test_train_window_interior_edit_changes_the_train_hash(self) -> None:
        # The Stage-3 TRAIN hash is THIS derivation over the lockbox-excluded
        # train slices (ADR-0007 A1) — an interior edit INSIDE the train window
        # must move it (→ a different refined-cell identity path).
        # Span the lockbox default (182d) plus a real train remainder so the
        # train slice is non-empty; the edit lands INSIDE the train window.
        base = [_bar(0, day=day) for day in range(200)]
        edited = list(base)
        edited[3] = dataclasses.replace(base[3], close=Decimal("100.50"))
        train_a = train_slice_bars(base)
        train_b = train_slice_bars(edited)
        assert train_a[0].timestamp == train_b[0].timestamp  # same train window...
        assert train_a[-1].timestamp == train_b[-1].timestamp
        assert data_snapshot_hash(
            **_SCOPE, slices={("NIFTY", "1minute"): list(train_a)}
        ) != data_snapshot_hash(**_SCOPE, slices={("NIFTY", "1minute"): list(train_b)})


class TestPerFieldSensitivity:
    """Every one of the 7 CandleBar fields flips the hash, at every position."""

    @pytest.mark.parametrize("position", [0, 2, 4])
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("timestamp", None),  # handled below (shift by one second)
            ("open", Decimal("100.05")),
            ("high", Decimal("102.05")),
            ("low", Decimal("98.95")),
            ("close", Decimal("100.95")),
            ("volume", 1001),
            ("open_interest", 7),
        ],
    )
    def test_single_field_mutation_flips_the_hash(
        self, position: int, field: str, value: object
    ) -> None:
        base = _bars(5)
        if field == "timestamp":
            value = base[position].timestamp + timedelta(seconds=1)
        edited = list(base)
        edited[position] = dataclasses.replace(base[position], **{field: value})
        assert data_snapshot_hash(
            **_SCOPE, slices={("NIFTY", "1minute"): base}
        ) != data_snapshot_hash(**_SCOPE, slices={("NIFTY", "1minute"): edited})

    def test_none_vs_zero_open_interest_are_distinct(self) -> None:
        base = _bars(3)
        edited = [dataclasses.replace(b, open_interest=0) for b in base]
        assert data_snapshot_hash(
            **_SCOPE, slices={("NIFTY", "1minute"): base}
        ) != data_snapshot_hash(**_SCOPE, slices={("NIFTY", "1minute"): edited})

    def test_bar_reorder_at_equal_multiset_flips_the_hash(self) -> None:
        base = _bars(4)
        reordered = [base[0], base[2], base[1], base[3]]
        assert data_snapshot_hash(
            **_SCOPE, slices={("NIFTY", "1minute"): base}
        ) != data_snapshot_hash(**_SCOPE, slices={("NIFTY", "1minute"): reordered})


class TestPreimageStructure:
    def test_slices_insertion_order_is_irrelevant(self) -> None:
        # sorted(slices.items()) pin — dict insertion order must never matter.
        nifty, bank = _bars(3), _bars(2)
        a = {("NIFTY", "1minute"): nifty, ("BANKNIFTY", "1minute"): bank}
        b = {("BANKNIFTY", "1minute"): bank, ("NIFTY", "1minute"): nifty}
        assert data_snapshot_hash(**_SCOPE, slices=a) == data_snapshot_hash(**_SCOPE, slices=b)

    def test_empty_slice_participates_and_scope_is_folded(self) -> None:
        a = data_snapshot_hash(**_SCOPE, slices={("NIFTY", "1minute"): []})
        b = data_snapshot_hash(**_SCOPE, slices={("BANKNIFTY", "1minute"): []})
        c = data_snapshot_hash(
            exchange="BSE",
            segment=_SCOPE["segment"],
            start=_SCOPE["start"],
            end=_SCOPE["end"],
            slices={("NIFTY", "1minute"): []},
        )
        assert len({a, b, c}) == 3

    def test_v2_never_collides_with_v1_even_on_the_degenerate_empty_grid(self) -> None:
        # The bxsnap2| domain tag: the all-empty-slices preimages differ
        # structurally, so the two derivations can never alias an identity.
        empty: dict[tuple[str, str], list[CandleBar]] = {}
        assert data_snapshot_hash(**_SCOPE, slices=empty) != _data_snapshot_hash_v1(
            **_SCOPE, slices=empty
        )

    def test_v1_diagnostic_is_deterministic_64_hex(self) -> None:
        slices = {("NIFTY", "1minute"): _bars(3)}
        a = _data_snapshot_hash_v1(**_SCOPE, slices=slices)
        assert a == _data_snapshot_hash_v1(**_SCOPE, slices=slices)
        assert len(a) == 64
        assert a == a.lower()
        int(a, 16)

    def test_v1_diagnostic_keeps_the_empty_slice_zero_entry(self) -> None:
        # The retained v1 body must stay byte-faithful to the retired
        # derivation (a legacy tree's recorded hash includes empty slices as
        # explicit zero-count entries) — else the recount diagnostic misfires.
        a = _data_snapshot_hash_v1(**_SCOPE, slices={("NIFTY", "1minute"): []})
        b = _data_snapshot_hash_v1(**_SCOPE, slices={("BANKNIFTY", "1minute"): []})
        assert a != b
