"""W-6a ENDGAME scan gate — Rust-arm determinism + frozen expectations.

THE FLIP LANDED (DEC-RUST-LAKE-WAVED-001 / ADR-0020 slice G9): the Python
scan core is DELETED, so the `#1916` harness
(`tests/integration/test_waved_poison_corpus_differential.py`) now drives
the ONE Rust-backed ``scan_lake_invariants`` seam directly — its identity
self-test IS the Rust determinism gate and its ``test_expected_verdict``
IS the frozen-expectation gate over all 27 corpus cases (the harness stays
byte-UNMODIFIED — never weaken the harness).

THIS file keeps the W-4 flip-gate record honest on top of that:

* the two-fresh-builds determinism differential through the harness's own
  ``run_differential`` + normalizer (impl_a = impl_b = the PRODUCTION seam);
* the FROZEN per-case expectation pin (finding-type set + skip census) —
  the record that survived the oracle deletion verbatim from the corpus
  manifest;
* the ENGAGEMENT census — ``last_rust_scan_reads()`` publishes ONLY on a
  crate serve, so a fabricated/no-op pass can never masquerade as parity;
* the pause-callback verbatim re-raise law across the FFI.

Requires the built ``brutex_lake`` wheel — with the Python core deleted the
wheel is a production PREREQUISITE; this suite skips honestly when it is
absent (the unit suites own the wheel-free typed-refusal story).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
pytest.importorskip("brutex_lake")

from brutex.data import lake_rust_waved as lrw
from brutex.data.lake_invariants import LakeScanReport, scan_lake_invariants

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
def _endgame_arm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The ONE Rust path: retired flag unset, module state reset each test."""
    monkeypatch.delenv(lrw.LAKE_WAVED_RUST_ENV, raising=False)
    lrw.reset_lake_rust_waved_for_tests()
    yield
    lrw.reset_lake_rust_waved_for_tests()


def _rust_arm(lake_root: Path, **kwargs: object) -> LakeScanReport:
    """The PRODUCTION seam — Rust-backed unconditionally since W-6a."""
    return scan_lake_invariants(lake_root, **kwargs)  # type: ignore[arg-type]


def _assert_engaged(case_name: str) -> None:
    """The engagement census — a serve that never touched the crate fails."""
    assert lrw.last_rust_scan_reads() is not None, (
        f"ENGAGEMENT FAILURE [{case_name}]: the scan completed without the "
        "crate publishing its reads counter — the seam did not serve from "
        "the Rust arm (do not relax this assertion)"
    )


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: str(case.name))
class TestScanRustArmDifferential:
    """27-case Rust determinism + frozen expectations + engagement."""

    def test_rust_arm_is_deterministic(self, case: PoisonCase, tmp_path: Path) -> None:
        """impl_a = impl_b = the Rust seam over two fresh builds → EQUAL."""
        _skip_unless_supported(case, tmp_path)
        verdict_a, verdict_b = run_differential(_rust_arm, _rust_arm, case, tmp_path)
        assert verdict_a == verdict_b
        _assert_engaged(case.name)

    def test_frozen_expectation_pin(self, case: PoisonCase, tmp_path: Path) -> None:
        """The Rust arm yields EXACTLY the corpus manifest's frozen verdict.

        The finding-TYPE set + the skip census were frozen while the Python
        oracle still existed (the pre-delete W-4 parity record) — the crate
        must keep matching them forever, by name, per case.
        """
        _skip_unless_supported(case, tmp_path)
        lake = tmp_path / "lake"
        lake.mkdir()
        case.build(lake)
        report = scan_lake_invariants(lake, **dict(case.scan_kwargs))
        got_types = {type(finding).__name__ for finding in report.findings}
        assert got_types == set(case.expected_finding_types)
        if case.expects_skip:
            assert report.files_skipped + report.option_files_skipped > 0
        _assert_engaged(case.name)


def test_raising_pause_callback_reraises_verbatim_through_the_crate(
    tmp_path: Path,
) -> None:
    """A real-wheel L3-throttle raise crosses the FFI and re-raises VERBATIM."""
    case = next(case for case in CORPUS if case.name == "bars_intra_file_dup")
    lake = tmp_path / "lake"
    lake.mkdir()
    case.build(lake)
    marker = RuntimeError("throttle exploded mid-scan")

    def _pause() -> None:
        raise marker

    with pytest.raises(RuntimeError) as caught:
        scan_lake_invariants(lake, per_file_pause=_pause)
    assert caught.value is marker
