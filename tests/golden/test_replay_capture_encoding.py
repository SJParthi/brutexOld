"""Golden pin — the canonical replay-capture bar ENCODING (P-20).

DEC-AUDIT-LAKE-REPLAY-ORACLE-001. Locks the byte-exact wire form of the
capture-mode ``bars`` payload: the 7-element per-bar arrays produced by
:func:`brutex.data.audit_capture.encode_bars`
(``[epoch_s:int, open:str, high:str, low:str, close:str, volume:int,
oi:int|null]`` — prices as ``str(Decimal)``, never JSON floats), the
canonical-JSON serialisation those arrays take on the hash-chained audit
line, and the :func:`~brutex.data.audit_capture.input_blake3` digest stamped
into every ``LAKE_APPEND_WRITTEN`` event. Any drift in the encoding silently
re-keys every stored ``input_blake3`` and breaks replay of historic capture
logs — exactly the trip-wire this golden exists to spring.

Deliberately NOT pinned: any Parquet ``raw_hash``. Writer bytes are
deterministic per environment, not warranted across polars/zstd bumps
(P-19's own contract) — pinning one here would be a cross-version landmine
(design §4, stated reason).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from brutex.data.audit_capture import decode_bars, encode_bars, input_blake3
from brutex.data.models import CandleBar
from brutex.orchestrator.audit import _canonical_json  # pyright: ignore[reportPrivateUsage]

# ── Locked golden answer-keys ───────────────────────────────────────────

#: The frozen two-bar fixture's encoded rows (input order preserved).
_GOLDEN_ENCODED: list[list[object]] = [
    [1609731900, "14020.79", "14031.10", "14019.05", "14030.00", 1200, None],
    [1609731960, "14030.00", "14042.35", "14025.50", "14040.15", 980, 42],
]

#: The canonical-JSON wire form the chain stores (sorted keys are moot for
#: arrays; separators compact; NaN/Inf refused upstream).
_GOLDEN_JSON = (
    '[[1609731900,"14020.79","14031.10","14019.05","14030.00",1200,null],'
    '[1609731960,"14030.00","14042.35","14025.50","14040.15",980,42]]'
)

#: blake3 hex over the canonical JSON of the full fixture / its first bar /
#: the empty batch — three anchors so a digest change can never hide.
_GOLDEN_BLAKE3 = "1965d5631877576a3c4804a17f385fd1e79f04996a088428165b00884aee6ea8"
_GOLDEN_BLAKE3_FIRST = "46a9d533c4008ac378cdfe99fd2fa8c8c1deb841932cc38354db9e78b42a7192"
_GOLDEN_BLAKE3_EMPTY = "d53d18c23212ea7b6300594bb89bce60218f6eff2b9d628b8cc42d3e79bbd5ab"


def _fixture_bars() -> list[CandleBar]:
    """The frozen two-bar fixture (whole-second UTC, exact Decimals, OI None/int)."""
    return [
        CandleBar(
            timestamp=datetime(2021, 1, 4, 3, 45, tzinfo=UTC),
            open=Decimal("14020.79"),
            high=Decimal("14031.10"),
            low=Decimal("14019.05"),
            close=Decimal("14030.00"),
            volume=1200,
            open_interest=None,
        ),
        CandleBar(
            timestamp=datetime(2021, 1, 4, 3, 46, tzinfo=UTC),
            open=Decimal("14030.00"),
            high=Decimal("14042.35"),
            low=Decimal("14025.50"),
            close=Decimal("14040.15"),
            volume=980,
            open_interest=42,
        ),
    ]


def test_encoded_rows_match_golden() -> None:
    """The 7-element tuple encoding is byte-for-byte the locked answer key."""
    assert encode_bars(_fixture_bars()) == _GOLDEN_ENCODED


def test_canonical_json_wire_form_matches_golden() -> None:
    """The chain's canonical-JSON serialisation of the encoding is frozen."""
    assert _canonical_json(encode_bars(_fixture_bars())) == _GOLDEN_JSON


def test_input_blake3_matches_golden() -> None:
    """The per-event input digest is frozen (full / first-bar / empty)."""
    encoded = encode_bars(_fixture_bars())
    assert input_blake3(encoded) == _GOLDEN_BLAKE3
    assert input_blake3(encoded[:1]) == _GOLDEN_BLAKE3_FIRST
    assert input_blake3([]) == _GOLDEN_BLAKE3_EMPTY


def test_golden_encoding_round_trips_exactly() -> None:
    """decode(golden rows) reproduces the fixture bars exactly (Decimal/None)."""
    assert decode_bars(_GOLDEN_ENCODED) == _fixture_bars()
