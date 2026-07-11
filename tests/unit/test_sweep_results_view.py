"""Unit tests for the sweep-leaderboard reader (DEC-DASHBOARD-RESULTS-PAGE-001).

Exercises :mod:`brutex.dashboard.sweep_results_view` directly (pure, no FastAPI):
the happy-path discovery + ranking, the optional filters, the honest empty state,
the rank-by fall-back + clamp, the cross-store run_id dedup, the malformed-file
skip (a real corrupt Parquet), and the money / full-precision-metric projection.
"""

# The drift-pin tests read module-private constants deliberately.
# pyright: reportPrivateUsage=none
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars", reason="the reader loads per-tuple Parquet stores via ResultsStore")

from brutex.dashboard.sweep_results_view import (
    DEFAULT_RANK_BY,
    DEFAULT_TOP_N,
    MAX_TOP_N,
    RANKABLE_COLUMNS,
    SweepLeaderboard,
    build_sweep_leaderboard,
    load_sweep_scan,
)
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.money import format_inr


def _run_id(seed: int) -> str:
    """A valid 64-char lowercase-hex run-identity from an int seed (deterministic)."""
    return f"{seed:064x}"


def _row(
    *,
    seed: int,
    instrument: str = "NIFTY",
    tf: str = "5m",
    direction: str = "bull",
    net_pnl_paisa: int = 100_00,
    sharpe: float = 1.0,
    mask_bits: int = 0,
    total_trades: int = 10,
) -> ResultRow:
    """A minimal valid :class:`ResultRow` with the fields the leaderboard reads."""
    return ResultRow(
        run_id=_run_id(seed),
        instrument=instrument,
        tf=tf,
        direction=direction,
        mode="A",
        mask_bits=mask_bits,
        net_pnl_paisa=net_pnl_paisa,
        gross_profit_paisa=200_00,
        gross_loss_paisa=100_00,
        max_drawdown_paisa=50_00,
        expectancy_paisa=10_00,
        total_trades=total_trades,
        winning_trades=6,
        losing_trades=4,
        sharpe_ratio=sharpe,
        sortino_ratio=1.2,
        calmar_ratio=0.9,
        profit_factor=2.0,
        win_rate=0.6,
        max_drawdown_pct=0.05,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
    )


def _write_store(
    lake_root: Path, *, instrument: str, tf: str, direction: str, rows: list[ResultRow]
) -> Path:
    """Write a per-tuple store at ``<lake>/sweeps/<instrument>/<tf>/<direction>_k1_<digest>.parquet``."""
    dest = lake_root / "sweeps" / instrument / tf / f"{direction}_k1_{'a' * 16}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    store = ResultsStore(path=dest)
    store.extend(rows)
    store.flush()
    return dest


def test_empty_when_no_sweeps_dir(tmp_path: Path) -> None:
    """No ``<lake>/sweeps`` directory → honest empty leaderboard (available False)."""
    board = build_sweep_leaderboard(tmp_path)
    assert isinstance(board, SweepLeaderboard)
    assert board.available is False
    assert board.rows == ()
    assert board.total_rows == 0
    assert board.store_count == 0
    assert board.skipped == 0
    assert board.instruments == ()
    assert board.timeframes == ()
    assert board.directions == ()


def test_ranks_descending_by_default_metric(tmp_path: Path) -> None:
    """Rows rank by Sharpe descending — best first — with sequential ranks."""
    _write_store(
        tmp_path,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        rows=[
            _row(seed=1, sharpe=0.5),
            _row(seed=2, sharpe=2.5),
            _row(seed=3, sharpe=1.5),
        ],
    )
    board = build_sweep_leaderboard(tmp_path)
    assert board.available is True
    assert board.store_count == 1
    assert board.rank_by == DEFAULT_RANK_BY
    sharpes = [r["sharpe_ratio"] for r in board.rows]
    assert sharpes == [2.5, 1.5, 0.5]
    assert [r["rank"] for r in board.rows] == [1, 2, 3]
    assert board.total_rows == 3


def test_filters_instrument_tf_direction_case_insensitive(tmp_path: Path) -> None:
    """The optional filters select exactly the matching rows (case-insensitive)."""
    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[_row(seed=1)])
    _write_store(
        tmp_path,
        instrument="BANKNIFTY",
        tf="15m",
        direction="bear",
        rows=[_row(seed=2, instrument="BANKNIFTY", tf="15m", direction="bear")],
    )
    board = build_sweep_leaderboard(tmp_path, instrument="nifty", tf="5M", direction="BULL")
    assert {r["instrument"] for r in board.rows} == {"NIFTY"}
    assert board.total_rows == 1
    # The dropdown option sets reflect EVERY discovered row, not just the filtered set.
    assert board.instruments == ("BANKNIFTY", "NIFTY")
    assert board.timeframes == ("15m", "5m")
    assert board.directions == ("bear", "bull")


def test_filter_tf_mismatch_excludes_row(tmp_path: Path) -> None:
    """A row matching the instrument but NOT the tf filter is excluded (tf branch)."""
    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[_row(seed=1)])
    board = build_sweep_leaderboard(tmp_path, instrument="NIFTY", tf="15m")
    assert board.rows == ()
    assert board.total_rows == 0


def test_filter_direction_mismatch_excludes_row(tmp_path: Path) -> None:
    """A row matching instrument + tf but NOT the direction filter is excluded."""
    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[_row(seed=1)])
    board = build_sweep_leaderboard(tmp_path, instrument="NIFTY", tf="5m", direction="bear")
    assert board.rows == ()
    assert board.total_rows == 0


def test_money_and_full_precision_projection(tmp_path: Path) -> None:
    """Net P&L is ``₹`` formatted; Sharpe / win-rate are passed through unrounded."""
    _write_store(
        tmp_path,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        rows=[_row(seed=1, net_pnl_paisa=123_456, sharpe=1.23456789)],
    )
    board = build_sweep_leaderboard(tmp_path)
    row = board.rows[0]
    assert row["net_pnl"] == format_inr(123_456)
    assert row["net_pnl_rupees"] == pytest.approx(1234.56)
    # Full precision preserved (NEVER rounded — ranking integrity).
    assert row["sharpe_ratio"] == 1.23456789
    assert row["win_rate"] == 0.6


def test_conditions_decoded_from_mask(tmp_path: Path) -> None:
    """A mask with set bits decodes to its condition names; mask 0 → empty string."""
    _write_store(
        tmp_path,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        rows=[_row(seed=1, mask_bits=0), _row(seed=2, mask_bits=0b11)],
    )
    board = build_sweep_leaderboard(tmp_path)
    by_mask = {r["mask_bits"]: r["conditions"] for r in board.rows}
    assert by_mask[0] == ""
    assert by_mask[0b11] != ""  # two condition names, joined


def test_top_n_clamped(tmp_path: Path) -> None:
    """``top_n`` is clamped into [1, MAX_TOP_N]."""
    _write_store(
        tmp_path,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        rows=[_row(seed=i, sharpe=float(i)) for i in range(1, 6)],
    )
    assert len(build_sweep_leaderboard(tmp_path, top_n=2).rows) == 2
    assert len(build_sweep_leaderboard(tmp_path, top_n=0).rows) == 1  # clamped up to 1
    big = build_sweep_leaderboard(tmp_path, top_n=MAX_TOP_N + 100)
    assert len(big.rows) == 5  # capped depth ≥ available rows → all 5


def test_unknown_rank_by_falls_back_to_default(tmp_path: Path) -> None:
    """An unknown ``rank_by`` degrades to the default metric, never an error."""
    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[_row(seed=1)])
    board = build_sweep_leaderboard(tmp_path, rank_by="not_a_column")
    assert board.rank_by == DEFAULT_RANK_BY


def test_rank_by_net_pnl(tmp_path: Path) -> None:
    """A money ranking column ranks largest-P&L first."""
    _write_store(
        tmp_path,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        rows=[_row(seed=1, net_pnl_paisa=100), _row(seed=2, net_pnl_paisa=900)],
    )
    board = build_sweep_leaderboard(tmp_path, rank_by="net_pnl_paisa")
    assert board.rank_by == "net_pnl_paisa"
    assert board.rows[0]["net_pnl_rupees"] > board.rows[1]["net_pnl_rupees"]


def test_rank_by_int_money_preserves_precision_beyond_float53(tmp_path: Path) -> None:
    """A money column ranks on the RAW int64 — two values that collapse to the SAME
    float when cast (``2**53`` vs ``2**53 + 1``) still rank in the correct order
    (MINOR-1, the same total order as ``ResultsStore.top_k`` — Golden Rule #4).
    """
    big = 2**53  # the largest int every float can represent exactly
    bigger = big + 1  # float(bigger) == float(big) — a float cast would TIE them
    assert float(bigger) == float(big)  # the precision trap the fix avoids
    _write_store(
        tmp_path,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        rows=[_row(seed=1, net_pnl_paisa=big), _row(seed=2, net_pnl_paisa=bigger)],
    )
    board = build_sweep_leaderboard(tmp_path, rank_by="net_pnl_paisa")
    # The +1 combo (run 2) must rank ABOVE the smaller one — a float-coerced key would
    # tie them and fall back to the run_id tie-break (run 1 first), the bug this guards.
    assert board.rows[0]["run_id"] == _run_id(2)
    assert board.rows[1]["run_id"] == _run_id(1)


def test_dedup_same_run_id_across_stores(tmp_path: Path) -> None:
    """The same combo (run_id) in two stores is counted once."""
    shared = _row(seed=7)
    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[shared])
    _write_store(tmp_path, instrument="NIFTY", tf="15m", direction="bull", rows=[shared])
    board = build_sweep_leaderboard(tmp_path)
    assert board.store_count == 2
    assert board.total_rows == 1  # deduped by run_id


def test_corrupt_store_is_skipped_not_crashed(tmp_path: Path) -> None:
    """A corrupt per-tuple Parquet SKIPS that store (counted), never 500s the read."""
    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[_row(seed=1)])
    bad = tmp_path / "sweeps" / "BANKNIFTY" / "15m" / "bull_k1_deadbeefdeadbeef.parquet"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not a parquet file at all")
    board = build_sweep_leaderboard(tmp_path)
    assert board.store_count == 1  # the good one
    assert board.skipped == 1  # the corrupt one
    assert board.available is True
    assert {r["instrument"] for r in board.rows} == {"NIFTY"}


def test_raw_polars_error_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw polars error escaping the wrap is still SKIPPED (the belt-and-suspenders set)."""
    import polars as pl
    from brutex.dashboard import sweep_results_view as mod

    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[_row(seed=1)])

    def _raise(_path: Path) -> ResultsStore:
        raise pl.exceptions.ComputeError("synthetic raw polars fault")

    monkeypatch.setattr(mod.ResultsStore, "load", classmethod(lambda cls, p: _raise(p)))
    board = build_sweep_leaderboard(tmp_path)
    assert board.store_count == 0
    assert board.skipped == 1
    assert board.available is False


def test_to_dict_shape(tmp_path: Path) -> None:
    """``to_dict`` carries every payload key the route + page expect."""
    _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[_row(seed=1)])
    payload = build_sweep_leaderboard(tmp_path).to_dict()
    assert set(payload) == {
        "available",
        "total_rows",
        "store_count",
        "skipped",
        "rank_by",
        "instruments",
        "timeframes",
        "directions",
        "snapshot_hashes",
        "mixed_snapshot_eras",
        "run_id_conflicts",
        # The four ALWAYS-present additive gate-scoping keys
        # (DEC-SWEEP-SERVING-GATE-PROVENANCE-001).
        "gate_coordinate",
        "gate_coordinates",
        "mixed_gate_coordinates",
        "gate_reasons",
        "rows",
    }
    assert isinstance(payload["rows"], list)
    assert isinstance(payload["instruments"], list)
    assert isinstance(payload["snapshot_hashes"], list)
    assert payload["mixed_snapshot_eras"] is False
    assert payload["run_id_conflicts"] == 0


def test_module_constants() -> None:
    """The public constants are sane + the rank-by allow-list is non-empty."""
    assert DEFAULT_TOP_N == 50
    assert MAX_TOP_N == 500
    assert DEFAULT_RANK_BY in RANKABLE_COLUMNS
    assert "net_pnl_paisa" in RANKABLE_COLUMNS


# ── DEC-RESULTS-SURFACE-HONESTY-001 — snapshot-era markers + run_id conflicts ──


def _write_sweep_start_audit(
    lake_root: Path, payloads: list[dict[str, object]], *, noise_action: bool = False
) -> Path:
    """Write a REAL hash-chained audit log with one ``SWEEP_START`` per payload.

    Uses the production :class:`brutex.orchestrator.audit.AuditWriter` at the
    SAME ``<sweeps>/audit/sweep-all.jsonl`` path every ``sweep-all`` entry point
    writes, so the era scan is exercised against genuine chain bytes.
    """
    from brutex.orchestrator.audit import AuditWriter

    audit_path = lake_root / "sweeps" / "audit" / "sweep-all.jsonl"
    with AuditWriter.open(audit_path, resume=audit_path.exists()) as writer:
        if noise_action:
            # A non-SWEEP_START entry the era scan must skip.
            writer.append(action="SWEEP_COMPLETE", subject="sweep:noise", payload={"ok": True})
        for payload in payloads:
            writer.append(action="SWEEP_START", subject="sweep:test", payload=payload)
    return audit_path


def _start_payload(
    *,
    instrument: str = "NIFTY",
    tf: str = "5m",
    direction: str = "bull",
    mode: str = "A",
    k: int = 1,
    snapshot: str,
) -> dict[str, object]:
    """A ``SWEEP_START`` payload shaped like ``sweep_run``'s (context keys included)."""
    return {
        "instrument": instrument,
        "tf": tf,
        "direction": direction,
        "mode": mode,
        "k": k,
        "data_snapshot_hash": snapshot,
        "indicator_version": "test-iv-1",
        "code_commit_sha": "c" * 40,
    }


class TestSnapshotEraHonesty:
    """Mixed-era co-ranking is MARKED, never hidden (and never a row exclusion)."""

    def test_mixed_eras_across_two_real_stores(self, tmp_path: Path) -> None:
        """Two stores swept under DIFFERENT data eras → loud payload markers.

        Vacuity guard: BOTH eras must have persisted ≥1 ranked row — asserting
        ``mixed_snapshot_eras`` alone could pass on a tree where one era
        contributed nothing.
        """
        _write_store(
            tmp_path,
            instrument="NIFTY",
            tf="5m",
            direction="bull",
            rows=[_row(seed=1, mask_bits=1)],
        )
        _write_store(
            tmp_path,
            instrument="BANKNIFTY",
            tf="15m",
            direction="bear",
            rows=[_row(seed=2, instrument="BANKNIFTY", tf="15m", direction="bear", mask_bits=1)],
        )
        era_a, era_b = "a" * 64, "b" * 64
        _write_sweep_start_audit(
            tmp_path,
            [
                _start_payload(snapshot=era_a),
                _start_payload(instrument="BANKNIFTY", tf="15m", direction="bear", snapshot=era_b),
            ],
            noise_action=True,
        )
        board = build_sweep_leaderboard(tmp_path)
        assert board.mixed_snapshot_eras is True
        assert board.snapshot_hashes == (era_a, era_b)
        # NO row exclusion — the honest-marker approach (exclusion without
        # PR-3's census could hide legitimate multi-era research).
        assert board.total_rows == 2
        eras_seen = {r["snapshot_era"] for r in board.rows}
        assert eras_seen == {era_a, era_b}  # both eras persisted ≥1 ranked row
        payload = board.to_dict()
        assert payload["mixed_snapshot_eras"] is True
        assert payload["snapshot_hashes"] == [era_a, era_b]

    def test_single_era_reads_false_and_stamps_every_row(self, tmp_path: Path) -> None:
        """One recorded era → ``mixed_snapshot_eras`` False, rows stamped with it."""
        _write_store(
            tmp_path,
            instrument="NIFTY",
            tf="5m",
            direction="bull",
            rows=[_row(seed=1, mask_bits=1)],
        )
        era = "a" * 64
        _write_sweep_start_audit(tmp_path, [_start_payload(snapshot=era)])
        board = build_sweep_leaderboard(tmp_path)
        assert board.mixed_snapshot_eras is False
        assert board.snapshot_hashes == (era,)
        assert [r["snapshot_era"] for r in board.rows] == [era]

    def test_single_era_payload_compatible_otherwise(self, tmp_path: Path) -> None:
        """Apart from the three new keys + per-row marker, the payload is unchanged."""
        rows = [_row(seed=1, mask_bits=1), _row(seed=2, sharpe=2.0, mask_bits=1)]
        _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=rows)
        era = "a" * 64
        _write_sweep_start_audit(tmp_path, [_start_payload(snapshot=era)])
        payload = build_sweep_leaderboard(tmp_path).to_dict()
        new_top_level = {
            "snapshot_hashes",
            "mixed_snapshot_eras",
            "run_id_conflicts",
            # The additive gate-scoping keys (DEC-SWEEP-SERVING-GATE-PROVENANCE-001).
            "gate_coordinate",
            "gate_coordinates",
            "mixed_gate_coordinates",
            "gate_reasons",
        }
        legacy = {k: v for k, v in payload.items() if k not in new_top_level}
        for row in legacy["rows"]:
            row.pop("snapshot_era")
            row.pop("gate_provenance")
            row.pop("gate_coordinate")
            row.pop("gate_reasons")
        assert legacy == {
            "available": True,
            "total_rows": 2,
            "store_count": 1,
            "skipped": 0,
            "rank_by": DEFAULT_RANK_BY,
            "instruments": ["NIFTY"],
            "timeframes": ["5m"],
            "directions": ["bull"],
            "rows": legacy["rows"],  # shape asserted by the pre-existing tests
        }

    def test_no_audit_chain_marks_rows_unknown(self, tmp_path: Path) -> None:
        """An audit-less tree → empty hashes + honest ``unknown`` rows, never a guess."""
        from brutex.dashboard.sweep_results_view import SNAPSHOT_ERA_UNKNOWN

        _write_store(
            tmp_path,
            instrument="NIFTY",
            tf="5m",
            direction="bull",
            rows=[_row(seed=1, mask_bits=1)],
        )
        board = build_sweep_leaderboard(tmp_path)
        assert board.snapshot_hashes == ()
        assert board.mixed_snapshot_eras is False
        assert [r["snapshot_era"] for r in board.rows] == [SNAPSHOT_ERA_UNKNOWN]

    def test_reswept_coordinates_read_ambiguous(self, tmp_path: Path) -> None:
        """The SAME tuple coordinates swept under two eras → per-row ``ambiguous``.

        Per-FILE recorded hashes do not exist (the filename digest is one-way
        BLAKE3), so the row's own era honestly cannot be pinned — that is
        PR-3's census, not this marker.
        """
        from brutex.dashboard.sweep_results_view import SNAPSHOT_ERA_AMBIGUOUS

        _write_store(
            tmp_path,
            instrument="NIFTY",
            tf="5m",
            direction="bull",
            rows=[_row(seed=1, mask_bits=1)],
        )
        era_a, era_b = "a" * 64, "b" * 64
        _write_sweep_start_audit(
            tmp_path, [_start_payload(snapshot=era_a), _start_payload(snapshot=era_b)]
        )
        board = build_sweep_leaderboard(tmp_path)
        assert board.mixed_snapshot_eras is True
        assert board.snapshot_hashes == (era_a, era_b)
        assert [r["snapshot_era"] for r in board.rows] == [SNAPSHOT_ERA_AMBIGUOUS]

    def test_corrupt_audit_chain_degrades_to_unknown(self, tmp_path: Path) -> None:
        """A corrupt chain → logged era-scan failure + ``unknown`` rows, never a 500."""
        from brutex.dashboard.sweep_results_view import SNAPSHOT_ERA_UNKNOWN
        from structlog.testing import capture_logs

        _write_store(
            tmp_path,
            instrument="NIFTY",
            tf="5m",
            direction="bull",
            rows=[_row(seed=1, mask_bits=1)],
        )
        audit_path = tmp_path / "sweeps" / "audit" / "sweep-all.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text("this is not a committed audit line\n")
        with capture_logs() as logs:
            board = build_sweep_leaderboard(tmp_path)
        assert board.snapshot_hashes == ()
        assert board.mixed_snapshot_eras is False
        assert [r["snapshot_era"] for r in board.rows] == [SNAPSHOT_ERA_UNKNOWN]
        assert any(log["event"] == "sweep_results.era_scan_failed" for log in logs)

    def test_malformed_sweep_start_payload_is_skipped(self, tmp_path: Path) -> None:
        """A ``SWEEP_START`` missing the needed keys contributes no signal (no crash)."""
        from brutex.dashboard.sweep_results_view import SNAPSHOT_ERA_UNKNOWN

        _write_store(
            tmp_path,
            instrument="NIFTY",
            tf="5m",
            direction="bull",
            rows=[_row(seed=1, mask_bits=1)],
        )
        broken = _start_payload(snapshot="a" * 64)
        del broken["k"]
        _write_sweep_start_audit(tmp_path, [broken])
        board = build_sweep_leaderboard(tmp_path)
        assert board.snapshot_hashes == ()
        assert [r["snapshot_era"] for r in board.rows] == [SNAPSHOT_ERA_UNKNOWN]

    def test_audit_relpath_pinned_to_the_engine_convention(self) -> None:
        """The not-imported audit relpath copy cannot drift from the engine reader's."""
        from brutex.dashboard import sweep_results_view as view_mod
        from brutex.engine import results_report as report_mod

        assert view_mod._AUDIT_RELPATH == report_mod._AUDIT_RELPATH


class TestRunIdConflicts:
    """Duplicate run_ids with DIFFERENT metrics are counted + logged, never silent."""

    def test_divergent_duplicate_counts_and_warns_first_wins(self, tmp_path: Path) -> None:
        """The metric-divergent duplicate: deterministic first-wins + count + WARNING."""
        from structlog.testing import capture_logs

        first_wins = _row(seed=7, sharpe=1.0)
        divergent = _row(seed=7, sharpe=9.9)
        # Sorted store-path order: "15m" < "5m" lexicographically, so the 15m
        # store loads FIRST and its row deterministically wins.
        _write_store(tmp_path, instrument="NIFTY", tf="15m", direction="bull", rows=[first_wins])
        _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[divergent])
        with capture_logs() as logs:
            board = build_sweep_leaderboard(tmp_path)
        assert board.run_id_conflicts == 1
        assert board.total_rows == 1
        assert [r["sharpe_ratio"] for r in board.rows] == [1.0]  # first-wins, not 9.9
        conflict_logs = [log for log in logs if log["event"] == "sweep_results.run_id_conflict"]
        assert len(conflict_logs) == 1
        assert conflict_logs[0]["run_id"] == _run_id(7)
        assert board.to_dict()["run_id_conflicts"] == 1

    def test_identical_duplicate_is_not_a_conflict(self, tmp_path: Path) -> None:
        """A byte-identical duplicate stays the silent, conflict-free dedup."""
        from structlog.testing import capture_logs

        shared = _row(seed=7)
        _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[shared])
        _write_store(tmp_path, instrument="NIFTY", tf="15m", direction="bull", rows=[shared])
        with capture_logs() as logs:
            board = build_sweep_leaderboard(tmp_path)
        assert board.run_id_conflicts == 0
        assert board.total_rows == 1
        assert not [log for log in logs if log["event"] == "sweep_results.run_id_conflict"]


class TestVixOnlyDivergence:
    """A VIX-only cross-store difference is NOT the run_id_conflict family.

    Fails-before: the REFERENCE-ONLY trio sits deliberately OUTSIDE the run
    identity (DEC-VIX-STAMP-WIRE-001 — a moved India-VIX lake / a pre-VIX
    store beside a stamped one), yet ``_load_rows_resilient`` counted and
    framed it as the corruption-grade ``sweep_results.run_id_conflict``
    ("upstream non-determinism") and first-wins could keep the less-informed
    all-``None`` trio over the known one.
    """

    @staticmethod
    def _with_trio(
        row: ResultRow, mean: float | None, lo: float | None, hi: float | None
    ) -> ResultRow:
        from dataclasses import replace

        return replace(row, vix_mean=mean, vix_min=lo, vix_max=hi)

    def test_more_informed_trio_wins_and_logs_the_distinct_event(self, tmp_path: Path) -> None:
        """All-None first, known second → the KNOWN trio wins, zero conflicts."""
        from structlog.testing import capture_logs

        base = _row(seed=7)
        # Sorted store-path order: the 15m store loads FIRST with the
        # less-informed all-None trio; the stamped 5m twin must still win.
        _write_store(tmp_path, instrument="NIFTY", tf="15m", direction="bull", rows=[base])
        _write_store(
            tmp_path,
            instrument="NIFTY",
            tf="5m",
            direction="bull",
            rows=[self._with_trio(base, 13.5, 13.0, 14.0)],
        )
        with capture_logs() as logs:
            board = build_sweep_leaderboard(tmp_path)
        assert board.run_id_conflicts == 0  # never the corruption-grade counter
        assert board.total_rows == 1
        assert not [log for log in logs if log["event"] == "sweep_results.run_id_conflict"]
        vix_logs = [log for log in logs if log["event"] == "sweep_results.vix_only_divergence"]
        assert len(vix_logs) == 1
        assert vix_logs[0]["run_id"] == _run_id(7)
        assert vix_logs[0]["policy"] == "most-informed-wins"

    def test_both_known_differ_keeps_deterministic_first(self, tmp_path: Path) -> None:
        """Two KNOWN trios (the moved-lake case): tie on informedness → first wins."""
        from structlog.testing import capture_logs

        base = _row(seed=7)
        first = self._with_trio(base, 12.5, 12.0, 13.0)  # 15m sorts first
        second = self._with_trio(base, 20.5, 20.0, 21.0)
        _write_store(tmp_path, instrument="NIFTY", tf="15m", direction="bull", rows=[first])
        _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[second])
        with capture_logs() as logs:
            board = build_sweep_leaderboard(tmp_path)
        assert board.run_id_conflicts == 0
        assert board.total_rows == 1
        assert (
            len([log for log in logs if log["event"] == "sweep_results.vix_only_divergence"]) == 1
        )
        # The kept row is the deterministic FIRST (sorted paths), proven via
        # the private loader — the leaderboard renders no VIX column.
        from brutex.dashboard import sweep_results_view as view_mod

        rows, _, _, conflicts, _, _ = view_mod._load_rows_resilient(
            view_mod._iter_store_paths(tmp_path / "sweeps")
        )
        assert conflicts == 0
        assert [(r.vix_mean, r.vix_min, r.vix_max) for r in rows] == [(12.5, 12.0, 13.0)]

    def test_non_vix_divergence_still_counts_as_run_id_conflict(self, tmp_path: Path) -> None:
        """A metric divergence WITH a VIX divergence stays the conflict family."""
        from structlog.testing import capture_logs

        first = _row(seed=7, sharpe=1.0)
        divergent = self._with_trio(_row(seed=7, sharpe=9.9), 13.5, 13.0, 14.0)
        _write_store(tmp_path, instrument="NIFTY", tf="15m", direction="bull", rows=[first])
        _write_store(tmp_path, instrument="NIFTY", tf="5m", direction="bull", rows=[divergent])
        with capture_logs() as logs:
            board = build_sweep_leaderboard(tmp_path)
        assert board.run_id_conflicts == 1
        assert [r["sharpe_ratio"] for r in board.rows] == [1.0]  # first-wins unchanged
        assert not [log for log in logs if log["event"] == "sweep_results.vix_only_divergence"]


def test_prescanned_scan_is_ranked_without_a_rescan(tmp_path: Path) -> None:
    """An explicitly-passed ``scan=`` is ranked AS-IS — equal to scan-and-rank.

    The /results route's cached-scan path (DEC-LEDGER-DELTA-O-PAGE-001 scan/rank
    split); the discovery-gate standalone twin of the spy pins in
    ``test_sweep_results_scan_split.py``.
    """
    _write_store(
        tmp_path,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        rows=[_row(seed=1, sharpe=0.5), _row(seed=2, sharpe=2.5)],
    )
    scan = load_sweep_scan(tmp_path)
    board = build_sweep_leaderboard(tmp_path, scan=scan)
    assert board == build_sweep_leaderboard(tmp_path)
    assert [r["sharpe_ratio"] for r in board.rows] == [2.5, 0.5]
