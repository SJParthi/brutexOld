"""Frozen end-to-end Greeks-enrich golden (DEC-GREEKS-ENGINE-PR2B-001).

Replays the deterministic generator scenario through the REAL lake +
readers + kernel + write spine and asserts the STORED rows equal the frozen
``greeks_enrich_golden.json`` EXACTLY — solved Greeks bit-for-bit (the
per-artifact envelope of DEC-GREEKS-ENGINE-PR2-001 on the Linux x86_64 CI
reference platform), the stamped-NULL rows, and the packed provenance ids.
A drift here means the enrichment's bytes changed — a deliberate
``GREEKS_FORMULA_VERSION`` bump regenerates via
``tests/golden/generate_greeks_enrich_golden.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("trading_core", reason="the enrich golden needs the trading_core wheel")
pytest.importorskip("polars", reason="the enrich golden reads the temp lake via polars")

from tests.golden.generate_greeks_enrich_golden import (
    AT_EXPIRY_TS,
    CE_SYMBOL,
    NO_SPOT_TS,
    PE_SYMBOL,
    run_scenario,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "greeks_enrich_golden.json"


@pytest.fixture(scope="module")
def stored(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[dict[str, object]]]:
    lake = tmp_path_factory.mktemp("golden") / "lake"
    lake.mkdir()
    return run_scenario(lake)


@pytest.fixture(scope="module")
def frozen() -> dict[str, list[dict[str, object]]]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


class TestGreeksEnrichGolden:
    def test_stored_rows_match_the_frozen_fixture_exactly(
        self,
        stored: dict[str, list[dict[str, object]]],
        frozen: dict[str, list[dict[str, object]]],
    ) -> None:
        assert stored == frozen

    def test_fixture_covers_solved_null_and_boundary_stamps(
        self, frozen: dict[str, list[dict[str, object]]]
    ) -> None:
        from brutex.data.greeks_enrich import (
            STATUS_AT_EXPIRY,
            STATUS_NO_SPOT,
            STATUS_OK,
            unpack_greeks_provenance,
        )

        ce_by_ts = {row["timestamp"]: row for row in frozen[CE_SYMBOL]}
        statuses = {
            ts: unpack_greeks_provenance(int(row["greeks_provenance_id"])).status  # type: ignore[arg-type]
            for ts, row in ce_by_ts.items()
        }
        assert statuses[NO_SPOT_TS.isoformat()] == STATUS_NO_SPOT
        assert statuses[AT_EXPIRY_TS.isoformat()] == STATUS_AT_EXPIRY
        solved = [s for s in statuses.values() if s == STATUS_OK]
        assert len(solved) == 3
        # Every PE row solved; PE deltas are negative (the signed-side pin).
        for row in frozen[PE_SYMBOL]:
            assert unpack_greeks_provenance(int(row["greeks_provenance_id"])).status == STATUS_OK  # type: ignore[arg-type]
            assert isinstance(row["delta"], float) and row["delta"] < 0
