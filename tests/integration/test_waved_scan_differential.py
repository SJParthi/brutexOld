"""W-4 poison-corpus DIFFERENTIAL — the Rust scan arm vs the Python oracle.

THE FLIP GATE (DEC-RUST-LAKE-WAVED-001 / ADR-0020 slice G9): the #1916
harness (`tests/integration/test_waved_poison_corpus_differential.py`)
shipped the Python-identity self-test as "the mechanical gate the future
lake_invariants Rust flip must pass". THIS file binds the Wave-D W-4 Rust
scan (`brutex_lake.waved_scan_lake` behind ``BRUTEX_LAKE_WAVED_RUST``, via
the ONE ``scan_lake_invariants`` seam) as **impl_b** at the harness's own
``ScannerImpl`` seam and drives ALL 27 corpus cases with the harness's own
``run_differential`` + normalizer — the harness itself stays byte-UNMODIFIED
(never weaken the harness).

ENGAGEMENT CENSUS (the anti-vacuous flip law): every case additionally
asserts the Rust arm genuinely ENGAGED — ``last_rust_scan_reads()`` is
published ONLY by an engaged steady-state serve, and the shared latch stayed
clear — so a degrade-to-oracle pass can never masquerade as Rust parity. A
case that can only pass by degrading is a FLIP BLOCKER and fails here
LOUDLY, by name.

Requires the built ``brutex_lake`` wheel (skipped honestly when absent —
the lane workflow installs it; the wheel-free coverage story lives in the
unit suites).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
pytest.importorskip("brutex_lake")

from brutex.data import lake_rust_waved as lrw
from brutex.data.lake_invariants import (
    LakeScanReport,
    _scan_lake_invariants_python,  # pyright: ignore[reportPrivateUsage]
    scan_lake_invariants,
)

from tests.fakes.poison_corpus import CORPUS
from tests.integration.test_waved_poison_corpus_differential import (
    _skip_unless_supported,  # pyright: ignore[reportPrivateUsage]
    run_differential,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tests.fakes.poison_corpus import PoisonCase


@pytest.fixture(autouse=True)
def _flag_on_steady_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Flag ON + the post-burn-in steady state, reset around every test.

    ``remaining=0`` disarms the burn-in comparator so a served result is the
    CRATE's — with the full reconstruction + event-replay path exercised —
    and the engagement census below has real teeth (an armed burn-in serves
    the oracle report by design, which would read as a degrade here).
    """
    monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
    lrw.reset_lake_rust_waved_for_tests(remaining=0)
    yield
    lrw.reset_lake_rust_waved_for_tests()


def _rust_arm(lake_root: Path, **kwargs: object) -> LakeScanReport:
    """The Rust-backed impl_b: the PRODUCTION seam with the flag ON."""
    return scan_lake_invariants(lake_root, **kwargs)  # type: ignore[arg-type]


def _assert_engaged(case_name: str) -> None:
    """The engagement census — a degrade-only pass is a FLIP BLOCKER."""
    assert not lrw.rust_waved_disabled(), (
        f"FLIP BLOCKER [{case_name}]: the waved seam autoreverted/latched "
        f"({lrw._DISABLED_REASON[0]!r}) — the Rust arm did not survive this "  # pyright: ignore[reportPrivateUsage]
        "corpus case"
    )
    assert lrw.last_rust_scan_reads() is not None, (
        f"FLIP BLOCKER [{case_name}]: the Rust scan DEGRADED to the Python "
        "oracle instead of engaging — a degrade-only pass must never count "
        "as Rust parity (name the blocker in waved_progress.md, do not relax "
        "this assertion)"
    )


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: str(case.name))
class TestScanRustArmDifferential:
    """The 27-case Rust-vs-Python differential + determinism + engagement."""

    def test_rust_arm_matches_the_python_oracle(self, case: PoisonCase, tmp_path: Path) -> None:
        """impl_a = Python oracle, impl_b = the Rust seam → normalized-EQUAL."""
        _skip_unless_supported(case, tmp_path)
        verdict_python, verdict_rust = run_differential(
            _scan_lake_invariants_python, _rust_arm, case, tmp_path
        )
        assert verdict_python == verdict_rust
        _assert_engaged(case.name)

    def test_rust_arm_is_deterministic(self, case: PoisonCase, tmp_path: Path) -> None:
        """impl_a = impl_b = the Rust seam over two fresh builds → EQUAL."""
        _skip_unless_supported(case, tmp_path)
        verdict_a, verdict_b = run_differential(_rust_arm, _rust_arm, case, tmp_path)
        assert verdict_a == verdict_b
        _assert_engaged(case.name)


def test_real_wheel_burn_in_matches_on_a_poisoned_lake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh-process burn-in call over a REAL poisoned lake matches + serves.

    The armed comparator runs BOTH arms and must find the reconstructed
    crate report order-exact-equal to the Python report — no autorevert, the
    budget ticks down, the oracle report serves (its logs fired once).
    """
    monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
    lrw.reset_lake_rust_waved_for_tests()  # fresh process view — burn-in ARMED
    case = next(case for case in CORPUS if case.name == "bars_intra_file_dup")
    lake = tmp_path / "lake"
    lake.mkdir()
    case.build(lake)
    try:
        report = scan_lake_invariants(lake, **dict(case.scan_kwargs))
        assert not lrw.rust_waved_disabled(), lrw._DISABLED_REASON[0]  # pyright: ignore[reportPrivateUsage]
        assert (
            lrw._BURN_IN_REMAINING[lrw.SURFACE_SCAN]  # pyright: ignore[reportPrivateUsage]
            == lrw.BURN_IN_SAMPLES - 1
        )
        assert {type(finding).__name__ for finding in report.findings} == set(
            case.expected_finding_types
        )
        # Burn-in serves the ORACLE report — the reads counter stays unset.
        assert lrw.last_rust_scan_reads() is None
    finally:
        lrw.reset_lake_rust_waved_for_tests()


def test_raising_pause_callback_reraises_verbatim_through_the_crate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real-wheel L3-throttle raise crosses the FFI and re-raises VERBATIM."""
    monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
    lrw.reset_lake_rust_waved_for_tests(remaining=0)
    case = next(case for case in CORPUS if case.name == "bars_intra_file_dup")
    lake = tmp_path / "lake"
    lake.mkdir()
    case.build(lake)
    marker = RuntimeError("throttle exploded mid-scan")

    def _pause() -> None:
        raise marker

    try:
        with pytest.raises(RuntimeError) as caught:
            scan_lake_invariants(lake, per_file_pause=_pause)
        assert caught.value is marker
        # Never folded into the degrade set — the seam stays healthy.
        assert not lrw.rust_waved_disabled()
    finally:
        lrw.reset_lake_rust_waved_for_tests()
