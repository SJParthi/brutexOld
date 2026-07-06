"""Unit tests for ``brutex.data.lake_stats_manifest`` (DEC-LAKE-STATS-MANIFEST-001).

The per-file lake STATS manifest core: shard paths + fanout digest, the local
month grammar (test-PINNED against ``partition.py`` /
``option_parquet_writer.py``), stat tokens, the fail-closed load matrix
(EVERY validation clause individually violated ⇒ treated absent + one
``lake.stats_manifest.fallback`` naming the clause), the lazy-accretion
write hooks (upsert / row-remove / dataset-remove), the read-side seed, the
kill switch, and the fail-loud env knobs — including every best-effort error
path (a shard failure must log + unlink + return, NEVER raise into the pull;
a ``LakeWriteHalt`` from ``.locks/`` creation must PROPAGATE).

Whitebox: exercises private helpers per the DEC-P0-017 convention, so
``reportPrivateUsage`` is silenced.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import errno
import fcntl
import json
import os
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from brutex.data import lake_stats_manifest as lsm
from brutex.data.errors import LakeWriteHalt, PartitionLockTimeout
from brutex.data.partition_lock import partition_lock_path
from structlog.testing import capture_logs

BARS_KEY: dict[str, str] = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
}

OPTIONS_KEY: dict[str, str] = {"series": "FNO__NSE__IDX__NIFTY__2026-07-30__CE__24000"}


def _ts(year: int, month: int, day: int, hour: int = 9, minute: int = 15) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _rel(kind: str, year: int, month: int) -> str:
    if kind == lsm.KIND_BARS:
        return f"{year:04d}/{month:02d}.parquet"
    return f"{year:04d}-{month:02d}.parquet"


def _write_partition(
    lake: Path, kind: str, key: dict[str, str], year: int, month: int, body: bytes = b"pq"
) -> Path:
    dest = lsm.dataset_dir(lake, kind, key) / _rel(kind, year, month)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return dest


def _row_for(
    lake: Path,
    kind: str,
    key: dict[str, str],
    year: int,
    month: int,
    *,
    rows: int = 10,
    raw_hash: str | None = None,
) -> lsm.StatsFileRow:
    """Build a stat-accurate row for an already-written partition file."""
    rel = _rel(kind, year, month)
    stat = os.stat(lsm.dataset_dir(lake, kind, key) / rel)
    return lsm.StatsFileRow(
        rel=rel,
        rows=rows,
        min_ts=_ts(year, month, 1),
        max_ts=_ts(year, month, 2),
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        raw_hash=raw_hash,
    )


def _seed_bars(lake: Path, months: tuple[tuple[int, int], ...] = ((2026, 6), (2026, 7))) -> Path:
    """Write real partition files + seed the shard; return the shard path."""
    rows = []
    for year, month in months:
        _write_partition(lake, lsm.KIND_BARS, BARS_KEY, year, month, body=f"{year}{month}".encode())
        rows.append(_row_for(lake, lsm.KIND_BARS, BARS_KEY, year, month))
    assert lsm.seed_dataset_stats(lake, kind=lsm.KIND_BARS, key=BARS_KEY, files=rows)
    return lsm.stats_shard_path(lake, lsm.KIND_BARS, BARS_KEY)


def _mutate_shard(shard: Path, mutate: Any) -> None:
    """Load the shard JSON, apply *mutate*, and write it back verbatim."""
    obj = json.loads(shard.read_text(encoding="utf-8"))
    mutate(obj)
    shard.write_text(json.dumps(obj, sort_keys=True), encoding="utf-8")


def _fallback_clauses(logs: Sequence[Mapping[str, Any]]) -> list[str]:
    """The failed-clause names in order (accepts structlog's captured EventDicts)."""
    return [str(e["clause"]) for e in logs if e["event"] == "lake.stats_manifest.fallback"]


@contextmanager
def _raising_lock(exc: BaseException) -> Generator[None, None, None]:
    raise exc
    yield  # pragma: no cover - unreachable


class TestKillSwitch:
    def test_default_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(lsm.STATS_MANIFEST_ENV, raising=False)
        assert lsm.stats_manifest_enabled() is True

    @pytest.mark.parametrize("value", ["off", "OFF", "  off  ", "Off"])
    def test_off_disables(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, value)
        assert lsm.stats_manifest_enabled() is False

    @pytest.mark.parametrize("value", ["on", "", "0", "false", "anything"])
    def test_other_values_stay_enabled(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        """ONLY ``off`` disables — a typo can never silently kill the feature."""
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, value)
        assert lsm.stats_manifest_enabled() is True


class TestResolveStatsLockTimeout:
    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(lsm.STATS_LOCK_TIMEOUT_ENV, raising=False)
        assert lsm.resolve_stats_lock_timeout_seconds() == lsm.STATS_LOCK_TIMEOUT_S == 5.0

    def test_valid_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lsm.STATS_LOCK_TIMEOUT_ENV, "2.5")
        assert lsm.resolve_stats_lock_timeout_seconds() == 2.5

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_fails_loud(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(lsm.STATS_LOCK_TIMEOUT_ENV, value)
        with pytest.raises(ValueError, match="blank"):
            lsm.resolve_stats_lock_timeout_seconds()

    def test_non_numeric_fails_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lsm.STATS_LOCK_TIMEOUT_ENV, "fast")
        with pytest.raises(ValueError, match="not a number"):
            lsm.resolve_stats_lock_timeout_seconds()

    @pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-3"])
    def test_non_finite_or_non_positive_fails_loud(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(lsm.STATS_LOCK_TIMEOUT_ENV, value)
        with pytest.raises(ValueError, match="finite positive"):
            lsm.resolve_stats_lock_timeout_seconds()


class TestPathsAndDigest:
    def test_stats_dir_under_manifest(self, tmp_path: Path) -> None:
        assert lsm.stats_dir(tmp_path) == tmp_path / ".manifest" / "stats"

    def test_shard_path_fanout_and_determinism(self, tmp_path: Path) -> None:
        shard = lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, BARS_KEY)
        assert shard.suffix == ".json"
        digest = shard.stem
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
        assert shard.parent.name == digest[:2]
        assert shard.parent.parent == lsm.stats_dir(tmp_path)
        assert shard == lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, BARS_KEY)

    def test_shard_path_distinct_per_key_and_kind(self, tmp_path: Path) -> None:
        bars = lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, BARS_KEY)
        other = lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, {**BARS_KEY, "interval": "1day"})
        options = lsm.stats_shard_path(tmp_path, lsm.KIND_OPTIONS, OPTIONS_KEY)
        assert len({bars, other, options}) == 3

    def test_drift_key_needs_no_escaping(self, tmp_path: Path) -> None:
        """A #911-drift dir name (``&``, spaces) keys a shard without escaping."""
        drift = {**BARS_KEY, "symbol": "NSE-M&M "}
        shard = lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, drift)
        assert shard.name.endswith(".json")
        assert lsm.dataset_dir(tmp_path, lsm.KIND_BARS, drift).name == "1minute"

    def test_unknown_kind_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown stats-manifest kind"):
            lsm.stats_shard_path(tmp_path, "greeks", BARS_KEY)

    @pytest.mark.parametrize("bad", [{}, {**BARS_KEY, "symbol": ""}, {**BARS_KEY, "symbol": 7}])
    def test_malformed_key_raises(self, tmp_path: Path, bad: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="key field"):
            lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, bad)

    def test_dataset_dir_bars(self, tmp_path: Path) -> None:
        assert (
            lsm.dataset_dir(tmp_path, lsm.KIND_BARS, BARS_KEY)
            == tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute"
        )

    def test_dataset_dir_options(self, tmp_path: Path) -> None:
        assert (
            lsm.dataset_dir(tmp_path, lsm.KIND_OPTIONS, OPTIONS_KEY)
            == tmp_path / "options" / OPTIONS_KEY["series"]
        )


class TestMonthGrammar:
    def test_bars_rel_parses(self) -> None:
        assert lsm.parse_rel_month(lsm.KIND_BARS, "2026/07.parquet") == (2026, 7)

    def test_options_rel_parses(self) -> None:
        assert lsm.parse_rel_month(lsm.KIND_OPTIONS, "2026-07.parquet") == (2026, 7)

    @pytest.mark.parametrize(
        "rel",
        [
            "2026-07.parquet",  # options grammar under bars
            "2026/7.parquet",  # unpadded month
            "26/07.parquet",  # 2-digit year
            "2026/13.parquet",  # month out of range
            "2026/00.parquet",  # month zero
            "0000/01.parquet",  # year zero
            "2026/07.parquet.bak",
            "x/2026/07.parquet",
            "",
        ],
    )
    def test_bars_junk_rejected(self, rel: str) -> None:
        assert lsm.parse_rel_month(lsm.KIND_BARS, rel) is None

    @pytest.mark.parametrize(
        "rel", ["2026/07.parquet", "2026-13.parquet", "2026-00.parquet", "0000-01.parquet", ""]
    )
    def test_options_junk_rejected(self, rel: str) -> None:
        assert lsm.parse_rel_month(lsm.KIND_OPTIONS, rel) is None

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown stats-manifest kind"):
            lsm.parse_rel_month("greeks", "2026/07.parquet")

    def test_month_bounds_mid_year(self) -> None:
        bounds = lsm.rel_month_bounds(lsm.KIND_BARS, "2026/07.parquet")
        assert bounds == (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC))

    def test_month_bounds_december_rollover(self) -> None:
        bounds = lsm.rel_month_bounds(lsm.KIND_OPTIONS, "2026-12.parquet")
        assert bounds == (datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))

    def test_month_bounds_junk_is_none(self) -> None:
        assert lsm.rel_month_bounds(lsm.KIND_BARS, "junk.parquet") is None

    def test_pinned_against_partition_builder(self, tmp_path: Path) -> None:
        """The local bars grammar accepts exactly ``build_partition_path``'s tail."""
        from brutex.data.partition import BAR_DIR_NAME, build_partition_path

        assert lsm._BARS_TREE_DIRNAME == BAR_DIR_NAME
        dest = build_partition_path(
            lake_root=tmp_path,
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            year=2026,
            month=7,
        )
        assert lsm.dest_rel(lsm.KIND_BARS, dest) == "2026/07.parquet"

    def test_pinned_against_option_writer(self) -> None:
        """The local options grammar matches the option writer's partition name."""
        from brutex.data.option_parquet_writer import _OPTIONS_SUBDIR

        assert lsm._OPTIONS_TREE_DIRNAME == _OPTIONS_SUBDIR
        year, month = 2026, 7
        name = f"{year:04d}-{month:02d}.parquet"  # writer: f"{year:04d}-{month:02d}.parquet"
        assert lsm.parse_rel_month(lsm.KIND_OPTIONS, name) == (2026, 7)

    def test_identity_keys_pinned_against_lake_invariants(self) -> None:
        from brutex.data.lake_invariants import UNIQUE_KEY_BARS, UNIQUE_KEY_OPTIONS

        assert lsm.STATS_IDENTITY_KEYS[lsm.KIND_BARS] == UNIQUE_KEY_BARS
        assert lsm.STATS_IDENTITY_KEYS[lsm.KIND_OPTIONS] == UNIQUE_KEY_OPTIONS

    def test_deadline_ordering_pin(self) -> None:
        """Shard flock + partition acquire stay strictly inside the write deadline."""
        from brutex.adapters.groww.constants import GROWW_WRITE_DEADLINE_S
        from brutex.data.partition_lock import DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS

        assert (
            lsm.STATS_LOCK_TIMEOUT_S + DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS
            < GROWW_WRITE_DEADLINE_S
        )

    def test_dest_rel_options(self) -> None:
        dest = Path("/lake/options/SERIES/2026-07.parquet")
        assert lsm.dest_rel(lsm.KIND_OPTIONS, dest) == "2026-07.parquet"

    def test_dest_rel_junk_raises(self) -> None:
        with pytest.raises(ValueError, match="month grammar"):
            lsm.dest_rel(lsm.KIND_BARS, Path("/lake/bars/x/y/z/1minute/oops.parquet"))


class TestStatTokens:
    def test_stat_token_format(self, tmp_path: Path) -> None:
        target = tmp_path / "f.parquet"
        target.write_bytes(b"abc")
        stat = os.stat(target)
        assert lsm.stat_token(stat) == f"{stat.st_mtime_ns}:{stat.st_size}"

    def test_file_token_matches_true(self, tmp_path: Path) -> None:
        target = tmp_path / "f.parquet"
        target.write_bytes(b"abc")
        stat = os.stat(target)
        row = lsm.StatsFileRow(
            rel="2026/07.parquet",
            rows=1,
            min_ts=_ts(2026, 7, 1),
            max_ts=_ts(2026, 7, 1),
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            raw_hash=None,
        )
        assert lsm.file_token_matches(target, row) is True
        assert (
            lsm.file_token_matches(
                target,
                lsm.StatsFileRow(
                    rel=row.rel,
                    rows=1,
                    min_ts=row.min_ts,
                    max_ts=row.max_ts,
                    mtime_ns=stat.st_mtime_ns + 1,
                    size=stat.st_size,
                    raw_hash=None,
                ),
            )
            is False
        )
        assert (
            lsm.file_token_matches(
                target,
                lsm.StatsFileRow(
                    rel=row.rel,
                    rows=1,
                    min_ts=row.min_ts,
                    max_ts=row.max_ts,
                    mtime_ns=stat.st_mtime_ns,
                    size=stat.st_size + 1,
                    raw_hash=None,
                ),
            )
            is False
        )

    def test_file_token_matches_missing_file_false(self, tmp_path: Path) -> None:
        row = lsm.StatsFileRow(
            rel="2026/07.parquet",
            rows=1,
            min_ts=_ts(2026, 7, 1),
            max_ts=_ts(2026, 7, 1),
            mtime_ns=1,
            size=1,
            raw_hash=None,
        )
        assert lsm.file_token_matches(tmp_path / "gone.parquet", row) is False


class TestRowValidation:
    def test_require_int_rejects_bool(self) -> None:
        with pytest.raises(ValueError, match="must be an int"):
            lsm._require_int(True, field="rows", minimum=1)

    def test_require_int_rejects_str(self) -> None:
        with pytest.raises(ValueError, match="must be an int"):
            lsm._require_int("3", field="rows", minimum=1)

    def test_require_int_rejects_below_minimum(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            lsm._require_int(0, field="rows", minimum=1)

    def test_require_ts_accepts_aware_datetime_and_normalizes(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        out = lsm._require_ts(datetime(2026, 7, 1, 5, 30, tzinfo=ist), field="x")
        assert out == datetime(2026, 7, 1, 0, 0, tzinfo=UTC)

    def test_require_ts_accepts_iso_string(self) -> None:
        assert lsm._require_ts("2026-07-01T00:00:00+00:00", field="x") == _ts(2026, 7, 1, 0, 0)

    def test_require_ts_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            lsm._require_ts(datetime(2026, 7, 1), field="x")

    def test_require_ts_rejects_naive_string(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            lsm._require_ts("2026-07-01T00:00:00", field="x")

    def test_require_ts_rejects_junk_string(self) -> None:
        with pytest.raises(ValueError, match="ISO-8601"):
            lsm._require_ts("yesterday", field="x")

    def test_require_ts_rejects_non_timestamp(self) -> None:
        with pytest.raises(ValueError, match="must be a timestamp"):
            lsm._require_ts(42, field="x")

    def test_normalized_row_rejects_empty_rel(self) -> None:
        row = lsm.StatsFileRow(
            rel="",
            rows=1,
            min_ts=_ts(2026, 7, 1),
            max_ts=_ts(2026, 7, 2),
            mtime_ns=0,
            size=0,
            raw_hash=None,
        )
        with pytest.raises(ValueError, match="rel must be"):
            lsm._normalized_row(row)

    def test_normalized_row_rejects_inverted_bounds(self) -> None:
        row = lsm.StatsFileRow(
            rel="2026/07.parquet",
            rows=1,
            min_ts=_ts(2026, 7, 2),
            max_ts=_ts(2026, 7, 1),
            mtime_ns=0,
            size=0,
            raw_hash=None,
        )
        with pytest.raises(ValueError, match="min_ts > max_ts"):
            lsm._normalized_row(row)

    def test_normalized_row_rejects_empty_raw_hash(self) -> None:
        row = lsm.StatsFileRow(
            rel="2026/07.parquet",
            rows=1,
            min_ts=_ts(2026, 7, 1),
            max_ts=_ts(2026, 7, 2),
            mtime_ns=0,
            size=0,
            raw_hash="",
        )
        with pytest.raises(ValueError, match="raw_hash"):
            lsm._normalized_row(row)

    def test_normalized_row_rejects_negative_stat_fields(self) -> None:
        row = lsm.StatsFileRow(
            rel="2026/07.parquet",
            rows=1,
            min_ts=_ts(2026, 7, 1),
            max_ts=_ts(2026, 7, 2),
            mtime_ns=-1,
            size=0,
            raw_hash=None,
        )
        with pytest.raises(ValueError, match="mtime_ns"):
            lsm._normalized_row(row)

    def test_row_from_json_rejects_non_object(self) -> None:
        with pytest.raises(ValueError, match="must be an object"):
            lsm._row_from_json(["not", "a", "dict"])

    def test_row_from_json_rejects_bad_rel(self) -> None:
        with pytest.raises(ValueError, match="rel must be"):
            lsm._row_from_json({"rel": 7})

    def test_row_from_json_rejects_non_string_raw_hash(self) -> None:
        with pytest.raises(ValueError, match="raw_hash"):
            lsm._row_from_json(
                {
                    "rel": "2026/07.parquet",
                    "rows": 1,
                    "min_ts": "2026-07-01T00:00:00+00:00",
                    "max_ts": "2026-07-01T00:00:00+00:00",
                    "mtime_ns": 0,
                    "size": 0,
                    "raw_hash": 7,
                }
            )


class TestBuildDatasetStats:
    def test_happy_sorts_and_aggregates(self) -> None:
        july = lsm.StatsFileRow(
            rel="2026/07.parquet",
            rows=3,
            min_ts=_ts(2026, 7, 1),
            max_ts=_ts(2026, 7, 30),
            mtime_ns=2,
            size=20,
            raw_hash="b" * 64,
        )
        june = lsm.StatsFileRow(
            rel="2026/06.parquet",
            rows=5,
            min_ts=_ts(2026, 6, 1),
            max_ts=_ts(2026, 6, 30),
            mtime_ns=1,
            size=10,
            raw_hash="a" * 64,
        )
        stats = lsm.build_dataset_stats(
            kind=lsm.KIND_BARS,
            key=BARS_KEY,
            files=[july, june],
            updated_utc="2026-07-06T00:00:00+00:00",
        )
        assert [row.rel for row in stats.files] == ["2026/06.parquet", "2026/07.parquet"]
        assert stats.row_count == 8
        assert stats.min_ts == _ts(2026, 6, 1)
        assert stats.max_ts == _ts(2026, 7, 30)
        assert stats.identity_key == ("timestamp",)
        assert stats.updated_utc == "2026-07-06T00:00:00+00:00"

    def test_defaults_updated_utc_to_now(self) -> None:
        row = lsm.StatsFileRow(
            rel="2026-07.parquet",
            rows=1,
            min_ts=_ts(2026, 7, 1),
            max_ts=_ts(2026, 7, 2),
            mtime_ns=0,
            size=0,
            raw_hash=None,
        )
        stats = lsm.build_dataset_stats(kind=lsm.KIND_OPTIONS, key=OPTIONS_KEY, files=[row])
        assert stats.updated_utc  # a now() ISO stamp — value not pinned (observability only)
        assert stats.identity_key == ("side", "timestamp")

    def test_empty_files_raise(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            lsm.build_dataset_stats(kind=lsm.KIND_BARS, key=BARS_KEY, files=[])

    def test_duplicate_rel_raises(self) -> None:
        row = lsm.StatsFileRow(
            rel="2026/07.parquet",
            rows=1,
            min_ts=_ts(2026, 7, 1),
            max_ts=_ts(2026, 7, 2),
            mtime_ns=0,
            size=0,
            raw_hash=None,
        )
        with pytest.raises(ValueError, match="duplicate rel"):
            lsm.build_dataset_stats(kind=lsm.KIND_BARS, key=BARS_KEY, files=[row, row])

    def test_grammar_violating_rel_raises(self) -> None:
        row = lsm.StatsFileRow(
            rel="2026-07.parquet",
            rows=1,
            min_ts=_ts(2026, 7, 1),
            max_ts=_ts(2026, 7, 2),
            mtime_ns=0,
            size=0,
            raw_hash=None,
        )
        with pytest.raises(ValueError, match="month grammar"):
            lsm.build_dataset_stats(kind=lsm.KIND_BARS, key=BARS_KEY, files=[row])

    def test_month_poisoned_bounds_raise(self) -> None:
        """A foreign-month bound can never be persisted through the factory."""
        row = lsm.StatsFileRow(
            rel="2026/07.parquet",
            rows=1,
            min_ts=_ts(2026, 6, 30),
            max_ts=_ts(2026, 7, 2),
            mtime_ns=0,
            size=0,
            raw_hash=None,
        )
        with pytest.raises(ValueError, match="outside its UTC month"):
            lsm.build_dataset_stats(kind=lsm.KIND_BARS, key=BARS_KEY, files=[row])

    def test_max_at_next_month_start_raises(self) -> None:
        row = lsm.StatsFileRow(
            rel="2026/07.parquet",
            rows=1,
            min_ts=_ts(2026, 7, 1),
            max_ts=datetime(2026, 8, 1, tzinfo=UTC),
            mtime_ns=0,
            size=0,
            raw_hash=None,
        )
        with pytest.raises(ValueError, match="outside its UTC month"):
            lsm.build_dataset_stats(kind=lsm.KIND_BARS, key=BARS_KEY, files=[row])


class TestShardRoundtrip:
    def test_render_then_structural_load(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)
        assert shard.exists()
        assert (shard.stat().st_mode & 0o777) == 0o600
        stats = lsm.load_shard_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is not None
        assert stats.kind == lsm.KIND_BARS
        assert dict(stats.key) == BARS_KEY
        assert stats.row_count == 20
        assert [row.rel for row in stats.files] == ["2026/06.parquet", "2026/07.parquet"]

    def test_load_shard_stats_disabled_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_bars(tmp_path)
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, "off")
        assert lsm.load_shard_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None

    def test_updated_utc_non_string_reads_empty(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)

        def _drop(obj: dict[str, Any]) -> None:
            obj["updated_utc"] = 42

        _mutate_shard(shard, _drop)
        stats = lsm.load_shard_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is not None and stats.updated_utc == ""


class TestStructuralClauses:
    """Clause-1 fail-closed matrix — every structural violation individually."""

    def _clause(self, lake: Path, mutate: Any) -> str | None:
        shard = lsm.stats_shard_path(lake, lsm.KIND_BARS, BARS_KEY)
        if mutate is not None:
            _mutate_shard(shard, mutate)
        stats, clause = lsm._load_shard_with_clause(lake, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is None
        return clause

    def test_absent(self, tmp_path: Path) -> None:
        stats, clause = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is None and clause == "absent"

    def test_shard_read_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        shard = _seed_bars(tmp_path)
        real = Path.read_text

        def _boom(self: Path, *args: Any, **kwargs: Any) -> str:
            if self == shard:
                raise PermissionError("read denied")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _boom)
        stats, clause = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is None and clause == "shard_read"

    def test_corrupt_json(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)
        shard.write_text("{truncated", encoding="utf-8")
        stats, clause = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is None and clause == "parse"

    def test_non_object_json(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)
        shard.write_text("[1, 2]", encoding="utf-8")
        stats, clause = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is None and clause == "parse"

    def test_old_version(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["version"] = 2

        assert self._clause(tmp_path, _mutate) == "version"

    def test_bool_version_rejected(self, tmp_path: Path) -> None:
        """JSON ``true`` must not satisfy ``version == 1`` (bool is int)."""
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["version"] = True

        assert self._clause(tmp_path, _mutate) == "version"

    def test_kind_echo_mismatch(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["kind"] = "options"

        assert self._clause(tmp_path, _mutate) == "kind"

    def test_key_echo_mismatch(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["key"]["symbol"] = "BANKNIFTY"

        assert self._clause(tmp_path, _mutate) == "key_echo"

    def test_identity_key_mismatch(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["identity_key"] = ["side", "timestamp"]

        assert self._clause(tmp_path, _mutate) == "identity_key"

    def test_files_not_list(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["files"] = {"rel": "2026/06.parquet"}

        assert self._clause(tmp_path, _mutate) == "row_malformed"

    def test_files_empty(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["files"] = []

        assert self._clause(tmp_path, _mutate) == "files_empty"

    def test_row_malformed(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["files"][0]["rows"] = "ten"

        assert self._clause(tmp_path, _mutate) == "row_malformed"

    def test_files_unsorted(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["files"].reverse()

        assert self._clause(tmp_path, _mutate) == "files_unsorted"

    def test_files_duplicate_rel(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["files"][1] = dict(obj["files"][0])
            obj["row_count"] = obj["files"][0]["rows"] * 2

        assert self._clause(tmp_path, _mutate) == "files_unsorted"

    def test_sum_invariant_mismatch(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["row_count"] = obj["row_count"] + 1

        assert self._clause(tmp_path, _mutate) == "sum_invariant"

    def test_sum_invariant_junk_row_count(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["row_count"] = "twenty"

        assert self._clause(tmp_path, _mutate) == "sum_invariant"

    def test_aggregate_bounds_junk(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["min_ts"] = "not-a-time"

        assert self._clause(tmp_path, _mutate) == "aggregate_bounds"

    def test_aggregate_bounds_mismatch(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["max_ts"] = "2027-01-01T00:00:00+00:00"

        assert self._clause(tmp_path, _mutate) == "aggregate_bounds"


class TestScanDatasetRels:
    def test_missing_bars_dir_is_empty(self, tmp_path: Path) -> None:
        assert lsm.scan_dataset_rels(tmp_path, lsm.KIND_BARS, BARS_KEY) == set()

    def test_missing_options_dir_is_empty(self, tmp_path: Path) -> None:
        assert lsm.scan_dataset_rels(tmp_path, lsm.KIND_OPTIONS, OPTIONS_KEY) == set()

    def test_bars_any_depth_scan_sees_strays(self, tmp_path: Path) -> None:
        """The scan mirrors the legacy ``**/*.parquet`` glob: ANY depth, files only.

        A depth-1 / depth-3 stray IS read by the readers' legacy DuckDB glob
        (``**`` matches zero or more directories — empirically verified), so the
        file-set clause must SEE it: a stray-carrying dataset then fails set
        equality against any shard (whose rels are always month-grammar paths)
        and honestly falls back to the live scan instead of silently diverging.
        """
        ddir = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, BARS_KEY)
        (ddir / "2026").mkdir(parents=True)
        (ddir / "2026" / "07.parquet").write_bytes(b"x")
        (ddir / "2026" / "notes.txt").write_bytes(b"x")  # non-parquet leaf: ignored
        (ddir / "2026" / "08.parquet").mkdir()  # a DIR named *.parquet: ignored
        (ddir / "stray.parquet").write_bytes(b"x")  # depth-1 stray: the glob reads it
        (ddir / "2026" / "08.parquet" / "deep.parquet").write_bytes(b"x")  # depth-3 stray
        assert lsm.scan_dataset_rels(tmp_path, lsm.KIND_BARS, BARS_KEY) == {
            "2026/07.parquet",
            "stray.parquet",
            "2026/08.parquet/deep.parquet",
        }

    def test_options_any_depth_scan_sees_strays(self, tmp_path: Path) -> None:
        ddir = lsm.dataset_dir(tmp_path, lsm.KIND_OPTIONS, OPTIONS_KEY)
        ddir.mkdir(parents=True)
        (ddir / "2026-07.parquet").write_bytes(b"x")
        (ddir / "readme.md").write_bytes(b"x")
        (ddir / "sub").mkdir()  # an EMPTY subdir contributes nothing…
        (ddir / "sub" / "nested.parquet").write_bytes(b"x")  # …but a nested stray IS seen
        assert lsm.scan_dataset_rels(tmp_path, lsm.KIND_OPTIONS, OPTIONS_KEY) == {
            "2026-07.parquet",
            "sub/nested.parquet",
        }

    def test_walk_error_propagates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-missing-dir walk failure PROPAGATES (the caller fails closed)."""
        ddir = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, BARS_KEY)
        (ddir / "2026").mkdir(parents=True)
        (ddir / "2026" / "07.parquet").write_bytes(b"x")

        real_scandir = os.scandir

        def _deny(path: Any = None) -> Any:  # os.walk scandirs each dir it visits
            if str(path) == str(ddir / "2026"):
                raise PermissionError("scandir denied")
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", _deny)
        with pytest.raises(PermissionError):
            lsm.scan_dataset_rels(tmp_path, lsm.KIND_BARS, BARS_KEY)

    def test_dir_vanishing_mid_walk_is_fail_closed_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A FileNotFoundError mid-walk returns the EMPTY set (never a partial lie)."""
        ddir = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, BARS_KEY)
        (ddir / "2026").mkdir(parents=True)
        (ddir / "2026" / "07.parquet").write_bytes(b"x")

        real_scandir = os.scandir

        def _vanish(path: Any = None) -> Any:
            if str(path) == str(ddir / "2026"):
                raise FileNotFoundError("dir vanished mid-walk")
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", _vanish)
        assert lsm.scan_dataset_rels(tmp_path, lsm.KIND_BARS, BARS_KEY) == set()


class TestLoadValidDatasetStats:
    def test_happy_path_serves(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)
        with capture_logs() as logs:
            stats = lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is not None
        assert stats.row_count == 20
        assert _fallback_clauses(logs) == []

    def test_disabled_is_silent_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_bars(tmp_path)
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, "off")
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        assert _fallback_clauses(logs) == []

    def test_absent_falls_back_with_clause(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        assert _fallback_clauses(logs) == ["absent"]

    def test_structural_failure_names_clause(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)
        shard.write_text("{corrupt", encoding="utf-8")
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        assert _fallback_clauses(logs) == ["parse"]

    def test_scan_error_falls_back_file_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_bars(tmp_path)

        def _boom(*args: Any, **kwargs: Any) -> set[str]:
            raise PermissionError("scandir denied")

        monkeypatch.setattr(lsm, "scan_dataset_rels", _boom)
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        assert _fallback_clauses(logs) == ["file_set"]

    def test_out_of_band_new_file_falls_back(self, tmp_path: Path) -> None:
        """A hand-copied partition (the #1149 blindness) fails set equality."""
        _seed_bars(tmp_path)
        _write_partition(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 8)
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        assert _fallback_clauses(logs) == ["file_set"]

    def test_out_of_band_delete_falls_back(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)
        (lsm.dataset_dir(tmp_path, lsm.KIND_BARS, BARS_KEY) / "2026" / "07.parquet").unlink()
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        assert _fallback_clauses(logs) == ["file_set"]

    def test_mtime_move_falls_back_stat_token(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)
        target = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, BARS_KEY) / "2026" / "07.parquet"
        stat = os.stat(target)
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        assert _fallback_clauses(logs) == ["stat_token"]

    def test_size_move_falls_back_stat_token(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)
        target = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, BARS_KEY) / "2026" / "06.parquet"
        stat = os.stat(target)
        target.write_bytes(target.read_bytes() + b"+")
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # same mtime, size moved
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        assert _fallback_clauses(logs) == ["stat_token"]

    def _options_shard_with(self, lake: Path, *, rel: str, min_ts: str, max_ts: str) -> None:
        """Hand-write an options shard (bypassing the factory) with live stat tokens."""
        ddir = lsm.dataset_dir(lake, lsm.KIND_OPTIONS, OPTIONS_KEY)
        ddir.mkdir(parents=True, exist_ok=True)
        target = ddir / rel
        target.write_bytes(b"x")
        stat = os.stat(target)
        shard = lsm.stats_shard_path(lake, lsm.KIND_OPTIONS, OPTIONS_KEY)
        shard.parent.mkdir(parents=True, exist_ok=True)
        shard.write_text(
            json.dumps(
                {
                    "version": 1,
                    "kind": "options",
                    "key": dict(OPTIONS_KEY),
                    "identity_key": ["side", "timestamp"],
                    "row_count": 4,
                    "min_ts": min_ts,
                    "max_ts": max_ts,
                    "files": [
                        {
                            "rel": rel,
                            "rows": 4,
                            "min_ts": min_ts,
                            "max_ts": max_ts,
                            "mtime_ns": stat.st_mtime_ns,
                            "size": stat.st_size,
                            "raw_hash": None,
                        }
                    ],
                    "updated_utc": "2026-07-06T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

    def test_month_bounds_violation_falls_back(self, tmp_path: Path) -> None:
        """A stamped bound outside the rel's UTC month self-disables (anti-G-RACE-1)."""
        self._options_shard_with(
            tmp_path,
            rel="2026-07.parquet",
            min_ts="2026-06-30T23:59:00+00:00",  # foreign-month poison
            max_ts="2026-07-02T00:00:00+00:00",
        )
        with capture_logs() as logs:
            assert (
                lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_OPTIONS, key=OPTIONS_KEY)
                is None
            )
        assert _fallback_clauses(logs) == ["month_bounds"]

    def test_month_grammar_junk_rel_falls_back(self, tmp_path: Path) -> None:
        self._options_shard_with(
            tmp_path,
            rel="legacy.parquet",
            min_ts="2026-07-01T00:00:00+00:00",
            max_ts="2026-07-02T00:00:00+00:00",
        )
        with capture_logs() as logs:
            assert (
                lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_OPTIONS, key=OPTIONS_KEY)
                is None
            )
        assert _fallback_clauses(logs) == ["month_bounds"]

    def test_options_happy_path(self, tmp_path: Path) -> None:
        self._options_shard_with(
            tmp_path,
            rel="2026-07.parquet",
            min_ts="2026-07-01T00:00:00+00:00",
            max_ts="2026-07-02T00:00:00+00:00",
        )
        stats = lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_OPTIONS, key=OPTIONS_KEY)
        assert stats is not None and stats.row_count == 4


class TestUnlinkAndProbeHelpers:
    def test_unlink_missing_is_quiet(self, tmp_path: Path) -> None:
        lsm._unlink_shard(tmp_path / "gone.json")  # no raise

    def test_unlink_success(self, tmp_path: Path) -> None:
        target = tmp_path / "s.json"
        target.write_text("{}", encoding="utf-8")
        lsm._unlink_shard(target)
        assert not target.exists()

    def test_unlink_oserror_logged_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "s.json"
        target.write_text("{}", encoding="utf-8")
        real = Path.unlink

        def _boom(self: Path, *args: Any, **kwargs: Any) -> None:
            if self == target:
                raise PermissionError("unlink denied")
            real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _boom)
        with capture_logs() as logs:
            lsm._unlink_shard(target)
        assert any(e["event"] == "lake.stats_manifest.unlink_failed" for e in logs)

    def test_shard_exists_oserror_is_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "s.json"
        target.write_text("{}", encoding="utf-8")
        real = os.stat

        def _boom(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            if str(path) == str(target):
                raise PermissionError("stat denied")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(lsm.os, "stat", _boom)
        assert lsm._shard_exists(target) is False


class TestRecordPartitionStats:
    def _dest(self, lake: Path, year: int = 2026, month: int = 8) -> Path:
        return _write_partition(lake, lsm.KIND_BARS, BARS_KEY, year, month, body=b"new-month")

    def _record(
        self, lake: Path, dest: Path, year: int = 2026, month: int = 8, **overrides: Any
    ) -> bool:
        kwargs: dict[str, Any] = {
            "kind": lsm.KIND_BARS,
            "key": BARS_KEY,
            "dest": dest,
            "rows": 7,
            "min_ts": _ts(year, month, 1),
            "max_ts": _ts(year, month, 3),
            "raw_hash": "c" * 64,
            "stat": os.stat(dest),
        }
        kwargs.update(overrides)
        return lsm.record_partition_stats(lake, **kwargs)

    def test_disabled_no_ops(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_bars(tmp_path)
        dest = self._dest(tmp_path)
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, "off")
        assert self._record(tmp_path, dest) is False

    def test_lazy_accretion_absent_shard_no_op(self, tmp_path: Path) -> None:
        """No shard ⇒ ONE failed stat, immediate False, NO shard created."""
        dest = self._dest(tmp_path)
        assert self._record(tmp_path, dest) is False
        assert not lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, BARS_KEY).exists()

    def test_upsert_new_row(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)
        dest = self._dest(tmp_path)
        assert self._record(tmp_path, dest) is True
        stats = lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is not None
        assert [row.rel for row in stats.files] == [
            "2026/06.parquet",
            "2026/07.parquet",
            "2026/08.parquet",
        ]
        assert stats.row_count == 27  # 10 + 10 + 7

    def test_upsert_replaces_existing_row(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)
        dest = _write_partition(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 7, body=b"rewritten!")
        assert self._record(tmp_path, dest, month=7, rows=99) is True
        stats = lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is not None
        assert stats.row_count == 109  # 10 (june) + 99 (replaced july)
        assert len(stats.files) == 2

    def test_shard_vanished_between_probe_and_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dest = self._dest(tmp_path)
        monkeypatch.setattr(lsm, "_shard_exists", lambda _path: True)
        assert self._record(tmp_path, dest) is False
        assert not lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, BARS_KEY).exists()

    def test_corrupt_shard_unlinked_never_merged(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)
        shard.write_text("{corrupt", encoding="utf-8")
        dest = self._dest(tmp_path)
        with capture_logs() as logs:
            assert self._record(tmp_path, dest) is False
        assert not shard.exists()
        assert any(e["event"] == "lake.stats_manifest.corrupt_unlinked" for e in logs)

    def test_old_version_shard_unlinked(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["version"] = 99

        _mutate_shard(shard, _mutate)
        dest = self._dest(tmp_path)
        assert self._record(tmp_path, dest) is False
        assert not shard.exists()

    def test_bad_kind_returns_false(self, tmp_path: Path) -> None:
        dest = self._dest(tmp_path)
        with capture_logs() as logs:
            assert (
                lsm.record_partition_stats(
                    tmp_path,
                    kind="greeks",
                    key=BARS_KEY,
                    dest=dest,
                    rows=1,
                    min_ts=_ts(2026, 8, 1),
                    max_ts=_ts(2026, 8, 2),
                    raw_hash=None,
                    stat=os.stat(dest),
                )
                is False
            )
        assert any(e["event"] == "lake.stats_manifest.record_failed" for e in logs)

    def test_junk_dest_rel_fails_best_effort(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)
        dest = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, BARS_KEY) / "2026" / "oops.parquet"
        dest.write_bytes(b"x")
        with capture_logs() as logs:
            assert self._record(tmp_path, dest) is False
        assert not shard.exists()
        assert any(e["event"] == "lake.stats_manifest.record_failed" for e in logs)

    def test_month_poisoned_write_self_disables(self, tmp_path: Path) -> None:
        """A foreign-month stamped write unlinks the shard (reader falls back)."""
        shard = _seed_bars(tmp_path)
        dest = self._dest(tmp_path)
        assert self._record(tmp_path, dest, min_ts=_ts(2026, 7, 31)) is False
        assert not shard.exists()

    def test_naive_timestamp_fails_best_effort(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)
        dest = self._dest(tmp_path)
        assert self._record(tmp_path, dest, min_ts=datetime(2026, 8, 1)) is False
        assert not shard.exists()

    def test_env_misconfig_fails_loud_but_contained(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shard = _seed_bars(tmp_path)
        dest = self._dest(tmp_path)
        monkeypatch.setenv(lsm.STATS_LOCK_TIMEOUT_ENV, "banana")
        with capture_logs() as logs:
            assert self._record(tmp_path, dest) is False
        assert not shard.exists()
        assert any(e["event"] == "lake.stats_manifest.record_failed" for e in logs)

    def test_enospc_logged_unlinked_never_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shard = _seed_bars(tmp_path)
        dest = self._dest(tmp_path)

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise OSError(errno.ENOSPC, "no space left on device")

        monkeypatch.setattr(lsm, "atomic_write_simple", _boom)
        with capture_logs() as logs:
            assert self._record(tmp_path, dest) is False
        assert not shard.exists()
        assert any(e["event"] == "lake.stats_manifest.record_failed" for e in logs)

    def test_real_flock_contention_times_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A held shard flock times out (bounded), unlinks, and never raises."""
        shard = _seed_bars(tmp_path)
        dest = self._dest(tmp_path)
        lock_path = partition_lock_path(tmp_path, shard)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            monkeypatch.setenv(lsm.STATS_LOCK_TIMEOUT_ENV, "0.2")
            with capture_logs() as logs:
                assert self._record(tmp_path, dest) is False
        finally:
            os.close(fd)
        assert not shard.exists()
        failed = [e for e in logs if e["event"] == "lake.stats_manifest.record_failed"]
        assert failed and failed[0]["error_type"] == "PartitionLockTimeout"

    def test_lake_write_halt_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuinely full disk at ``.locks/`` creation must HALT, not be swallowed."""
        _seed_bars(tmp_path)
        dest = self._dest(tmp_path)
        monkeypatch.setattr(
            lsm, "partition_write_lock", lambda *a, **k: _raising_lock(LakeWriteHalt("disk_full"))
        )
        with pytest.raises(LakeWriteHalt):
            self._record(tmp_path, dest)


class TestRemovePartitionStats:
    def _dest(self, lake: Path, month: int) -> Path:
        return lsm.dataset_dir(lake, lsm.KIND_BARS, BARS_KEY) / "2026" / f"{month:02d}.parquet"

    def test_disabled_no_ops(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_bars(tmp_path)
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, "off")
        assert (
            lsm.remove_partition_stats(
                tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, dest=self._dest(tmp_path, 7)
            )
            is False
        )

    def test_absent_shard_no_op(self, tmp_path: Path) -> None:
        assert (
            lsm.remove_partition_stats(
                tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, dest=self._dest(tmp_path, 7)
            )
            is False
        )

    def test_bad_kind_returns_false(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            assert (
                lsm.remove_partition_stats(
                    tmp_path, kind="greeks", key=BARS_KEY, dest=self._dest(tmp_path, 7)
                )
                is False
            )
        assert any(e["event"] == "lake.stats_manifest.remove_failed" for e in logs)

    def test_shard_vanished_between_probe_and_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lsm, "_shard_exists", lambda _path: True)
        assert (
            lsm.remove_partition_stats(
                tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, dest=self._dest(tmp_path, 7)
            )
            is False
        )

    def test_remove_one_row_resums(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)
        # Quarantine-move July out of the dataset, then remove its row.
        self._dest(tmp_path, 7).unlink()
        assert (
            lsm.remove_partition_stats(
                tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, dest=self._dest(tmp_path, 7)
            )
            is True
        )
        stats = lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is not None
        assert [row.rel for row in stats.files] == ["2026/06.parquet"]
        assert stats.row_count == 10

    def test_remove_last_row_unlinks_shard(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path, months=((2026, 6),))
        assert (
            lsm.remove_partition_stats(
                tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, dest=self._dest(tmp_path, 6)
            )
            is True
        )
        assert not shard.exists()

    def test_remove_unknown_rel_is_idempotent_noop(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)
        before = shard.read_bytes()
        assert (
            lsm.remove_partition_stats(
                tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, dest=self._dest(tmp_path, 12)
            )
            is True
        )
        assert shard.read_bytes() == before

    def test_corrupt_shard_unlinked(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)
        shard.write_text("{corrupt", encoding="utf-8")
        with capture_logs() as logs:
            assert (
                lsm.remove_partition_stats(
                    tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, dest=self._dest(tmp_path, 7)
                )
                is False
            )
        assert not shard.exists()
        assert any(e["event"] == "lake.stats_manifest.corrupt_unlinked" for e in logs)

    def test_lock_timeout_best_effort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shard = _seed_bars(tmp_path)
        monkeypatch.setattr(
            lsm,
            "partition_write_lock",
            lambda *a, **k: _raising_lock(PartitionLockTimeout(5.0)),
        )
        with capture_logs() as logs:
            assert (
                lsm.remove_partition_stats(
                    tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, dest=self._dest(tmp_path, 7)
                )
                is False
            )
        assert not shard.exists()
        assert any(e["event"] == "lake.stats_manifest.remove_failed" for e in logs)


class TestRemoveDatasetStats:
    def test_disabled_keeps_shard(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        shard = _seed_bars(tmp_path)
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, "off")
        lsm.remove_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert shard.exists()

    def test_bad_kind_logged_not_raised(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            lsm.remove_dataset_stats(tmp_path, kind="greeks", key=BARS_KEY)
        assert any(e["event"] == "lake.stats_manifest.remove_dataset_failed" for e in logs)

    def test_absent_shard_no_op(self, tmp_path: Path) -> None:
        lsm.remove_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)  # no raise

    def test_unlinks_whole_shard(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)
        lsm.remove_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert not shard.exists()

    def test_lock_failure_still_unlinks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shard = _seed_bars(tmp_path)
        monkeypatch.setattr(
            lsm,
            "partition_write_lock",
            lambda *a, **k: _raising_lock(PartitionLockTimeout(5.0)),
        )
        with capture_logs() as logs:
            lsm.remove_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert not shard.exists()
        assert any(e["event"] == "lake.stats_manifest.remove_dataset_lock_failed" for e in logs)


class TestSeedDatasetStats:
    def test_disabled_writes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_partition(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 6)
        row = _row_for(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 6)
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, "off")
        assert (
            lsm.seed_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, files=[row]) is False
        )
        assert not lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, BARS_KEY).exists()

    def test_bad_kind_seed_failed(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            assert lsm.seed_dataset_stats(tmp_path, kind="greeks", key=BARS_KEY, files=[]) is False
        assert any(e["event"] == "lake.stats_manifest.seed_failed" for e in logs)

    def test_empty_files_unlinks_existing(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)
        with capture_logs() as logs:
            assert (
                lsm.seed_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, files=[])
                is False
            )
        assert not shard.exists()
        assert any(e["event"] == "lake.stats_manifest.seed_rejected" for e in logs)

    def test_month_violating_row_rejected_existing_kept(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)
        before = shard.read_bytes()
        bad = lsm.StatsFileRow(
            rel="2026/08.parquet",
            rows=1,
            min_ts=_ts(2026, 7, 31),
            max_ts=_ts(2026, 8, 2),
            mtime_ns=0,
            size=0,
            raw_hash=None,
        )
        with capture_logs() as logs:
            assert (
                lsm.seed_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, files=[bad])
                is False
            )
        assert shard.read_bytes() == before  # never clobbered by a rejected seed
        assert any(e["event"] == "lake.stats_manifest.seed_rejected" for e in logs)

    def test_happy_seed_serves(self, tmp_path: Path) -> None:
        _write_partition(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 6)
        row = _row_for(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 6, rows=42)
        with capture_logs() as logs:
            assert (
                lsm.seed_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, files=[row])
                is True
            )
        assert any(e["event"] == "lake.stats_manifest.seeded" for e in logs)
        stats = lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is not None and stats.row_count == 42

    def test_write_failure_keeps_existing_shard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A seed failure NEVER unlinks — it must lose gracefully to a writer."""
        shard = _seed_bars(tmp_path)
        before = shard.read_bytes()
        _write_partition(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 8)
        row = _row_for(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 8)

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise OSError(errno.ENOSPC, "no space left on device")

        monkeypatch.setattr(lsm, "atomic_write_simple", _boom)
        with capture_logs() as logs:
            assert (
                lsm.seed_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, files=[row])
                is False
            )
        assert shard.read_bytes() == before
        assert any(e["event"] == "lake.stats_manifest.seed_failed" for e in logs)

    def test_lock_timeout_seed_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_partition(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 6)
        row = _row_for(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 6)
        monkeypatch.setattr(
            lsm,
            "partition_write_lock",
            lambda *a, **k: _raising_lock(PartitionLockTimeout(5.0)),
        )
        with capture_logs() as logs:
            assert (
                lsm.seed_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, files=[row])
                is False
            )
        assert any(e["event"] == "lake.stats_manifest.seed_failed" for e in logs)

    def test_lake_write_halt_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_partition(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 6)
        row = _row_for(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 6)
        monkeypatch.setattr(
            lsm, "partition_write_lock", lambda *a, **k: _raising_lock(LakeWriteHalt("disk_full"))
        )
        with pytest.raises(LakeWriteHalt):
            lsm.seed_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, files=[row])
