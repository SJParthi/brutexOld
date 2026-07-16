"""Wave-D poison-corpus builders (ADR-0020 slice G9, DEC-LAKE-RUST-WAVED-HARNESS-001).

One :class:`PoisonCase` per known ON-DISK poison class of the lake scan/heal
surface (`brutex/data/lake_invariants.py` + `brutex/data/lake_heal.py`), each
carrying a builder that seeds ONE poisoned lake under a caller-supplied lake
root. The corpus is the shared input of the Wave-D DIFFERENTIAL harness
(`tests/integration/test_waved_poison_corpus_differential.py`): the future
Rust twin of the scanner must produce a normalized verdict EQUAL to the
Python oracle's over every case here before its flag may flip
(`docs/runbooks/wave-d-poison-corpus.md`).

Builder primitives are HOISTED COPIES of the proven fixture builders in
``tests/unit/test_lake_invariants.py`` / ``tests/unit/test_lake_heal.py``
(a test module is never imported from — the house test-doubles law; the
copies live here so BOTH suites can converge on this module later). Poison
is planted with RAW pyarrow because the fixed writers refuse to store
duplicates/foreign rows (#1067) — exactly like the operator's lake; clean
data is seeded through the REAL :class:`~brutex.data.parquet_writer.ParquetWriter`.

Determinism laws: fixed 2024-06-anchored timestamps (the ``_ts`` idiom), no
clocks, no network, every path under the supplied lake root (or its
``<name>.outside`` sibling for symlink-escape shapes).

RUNTIME-INJECTION classes (mid-heal TOCTOU swaps, PanicException at L3) are
deliberately OUT of this corpus — they are behaviors injected DURING a run,
not on-disk shapes; they stay pinned by ``tests/unit/test_lake_heal.py``
(see the readiness doc §5).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partition import build_partition_path

# ────────────────────────────────────────────────────────────────────────
# Manifest
# ────────────────────────────────────────────────────────────────────────

_EMPTY_KWARGS: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class PoisonCase:
    """ONE poison class: a lake builder + the expected scan verdict shape.

    ``expected_finding_types`` is the EXACT set of finding class NAMES the
    scan must emit over the built lake (``frozenset()`` for skip-only /
    clean-by-design shapes); ``expects_skip`` asserts at least one counted
    skip (``files_skipped + option_files_skipped > 0``); ``scan_kwargs`` is
    splatted into the scanner call (e.g. ``canonical_keys``); ``requires``
    names an optional filesystem capability (``"symlink"``) the runner
    skips on when unsupported.
    """

    name: str
    build: Callable[[Path], None]
    expected_finding_types: frozenset[str]
    expects_skip: bool = False
    scan_kwargs: Mapping[str, Any] = _EMPTY_KWARGS
    requires: str | None = None


def supports_symlink(base: Path) -> bool:
    """True iff the filesystem under *base* can create symlinks."""
    probe = base / ".symlink-probe"
    target = base / ".symlink-probe-target"
    try:
        os.symlink(target, probe)
    except OSError:
        return False
    probe.unlink()
    return True


# ────────────────────────────────────────────────────────────────────────
# Hoisted primitives (verbatim shapes from the unit suites)
# ────────────────────────────────────────────────────────────────────────


def _ts(minute: int, *, year: int = 2024, month: int = 6, day: int = 3) -> datetime:
    """A UTC 1-minute instant inside the 2024-06 partition by default."""
    return datetime(year, month, day, 4, minute, tzinfo=UTC)


def _bar(ts: datetime, *, close: str = "100.00", volume: int = 10) -> CandleBar:
    c = Decimal(close)
    return CandleBar(timestamp=ts, open=c, high=c, low=c, close=c, volume=volume, open_interest=None)


def _write_bars(
    lake: Path,
    bars: Sequence[CandleBar],
    *,
    exchange: str = "NSE",
    segment: str = "INDEX",
    symbol: str = "NIFTY",
    interval: str = "1minute",
) -> None:
    """Seed bars through the REAL writer, one write per UTC month group."""
    writer = ParquetWriter(lake_root=lake)
    groups: dict[tuple[int, int], list[CandleBar]] = {}
    for bar in bars:
        key = (bar.timestamp.year, bar.timestamp.month)
        groups.setdefault(key, []).append(bar)
    for _, group in sorted(groups.items()):
        writer.write(exchange=exchange, segment=segment, symbol=symbol, interval=interval, bars=group)


def _dest(
    lake: Path,
    *,
    exchange: str = "NSE",
    segment: str = "INDEX",
    symbol: str = "NIFTY",
    interval: str = "1minute",
    year: int = 2024,
    month: int = 6,
) -> Path:
    return build_partition_path(
        lake_root=lake,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=interval,
        year=year,
        month=month,
    )


def _opt_dest(lake: Path, name: str, *, series: str = "SERIES") -> Path:
    return lake / "options" / series / f"{name}.parquet"


def _outside_dir(lake: Path) -> Path:
    """A sibling dir OUTSIDE the lake root (the symlink-escape target home)."""
    outside = lake.parent / f"{lake.name}.outside"
    outside.mkdir(parents=True, exist_ok=True)
    return outside


def _plant_raw(dest: Path, timestamps: Sequence[datetime | None], *, price: float = 100.0) -> None:
    """Write a RAW pyarrow bar partition VERBATIM (dups/foreign rows preserved).

    The fixed writer refuses to store duplicates (#1067), so poison must be
    planted with raw pyarrow, exactly like the operator's lake.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = len(timestamps)
    prices = [price] * n
    table = pa.table(
        {
            "timestamp": pa.array(list(timestamps), type=pa.timestamp("us", tz="UTC")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([10] * n, type=pa.int64()),
            "open_interest": pa.array([None] * n, type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _plant_raw_naive(dest: Path, timestamps: Sequence[datetime], *, price: float = 100.0) -> None:
    """The ``_plant_raw`` twin with a tz-NAIVE timestamp column (routed as-if-UTC)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = len(timestamps)
    prices = [price] * n
    table = pa.table(
        {
            "timestamp": pa.array(list(timestamps), type=pa.timestamp("us")),
            "open": pa.array(prices, type=pa.float64()),
            "high": pa.array(prices, type=pa.float64()),
            "low": pa.array(prices, type=pa.float64()),
            "close": pa.array(prices, type=pa.float64()),
            "volume": pa.array([10] * n, type=pa.int64()),
            "open_interest": pa.array([None] * n, type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _plant_raw_ohlc(dest: Path, rows: Sequence[tuple[datetime, float, float, float, float]]) -> None:
    """Write a RAW bar partition with explicit per-row OHLC (poison-capable)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "timestamp": pa.array([r[0] for r in rows], type=pa.timestamp("us", tz="UTC")),
            "open": pa.array([r[1] for r in rows], type=pa.float64()),
            "high": pa.array([r[2] for r in rows], type=pa.float64()),
            "low": pa.array([r[3] for r in rows], type=pa.float64()),
            "close": pa.array([r[4] for r in rows], type=pa.float64()),
            "volume": pa.array([10] * len(rows), type=pa.int64()),
            "open_interest": pa.array([None] * len(rows), type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _plant_option_raw(dest: Path, rows: Sequence[tuple[str | None, datetime | None]]) -> None:
    """Write a RAW options-tree file with only the two scanner key columns."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "side": pa.array([side for side, _ in rows], type=pa.string()),
            "timestamp": pa.array([ts for _, ts in rows], type=pa.timestamp("us", tz="UTC")),
        }
    )
    pq.write_table(table, dest)


def _plant_option_nested_side(
    dest: Path, timestamps: Sequence[datetime], *, side_kind: str = "list"
) -> None:
    """A RAW options file whose ``side`` column carries a NESTED dtype.

    ``side_kind="list"`` writes ``List(Utf8)`` (values read back as Python
    lists), ``"struct"`` writes ``Struct`` (dicts) — both UNHASHABLE, the
    round-12 crash class the dtype gate exists for.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = len(timestamps)
    if side_kind == "list":
        side = pa.array([["CE"]] * n, type=pa.list_(pa.string()))
    else:
        side = pa.array([{"v": "CE"}] * n, type=pa.struct([("v", pa.string())]))
    table = pa.table(
        {
            "side": side,
            "timestamp": pa.array(list(timestamps), type=pa.timestamp("us", tz="UTC")),
        }
    )
    pq.write_table(table, dest)


def _plant_option_out_of_range(dest: Path) -> None:
    """A RAW options file whose SOUND ``Datetime`` column holds an i64 value
    no Python ``datetime`` can represent (``2**62`` µs ≈ year 146,000) — the
    round-18 OverflowError/PanicException crash class.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "side": pa.array(["CE"], type=pa.string()),
            "timestamp": pa.array([2**62], type=pa.int64()).cast(pa.timestamp("us", tz="UTC")),
        }
    )
    pq.write_table(table, dest)


def _plant_bars_out_of_range(dest: Path) -> None:
    """The bars twin of :func:`_plant_option_out_of_range` (full 7-column schema)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "timestamp": pa.array([2**62], type=pa.int64()).cast(pa.timestamp("us", tz="UTC")),
            "open": pa.array([100.0], type=pa.float64()),
            "high": pa.array([100.0], type=pa.float64()),
            "low": pa.array([100.0], type=pa.float64()),
            "close": pa.array([100.0], type=pa.float64()),
            "volume": pa.array([10], type=pa.int64()),
            "open_interest": pa.array([None], type=pa.int64()),
        }
    )
    pq.write_table(table, dest)


def _seed_mass_foreign_option_series(lake: Path) -> None:
    """21 raw option month files under ONE series, 11 each carrying ONE
    foreign 2019-12 row (> 50% of ≥ ``MASS_HEAL_MIN_FILES`` — the heal-side
    mass-rail fixture shape, hoisted from ``tests/unit/test_lake_heal.py``).
    """
    series = lake / "options" / "SERIES"
    series.mkdir(parents=True, exist_ok=True)
    for idx in range(21):
        year = 2020 + idx // 12
        month = 1 + idx % 12
        home_ts = datetime(year, month, 3, 4, 0, tzinfo=UTC)
        rows: list[datetime] = [home_ts]
        if idx < 11:  # 11 of 21 carry ONE foreign row
            rows.append(datetime(2019, 12, 2, 4, 0, tzinfo=UTC))
        pq.write_table(
            pa.table(
                {
                    "side": pa.array(["CE"] * len(rows), type=pa.string()),
                    "timestamp": pa.array(rows, type=pa.timestamp("us", tz="UTC")),
                }
            ),
            series / f"{year:04d}-{month:02d}.parquet",
        )


# ────────────────────────────────────────────────────────────────────────
# Case builders — bars tree
# ────────────────────────────────────────────────────────────────────────


def build_bars_intra_file_dup(lake: Path) -> None:
    _plant_raw(_dest(lake), [_ts(1), _ts(1), _ts(2), _ts(3)])


def build_bars_foreign_month_pure(lake: Path) -> None:
    # A June bar planted in the JULY file; no June file exists (overlap_rows=0).
    _plant_raw(_dest(lake, month=7), [_ts(1), _ts(2, month=7, day=1), _ts(3, month=7, day=1)])


def build_bars_foreign_month_overlap(lake: Path) -> None:
    # The SAME June key lives in BOTH the (real-writer) June file and the July stray.
    _write_bars(lake, [_bar(_ts(1)), _bar(_ts(2))])
    _plant_raw(_dest(lake, month=7), [_ts(1), _ts(3, month=7, day=1)])


def build_legacy_key_drift_sibling(lake: Path) -> None:
    _write_bars(lake, [_bar(_ts(1))], segment="CASH", symbol="M&M")
    _write_bars(lake, [_bar(_ts(2))], segment="CASH", symbol="NSE-M&M")


def build_legacy_key_drift_canonical_keys(lake: Path) -> None:
    # No canonical sibling dir — the injected instrument-master key universe
    # (scan_kwargs canonical_keys={"M&M"}) resolves the drift.
    _write_bars(lake, [_bar(_ts(2))], segment="CASH", symbol="NSE-M&M")


def build_bars_naive_timestamp_routing(lake: Path) -> None:
    # A tz-NAIVE column: routed as-if-UTC, never raises — the July row is foreign.
    _plant_raw_naive(_dest(lake), [datetime(2024, 6, 3, 4, 1), datetime(2024, 7, 1, 4, 0)])


def build_bars_zero_row_file(lake: Path) -> None:
    # An empty CANONICAL 7-column file (the real writer shape) is CLEAN.
    _plant_raw(_dest(lake), [])


def build_bars_schema_alien_missing_columns(lake: Path) -> None:
    # Timestamp-only file: cannot satisfy BAR_SCAN_COLUMNS — SchemaAlienFile.
    dest = _dest(lake)
    dest.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table({"timestamp": pa.array([_ts(1)], type=pa.timestamp("us", tz="UTC"))})
    pq.write_table(table, dest)


def build_bars_price_sanity_rows(lake: Path) -> None:
    _plant_raw_ohlc(
        _dest(lake),
        [
            (_ts(1), 100.0, 101.0, 99.0, 100.5),  # clean
            (_ts(2), -5.0, -1.0, -10.0, -2.0),  # negative but ordered
            (_ts(3), 100.0, 1.0, 50.0, 60.0),  # high < low
        ],
    )


def build_bars_out_of_range_datetime(lake: Path) -> None:
    _write_bars(lake, [_bar(_ts(1)), _bar(_ts(2))])  # clean June, real writer
    _plant_bars_out_of_range(_dest(lake, month=7))


# ────────────────────────────────────────────────────────────────────────
# Case builders — options tree
# ────────────────────────────────────────────────────────────────────────


def build_option_intra_file_dup(lake: Path) -> None:
    # Duplicate (side, ts); the SAME ts across CE/PE is NOT a dup.
    _plant_option_raw(_opt_dest(lake, "2024-06"), [("CE", _ts(1)), ("CE", _ts(1)), ("PE", _ts(1))])


def build_option_foreign_month_pure(lake: Path) -> None:
    _plant_option_raw(_opt_dest(lake, "2024-07"), [("CE", _ts(1)), ("CE", _ts(2, month=7, day=1))])


def build_option_foreign_month_overlap(lake: Path) -> None:
    _plant_option_raw(_opt_dest(lake, "2024-06"), [("CE", _ts(1)), ("PE", _ts(2))])
    _plant_option_raw(_opt_dest(lake, "2024-07"), [("CE", _ts(1)), ("CE", _ts(3, month=7, day=1))])


def build_option_multi_month_overlap(lake: Path) -> None:
    # June's foreign keys route to TWO canonical months — overlap is SUMMED.
    _plant_option_raw(_opt_dest(lake, "2024-05"), [("CE", _ts(1, month=5, day=2))])
    _plant_option_raw(_opt_dest(lake, "2024-07"), [("CE", _ts(2, month=7, day=2))])
    _plant_option_raw(
        _opt_dest(lake, "2024-06"),
        [("CE", _ts(0)), ("CE", _ts(1, month=5, day=2)), ("CE", _ts(2, month=7, day=2))],
    )


def build_option_null_side_foreign_key(lake: Path) -> None:
    # NULL side + foreign ts: routing is timestamp-only, side is key payload.
    _plant_option_raw(_opt_dest(lake, "2024-06"), [("CE", _ts(0)), (None, _ts(1, month=7, day=1))])


def build_option_non_canonical_stem(lake: Path) -> None:
    # "2024-6" (parsable but NON-canonical): abstains to dup-only classification.
    _plant_option_raw(
        _opt_dest(lake, "2024-6"), [("CE", _ts(1)), ("CE", _ts(1)), ("PE", _ts(2, month=7, day=1))]
    )


def build_option_unicode_digit_filename(lake: Path) -> None:
    # "²²".isdigit() is True but int("²²") raises — must abstain to dup-only.
    _plant_option_raw(
        _opt_dest(lake, "²²-06"), [("CE", _ts(1)), ("CE", _ts(1)), ("PE", _ts(2, month=7, day=1))]
    )


def build_glob_metachar_series(lake: Path) -> None:
    # Poisoned REAL siblings + a CLEAN literal "X[12]" — reads must be literal.
    for sibling in ("ABS", "X1", "X2"):
        _plant_option_raw(_opt_dest(lake, "2024-06", series=sibling), [("CE", _ts(1)), ("CE", _ts(1))])
    _plant_option_raw(_opt_dest(lake, "2024-06", series="X[12]"), [("CE", _ts(5))])


def build_option_nested_dtype_list_side(lake: Path) -> None:
    _plant_option_nested_side(
        _opt_dest(lake, "2024-06", series="SERIES_BAD"), [_ts(1), _ts(2, month=7, day=1)]
    )
    _plant_option_raw(_opt_dest(lake, "2024-06", series="SERIES_GOOD"), [("CE", _ts(1)), ("CE", _ts(1))])


def build_option_nested_dtype_struct_side(lake: Path) -> None:
    _plant_option_nested_side(
        _opt_dest(lake, "2024-06", series="SERIES_STRUCT"),
        [_ts(1), _ts(2, month=7, day=1)],
        side_kind="struct",
    )


def build_option_out_of_range_datetime(lake: Path) -> None:
    _plant_option_out_of_range(_opt_dest(lake, "2024-06", series="SERIES_BAD"))
    _plant_option_raw(_opt_dest(lake, "2024-06", series="SERIES_GOOD"), [("CE", _ts(1)), ("CE", _ts(1))])


def build_option_mass_poison_majority(lake: Path) -> None:
    _seed_mass_foreign_option_series(lake)


# ────────────────────────────────────────────────────────────────────────
# Case builders — filesystem / symlink shapes
# ────────────────────────────────────────────────────────────────────────


def build_symlinked_series_dir(lake: Path) -> None:
    outside = _outside_dir(lake) / "SERIES"
    _plant_option_raw(outside / "2024-06.parquet", [("CE", _ts(1)), ("CE", _ts(1))])
    (lake / "options").mkdir(parents=True, exist_ok=True)
    (lake / "options" / "LINKED").symlink_to(outside, target_is_directory=True)


def build_symlinked_options_root(lake: Path) -> None:
    outside = _outside_dir(lake) / "options"
    _plant_option_raw(
        outside / "SERIES" / "2024-06.parquet",
        [("CE", _ts(1)), ("CE", _ts(1)), ("PE", _ts(2, month=7, day=1))],
    )
    (lake / "options").symlink_to(outside, target_is_directory=True)


def build_symlinked_partition_file(lake: Path) -> None:
    outside = _outside_dir(lake)
    _plant_option_raw(outside / "2021-01.parquet", [("CE", _ts(1, year=2021, month=2, day=2))])
    series = lake / "options" / "SERIES"
    _plant_option_raw(series / "2021-03.parquet", [("CE", _ts(0, year=2021, month=3))])
    (series / "2021-01.parquet").symlink_to(outside / "2021-01.parquet")


def build_symlink_loop_partition_file(lake: Path) -> None:
    # The canonical July path is a SELF-REFERENTIAL symlink; June carries a
    # July foreign row — the finding stands, the sibling read abstains.
    series = lake / "options" / "SERIES"
    _plant_option_raw(series / "2024-06.parquet", [("CE", _ts(1)), ("CE", _ts(2, month=7, day=1))])
    loop = series / "2024-07.parquet"
    loop.symlink_to(loop)


def build_dangling_symlink_option_file(lake: Path) -> None:
    # A dangling month-file symlink beside a REAL dup-poisoned sibling: the
    # scan must survive AND still classify the sibling.
    series = lake / "options" / "SERIES"
    _plant_option_raw(series / "2024-06.parquet", [("CE", _ts(1)), ("CE", _ts(1))])
    (series / "2024-05.parquet").symlink_to(_outside_dir(lake) / "absent.parquet")


# ────────────────────────────────────────────────────────────────────────
# The corpus
# ────────────────────────────────────────────────────────────────────────

CORPUS: tuple[PoisonCase, ...] = (
    # bars tree
    PoisonCase("bars_intra_file_dup", build_bars_intra_file_dup, frozenset({"IntraFileDup"})),
    PoisonCase(
        "bars_foreign_month_pure", build_bars_foreign_month_pure, frozenset({"ForeignMonthRows"})
    ),
    PoisonCase(
        "bars_foreign_month_overlap",
        build_bars_foreign_month_overlap,
        frozenset({"ForeignMonthRows"}),
    ),
    PoisonCase(
        "legacy_key_drift_sibling", build_legacy_key_drift_sibling, frozenset({"LegacyKeyDrift"})
    ),
    PoisonCase(
        "legacy_key_drift_canonical_keys",
        build_legacy_key_drift_canonical_keys,
        frozenset({"LegacyKeyDrift"}),
        scan_kwargs=MappingProxyType({"canonical_keys": frozenset({"M&M"})}),
    ),
    PoisonCase(
        "bars_naive_timestamp_routing",
        build_bars_naive_timestamp_routing,
        frozenset({"ForeignMonthRows"}),
    ),
    PoisonCase("bars_zero_row_file", build_bars_zero_row_file, frozenset()),
    PoisonCase(
        "bars_schema_alien_missing_columns",
        build_bars_schema_alien_missing_columns,
        frozenset({"SchemaAlienFile"}),
        expects_skip=True,
    ),
    PoisonCase("bars_price_sanity_rows", build_bars_price_sanity_rows, frozenset({"PriceSanityRows"})),
    PoisonCase(
        "bars_out_of_range_datetime",
        build_bars_out_of_range_datetime,
        frozenset(),
        expects_skip=True,
    ),
    # options tree
    PoisonCase("option_intra_file_dup", build_option_intra_file_dup, frozenset({"OptionIntraFileDup"})),
    PoisonCase(
        "option_foreign_month_pure",
        build_option_foreign_month_pure,
        frozenset({"OptionForeignMonthRows"}),
    ),
    PoisonCase(
        "option_foreign_month_overlap",
        build_option_foreign_month_overlap,
        frozenset({"OptionForeignMonthRows"}),
    ),
    PoisonCase(
        "option_multi_month_overlap",
        build_option_multi_month_overlap,
        frozenset({"OptionForeignMonthRows"}),
    ),
    PoisonCase(
        "option_null_side_foreign_key",
        build_option_null_side_foreign_key,
        frozenset({"OptionForeignMonthRows"}),
    ),
    PoisonCase(
        "option_non_canonical_stem",
        build_option_non_canonical_stem,
        frozenset({"OptionIntraFileDup"}),
    ),
    PoisonCase(
        "option_unicode_digit_filename",
        build_option_unicode_digit_filename,
        frozenset({"OptionIntraFileDup"}),
    ),
    PoisonCase("glob_metachar_series", build_glob_metachar_series, frozenset({"OptionIntraFileDup"})),
    PoisonCase(
        "option_nested_dtype_list_side",
        build_option_nested_dtype_list_side,
        frozenset({"OptionIntraFileDup"}),
        expects_skip=True,
    ),
    PoisonCase(
        "option_nested_dtype_struct_side",
        build_option_nested_dtype_struct_side,
        frozenset(),
        expects_skip=True,
    ),
    PoisonCase(
        "option_out_of_range_datetime",
        build_option_out_of_range_datetime,
        frozenset({"OptionIntraFileDup"}),
        expects_skip=True,
    ),
    PoisonCase(
        "option_mass_poison_majority",
        build_option_mass_poison_majority,
        frozenset({"OptionForeignMonthRows"}),
    ),
    # filesystem / symlink shapes
    PoisonCase(
        "symlinked_series_dir", build_symlinked_series_dir, frozenset(), requires="symlink"
    ),
    PoisonCase(
        "symlinked_options_root", build_symlinked_options_root, frozenset(), requires="symlink"
    ),
    PoisonCase(
        "symlinked_partition_file",
        build_symlinked_partition_file,
        frozenset(),
        expects_skip=True,
        requires="symlink",
    ),
    PoisonCase(
        "symlink_loop_partition_file",
        build_symlink_loop_partition_file,
        frozenset({"OptionForeignMonthRows"}),
        expects_skip=True,
        requires="symlink",
    ),
    PoisonCase(
        "dangling_symlink_option_file",
        build_dangling_symlink_option_file,
        frozenset({"OptionIntraFileDup"}),
        requires="symlink",
    ),
)
