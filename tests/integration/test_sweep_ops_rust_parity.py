"""Real-wheel parity for the sweep-ops slice-1 laws (DEC-RUST-SWEEP-OPS-001).

Skipped when the ``brutex_sweep_ops`` wheel is not installed (the main CI
jobs deliberately do not build it — the brutex-lake / brutex-feed posture);
run locally / in a dedicated parity lane after
``maturin develop --release`` inside ``rust/brutex-sweep-ops``.

Every case drives the REAL wired production functions flag-ON against the
compiled crate and asserts value-identity with the retained Python oracles
(the flag-OFF path) — plus the crate-level drift pins.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Final

import pytest

brutex_sweep_ops = pytest.importorskip(
    "brutex_sweep_ops", reason="brutex_sweep_ops wheel not installed"
)

from brutex.engine.run_layout import (  # noqa: E402
    MIRROR_ROOT_ENV,
    RUN_DIR_SUBDIR,
    mirror_run_dir,
    run_dir,
)
from brutex.engine.sweep_identity import (  # noqa: E402
    _gate_config_fragment,
    _gate_config_fragment_python,
)
from brutex.engine.sweep_liveness import RunVerdict, progress_verdict  # noqa: E402
from brutex.engine.sweep_mem_budget import (  # noqa: E402
    BATCH_RANK_BYTES_PER_KEPT,
    BoundedRankMode,
    plan_rank_memory,
)
from brutex.engine.sweep_ops_rust import (  # noqa: E402
    SWEEP_OPS_RUST_ENV,
    reset_sweep_ops_rust_for_tests,
    sweep_ops_rust_disabled,
)
from brutex.engine.sweep_preflight import (  # noqa: E402
    preflight_derived_freshness,
    preflight_slices,
)
from brutex.engine.sweep_progress import (  # noqa: E402
    STATE_RUNNING,
    SweepProgressSnapshot,
    _derived_python,
)
from brutex.engine.sweep_sla_guard import SlaDecision, check_sweep_sla  # noqa: E402
from brutex.engine.sweep_stall_policy import (  # noqa: E402
    StallPolicy,
    resolve_stall_policy,
)
from brutex.ranking import OverfitGateConfig  # noqa: E402
from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

_RUN_ID: Final[str] = "deadbeef" * 8


def _snapshot(
    *,
    enumerated: int = 200,
    evaluated: int = 25,
    survivors: int = 4,
    started_at: float = 100.0,
    updated_at: float = 110.0,
) -> SweepProgressSnapshot:
    return SweepProgressSnapshot(
        state=STATE_RUNNING,
        enumerated=enumerated,
        evaluated=evaluated,
        survivors=survivors,
        skipped=0,
        current="",
        detail="",
        started_at=started_at,
        updated_at=updated_at,
    )


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "1")
    reset_sweep_ops_rust_for_tests()
    yield
    reset_sweep_ops_rust_for_tests()


class _FragmentGrid:
    """A minimal structural `_GateConfigInput` (frozen-attribute style)."""

    def __init__(
        self,
        *,
        overfit: OverfitGateConfig | None = None,
        stage3_enable: bool = False,
        stage3_top_k: int = 10,
        stage3_max_fevals: int = 200,
        stage3_max_restarts: int = 3,
    ) -> None:
        self._overfit = overfit
        self._stage3_enable = stage3_enable
        self._stage3_top_k = stage3_top_k
        self._stage3_max_fevals = stage3_max_fevals
        self._stage3_max_restarts = stage3_max_restarts

    @property
    def overfit(self) -> OverfitGateConfig | None:
        return self._overfit

    @property
    def stage3_enable(self) -> bool:
        return self._stage3_enable

    @property
    def stage3_top_k(self) -> int:
        return self._stage3_top_k

    @property
    def stage3_max_fevals(self) -> int:
        return self._stage3_max_fevals

    @property
    def stage3_max_restarts(self) -> int:
        return self._stage3_max_restarts


class TestCrateDriftPins:
    def test_run_dir_subdir_twin(self) -> None:
        """The crate's RUN_DIR_SUBDIR is the Python constant (drift pin)."""
        assert brutex_sweep_ops.RUN_DIR_SUBDIR == RUN_DIR_SUBDIR

    def test_version_present(self) -> None:
        assert isinstance(brutex_sweep_ops.__version__, str)
        assert brutex_sweep_ops.__version__


class TestRunLayoutParity:
    @pytest.mark.parametrize(
        "root",
        [
            "/lake/sweeps",
            "/lake/sweeps/",  # trailing slash
            "relative/root",
            ".",  # cwd-shaped root (Path normalisation law)
            "/",
        ],
    )
    def test_run_dir_parity(self, root: str) -> None:
        expected = Path(root) / RUN_DIR_SUBDIR / _RUN_ID
        assert run_dir(Path(root), _RUN_ID) == expected
        assert sweep_ops_rust_disabled() is False  # burn-in stayed clean

    def test_mirror_override_parity(self) -> None:
        assert mirror_run_dir(_RUN_ID, env={MIRROR_ROOT_ENV: " /mnt/mirror "}) == (
            Path("/mnt/mirror") / _RUN_ID
        )
        assert sweep_ops_rust_disabled() is False

    def test_mirror_home_parity(self) -> None:
        assert mirror_run_dir(_RUN_ID, env={}) == Path.home() / ".brutex" / "runs" / _RUN_ID
        assert sweep_ops_rust_disabled() is False

    def test_mirror_disabled_stays_none(self) -> None:
        assert mirror_run_dir(_RUN_ID, env={MIRROR_ROOT_ENV: "  "}) is None


class TestGateFragmentParity:
    @pytest.mark.parametrize(
        "grid",
        [
            _FragmentGrid(),
            _FragmentGrid(overfit=OverfitGateConfig(dsr_min=0.95)),
            _FragmentGrid(stage3_enable=True, stage3_top_k=7, stage3_max_fevals=11),
            _FragmentGrid(overfit=OverfitGateConfig(dsr_min=0.90), stage3_enable=True),
        ],
    )
    def test_fragment_parity(self, grid: _FragmentGrid) -> None:
        assert _gate_config_fragment(grid) == _gate_config_fragment_python(grid)
        assert sweep_ops_rust_disabled() is False


class TestStallPolicyParity:
    @pytest.mark.parametrize(
        ("estimate", "expected"),
        [
            (60.0, StallPolicy(timeout_seconds=600.0, source="scaled")),
            (30.0, StallPolicy(timeout_seconds=300.0, source="default")),  # boundary
            (1.0, StallPolicy(timeout_seconds=300.0, source="default")),
        ],
    )
    def test_stall_parity(self, estimate: float, expected: StallPolicy) -> None:
        assert resolve_stall_policy(per_tuple_estimate_s=estimate, workers=1, env={}) == expected
        assert sweep_ops_rust_disabled() is False


class TestBurnInSteadyState:
    def test_burn_in_completes_and_serves_rust(self) -> None:
        """>16 engaged calls per law complete the burn-in with zero autoreverts."""
        for _ in range(20):
            run_dir(Path("/lake/sweeps"), _RUN_ID)
            _gate_config_fragment(_FragmentGrid(stage3_enable=True))
            resolve_stall_policy(per_tuple_estimate_s=60.0, workers=1, env={})
        assert sweep_ops_rust_disabled() is False
        # Steady state still value-identical to the oracle.
        assert run_dir(Path("/lake/sweeps"), _RUN_ID) == (
            Path("/lake/sweeps") / RUN_DIR_SUBDIR / _RUN_ID
        )


# ────────────────── slice 1b — the deferred leaf batch ──────────────────


class TestSliceOneBCrateDriftPins:
    def test_rank_plan_bytes_per_kept_twin(self) -> None:
        """The crate's 1024 B/kept constant is the Python constant (drift pin:
        n_masks=1 makes the projection read the constant directly)."""
        projected, verdict = brutex_sweep_ops.rank_plan_decision(1, 2**40, "auto", True, True)
        assert projected == BATCH_RANK_BYTES_PER_KEPT
        assert verdict == "batch"

    def test_sla_tokens_are_enum_values(self) -> None:
        assert brutex_sweep_ops.sla_decision(1, 1.0, 10.0, False)[1] == SlaDecision.ALLOWED.value
        assert brutex_sweep_ops.sla_decision(100, 1.0, 10.0, False)[1] == SlaDecision.REFUSED.value
        assert (
            brutex_sweep_ops.sla_decision(100, 1.0, 10.0, True)[1] == SlaDecision.ACKNOWLEDGED.value
        )

    def test_liveness_tokens_are_enum_values(self) -> None:
        assert brutex_sweep_ops.liveness_tail(False, 0.0, 0.0, 1.0) == RunVerdict.NOT_RUNNING.value
        assert brutex_sweep_ops.liveness_tail(True, 100.0, 0.0, 1.0) == RunVerdict.STALE.value
        assert brutex_sweep_ops.liveness_tail(True, 0.5, 0.0, 1.0) == RunVerdict.RUNNING.value


class TestProgressDerivedParity:
    def test_snapshot_to_dict_parity_flag_on_vs_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        snap = _snapshot()
        monkeypatch.delenv(SWEEP_OPS_RUST_ENV, raising=False)
        reset_sweep_ops_rust_for_tests()
        off = snap.to_dict()
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "1")
        reset_sweep_ops_rust_for_tests()
        assert snap.to_dict() == off
        assert sweep_ops_rust_disabled() is False

    @pytest.mark.parametrize(
        ("enumerated", "evaluated", "expected_percent"),
        [
            (400, 1, 0.2),  # 0.25% — the exact decimal tie → half-even 0.2
            (400, 3, 0.8),  # 0.75% — the exact decimal tie → half-even 0.8
            (3, 1, 33.3),  # correctly-rounded non-tie
            (8, 1, 12.5),  # already at one fractional digit
            (10, 10, 100.0),  # the min(100, …) cap
        ],
    )
    def test_percent_round_half_even_parity(
        self, enumerated: int, evaluated: int, expected_percent: float
    ) -> None:
        snap = _snapshot(enumerated=enumerated, evaluated=evaluated)
        rust = brutex_sweep_ops.progress_derived(
            enumerated, evaluated, snap.survivors, snap.started_at, snap.updated_at
        )
        assert rust[0] == expected_percent
        assert rust == _derived_python(snap)

    @given(
        enumerated=st.integers(min_value=0, max_value=2**53),
        evaluated=st.integers(min_value=0, max_value=2**53),
        survivors=st.integers(min_value=0, max_value=2**53),
        started_at=st.floats(allow_nan=False, allow_infinity=False, min_value=0.0, max_value=1e12),
        updated_at=st.floats(allow_nan=False, allow_infinity=False, min_value=0.0, max_value=1e12),
    )
    def test_hypothesis_differential_vs_python_law(
        self,
        enumerated: int,
        evaluated: int,
        survivors: int,
        started_at: float,
        updated_at: float,
    ) -> None:
        """The crate cluster ≡ the Python law over the whole gated domain
        (the round-half-even + int-true-division parity teeth)."""
        snap = _snapshot(
            enumerated=enumerated,
            evaluated=evaluated,
            survivors=survivors,
            started_at=started_at,
            updated_at=updated_at,
        )
        assert brutex_sweep_ops.progress_derived(
            enumerated, evaluated, survivors, started_at, updated_at
        ) == _derived_python(snap)


class TestSlaGuardParity:
    @pytest.mark.parametrize(
        ("tuple_count", "acknowledge", "decision"),
        [
            (100, False, SlaDecision.ALLOWED),
            (1_000_000, False, SlaDecision.REFUSED),
            (1_000_000, True, SlaDecision.ACKNOWLEDGED),
        ],
    )
    def test_check_sweep_sla_parity_flag_on_vs_off(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tuple_count: int,
        acknowledge: bool,
        decision: SlaDecision,
    ) -> None:
        monkeypatch.delenv(SWEEP_OPS_RUST_ENV, raising=False)
        reset_sweep_ops_rust_for_tests()
        off = check_sweep_sla(tuple_count, acknowledge=acknowledge, env={})
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "1")
        reset_sweep_ops_rust_for_tests()
        on = check_sweep_sla(tuple_count, acknowledge=acknowledge, env={})
        assert on == off
        assert on.decision is decision
        assert sweep_ops_rust_disabled() is False


class TestRankPlanParity:
    def test_plan_rank_memory_parity_every_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cases: list[dict[str, object]] = [
            {
                "n_masks": 1000,
                "top_k": 5,
                "digest_path": True,
                "mode": BoundedRankMode.AUTO,
                "budget_bytes": 8 * 1024**2,
            },
            {
                "n_masks": 10_000_000,
                "top_k": 5,
                "digest_path": True,
                "mode": BoundedRankMode.AUTO,
                "budget_bytes": 1024,
            },
            {
                "n_masks": 10_000_000,
                "top_k": None,
                "digest_path": True,
                "mode": BoundedRankMode.AUTO,
                "budget_bytes": 1024,
            },
            {
                "n_masks": 10_000_000,
                "top_k": 5,
                "digest_path": False,
                "mode": BoundedRankMode.AUTO,
                "budget_bytes": 1024,
            },
            {
                "n_masks": 10_000_000,
                "top_k": 5,
                "digest_path": True,
                "mode": BoundedRankMode.OFF,
                "budget_bytes": 1024,
            },
            {
                "n_masks": 10_000_000,
                "top_k": 5,
                "digest_path": True,
                "mode": BoundedRankMode.ON,
                "budget_bytes": 1024,
            },
        ]
        monkeypatch.delenv(SWEEP_OPS_RUST_ENV, raising=False)
        reset_sweep_ops_rust_for_tests()
        off = [plan_rank_memory(**case) for case in cases]  # type: ignore[arg-type]
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "1")
        reset_sweep_ops_rust_for_tests()
        on = [plan_rank_memory(**case) for case in cases]  # type: ignore[arg-type]
        assert on == off
        assert sweep_ops_rust_disabled() is False


class TestPreflightParity:
    def test_both_folds_parity_flag_on_vs_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sizes = {
            ("NSE-NIFTY", "1minute"): 375,
            ("NSE-NIFTY", "5minute"): 0,
            ("BSE-SENSEX", "1minute"): 7,
        }
        symbols = ["NSE-NIFTY", "BSE-SENSEX"]
        intervals = ["1minute", "5minute"]
        verdicts = {
            ("Z", "5minute"): "stale",
            ("A", "1day"): "fresh",
            ("A", "5minute"): "weird",
        }
        monkeypatch.delenv(SWEEP_OPS_RUST_ENV, raising=False)
        reset_sweep_ops_rust_for_tests()
        off_missing = preflight_slices(sizes, symbols=symbols, intervals=intervals)
        off_fresh = preflight_derived_freshness(verdicts)
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "1")
        reset_sweep_ops_rust_for_tests()
        assert preflight_slices(sizes, symbols=symbols, intervals=intervals) == off_missing
        assert preflight_derived_freshness(verdicts) == off_fresh
        assert sweep_ops_rust_disabled() is False


class TestLivenessParity:
    def test_progress_verdict_parity_flag_on_vs_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {"running": True, "wall_time_utc": 1000.0, "pid": 4242}

        def verdicts() -> list[RunVerdict]:
            return [
                progress_verdict(payload, now_wall=1100.0, pid_alive=lambda pid: True),
                progress_verdict(payload, now_wall=1601.0, pid_alive=lambda pid: True),
                progress_verdict(payload, now_wall=1100.0, pid_alive=lambda pid: False),
                progress_verdict({}, now_wall=1100.0),  # legacy → UNKNOWN (pre-tail)
            ]

        monkeypatch.delenv(SWEEP_OPS_RUST_ENV, raising=False)
        reset_sweep_ops_rust_for_tests()
        off = verdicts()
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "1")
        reset_sweep_ops_rust_for_tests()
        assert (
            verdicts()
            == off
            == [
                RunVerdict.RUNNING,
                RunVerdict.STALE,
                RunVerdict.NOT_RUNNING,
                RunVerdict.UNKNOWN,
            ]
        )
        assert sweep_ops_rust_disabled() is False


class TestSliceOneBBurnInSteadyState:
    def test_burn_in_completes_and_serves_rust(self) -> None:
        """>16 engaged calls per slice-1b law with zero autoreverts."""
        snap = _snapshot()
        sizes = {("NSE-NIFTY", "1minute"): 0}
        verdicts = {("A", "5minute"): "stale"}
        payload = {"running": True, "wall_time_utc": 1000.0, "pid": 4242}
        for _ in range(20):
            snap.to_dict()  # 6 engaged derived-cluster calls per poll
            check_sweep_sla(100, acknowledge=False, env={})
            plan_rank_memory(
                n_masks=1000,
                top_k=5,
                digest_path=True,
                mode=BoundedRankMode.AUTO,
                budget_bytes=8 * 1024**2,
            )
            preflight_slices(sizes, symbols=["NSE-NIFTY"], intervals=["1minute"])
            preflight_derived_freshness(verdicts)
            progress_verdict(payload, now_wall=1100.0, pid_alive=lambda pid: True)
        assert sweep_ops_rust_disabled() is False
        # Steady state still value-identical to the oracle.
        assert snap.to_dict()["percent"] == 12.5
        assert preflight_slices(sizes, symbols=["NSE-NIFTY"], intervals=["1minute"]).missing == (
            ("NSE-NIFTY", "1minute"),
        )
