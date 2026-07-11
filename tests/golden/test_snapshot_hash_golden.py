"""Golden preimage pin — the ``bxsnap2`` snapshot hash (DEC-SNAPSHOT-HASH-FULL-001).

The tripwire for SILENT preimage drift: ``data_snapshot_hash`` is the shared
run-identity input (Golden Rule #4 — ``blake3(… ‖ data_snapshot_hash ‖ …)``,
TRACK1_BACKTEST_SPEC.md run identity), so ANY change to its byte layout
(domain tag, field order, separator, Decimal rendering, ``None`` OI rendering)
re-keys every dedup/cache/store identity. That re-key must always be a
DELIBERATE, decision-ID'd act — this pin makes an accidental one fail loudly.

The fixture is fully deterministic (fixed IST timestamps, Decimal literals,
one ``None`` and two integer open-interest values) so the expected hex is
reproducible byte-for-byte on any platform.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Final

from brutex.data.models import CandleBar
from brutex.engine.snapshot_hash import data_snapshot_hash

_IST = timezone(timedelta(hours=5, minutes=30))

#: The pinned bxsnap2 digest of the fixture below. Regenerating this hex is a
#: PREIMAGE-VERSION change — it requires a new decision ID + the migration
#: note that every dedup/cache identity goes cold (the v1→v2 precedent).
_EXPECTED_V2_HEX: Final[str] = "dd776d5204b72c8857b029cd207a357142454c55cc830850336a8596afb05f8b"


def _fixture_bars() -> list[CandleBar]:
    return [
        CandleBar(
            timestamp=datetime(2026, 1, 5, 9, 16 + i, tzinfo=_IST).astimezone(UTC),
            open=Decimal("100.00"),
            high=Decimal("102.00"),
            low=Decimal("99.00"),
            close=Decimal("100.00") + Decimal(i),
            volume=1000 + i,
            open_interest=None if i == 0 else 10 * i,
        )
        for i in range(3)
    ]


def test_bxsnap2_preimage_is_pinned() -> None:
    digest = data_snapshot_hash(
        exchange="NSE",
        segment="INDEX",
        start="2026-01-01",
        end="2026-12-31",
        slices={("NIFTY", "1minute"): _fixture_bars()},
    )
    assert digest == _EXPECTED_V2_HEX
