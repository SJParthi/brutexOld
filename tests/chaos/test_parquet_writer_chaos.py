"""Chaos tests for the two lake Parquet writers (DEC-CHAOS-SWEEP-PARQUET-001).

The lake's durability contract under hostile schedules — randomized **torn
writes** (a crash at the atomic-commit boundary), **ENOSPC** mid-write, and a
**concurrent multi-writer race** on one partition — for BOTH
:class:`brutex.data.parquet_writer.ParquetWriter` (spot OHLCV) and
:class:`brutex.data.option_parquet_writer.OptionParquetWriter` (expired options).
Sibling of ``tests/chaos/test_audit_chain_chaos.py``; complements the single
``OSError``-at-``os.replace`` injection the unit suites already have
(``test_parquet_writer.py`` / ``test_option_parquet_writer.py``) by hammering the
invariant across many randomized crash sites + concurrent schedules.

The one hard invariant proved for both writers: **a reader NEVER sees a partial /
torn / corrupt partition.** Every commit goes through ``tempfile`` →
``os.replace`` (atomic on POSIX), so after ANY crash the destination is EITHER
absent (a never-committed first write) OR the last fully-committed bytes — never a
half-rewritten file — and no ``*.tmp`` scratch file is ever left behind.

Concurrency, honestly (Golden Rule #10)
---------------------------------------
* The **spot** writer holds a process-wide **per-partition lock**
  (``_PARTITION_LOCKS``, DEC-DHAN-PARALLEL-WINDOWS-001), so concurrent writers to
  the SAME ``(symbol, interval, year, month)`` file serialise their
  read-modify-write merge → the final partition is the loss-free **union** of
  every writer's bars. We assert that strong property.
* The **option** writer has NO writer-level lock — production serialises its
  writes one level up, inside :class:`~brutex.data.option_sink.OptionBarSink`'s
  single critical section (``option_sink.py`` ``with self._lock``,
  DEC-DHAN-PARALLEL-INGEST-001). So for the option writer we assert only the
  invariant it alone guarantees: each write is **individually atomic**, so a
  concurrent race may drop a racing writer's rows (a lost update) but can NEVER
  corrupt the file — the final partition is always a valid, duplicate-free
  Parquet. We do NOT falsely assert union-completeness at the writer level.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import errno
import os
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytestmark = pytest.mark.chaos

# polars backs both writers; the whole module is meaningless without it.
pl = pytest.importorskip("polars")

from brutex.data.models import CandleBar, OptionBar  # noqa: E402
from brutex.data.option_parquet_writer import (  # noqa: E402
    OptionParquetWriter,
    OptionSeriesCoordinate,
)
from brutex.data.parquet_writer import ParquetWriter  # noqa: E402

_ROUNDS = 24
_THREADS = 8


# ────────────────────────── spot writer fixtures ──────────────────────────


def _spot_bar(minute: int, price: float = 100.0) -> CandleBar:
    """One 2dp spot bar at a fixed Jan-2021 partition, addressed by *minute*."""
    return CandleBar(
        timestamp=datetime(2021, 1, 4, 4, minute, tzinfo=UTC),
        open=Decimal(str(round(price, 2))),
        high=Decimal(str(round(price + 1, 2))),
        low=Decimal(str(round(price - 1, 2))),
        close=Decimal(str(round(price + 0.5, 2))),
        volume=minute,
        open_interest=None,
    )


def _spot_write(writer: ParquetWriter, bars: list[CandleBar]) -> None:
    writer.write(exchange="NSE", segment="CASH", symbol="NSE-NIFTY", interval="1minute", bars=bars)


def _spot_dest(lake_root: Path) -> Path:
    """The single partition path every spot fixture write resolves to."""
    (dest,) = lake_root.rglob("*.parquet")
    return dest


def _no_tmp(lake_root: Path) -> bool:
    """True iff no scratch ``*.tmp`` file leaked anywhere under the lake."""
    return list(lake_root.rglob("*.tmp")) == []


# ────────────────────────── option writer fixtures ─────────────────────────

_OPT_PK = "NSE_FNO|13|WEEK|0|ATM|CALL"
_OPT_COORD = OptionSeriesCoordinate(
    exch_id="NSE",
    exchange_segment="NSE_FNO",
    security_id=13,
    underlying_symbol="NIFTY",
    expiry_flag="WEEK",
    expiry_code=0,
    strike_offset="ATM",
)


def _opt_bar(side: str, minute: int) -> OptionBar:
    """One 2dp option bar keyed by ``(side, timestamp)`` within a fixed partition."""
    return OptionBar(
        timestamp=datetime(2021, 1, 4, 4, minute, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("110.10"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=250,
        open_interest=1200,
        side=side,
        strike=Decimal("17500.00"),
        spot=Decimal("17480.55"),
        iv=Decimal("0.1523"),
    )


def _opt_dest(lake_root: Path) -> Path:
    (dest,) = (lake_root / "options").rglob("*.parquet")
    return dest


# ══════════════════════════ spot: torn write ══════════════════════════════


@pytest.mark.parametrize("seed", [1, 7, 42, 1234])
def test_spot_randomized_torn_writes_never_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    """Across many rounds, a randomly-failed ``os.replace`` never tears the partition.

    Each round grows the month by one bar and, with ~50% probability, the atomic
    rename "crashes" (raises). The invariant checked AFTER every round (crash or
    not), mirroring the audit chaos test: the partition is absent OR a valid
    Parquet equal to the LAST successfully-committed bytes, and no ``*.tmp`` leaks.
    """
    import random

    rng = random.Random(seed)
    real_replace = os.replace
    state = {"fail": False}

    def maybe_replace(src: str, dst: str) -> None:
        if state["fail"]:
            raise OSError("simulated torn rename (crash at commit)")
        real_replace(src, dst)

    monkeypatch.setattr("brutex.data.parquet_writer.os.replace", maybe_replace)

    writer = ParquetWriter(lake_root=tmp_path)
    committed: bytes | None = None
    for round_no in range(_ROUNDS):
        bars = [_spot_bar(m) for m in range(round_no + 1)]  # growing month
        state["fail"] = rng.random() < 0.5
        if state["fail"]:
            with pytest.raises(OSError, match="torn rename"):
                _spot_write(writer, bars)
        else:
            _spot_write(writer, bars)
            committed = _spot_dest(tmp_path).read_bytes()

        assert _no_tmp(tmp_path), "a torn write leaked a .tmp scratch file"
        files = list(tmp_path.rglob("*.parquet"))
        if committed is None:
            assert files == [], "a never-committed write left a partition on disk"
        else:
            # The partition equals the last good commit exactly — never torn.
            assert files and files[0].read_bytes() == committed
            pl.read_parquet(files[0])  # readable → not corrupt


def test_spot_enospc_mid_write_leaves_no_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOSPC at ``fsync`` aborts the write, cleans the tmp, and never half-creates.

    A first write that hits disk-full while flushing the tmp file must leave the
    partition absent (it was never committed) and no scratch ``*.tmp`` behind; a
    SUBSEQUENT write to the same partition once "space" returns must still create
    a clean, readable file.
    """
    real_fsync = os.fsync
    state = {"fail": True}

    def maybe_fsync(fd: int) -> None:
        if state["fail"]:
            raise OSError(errno.ENOSPC, "No space left on device")
        real_fsync(fd)

    monkeypatch.setattr("brutex.data.parquet_writer.os.fsync", maybe_fsync)
    writer = ParquetWriter(lake_root=tmp_path)

    with pytest.raises(OSError) as exc:
        _spot_write(writer, [_spot_bar(0)])
    assert exc.value.errno == errno.ENOSPC
    assert list(tmp_path.rglob("*.parquet")) == []
    assert _no_tmp(tmp_path)

    # Space recovers → the next write succeeds cleanly.
    state["fail"] = False
    _spot_write(writer, [_spot_bar(0)])
    assert pl.read_parquet(_spot_dest(tmp_path)).height == 1
    assert _no_tmp(tmp_path)


def test_spot_concurrent_writers_union_no_lost_update(tmp_path: Path) -> None:
    """N threads writing disjoint bars of ONE partition produce the loss-free union.

    The per-partition lock (DEC-DHAN-PARALLEL-WINDOWS-001) serialises the
    read-modify-write merge so NO writer's bars are dropped — the final partition
    has every thread's rows exactly once, regardless of completion order, and is a
    valid Parquet with no ``*.tmp`` residue.
    """
    writer = ParquetWriter(lake_root=tmp_path)
    per_thread = 5
    barrier = threading.Barrier(_THREADS)
    errors: list[BaseException] = []

    def worker(tid: int) -> None:
        # Disjoint minute ranges → a pure union, no keep-first conflicts.
        bars = [_spot_bar(tid * per_thread + i) for i in range(per_thread)]
        try:
            barrier.wait()  # maximise the race window
            _spot_write(writer, bars)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"a concurrent writer raised: {errors[:1]}"
    df = pl.read_parquet(_spot_dest(tmp_path))
    assert df.height == _THREADS * per_thread  # every bar present, none lost
    assert df["volume"].n_unique() == _THREADS * per_thread  # no dup/overwrite
    assert _no_tmp(tmp_path)


# ══════════════════════════ option: torn write ════════════════════════════


@pytest.mark.parametrize("seed", [2, 13, 99, 2024])
def test_option_randomized_torn_writes_never_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    """A randomly-failed option ``os.replace`` never tears the expired-options table."""
    import random

    rng = random.Random(seed)
    real_replace = os.replace
    state = {"fail": False}

    def maybe_replace(src: str, dst: str) -> None:
        if state["fail"]:
            raise OSError("simulated torn rename (crash at commit)")
        real_replace(src, dst)

    monkeypatch.setattr("brutex.data.option_parquet_writer.os.replace", maybe_replace)

    writer = OptionParquetWriter(lake_root=tmp_path)
    committed: bytes | None = None
    for round_no in range(_ROUNDS):
        bars = [_opt_bar("CE", m) for m in range(round_no + 1)]  # growing series
        state["fail"] = rng.random() < 0.5
        if state["fail"]:
            with pytest.raises(OSError, match="torn rename"):
                writer.write(partition_key=_OPT_PK, coordinate=_OPT_COORD, bars=bars)
        else:
            writer.write(partition_key=_OPT_PK, coordinate=_OPT_COORD, bars=bars)
            committed = _opt_dest(tmp_path).read_bytes()

        assert _no_tmp(tmp_path), "a torn option write leaked a .tmp scratch file"
        files = list((tmp_path / "options").rglob("*.parquet"))
        if committed is None:
            assert files == []
        else:
            assert files and files[0].read_bytes() == committed
            pl.read_parquet(files[0])  # readable → not corrupt


def test_option_enospc_mid_write_leaves_no_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOSPC while writing the option tmp file aborts cleanly — no partial table.

    The disk fills at the real data-write site (``DataFrame.write_parquet`` to the
    tmp path); the writer's ``finally`` unlinks the scratch file, the partition is
    never created, and a later write once space returns produces a clean table.
    """
    real_write_parquet = pl.DataFrame.write_parquet
    state = {"fail": True}

    def maybe_write_parquet(self: object, *args: object, **kwargs: object) -> object:
        if state["fail"]:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write_parquet(self, *args, **kwargs)

    monkeypatch.setattr(pl.DataFrame, "write_parquet", maybe_write_parquet)
    writer = OptionParquetWriter(lake_root=tmp_path)

    with pytest.raises(OSError) as exc:
        writer.write(partition_key=_OPT_PK, coordinate=_OPT_COORD, bars=[_opt_bar("CE", 0)])
    assert exc.value.errno == errno.ENOSPC
    assert list((tmp_path / "options").rglob("*.parquet")) == []
    assert _no_tmp(tmp_path)

    state["fail"] = False
    n = writer.write(partition_key=_OPT_PK, coordinate=_OPT_COORD, bars=[_opt_bar("CE", 0)])
    assert n == 1
    assert pl.read_parquet(_opt_dest(tmp_path)).height == 1
    assert _no_tmp(tmp_path)


def test_option_concurrent_writers_never_corrupt(tmp_path: Path) -> None:
    """A concurrent option-writer race may lose rows but NEVER corrupts the table.

    Without a writer-level partition lock (production serialises via
    ``OptionBarSink``), racing writers to one partition can drop a racing writer's
    rows — but because each write is individually atomic the final file is ALWAYS
    a valid, duplicate-free Parquet (the hard "no partial/corrupt partition"
    invariant). We assert exactly that, not union-completeness (Golden Rule #10).
    """
    writer = OptionParquetWriter(lake_root=tmp_path)
    per_thread = 5
    barrier = threading.Barrier(_THREADS)
    errors: list[BaseException] = []

    def worker(tid: int) -> None:
        side = "CE" if tid % 2 == 0 else "PE"
        bars = [_opt_bar(side, tid * per_thread + i) for i in range(per_thread)]
        try:
            barrier.wait()
            writer.write(partition_key=_OPT_PK, coordinate=_OPT_COORD, bars=bars)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"a concurrent option writer raised: {errors[:1]}"
    df = pl.read_parquet(_opt_dest(tmp_path))  # readable → not torn/corrupt
    # The merge dedups on (side, timestamp); the surviving file is always
    # internally consistent (no duplicate keys), whatever raced.
    keys = df.select(["side", "timestamp"]).unique()
    assert keys.height == df.height, "the committed table has duplicate (side, ts) keys"
    assert df.height >= per_thread, "at least one full writer's batch must survive"
    assert _no_tmp(tmp_path)
