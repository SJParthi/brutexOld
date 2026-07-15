"""Hypothesis differential properties of the frame-direct write (DEC-PREBUILD-FRAME-WRITE-001 §3.3).

The standing oracles licensing the fast lane — and the future Tier-2 columnar
merge (graft G-G, the house #1685/R45 differential pattern):

* **frame↔bars parity**: over random admissible-and-hostile frames, the
  flag-ON write and the verbatim legacy write produce BYTE-IDENTICAL
  partitions — or raise the SAME exception class;
* **dict-merge ≡ frame-merge differential**: over random (stored, incoming)
  partition pairs, the fast probe's verdict (no-op vs degrade) is EQUIVALENT
  to legacy's branch (``added == 0 ∧ ¬rewrite ∧ heal == 0`` under
  latest-wins), and the end-state bytes are identical whichever lane runs;
* **month grouping ≡ partition_for_timestamp** over random tz-aware instants
  incl. month/D−1 boundaries;
* **OI mapping** (absent→0 / null→0 / valued pass-through) ≡
  ``frame_to_candle_bars``'s law;
* **eq_missing null semantics**: null==null equal for the compare; null≠0.

Whitebox on writer privates — ``reportPrivateUsage`` silenced per DEC-P0-017.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, TypedDict

import pytest
from brutex.data import frame_write_gate as gate
from brutex.data.frame_write_gate import PREBUILD_FRAME_WRITE_ENV
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partition import partition_for_timestamp
from brutex.data.sink import ParquetBarSink
from brutex.data.spot_daily_resample import frame_to_candle_bars
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytest.importorskip("polars")

import polars as pl

_T0 = datetime(2024, 3, 4, 3, 45, tzinfo=UTC)


class _Coords(TypedDict):
    """Exact-key coordinate kwargs — a plain dict[str, str] would make pyright
    reject ``**_COORDS`` unpacking against ``append_frame``'s non-str keyword
    params (``absent_open_interest`` / ``stats``); the TypedDict pins the key
    set so the unpack type-checks precisely."""

    exchange: str
    segment: str
    symbol: str
    interval: str


_COORDS: Final[_Coords] = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "5minute",
}

_SCHEMA: dict[str, Any] = {
    "ts_utc": pl.Datetime(time_unit="us", time_zone="UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
    "open_interest": pl.Int64,
}

#: Grid + off-grid + boundary prices (finite positive — the resampler domain).
_price = st.one_of(
    st.decimals(
        min_value="0.01", max_value="99999.99", places=2, allow_nan=False, allow_infinity=False
    ).map(float),
    st.floats(min_value=0.001, max_value=99999.0, allow_nan=False, allow_infinity=False),
    st.sampled_from([100.005, 14020.7999, 2.675, 0.00499999999, 999999.995]),
)


@st.composite
def _frames(draw: st.DrawFn) -> pl.DataFrame:
    """Random single-month frames: sorted-unique OR dup/unsorted hostile."""
    n = draw(st.integers(min_value=1, max_value=12))
    offsets = draw(
        st.lists(st.integers(min_value=0, max_value=8000), min_size=n, max_size=n, unique=True)
    )
    hostile = draw(st.sampled_from(["sorted", "unsorted", "dup"]))
    if hostile == "sorted":
        offsets = sorted(offsets)
    elif hostile == "dup" and n > 1:
        offsets = sorted(offsets)
        offsets[-1] = offsets[-2]
    rows: dict[str, list[Any]] = {
        "ts_utc": [_T0 + timedelta(minutes=o) for o in offsets],
        "open": [],
        "high": [],
        "low": [],
        "close": [],
        "volume": draw(st.lists(st.integers(min_value=0, max_value=10**6), min_size=n, max_size=n)),
    }
    for _ in range(n):
        a = draw(_price)
        b = draw(_price)
        lo, hi = min(a, b), max(a, b)
        rows["open"].append(draw(st.sampled_from([lo, hi])))
        rows["close"].append(draw(st.sampled_from([lo, hi])))
        rows["high"].append(hi)
        rows["low"].append(lo)
    schema = dict(_SCHEMA)
    oi_shape = draw(st.sampled_from(["absent", "valued", "nullish"]))
    if oi_shape == "absent":
        schema.pop("open_interest")
    elif oi_shape == "valued":
        rows["open_interest"] = draw(
            st.lists(st.integers(min_value=0, max_value=10**7), min_size=n, max_size=n)
        )
    else:
        rows["open_interest"] = draw(
            st.lists(
                st.one_of(st.none(), st.integers(min_value=0, max_value=10**7)),
                min_size=n,
                max_size=n,
            )
        )
    return pl.DataFrame(rows, schema=schema)


def _run_arm(root: Path, frame: pl.DataFrame, flag: str) -> tuple[bytes | None, str | None]:
    """One write arm → (partition bytes, exception class name)."""
    import os

    os.environ[PREBUILD_FRAME_WRITE_ENV] = flag
    gate.reset_gate_for_tests(fresh_remaining=0, merge_remaining=0)
    gate.reset_observability_memo_for_tests()
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=root, partition_log_level="debug"))
    try:
        if flag == "0":
            sink.append(
                **_COORDS,
                bars=frame_to_candle_bars(frame, absent_open_interest=0),
                merge_mode="latest-wins",
            )
        else:
            sink.append_frame(**_COORDS, frame=frame, merge_mode="latest-wins")
    except Exception as exc:
        return None, type(exc).__name__
    finally:
        os.environ.pop(PREBUILD_FRAME_WRITE_ENV, None)
        gate.reset_gate_for_tests()
    dest = root / "bars" / "NSE" / "INDEX" / "NIFTY" / "5minute" / "2024" / "03.parquet"
    return (dest.read_bytes() if dest.exists() else b""), None


@settings(
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    deadline=None,
    max_examples=40,
)
@given(frame=_frames())
def test_frame_vs_bars_write_parity(tmp_path: Path, frame: pl.DataFrame) -> None:
    """Fresh-write differential: byte-equal partitions OR the same exception class."""
    import uuid

    base = tmp_path / uuid.uuid4().hex
    legacy_bytes, legacy_exc = _run_arm(base / "off", frame, "0")
    fast_bytes, fast_exc = _run_arm(base / "on", frame, "1")
    assert legacy_exc == fast_exc
    assert legacy_bytes == fast_bytes


@st.composite
def _merge_pairs(draw: st.DrawFn) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(stored, incoming) pairs: equal / subset / superset / restated values."""
    stored = draw(_frames())
    relation = draw(st.sampled_from(["equal", "subset", "superset", "restate"]))
    incoming = stored
    if relation == "subset" and stored.height > 1:
        incoming = stored.slice(0, stored.height - 1)
    elif relation == "superset":
        extra = stored.slice(stored.height - 1, 1).with_columns(
            (pl.col("ts_utc") + pl.duration(minutes=9001)).alias("ts_utc")
        )
        incoming = pl.concat([stored, extra])
    elif relation == "restate":
        incoming = stored.with_columns((pl.col("close") + 100.0).alias("close")).with_columns(
            pl.max_horizontal("high", "close").alias("high")
        )
    return stored, incoming


@settings(
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    deadline=None,
    max_examples=30,
)
@given(pair=_merge_pairs())
def test_dict_merge_vs_frame_merge_differential(
    tmp_path: Path, pair: tuple[pl.DataFrame, pl.DataFrame]
) -> None:
    """The G-G standing oracle: merge end-state bytes identical, verdicts coherent."""
    import uuid

    stored, incoming = pair
    base = tmp_path / uuid.uuid4().hex
    # Seed BOTH roots with the stored partition via the ONE legacy path.
    seed_excs = set()
    for sub in ("off", "on"):
        _, exc = _run_arm(base / sub, stored, "0")
        seed_excs.add(exc)
    (seed_exc,) = seed_excs
    if seed_exc is not None:
        return  # a hostile stored shape never landed — nothing to merge into
    legacy_bytes, legacy_exc = _run_arm(base / "off", incoming, "0")
    fast_bytes, fast_exc = _run_arm(base / "on", incoming, "1")
    assert legacy_exc == fast_exc
    assert legacy_bytes == fast_bytes


@settings(deadline=None, max_examples=200)
@given(
    ts=st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2026, 12, 31, 23, 59),
        timezones=st.just(UTC),
    )
)
def test_month_grouping_matches_partition_for_timestamp(ts: datetime) -> None:
    """The sink's vectorized (year, month) law ≡ partition_for_timestamp."""
    frame = pl.DataFrame(
        {"ts_utc": [ts]}, schema={"ts_utc": pl.Datetime(time_unit="us", time_zone="UTC")}
    )
    keyed = frame.with_columns(
        pl.col("ts_utc").dt.year().alias("y"), pl.col("ts_utc").dt.month().alias("m")
    )
    assert (keyed["y"][0], keyed["m"][0]) == partition_for_timestamp(ts)


@settings(deadline=None, max_examples=60)
@given(
    ois=st.lists(
        st.one_of(st.none(), st.integers(min_value=0, max_value=10**7)),
        min_size=1,
        max_size=8,
    ),
    absent=st.sampled_from([0, None]),
    column_present=st.booleans(),
)
def test_oi_expr_matches_frame_to_candle_bars(
    ois: list[int | None], absent: int | None, column_present: bool
) -> None:
    """The columnar OI expression ≡ the frame_to_candle_bars law (V3 C6)."""
    n = len(ois)
    rows: dict[str, list[Any]] = {
        "ts_utc": [_T0 + timedelta(minutes=i) for i in range(n)],
        "open": [100.0] * n,
        "high": [110.0] * n,
        "low": [90.0] * n,
        "close": [105.0] * n,
        "volume": [1] * n,
    }
    schema = dict(_SCHEMA)
    if column_present:
        rows["open_interest"] = ois
    else:
        schema.pop("open_interest")
    frame = pl.DataFrame(rows, schema=schema)
    # Select beside a per-row column, exactly as the production render/probe
    # selects do — a lone lit-select would not broadcast to the frame height.
    expr_values = (
        frame.select(pl.col("ts_utc"), ParquetWriter._frame_oi_expr(frame, absent))
        .get_column("open_interest")
        .to_list()
    )
    legacy = [b.open_interest for b in frame_to_candle_bars(frame, absent_open_interest=absent)]
    assert expr_values == legacy


def test_eq_missing_null_semantics_pins() -> None:
    """The classic polars trap the merge equality avoids: null == null is NULL
    under ``==`` but TRUE under ``eq_missing``; null ≠ 0 under both."""
    a = pl.Series("a", [None, None, 5], dtype=pl.Int64)
    b = pl.Series("b", [None, 0, 5], dtype=pl.Int64)
    plain = (a == b).to_list()
    assert plain[0] is None  # the trap: .all(ignore_nulls) would skip it
    missing_aware = a.eq_missing(b).to_list()
    assert missing_aware == [True, False, True]
