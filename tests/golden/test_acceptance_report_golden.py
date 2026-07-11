"""Frozen-schema golden: the v1 report shape for a pinned lake+clock+seed.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). Volatile cells
(run-id nonce, wall-clock durations, box fingerprint, RSS/latency numbers)
are normalised by an EXPLICIT allowlist; everything else — key sets, verdict
tokens, abstain reasons, evidence keys, coverage blocks, totals — is
byte-pinned against the committed fixture. Schema evolution is additive-only:
regenerating this fixture is a REVIEWED act (BRUTEX_REGEN_ACCEPTANCE_GOLDEN=1),
never a drive-by.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from brutex.acceptance.run import RunConfig, run_acceptance

from tests.fakes.acceptance_twin import NOW, make_twin

FIXTURE = Path(__file__).parent / "fixtures" / "acceptance_report_v1_golden.json"

#: Evidence keys whose VALUES are wall-clock/box-volatile (names still pinned).
_VOLATILE_EVIDENCE_KEYS = frozenset({"ledger_rss_kib_before", "ledger_rss_kib_after"})


def _normalise(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if key in {"run_id", "generated_utc", "duration_s", "lake_root"}:
                out[key] = "<volatile>"
            elif key == "box":
                out[key] = {"keys": sorted(value)}
            elif key in _VOLATILE_EVIDENCE_KEYS:
                out[key] = "<volatile>"
            else:
                out[key] = _normalise(value)
        return out
    if isinstance(obj, list):
        return [_normalise(v) for v in obj]
    return obj


def _build_report(tmp_path: Path) -> dict[str, Any]:
    lake = make_twin(tmp_path)
    cfg = RunConfig(
        lake_root=lake,
        checks=("unique", "tf", "pull", "rate"),
        seed="2026-07-10",
        sample_n=50,
        abstain_budget=10,
        self_test_mode="skipped",  # teeth run separately; the golden pins SHAPE
        argv=("verify-acceptance", "--seed", "2026-07-10"),
    )
    _code, report = run_acceptance(cfg, now_utc=lambda: NOW)
    return report


class TestReportGolden:
    def test_normalised_report_matches_frozen_fixture(self, tmp_path: Path) -> None:
        got = _normalise(_build_report(tmp_path))
        if os.environ.get("BRUTEX_REGEN_ACCEPTANCE_GOLDEN") == "1":  # pragma: no cover
            FIXTURE.parent.mkdir(parents=True, exist_ok=True)
            FIXTURE.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
        assert got == expected, (
            "acceptance report v1 shape drifted from the frozen golden — schema "
            "evolution is additive-only and regeneration is a reviewed act "
            "(BRUTEX_REGEN_ACCEPTANCE_GOLDEN=1)"
        )
