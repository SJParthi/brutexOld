"""Scan/rank split + streaming era read for /results (DEC-LEDGER-DELTA-O-PAGE-001).

Sub-part (d): ``build_sweep_leaderboard`` splits into the generation-invariant
:func:`load_sweep_scan` (store discovery + resilient load + the STREAMING audit
era pass + every payload-honesty field) and the cheap per-request rank, whose
two-pass sort is moved VERBATIM so the total order stays byte-identical to
``ResultsStore.top_k``. ``_snapshot_eras`` streams
:func:`brutex.orchestrator.audit.iter_entries` (O(1) memory) instead of
materialising ``read_entries``; ``audit.py`` itself has ZERO diff, so
``verify_chain``'s integrity contract is structurally untouched (regression
pinned here).

Covers T-d1..T-d6.
"""

# The drift-pin / era tests read module-private helpers deliberately.
# pyright: reportPrivateUsage=none

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars", reason="the reader loads per-tuple Parquet stores via ResultsStore")

from brutex.dashboard.sweep_results_view import (
    SweepScan,
    build_sweep_leaderboard,
    load_sweep_scan,
)
from brutex.engine.results_store import ResultRow, ResultsStore


def _run_id(seed: int) -> str:
    return f"{seed:064x}"


def _row(
    *,
    seed: int,
    instrument: str = "NIFTY",
    tf: str = "5m",
    direction: str = "bull",
    net_pnl_paisa: int = 100_00,
    sharpe: float = 1.0,
    mask_bits: int = 1,
) -> ResultRow:
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
        total_trades=10,
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
    lake_root: Path, rows: list[ResultRow], *, instrument: str = "NIFTY", tf: str = "5m"
) -> ResultsStore:
    dest = lake_root / "sweeps" / instrument / tf / f"bull_k1_{'a' * 16}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    store = ResultsStore(path=dest)
    store.extend(rows)
    store.flush()
    return store


def _write_sweep_start_audit(lake_root: Path, payloads: list[dict[str, object]]) -> Path:
    from brutex.orchestrator.audit import AuditWriter

    audit_path = lake_root / "sweeps" / "audit" / "sweep-all.jsonl"
    with AuditWriter.open(audit_path, resume=audit_path.exists()) as writer:
        for payload in payloads:
            writer.append(action="SWEEP_START", subject="sweep:test", payload=payload)
    return audit_path


def _start_payload(*, snapshot: str, tf: str = "5m") -> dict[str, object]:
    return {
        "instrument": "NIFTY",
        "tf": tf,
        "direction": "bull",
        "mode": "A",
        "k": 1,
        "data_snapshot_hash": snapshot,
    }


class TestSplitGolden:
    """T-d1 — order + payload preservation across the scan/rank split."""

    def test_scan_passed_output_byte_identical_to_inline_scan(self, tmp_path: Path) -> None:
        """``scan=load_sweep_scan(...)`` and ``scan=None`` produce byte-identical
        payloads on a precision + tie + mixed-era fixture (the split golden).
        """
        rows = [
            _row(seed=3, net_pnl_paisa=2**53),  # the largest exactly-float int
            _row(seed=2, net_pnl_paisa=2**53 + 1),  # float-ties with 2**53 — raw-int teeth
            _row(seed=5, net_pnl_paisa=77),  # equal-value tie pair → run_id asc
            _row(seed=4, net_pnl_paisa=77),
        ]
        _write_store(tmp_path, rows)
        _write_sweep_start_audit(tmp_path, [_start_payload(snapshot="a" * 64)])
        inline = build_sweep_leaderboard(tmp_path, rank_by="net_pnl_paisa").to_dict()
        split = build_sweep_leaderboard(
            tmp_path, rank_by="net_pnl_paisa", scan=load_sweep_scan(tmp_path)
        ).to_dict()
        assert split == inline

    def test_rank_order_byte_identical_to_results_store_top_k(self, tmp_path: Path) -> None:
        """The ranked run_id sequence equals ``ResultsStore.top_k``'s total order
        (Golden Rule #4) on the raw-int precision teeth + the run_id-asc tie
        fixture — the two-pass sort moved verbatim, never a float coercion.
        """
        big = 2**53
        assert float(big + 1) == float(big)  # the precision trap the raw key avoids
        rows = [
            _row(seed=1, net_pnl_paisa=big),
            _row(seed=2, net_pnl_paisa=big + 1),
            _row(seed=6, net_pnl_paisa=77),  # tie pair: run_id ascending breaks it
            _row(seed=5, net_pnl_paisa=77),
        ]
        store = _write_store(tmp_path, rows)
        board = build_sweep_leaderboard(
            tmp_path, rank_by="net_pnl_paisa", scan=load_sweep_scan(tmp_path)
        )
        expected = [r.run_id for r in store.top_k(len(rows), by="net_pnl_paisa")]
        assert [r["run_id"] for r in board.rows] == expected
        # Hand-derived teeth: +1 beats 2**53; the tie pair ranks run_id-ascending.
        assert expected == [_run_id(2), _run_id(1), _run_id(5), _run_id(6)]


class TestScanReuse:
    """T-d2 — one scan serves every filter/rank click (zero store/audit re-reads)."""

    def test_rank_calls_perform_zero_store_loads_and_zero_audit_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.orchestrator import audit as audit_mod

        _write_store(tmp_path, [_row(seed=1), _row(seed=2, sharpe=2.0)])
        _write_store(tmp_path, [_row(seed=3, tf="15m")], tf="15m")
        _write_sweep_start_audit(tmp_path, [_start_payload(snapshot="a" * 64)])

        load_calls: list[Path] = []
        real_load = ResultsStore.load

        def _load_spy(cls: type[ResultsStore], path: Path) -> ResultsStore:
            load_calls.append(path)
            return real_load(path)

        monkeypatch.setattr(ResultsStore, "load", classmethod(_load_spy))

        iter_calls: list[Path] = []
        real_iter = audit_mod.iter_entries

        def _iter_spy(path: Path) -> Any:
            iter_calls.append(path)
            return real_iter(path)

        monkeypatch.setattr(audit_mod, "iter_entries", _iter_spy)

        scan = load_sweep_scan(tmp_path)
        assert len(load_calls) == 2  # one per store, paid by the scan
        assert len(iter_calls) == 1  # ONE streaming era pass

        boards = [
            build_sweep_leaderboard(tmp_path, scan=scan),
            build_sweep_leaderboard(tmp_path, rank_by="net_pnl_paisa", top_n=1, scan=scan),
            build_sweep_leaderboard(tmp_path, instrument="NIFTY", tf="15m", scan=scan),
        ]
        assert [b.total_rows for b in boards] == [3, 3, 1]
        assert len(load_calls) == 2  # rank clicks re-loaded NOTHING
        assert len(iter_calls) == 1  # and re-read NO audit lines


class TestHonestyFieldParity:
    """T-d3 — every honesty field survives the split unchanged."""

    def test_corrupt_store_conflict_and_mixed_eras_project_identically(
        self, tmp_path: Path
    ) -> None:
        # A good store, a run_id-conflicting twin, and a corrupt Parquet.
        _write_store(tmp_path, [_row(seed=7, sharpe=1.0), _row(seed=1)])
        _write_store(tmp_path, [_row(seed=7, sharpe=9.9, tf="15m")], tf="15m")
        bad = tmp_path / "sweeps" / "BANKNIFTY" / "15m" / "bull_k1_deadbeefdeadbeef.parquet"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"not a parquet file at all")
        # Mixed eras for the covered coordinates.
        _write_sweep_start_audit(
            tmp_path,
            [
                _start_payload(snapshot="a" * 64),
                _start_payload(snapshot="b" * 64, tf="15m"),
            ],
        )
        inline = build_sweep_leaderboard(tmp_path)
        split = build_sweep_leaderboard(tmp_path, scan=load_sweep_scan(tmp_path))
        assert split.to_dict() == inline.to_dict()
        # The fixture genuinely exercised every honesty field (vacuity guard).
        assert split.skipped == 1
        assert split.run_id_conflicts == 1
        assert split.mixed_snapshot_eras is True
        assert split.snapshot_hashes == ("a" * 64, "b" * 64)
        scan = load_sweep_scan(tmp_path)
        assert isinstance(scan, SweepScan)
        assert (scan.skipped, scan.run_id_conflicts) == (1, 1)


class TestStreamingEras:
    """T-d4/T-d5 — the era read streams ``iter_entries`` with today's degrades."""

    def test_torn_tail_is_tolerated(self, tmp_path: Path) -> None:
        """An unterminated final line (interrupted append) is skipped in the
        generator itself — the committed ``SWEEP_START`` prefix still stamps rows.
        """
        _write_store(tmp_path, [_row(seed=1)])
        era = "a" * 64
        audit_path = _write_sweep_start_audit(tmp_path, [_start_payload(snapshot=era)])
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write('{"torn": "no trailing newline')  # a torn tail, NOT committed
        board = build_sweep_leaderboard(tmp_path)
        assert board.snapshot_hashes == (era,)
        assert [r["snapshot_era"] for r in board.rows] == [era]

    def test_malformed_committed_line_discards_partial_collection(self, tmp_path: Path) -> None:
        """A malformed COMMITTED line mid-chain raises at ITERATION time — the
        loop-wrapped guard discards any partially-collected eras (today's exact
        all-or-nothing degrade) + logs ``era_scan_failed``, never a 500.
        """
        from brutex.dashboard.sweep_results_view import SNAPSHOT_ERA_UNKNOWN
        from structlog.testing import capture_logs

        _write_store(tmp_path, [_row(seed=1)])
        audit_path = _write_sweep_start_audit(tmp_path, [_start_payload(snapshot="a" * 64)])
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write("this committed line is not JSON\n")  # newline = committed
            f.write('{"decoy": "keeps the malformed line mid-chain"}\n')
        with capture_logs() as logs:
            board = build_sweep_leaderboard(tmp_path)
        # The valid SWEEP_START collected BEFORE the bad line is DISCARDED —
        # all-or-nothing, exactly the pre-change read_entries behaviour.
        assert board.snapshot_hashes == ()
        assert [r["snapshot_era"] for r in board.rows] == [SNAPSHOT_ERA_UNKNOWN]
        assert any(log["event"] == "sweep_results.era_scan_failed" for log in logs)

    def test_read_entries_is_never_called_by_the_era_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-d5: the era scan streams — the list-materialising ``read_entries``
        is structurally out of the path (an import-site spy would fail the build).
        """
        from brutex.orchestrator import audit as audit_mod

        _write_store(tmp_path, [_row(seed=1)])
        era = "a" * 64
        _write_sweep_start_audit(tmp_path, [_start_payload(snapshot=era)])

        def _forbidden(path: Path) -> Any:
            raise AssertionError("read_entries must not be used by _snapshot_eras")

        monkeypatch.setattr(audit_mod, "read_entries", _forbidden)
        board = build_sweep_leaderboard(tmp_path)
        assert board.snapshot_hashes == (era,)


class TestVerifyChainUnchanged:
    """T-d6 — the audit integrity oracle is untouched by the display-side switch."""

    def test_verify_chain_still_verifies_the_same_fixture_chain(self, tmp_path: Path) -> None:
        from brutex.orchestrator.audit import verify_chain

        audit_path = _write_sweep_start_audit(
            tmp_path,
            [_start_payload(snapshot="a" * 64), _start_payload(snapshot="b" * 64)],
        )
        assert verify_chain(audit_path) == 2
        # A torn tail is forgiven by the shared discriminator; the committed
        # prefix still verifies end-to-end (the contract the switch must not erode).
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write('{"torn": "no trailing newline')
        assert verify_chain(audit_path) == 2

    def test_verify_chain_still_raises_on_committed_corruption(self, tmp_path: Path) -> None:
        from brutex.orchestrator.audit import AuditError, verify_chain

        audit_path = _write_sweep_start_audit(tmp_path, [_start_payload(snapshot="a" * 64)])
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write("committed garbage\n")
        with pytest.raises(AuditError):
            verify_chain(audit_path)
