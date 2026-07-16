"""Flag-ON re-drive of the frozen ``bxsnap2`` parity goldens
(DEC-FEED-RUST-CORE-001 over DEC-BXSNAP2-PARITY-GOLDEN-001 / MIGRATION.md
#1695).

The sibling ``test_bxsnap2_parity_golden.py`` freezes the PYTHON oracle path
against ``fixtures/bxsnap2_parity_golden.json``. THIS gate re-drives every
frozen digest case + the positivity-refusal contract through the SAME public
entry point with ``BRUTEX_FEED_RUST=1`` — the byte-untouched fixture is the
oracle, and an **engagement spy** proves the ON arm genuinely exercised the
Rust kernel (a silently-degrading arm would make the gate vacuous). An
ON≡OFF A/B digest equality per case closes the loop.

Skips loudly when the wheel is absent (the CI golden job installs it).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
from brutex import feed_rust
from brutex.feed import replay
from brutex.feed_rust import FEED_RUST_ENV, reset_for_tests

from tests.golden._bxsnap2_parity import (
    DIGEST_CASES,
    POSITIVITY_REFUSAL_ROWS,
    build_bar_table,
    digest_for_bars,
    materialize,
    run_case,
)

pytest.importorskip(
    "brutex_feed",
    reason="brutex_feed wheel not installed — build via maturin (the golden CI job installs it)",
)

_FIXTURE_PATH: Final[Path] = Path(__file__).parent / "fixtures" / "bxsnap2_parity_golden.json"


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    """Isolate the feed_rust warn-once registry between tests."""
    reset_for_tests()


@pytest.fixture
def engaged_kernel(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Flag ON + a counting spy around the REAL kernel; returns the call log."""
    real = feed_rust.resolve_feed_kernel()
    assert real is not None
    calls: list[tuple[Any, ...]] = []

    def _spy(*args: Any) -> Any:
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(replay, "resolve_feed_kernel", lambda: _spy)
    monkeypatch.setenv(FEED_RUST_ENV, "1")
    return calls


def _frozen_digests() -> dict[str, str]:
    fixture: dict[str, Any] = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    return {case["name"]: str(case["digest"]) for case in fixture["cases"]}


@pytest.mark.parametrize("case", DIGEST_CASES, ids=lambda c: c.name)
def test_flag_on_reproduces_frozen_digest(case: Any, engaged_kernel: list[tuple[Any, ...]]) -> None:
    """Every frozen digest case, flag-ON, against the byte-untouched fixture."""
    bars, digest = run_case(case)
    assert engaged_kernel, f"kernel not engaged for case {case.name!r}"
    frozen = _frozen_digests()
    assert digest == frozen[case.name]
    assert len(bars) == case.expected_kept


@pytest.mark.parametrize("case", DIGEST_CASES, ids=lambda c: c.name)
def test_on_off_ab_digest_equality(case: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """ON ≡ OFF digest equality per case (the A/B belt over the fixture pin)."""
    monkeypatch.delenv(FEED_RUST_ENV, raising=False)
    off_digest = digest_for_bars(materialize(case))
    monkeypatch.setenv(FEED_RUST_ENV, "1")
    on_digest = digest_for_bars(materialize(case))
    assert on_digest == off_digest


def test_flag_on_positivity_refusal_contract(
    engaged_kernel: list[tuple[Any, ...]],
) -> None:
    """The refusal rows raise the SAME loud ValueError flag-ON."""
    with pytest.raises(ValueError, match="non-positive price"):
        replay.arrow_table_to_bars(
            build_bar_table(POSITIVITY_REFUSAL_ROWS), source="NSE/INDEX/NSE-NIFTY/1minute"
        )
    assert engaged_kernel, "kernel not engaged for the refusal contract"
