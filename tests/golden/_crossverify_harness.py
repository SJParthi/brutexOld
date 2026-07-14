"""Shared golden harness for the cross-verify CSV byte contract.

DEC-CROSSVERIFY-PRODUCER-001 §9.2. Builds a deterministic golden lake through
the REAL :class:`~brutex.data.sink.ParquetBarSink` (the production write path
— 2dp tick-snap included) and renders each symbol through the REAL export
chain (:func:`read_day_bars` → gate → :func:`export_symbol`). Imported by
BOTH the generator script and the golden test so neither copies the build
(the ``_preopen_prevday_harness`` precedent).

The gate binding uses the contract-faithful fake twin
(:func:`tests.fakes.fake_completeness_gate.faithful_classify`) until the
sibling gate module (MIGRATION.md #1774) merges — the golden pins RENDER
bytes; the per-class gate laws are the sibling's own suite's job.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

from brutex.calendars.nse import get_session_checked
from brutex.crossverify.canonical import CanonicalIdentity, canonical_identity, s3_key
from brutex.crossverify.day_export import SymbolExport, export_symbol, make_day_frame_reader
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink

from tests.fakes.fake_completeness_gate import faithful_classify

if TYPE_CHECKING:
    from collections.abc import Iterable

#: The golden day — verified a REGULAR NSE/BSE session (Friday, 375 minutes).
GOLDEN_DAY: Final[date] = date(2026, 7, 10)

#: Fixture home: ``tests/golden/fixtures/crossverify/``.
FIXTURES_DIR: Final[Path] = Path(__file__).parent / "fixtures" / "crossverify"

#: The pinned S3 key strings fixture (one per line, sorted).
S3_KEYS_FIXTURE: Final[Path] = FIXTURES_DIR / "s3_keys.txt"

#: The PUBLISHED golden symbols (fixture stem == canonical symbol) — SPOT
#: ONLY since DEC-CROSSVERIFY-SPOT-ONLY-001 (MIGRATION.md #1788); these are
#: the symbols whose S3 keys the producer contract pins (s3_keys.txt).
GOLDEN_SYMBOLS: Final[tuple[str, ...]] = (
    "NIFTY",
    "SENSEX",
    "RELIANCE",
    "BAJAJ-AUTO",
    "M&M",
)

#: STRUCTURAL renderer goldens (#1788): the FUT export chain is retained
#: direct-unit-covered but UNREACHABLE from the spot-only producer — these
#: byte pins freeze the oi-int render law for the contract's possible
#: re-expansion; their keys are deliberately ABSENT from s3_keys.txt.
STRUCTURAL_FUT_SYMBOLS: Final[tuple[str, ...]] = (
    "NIFTY-2026-07-30-FUT",
    "SENSEX-2026-09-24-FUT",
)

#: The negative golden: a 3-row truncated INDEX day (must be UNPUBLISHABLE).
TRUNCATED_INDEX_SYMBOL: Final[str] = "BANKNIFTY"

_SESSION_MINUTES: Final[int] = 375


def _identity(exchange: str, segment: str, symbol: str) -> CanonicalIdentity:
    identity = canonical_identity(exchange=exchange, segment=segment, lake_symbol=symbol)
    if identity is None:  # defensive — every golden coordinate maps
        raise AssertionError(f"golden coordinate failed to map: {exchange}/{segment}/{symbol}")
    return identity


#: Every golden coordinate, keyed by canonical symbol.
GOLDEN_IDENTITIES: Final[dict[str, CanonicalIdentity]] = {
    "NIFTY": _identity("NSE", "INDEX", "NIFTY"),
    "SENSEX": _identity("BSE", "INDEX", "SENSEX"),
    "RELIANCE": _identity("NSE", "CASH", "RELIANCE"),
    "BAJAJ-AUTO": _identity("NSE", "CASH", "BAJAJ-AUTO"),
    "M&M": _identity("NSE", "CASH", "M&M"),
    "NIFTY-2026-07-30-FUT": _identity("NSE", "FNO", "NSE-NIFTY-30Jul26-FUT"),
    "SENSEX-2026-09-24-FUT": _identity("BSE", "FNO", "BSE-SENSEX-24Sep26-FUT"),
    TRUNCATED_INDEX_SYMBOL: _identity("NSE", "INDEX", "BANKNIFTY"),
}


def _minute_open_utc(minute_index: int) -> datetime:
    """The UTC instant of session minute *minute_index* (0 = 09:15 IST)."""
    return datetime(
        GOLDEN_DAY.year, GOLDEN_DAY.month, GOLDEN_DAY.day, 3, 45, tzinfo=UTC
    ) + timedelta(minutes=minute_index)


def _bars(
    *,
    base_price: str,
    minutes: Iterable[int],
    oi_base: int | None,
    zero_volume_minutes: frozenset[int] = frozenset(),
) -> list[CandleBar]:
    """Deterministic bars: price walks 5 paisa/minute off *base_price*."""
    base = Decimal(base_price)
    out: list[CandleBar] = []
    for minute in minutes:
        open_ = base + Decimal(minute) * Decimal("0.05")
        oi: int | None = None
        if oi_base is not None:
            # Minute 0 carries oi=0 — the pinned `oi=0 renders 0` row.
            oi = 0 if minute == 0 else oi_base + minute
        out.append(
            CandleBar(
                timestamp=_minute_open_utc(minute),
                open=open_,
                high=open_ + Decimal("0.30"),
                low=open_ - Decimal("0.25"),
                close=open_ + Decimal("0.10"),
                volume=0 if minute in zero_volume_minutes else 100 + minute,
                open_interest=oi,
            )
        )
    return out


def build_golden_lake(lake_root: Path) -> None:
    """Write every golden symbol's 1-minute day through the REAL sink."""
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    full = range(_SESSION_MINUTES)
    plans: list[tuple[str, list[CandleBar]]] = [
        ("NIFTY", _bars(base_price="24000.00", minutes=full, oi_base=None)),
        ("SENSEX", _bars(base_price="80000.00", minutes=full, oi_base=None)),
        (
            # A missing minute (09:47 = index 32) + a volume=0 minute (10:15 =
            # index 60): honest absence, never fabrication — CASH publishes.
            "RELIANCE",
            _bars(
                base_price="1400.00",
                minutes=(m for m in full if m != 32),
                oi_base=None,
                zero_volume_minutes=frozenset({60}),
            ),
        ),
        ("BAJAJ-AUTO", _bars(base_price="8010.00", minutes=range(3), oi_base=None)),
        ("M&M", _bars(base_price="2900.00", minutes=range(3), oi_base=None)),
        ("NIFTY-2026-07-30-FUT", _bars(base_price="24100.00", minutes=full, oi_base=50_000)),
        (
            # STRUCTURAL BSE_FNO fixture (synthetic — never pulled in v1).
            "SENSEX-2026-09-24-FUT",
            _bars(base_price="80500.00", minutes=range(3), oi_base=1_000),
        ),
        (
            # The NEGATIVE golden: a 3-row truncated index day.
            TRUNCATED_INDEX_SYMBOL,
            _bars(base_price="52000.00", minutes=range(3), oi_base=None),
        ),
    ]
    for symbol, bars in plans:
        identity = GOLDEN_IDENTITIES[symbol]
        sink.append(
            exchange=identity.exchange,
            segment=identity.lake_segment,
            symbol=identity.lake_symbol,
            interval="1minute",
            bars=bars,
        )


def golden_exports(lake_root: Path) -> dict[str, SymbolExport]:
    """Run the REAL export chain over the golden lake, keyed by symbol."""
    session = get_session_checked(GOLDEN_DAY)
    if session is None:  # defensive — GOLDEN_DAY is a verified session
        raise AssertionError(f"{GOLDEN_DAY} is not a trading day")
    reader = make_day_frame_reader(lake_root)
    return {
        symbol: export_symbol(
            identity=identity,
            day=GOLDEN_DAY,
            session=session,
            reader=reader,
            classifier=faithful_classify,
        )
        for symbol, identity in GOLDEN_IDENTITIES.items()
    }


def golden_key_lines(exports: dict[str, SymbolExport]) -> str:
    """The pinned S3 key strings for the PUBLISHED (spot) goldens — sorted.

    SPOT ONLY (#1788): the STRUCTURAL_FUT_SYMBOLS renderer goldens never
    contribute a key — the producer publishes no futures files.
    """
    keys = sorted(
        s3_key(prefix="crossverify/groww", day=GOLDEN_DAY, identity=exports[s].identity)
        for s in GOLDEN_SYMBOLS
    )
    return "\n".join(keys) + "\n"


__all__ = [
    "FIXTURES_DIR",
    "GOLDEN_DAY",
    "GOLDEN_IDENTITIES",
    "GOLDEN_SYMBOLS",
    "S3_KEYS_FIXTURE",
    "STRUCTURAL_FUT_SYMBOLS",
    "TRUNCATED_INDEX_SYMBOL",
    "build_golden_lake",
    "golden_exports",
    "golden_key_lines",
]
