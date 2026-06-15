"""Stage-3 optimizer dependency smoke (DEC-STAGE3-OPTIMIZE-EXTRA-001, ADR-0007 slice 1).

The BIPOP-CMA-ES refinement (ADR-0007 / DEC-STAGE3-DESIGN-LOCK-001) hashes the
pycma version into every refinement-run identity, so the dependency contract is
load-bearing: the EXACT pin (``cma==4.4.4``) and same-environment seed
determinism (condition 5) are pinned here, in CI, on every push. The CI unit job
installs the ``[optimize]`` extra so these tests RUN rather than silently skip
(the DEC-DASHBOARD-CI-UNMASK-001 masking lesson).

Honest envelope (Golden Rule #10 / ADR-0007 condition 5): determinism is
asserted WITHIN one environment (same process, same pycma+numpy, same platform)
— CMA-ES eigendecomposition is ULP-sensitive to BLAS backends, so NO
cross-version or cross-platform bit-identity is claimed or tested here.
"""

from __future__ import annotations

import pytest

cma = pytest.importorskip("cma")

#: The ADR-0007 condition-9 pin — pyproject's `optimize` extra. A drifted
#: environment (auto-bumped or hand-upgraded optimizer) must fail loudly here,
#: because a version change silently changes search trajectories and
#: invalidates every cached refinement-run identity.
_PINNED_VERSION = "4.4.4"


def _sphere(x: list[float]) -> float:
    """The 2-D sphere objective — deterministic, minimum at the origin."""
    return float(sum(v * v for v in x))


def _run(seed: int) -> tuple[list[float], float, int]:
    """One tiny seeded BIPOP-style inner run via the manual ask/tell loop.

    Mirrors the locked Stage-3 shape (single-threaded, seeded, bounded budget);
    ``verbose=-9`` keeps pycma fully silent and file-free.
    """
    es = cma.CMAEvolutionStrategy(
        [0.5, 0.5],
        0.3,
        {"seed": seed, "verbose": -9, "maxfevals": 80},
    )
    while not es.stop():
        xs = es.ask()
        es.tell(xs, [_sphere(list(x)) for x in xs])
    return list(es.result.xbest), float(es.result.fbest), int(es.result.evaluations)


class TestStage3OptimizerContract:
    """The dependency facts Stage-3's reproducibility hash relies on."""

    def test_exact_version_pin(self) -> None:
        # The environment must carry EXACTLY the pinned optimizer — a drift here
        # means cached refinement-run identities no longer match reality.
        assert cma.__version__ == _PINNED_VERSION

    def test_seeded_run_is_deterministic_within_env(self) -> None:
        # ADR-0007 condition 5: same seed + same env ⇒ bit-identical trajectory.
        # Two independent optimizer instances must agree EXACTLY (xbest, fbest,
        # and the evaluation count — the value that feeds the DSR trial count N).
        first = _run(seed=11)
        second = _run(seed=11)
        assert first == second

    def test_seed_actually_drives_the_trajectory(self) -> None:
        # A different seed must change the sampled trajectory — otherwise the
        # hash-derived per-mask seeds (condition 5) would be decorative.
        assert _run(seed=11)[0] != _run(seed=12)[0]
