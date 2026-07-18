"""Unit tests — the nullable REFERENCE-ONLY VIX trio on :class:`ResultRow`.

DEC-VIX-STAMP-WIRE-001 (Stage C): ``vix_mean`` / ``vix_min`` / ``vix_max``
land on :class:`~brutex.engine.results_store.ResultRow` as nullable Float64
columns with (a) the optional-column back-compat read law (an old store loads
``None`` — honest unknown, never a fabricated ``0.0``), (b) the DIRECTIONAL
three-branch :meth:`~brutex.engine.results_store.ResultsStore.append` upgrade
rule (backfill ``None → value`` OK; identical OK; two different known values →
error), and (c) the REFERENCE-ONLY scope law — the trio is NEVER rankable and
NEVER an input to the BLAKE3 run identity (CLAUDE.md §2 Rule 3).

Companions: ``tests/golden/test_results_store_backcompat_golden.py`` (the
committed HISTORICAL fixtures) and the run-identity invariance regression in
``tests/unit/test_engine_vix_stamper_wiring.py``.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import math
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.engine.results_store import (
    _RANKABLE_FIELDS,
    VIX_FIELDS,
    ResultRow,
    ResultsStore,
    ResultsStoreError,
    VixTrioConflictError,
)
from structlog.testing import capture_logs


def _rid(seed: int) -> str:
    """Return a valid 64-char lowercase-hex run-identity from *seed*."""
    return f"{seed:064x}"


def _row(**overrides: object) -> ResultRow:
    """Build a valid :class:`ResultRow`, overriding any field by keyword."""
    base: dict[str, object] = {
        "run_id": _rid(1),
        "instrument": "NIFTY",
        "tf": "5m",
        "direction": "bull",
        "mode": "A",
        "mask_bits": 0b101,
        "net_pnl_paisa": 1_000,
        "gross_profit_paisa": 1_500,
        "gross_loss_paisa": 500,
        "max_drawdown_paisa": 250,
        "expectancy_paisa": 25,
        "total_trades": 40,
        "winning_trades": 25,
        "losing_trades": 15,
        "sharpe_ratio": 1.5,
        "sortino_ratio": 2.0,
        "calmar_ratio": 1.25,
        "profit_factor": 1.8,
        "win_rate": 0.625,
        "max_drawdown_pct": 12.5,
        "avg_entry_price": Decimal("14020.50"),
        "avg_exit_price": Decimal("14100.25"),
    }
    base.update(overrides)
    return ResultRow(**base)  # type: ignore[arg-type]


_TRIO = {"vix_mean": 13.5, "vix_min": 12.25, "vix_max": 15.75}


# ──────────────────────────────────────────────────────────────────────────
# ResultRow — construction + validation
# ──────────────────────────────────────────────────────────────────────────


class TestVixFieldValidation:
    def test_trio_defaults_to_none(self) -> None:
        row = _row()
        assert (row.vix_mean, row.vix_min, row.vix_max) == (None, None, None)

    def test_full_trio_round_trips_at_full_precision(self) -> None:
        row = _row(vix_mean=13.123456789012345, vix_min=11.000000000000002, vix_max=15.9)
        assert row.vix_mean == 13.123456789012345  # never rounded (CLAUDE.md §4)
        assert row.vix_min == 11.000000000000002
        assert row.vix_max == 15.9

    @pytest.mark.parametrize("name", ["vix_mean", "vix_min", "vix_max"])
    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_non_finite_rejected_per_field(self, name: str, bad: float) -> None:
        with pytest.raises(ResultsStoreError, match=f"{name} must be a finite float or None"):
            _row(**{name: bad})

    def test_negative_zero_canonicalised(self) -> None:
        row = _row(vix_mean=-0.0, vix_min=-0.0, vix_max=-0.0)
        assert str(row.vix_mean) == "0.0"  # -0.0 → +0.0 (byte-determinism)
        assert str(row.vix_min) == "0.0"
        assert str(row.vix_max) == "0.0"

    @pytest.mark.parametrize(
        ("kwargs", "lo_name"),
        [
            ({"vix_min": 14.0, "vix_mean": 13.0, "vix_max": 15.0}, "vix_min"),
            ({"vix_min": 12.0, "vix_mean": 16.0, "vix_max": 15.0}, "vix_mean"),
            ({"vix_min": 16.0, "vix_max": 15.0}, "vix_min"),  # mean absent — direct pair
        ],
    )
    def test_order_law_violation_rejected(self, kwargs: dict[str, float], lo_name: str) -> None:
        with pytest.raises(ResultsStoreError, match=f"{lo_name} .* exceeds"):
            _row(**kwargs)

    def test_degenerate_equal_trio_allowed(self) -> None:
        row = _row(vix_mean=13.0, vix_min=13.0, vix_max=13.0)
        assert (row.vix_mean, row.vix_min, row.vix_max) == (13.0, 13.0, 13.0)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"vix_mean": 13.0},
            {"vix_min": 12.0},
            {"vix_max": 15.0},
            {"vix_min": 12.0, "vix_max": 15.0},
        ],
    )
    def test_part_null_trio_allowed(self, kwargs: dict[str, float]) -> None:
        row = _row(**kwargs)
        for name in VIX_FIELDS:
            assert getattr(row, name) == kwargs.get(name)


# ──────────────────────────────────────────────────────────────────────────
# Scope law — never rankable, never in the run identity
# ──────────────────────────────────────────────────────────────────────────


class TestVixReferenceOnlyScope:
    def test_vix_fields_are_not_rankable(self) -> None:
        assert _RANKABLE_FIELDS.isdisjoint(VIX_FIELDS)

    @pytest.mark.parametrize("name", ["vix_mean", "vix_min", "vix_max"])
    def test_top_k_refuses_a_vix_column(self, name: str, tmp_path: Path) -> None:
        store = ResultsStore(path=tmp_path / "s.parquet")
        store.append(_row(**_TRIO))
        with pytest.raises(ResultsStoreError, match="unknown ranking field"):
            store.top_k(1, by=name)


# ──────────────────────────────────────────────────────────────────────────
# append() — the directional three-branch upgrade rule
# ──────────────────────────────────────────────────────────────────────────


class TestDirectionalAppendUpgrade:
    def test_identical_stamped_reappend_is_a_noop(self, tmp_path: Path) -> None:
        store = ResultsStore(path=tmp_path / "s.parquet")
        stamped = _row(**_TRIO)
        first_added = store.append(stamped)
        assert first_added is True
        reappend_added = store.append(stamped)
        assert reappend_added is False  # branch 1: identical value OK
        assert store.get(stamped.run_id) == stamped

    def test_backfill_none_to_value_upgrades_and_logs(self, tmp_path: Path) -> None:
        store = ResultsStore(path=tmp_path / "s.parquet")
        unstamped_added = store.append(_row())
        assert unstamped_added is True  # unstamped first run
        with capture_logs() as logs:
            backfill_added = store.append(_row(**_TRIO))
        assert backfill_added is True  # branch 2: stamped redo backfills
        stored = store.get(_rid(1))
        assert stored is not None
        assert (stored.vix_mean, stored.vix_min, stored.vix_max) == (13.5, 12.25, 15.75)
        events = [e for e in logs if e["event"] == "results_store.vix_backfill"]
        assert len(events) == 1
        assert events[0]["run_id"] == _rid(1)
        assert events[0]["backfilled_fields"] == ("vix_mean", "vix_min", "vix_max")

    def test_partial_backfill_unions_per_field(self, tmp_path: Path) -> None:
        store = ResultsStore(path=tmp_path / "s.parquet")
        store.append(_row(vix_min=12.0))
        partial_added = store.append(_row(vix_mean=13.0, vix_max=15.0))
        assert partial_added is True
        stored = store.get(_rid(1))
        assert stored is not None
        assert (stored.vix_mean, stored.vix_min, stored.vix_max) == (13.0, 12.0, 15.0)

    def test_value_to_none_never_erases(self, tmp_path: Path) -> None:
        store = ResultsStore(path=tmp_path / "s.parquet")
        stamped = _row(**_TRIO)
        store.append(stamped)
        # An UNSTAMPED redo is less informed, not conflicting — silent no-op.
        unstamped_redo_added = store.append(_row())
        assert unstamped_redo_added is False
        assert store.get(stamped.run_id) == stamped

    def test_conflicting_known_values_raise_the_typed_vix_conflict(self, tmp_path: Path) -> None:
        store = ResultsStore(path=tmp_path / "s.parquet")
        store.append(_row(**_TRIO))
        clash = dict(_TRIO, vix_mean=14.0)  # branch 3: two different known values
        with pytest.raises(VixTrioConflictError, match="VIX trio conflict"):
            store.append(_row(**clash))

    def test_vix_conflict_is_a_results_store_error_subclass(self) -> None:
        # Every existing store-error handler still catches the typed conflict.
        assert issubclass(VixTrioConflictError, ResultsStoreError)

    def test_vix_conflict_message_names_the_cause_and_the_remedy(self, tmp_path: Path) -> None:
        """The raise diagnoses the VIX-lake movement, NEVER 'non-determinism'.

        Round-2 panel MAJOR (DEC-VIX-STAMP-WIRE-001): VIX is deliberately
        OUTSIDE the BLAKE3 run identity, so a redo whose trio disagrees after
        a legitimate VIX re-pull / 1day re-derivation must not be misdiagnosed
        as an upstream-non-determinism run-identity breach.
        """
        store = ResultsStore(path=tmp_path / "s.parquet")
        store.append(_row(**_TRIO))
        with pytest.raises(VixTrioConflictError) as exc:
            store.append(_row(**dict(_TRIO, vix_mean=14.0)))
        message = str(exc.value)
        assert "NOT upstream non-determinism" in message
        assert "India-VIX lake moved" in message
        assert "re-pull" in message and "1day" in message  # the two lake-move causes
        assert "tuple" in message and "remedy" in message  # skip contract + remediation
        assert "run-identity conflict" not in message  # never the misdiagnosis

    def test_vix_conflict_log_names_the_field_and_both_values(self, tmp_path: Path) -> None:
        store = ResultsStore(path=tmp_path / "s.parquet")
        store.append(_row(**_TRIO))
        with capture_logs() as logs, pytest.raises(VixTrioConflictError):
            store.append(_row(**dict(_TRIO, vix_max=99.0)))
        events = [e for e in logs if e["event"] == "results_store.vix_conflict"]
        assert len(events) == 1
        assert events[0]["conflicting_fields"] == ("vix_max",)
        assert events[0]["stored"]["vix_max"] == 15.75
        assert events[0]["incoming"]["vix_max"] == 99.0
        # The generic run-identity conflict event never fires for a VIX-only clash.
        assert not [e for e in logs if e["event"] == "results_store.conflict"]

    def test_non_vix_difference_still_raises_even_with_backfillable_vix(
        self, tmp_path: Path
    ) -> None:
        store = ResultsStore(path=tmp_path / "s.parquet")
        store.append(_row())
        with pytest.raises(ResultsStoreError, match="run-identity conflict") as exc:
            store.append(_row(sharpe_ratio=9.9, **_TRIO))  # metrics conflict wins
        # A genuine (non-VIX) conflict is the PLAIN error — grid drivers must
        # still ABORT on it, so it must never alias the tuple-skip subclass.
        assert not isinstance(exc.value, VixTrioConflictError)
        stored = store.get(_rid(1))
        assert stored is not None
        assert stored.vix_mean is None  # the backfill never applied

    def test_mixed_vix_and_non_vix_difference_is_the_plain_conflict(self, tmp_path: Path) -> None:
        # BOTH a metric AND a known VIX value differ: the genuine run-identity
        # conflict wins (and lists every differing field), never the VIX skip.
        store = ResultsStore(path=tmp_path / "s.parquet")
        store.append(_row(**_TRIO))
        with (
            capture_logs() as logs,
            pytest.raises(ResultsStoreError, match="run-identity conflict") as exc,
        ):
            store.append(_row(sharpe_ratio=9.9, **dict(_TRIO, vix_max=99.0)))
        assert not isinstance(exc.value, VixTrioConflictError)
        (event,) = [e for e in logs if e["event"] == "results_store.conflict"]
        assert set(event["differing_fields"]) == {"sharpe_ratio", "vix_max"}

    def test_mutually_inconsistent_backfill_raises_the_order_law(self, tmp_path: Path) -> None:
        # existing knows only vix_min=15; the redo knows only vix_mean=13 — the
        # union would violate min <= mean, and the merged row's OWN validation
        # refuses it loudly (never a silently-stored inconsistent trio).
        store = ResultsStore(path=tmp_path / "s.parquet")
        store.append(_row(vix_min=15.0))
        with pytest.raises(ResultsStoreError, match="vix_min .* exceeds vix_mean"):
            store.append(_row(vix_mean=13.0))

    def test_extend_counts_backfill_upgrades(self, tmp_path: Path) -> None:
        store = ResultsStore(path=tmp_path / "s.parquet")
        store.append(_row())
        store.append(_row(run_id=_rid(2)))
        added = store.extend(
            [
                _row(**_TRIO),  # upgrade → counted
                _row(run_id=_rid(2)),  # identical no-op → not counted
                _row(run_id=_rid(3)),  # new → counted
            ]
        )
        assert added == 2


# ──────────────────────────────────────────────────────────────────────────
# flush → load — nullable round-trip + optional-column back-compat
# ──────────────────────────────────────────────────────────────────────────


class TestVixFlushLoad:
    def test_trio_round_trips_value_partnull_and_none(self, tmp_path: Path) -> None:
        pytest.importorskip("polars")
        path = tmp_path / "s.parquet"
        store = ResultsStore(path=path)
        store.append(_row(run_id=_rid(1), **_TRIO))
        store.append(_row(run_id=_rid(2), vix_min=12.0))
        store.append(_row(run_id=_rid(3)))
        store.flush()
        loaded = ResultsStore.load(path)
        assert loaded.rows() == store.rows()

    def test_vix_columns_are_appended_last_on_disk(self, tmp_path: Path) -> None:
        pl = pytest.importorskip("polars")
        path = tmp_path / "s.parquet"
        store = ResultsStore(path=path)
        store.append(_row())
        store.flush()
        schema = pl.read_parquet_schema(path)
        # Ruling-5 generation order: the VIX trio, then the growth trio
        # STRICTLY AFTER it (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 /
        # DEC-METRICS-KELLY-GROWTH-001 — each optional-column generation is
        # appended after the previous one; the pre-change pin ended at the
        # VIX trio and was consciously extended, not weakened).
        assert [*schema][-6:] == [
            "vix_mean",
            "vix_min",
            "vix_max",
            "loss_rate",
            "kelly_fraction",
            "log_growth_at_half_kelly",
        ]
        assert all(schema[name] == pl.Float64 for name in VIX_FIELDS)

    def test_store_without_the_columns_loads_none_trio(self, tmp_path: Path) -> None:
        """The optional-column law on a synthetic legacy file (unit-level twin
        of the committed golden fixtures): drop the trio + deflated_sharpe
        columns from a real flush and load — every absent column reads None."""
        pl = pytest.importorskip("polars")
        modern = tmp_path / "modern.parquet"
        store = ResultsStore(path=modern)
        store.append(_row(deflated_sharpe=0.5, **_TRIO))
        store.flush()
        legacy = tmp_path / "legacy.parquet"
        pl.read_parquet(modern).drop("deflated_sharpe", *VIX_FIELDS).write_parquet(legacy)
        loaded = ResultsStore.load(legacy)
        row = loaded.get(_rid(1))
        assert row is not None
        assert row.deflated_sharpe is None  # honest unknown — never fabricated
        assert (row.vix_mean, row.vix_min, row.vix_max) == (None, None, None)
        # Every non-optional field survived unchanged.
        assert row == _row(deflated_sharpe=None)

    def test_load_time_vix_only_duplicate_re_raises_distinct_not_schema_error(
        self, tmp_path: Path
    ) -> None:
        """The G14-wrap carve-out (DEC-VIX-STAMP-WIRE-001 ∩
        DEC-SWEEP-RESULTS-DEPTH-001 reconciliation): an in-file duplicate
        run_id differing ONLY in a non-None VIX value re-raises the DISTINCT
        :class:`VixTrioConflictError` from :meth:`ResultsStore.load` — never
        wrapped into ``ResultsStoreSchemaError`` — so the stage-3 pass keeps
        its typed cell-skip contract instead of condemning the file as
        verdict-grade damage."""
        pl = pytest.importorskip("polars")
        from brutex.engine.results_store import ResultsStoreSchemaError

        path = tmp_path / "s.parquet"
        store = ResultsStore(path=path)
        store.append(_row(**_TRIO))
        store.flush()
        df = pl.read_parquet(path)
        # Stay inside the trio's [min, max] order law so the clone row itself
        # CONSTRUCTS cleanly — the only difference is the conflicting mean.
        clone = df.head(1).with_columns(pl.lit(14.5, dtype=pl.Float64).alias("vix_mean"))
        tampered = tmp_path / "tampered.parquet"
        pl.concat([df, clone]).write_parquet(tampered)
        with pytest.raises(VixTrioConflictError) as excinfo:
            ResultsStore.load(tampered)
        assert not isinstance(excinfo.value, ResultsStoreSchemaError)

    def test_stamped_and_unstamped_rows_differ_only_in_the_trio(self) -> None:
        plain = _row()
        stamped = _row(**_TRIO)
        assert plain.run_id == stamped.run_id  # identity NEVER derives from VIX
        assert plain != stamped
        from dataclasses import replace

        assert replace(stamped, vix_mean=None, vix_min=None, vix_max=None) == plain
