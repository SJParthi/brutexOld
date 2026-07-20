"""cascade_wire (de)serialisation of the extreme-vol filter-#10 surface.

Stem-matched companion of :mod:`tests.unit.test_engine_cascade_wire` for the
discovery coverage gate (DEC-REGIME-MUST-SURVIVE-STRATUM-001): pins the
flag-ON arms of the DEFAULT-ELIDED ``extreme_vol_stratum`` config key and the
EMIT-ON-PRESENT ``extreme_vol_status``/``extreme_vol_stats`` verdict keys —
the two branches the pre-filter stem suite cannot reach.  The full behaviour
battery lives in :mod:`tests.unit.test_cascade_extreme_vol`; this file keeps
:mod:`brutex.engine.cascade_wire` at 100% under its OWN stem-matched set.
"""

from __future__ import annotations

import brutex.engine.cascade_wire as cw
from brutex.metrics.cascade import CandidateVerdict


def test_config_to_dict_keeps_flag_when_on_and_round_trips() -> None:
    cfg = cw.cascade_config_for_sweep("abcd", overrides={"extreme_vol_stratum": True})
    data = cw.cascade_config_to_dict(cfg)
    assert data["extreme_vol_stratum"] is True  # NOT elided when non-default
    assert cw.cascade_config_from_dict(data) == cfg


def test_verdict_to_dict_emits_status_with_stats_dict() -> None:
    verdict = CandidateVerdict(
        candidate_id="c1",
        passed=False,
        rejected_at=None,
        detail="extreme-vol stratum fail",
        extreme_vol_status="fail",
        extreme_vol_stats={"stratum_trades": 8, "stratum_expectancy_paisa": -50.0},
    )
    payload = cw._verdict_to_dict(verdict)
    assert payload["extreme_vol_status"] == "fail"
    stats = payload["extreme_vol_stats"]
    assert stats == {"stratum_trades": 8, "stratum_expectancy_paisa": -50.0}
    assert stats is not verdict.extreme_vol_stats  # defensive copy, never aliased


def test_verdict_to_dict_emits_status_with_none_stats() -> None:
    verdict = CandidateVerdict(
        candidate_id="c2",
        passed=True,
        rejected_at=None,
        detail="pass",
        extreme_vol_status="unproven_current_regime",
        extreme_vol_stats=None,
    )
    payload = cw._verdict_to_dict(verdict)
    assert payload["extreme_vol_status"] == "unproven_current_regime"
    assert payload["extreme_vol_stats"] is None
