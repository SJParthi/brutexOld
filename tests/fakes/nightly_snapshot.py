"""Test double helpers for nightly sealed snapshots (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

`inject_legacy_audit_entry` forges the PRE-round-5 snapshot shape: a sealed
snapshot whose manifest carries the shared cross-run nightly audit chain
(``results/nightly/audit/nightly.jsonl``). `backup_inventory` can no longer
produce that shape (the chain is excluded by law), so restore's legacy-skip
belt is only reachable through this forge.
"""

from __future__ import annotations

import json
from pathlib import Path

from brutex.nightly.backup import MANIFEST_NAME, sha256_file

#: The forged entry's combined key, as restore reports it.
LEGACY_AUDIT_KEY = "results/nightly/audit/nightly.jsonl"


def inject_legacy_audit_entry(snapshot: Path, *, content: bytes = b"SNAPSHOT-ERA-CHAIN\n") -> str:
    """Add a snapshot-era nightly-audit file + manifest entry to *snapshot*.

    Returns the injected ``<namespace>/<relpath>`` key. The manifest's
    ``item_count``/``total_bytes`` are kept coherent so Phase-A verification
    still passes over the forged legacy snapshot.
    """
    audit_copy = snapshot / "results" / "nightly" / "audit" / "nightly.jsonl"
    audit_copy.parent.mkdir(parents=True)
    audit_copy.write_bytes(content)
    manifest_path = snapshot / MANIFEST_NAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["entries"].append(
        {
            "namespace": "results",
            "relpath": "nightly/audit/nightly.jsonl",
            "size_bytes": audit_copy.stat().st_size,
            "sha256": sha256_file(audit_copy),
            "restore_order": 0,
        }
    )
    data["item_count"] += 1
    data["total_bytes"] += audit_copy.stat().st_size
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    return LEGACY_AUDIT_KEY
