"""Tests for the two-tier O(1) ratio-ceiling helper (DEC-O1-STRICT-TIER-001).

Guards that the strict 1.1x tier activates ONLY on the literal ``BRUTEX_M4PRO_STRICT=1``
(the self-hosted M4-Pro runner) and that shared CI keeps its robust default ceiling
unchanged — i.e. the two-tier gate adds zero behaviour to shared runners.
"""

from __future__ import annotations

import pytest

from tests.bench._o1_tiers import (
    ENV_M4PRO_STRICT,
    STRICT_RATIO_CEILING,
    m4pro_strict_active,
    ratio_ceiling,
)


def test_shared_ci_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the strict env unset, the caller's robust shared-CI ceiling is returned."""
    monkeypatch.delenv(ENV_M4PRO_STRICT, raising=False)
    assert not m4pro_strict_active()
    assert ratio_ceiling(5.0) == 5.0
    assert ratio_ceiling(8.0) == 8.0
    assert ratio_ceiling(10.0) == 10.0


def test_strict_tier_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """With BRUTEX_M4PRO_STRICT=1 every ceiling tightens to the decreed 1.1x."""
    monkeypatch.setenv(ENV_M4PRO_STRICT, "1")
    assert m4pro_strict_active()
    assert STRICT_RATIO_CEILING == 1.1
    assert ratio_ceiling(5.0) == 1.1
    assert ratio_ceiling(10.0) == 1.1


@pytest.mark.parametrize("val", ["", "0", "true", "yes", "2", " 1 "])
def test_only_literal_one_activates(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    """Strict mode uses the strict-decoder: only a trimmed literal '1' activates it."""
    monkeypatch.setenv(ENV_M4PRO_STRICT, val)
    expected_active = val.strip() == "1"
    assert m4pro_strict_active() is expected_active
    assert ratio_ceiling(5.0) == (1.1 if expected_active else 5.0)
