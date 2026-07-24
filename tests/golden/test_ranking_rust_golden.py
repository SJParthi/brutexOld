"""Flag-ON golden legs for the Rust ranking conversion (slice-4 PR-1).

The byte-frozen overfit leaderboard of
:mod:`tests.golden.test_ranking_overfit_golden` — including the exact
ranked-JSON literal ``0.7927106202284727`` — MUST be reproduced with
``BRUTEX_RANKING_RUST=1``, on EVERY host:

* The output equality is UNCONDITIONAL — the (b*) families run an
  always-on per-call differential that ships the Python oracle's bits on
  any mismatch, so a host whose libm breaks the modeled pow identities can
  latch a family but can never move a byte of the leaderboard.
* The ENGAGEMENT assertions are two-arm (the design's host-probe law): the
  (a)-class families (gates, order) are pow-free and must engage with zero
  divergence everywhere; the (b*) composite family must engage with zero
  divergence on a pow-identity host (``tests.fakes.pow_identity`` — NO
  measured host currently qualifies: the production M4-Pro itself measured
  non-identity, verify-m4pro run 29194011032, coordinator-ruled 2026-07-12,
  MIGRATION.md #1731 amendment) and may instead autorevert LOUDLY on a
  non-identity host (this repo's glibc build container AND the M4-Pro's
  Apple libm, both measured) — never silently, never with a wire delta; the
  oracle-served posture is the ruled PERMANENT one.
"""

from __future__ import annotations

import pytest
from brutex.metrics.compute import ResultMetrics
from brutex.ranking import OverfitGateConfig, rank_combos
from brutex.ranking.rust_ranking import RANKING_RUST_ENV, reset_ranking_rust_for_tests
from structlog.testing import capture_logs

from tests.fakes.pow_identity import pow_identity_holds
from tests.golden.test_ranking_overfit_golden import _GOLDEN, _m, _render


@pytest.fixture(autouse=True)
def _fresh_families() -> object:
    reset_ranking_rust_for_tests()
    yield
    reset_ranking_rust_for_tests()


def _candidates() -> list[tuple[int, ResultMetrics]]:
    """The EXACT pool of the byte-frozen sibling golden (imported fixtures)."""
    return [
        (0b0001, _m(sharpe_ratio=2.0, sortino_ratio=1.8, calmar_ratio=1.2)),
        (0b0010, _m(sharpe_ratio=2.5, sortino_ratio=2.0, calmar_ratio=1.5)),
        (0b0100, _m(sharpe_ratio=3.0, sortino_ratio=2.4, calmar_ratio=1.8)),
        (0b1000, _m(sharpe_ratio=1.0, sortino_ratio=0.9, calmar_ratio=0.5)),
    ]


def test_flag_on_reproduces_the_byte_frozen_overfit_golden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ON arm lands on the EXACT frozen leaderboard bytes — including
    the ``0.7927106202284727`` composite literal — with the (a)-class
    families genuinely engaged (the vacuous-gate anti-trap)."""
    monkeypatch.setenv(RANKING_RUST_ENV, "1")
    with capture_logs() as logs:
        ranked = rank_combos(_candidates(), overfit=OverfitGateConfig(dsr_min=0.95))
    assert _render(ranked) == _GOLDEN
    assert repr(ranked[0].composite_score) == "0.7927106202284727"
    assert repr(ranked[0].deflated_sharpe) == "0.9999999999834508"
    engaged = [e["family"] for e in logs if e["event"] == "rust_parity.engaged"]
    # Exactly one engaged event per pow-free family (the (a) rows can never
    # diverge — a miss here means the ON path silently served Python).
    assert engaged.count("ranking.gates") == 1
    assert engaged.count("ranking.order") == 1
    assert engaged.count("ranking.term_inputs") == 1
    assert engaged.count("ranking.overfit_verdict") == 1
    # Any autorevert must be a LOUD (b*) divergence — legal only on a
    # non-identity host, and never a wire delta (the equality above).
    reverted = [e for e in logs if e["event"] == "rust_parity.autoreverted"]
    for event in reverted:
        assert event["family"] in {"ranking.composite", "ranking.dsr_z"}
        assert event["reason"] == "divergence"
        assert not pow_identity_holds()


def test_flag_on_composite_engages_cleanly_on_a_pow_identity_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE strong (b*) leg — identity-host-only arm: on a host whose libm
    satisfies the modeled identities the composite family engages with ZERO
    divergence. The production M4-Pro itself measured NON-identity
    (verify-m4pro run 29194011032, coordinator-ruled 2026-07-12), so this
    leg SKIPS there by design — oracle-served is the ruled permanent
    posture (MIGRATION.md #1731 amendment). Skipped loudly elsewhere too
    (the host-probe test carries the ERROR-level visibility)."""
    if not pow_identity_holds():
        pytest.skip(
            "non-identity host libm (see test_pow_host_probe) — the always-on "
            "differential serves oracle bits here; every measured host so far "
            "(glibc build container AND the production M4-Pro, run "
            "29194011032) is non-identity, so oracle-served is the permanent "
            "posture (DEC-RANKING-POWF-ONHOST-001 is the deferred on-host "
            "powf follow-up)."
        )
    monkeypatch.setenv(RANKING_RUST_ENV, "1")
    with capture_logs() as logs:
        ranked = rank_combos(_candidates(), overfit=OverfitGateConfig(dsr_min=0.95))
    assert _render(ranked) == _GOLDEN
    engaged = [e["family"] for e in logs if e["event"] == "rust_parity.engaged"]
    assert engaged.count("ranking.composite") == 1
    assert engaged.count("ranking.dsr_z") == 1
    assert not [e for e in logs if e["event"] == "rust_parity.autoreverted"]


def test_flag_on_off_renders_are_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both arms render byte-identically on every host (the wire law)."""
    cfg = OverfitGateConfig(dsr_min=0.95)
    monkeypatch.setenv(RANKING_RUST_ENV, "0")
    off = _render(rank_combos(_candidates(), overfit=cfg))
    monkeypatch.setenv(RANKING_RUST_ENV, "1")
    on = _render(rank_combos(_candidates(), overfit=cfg))
    assert on == off == _GOLDEN


def test_off_arm_never_emits_parity_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """The explicit ``"0"`` kill-switch arm never touches the parity machinery,
    yet renders the byte-frozen golden.

    Since DEC-RUST-DEFAULT-ON-WAVE-001 (#2215) the ranking seam is default-ON, so
    an ABSENT flag ENGAGES (and emits ``rust_parity.*`` on an identity host). The
    parity-free proof therefore arms the explicit OFF kill switch — the operator's
    one lever to force the verbatim Python pipeline; the render is byte-identical."""
    monkeypatch.setenv(RANKING_RUST_ENV, "0")
    with capture_logs() as logs:
        ranked = rank_combos(_candidates(), overfit=OverfitGateConfig(dsr_min=0.95))
    assert _render(ranked) == _GOLDEN
    assert not [e for e in logs if str(e["event"]).startswith("rust_parity.")]
