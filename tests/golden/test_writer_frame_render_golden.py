"""Frozen frame-render byte golden — the LEGACY ``_render_bytes`` law, verbatim.

DEC-PREBUILD-FRAME-WRITE-001 §2 item 7(b): BEFORE the one-encoder Seam-D split
(``_render_bytes`` → ``_frame_from_bars`` + ``_render_frame_bytes``) lands, this
file freezes the LEGACY render byte contract as an INLINE VERBATIM ORACLE (the
DEC-SWEEP-COMPUTE-FOLD-DECOMP-001 / MIGRATION.md #1685 frozen-oracle pattern —
polars-version-independent, because BOTH sides run on the installed polars) and
bit-compares the production :meth:`ParquetWriter._render_bytes` against it over
a hostile corpus:

* plain spot bars (2dp Decimals, ``open_interest=0`` — the derived-intraday
  shape);
* bars seeded from LEGACY OFF-GRID float reprs (``Decimal("14020.7999")`` …—
  the V3 §9.2 class a pre-DEC-SPOT-PRICE-2DP-001 partition holds; the render
  ``float(Decimal)`` cast must reproduce those bytes too);
* null-OI F&O bars (``open_interest=None`` — the nullable Int64 column);
* mixed null/valued OI in ONE batch;
* a Greeks-enriched :class:`FnoGreeksBar` batch (the 17-column overlay arm);
* a deep 5,000-row batch (row-group / page interactions under the fixed
  ``row_group_size=100_000``).

The five encoder knobs (zstd · level 3 · row_group_size 100_000 ·
``use_pyarrow=False`` · ``statistics=False``), the 7-column order and the exact
dtypes are FROZEN AS LITERALS inside the oracle (never imported from the
production module), so a production knob/schema drift FAILS this golden loudly
(V3 C2). After the Seam-D refactor this file must pass UNMODIFIED — that is the
byte-preservation proof for the moved lines.

Whitebox: exercises the private ``_render_bytes``, so per the DEC-P0-017
convention the file silences ``reportPrivateUsage`` at the top.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from brutex.data.models import CandleBar, FnoGreeksBar
from brutex.data.parquet_writer import ParquetWriter

pytest.importorskip("polars")

import polars as pl

#: The overlay field names + order FROZEN as literals (the models.py canonical
#: order at the freeze instant) — a production reorder must fail this golden,
#: never silently re-freeze it.
_FROZEN_OVERLAY_FIELDS: tuple[str, ...] = (
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "spot_at_bar",
    "t_years_used",
    "rate_used",
    "greeks_provenance_id",
)


def _legacy_render_bytes_oracle(sorted_bars: list[CandleBar]) -> bytes:
    """The PRE-Seam-D ``ParquetWriter._render_bytes`` body, frozen VERBATIM.

    Copied token-for-token from ``brutex/data/parquet_writer.py`` at the
    pre-change tree (branch base ``origin/main`` @ 7f89590) with ONLY the
    ``lake_write_guard`` wrapper dropped (an exception-mapping context manager —
    no byte effect on a successful render) and the module constants inlined as
    literals (so the oracle can never drift WITH the production code). This is
    the byte law every later render implementation must reproduce.
    """
    rows: dict[str, list[object]] = {
        "timestamp": [b.timestamp for b in sorted_bars],
        "open": [float(b.open) for b in sorted_bars],
        "high": [float(b.high) for b in sorted_bars],
        "low": [float(b.low) for b in sorted_bars],
        "close": [float(b.close) for b in sorted_bars],
        "volume": [b.volume for b in sorted_bars],
        "open_interest": [b.open_interest for b in sorted_bars],
    }
    schema: dict[str, pl.DataType | type[pl.DataType]] = {
        "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
        "open": pl.Float64,
        "high": pl.Float64,
        "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Int64,
        "open_interest": pl.Int64,
    }
    if any(isinstance(b, FnoGreeksBar) for b in sorted_bars):
        for name in _FROZEN_OVERLAY_FIELDS:
            if name == "greeks_provenance_id":
                rows[name] = [getattr(b, name, None) for b in sorted_bars]
                schema[name] = pl.Int32
            elif name == "spot_at_bar":
                rows[name] = [
                    None if (v := getattr(b, name, None)) is None else float(v) for b in sorted_bars
                ]
                schema[name] = pl.Float64
            else:
                rows[name] = [getattr(b, name, None) for b in sorted_bars]
                schema[name] = pl.Float64
    df = pl.DataFrame(rows, schema=schema)

    buffer: list[bytes] = []
    bio = io.BytesIO()
    df.write_parquet(
        bio,
        compression="zstd",
        compression_level=3,  # frozen literal — ZSTD_LEVEL at the freeze instant
        row_group_size=100_000,  # frozen literal — ROW_GROUP_SIZE at the freeze instant
        use_pyarrow=False,
        statistics=False,
    )
    buffer.append(bio.getvalue())
    return b"".join(buffer)


_T0 = datetime(2024, 3, 4, 3, 45, tzinfo=UTC)  # 09:15 IST, a regular NSE session


def _bar(
    i: int,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: int = 100,
    oi: int | None = 0,
) -> CandleBar:
    return CandleBar(
        timestamp=_T0 + timedelta(minutes=i),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        open_interest=oi,
    )


def _plain_spot_corpus() -> list[CandleBar]:
    """2dp-snapped spot bars — the derived-intraday shape (OI = 0, never null)."""
    return [
        _bar(0, open_="100.00", high="101.25", low="99.90", close="100.75"),
        _bar(1, open_="100.75", high="102.00", low="100.10", close="101.95", volume=250),
        _bar(2, open_="101.95", high="101.95", low="101.95", close="101.95", volume=0),
    ]


def _off_grid_corpus() -> list[CandleBar]:
    """Bars seeded from LEGACY OFF-GRID float reprs (the V3 §9.2 boundary class).

    ``_render_bytes`` is a pure function over its input bars — a pre-DEC lake
    read through a path that does NOT snap would hand it exactly these
    Decimals, so the float-cast law must be pinned on them too.
    """
    return [
        _bar(0, open_="14020.7999", high="14021.0001", low="14019.5", close="14020.8"),
        _bar(1, open_="100.005", high="100.015", low="99.995", close="100.005"),
        _bar(2, open_="0.00499999999", high="2.675", low="0.0001", close="1.005"),
    ]


def _null_oi_corpus() -> list[CandleBar]:
    """F&O bars with a NULL open_interest (the nullable Int64 column arm)."""
    return [
        _bar(0, open_="55.10", high="56.00", low="55.00", close="55.85", oi=None),
        _bar(1, open_="55.85", high="57.25", low="55.50", close="57.00", oi=None),
    ]


def _mixed_oi_corpus() -> list[CandleBar]:
    """Null + valued OI in ONE batch (null slots inside a non-null column)."""
    return [
        _bar(0, open_="55.10", high="56.00", low="55.00", close="55.85", oi=None),
        _bar(1, open_="55.85", high="57.25", low="55.50", close="57.00", oi=1500),
        _bar(2, open_="57.00", high="57.10", low="56.80", close="56.95", oi=0),
    ]


def _overlay_corpus() -> list[CandleBar]:
    """A Greeks-enriched batch — the 17-column overlay render arm."""
    plain = _bar(0, open_="120.00", high="121.00", low="119.50", close="120.50", oi=200)
    enriched = FnoGreeksBar(
        timestamp=_T0 + timedelta(minutes=1),
        open=Decimal("120.50"),
        high=Decimal("122.00"),
        low=Decimal("120.25"),
        close=Decimal("121.75"),
        volume=340,
        open_interest=250,
        iv=0.1834,
        delta=0.5521,
        gamma=0.0012,
        theta=-4.21,
        vega=8.77,
        rho=1.02,
        spot_at_bar=Decimal("21510.55"),
        t_years_used=0.0451,
        rate_used=0.065,
        greeks_provenance_id=7,
    )
    return [plain, enriched]


def _deep_corpus() -> list[CandleBar]:
    """5,000 deterministic rows — page/row-group interaction under the fixed knobs."""
    return [
        _bar(
            i,
            open_=f"{100 + (i % 251)}.{i % 100:02d}",
            high=f"{400 + (i % 251)}.{(i * 7) % 100:02d}",
            low=f"{50 + (i % 41)}.{(i * 3) % 100:02d}",
            close=f"{100 + ((i + 13) % 251)}.{(i * 11) % 100:02d}",
            volume=i,
            oi=i % 5000,
        )
        for i in range(5000)
    ]


_CORPORA = {
    "plain_spot": _plain_spot_corpus,
    "off_grid": _off_grid_corpus,
    "null_oi": _null_oi_corpus,
    "mixed_oi": _mixed_oi_corpus,
    "overlay": _overlay_corpus,
    "deep": _deep_corpus,
}


@pytest.mark.parametrize("name", sorted(_CORPORA))
def test_render_bytes_matches_frozen_legacy_oracle(name: str) -> None:
    """Production ``_render_bytes`` ≡ the frozen verbatim legacy oracle, byte-for-byte."""
    bars = _CORPORA[name]()
    produced = ParquetWriter._render_bytes(bars)
    expected = _legacy_render_bytes_oracle(bars)
    assert hashlib.sha256(produced).hexdigest() == hashlib.sha256(expected).hexdigest()
    assert produced == expected


def test_render_bytes_deterministic_across_calls() -> None:
    """Two renders of the SAME bars are byte-identical (the idempotence base law)."""
    bars = _deep_corpus()
    assert ParquetWriter._render_bytes(bars) == ParquetWriter._render_bytes(bars)


def test_oracle_decodes_to_expected_schema() -> None:
    """Anti-vacuity: the frozen oracle really renders the pinned 7-column shape."""
    blob = _legacy_render_bytes_oracle(_plain_spot_corpus())
    df = pl.read_parquet(io.BytesIO(blob))
    assert df.columns == [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
    ]
    assert df.schema["timestamp"] == pl.Datetime(time_unit="us", time_zone="UTC")
    assert df.schema["open"] == pl.Float64
    assert df.schema["volume"] == pl.Int64
    assert df.schema["open_interest"] == pl.Int64
