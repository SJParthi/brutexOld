"""Golden-master hash pin for ``brutex.adapters.dhan.constants``.

If anyone changes a securityId, a symbol, a segment, or an exchange — or
adds a 5th instrument — the sha256 hash flips and this test fails. The
test message instructs the operator to (a) verify the change is
intentional and (b) update the recorded hash + open a MIGRATION.md
decision entry.

The hash is computed over a canonical serialization of
``ALL_SPOT_INSTRUMENTS`` (the order is locked by universe.py order). The
serialization is JSON with ``sort_keys=True`` so dataclass field-order
changes are detected but Python-dict iteration order is not a flake risk.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any

import pytest
from brutex.adapters.dhan.constants import ALL_SPOT_INSTRUMENTS

#: Locked sha256 over the canonical JSON serialization of
#: ``ALL_SPOT_INSTRUMENTS``. Updated only by a MIGRATION.md decision
#: entry; never by hand. To regenerate after an intentional change:
#:
#:   python -c "from tests.golden.test_dhan_constants_frozen import \
#:               _canonical_hash; print(_canonical_hash())"
#:
#: Then paste the new value here and add a MIGRATION.md entry that
#: explains WHY the 4-tuple changed.
_GOLDEN_SHA256: str = "6499c34c612a89861980321d8f24d3eabcbd4e3214c9b130d12b517781c96176"


def _canonical_payload() -> list[dict[str, Any]]:
    return [dataclasses.asdict(inst) for inst in ALL_SPOT_INSTRUMENTS]


def _canonical_hash() -> str:
    payload = _canonical_payload()
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@pytest.mark.golden
def test_all_spot_instruments_sha256_is_frozen() -> None:
    """The 4-tuple sha256 matches the locked golden value.

    If this fails:
      1. Confirm the underlying change is intentional (e.g., Dhan
         re-assigned a securityId — verify against
         web.dhan.co/Charts?secid=N).
      2. Open a MIGRATION.md entry under a new DEC-P0-NNN ID.
      3. Update ``_GOLDEN_SHA256`` above to the new hash.
    """
    actual = _canonical_hash()
    assert actual == _GOLDEN_SHA256, (
        f"DhanSpotInstrument 4-tuple changed.\n"
        f"  expected sha256: {_GOLDEN_SHA256}\n"
        f"  actual sha256:   {actual}\n"
        f"  payload:         {json.dumps(_canonical_payload(), indent=2)}\n"
        "If this change is intentional, update _GOLDEN_SHA256 + open a "
        "MIGRATION.md decision entry. If not, revert the change to "
        "brutex/adapters/dhan/constants.py."
    )


@pytest.mark.golden
def test_canonical_payload_shape() -> None:
    """The serialization shape is locked alongside the hash.

    Adding a 6th field to DhanSpotInstrument (or removing one) is allowed
    — but it MUST flip the golden hash AND surface here as an extra/missing
    key, so the reviewer can't miss it.
    """
    payload = _canonical_payload()
    assert len(payload) == 4
    expected_keys = {
        "symbol",
        "dhan_csv_symbol",
        "security_id",
        "exchange",
        "segment",
        "instrument_type",
    }
    for entry in payload:
        assert set(entry.keys()) == expected_keys, (
            f"DhanSpotInstrument field set changed. expected={expected_keys}, "
            f"actual={set(entry.keys())}. Update _GOLDEN_SHA256 + MIGRATION.md."
        )
