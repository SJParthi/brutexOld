"""Unit tests for the twin-run parity harness's pure helpers + the trade
divergence utility (slice-3 scaffolding, DEC-SWEEP-BELT-RUST-001).

The end-to-end arm runners are exercised by the shipped twin gate
(``tests/golden/test_sweep_belt_twin.py``); this file proves the PURE halves —
env application/restoration, divergence folds (including the negative cases:
a genuine difference is DETECTED, never silently equal), the stable-view
projections, and the ClosedTrade field-exact comparison incl. the Decimal
representation-exactness teeth — PLUS the ``before_run`` latch-reset seam's
invocation/ordering contract on BOTH arm runners (review M1: fires exactly
once, INSIDE the arm's env delta, BEFORE any artifact is produced).
"""

# The house private-fixture import pattern (the M1 seam tests reuse the frozen
# golden's _bars/_config + the parallel suite's _grid/_bars_for, exactly like
# tests/golden/test_sweep_belt_twin.py):
# pyright: reportPrivateUsage=false

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.metrics.types import ClosedTrade
from brutex.strategies.types import Side

from tests.parity.sweep_twin_harness import (
    CHECKPOINT_VOLATILE_KEYS,
    PROGRESS_VOLATILE_KEYS,
    AuditLine,
    TwinArm,
    arm_env,
    audit_line_divergences,
    checkpoint_stable_view,
    identical_baseline_arms,
    mapping_divergences,
    progress_stable_view,
    run_grid_arm,
    run_persisted_arm,
    tree_sha256,
)
from tests.parity.trade_divergence import render_divergence_report, trade_divergences

# ────────────────────────── arm_env ──────────────────────────


class TestArmEnv:
    def test_sets_and_restores_a_new_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_TWIN_TEST_VAR", raising=False)
        with arm_env({"BRUTEX_TWIN_TEST_VAR": "1"}):
            assert os.environ["BRUTEX_TWIN_TEST_VAR"] == "1"
        assert "BRUTEX_TWIN_TEST_VAR" not in os.environ

    def test_restores_a_preexisting_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TWIN_TEST_VAR", "prior")
        with arm_env({"BRUTEX_TWIN_TEST_VAR": "override"}):
            assert os.environ["BRUTEX_TWIN_TEST_VAR"] == "override"
        assert os.environ["BRUTEX_TWIN_TEST_VAR"] == "prior"

    def test_none_means_ensure_unset_then_restore(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TWIN_TEST_VAR", "prior")
        with arm_env({"BRUTEX_TWIN_TEST_VAR": None}):
            assert "BRUTEX_TWIN_TEST_VAR" not in os.environ
        assert os.environ["BRUTEX_TWIN_TEST_VAR"] == "prior"

    def test_none_on_an_absent_variable_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_TWIN_TEST_VAR", raising=False)
        with arm_env({"BRUTEX_TWIN_TEST_VAR": None}):
            assert "BRUTEX_TWIN_TEST_VAR" not in os.environ
        assert "BRUTEX_TWIN_TEST_VAR" not in os.environ

    def test_restores_even_when_the_body_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_TWIN_TEST_VAR", "prior")

        def _boom() -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"), arm_env({"BRUTEX_TWIN_TEST_VAR": "x"}):
            _boom()
        assert os.environ["BRUTEX_TWIN_TEST_VAR"] == "prior"


class TestTwinArm:
    def test_identical_baseline_arms_carry_no_env_delta(self) -> None:
        arm_a, arm_b = identical_baseline_arms()
        assert arm_a.name != arm_b.name  # divergence lines must name the arm
        assert dict(arm_a.env) == {} == dict(arm_b.env)
        assert arm_a.before_run is None
        assert arm_b.before_run is None

    def test_future_belt_arm_shape_is_one_line(self) -> None:
        # The design's one-line belt-ON arm addition (G2) type-checks as-is.
        arm = TwinArm(name="belt_on", env={"BRUTEX_SWEEP_RUST_BELT": "1"})
        assert arm.env["BRUTEX_SWEEP_RUST_BELT"] == "1"


# ────────────────────────── before_run seam (review M1) ──────────────────────


class TestBeforeRunSeam:
    """The ``before_run`` latch-reset seam's design-critical ordering contract.

    On BOTH arm runners the hook must fire (a) exactly ONCE per arm run,
    (b) WHILE the arm's env delta is applied (the flagged kernel's latch reset
    must observe the arm's flag), and (c) BEFORE any artifact is produced (a
    reset that ran after the sweep would be a no-op for that arm).
    """

    _HOOK_VAR = "BRUTEX_TWIN_HOOK_VAR"

    def _make_hook(
        self, root: Path, observed: list[tuple[str | None, list[str]]]
    ) -> Callable[[], None]:
        def hook() -> None:
            files = (
                sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
                if root.exists()
                else []
            )
            observed.append((os.environ.get(self._HOOK_VAR), files))

        return hook

    def test_persisted_arm_hook_fires_once_inside_env_before_artifacts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("polars")
        from tests.golden.test_engine_sweep_run_golden import _bars, _config

        monkeypatch.delenv(self._HOOK_VAR, raising=False)
        root = tmp_path / "arm"
        observed: list[tuple[str | None, list[str]]] = []
        arm = TwinArm(
            name="hooked",
            env={self._HOOK_VAR: "on"},
            before_run=self._make_hook(root, observed),
        )
        artifacts = run_persisted_arm(root, arm, bars=_bars(), cfg=_config())
        # (a) exactly once; (b) inside the env delta; (c) zero artifacts yet.
        assert observed == [("on", [])]
        assert len(artifacts.tree) > 0  # ...and the run then really produced them
        assert self._HOOK_VAR not in os.environ  # env restored after the arm

    def test_grid_arm_hook_fires_once_inside_env_before_artifacts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("polars")
        from tests.unit.test_engine_sweep_parallel import _bars_for, _grid

        monkeypatch.delenv(self._HOOK_VAR, raising=False)
        root = tmp_path / "arm"
        observed: list[tuple[str | None, list[str]]] = []
        arm = TwinArm(
            name="hooked",
            env={self._HOOK_VAR: "on"},
            before_run=self._make_hook(root, observed),
        )
        artifacts = run_grid_arm(root, arm, grid=_grid(), bars_for=_bars_for)
        assert observed == [("on", [])]
        assert len(artifacts.tree) > 0
        assert self._HOOK_VAR not in os.environ


# ────────────────────────── divergence folds ──────────────────────────


class TestMappingDivergences:
    def test_equal_mappings_yield_empty(self) -> None:
        assert mapping_divergences("m", {"a": 1, "b": "x"}, {"a": 1, "b": "x"}) == []

    def test_value_difference_is_named(self) -> None:
        (line,) = mapping_divergences("m", {"a": 1}, {"a": 2})
        assert line == "m['a']: 1 != 2"

    def test_missing_keys_name_the_owning_arm(self) -> None:
        lines = mapping_divergences("m", {"a": 1}, {"b": 2})
        assert lines == [
            "m['a']: only in first arm (1)",
            "m['b']: only in second arm (2)",
        ]


class TestAuditLineDivergences:
    def test_equal_chains_yield_empty(self) -> None:
        lines: list[AuditLine] = [
            (0, "SWEEP_START", "s", {"n": 1}),
            (1, "SWEEP_COMPLETE", "s", {"n": 1}),
        ]
        assert audit_line_divergences("audit", lines, list(lines)) == []

    def test_payload_difference_is_detected(self) -> None:
        a: list[AuditLine] = [(0, "SWEEP_START", "s", {"n": 1})]
        b: list[AuditLine] = [(0, "SWEEP_START", "s", {"n": 2})]
        (line,) = audit_line_divergences("audit", a, b)
        assert line.startswith("audit[0]:")

    def test_length_mismatch_is_reported_and_prefix_still_compared(self) -> None:
        a: list[AuditLine] = [(0, "SWEEP_START", "s", {}), (1, "SWEEP_COMPLETE", "s", {})]
        b: list[AuditLine] = [(0, "SWEEP_START", "other", {})]
        lines = audit_line_divergences("audit", a, b)
        assert lines[0] == "audit: length mismatch 2 != 1"
        assert lines[1].startswith("audit[0]:")


# ────────────────────────── stable views ──────────────────────────


class TestStableViews:
    def test_progress_stable_view_drops_every_timing_key(self) -> None:
        raw = {
            "state": "done",
            "enumerated": 5,
            "evaluated": 5,
            "survivors": 4,
            "skipped": 0,
            "current": "t",
            "detail": "d",
            "percent": 100.0,
            "running": False,
            "survivor_rate": 0.8,
            "combos_per_sec": 123.4,
            "elapsed_seconds": 0.5,
            "eta_seconds": None,
            "started_at": 1.0,
            "updated_at": 2.0,
            "wall_time_utc": 1_752_000_000.5,
        }
        view = progress_stable_view(raw)
        assert set(view) == set(raw) - PROGRESS_VOLATILE_KEYS
        assert view["state"] == "done"
        assert "updated_at" not in view
        assert "wall_time_utc" not in view

    def test_progress_stable_view_tolerates_absent_keys(self) -> None:
        assert progress_stable_view({"state": "done"}) == {"state": "done"}

    def test_progress_stable_view_keeps_a_future_deterministic_key(self) -> None:
        # DENYLIST semantics (review M2): a key the harness has never heard of
        # is compared by default — never silently dropped like an allowlist.
        view = progress_stable_view({"state": "done", "belt_served": 7, "updated_at": 2.0})
        assert view == {"state": "done", "belt_served": 7}

    def test_progress_volatile_keys_are_exactly_the_wall_clock_six(self) -> None:
        # ``wall_time_utc`` joined the set when main's #1712 G4
        # (DEC-SWEEP-UNKILLABLE-CORE-001) made publish_progress always stamp
        # it from ``time.time()``; its G4 siblings ``pid``/``running`` are
        # deterministic in the in-process twin and stay compared.
        assert {
            "combos_per_sec",
            "elapsed_seconds",
            "eta_seconds",
            "started_at",
            "updated_at",
            "wall_time_utc",
        } == PROGRESS_VOLATILE_KEYS

    def test_checkpoint_stable_view_drops_only_the_wall_clock(self) -> None:
        raw = {
            "last_completed_index": 3,
            "sweep_id": "abc",
            "total_combos": 4,
            "updated_at": 1_752_000_000.0,
            "version": 1,
        }
        view = checkpoint_stable_view(raw)
        assert set(CHECKPOINT_VOLATILE_KEYS) == {"updated_at"}
        assert "updated_at" not in view
        assert view == {
            "last_completed_index": 3,
            "sweep_id": "abc",
            "total_combos": 4,
            "version": 1,
        }


class TestTreeSha256:
    def test_hashes_every_file_and_detects_a_byte_change(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.txt").write_bytes(b"alpha")
        (tmp_path / "sub" / "b.txt").write_bytes(b"beta")
        first = tree_sha256(tmp_path)
        assert set(first) == {"a.txt", "sub/b.txt"}
        (tmp_path / "sub" / "b.txt").write_bytes(b"beta'")
        second = tree_sha256(tmp_path)
        divergences = mapping_divergences("tree", first, second)
        assert len(divergences) == 1
        assert divergences[0].startswith("tree['sub/b.txt']:")

    def test_empty_directory_yields_empty_tree(self, tmp_path: Path) -> None:
        assert tree_sha256(tmp_path) == {}


# ────────────────────────── trade divergences ──────────────────────────


def _trade(
    *,
    pnl: int = 100,
    vix_entry: Decimal | None = None,
    vix_exit: Decimal | None = None,
    holding: int = 2,
) -> ClosedTrade:
    return ClosedTrade(
        pnl_paisa=pnl,
        mfe_paisa=150,
        mae_paisa=40,
        holding_bars=holding,
        entry_ts=datetime(2026, 1, 5, 4, 0, tzinfo=UTC),
        exit_ts=datetime(2026, 1, 5, 4, 30, tzinfo=UTC),
        direction=Side.BUY,
        vix_at_entry=vix_entry,
        vix_at_exit=vix_exit,
    )


class TestTradeDivergences:
    def test_equal_lists_yield_empty(self) -> None:
        a = [_trade(), _trade(pnl=-50, vix_entry=Decimal("14.25"))]
        b = [_trade(), _trade(pnl=-50, vix_entry=Decimal("14.25"))]
        assert trade_divergences(a, b) == []

    def test_int_field_difference_is_named(self) -> None:
        (line,) = trade_divergences([_trade(pnl=100)], [_trade(pnl=101)])
        assert line == "trade[0].pnl_paisa: expected=100 actual=101"

    def test_every_closedtrade_field_is_covered(self) -> None:
        # holding_bars is neither a money field nor a stamp — a divergence on
        # it proves the compare iterates dataclasses.fields, not a hand list.
        (line,) = trade_divergences([_trade(holding=2)], [_trade(holding=3)])
        assert "holding_bars" in line

    def test_vix_decimal_representation_exactness(self) -> None:
        # Numerically equal, representation different → MUST diverge (the
        # design's "Decimal equality exact" law, strictly stronger than ==).
        a = [_trade(vix_entry=Decimal("19.5"))]
        b = [_trade(vix_entry=Decimal("19.50"))]
        (line,) = trade_divergences(a, b)
        assert "vix_at_entry" in line

    def test_vix_none_vs_value_diverges_both_ways(self) -> None:
        stamped = [_trade(vix_exit=Decimal("15"))]
        unstamped = [_trade(vix_exit=None)]
        assert len(trade_divergences(stamped, unstamped)) == 1
        assert len(trade_divergences(unstamped, stamped)) == 1

    def test_length_mismatch_reports_and_compares_the_prefix(self) -> None:
        a = [_trade(pnl=1), _trade(pnl=2)]
        b = [_trade(pnl=9)]
        lines = trade_divergences(a, b, label="finalist#0")
        assert lines[0] == "finalist#0: length mismatch expected=2 actual=1"
        assert lines[1] == "finalist#0[0].pnl_paisa: expected=1 actual=9"

    def test_custom_label_prefixes_every_line(self) -> None:
        lines = trade_divergences([_trade(pnl=1)], [_trade(pnl=2)], label="mask=0b101")
        assert all(line.startswith("mask=0b101") for line in lines)


class TestRenderDivergenceReport:
    def test_empty_reads_no_divergences(self) -> None:
        assert render_divergence_report([]) == "no divergences"

    def test_under_limit_renders_verbatim(self) -> None:
        assert render_divergence_report(["a", "b"]) == "a\nb"

    def test_over_limit_names_the_truncation(self) -> None:
        report = render_divergence_report([f"line{i}" for i in range(25)], limit=20)
        lines = report.splitlines()
        assert len(lines) == 21
        assert lines[-1] == "... 5 more divergence line(s) suppressed (limit=20)"

    def test_exactly_at_limit_has_no_suppression_note(self) -> None:
        report = render_divergence_report(["a", "b", "c"], limit=3)
        assert report == "a\nb\nc"

    def test_nonpositive_limit_is_refused(self) -> None:
        with pytest.raises(ValueError, match="limit must be >= 1"):
            render_divergence_report(["a"], limit=0)
