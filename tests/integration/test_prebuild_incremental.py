"""Incremental prebuild integration oracle (DEC-PREBUILD-INCREMENTAL-001).

Round-4 finding: a nothing-changed re-run's prebuild re-read + re-resampled
every contract's full 1-minute base across 20 TFs (~2 h) — only the WRITE was a
no-op. These tests pin the fix end-to-end over a REAL tmp lake through the
production ``materialize_lake_timeframes`` glue:

* the stamped re-run SKIPS (``unchanged=True``, zero reads asserted via a
  byte-identical lake), and the skip is provably byte-equal to a FULL re-derive
  (the GOLDEN pin: two clone lakes — one skipping, one forced full — end in
  recursively identical ``bars/`` bytes);
* any 1-minute source change (an appended pull) invalidates the stamp;
* the daily forming-session cutoff can never yield a stale ``1day`` skip: when
  the stamped cutoff EXCLUDED the data's last session, a later-day re-run
  re-derives and finalises that daily bar;
* a missing derived partition dir (a failed heal rebuild / out-of-band delete)
  falls through to a full re-derive;
* ``BRUTEX_PREBUILD_INCREMENTAL=0`` disables the skip entirely.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from brutex.data.derive_stamp import derive_stamp_path, load_derive_stamp
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import (
    MaterializeResult,
    PrebuildKind,
    materialize_lake_timeframes,
)

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NIFTY"

#: 09:15 IST on 2021-01-04 == 03:45 UTC — one full historical session.
_SESSION_OPEN_UTC = datetime(2021, 1, 4, 3, 45, tzinfo=UTC)


def _bars(n: int, *, start: datetime = _SESSION_OPEN_UTC, offset: int = 0) -> list[CandleBar]:
    return [
        CandleBar(
            timestamp=start + timedelta(minutes=i),
            open=Decimal(str(100.0 + offset + i)),
            high=Decimal(str(110.0 + offset + i)),
            low=Decimal(str(90.0 + offset + i)),
            close=Decimal(str(105.0 + offset + i)),
            volume=10 + i,
            open_interest=0,
        )
        for i in range(n)
    ]


def _write_1m(lake_root: Path, bars: list[CandleBar]) -> None:
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(exchange=_EXCHANGE, segment=_SEGMENT, symbol=_SYMBOL, interval="1minute", bars=bars)


def _derive(
    lake_root: Path,
    *,
    daily_before: date = date(2021, 1, 5),
    incremental: bool | None = None,
) -> MaterializeResult:
    return materialize_lake_timeframes(
        lake_root=lake_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        tfs=("5m", "15m"),
        include_daily=True,
        daily_before=daily_before,
        incremental=incremental,
    )


def _tree_bytes(lake_root: Path) -> dict[str, bytes]:
    """Every ``bars/`` file's bytes, keyed by lake-relative posix path."""
    root = lake_root / "bars"
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*.parquet"))}


def test_rerun_skips_unchanged_and_records_stamp(tmp_path: Path) -> None:
    _write_1m(tmp_path, _bars(60))
    first = _derive(tmp_path)
    assert first.unchanged is False
    assert first.written["5m"] == 12 and first.written["15m"] == 4
    assert first.written["1day"] == 1  # the 2021-01-04 session, final vs before=01-05

    stamp = load_derive_stamp(derive_stamp_path(tmp_path, _EXCHANGE, _SEGMENT, _SYMBOL))
    assert stamp is not None
    assert stamp.include_daily is True
    assert stamp.daily_before == "2021-01-05"
    assert stamp.last_1m_ist_date == "2021-01-04"

    again = _derive(tmp_path)
    assert again.unchanged is True
    assert again.written == {}


def test_golden_stamped_skip_lake_equals_full_rederive_lake(tmp_path: Path) -> None:
    """THE byte-equality pin: skip-vs-full-re-derive end in the identical lake."""
    lake_skip = tmp_path / "lake_skip"
    lake_full = tmp_path / "lake_full"
    for lake in (lake_skip, lake_full):
        _write_1m(lake, _bars(60))
        assert _derive(lake).unchanged is False

    # Second pass: one lake takes the incremental skip, the twin is FORCED full.
    assert _derive(lake_skip).unchanged is True
    assert _derive(lake_full, incremental=False).unchanged is False

    assert _tree_bytes(lake_skip) == _tree_bytes(lake_full)


def test_source_change_invalidates_the_stamp(tmp_path: Path) -> None:
    _write_1m(tmp_path, _bars(60))
    assert _derive(tmp_path).unchanged is False
    # A later pull appends a second session — the 1m month file is rewritten.
    _write_1m(tmp_path, _bars(30, start=_SESSION_OPEN_UTC + timedelta(days=1), offset=500))
    rerun = _derive(tmp_path, daily_before=date(2021, 1, 6))
    assert rerun.unchanged is False
    assert rerun.written["5m"] == 18  # 12 + 6 buckets, the new session derived
    assert rerun.written["1day"] == 2


def test_later_cutoff_rederives_when_stamped_run_excluded_a_forming_session(
    tmp_path: Path,
) -> None:
    """The stale-1day trap: run day == data's last session → the daily was
    EXCLUDED; the NEXT day's re-run (unchanged 1m!) must re-derive, not skip."""
    _write_1m(tmp_path, _bars(60))
    same_day = _derive(tmp_path, daily_before=date(2021, 1, 4))  # run "on" the data day
    assert same_day.unchanged is False
    assert same_day.written["1day"] == 0  # the forming session was excluded

    next_day = _derive(tmp_path, daily_before=date(2021, 1, 5))
    assert next_day.unchanged is False  # MUST NOT skip — a daily bar is now due
    assert next_day.written["1day"] == 1  # …and it lands

    # And now the data is final past the cutoff: an even later re-run skips.
    later = _derive(tmp_path, daily_before=date(2021, 1, 6))
    assert later.unchanged is True


def test_missing_derived_dir_falls_through_to_full_derive(tmp_path: Path) -> None:
    import shutil

    _write_1m(tmp_path, _bars(60))
    assert _derive(tmp_path).unchanged is False
    shutil.rmtree(tmp_path / "bars" / _EXCHANGE / _SEGMENT / _SYMBOL / "5minute")
    restored = _derive(tmp_path)
    assert restored.unchanged is False  # the dir guard refused the skip
    assert restored.written["5m"] == 12  # …and the partition is restored


def test_env_escape_hatch_disables_the_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_1m(tmp_path, _bars(60))
    assert _derive(tmp_path).unchanged is False
    monkeypatch.setenv("BRUTEX_PREBUILD_INCREMENTAL", "0")
    forced = _derive(tmp_path)  # incremental=None → env resolves OFF
    assert forced.unchanged is False
    assert forced.written["5m"] == 12


def test_incremental_false_never_stamps(tmp_path: Path) -> None:
    # The heal-rebuild contract: a forced run neither skips nor writes a stamp.
    _write_1m(tmp_path, _bars(60))
    assert _derive(tmp_path, incremental=False).unchanged is False
    assert load_derive_stamp(derive_stamp_path(tmp_path, _EXCHANGE, _SEGMENT, _SYMBOL)) is None
