"""Strict-kernel re-drive of the frozen ``bxsnap2`` parity goldens
(DEC-FEED-RUST-MATERIALIZE-002 over DEC-BXSNAP2-PARITY-GOLDEN-001 /
MIGRATION.md #1695).

The sibling ``test_bxsnap2_parity_golden.py`` freezes the materialization
path against ``fixtures/bxsnap2_parity_golden.json``. THIS gate re-drives
every frozen digest case + the positivity-refusal contract through the SAME
public entry point with an **engagement spy** proving the strict Rust kernel
(``scan_bars_strict``) genuinely made the decisions — the two-arm ON/OFF
machinery of DEC-FEED-RUST-CORE-001 died with the Python oracle
(DEC-FEED-RUST-MATERIALIZE-002 deleted its bodies), so the wheel-required
strict path is the ONLY path and the byte-untouched fixture is the oracle.

Skips loudly when the wheel is absent (the CI golden job installs it).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
from brutex.feed import replay

from tests.golden._bxsnap2_parity import (
    DIGEST_CASES,
    POSITIVITY_REFUSAL_ROWS,
    build_bar_table,
    run_case,
)

brutex_feed = pytest.importorskip(
    "brutex_feed",
    reason="brutex_feed wheel not installed — build via maturin (the golden CI job installs it)",
)

_FIXTURE_PATH: Final[Path] = Path(__file__).parent / "fixtures" / "bxsnap2_parity_golden.json"


class _KernelSpy:
    """Delegate every attribute to the REAL wheel; count strict-scan calls."""

    def __init__(self, calls: list[tuple[Any, ...]]) -> None:
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        return getattr(brutex_feed, name)

    def scan_bars_strict(self, *args: Any) -> Any:
        self._calls.append(args)
        return brutex_feed.scan_bars_strict(*args)


@pytest.fixture
def engaged_kernel(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """A counting spy around the REAL strict kernel; returns the call log."""
    calls: list[tuple[Any, ...]] = []
    spy = _KernelSpy(calls)
    monkeypatch.setattr(replay, "require_feed_kernel", lambda: spy)
    return calls


def _frozen_digests() -> dict[str, str]:
    fixture: dict[str, Any] = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    return {case["name"]: str(case["digest"]) for case in fixture["cases"]}


@pytest.mark.parametrize("case", DIGEST_CASES, ids=lambda c: c.name)
def test_frozen_digest_cases(case: Any, engaged_kernel: list[tuple[Any, ...]]) -> None:
    """Every frozen digest case against the byte-untouched fixture, with the
    spy proving the strict kernel genuinely decided the kept set."""
    bars, digest = run_case(case)
    assert engaged_kernel, f"strict kernel not engaged for case {case.name!r}"
    frozen = _frozen_digests()
    assert digest == frozen[case.name]
    assert len(bars) == case.expected_kept


def test_positivity_refusal_contract(engaged_kernel: list[tuple[Any, ...]]) -> None:
    """The refusal rows raise the SAME loud ValueError on the strict path."""
    with pytest.raises(ValueError, match="non-positive price"):
        replay.arrow_table_to_bars(
            build_bar_table(POSITIVITY_REFUSAL_ROWS), source="NSE/INDEX/NSE-NIFTY/1minute"
        )
    assert engaged_kernel, "strict kernel not engaged for the refusal contract"
