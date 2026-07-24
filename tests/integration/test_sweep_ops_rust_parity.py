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

import itertools
import json
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Final

import numpy as np
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
from brutex.engine.sweep_apriori import (  # noqa: E402
    _BUDGET_CHECK_STRIDE,
    _CONFLICT_PAIR_MASKS,
    _join_result_python,
    _mask_of,
    generate_apriori_candidates,
)
from brutex.engine.sweep_audit import (  # noqa: E402
    SweepAuditChainCorruption,
    SweepAuditWriter,
    _entry_hash_python,
    verify_chain,
)
from brutex.engine.sweep_checkpoint import (  # noqa: E402
    SweepCheckpointError,
    SweepCheckpointStore,
)
from brutex.engine.sweep_detach import (  # noqa: E402
    DetachedSweepHandle,
    probe_handle,
)
from brutex.engine.sweep_exit_status import (  # noqa: E402
    RunState,
    read_exit_status,
    write_exit_status,
)
from brutex.engine.sweep_heal import _mismatch_decision_python  # noqa: E402
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
    _U64_CEILING,
    SWEEP_OPS_RUST_ENV,
    reset_sweep_ops_rust_for_tests,
    routed_count_violations,
    routed_grid_tuple_count,
    routed_heal_mismatch,
    routed_recount_verdict,
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
from brutex.engine.sweep_quarantine import quarantine_tuple_trio  # noqa: E402
from brutex.engine.sweep_run import SweepAprioriBudgetError  # noqa: E402
from brutex.engine.sweep_run_core import (  # noqa: E402
    _diffcheck_budget_python,
    _record_stride_python,
    per_tuple_diffcheck_budget,
    tuple_record_stride,
)
from brutex.engine.sweep_scheduler import (  # noqa: E402
    IST_OFFSET_SECONDS,
    CheckpointView,
    Schedule,
    _next_fire_python,
    cycle_audit_payload,
    decide_resume,
    plan_cycle,
)
from brutex.engine.sweep_sla_guard import SlaDecision, check_sweep_sla  # noqa: E402
from brutex.engine.sweep_stall_policy import (  # noqa: E402
    StallPolicy,
    resolve_stall_policy,
)
from brutex.engine.sweep_verify import _recount_verdict_python  # noqa: E402
from brutex.observability.chain_map import LINKS, build_chain_map  # noqa: E402
from brutex.observability.run_tripwires import (  # noqa: E402
    TripwireConfig,
    TripwireMonitor,
    _count_violations_python,
)
from brutex.observability.stage_stamp import (  # noqa: E402
    SNAPSHOT_EXTRA_KEY,
    StampVerdict,
    validate_stage_stamp,
    write_stage_stamp,
)
from brutex.ranking import CvarGateConfig, OverfitGateConfig  # noqa: E402
from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

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
    """A minimal structural `_GateConfigInput` (frozen-attribute style).

    Carries `max_sl_hit_rate` (default None — the real grid's default) so
    the fake tracks the REAL `SweepGridConfig` shape after the
    DEC-METRICS-KELLY-GROWTH-001 identity-axis extension (the train-b
    tier-2 seam: a stale fake without the attribute AttributeError'd the
    extended Python law inside the burn-in oracle callback); `cvar_gate`
    (default None — gate OFF) tracks the DEC-RANK-CVAR-TAIL-GATE-001
    identity-fragment extension the same way.
    """

    def __init__(
        self,
        *,
        overfit: OverfitGateConfig | None = None,
        max_sl_hit_rate: float | None = None,
        cvar_gate: CvarGateConfig | None = None,
        stage3_enable: bool = False,
        stage3_top_k: int = 10,
        stage3_max_fevals: int = 200,
        stage3_max_restarts: int = 3,
    ) -> None:
        self._overfit = overfit
        self._max_sl_hit_rate = max_sl_hit_rate
        self._cvar_gate = cvar_gate
        self._stage3_enable = stage3_enable
        self._stage3_top_k = stage3_top_k
        self._stage3_max_fevals = stage3_max_fevals
        self._stage3_max_restarts = stage3_max_restarts

    @property
    def overfit(self) -> OverfitGateConfig | None:
        return self._overfit

    @property
    def max_sl_hit_rate(self) -> float | None:
        return self._max_sl_hit_rate

    @property
    def cvar_gate(self) -> CvarGateConfig | None:
        return self._cvar_gate

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


class TestGateFragmentBeyondCrateRouting:
    """The train-b tier-2 seam routing gate (#1699 knob × #1665 facade).

    A SET ``max_sl_hit_rate`` (DEC-METRICS-KELLY-GROWTH-001) is OUTSIDE the
    crate fragment law's domain — the crate predates the field — so the
    WHOLE computation routes to the Python oracle (warn-once
    ``gate_fragment_beyond_crate``): never a burn-in autorevert, never a
    post-burn-in silent key drop. The unset-knob routing is UNCHANGED.
    """

    def test_set_knob_routes_to_oracle_crate_never_called(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[object, ...]] = []
        real = brutex_sweep_ops.gate_config_fragment

        def _spy(*args: object) -> object:
            calls.append(args)
            return real(*args)

        monkeypatch.setattr(brutex_sweep_ops, "gate_config_fragment", _spy)
        grid = _FragmentGrid(
            overfit=OverfitGateConfig(dsr_min=0.95),
            max_sl_hit_rate=0.35,
            stage3_enable=True,
        )
        with capture_logs() as logs:
            result = _gate_config_fragment(grid)
        assert result == _gate_config_fragment_python(grid)
        assert result is not None
        assert result["max_sl_hit_rate"] == 0.35  # the identity key survives
        assert calls == []  # the crate fragment fn is NEVER consulted
        assert sweep_ops_rust_disabled() is False  # a refusal, never an autorevert
        warns = [e for e in logs if e.get("reason") == "gate_fragment_beyond_crate"]
        assert len(warns) == 1
        assert warns[0]["leg"] == "max_sl_hit_rate"

    def test_unset_knob_still_routes_to_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[object, ...]] = []
        real = brutex_sweep_ops.gate_config_fragment

        def _spy(*args: object) -> object:
            calls.append(args)
            return real(*args)

        monkeypatch.setattr(brutex_sweep_ops, "gate_config_fragment", _spy)
        grid = _FragmentGrid(stage3_enable=True)
        assert _gate_config_fragment(grid) == _gate_config_fragment_python(grid)
        assert len(calls) == 1  # the crate path engages exactly as before the gate
        assert sweep_ops_rust_disabled() is False

    def test_post_burn_in_set_knob_keeps_identity_key(self) -> None:
        """The latent divergence the gate kills: post-burn-in (remaining=0 —
        no oracle in the crate path) a SET knob must STILL carry the
        ``max_sl_hit_rate`` identity key; pre-gate the crate fragment
        silently dropped it (a run-identity-class divergence)."""
        reset_sweep_ops_rust_for_tests(remaining=0)
        fragment = _gate_config_fragment(_FragmentGrid(max_sl_hit_rate=0.25))
        assert fragment == {"max_sl_hit_rate": 0.25}
        assert sweep_ops_rust_disabled() is False

    def test_flag_off_set_knob_byte_identical_and_crate_free(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _bomb(*args: object) -> object:
            raise AssertionError("flag OFF must never consult the crate")

        monkeypatch.setattr(brutex_sweep_ops, "gate_config_fragment", _bomb)
        # Explicit "0" (kill switch), NOT delenv-to-unset — under
        # DEC-RUST-DEFAULT-ON-WAVE-001 an unset flag resolves default-ON, which
        # would route to the bombed crate. "0" pins the genuine OFF/oracle arm.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
        reset_sweep_ops_rust_for_tests()
        grid = _FragmentGrid(overfit=OverfitGateConfig(dsr_min=0.95), max_sl_hit_rate=0.4)
        assert _gate_config_fragment(grid) == _gate_config_fragment_python(grid)


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
        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # this "off" arm run Rust too — a vacuous Rust-vs-Rust compare. "0"
        # is the kill switch: the genuine Python-oracle arm parity needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
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
        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # this "off" arm run Rust too — a vacuous Rust-vs-Rust compare. "0"
        # is the kill switch: the genuine Python-oracle arm parity needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
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
        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # this "off" arm run Rust too — a vacuous Rust-vs-Rust compare. "0"
        # is the kill switch: the genuine Python-oracle arm parity needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
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
        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # this "off" arm run Rust too — a vacuous Rust-vs-Rust compare. "0"
        # is the kill switch: the genuine Python-oracle arm parity needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
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

        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # this "off" arm run Rust too — a vacuous Rust-vs-Rust compare. "0"
        # is the kill switch: the genuine Python-oracle arm parity needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
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


# ────────────────── slice 2 — the audit-chain family ──────────────────

#: The cross-language golden literals — the SAME hex strings are pinned in
#: ``rust/brutex-sweep-ops/src/audit_chain.rs``'s cargo tests (the
#: shared-literal pin pattern: pip blake3 here, the ``blake3`` crate there).
_GENESIS: Final[str] = "0" * 64
_G1: Final[str] = "ab1df54e0d8428288ece95f9b5e462f4af17beb3046ea429c6ee86d67e33ff2e"
_G2: Final[str] = "4966787250e36bea06d71232fdb6e1087ed6b1757f1fa454152b7be989e087a7"
_G3: Final[str] = "7fb88619e479fc3cf60860936a745c9a3ec67e3aef6bcbc1d57ff410202563b8"

_G1_FIELDS: Final[tuple[str, ...]] = (
    "0",
    _GENESIS,
    "1700000000.5",
    "sweep",
    "SWEEP_START",
    "sweep:abc",
    '{"total_combos":22100}',
)
_G2_FIELDS: Final[tuple[str, ...]] = (
    "1",
    _G1,
    "1700000001.25",
    "sweep",
    "COMBO_DONE",
    "combo:5e",
    '{"mask":7,"trades":42}',
)
_G3_FIELDS: Final[tuple[str, ...]] = (
    "7",
    "f" * 64,
    "123.5",
    "aktör",
    "ACT",
    "subj-ß",
    '{"k":"v–"}',
)


class TestSliceTwoGoldenLiterals:
    @pytest.mark.parametrize(
        ("fields", "expected"),
        [(_G1_FIELDS, _G1), (_G2_FIELDS, _G2), (_G3_FIELDS, _G3)],
        ids=["genesis-entry", "chained-entry", "non-ascii-utf8"],
    )
    def test_crate_and_pip_blake3_agree_on_goldens(
        self, fields: tuple[str, ...], expected: str
    ) -> None:
        """The compiled crate AND the pip-blake3 oracle reproduce the SAME
        frozen hex literals the cargo suite pins (cross-language byte pin)."""
        assert brutex_sweep_ops.audit_entry_hash(*fields) == expected
        assert _entry_hash_python(*fields) == expected

    def test_chain_links_twin(self) -> None:
        """The crate's CHAIN_LINKS is the chain_map LINKS tuple (drift pin)."""
        assert tuple(brutex_sweep_ops.CHAIN_LINKS) == LINKS

    @given(
        seq=st.integers(min_value=-(2**31), max_value=2**63),
        prev=st.text(max_size=80),
        actor=st.text(max_size=24),
        action=st.text(max_size=24),
        subject=st.text(max_size=48),
        payload_json=st.text(max_size=64),
        ts=st.floats(allow_nan=False, allow_infinity=False),
    )
    def test_hypothesis_differential_vs_pip_blake3(
        self,
        seq: int,
        prev: str,
        actor: str,
        action: str,
        subject: str,
        payload_json: str,
        ts: float,
    ) -> None:
        """crate blake3 ≡ pip blake3 over arbitrary pre-rendered UTF-8 fields
        (incl. non-ASCII — st.text() spans the non-surrogate plane)."""
        fields = (str(seq), prev, str(ts), actor, action, subject, payload_json)
        assert brutex_sweep_ops.audit_entry_hash(*fields) == _entry_hash_python(*fields)


def _write_chain(path: Path) -> None:
    """Write a small deterministic REAL chain via the production writer."""
    with SweepAuditWriter.open(path) as log:
        log.append(
            action="SWEEP_START",
            subject="sweep:abc",
            payload={"total_combos": 22100},
            ts_utc=1700000000.5,
        )
        log.append(
            action="COMBO_DONE",
            subject="combo:5e",
            payload={"mask": 7, "trades": 42},
            ts_utc=1700000001.25,
        )
        log.append(
            action="SWEEP_COMPLETE",
            subject="sweep:abc",
            payload={"note": "done–ß"},
            ts_utc=1700000002.75,
        )


class TestAuditChainParity:
    def test_flag_on_chain_bytes_identical_to_flag_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A chain written flag-ON is BYTE-identical to the flag-OFF chain
        (the load-bearing byte-identity law — Python renders, Rust hashes)."""
        on_log = tmp_path / "on.jsonl"
        _write_chain(on_log)
        assert sweep_ops_rust_disabled() is False
        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # the "OFF" arm run Rust too (vacuous Rust-vs-Rust). "0" is the kill
        # switch — the genuine Python-oracle arm the parity compare needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
        reset_sweep_ops_rust_for_tests()
        off_log = tmp_path / "off.jsonl"
        _write_chain(off_log)
        assert on_log.read_bytes() == off_log.read_bytes()

    def test_verify_chain_green_flag_on(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.jsonl"
        _write_chain(log)
        assert verify_chain(log) == 3
        assert sweep_ops_rust_disabled() is False

    @pytest.mark.parametrize(
        ("tamper", "reason_fragment"),
        [
            (lambda text: text.replace('"trades":42', '"trades":43'), "content tampered"),
            (
                lambda text: (
                    "\n".join([text.splitlines()[0], text.splitlines()[2], text.splitlines()[1]])
                    + "\n"
                ),
                "seq number out of order",
            ),
            (
                # Relink entry 1's prev_hash (its SECOND-line _G1 occurrence) —
                # the linkage check fires before the content recompute.
                lambda text: (
                    "\n".join(
                        line.replace(_G1, "e" * 64, 1) if index == 1 else line
                        for index, line in enumerate(text.splitlines())
                    )
                    + "\n"
                ),
                "prev_hash does not match",
            ),
        ],
        ids=["payload-byte-flip", "reordered-lines", "prev-hash-relink"],
    )
    def test_tamper_breaks_chain_with_identical_message_both_arms(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        tamper: Callable[[str], str],
        reason_fragment: str,
    ) -> None:
        """Every tamper shape still BREAKS the chain flag-ON, with the SAME
        exception type + message BYTES as the flag-OFF oracle arm."""
        log = tmp_path / "audit.jsonl"
        _write_chain(log)
        log.write_text(tamper(log.read_text(encoding="utf-8")), encoding="utf-8")

        with pytest.raises(SweepAuditChainCorruption) as on_exc:
            verify_chain(log)
        assert sweep_ops_rust_disabled() is False

        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # the "OFF" arm run Rust too (vacuous Rust-vs-Rust). "0" is the kill
        # switch — the genuine Python-oracle arm the parity compare needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
        reset_sweep_ops_rust_for_tests()
        with pytest.raises(SweepAuditChainCorruption) as off_exc:
            verify_chain(log)

        assert str(on_exc.value) == str(off_exc.value)
        assert on_exc.value.seq == off_exc.value.seq
        assert reason_fragment in str(on_exc.value)


class TestStampVerdictParity:
    def _stamped(self, tmp_path: Path, *, snapshot: str | None = None) -> Path:
        tmp_path.mkdir(parents=True, exist_ok=True)
        artifact = tmp_path / "artifact.bin"
        artifact.write_bytes(b"artifact-bytes")
        extra = None if snapshot is None else {SNAPSHOT_EXTRA_KEY: snapshot}
        write_stage_stamp(artifact, stage="sweep", run_id=_RUN_ID, extra=extra)
        return artifact

    def test_verdict_matrix_parity_flag_on_vs_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ok = self._stamped(tmp_path / "ok", snapshot="snap-a")
        corrupt = self._stamped(tmp_path / "corrupt")
        corrupt.write_bytes(b"tampered-bytes")
        wrong_stage = self._stamped(tmp_path / "stage")
        wrong_run = self._stamped(tmp_path / "run")
        snap = self._stamped(tmp_path / "snap", snapshot="snap-a")

        def verdicts() -> list[StampVerdict]:
            return [
                validate_stage_stamp(
                    ok,
                    expected_stage="sweep",
                    expected_run_id=_RUN_ID,
                    expected_snapshot_hash="snap-a",
                )[0],
                validate_stage_stamp(corrupt, expected_stage="sweep")[0],
                validate_stage_stamp(wrong_stage, expected_stage="stage3_cell")[0],
                validate_stage_stamp(wrong_run, expected_stage="sweep", expected_run_id="other")[0],
                validate_stage_stamp(snap, expected_stage="sweep", expected_snapshot_hash="snap-b")[
                    0
                ],
            ]

        on = verdicts()
        assert sweep_ops_rust_disabled() is False
        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # the "OFF" arm run Rust too (vacuous Rust-vs-Rust). "0" is the kill
        # switch — the genuine Python-oracle arm the parity compare needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
        reset_sweep_ops_rust_for_tests()
        off = verdicts()
        assert on == off
        assert on == [
            StampVerdict.OK,
            StampVerdict.HASH_MISMATCH,
            StampVerdict.HASH_MISMATCH,
            StampVerdict.HASH_MISMATCH,
            StampVerdict.HASH_MISMATCH,
        ]


class TestChainLinkStateParity:
    _SCENARIOS: Final[list[dict[str, object]]] = [
        {  # running mid-stage: before/at/after split + a validated stamp
            "progress": {"running": True, "stage": "stage3", "run_id": "r1"},
            "stamps": {"preflight": StampVerdict.OK},
            "suspect": False,
            "exit_status": None,
        },
        {  # running, stage unknown → amber everywhere undecided
            "progress": {"running": True},
            "stamps": {},
            "suspect": False,
            "exit_status": None,
        },
        {  # red stamp evidence wins over a complete terminal
            "progress": None,
            "stamps": {"sweep": StampVerdict.HASH_MISMATCH},
            "suspect": False,
            "exit_status": {"state": "complete", "run_id": "r2"},
        },
        {  # refusal with the EMPTY refusal_text (the falsy-law pin)
            "progress": None,
            "stamps": {},
            "suspect": False,
            "exit_status": {"state": "refused", "refusal_text": ""},
        },
        {  # failed at a known stage — error_type absent → "unknown error"
            "progress": {"running": False, "stage": "cascade"},
            "stamps": {},
            "suspect": False,
            "exit_status": {"state": "halted"},
        },
        {  # failed, stage unknown → amber
            "progress": None,
            "stamps": {},
            "suspect": False,
            "exit_status": {"state": "failed", "error_type": "OSError"},
        },
        {  # suspect amber floor over a complete run
            "progress": None,
            "stamps": {"artifacts": StampVerdict.OK},
            "suspect": True,
            "exit_status": {"state": "complete"},
        },
        {  # no evidence at all → pending everywhere
            "progress": None,
            "stamps": {},
            "suspect": False,
            "exit_status": None,
        },
    ]

    def test_chain_map_parity_flag_on_vs_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def maps() -> list[dict[str, object]]:
            return [
                build_chain_map(
                    progress=case["progress"],  # type: ignore[arg-type]
                    stamps=case["stamps"],  # type: ignore[arg-type]
                    suspect=case["suspect"],  # type: ignore[arg-type]
                    exit_status=case["exit_status"],  # type: ignore[arg-type]
                )
                for case in self._SCENARIOS
            ]

        on = maps()
        assert sweep_ops_rust_disabled() is False
        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # the "OFF" arm run Rust too (vacuous Rust-vs-Rust). "0" is the kill
        # switch — the genuine Python-oracle arm the parity compare needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
        reset_sweep_ops_rust_for_tests()
        assert maps() == on


class TestCountViolationsParity:
    @pytest.mark.parametrize(
        "case",
        [
            (100, 100, 50, 10, 5, 0, None, False),  # clean
            (100, 90, 50, 60, 5, -1, None, False),  # negative in-flight + 2 more
            (100, 100, 50, 10, 60, 60, 55, False),  # survivors/backwards/overrun
            (100, 100, 90, 0, 0, 0, None, True),  # done but short
            (100, 100, 100, 0, 0, 0, 100, True),  # clean done
        ],
        ids=["clean", "neg-in-flight", "triple", "done-short", "clean-done"],
    )
    def test_crate_matches_python_law(
        self, case: tuple[int, int, int, int, int, int, int | None, bool]
    ) -> None:
        assert brutex_sweep_ops.count_reconciliation(*case) == _count_violations_python(*case)

    def test_real_monitor_fires_identically_both_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = TripwireConfig(
            throughput_floor_fraction=0.0,
            rss_budget_fraction=0.0,
            memory_slope_bytes_per_tuple=0,
        )

        def fired_details() -> tuple[bool, list[str]]:
            monitor = TripwireMonitor(
                config=config,
                enumerated_total=90,
                on_trip=lambda event: None,
                ram_total_bytes=0,
            )
            monitor.on_progress(
                _snapshot(enumerated=100, evaluated=150, survivors=200), in_flight=0
            )
            return (monitor.suspect, [event.detail for event in monitor.events])

        on = fired_details()
        assert sweep_ops_rust_disabled() is False
        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # the "OFF" arm run Rust too (vacuous Rust-vs-Rust). "0" is the kill
        # switch — the genuine Python-oracle arm the parity compare needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
        reset_sweep_ops_rust_for_tests()
        off = fired_details()
        assert on == off
        assert on[0] is True
        assert "enumerated 100 != armed total 90" in on[1][0]


class TestSliceTwoBurnInSteadyState:
    def test_burn_in_completes_and_serves_rust(self, tmp_path: Path) -> None:
        """>16 engaged calls per slice-2 law with zero autoreverts."""
        log = tmp_path / "audit.jsonl"
        with SweepAuditWriter.open(log) as writer:
            for i in range(18):  # 18 engaged entry-hash calls
                writer.append(
                    action="COMBO_DONE",
                    subject=f"combo:{i}",
                    payload={"i": i},
                    ts_utc=1700000000.0 + i,
                )
        assert verify_chain(log) == 18  # 18 engaged verify-step calls

        artifact = tmp_path / "artifact.bin"
        artifact.write_bytes(b"payload")
        write_stage_stamp(artifact, stage="sweep", run_id=_RUN_ID)
        for _ in range(18):  # 18 engaged stamp-verdict calls
            verdict, _stamp = validate_stage_stamp(artifact, expected_stage="sweep")
            assert verdict is StampVerdict.OK

        for _ in range(4):  # 4 × 5 links = 20 engaged link-state calls
            build_chain_map(
                progress=None,
                stamps={},
                suspect=False,
                exit_status={"state": "complete"},
            )

        law_args = (100, 100, 50, 10, 5, 0, None, False)
        for _ in range(18):  # 18 engaged count-violation folds
            detail = routed_count_violations(
                *law_args, python_law=lambda: _count_violations_python(*law_args)
            )
            assert detail == ""

        assert sweep_ops_rust_disabled() is False
        # Steady state still value-identical to the oracle.
        assert verify_chain(log) == 18


# ────────────────── slice 3 — the durable-artifacts family ──────────────────


class TestSliceThreeCrateDriftPins:
    def test_run_state_tokens_twin(self) -> None:
        """The crate's RUN_STATE_TOKENS is the RunState wire vocabulary."""
        assert set(brutex_sweep_ops.RUN_STATE_TOKENS) == {s.value for s in RunState}

    def test_resolution_float_bits_pass_through(self) -> None:
        one_ulp = 1.0 + 2.0**-52
        started, ended, suspect = brutex_sweep_ops.exit_status_resolution(
            "halted", None, one_ulp, None, None
        )
        assert started == one_ulp
        assert ended == one_ulp
        assert suspect is False


class TestExitStatusParity:
    def _write_lifecycle(self, root: Path) -> list[bytes]:
        """RUNNING → mid-run RUNNING rewrite → SUSPECT_COMPLETE, fixed clocks."""
        run_id = "cafe" * 16
        snapshots: list[bytes] = []
        write_exit_status(
            root, run_id=run_id, state=RunState.RUNNING, attempt=1, pid=42, clock=lambda: 100.5
        )
        snapshots.append((root / "runs" / run_id / "exit-status.json").read_bytes())
        # The #1712 round-3 law: a mid-run RUNNING write must preserve the start.
        write_exit_status(
            root,
            run_id=run_id,
            state=RunState.RUNNING,
            attempt=1,
            pid=42,
            suspect=True,
            clock=lambda: 150.25,
        )
        snapshots.append((root / "runs" / run_id / "exit-status.json").read_bytes())
        write_exit_status(
            root,
            run_id=run_id,
            state=RunState.SUSPECT_COMPLETE,
            exit_code=0,
            attempt=1,
            pid=42,
            clock=lambda: 200.75,
        )
        snapshots.append((root / "runs" / run_id / "exit-status.json").read_bytes())
        return snapshots

    def test_flag_on_artifacts_byte_identical_to_flag_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        on = self._write_lifecycle(tmp_path / "on")
        assert sweep_ops_rust_disabled() is False
        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # the "OFF" arm run Rust too (vacuous Rust-vs-Rust). "0" is the kill
        # switch — the genuine Python-oracle arm the parity compare needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
        reset_sweep_ops_rust_for_tests()
        off = self._write_lifecycle(tmp_path / "off")
        assert on == off

    def test_flag_on_preserves_start_and_forces_suspect(self, tmp_path: Path) -> None:
        self._write_lifecycle(tmp_path)
        doc = read_exit_status(tmp_path, "cafe" * 16)
        assert doc is not None
        assert doc["wall_started_utc"] == 100.5  # preserved across all three writes
        assert doc["wall_ended_utc"] == 200.75
        assert doc["suspect"] is True  # SUSPECT_COMPLETE forces True
        assert sweep_ops_rust_disabled() is False


class TestQuarantineOrdinalParity:
    def _quarantine_twice(self, root: Path) -> list[str]:
        store_dir = root / "sweeps"
        store_dir.mkdir(parents=True, exist_ok=True)
        dests: list[str] = []
        for _ in range(2):
            store = store_dir / "tuple.parquet"
            store.write_bytes(b"corrupt")
            outcome = quarantine_tuple_trio(
                store, results_root=root, reason="parity", run_id="runx"
            )
            dests.append(outcome.dest.name)
        return dests

    def test_flag_on_dirnames_match_flag_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        on = self._quarantine_twice(tmp_path / "on")
        assert on == ["runx-1", "runx-2"]  # monotonic ordinals via the crate fold
        assert sweep_ops_rust_disabled() is False
        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # the "OFF" arm run Rust too (vacuous Rust-vs-Rust). "0" is the kill
        # switch — the genuine Python-oracle arm the parity compare needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
        reset_sweep_ops_rust_for_tests()
        assert self._quarantine_twice(tmp_path / "off") == on

    def test_unicode_dirname_degrades_to_oracle_same_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A planted NON-ASCII quarantine dirname refuses to the oracle — the
        served ordinal is the ORACLE's (Python int() reads the Unicode digits)
        on BOTH arms."""
        parent = tmp_path / ".quarantine" / "runtime"
        parent.mkdir(parents=True)
        (parent / "runx-٢").mkdir()  # Arabic-Indic TWO — int("٢") == 2 in Python
        (tmp_path / "sweeps").mkdir()
        store = tmp_path / "sweeps" / "tuple.parquet"
        store.write_bytes(b"corrupt")
        on = quarantine_tuple_trio(
            store, results_root=tmp_path, reason="parity", run_id="runx"
        ).dest.name
        assert on == "runx-3"  # max(2) + 1 — the oracle's Unicode-digit answer
        assert sweep_ops_rust_disabled() is False  # degrade, never a latch


class TestCheckpointParity:
    def _drive_store(self, path: Path) -> list[object]:
        outcomes: list[object] = []
        store = SweepCheckpointStore(path=path, sweep_id="s1", total_combos=10, clock=lambda: 5.0)
        outcomes.append(store.record(0))  # advance
        outcomes.append(store.record(3))  # advance
        outcomes.append(store.record(2))  # noop — the never-rewind law
        with pytest.raises(SweepCheckpointError) as below:
            store.record(-1)
        outcomes.append(str(below.value))
        with pytest.raises(SweepCheckpointError) as beyond:
            store.record(10)
        outcomes.append(str(beyond.value))
        outcomes.append(path.read_bytes())
        # Adoption arms: same-id adopt, foreign None, tampered-total refusal.
        adopt = SweepCheckpointStore(path=path, sweep_id="s1", total_combos=10)
        loaded = adopt.load()
        assert loaded is not None
        outcomes.append(loaded.last_completed_index)
        foreign = SweepCheckpointStore(path=path, sweep_id="other", total_combos=10)
        outcomes.append(foreign.load())
        tampered = SweepCheckpointStore(path=path, sweep_id="s1", total_combos=99)
        with pytest.raises(SweepCheckpointError) as mismatch:
            tampered.load()
        outcomes.append(str(mismatch.value))
        return outcomes

    def test_flag_on_store_behavior_identical_to_flag_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        on = self._drive_store(tmp_path / "on.json")
        assert on[:3] == [True, True, False]
        assert sweep_ops_rust_disabled() is False
        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # the "OFF" arm run Rust too (vacuous Rust-vs-Rust). "0" is the kill
        # switch — the genuine Python-oracle arm the parity compare needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
        reset_sweep_ops_rust_for_tests()
        off = self._drive_store(tmp_path / "off.json")
        # Byte-equal checkpoints + identical outcomes INCLUDING the refusal
        # message bytes (path differs by arm — normalise it out).
        assert [str(x).replace("on.json", "X").replace("off.json", "X") for x in on] == [
            str(x).replace("on.json", "X").replace("off.json", "X") for x in off
        ]


class TestSliceThreeBurnInSteadyState:
    def test_burn_in_completes_and_serves_rust(self, tmp_path: Path) -> None:
        """>16 engaged calls per slice-3 law with zero autoreverts."""
        run_id = "beef" * 16
        for i in range(18):  # 18 engaged resolution calls
            write_exit_status(
                tmp_path,
                run_id=run_id,
                state=RunState.RUNNING,
                attempt=1,
                pid=42,
                clock=lambda i=i: 100.0 + i,
            )
        doc = read_exit_status(tmp_path, run_id)
        assert doc is not None and doc["wall_started_utc"] == 100.0

        store_path = tmp_path / "checkpoint.json"
        store = SweepCheckpointStore(path=store_path, sweep_id="s1", total_combos=100)
        for i in range(18):  # 18 engaged record decisions
            assert store.record(i) is True
        for _ in range(18):  # 18 engaged adoption verdicts
            reloaded = SweepCheckpointStore(path=store_path, sweep_id="s1", total_combos=100)
            checkpoint = reloaded.load()
            assert checkpoint is not None and checkpoint.last_completed_index == 17

        sweeps = tmp_path / "sweeps"
        sweeps.mkdir()
        for i in range(18):  # 18 engaged ordinal folds
            store_file = sweeps / "tuple.parquet"
            store_file.write_bytes(b"corrupt")
            outcome = quarantine_tuple_trio(
                store_file, results_root=tmp_path, reason="burn-in", run_id="runx"
            )
            assert outcome.dest.name == f"runx-{i + 1}"

        assert sweep_ops_rust_disabled() is False


# ─────────────────── slice 4 — the process/lock-ops family ───────────────────


def _write_probe_progress(root: Path, payload: dict[str, object]) -> None:
    path = root / "progress" / "sweep-progress.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestDetachProbeParity:
    def _drive(self, root: Path) -> list[RunVerdict]:
        handle = DetachedSweepHandle(pid=4321, run_id_hint=None, results_root=root, cmdline=("x",))
        now = 1_000_000.0
        verdicts = [
            probe_handle(handle, now_wall=now, pid_alive=lambda pid: False),
            probe_handle(handle, now_wall=now, pid_alive=lambda pid: True),
        ]
        _write_probe_progress(root, {"running": True, "wall_time_utc": now, "pid": 4321})
        verdicts.append(probe_handle(handle, now_wall=now, pid_alive=lambda pid: True))
        _write_probe_progress(root, {"running": True, "wall_time_utc": now, "pid": 9999})
        verdicts.append(probe_handle(handle, now_wall=now, pid_alive=lambda pid: True))
        return verdicts

    def test_probe_parity_flag_on_vs_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # this "off" arm run Rust too — a vacuous Rust-vs-Rust compare. "0"
        # is the kill switch: the genuine Python-oracle arm parity needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
        reset_sweep_ops_rust_for_tests()
        off = self._drive(tmp_path / "off")
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "1")
        reset_sweep_ops_rust_for_tests()
        on = self._drive(tmp_path / "on")
        assert on == off
        assert on == [
            RunVerdict.NOT_RUNNING,
            RunVerdict.UNKNOWN,
            RunVerdict.RUNNING,
            RunVerdict.UNKNOWN,
        ]
        assert sweep_ops_rust_disabled() is False


class TestHealMismatchParity:
    """The crate's mismatch tail ≡ the retained sweep_heal oracle.

    The whole-surface ``assess_heal`` parity (real trees, refusal bytes)
    lives in the stem suite's flag-driven ``TestSweepOpsRouting`` class —
    here the REAL wheel's decision fold is driven differentially.
    """

    @given(
        src=st.lists(
            st.one_of(st.none(), st.sampled_from(["a" * 64, "b" * 64, "f" * 64])),
            min_size=3,
            max_size=3,
        ),
        plan=st.lists(
            st.one_of(st.none(), st.sampled_from(["a" * 64, "b" * 64, "f" * 64])),
            min_size=3,
            max_size=3,
        ),
        dest=st.lists(
            st.one_of(st.none(), st.sampled_from(["a" * 64, "b" * 64, "f" * 64])),
            min_size=3,
            max_size=3,
        ),
    )
    def test_hypothesis_differential_vs_python_law(
        self,
        src: list[str | None],
        plan: list[str | None],
        dest: list[str | None],
    ) -> None:
        assert brutex_sweep_ops.heal_mismatch_decision(
            src, plan, dest
        ) == _mismatch_decision_python(src, plan, dest)

    def test_routed_wrapper_serves_the_crate_value(self) -> None:
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> str:
            raise AssertionError("steady state must not consult the oracle")

        assert (
            routed_heal_mismatch(
                [None, None, None],
                ["a" * 64, "b" * 64, None],
                ["a" * 64, "b" * 64, None],
                python_law=forbidden_oracle,
            )
            == "already_quarantined"
        )


class TestSchedulerParity:
    @staticmethod
    def _drive() -> list[object]:
        interval = Schedule.parse("30m")
        daily = Schedule.parse("daily@02:30", tz_offset_seconds=IST_OFFSET_SECONDS)
        now = 1_728_000_000.0
        outcomes: list[object] = []
        for schedule in (interval, daily):
            fire = schedule.next_fire_after(now)
            refire = schedule.next_fire_after(float(fire), last_fire=float(fire))
            outcomes.append((fire, type(fire).__name__, refire, type(refire).__name__))
            plan = plan_cycle(
                now=now,
                schedule=schedule,
                checkpoint_view=CheckpointView(
                    exists=True, sweep_id_matches=True, next_index=3, total_combos=10
                ),
            )
            outcomes.append(
                json.dumps(
                    cycle_audit_payload(
                        cycle_index=0,
                        fire_at=plan.fire_at,
                        schedule=schedule,
                        resume=plan.resume,
                        once=False,
                    ),
                    sort_keys=True,
                )
            )
        views = [
            CheckpointView(exists=False, sweep_id_matches=False, next_index=0, total_combos=0),
            CheckpointView(exists=True, sweep_id_matches=False, next_index=3, total_combos=10),
            CheckpointView(exists=True, sweep_id_matches=True, next_index=10, total_combos=10),
            CheckpointView(exists=True, sweep_id_matches=True, next_index=3, total_combos=10),
        ]
        outcomes.extend(decide_resume(view) for view in views)
        return outcomes

    def test_scheduler_parity_flag_on_vs_off_including_audit_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # this "off" arm run Rust too — a vacuous Rust-vs-Rust compare. "0"
        # is the kill switch: the genuine Python-oracle arm parity needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
        reset_sweep_ops_rust_for_tests()
        off = self._drive()
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "1")
        reset_sweep_ops_rust_for_tests()
        on = self._drive()
        assert on == off
        # The daily fire epoch is an INT on both arms — the json.dumps audit
        # payload renders int bytes, never a trailing ".0".
        daily_fire = on[2]
        assert isinstance(daily_fire, tuple)
        assert daily_fire[1] == "int" and daily_fire[3] == "int"
        assert sweep_ops_rust_disabled() is False

    @given(
        is_interval=st.booleans(),
        interval_seconds=st.integers(min_value=1, max_value=2**31),
        second_of_day=st.integers(min_value=0, max_value=86_399),
        tz_offset_seconds=st.integers(min_value=-86_400, max_value=86_400),
        now=st.floats(allow_nan=False, allow_infinity=False, min_value=0.0, max_value=4e9),
        last_fire=st.one_of(
            st.none(),
            st.floats(allow_nan=False, allow_infinity=False, min_value=0.0, max_value=4e9),
        ),
    )
    def test_hypothesis_differential_vs_python_law(
        self,
        is_interval: bool,
        interval_seconds: int,
        second_of_day: int,
        tz_offset_seconds: int,
        now: float,
        last_fire: float | None,
    ) -> None:
        """The crate arithmetic ≡ the Python oracle over the gated domain —
        VALUE equality (the wrapper's output gate owns the daily int
        reconstruction; the oracle's daily arm returns exact-int values)."""
        rust = brutex_sweep_ops.schedule_next_fire(
            is_interval, interval_seconds, second_of_day, tz_offset_seconds, now, last_fire
        )
        assert rust == _next_fire_python(
            is_interval, interval_seconds, second_of_day, tz_offset_seconds, now, last_fire
        )


class TestSliceFourBurnInSteadyState:
    def test_eighteen_engaged_calls_never_autorevert(self, tmp_path: Path) -> None:
        reset_sweep_ops_rust_for_tests()
        root = tmp_path / "probe"
        handle = DetachedSweepHandle(pid=7, run_id_hint=None, results_root=root, cmdline=("x",))
        _write_probe_progress(root, {"running": True, "wall_time_utc": 50.0, "pid": 7})
        for _ in range(18):  # 18 engaged probe folds
            assert (
                probe_handle(handle, now_wall=50.0, pid_alive=lambda pid: True)
                is RunVerdict.RUNNING
            )
        schedule = Schedule.parse("daily@02:30", tz_offset_seconds=IST_OFFSET_SECONDS)
        for i in range(18):  # 18 engaged next-fire folds
            fire = schedule.next_fire_after(1_728_000_000.0 + i)
            assert type(fire) is int
        view = CheckpointView(exists=True, sweep_id_matches=True, next_index=3, total_combos=10)
        for _ in range(18):  # 18 engaged resume folds
            assert decide_resume(view).resume_from_index == 3
        for _ in range(18):  # 18 engaged heal mismatch folds
            assert (
                routed_heal_mismatch(
                    [None, "b" * 64, None],
                    ["a" * 64, "b" * 64, None],
                    [None, None, None],
                    python_law=lambda: "complete_partial",
                )
                == "complete_partial"
            )
        assert sweep_ops_rust_disabled() is False


# ─────────────────────────── slice 5: verify/apriori ───────────────────────────


class TestRecountVerdictParity:
    def test_exhaustive_thirty_two_combination_differential(self) -> None:
        """The crate ladder ≡ the Python oracle over the WHOLE 2^5 domain."""
        for flags in itertools.product((False, True), repeat=5):
            assert brutex_sweep_ops.recount_verdict(*flags) == _recount_verdict_python(*flags)

    def test_token_vocabulary_pin(self) -> None:
        assert set(brutex_sweep_ops.RECOUNT_VERDICT_TOKENS) == {
            "vix_divergence",
            "grid_corrupt",
            "grid_inconclusive",
            "cell_dependent",
            "cell_corrupt",
            "cell_inconclusive",
        }

    def test_routed_wrapper_serves_the_crate_value(self) -> None:
        reset_sweep_ops_rust_for_tests(remaining=0)

        def forbidden_oracle() -> str:
            raise AssertionError("steady state must not consult the oracle")

        assert (
            routed_recount_verdict(False, True, True, False, False, python_law=forbidden_oracle)
            == "grid_corrupt"
        )


class TestAprioriJoinParity:
    @given(
        k=st.integers(min_value=1, max_value=3),
        data=st.data(),
        drop_conflicts=st.booleans(),
    )
    def test_hypothesis_differential_vs_python_law(
        self, k: int, data: st.DataObject, drop_conflicts: bool
    ) -> None:
        """Crate join+prune ≡ ``_join_result_python`` over gated frontiers."""
        universe = data.draw(st.integers(min_value=k, max_value=9))
        pool = list(itertools.combinations(range(universe), k))
        subset = data.draw(st.sets(st.sampled_from(pool), min_size=1, max_size=min(len(pool), 12)))
        frontier = sorted(subset)
        masks = np.array(sorted(_mask_of(t) for t in frontier), dtype=np.uint64)
        # The sweep-ops crate's ``apriori_join_prune`` takes ``conflict_masks:
        # Vec<u64>`` — the 74-bit vocab (V1 #1726) grows conflict pairs on bits
        # 64-67, so ``_CONFLICT_PAIR_MASKS`` now holds masks >= 2**64 that
        # OverflowError at the raw FFI. The FACADE's ``routed_apriori_join_prune``
        # already guards this exact bound (``mask < _U64_CEILING`` degrades the
        # whole call to the Python oracle), so scope the RAW-crate differential
        # to the u64 domain the facade would ever forward. Decision-equivalent
        # for these frontiers (universe <= 9 ⇒ candidate masks carry only bits
        # 0-8, so a >=2**64 conflict pair can never fire either arm), and the
        # oracle rebuilds conflicts from the canonical ``has_conflict`` itself.
        u64_conflict_masks = [m for m in _CONFLICT_PAIR_MASKS if m < _U64_CEILING]
        raw = brutex_sweep_ops.apriori_join_prune(
            [list(t) for t in frontier],
            drop_conflicts,
            u64_conflict_masks,
            375,
            10**15,
            _BUDGET_CHECK_STRIDE,
        )
        expected = _join_result_python(
            frontier, masks, drop_conflicts=drop_conflicts, n_bars=375, limit=10**15
        )
        assert ([tuple(c) for c in raw[0]], raw[1]) == expected

    def test_strided_trip_hand_derived_pins(self) -> None:
        """The trip fires only at stride multiples, STRICT ``>``, count returned."""
        frontier = [[0], [1], [2]]  # join → (0,1), (0,2), (1,2)
        assert brutex_sweep_ops.apriori_join_prune(frontier, False, [], 10, 5, 1) == ([], 1)
        assert brutex_sweep_ops.apriori_join_prune(frontier, False, [], 10, 5, 2) == ([], 2)
        # At-limit is NOT over-limit (strict >): 3 candidates x 10 bars == 30 <= 30.
        assert brutex_sweep_ops.apriori_join_prune(frontier, False, [], 10, 30, 1) == (
            [[0, 1], [0, 2], [1, 2]],
            None,
        )

    def test_whole_generate_flag_on_vs_off_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``generate_apriori_candidates`` end-to-end ON ≡ OFF (masks + order)."""
        bar_bits = [0b111, 0b101, 0b011, 0b110, 0b001] * 3

        def _drive() -> list[list[int]]:
            return [
                generate_apriori_candidates(bar_bits, condition_bits=(0, 1, 2), k=k, min_hits=2)
                for k in (1, 2, 3)
            ]

        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # this "off" arm run Rust too — a vacuous Rust-vs-Rust compare. "0"
        # is the kill switch: the genuine Python-oracle arm parity needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
        reset_sweep_ops_rust_for_tests()
        off = _drive()
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "1")
        reset_sweep_ops_rust_for_tests()
        on = _drive()
        assert on == off
        assert sweep_ops_rust_disabled() is False

    def test_budget_trip_message_bytes_flag_on_vs_off_parity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``SweepAprioriBudgetError`` bytes are identical either arm."""
        bar_bits = [0b11111, 0b10101, 0b01111] * 5

        def _message() -> str:
            with pytest.raises(SweepAprioriBudgetError) as exc:
                generate_apriori_candidates(
                    bar_bits,
                    condition_bits=(0, 1, 2, 3, 4),
                    k=2,
                    min_hits=1,
                    max_mask_bars=1,
                    cell_label="parity-cell",
                )
            return str(exc.value)

        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # this "off" arm run Rust too — a vacuous Rust-vs-Rust compare. "0"
        # is the kill switch: the genuine Python-oracle arm parity needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
        reset_sweep_ops_rust_for_tests()
        off = _message()
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "1")
        reset_sweep_ops_rust_for_tests()
        on = _message()
        assert on == off
        assert "parity-cell" in on


class TestSliceFiveBurnInSteadyState:
    def test_eighteen_engaged_calls_never_autorevert(self) -> None:
        reset_sweep_ops_rust_for_tests()
        for _ in range(18):  # 18 engaged recount folds
            assert (
                routed_recount_verdict(
                    False, False, False, True, False, python_law=lambda: "cell_inconclusive"
                )
                == "cell_inconclusive"
            )
        bar_bits = [0b111, 0b110, 0b011] * 4
        for _ in range(18):  # 18 engaged join folds (k=2 routes one join level)
            masks = generate_apriori_candidates(bar_bits, condition_bits=(0, 1, 2), k=2, min_hits=1)
            assert masks  # non-empty: the level genuinely joined
        assert sweep_ops_rust_disabled() is False


# ─────────────────────── slice 6: run-core/orchestrator ───────────────────────


class TestRunCoreParity:
    @given(total=st.integers(min_value=-(2**52), max_value=2**52))
    def test_stride_hypothesis_differential_vs_python_law(self, total: int) -> None:
        """The crate's f64-mirrored stride ≡ the Python oracle over the band."""
        assert brutex_sweep_ops.tuple_record_stride(total) == _record_stride_python(total)

    @given(
        k=st.integers(min_value=-(2**52), max_value=2**52),
        total=st.integers(min_value=-(2**52), max_value=2**52),
    )
    def test_budget_hypothesis_differential_vs_python_law(self, k: int, total: int) -> None:
        assert brutex_sweep_ops.per_tuple_diffcheck_budget(k, total) == _diffcheck_budget_python(
            k, total
        )

    def test_wired_folds_flag_on_vs_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _drive() -> list[int]:
            strides = [tuple_record_stride(t) for t in (0, 10_001, 25_000, 100_000)]
            budgets = [per_tuple_diffcheck_budget(k, t) for k, t in ((0, 5), (7, 3), (200, 1))]
            return strides + budgets

        # Wave flip: unset resolves default-ON, so a bare delenv would make
        # this "off" arm run Rust too — a vacuous Rust-vs-Rust compare. "0"
        # is the kill switch: the genuine Python-oracle arm parity needs.
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "0")
        reset_sweep_ops_rust_for_tests()
        off = _drive()
        monkeypatch.setenv(SWEEP_OPS_RUST_ENV, "1")
        reset_sweep_ops_rust_for_tests()
        on = _drive()
        assert on == off == [1, 2, 3, 10, 0, 2, 200]
        assert sweep_ops_rust_disabled() is False

    def test_grid_count_parity_and_overflow_degrade(self) -> None:
        assert brutex_sweep_ops.grid_tuple_count([3, 21, 2, 6, 5, 5, 1, 3, 1]) == 56_700
        with pytest.raises(ValueError, match="exceeds u64"):
            brutex_sweep_ops.grid_tuple_count([2**63, 2])
        # The wrapper folds the crate ValueError back to the bigint oracle.
        reset_sweep_ops_rust_for_tests(remaining=0)
        assert routed_grid_tuple_count((2**63, 2), python_law=lambda: 2**64) == 2**64

    def test_eighteen_engaged_calls_never_autorevert(self) -> None:
        reset_sweep_ops_rust_for_tests()
        for i in range(18):  # 18 engaged stride + budget + count folds
            assert tuple_record_stride(10_000 * i + 1) == i + 1
            assert per_tuple_diffcheck_budget(200, i + 1) == max(1, 200 // (i + 1))
            expected = 6 * (i + 1)
            assert (
                routed_grid_tuple_count((2, 3, i + 1), python_law=lambda v=expected: v) == expected
            )
        assert sweep_ops_rust_disabled() is False
