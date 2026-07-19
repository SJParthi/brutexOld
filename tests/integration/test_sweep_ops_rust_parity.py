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
from brutex.engine.sweep_ops_rust import (  # noqa: E402
    SWEEP_OPS_RUST_ENV,
    reset_sweep_ops_rust_for_tests,
    sweep_ops_rust_disabled,
)
from brutex.engine.sweep_stall_policy import (  # noqa: E402
    StallPolicy,
    resolve_stall_policy,
)
from brutex.ranking import OverfitGateConfig  # noqa: E402

_RUN_ID: Final[str] = "deadbeef" * 8


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
