"""Byte-frozen golden for the run-manifest JSON (R28, D-7/D-8 + P4 sub-shapes).

Freezes the EXACT on-disk manifest text for a fully-populated
:class:`RunManifest` — the nine contract keys in order, the ordered
``per_door_kills`` ``[{door, killed}]`` list, the ``actual.funnel`` sub-shape,
the scalar ``dedup_skips`` + three-key detail, and every additive key. Any
serialiser / key-order / shape drift breaks this file byte-for-byte, and the
load path must reproduce the identical dataclass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
from brutex.observability.run_manifest import (
    RunManifest,
    load_manifest,
    manifest_path,
    write_manifest,
)

pytestmark = pytest.mark.golden

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r28_manifest_bytes_frozen": ("R28",),
    "test_r28_manifest_golden_roundtrip": ("R28",),
}

_RUN_ID: Final[str] = "cd" * 32

#: The FROZEN on-disk text (generated once from the real writer; regenerating
#: requires a ledgered decision — this IS the byte contract).
_GOLDEN_TEXT: Final[str] = (
    "{\n"
    f'  "run_id": "{_RUN_ID}",\n'
    '  "selection": {\n'
    '    "symbols": [\n'
    '      "NIFTY"\n'
    "    ],\n"
    '    "intervals": [\n'
    '      "5minute"\n'
    "    ],\n"
    '    "k": 2,\n'
    '    "mode": "A"\n'
    "  },\n"
    '  "estimate": {\n'
    '    "trials": 1770,\n'
    '    "est_seconds_provisional": 60,\n'
    '    "est_bytes": 1048576\n'
    "  },\n"
    '  "actual": {\n'
    '    "trials": 1770,\n'
    '    "wall_seconds": 12.5,\n'
    '    "tuples_done": 3,\n'
    '    "tuples_failed": 0,\n'
    '    "funnel": [\n'
    "      {\n"
    '        "stage": "enumerated",\n'
    '        "count": 1770\n'
    "      },\n"
    "      {\n"
    '        "stage": "tested",\n'
    '        "count": 1700\n'
    "      }\n"
    "    ],\n"
    '    "enumerated": 1770,\n'
    '    "tested": 1700,\n'
    '    "skipped": 70\n'
    "  },\n"
    '  "per_stage_timings": {\n'
    '    "launch": 0.5,\n'
    '    "sweep": 10.0\n'
    "  },\n"
    '  "per_door_kills": [\n'
    "    {\n"
    '      "door": "min_hits",\n'
    '      "killed": 40\n'
    "    },\n"
    "    {\n"
    '      "door": "dsr",\n'
    '      "killed": 3\n'
    "    }\n"
    "  ],\n"
    '  "dedup_skips": 7,\n'
    '  "tripwires": [\n'
    "    {\n"
    '      "ts_utc": 1700000001.0,\n'
    '      "name": "count_reconcile",\n'
    '      "severity": "error",\n'
    '      "detail": "off by 1"\n'
    "    }\n"
    "  ],\n"
    '  "suspect": true,\n'
    '  "schema_version": 1,\n'
    '  "attempt": 2,\n'
    '  "exit_code": 0,\n'
    '  "updated_utc": 1700000002.0,\n'
    '  "suspect_reasons": [\n'
    '    "count_reconcile"\n'
    "  ],\n"
    '  "dedup_skips_detail": {\n'
    '    "cache": 4,\n'
    '    "resume": 2,\n'
    '    "sentinel": 1\n'
    "  },\n"
    '  "preflight": {\n'
    '    "refusals": [],\n'
    '    "clamps": [\n'
    "      {\n"
    '        "field": "start",\n'
    '        "clamped_to": "2020-01-01"\n'
    "      }\n"
    "    ]\n"
    "  },\n"
    '  "recorder_degraded": false\n'
    "}\n"
)


def _golden_manifest() -> RunManifest:
    return RunManifest(
        run_id=_RUN_ID,
        selection={"symbols": ["NIFTY"], "intervals": ["5minute"], "k": 2, "mode": "A"},
        estimate={"trials": 1770, "est_seconds_provisional": 60, "est_bytes": 1048576},
        actual={
            "trials": 1770,
            "wall_seconds": 12.5,
            "tuples_done": 3,
            "tuples_failed": 0,
            "funnel": [
                {"stage": "enumerated", "count": 1770},
                {"stage": "tested", "count": 1700},
            ],
            "enumerated": 1770,
            "tested": 1700,
            "skipped": 70,
        },
        per_stage_timings={"launch": 0.5, "sweep": 10.0},
        per_door_kills={"min_hits": 40, "dsr": 3},
        dedup_skips=7,
        tripwires=[
            {
                "ts_utc": 1700000001.0,
                "name": "count_reconcile",
                "severity": "error",
                "detail": "off by 1",
            }
        ],
        suspect=True,
        schema_version=1,
        attempt=2,
        exit_code=0,
        updated_utc=1700000002.0,
        suspect_reasons=("count_reconcile",),
        dedup_skips_detail={"cache": 4, "resume": 2, "sentinel": 1},
        preflight={"refusals": [], "clamps": [{"field": "start", "clamped_to": "2020-01-01"}]},
        recorder_degraded=False,
    )


class TestRunManifestGolden:
    def test_r28_manifest_bytes_frozen(self, tmp_path: Path) -> None:
        """write_manifest reproduces the frozen manifest text EXACTLY."""
        write_manifest(tmp_path, _golden_manifest())
        assert manifest_path(tmp_path, _RUN_ID).read_text(encoding="utf-8") == _GOLDEN_TEXT

    def test_r28_manifest_golden_roundtrip(self, tmp_path: Path) -> None:
        """Loading the frozen text reproduces the identical dataclass."""
        path = manifest_path(tmp_path, _RUN_ID)
        path.parent.mkdir(parents=True)
        path.write_text(_GOLDEN_TEXT, encoding="utf-8")
        assert load_manifest(tmp_path, _RUN_ID) == _golden_manifest()
