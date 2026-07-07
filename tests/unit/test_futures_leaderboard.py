"""Display-annotation + sidecar pins (DEC-FUTURES-EXEC-TRANSLATION-001).

R13 annotation-never-reranks · R27 every number traces + fail-loud direct
attribute access · R42 atomic content-hash-stamped sidecar.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from brutex.futures.executor import STOCK_SPOT_DEFERRAL_NOTE, ExecutionKind, FuturesRuleResult
from brutex.futures.leaderboard import (
    ANNOTATION_KEY,
    project_futures_annotation,
    read_translation_sidecar,
    render_futures_leaderboard,
    write_translation_sidecar,
)


def _result(**overrides: Any) -> FuturesRuleResult:
    kwargs: dict[str, Any] = {
        "instrument": "NIFTY",
        "direction": "bull",
        "n_signals": 4,
        "n_accepted": 2,
        "n_rejected_lot_size_unknown": 1,
        "n_rejected_cost_regime_unverified": 0,
        "n_rejected_no_contract": 0,
        "n_rejected_no_data": 1,
        "n_rejected_stale_bar": 0,
        "gross_pnl_paisa": 749_250,
        "total_charges_paisa": 0,
        "net_pnl_paisa": 749_250,
        "n_winners": 1,
        "n_losers": 1,
        "n_vix_missing": 1,
    }
    kwargs.update(overrides)
    return FuturesRuleResult(**kwargs)


def _row() -> SimpleNamespace:
    return SimpleNamespace(
        instrument="NIFTY", tf="5m", direction="bull", run_id="abc123def456ghi789"
    )


class TestRenderLeaderboard:
    def test_r27_every_number_traces(self) -> None:
        # [R27] each displayed figure derives from the result's real fields —
        # money via format_inr, win% from win_rate, rejects itemised.
        text = render_futures_leaderboard(_row(), _result())
        assert "futures-exec · NIFTY 5m bull · run abc123def456…" in text
        assert "₹7,492.50" in text  # 749,250 paisa gross AND net
        assert "₹0.00" in text  # zero charges (cost-free index)
        assert " 50.0%" in text  # 1 of 2 winners
        assert "lot 1" in text
        assert "data 1" in text
        assert "vix-missing 1" in text

    def test_honesty_footer_absent_when_no_cost_rejects(self) -> None:
        assert "UNVERIFIED" not in render_futures_leaderboard(_row(), _result())

    def test_leaderboard_render_display_only_fail_loud(self) -> None:
        # [R13/R27] the footer derives from regime-table DATA when cost
        # rejects exist; a renamed field fails LOUD (direct attribute access).
        text = render_futures_leaderboard(
            _row(), _result(n_rejected_cost_regime_unverified=1, n_rejected_lot_size_unknown=0)
        )
        assert "1 trade(s) fell in an UNVERIFIED futures cost window" in text
        assert "history-start → open" in text  # derived span, both exchanges
        assert "never priced with a fabricated rate" in text
        # Fail-loud proof: a result-shaped object MISSING the counter raises.
        broken = SimpleNamespace(instrument="NIFTY", n_accepted=0, win_rate=0.0)
        with pytest.raises(AttributeError):
            render_futures_leaderboard(_row(), broken)  # type: ignore[arg-type]

    def test_render_never_mutates(self) -> None:
        result = _result()
        before = repr(result)
        render_futures_leaderboard(_row(), result)
        assert repr(result) == before


class TestProjectAnnotation:
    def test_executed_annotation_additive_only(self) -> None:
        # [R13/R30] every pre-existing key passes through byte-identical; the
        # ONE new key carries the leg; the input mapping is NOT mutated.
        row = {"run_id": "abc", "rank": 1, "net_pnl_paisa": 111}
        out = project_futures_annotation(row, _result())
        assert {k: out[k] for k in row} == row
        assert row == {"run_id": "abc", "rank": 1, "net_pnl_paisa": 111}  # unmutated
        leg = out[ANNOTATION_KEY]
        assert isinstance(leg, dict)
        assert leg["status"] == "executed"
        assert leg["net_pnl_paisa"] == 749_250
        assert leg["n_rejected"] == 2
        assert leg["win_rate"] == 0.5

    def test_r13_r30_rank_independence(self) -> None:
        # [R13/R30] removing/perturbing the futures leg leaves the pure row
        # BIT-IDENTICAL: the annotation is a separate key, so stripping it
        # recovers the exact input row.
        row = {"run_id": "abc", "rank": 1, "sharpe_ratio": 1.25}
        annotated = project_futures_annotation(row, _result())
        stripped = {k: v for k, v in annotated.items() if k != ANNOTATION_KEY}
        assert stripped == row

    def test_r11_stock_winner_deferred_annotation(self) -> None:
        # [R11] a STOCK winner annotates the honest deferral note NAMING the
        # future scope decree — never a fabricated leg.
        out = project_futures_annotation(
            {"run_id": "x"}, None, kind=ExecutionKind.STOCK_SPOT_DEFERRED
        )
        leg = out[ANNOTATION_KEY]
        assert isinstance(leg, dict)
        assert leg["status"] == "stock_spot_deferred"
        assert leg["note"] == STOCK_SPOT_DEFERRAL_NOTE
        assert "scope decree" in STOCK_SPOT_DEFERRAL_NOTE

    def test_unsupported_annotation(self) -> None:
        out = project_futures_annotation({"run_id": "x"}, None, kind=ExecutionKind.UNSUPPORTED)
        leg = out[ANNOTATION_KEY]
        assert isinstance(leg, dict)
        assert leg["status"] == "unsupported"

    def test_double_annotation_refused(self) -> None:
        annotated = project_futures_annotation({"run_id": "x"}, _result())
        with pytest.raises(ValueError, match="refusing to overwrite"):
            project_futures_annotation(annotated, _result())

    def test_kind_result_mismatches_fail_loud(self) -> None:
        with pytest.raises(ValueError, match="requires the executed"):
            project_futures_annotation({"run_id": "x"}, None)
        with pytest.raises(ValueError, match="must not carry a result"):
            project_futures_annotation(
                {"run_id": "x"}, _result(), kind=ExecutionKind.STOCK_SPOT_DEFERRED
            )


class TestTranslationSidecar:
    def test_r42_r16_r7_idempotent_rerun(self, tmp_path: Path) -> None:
        # [R42/R16/R7] write → read round-trips; a re-run rebuilds a
        # byte-identical sidecar (deterministic, upstream untouched).
        result = _result()
        path1 = write_translation_sidecar(result, out_root=tmp_path, run_id="abc123def456ghi")
        first_bytes = path1.read_bytes()
        payload = read_translation_sidecar(path1)
        assert payload["run_id"] == "abc123def456ghi"
        assert payload["net_pnl_paisa"] == 749_250
        path2 = write_translation_sidecar(result, out_root=tmp_path, run_id="abc123def456ghi")
        assert path2 == path1
        assert path2.read_bytes() == first_bytes

    def test_r28_r41_r43_sidecar_carries_run_id_and_stamp(self, tmp_path: Path) -> None:
        path = write_translation_sidecar(_result(), out_root=tmp_path, run_id="runid-42")
        assert path.name == "futures-exec-runid-42.jsonl"
        record = json.loads(path.read_text())
        assert set(record) == {"payload", "content_hash"}

    def test_r42_tampered_sidecar_fails_stamp(self, tmp_path: Path) -> None:
        # [R42] a tampered payload fails verification — discard + rebuild.
        path = write_translation_sidecar(_result(), out_root=tmp_path, run_id="tamper-run")
        record = json.loads(path.read_text())
        record["payload"]["net_pnl_paisa"] = 999_999_999
        path.write_text(json.dumps(record))
        with pytest.raises(ValueError, match="failed its content-hash stamp"):
            read_translation_sidecar(path)

    def test_r42_half_written_sidecar_fails_stamp(self, tmp_path: Path) -> None:
        # [R42] a torn/truncated record fails loudly, never consumed.
        path = write_translation_sidecar(_result(), out_root=tmp_path, run_id="torn-run")
        raw = path.read_text()
        path.write_text(raw[: len(raw) // 2])
        with pytest.raises(ValueError, match="not valid JSON"):
            read_translation_sidecar(path)

    def test_malformed_record_shape_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text(json.dumps({"payload": "not-a-dict", "content_hash": "x"}))
        with pytest.raises(ValueError, match="malformed"):
            read_translation_sidecar(path)
        path.write_text(json.dumps(["not", "a", "dict"]))
        with pytest.raises(ValueError, match="malformed"):
            read_translation_sidecar(path)
