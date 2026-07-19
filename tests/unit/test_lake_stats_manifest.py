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

import dataclasses
import errno
import fcntl
import hashlib
import json
import os
import re
import signal
import threading
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


@contextmanager
def _hang_watchdog(seconds: int = 5) -> Generator[None, None, None]:
    """Turn an indefinite block inside the body into a LOUD failure.

    The fails-before shape of the round-6 FIFO defect is an ``open(2)`` that
    blocks FOREVER (a reader-side FIFO open waits for a writer), so a plain
    regression test would HANG rather than fail; the ``SIGALRM`` handler
    raising interrupts the blocked syscall and fails the test instead.
    """

    def _alarm(_signum: int, _frame: object) -> None:
        raise AssertionError(f"shard read blocked > {seconds}s (FIFO open hang)")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


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

    @pytest.mark.parametrize(
        "rel",
        [
            "٢٠٢٦/٠٧.parquet",  # Arabic-Indic digits (U+0660-0669), whole rel
            "2026/٠7.parquet",  # one Unicode digit inside an ASCII month
            "２０２６/０７.parquet",  # full-width digits (U+FF10-FF19)
        ],
    )
    def test_bars_unicode_digit_rels_rejected(self, rel: str) -> None:
        """Unicode-decimal rels must fail the ASCII month grammar (endgame round 3).

        Fails-before: the Unicode-aware ``\\d`` matched these and ``int()``
        converted them, so ``٢٠٢٦/٠٧.parquet`` parsed as ``(2026, 7)`` with
        VALID month bounds — yet it sorts lexicographically AFTER every ASCII
        rel, breaking the rel-sort ≡ chronological law the /db page planner's
        prefix-sum walk depends on (a validating shard could serve a WRONG
        page with no fail-closed clause firing).
        """
        assert lsm.parse_rel_month(lsm.KIND_BARS, rel) is None
        assert lsm.rel_month_bounds(lsm.KIND_BARS, rel) is None

    @pytest.mark.parametrize("rel", ["٢٠٢٦-٠٧.parquet", "２０２６-０７.parquet"])
    def test_options_unicode_digit_rels_rejected(self, rel: str) -> None:
        assert lsm.parse_rel_month(lsm.KIND_OPTIONS, rel) is None
        assert lsm.rel_month_bounds(lsm.KIND_OPTIONS, rel) is None

    def test_unicode_digit_trap_is_real(self) -> None:
        """Pin the stdlib behaviours that made the trap possible.

        Documents WHY the grammar must be compiled ``re.ASCII``: the default
        Unicode-aware ``\\d`` really does match Arabic-Indic digits and
        ``int()`` really does convert them (the same interpreter facts behind
        the DEC-CLI-GREEKS-ENRICH-001 ``_is_canonical_digits`` fix). If a
        future Python changed either fact this canary flags the assumption.
        """
        assert int("٢٠٢٦") == 2026
        assert re.match(r"\A\d{4}\Z", "٢٠٢٦") is not None  # Unicode-aware default
        assert re.match(r"\A\d{4}\Z", "٢٠٢٦", re.ASCII) is None  # the fix
        unicode_rel, ascii_rel = "٢٠٢٦/٠٧.parquet", "2027/01.parquet"
        assert unicode_rel > ascii_rel  # a Unicode rel sorts AFTER every ASCII rel

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

    def test_month_bounds_year_9999_december_fails_closed(self) -> None:
        """A year-9999 December rel's next-month bound is year 10000 —
        unrepresentable by ``datetime`` — and previously RAISED ``ValueError:
        year 10000 is out of range`` straight out of the read path's clause-4
        loop (review round 3). Fail-closed ``None`` instead: the caller treats
        the row invalid (only a CRAFTED shard names such a rel)."""
        assert lsm.rel_month_bounds(lsm.KIND_BARS, "9999/12.parquet") is None
        assert lsm.rel_month_bounds(lsm.KIND_OPTIONS, "9999-12.parquet") is None

    def test_month_bounds_year_9999_november_still_representable(self) -> None:
        """Only the December→year-10000 rollover is unrepresentable."""
        bounds = lsm.rel_month_bounds(lsm.KIND_BARS, "9999/11.parquet")
        assert bounds == (datetime(9999, 11, 1, tzinfo=UTC), datetime(9999, 12, 1, tzinfo=UTC))

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
        """Shard flock + partition acquire stay strictly inside the write deadline.

        DEFAULTS ONLY: ``resolve_stats_lock_timeout_seconds`` accepts any
        finite positive env override (a clamp would need the Groww adapter
        constant — a leaf-discipline break), so the interaction is DOCUMENTED
        in the resolver's docstring instead; the doc-pin below keeps that
        caution from silently drifting away.
        """
        from brutex.adapters.groww.constants import GROWW_WRITE_DEADLINE_S
        from brutex.data.partition_lock import DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS

        assert (
            lsm.STATS_LOCK_TIMEOUT_S + DEFAULT_PARTITION_LOCK_TIMEOUT_SECONDS
            < GROWW_WRITE_DEADLINE_S
        )
        # Doc-pin: the resolver must keep naming the write-deadline interaction
        # its env override is NOT clamped against.
        doc = lsm.resolve_stats_lock_timeout_seconds.__doc__ or ""
        assert "GROWW_WRITE_DEADLINE_S" in doc
        assert "NOT clamped" in doc

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

    def test_require_ts_extreme_offset_overflow_is_value_error(self) -> None:
        """An extreme-but-parseable offset timestamp overflows ``astimezone(UTC)``
        with ``OverflowError`` — neither a ``ValueError`` nor an ``OSError``, so
        it previously ESCAPED both the clause-1 parser and the hooks'
        best-effort envelope (review round 3: raised into the pull / 500'd a
        /db read). It must map to the module's fail-closed ``ValueError`` at
        its source — for the string form AND the aware-datetime form."""
        with pytest.raises(ValueError, match="out of datetime range"):
            lsm._require_ts("0001-01-01T00:00:00+05:30", field="min_ts")
        with pytest.raises(ValueError, match="out of datetime range"):
            lsm._require_ts(
                datetime(9999, 12, 31, 23, 0, tzinfo=timezone(timedelta(hours=-5))),
                field="max_ts",
            )

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

    def test_unicode_digit_rel_raises(self) -> None:
        """The factory refuses a Unicode-decimal rel (endgame round 3).

        Fails-before: ``٢٠٢٦/٠٧.parquet`` passed the Unicode-aware grammar as
        ``(2026, 7)`` so the factory would happily persist a shard whose rel
        sort order is NOT chronological order.
        """
        row = lsm.StatsFileRow(
            rel="٢٠٢٦/٠٧.parquet",
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


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop.

    Review round 8: MIGRATION #1614 claimed the module's mutation score
    "rides the standard weekly Mutation workflow" while NO
    ``--paths-to-mutate`` in ``.github/workflows/mutation.yml`` named it —
    an unwired gate presented as a deferred one (Golden Rule #10). The
    module:suite pair now sits in the workflow's per-module ``mutmut`` loop;
    this deterministic pin (the ``TestBenchWorkflowWiring`` precedent) fails
    if the pair is ever dropped. HONEST: it pins the WIRING only — the
    weekly run executes on its own schedule/push triggers.

    Pair format: DEC-MUTATION-RUNTIME-003 (MIGRATION.md #1597, merged in from
    main) restructured the loop onto 3-field ``module:testfile:timeout``
    pairs — an EMPTY third field keeps the exact historical runner. This
    module rides the historical runner, so the pinned literal carries the
    trailing ``:`` (the 2-field form would now mis-parse the TEST PATH as
    the timeout).
    """

    _WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        text = self._WORKFLOW.read_text(encoding="utf-8")
        assert (
            '"brutex/data/lake_stats_manifest.py:tests/unit/test_lake_stats_manifest.py:"' in text
        ), "lake_stats_manifest.py is not wired into mutation.yml's per-module mutmut loop"


class TestMutationHardening:
    """Deterministic pins killing the round-8 in-session mutation survivors.

    The wired mutation gate (``TestMutationWorkflowWiring``) demands ZERO
    surviving mutants, so every behavioural literal the module persists or
    exposes — kind tags, env-var names, the shard-path digest recipe, the
    canonical render, error messages, boundary comparisons, log event
    names — is pinned EXACTLY here. The only remaining provably-equivalent
    mutants (the ``field=`` names feeding messages the loader's ``except``
    clauses DISCARD) carry documented ``# pragma: no mutate`` markers in the
    module instead; endgame round 2 removed the round-8 fd-sentinel /
    bounded-read / dead-``"unknown"`` pragmas by restructuring their lines
    into fully killable shapes (see ``test_loader_fd_close_discipline`` +
    ``test_shard_exactly_at_cap_still_loads`` — several suppressed mutants
    were NOT equivalent, the refuter-confirmed Golden-Rule-#10 gap).
    Placed BEFORE ``TestStructuralClauses`` so ``pytest -x`` under mutmut
    fails FAST on flag mutants whose only later symptom is a 5 s watchdog
    alarm (a "suspicious"-timing classification, not a clean kill).
    """

    # ── schema / knob literals (persisted or operator-facing contracts) ──

    def test_schema_and_knob_literals_pinned(self) -> None:
        assert lsm.KIND_BARS == "bars"
        assert lsm.KIND_OPTIONS == "options"
        assert lsm.STATS_SUBDIRNAME == "stats"
        assert lsm.STATS_MANIFEST_VERSION == 1
        assert lsm.STATS_MANIFEST_OFF == "off"
        assert lsm.STATS_LOCK_TIMEOUT_S == 5.0
        assert lsm.FALLBACK_REL_LOG_CAP == 20

    def test_env_names_pinned_against_real_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The env-var NAMES are the operator contract — literal, not via the constant."""
        monkeypatch.setenv("BRUTEX_LAKE_STATS_MANIFEST", "off")
        assert lsm.stats_manifest_enabled() is False
        monkeypatch.delenv("BRUTEX_LAKE_STATS_MANIFEST")
        assert lsm.stats_manifest_enabled() is True
        monkeypatch.setenv("BRUTEX_STATS_LOCK_TIMEOUT_S", "2.5")
        assert lsm.resolve_stats_lock_timeout_seconds() == 2.5

    def test_shard_path_digest_literal_pin(self, tmp_path: Path) -> None:
        """The digest recipe (sorted-keys compact JSON token) is an on-disk contract."""
        digest = "de2f4528964ee6cb947b89f65cde0201c9d79a1b23b4a4ee667bc4f71326fa59"
        expected = tmp_path / ".manifest" / "stats" / digest[:2] / f"{digest}.json"
        assert lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, BARS_KEY) == expected
        token = (
            '["bars",{"exchange":"NSE","interval":"1minute","segment":"INDEX","symbol":"NIFTY"}]'
        )
        assert hashlib.sha256(token.encode("utf-8")).hexdigest() == digest

    def test_rows_are_frozen_slots_dataclasses(self) -> None:
        row = lsm.StatsFileRow(
            rel="2026/07.parquet",
            rows=1,
            min_ts=_ts(2026, 7, 1),
            max_ts=_ts(2026, 7, 2),
            mtime_ns=0,
            size=0,
            raw_hash=None,
        )
        stats = lsm.build_dataset_stats(kind=lsm.KIND_BARS, key=BARS_KEY, files=[row])
        # via setattr on a REAL field of each class: a literal frozen-field
        # assignment is a type error, and a non-field name would take the
        # frozen __setattr__'s super() path instead of FrozenInstanceError.
        for frozen, attr in ((row, "rel"), (stats, "kind")):
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(frozen, attr, "x")
            assert not hasattr(frozen, "__dict__"), "slots=True must hold (no __dict__)"

    def test_render_shard_canonical_literal_pin(self) -> None:
        """Unit-level schema freeze: the golden suite does NOT run under the
        focused mutmut runner, so the canonical render literal is pinned here
        too (kills the render key-name / sort_keys / separators mutants)."""
        row = lsm.StatsFileRow(
            rel="2026/07.parquet",
            rows=2,
            min_ts=_ts(2026, 7, 1),
            max_ts=_ts(2026, 7, 2),
            mtime_ns=5,
            size=9,
            raw_hash=None,
        )
        stats = lsm.build_dataset_stats(
            kind=lsm.KIND_BARS,
            key=BARS_KEY,
            files=[row],
            updated_utc="2026-07-07T00:00:00+00:00",
        )
        assert lsm.render_shard(stats) == (
            '{"files":[{"max_ts":"2026-07-02T09:15:00+00:00","min_ts":"2026-07-01T09:15:00+00:00"'
            ',"mtime_ns":5,"raw_hash":null,"rel":"2026/07.parquet","rows":2,"size":9}],'
            '"identity_key":["timestamp"],"key":{"exchange":"NSE","interval":"1minute",'
            '"segment":"INDEX","symbol":"NIFTY"},"kind":"bars",'
            '"max_ts":"2026-07-02T09:15:00+00:00","min_ts":"2026-07-01T09:15:00+00:00",'
            '"row_count":2,"updated_utc":"2026-07-07T00:00:00+00:00","version":1}'
        )

    # ── month grammar boundaries ──

    def test_parse_rel_month_lower_bounds(self) -> None:
        assert lsm.parse_rel_month(lsm.KIND_BARS, "0001/01.parquet") == (1, 1)
        assert lsm.parse_rel_month(lsm.KIND_BARS, "0000/01.parquet") is None
        assert lsm.parse_rel_month(lsm.KIND_BARS, "2026/00.parquet") is None
        assert lsm.parse_rel_month(lsm.KIND_OPTIONS, "0001-01.parquet") == (1, 1)

    # ── exact error messages (fail-loud contracts; kills the XX-string mutants) ──

    def test_timeout_env_messages_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cases = [
            (
                "   ",
                "BRUTEX_STATS_LOCK_TIMEOUT_S is set but blank — "
                "set a positive number of seconds or unset it",
            ),
            ("banana", "BRUTEX_STATS_LOCK_TIMEOUT_S='banana' is not a number"),
            ("0", "BRUTEX_STATS_LOCK_TIMEOUT_S='0' must be a finite positive number of seconds"),
        ]
        for raw, message in cases:
            monkeypatch.setenv(lsm.STATS_LOCK_TIMEOUT_ENV, raw)
            with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
                lsm.resolve_stats_lock_timeout_seconds()

    def test_key_and_kind_messages_exact(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"^unknown stats-manifest kind 'greeks'$"):
            lsm.stats_shard_path(tmp_path, "greeks", BARS_KEY)
        with pytest.raises(ValueError, match=r"^unknown stats-manifest kind 'greeks'$"):
            lsm.parse_rel_month("greeks", "2026/07.parquet")
        with pytest.raises(
            ValueError,
            match=r"^stats-manifest key field 'exchange' must be a non-empty string, got None$",
        ):
            lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, {})

    def test_dest_rel_message_exact(self) -> None:
        dest = Path("/lake/bars/NSE/INDEX/NIFTY/1minute/2026/june.parquet")
        message = f"dest {dest!r} does not match the {lsm.KIND_BARS!r} month grammar"
        with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
            lsm.dest_rel(lsm.KIND_BARS, dest)

    def test_require_int_and_ts_messages_exact(self) -> None:
        with pytest.raises(ValueError, match=r"^rows must be an int, got 'x'$"):
            lsm._require_int("x", field="rows", minimum=1)
        with pytest.raises(ValueError, match=r"^rows must be >= 1, got 0$"):
            lsm._require_int(0, field="rows", minimum=1)
        with pytest.raises(ValueError, match=r"^min_ts must be an ISO-8601 timestamp, got 'junk'$"):
            lsm._require_ts("junk", field="min_ts")
        with pytest.raises(ValueError, match=r"^min_ts must be a timestamp, got 42$"):
            lsm._require_ts(42, field="min_ts")
        with pytest.raises(
            ValueError,
            match=r"^min_ts must be timezone-aware, got naive '2026-07-01T09:15:00'$",
        ):
            lsm._require_ts("2026-07-01T09:15:00", field="min_ts")
        with pytest.raises(
            ValueError,
            match=r"^min_ts timestamp out of datetime range: '0001-01-01T00:00:00\+05:30'$",
        ):
            lsm._require_ts("0001-01-01T00:00:00+05:30", field="min_ts")

    def _valid_row(self, **overrides: Any) -> lsm.StatsFileRow:
        base: dict[str, Any] = {
            "rel": "2026/07.parquet",
            "rows": 1,
            "min_ts": _ts(2026, 7, 1),
            "max_ts": _ts(2026, 7, 2),
            "mtime_ns": 0,
            "size": 0,
            "raw_hash": None,
        }
        base.update(overrides)
        return lsm.StatsFileRow(**base)

    def test_normalized_row_messages_name_the_field_exactly(self) -> None:
        naive = datetime(2026, 7, 1, 9, 15)
        cases: list[tuple[lsm.StatsFileRow, str]] = [
            (self._valid_row(rel=""), "rel must be a non-empty string, got ''"),
            (self._valid_row(rows=0), "rows must be >= 1, got 0"),
            (self._valid_row(min_ts=naive), f"min_ts must be timezone-aware, got naive {naive!r}"),
            (self._valid_row(max_ts=naive), f"max_ts must be timezone-aware, got naive {naive!r}"),
            (self._valid_row(min_ts=_ts(2026, 7, 3)), "file '2026/07.parquet' has min_ts > max_ts"),
            (self._valid_row(mtime_ns=-1), "mtime_ns must be >= 0, got -1"),
            (self._valid_row(size=-1), "size must be >= 0, got -1"),
            (self._valid_row(raw_hash=""), "raw_hash must be a non-empty string or None, got ''"),
        ]
        for row, message in cases:
            with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
                lsm._normalized_row(row)

    def test_normalized_row_accepts_equal_bounds(self) -> None:
        """min_ts == max_ts is a VALID single-instant file (the > is strict)."""
        row = self._valid_row(min_ts=_ts(2026, 7, 1), max_ts=_ts(2026, 7, 1))
        assert lsm._normalized_row(row).rows == 1

    def test_build_dataset_stats_messages_exact(self) -> None:
        with pytest.raises(
            ValueError, match=r"^files must be non-empty — an empty dataset has NO shard$"
        ):
            lsm.build_dataset_stats(kind=lsm.KIND_BARS, key=BARS_KEY, files=[])
        dup = [self._valid_row(), self._valid_row(rows=2)]
        rels = ["2026/07.parquet", "2026/07.parquet"]
        with pytest.raises(ValueError, match=f"^{re.escape(f'duplicate rel in files: {rels!r}')}$"):
            lsm.build_dataset_stats(kind=lsm.KIND_BARS, key=BARS_KEY, files=dup)
        with pytest.raises(
            ValueError,
            match=r"^rel 'junk\.parquet' does not match the 'bars' month grammar$",
        ):
            lsm.build_dataset_stats(
                kind=lsm.KIND_BARS, key=BARS_KEY, files=[self._valid_row(rel="junk.parquet")]
            )
        with pytest.raises(
            ValueError, match=r"^file '2026/07\.parquet' bounds lie outside its UTC month$"
        ):
            lsm.build_dataset_stats(
                kind=lsm.KIND_BARS,
                key=BARS_KEY,
                files=[self._valid_row(min_ts=_ts(2026, 6, 30), max_ts=_ts(2026, 7, 2))],
            )

    def test_build_dataset_stats_accepts_min_exactly_at_month_start(self) -> None:
        """The month window is half-open [start, next): min_ts == start is INSIDE."""
        start = datetime(2026, 7, 1, tzinfo=UTC)
        stats = lsm.build_dataset_stats(
            kind=lsm.KIND_BARS,
            key=BARS_KEY,
            files=[self._valid_row(min_ts=start, max_ts=_ts(2026, 7, 2))],
        )
        assert stats.min_ts == start

    def _row_json(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "rel": "2026/07.parquet",
            "rows": 1,
            "min_ts": "2026-07-01T09:15:00+00:00",
            "max_ts": "2026-07-02T09:15:00+00:00",
            "mtime_ns": 0,
            "size": 0,
            "raw_hash": None,
        }
        base.update(overrides)
        return base

    def test_row_from_json_messages_exact(self) -> None:
        with pytest.raises(ValueError, match=r"^file row must be an object, got 42$"):
            lsm._row_from_json(42)
        with pytest.raises(ValueError, match=r"^rel must be a non-empty string, got None$"):
            lsm._row_from_json({})
        with pytest.raises(ValueError, match=r"^raw_hash must be a string or null, got 7$"):
            lsm._row_from_json(self._row_json(raw_hash=7))
        for field, overrides, message in [
            ("rows", {"rows": 0}, "rows must be >= 1, got 0"),
            ("min_ts", {"min_ts": 42}, "min_ts must be a timestamp, got 42"),
            ("max_ts", {"max_ts": 42}, "max_ts must be a timestamp, got 42"),
            ("mtime_ns", {"mtime_ns": -1}, "mtime_ns must be >= 0, got -1"),
            ("size", {"size": -1}, "size must be >= 0, got -1"),
        ]:
            assert field in message  # the message must NAME the offending field
            with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
                lsm._row_from_json(self._row_json(**overrides))

    def test_row_from_json_accepts_minimal_row(self) -> None:
        """rows=1 / mtime_ns=0 / size=0 are all VALID minima (>=, not >)."""
        row = lsm._row_from_json(self._row_json())
        assert (row.rows, row.mtime_ns, row.size) == (1, 0, 0)

    # ── loader boundaries + observability round-trips ──

    def test_loader_opens_shard_nonblocking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The O_NONBLOCK bit is the FIFO-hang guard (review round 6) — pin
        the flags actually passed to ``os.open`` so a ``|``→``&`` mutation
        dies HERE, fast, instead of only via the 5 s watchdog alarm in the
        FIFO tests further down the file (a "suspicious"-timing
        classification under mutmut, not a clean kill)."""
        shard = _seed_bars(tmp_path)
        seen: list[int] = []
        real_open = os.open

        def _spy(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if Path(os.fspath(path)) == shard:
                seen.append(flags)
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", _spy)
        loaded = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert isinstance(loaded, lsm.DatasetStats)
        assert seen and all(flags & os.O_NONBLOCK for flags in seen)

    def test_shard_exactly_at_cap_still_loads(self, tmp_path: Path) -> None:
        """The oversized guard is STRICTLY greater-than: a shard of exactly
        ``MAX_SHARD_BYTES`` must load — padded INSIDE the JSON (whitespace
        before the final ``}``), so the document's LAST byte is the closing
        brace and any read bound short of the full cap truncates it into a
        ``parse`` failure. Endgame round 2: the prior trailing-space padding
        could NOT kill a shortened-read-bound mutant (truncating a trailing
        space still parses), which is exactly why the round-8 read line
        needed its ``# pragma: no mutate``; this padding shape makes the
        boundary a real mutation tooth (the pragma is gone)."""
        shard = _seed_bars(tmp_path)
        body = shard.read_bytes()
        assert body.endswith(b"}")
        pad = lsm.MAX_SHARD_BYTES - len(body)
        shard.write_bytes(body[:-1] + b" " * pad + b"}")
        assert shard.stat().st_size == lsm.MAX_SHARD_BYTES
        loaded = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert isinstance(loaded, lsm.DatasetStats)

    def test_loader_fd_close_discipline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fd ownership handoff is a REAL contract, pinned via an
        ``os.close`` spy (endgame round 2 — kills every mutant of the
        ``owned`` handoff flag; the round-8 ``fd = -1`` line pragmas used to
        suppress this surface, and several suppressed mutants were NOT
        equivalent: a ``+1`` sentinel closes STDOUT on the absent path; a
        clobbered handoff double-closes a descriptor the ``BufferedReader``
        already owns — the classic reused-fd corruption bug. The flag is a
        plain boolean, NOT an ``fd: int | None`` sentinel, because a local
        annotation is never evaluated at runtime [PEP 526] and its
        ``int & None`` annotation-only mutant provably SURVIVES — the first
        endgame-round-2 re-enumeration caught exactly that):

        * ABSENT shard — ``os.open`` raised, nothing was opened, the
          ``finally`` must close NOTHING;
        * SERVED shard — ``os.fdopen`` took ownership, the ``finally`` must
          close NOTHING (the reader closes its own fd internally);
        * NOT-REGULAR shard (FIFO) — opened but never handed off, the
          ``finally`` must close EXACTLY the fd ``os.open`` returned.
        """
        closed: list[int] = []
        opened: list[int] = []
        real_open, real_close = os.open, os.close

        def _open_spy(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            fd = real_open(path, flags, *args, **kwargs)
            opened.append(fd)
            return fd

        def _close_spy(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        monkeypatch.setattr(os, "open", _open_spy)
        monkeypatch.setattr(os, "close", _close_spy)

        # Absent: no fd exists, so no close may happen.
        assert lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) == "absent"
        assert closed == []

        # Served: ownership moved to the BufferedReader — the loader's own
        # finally must not (double-)close it. The reader's internal close is
        # C-level, not os.close, so the spy stays silent. (The seeding writer
        # may legitimately close fds of its own — reset both ledgers so only
        # the LOADER's behaviour is measured.)
        shard = _seed_bars(tmp_path)
        opened.clear()
        closed.clear()
        loaded = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert isinstance(loaded, lsm.DatasetStats)
        assert closed == []

        # Not-regular (FIFO): opened, never handed off — closed exactly once.
        shard.unlink()
        os.mkfifo(shard)
        opened.clear()
        with _hang_watchdog():
            result = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert result == "not_regular"
        assert len(opened) == 1 and closed == opened

    def test_load_shard_stats_corrupt_shard_is_none(self, tmp_path: Path) -> None:
        """The clause-1-only public loader maps ANY clause to an honest
        ``None`` — never a truthy sentinel (pins the union-return adapter
        added in endgame round 2)."""
        shard = _seed_bars(tmp_path)
        shard.write_text("{truncated", encoding="utf-8")
        assert lsm.load_shard_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None

    def test_loaded_stats_preserve_updated_utc(self, tmp_path: Path) -> None:
        """The observability stamp round-trips verbatim (never re-derived)."""
        shard = _seed_bars(tmp_path)
        stamp = json.loads(shard.read_text(encoding="utf-8"))["updated_utc"]
        assert isinstance(stamp, str) and stamp
        loaded = lsm.load_shard_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert loaded is not None and loaded.updated_utc == stamp

    def test_single_file_row_count_one_shard_loads(self, tmp_path: Path) -> None:
        """row_count == 1 is a VALID total (minimum 1, not 2)."""
        _write_partition(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 6)
        row = _row_for(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 6, rows=1)
        assert lsm.seed_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, files=[row])
        stats = lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is not None and stats.row_count == 1

    def test_read_side_serves_min_exactly_at_month_start(self, tmp_path: Path) -> None:
        """Clause 4 mirrors the factory: min_ts == month start is INSIDE."""
        _write_partition(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 6)
        stat = os.stat(lsm.dataset_dir(tmp_path, lsm.KIND_BARS, BARS_KEY) / "2026" / "06.parquet")
        row = lsm.StatsFileRow(
            rel="2026/06.parquet",
            rows=3,
            min_ts=datetime(2026, 6, 1, tzinfo=UTC),
            max_ts=_ts(2026, 6, 15),
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            raw_hash=None,
        )
        assert lsm.seed_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, files=[row])
        assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is not None

    def test_read_side_month_bounds_reject_each_side_independently(self, tmp_path: Path) -> None:
        """max_ts AT the next month's start (half-open) falls back, and a
        min-only violation falls back too (the check is an OR, not an AND) —
        crafted shard bytes, since the factory refuses to build these."""
        for lo, hi in [
            ("2026-06-15T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
            ("2026-05-31T23:59:00+00:00", "2026-06-15T00:00:00+00:00"),
        ]:
            shard = _seed_bars(tmp_path, months=((2026, 6),))

            def _poison(obj: dict[str, Any], lo: str = lo, hi: str = hi) -> None:
                obj["files"][0]["min_ts"] = lo
                obj["files"][0]["max_ts"] = hi
                obj["min_ts"] = lo
                obj["max_ts"] = hi

            _mutate_shard(shard, _poison)
            with capture_logs() as logs:
                assert (
                    lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
                )
            assert _fallback_clauses(logs) == ["month_bounds"]
            shard.unlink()

    # ── write-hook observability event names + env-misconfig containment ──

    def test_record_success_event_name_pinned(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)
        dest = _write_partition(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 8)
        with capture_logs() as logs:
            assert (
                lsm.record_partition_stats(
                    tmp_path,
                    kind=lsm.KIND_BARS,
                    key=BARS_KEY,
                    dest=dest,
                    rows=7,
                    min_ts=_ts(2026, 8, 1),
                    max_ts=_ts(2026, 8, 2),
                    raw_hash=None,
                    stat=os.stat(dest),
                )
                is True
            )
        assert any(e["event"] == "lake.stats_manifest.recorded" for e in logs)

    def test_vanished_shard_between_probe_and_lock_is_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The absent clause under the lock stays LAZY — never the
        corrupt-unlink warn path (record AND remove twins)."""
        monkeypatch.setattr(lsm, "_shard_exists", lambda _path: True)
        dest = _write_partition(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 8)
        with capture_logs() as logs:
            assert (
                lsm.record_partition_stats(
                    tmp_path,
                    kind=lsm.KIND_BARS,
                    key=BARS_KEY,
                    dest=dest,
                    rows=7,
                    min_ts=_ts(2026, 8, 1),
                    max_ts=_ts(2026, 8, 2),
                    raw_hash=None,
                    stat=os.stat(dest),
                )
                is False
            )
            assert (
                lsm.remove_partition_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, dest=dest)
                is False
            )
        assert not any(e["event"] == "lake.stats_manifest.corrupt_unlinked" for e in logs)

    def test_remove_event_names_pinned(self, tmp_path: Path) -> None:
        dest_dir = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, BARS_KEY)
        _seed_bars(tmp_path)
        with capture_logs() as logs:
            assert (
                lsm.remove_partition_stats(
                    tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, dest=dest_dir / "2026/12.parquet"
                )
                is True
            )
        assert any(e["event"] == "lake.stats_manifest.remove_noop" for e in logs)
        with capture_logs() as logs:
            assert (
                lsm.remove_partition_stats(
                    tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, dest=dest_dir / "2026/07.parquet"
                )
                is True
            )
        assert any(e["event"] == "lake.stats_manifest.row_removed" for e in logs)
        with capture_logs() as logs:
            assert (
                lsm.remove_partition_stats(
                    tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, dest=dest_dir / "2026/06.parquet"
                )
                is True
            )
        assert any(e["event"] == "lake.stats_manifest.removed_last_row" for e in logs)

    def test_remove_dataset_event_names_pinned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_bars(tmp_path)
        with capture_logs() as logs:
            lsm.remove_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert any(e["event"] == "lake.stats_manifest.dataset_removed" for e in logs)
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

    def test_seed_empty_files_reason_pinned(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            assert (
                lsm.seed_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, files=[])
                is False
            )
        assert any(
            e["event"] == "lake.stats_manifest.seed_rejected" and e["reason"] == "empty_files"
            for e in logs
        )

    def test_env_misconfig_contained_in_remove_and_seed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A misconfigured timeout knob must be CONSULTED (and contained) by
        every lock-taking hook — remove / remove-dataset / seed; the record
        twin is pinned by ``test_env_misconfig_fails_loud_but_contained``."""
        shard = _seed_bars(tmp_path)
        row = _row_for(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 6)
        monkeypatch.setenv(lsm.STATS_LOCK_TIMEOUT_ENV, "banana")
        with capture_logs() as logs:
            assert (
                lsm.remove_partition_stats(
                    tmp_path,
                    kind=lsm.KIND_BARS,
                    key=BARS_KEY,
                    dest=lsm.dataset_dir(tmp_path, lsm.KIND_BARS, BARS_KEY) / "2026/07.parquet",
                )
                is False
            )
        assert not shard.exists()
        assert any(e["event"] == "lake.stats_manifest.remove_failed" for e in logs)

        with capture_logs() as logs:
            assert (
                lsm.seed_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, files=[row])
                is False
            )
        assert any(e["event"] == "lake.stats_manifest.seed_failed" for e in logs)

        monkeypatch.delenv(lsm.STATS_LOCK_TIMEOUT_ENV)
        shard = _seed_bars(tmp_path)
        monkeypatch.setenv(lsm.STATS_LOCK_TIMEOUT_ENV, "banana")
        with capture_logs() as logs:
            lsm.remove_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert not shard.exists()
        assert any(e["event"] == "lake.stats_manifest.remove_dataset_lock_failed" for e in logs)


class TestStructuralClauses:
    """Clause-1 fail-closed matrix — every structural violation individually."""

    def _clause(self, lake: Path, mutate: Any) -> str:
        shard = lsm.stats_shard_path(lake, lsm.KIND_BARS, BARS_KEY)
        if mutate is not None:
            _mutate_shard(shard, mutate)
        loaded = lsm._load_shard_with_clause(lake, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert isinstance(loaded, str), f"expected a clause name, got {loaded!r}"
        return loaded

    def test_absent(self, tmp_path: Path) -> None:
        loaded = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert loaded == "absent"

    def test_shard_read_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        shard = _seed_bars(tmp_path)
        real = os.open

        def _boom(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if Path(os.fspath(path)) == shard:
                raise PermissionError("read denied")
            return real(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", _boom)
        loaded = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert loaded == "shard_read"

    def test_failure_after_open_is_shard_read_and_closes_the_fd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ``OSError`` AFTER a successful ``os.open`` (fstat / fdopen /
        read on the OPEN descriptor) maps to ``shard_read`` via the SECOND
        handler of the endgame-round-2 owned-flag split (the
        FileNotFoundError→``absent`` mapping applies only to the ``os.open``
        itself), and the still-owned descriptor is closed exactly once."""
        _seed_bars(tmp_path)
        closed: list[int] = []
        real_close = os.close

        def _close_spy(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        def _fstat_boom(fd: int) -> os.stat_result:
            raise PermissionError("fstat denied")

        monkeypatch.setattr(os, "close", _close_spy)
        monkeypatch.setattr(os, "fstat", _fstat_boom)
        loaded = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert loaded == "shard_read"
        assert len(closed) == 1

    def test_fifo_shard_is_fail_closed_not_regular_clause(self, tmp_path: Path) -> None:
        """A FIFO planted at the shard path fails CLOSED as ``not_regular`` —
        the blocking-inode member of the rounds-3/4 "shard replaced
        out-of-band" tamper class (review round 6). Fails-before: the
        pre-round-6 plain ``Path.open("rb")`` blocked inside ``open(2)`` until
        a writer appeared — i.e. FOREVER — BEFORE any fail-closed clause could
        fire (the oversized/parse/UTF-8 guards never ran); the watchdog turns
        that hang into a loud failure. The ``O_NONBLOCK`` +
        ``S_ISREG``-on-the-open-descriptor open returns immediately with the
        new clause, and ``load_valid_dataset_stats`` degrades to the legacy
        scan with the clause named."""
        shard = _seed_bars(tmp_path)
        shard.unlink()
        os.mkfifo(shard)
        with _hang_watchdog():
            loaded = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert loaded == "not_regular"
        with _hang_watchdog(), capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        assert _fallback_clauses(logs) == ["not_regular"]

    def test_oversized_shard_is_fail_closed_oversized_clause(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A shard grown past :data:`MAX_SHARD_BYTES` must fail CLOSED from a
        BOUNDED read — the fails-before shape is a still-VALID-JSON shard
        merely padded past the cap (``json.loads`` ignores trailing
        whitespace), which the pre-round-4 unbounded ``read_text`` loaded
        successfully; a multi-GB out-of-band shard would have been read WHOLE
        on every /db consult AND inside the writers' partition-locked hooks,
        where the resulting ``MemoryError`` is outside
        ``_BEST_EFFORT_ERRORS`` and raises raw into the pull (review
        round 4)."""
        shard = _seed_bars(tmp_path)
        shard.write_bytes(shard.read_bytes() + b" " * 4096)
        monkeypatch.setattr(lsm, "MAX_SHARD_BYTES", 1024)
        loaded = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert loaded == "oversized"
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        assert _fallback_clauses(logs) == ["oversized"]

    def test_oversized_shard_rejected_at_the_real_cap(self, tmp_path: Path) -> None:
        """End-to-end at the REAL 16 MiB cap: a valid-JSON shard padded to
        ``MAX_SHARD_BYTES + 1`` bytes reads ``oversized``; the loader read at
        most ``MAX_SHARD_BYTES + 1`` bytes by construction (the bound is the
        read calls themselves — a cap-sized read plus a ONE-byte over-cap
        probe, race-free — not a ``stat`` pre-check)."""
        shard = _seed_bars(tmp_path)
        body = shard.read_bytes()
        shard.write_bytes(body + b" " * (lsm.MAX_SHARD_BYTES + 1 - len(body)))
        assert shard.stat().st_size == lsm.MAX_SHARD_BYTES + 1
        loaded = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert loaded == "oversized"

    def test_shard_size_cap_is_pinned(self) -> None:
        """The cap is ~1000× a real ~16 KB shard — a deliberate constant pin."""
        assert lsm.MAX_SHARD_BYTES == 16 * 1024 * 1024

    def test_corrupt_json(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)
        shard.write_text("{truncated", encoding="utf-8")
        loaded = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert loaded == "parse"

    def test_non_object_json(self, tmp_path: Path) -> None:
        shard = _seed_bars(tmp_path)
        shard.write_text("[1, 2]", encoding="utf-8")
        loaded = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert loaded == "parse"

    def test_deep_nested_json_is_parse_clause_not_a_crash(self, tmp_path: Path) -> None:
        """A shard replaced with deeply-nested JSON raises ``RecursionError``
        out of ``json.loads`` (a RuntimeError subclass — OUTSIDE the old
        ``JSONDecodeError`` catch AND the hooks' ValueError net); the parser
        must fail CLOSED to the ``parse`` clause, never propagate."""
        shard = _seed_bars(tmp_path)
        depth = 100_000
        shard.write_text("[" * depth + "]" * depth, encoding="utf-8")
        loaded = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert loaded == "parse"
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        assert _fallback_clauses(logs) == ["parse"]

    def test_huge_int_shard_is_parse_clause_not_a_crash(self, tmp_path: Path) -> None:
        """CPython's >4300-digit int-conversion limit raises a PLAIN
        ``ValueError`` out of ``json.loads`` — NOT a ``JSONDecodeError`` — so
        it previously escaped the old ``(JSONDecodeError, RecursionError)``
        catch and turned every /db paging click on the dataset into a 400
        with NO fallback and NO reseed (review round 3). Fail closed to the
        ``parse`` clause like every other arbitrary-shard-bytes input."""
        shard = _seed_bars(tmp_path)
        shard.write_text("9" * 5000, encoding="utf-8")
        loaded = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert loaded == "parse"
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        assert _fallback_clauses(logs) == ["parse"]

    def test_invalid_utf8_shard_is_parse_clause_not_a_crash(self, tmp_path: Path) -> None:
        """Invalid-UTF-8 shard bytes raise ``UnicodeDecodeError`` from
        ``read_text`` — a ``ValueError`` subclass, NOT an ``OSError`` — so it
        previously slipped past BOTH read handlers and escaped raw (review
        round 3). Same fail-closed ``parse`` clause."""
        shard = _seed_bars(tmp_path)
        shard.write_bytes(b"\xff\xfe{}")
        loaded = lsm._load_shard_with_clause(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert loaded == "parse"
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        assert _fallback_clauses(logs) == ["parse"]

    def test_overflow_timestamp_row_is_row_malformed_not_a_crash(self, tmp_path: Path) -> None:
        """The ``astimezone(UTC)`` ``OverflowError`` on a row timestamp
        (e.g. ``0001-01-01T00:00:00+05:30``) previously escaped the parser
        as a raw raise — the round-1 RecursionError CLASS, not just the name
        (review round 3); it must read as a malformed row."""
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["files"][0]["min_ts"] = "0001-01-01T00:00:00+05:30"

        assert self._clause(tmp_path, _mutate) == "row_malformed"

    def test_overflow_timestamp_aggregate_is_aggregate_bounds_not_a_crash(
        self, tmp_path: Path
    ) -> None:
        """The same overflow on the AGGREGATE bound maps to ``aggregate_bounds``."""
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["min_ts"] = "9999-12-31T23:00:00-05:00"

        assert self._clause(tmp_path, _mutate) == "aggregate_bounds"

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

    def test_rel_grammar_junk_rel_is_clause_1(self, tmp_path: Path) -> None:
        """A non-month rel now fails CLOSED at clause 1 (endgame round 3) —
        the chronological-order law needs every rel to PARSE before the
        strictly-increasing check can hold structurally."""
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["files"][0]["rel"] = "junk.parquet"

        assert self._clause(tmp_path, _mutate) == "rel_grammar"

    def test_unicode_digit_rel_is_rel_grammar_clause(self, tmp_path: Path) -> None:
        """The endgame-round-3 attack shape at clause 1: a crafted shard whose
        second rel is the Arabic-Indic twin of a real month. Fails-before:
        the Unicode-aware grammar parsed it as (2026, 7), the rel list
        ``["2026/06.parquet", "٢٠٢٦/٠٧.parquet"]`` IS lexicographically
        sorted + unique, and every other clause held — the shard LOADED, and
        the /db page planner would treat that NON-chronological rel order as
        chronological. The ASCII grammar fails it closed as ``rel_grammar``."""
        _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["files"][1]["rel"] = "٢٠٢٦/٠٧.parquet"

        assert self._clause(tmp_path, _mutate) == "rel_grammar"

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

    def test_file_set_fallback_rel_lists_are_capped(self, tmp_path: Path) -> None:
        """The per-consult ``file_set`` event bounds its rel enumerations at
        ``FALLBACK_REL_LOG_CAP`` (a drifted dataset with thousands of strays
        must never inflate every log line to ~100 KB — log-volume exhaustion);
        the TRUE totals ride the ``extra_count`` / ``missing_count`` ints."""
        _seed_bars(tmp_path)
        ddir = lsm.dataset_dir(tmp_path, lsm.KIND_BARS, BARS_KEY)
        n_strays = lsm.FALLBACK_REL_LOG_CAP + 5
        for i in range(n_strays):
            (ddir / f"stray{i:04d}.parquet").write_bytes(b"x")
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        events = [e for e in logs if e["event"] == "lake.stats_manifest.fallback"]
        assert len(events) == 1 and events[0]["clause"] == "file_set"
        assert len(events[0]["extra"]) == lsm.FALLBACK_REL_LOG_CAP
        assert events[0]["extra_count"] == n_strays
        assert events[0]["missing"] == []
        assert events[0]["missing_count"] == 0
        assert events[0]["extra"] == sorted(f"stray{i:04d}.parquet" for i in range(n_strays))[:20]

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
        """A non-month rel fails closed at clause 1 (``rel_grammar``) since
        endgame round 3 — previously it survived to clause 4 and fell back as
        ``month_bounds``; either way the dataset is legacy-served."""
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
        assert _fallback_clauses(logs) == ["rel_grammar"]

    def test_unicode_digit_rel_shard_falls_back_rel_grammar(self, tmp_path: Path) -> None:
        """The FULL endgame-round-3 attack: a planted ``٢٠٢٦-٠٧.parquet``
        with a crafted shard whose stamped stats all MATCH the live file.
        Fails-before: ``parse_rel_month`` accepted the Arabic-Indic digits
        (Unicode-aware ``\\d`` + ``int()``), ``rel_month_bounds`` returned
        valid July-2026 bounds, file-set equality saw the planted file, the
        stat token matched, and the month-bounds clause held — the shard was
        SERVED, the only tamper class that served a potentially misordered
        page instead of failing closed. The ASCII grammar must reject it at
        clause 1."""
        self._options_shard_with(
            tmp_path,
            rel="٢٠٢٦-٠٧.parquet",
            min_ts="2026-07-01T00:00:00+00:00",
            max_ts="2026-07-02T00:00:00+00:00",
        )
        with capture_logs() as logs:
            assert (
                lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_OPTIONS, key=OPTIONS_KEY)
                is None
            )
        assert _fallback_clauses(logs) == ["rel_grammar"]

    def test_year_9999_december_rel_falls_back_month_bounds(self, tmp_path: Path) -> None:
        """A CRAFTED year-9999 December rel with a matching on-disk file passes
        clauses 1–3, then clause 4's ``rel_month_bounds`` previously RAISED
        ``ValueError: year 10000 is out of range`` straight out of
        ``load_valid_dataset_stats`` — a /db 400 with NO fallback (review
        round 3). It must fail closed to the ``month_bounds`` clause."""
        self._options_shard_with(
            tmp_path,
            rel="9999-12.parquet",
            min_ts="9999-12-01T00:00:00+00:00",
            max_ts="9999-12-02T00:00:00+00:00",
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

    def test_unlink_directory_planted_is_removed(self, tmp_path: Path) -> None:
        """Review round 7 (fails-before): a DIRECTORY planted at the shard
        path is not ``unlink``-able (EISDIR/EPERM), so the heal previously
        never succeeded — the planted tree (NON-empty + nested, so a bare
        ``os.rmdir`` fallback would not suffice) must be removed instead,
        with no ``unlink_failed`` warn."""
        target = tmp_path / "s.json"
        (target / "nested").mkdir(parents=True)
        (target / "nested" / "f.txt").write_text("x", encoding="utf-8")
        with capture_logs() as logs:
            lsm._unlink_shard(target)
        assert not target.exists(), "the planted directory tree must be removed"
        assert any(e["event"] == "lake.stats_manifest.dir_shard_removed" for e in logs)
        assert not any(e["event"] == "lake.stats_manifest.unlink_failed" for e in logs)

    def test_unlink_directory_rmtree_failure_logged_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The never-raises contract holds even when the rmtree heal fails."""
        target = tmp_path / "s.json"
        target.mkdir()

        def _boom(path: Any, *args: Any, **kwargs: Any) -> None:
            raise PermissionError("rmtree denied")

        monkeypatch.setattr(lsm.shutil, "rmtree", _boom)
        with capture_logs() as logs:
            lsm._unlink_shard(target)  # no raise
        assert target.exists()
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


class TestPartitionStatsTracked:
    """The writers' probe-FIRST existence check (review round 2).

    The lazy-accretion pre-probe the write hooks run before ANY other
    per-write work: one shard-path key digest + one ``os.stat`` — an unviewed
    dataset's write pays exactly this and nothing else.
    """

    def test_absent_shard_is_false(self, tmp_path: Path) -> None:
        assert lsm.partition_stats_tracked(tmp_path, lsm.KIND_BARS, BARS_KEY) is False

    def test_present_shard_is_true(self, tmp_path: Path) -> None:
        _seed_bars(tmp_path)
        assert lsm.partition_stats_tracked(tmp_path, lsm.KIND_BARS, BARS_KEY) is True

    def test_kill_switch_is_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_bars(tmp_path)
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, "off")
        assert lsm.partition_stats_tracked(tmp_path, lsm.KIND_BARS, BARS_KEY) is False

    def test_malformed_key_is_false_never_raises(self, tmp_path: Path) -> None:
        """Fail-closed: unknown kind / malformed key → silent False (the hook
        abstains; record_partition_stats still validates loudly for direct
        callers)."""
        assert lsm.partition_stats_tracked(tmp_path, "bogus-kind", BARS_KEY) is False
        assert lsm.partition_stats_tracked(tmp_path, lsm.KIND_BARS, {"exchange": "NSE"}) is False

    def test_probe_is_one_stat_on_the_shard_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The absent-shard cost claim, pinned: ONE os.stat, on the shard path."""
        statted: list[str] = []
        real = os.stat

        def _rec(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            statted.append(str(path))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(lsm.os, "stat", _rec)
        assert lsm.partition_stats_tracked(tmp_path, lsm.KIND_BARS, BARS_KEY) is False
        assert statted == [str(lsm.stats_shard_path(tmp_path, lsm.KIND_BARS, BARS_KEY))]


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

    def test_deep_nested_shard_unlinked_never_raises_into_the_pull(self, tmp_path: Path) -> None:
        """The RecursionError input class inside the WRITER hook: the module's
        'NEVER raises into the pull' contract must hold — the shard is
        treated corrupt (unlink), never merged, never a raw crash."""
        shard = _seed_bars(tmp_path)
        depth = 100_000
        shard.write_text("[" * depth + "]" * depth, encoding="utf-8")
        dest = self._dest(tmp_path)
        with capture_logs() as logs:
            assert self._record(tmp_path, dest) is False
        assert not shard.exists()
        assert any(e["event"] == "lake.stats_manifest.corrupt_unlinked" for e in logs)

    def test_overflow_shard_timestamp_unlinked_never_raises_into_the_pull(
        self, tmp_path: Path
    ) -> None:
        """The OverflowError shard class inside the WRITER hook: an
        extreme-but-parseable stamped timestamp previously raised RAW into
        ``ParquetWriter.write`` and hence the production pull — and, being no
        ``BruteXError``, would have tripped the whole async fetch pool (review
        round 3). It must take the corrupt-shard unlink path instead."""
        shard = _seed_bars(tmp_path)

        def _mutate(obj: dict[str, Any]) -> None:
            obj["files"][0]["min_ts"] = "0001-01-01T00:00:00+05:30"

        _mutate_shard(shard, _mutate)
        dest = self._dest(tmp_path)
        with capture_logs() as logs:
            assert self._record(tmp_path, dest) is False
        assert not shard.exists()
        assert any(e["event"] == "lake.stats_manifest.corrupt_unlinked" for e in logs)

    def test_oversized_shard_unlinked_never_read_whole_into_the_pull(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The multi-GB-shard class inside the WRITER hook (review round 4):
        the spot writer's hook runs inside the held partition locks and
        OUTSIDE any ``lake_write_guard``, so an unbounded read's
        ``MemoryError`` (deliberately NOT in ``_BEST_EFFORT_ERRORS``) would
        raise RAW out of ``ParquetWriter.write`` into the ingest spine —
        neither a ``BruteXError`` (per-symbol quarantine) nor a global stop,
        i.e. a pool-tripping "code bug". The bounded read caps the exposure
        at ``MAX_SHARD_BYTES + 1`` bytes and the oversized shard takes the
        corrupt-unlink path (fails-before: valid JSON padded past the cap
        was previously read whole AND merged into)."""
        shard = _seed_bars(tmp_path)
        shard.write_bytes(shard.read_bytes() + b" " * 4096)
        monkeypatch.setattr(lsm, "MAX_SHARD_BYTES", 1024)
        dest = self._dest(tmp_path)
        with capture_logs() as logs:
            assert self._record(tmp_path, dest) is False
        assert not shard.exists()
        assert any(
            e["event"] == "lake.stats_manifest.corrupt_unlinked" and e["clause"] == "oversized"
            for e in logs
        )

    def test_fifo_shard_unlinked_never_blocks_the_pull(self, tmp_path: Path) -> None:
        """The WRITE-side FIFO exposure (review round 6): the hook re-reads
        the shard INSIDE the held data-partition lock + shard flock, so the
        pre-round-6 blocking ``open(2)`` converted a planted FIFO into a
        permanent per-window ``WriteDeadlineExceeded`` defer loop on the spot
        spine (an accumulating zombie thread per pull) and wedged the
        deadline-less heal hooks outright. The ``not_regular`` clause must
        take the corrupt-unlink path — HEALING the inode — and return within
        the watchdog bound, never raising into the pull."""
        shard = _seed_bars(tmp_path)
        shard.unlink()
        os.mkfifo(shard)
        dest = self._dest(tmp_path)
        with _hang_watchdog(), capture_logs() as logs:
            assert self._record(tmp_path, dest) is False
        assert not shard.exists(), "the FIFO inode must be corrupt-unlinked (healed)"
        assert any(
            e["event"] == "lake.stats_manifest.corrupt_unlinked" and e["clause"] == "not_regular"
            for e in logs
        )

    def test_directory_shard_removed_heals_never_warn_loops(self, tmp_path: Path) -> None:
        """Review round 7 (fails-before): a DIRECTORY planted at the shard
        path fails closed as ``not_regular`` (safe — never a wrong number or
        a raise), but ``Path.unlink`` cannot remove a directory, so the heal
        never succeeded and EVERY later write to the viewed dataset
        re-emitted the ``corrupt_unlinked`` + ``unlink_failed`` warn pair
        forever. The planted tree (non-empty — an ``os.rmdir`` fallback
        would not suffice) must be REMOVED, and the NEXT write must be the
        silent lazy-accretion no-op (absent shard), never a repeat warn."""
        shard = _seed_bars(tmp_path)
        shard.unlink()
        shard.mkdir()
        (shard / "planted.txt").write_text("x", encoding="utf-8")
        dest = self._dest(tmp_path)
        with capture_logs() as logs:
            assert self._record(tmp_path, dest) is False
        assert any(
            e["event"] == "lake.stats_manifest.corrupt_unlinked" and e["clause"] == "not_regular"
            for e in logs
        )
        assert not shard.exists(), "the planted directory must be removed (healed)"
        assert not any(e["event"] == "lake.stats_manifest.unlink_failed" for e in logs)
        with capture_logs() as logs2:
            assert self._record(tmp_path, dest) is False
        assert not any(e["event"] == "lake.stats_manifest.corrupt_unlinked" for e in logs2), (
            "the healed shard path must make the next write a silent lazy no-op"
        )

    def test_future_overflow_error_degrades_best_effort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defence-in-depth: ``OverflowError`` is in ``_BEST_EFFORT_ERRORS``,
        so a future datetime-arithmetic overflow ANYWHERE inside the locked
        section degrades (log + unlink + ``False``) — never into the pull."""
        assert OverflowError in lsm._BEST_EFFORT_ERRORS
        shard = _seed_bars(tmp_path)
        dest = self._dest(tmp_path)
        monkeypatch.setattr(
            lsm,
            "partition_write_lock",
            lambda *a, **k: _raising_lock(OverflowError("future datetime overflow")),
        )
        with capture_logs() as logs:
            assert self._record(tmp_path, dest) is False
        assert not shard.exists()
        failed = [e for e in logs if e["event"] == "lake.stats_manifest.record_failed"]
        assert failed and failed[0]["error_type"] == "OverflowError"

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

    def test_seed_overwrites_fresher_shard_last_writer_wins_then_converges(
        self, tmp_path: Path
    ) -> None:
        """The documented LAST-WRITER-WINS clobber (docstring honesty pin): a
        writer upsert completed BEFORE the seed flock is OVERWRITTEN by the
        seed's older snapshot — and the very next validated read detects the
        stale shard (file-set inequality against the on-disk truth) and falls
        back, so the clobber is fail-closed, never a wrong number."""
        shard = _seed_bars(tmp_path)  # months 6+7
        stale_rows = [
            _row_for(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 6),
            _row_for(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 7),
        ]
        # A concurrent writer lands month 8 + upserts it into the shard.
        dest = _write_partition(tmp_path, lsm.KIND_BARS, BARS_KEY, 2026, 8)
        assert lsm.record_partition_stats(
            tmp_path,
            kind=lsm.KIND_BARS,
            key=BARS_KEY,
            dest=dest,
            rows=5,
            min_ts=_ts(2026, 8, 1),
            max_ts=_ts(2026, 8, 2),
            raw_hash=None,
            stat=os.stat(dest),
        )
        # The (stale) seed now acquires the flock and clobbers the upsert.
        assert lsm.seed_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY, files=stale_rows)
        obj = json.loads(shard.read_text(encoding="utf-8"))
        assert [row["rel"] for row in obj["files"]] == ["2026/06.parquet", "2026/07.parquet"], (
            "the seed is last-writer-wins — the upserted month-8 row is clobbered"
        )
        # Fail-closed convergence: the stale shard can never be SERVED — the
        # on-disk month-8 file fails file-set equality on the next consult.
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(tmp_path, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        assert _fallback_clauses(logs) == ["file_set"]

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


# ────────────────────────────────────────────────────────────────────────
# Read-side stat deadline (DEC-DASHBOARD-COLD-IO-DEADLINE-001, BUG-2a)
# ────────────────────────────────────────────────────────────────────────
#
# REVERSION-GATE DISCIPLINE (ci.yml validate_bugfix_reversion.py): the new
# names (load_valid_dataset_stats_guarded, StatsConsult, bounded_token_sweep,
# TOKEN_SWEEP_* — reached via the module-object ``lsm.`` attribute or imported
# function-locally) never appear in this file's module-level imports, so the
# file still COLLECTS against the merge-base tree and W3 fails with its
# GENUINE watchdog-caught HANG shape rather than a collection error.


@contextmanager
def _wedged_os_stat(
    monkeypatch: pytest.MonkeyPatch, dataset_root: Path
) -> Generator[threading.Event, None, None]:
    """Monkeypatch ``os.stat`` to BLOCK (on a test Event) for *dataset_root* files.

    Everything else delegates to the captured real ``os.stat`` (locally
    captured INSIDE the test — never a module-level capture, the bench
    stat-spy law). The Event is released in the ``finally`` so any abandoned
    guard daemon drains at teardown (zombie hygiene) and the merge-base
    fails-before run is un-wedged after the watchdog fires.
    """
    release = threading.Event()
    real_stat = os.stat
    prefix = str(dataset_root)

    def _wedge(path: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(path, (str, Path)) and str(path).startswith(prefix):
            release.wait(30.0)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", _wedge)
    try:
        yield release
    finally:
        release.set()


def test_consult_deadline_bounds_wedged_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W3 — a WEDGED per-file stat no longer hangs the consult (bounded < 2 s).

    DEC-DASHBOARD-COLD-IO-DEADLINE-001 (guarantee-audit BUG-2a): the clause-3
    D-stat loop's ``os.stat`` on a stalled volume blocked the /db reader's
    MAIN thread forever. Fails-before shape: at the merge-base the wedged
    ``load_valid_dataset_stats`` call blocks until ``_hang_watchdog(5)``
    raises — a clean LOUD failure, never a suite hang (the wedge Event is
    released in the fixture's ``finally``). Pass-after: the (thin-wrapped)
    consult returns ``None`` well inside the watchdog with ONE NAMED
    ``consult_deadline`` fallback + ONE ``stat_gate.deadline_exceeded`` WARN,
    a SECOND consult while wedged fails fast with NO second guard daemon,
    and the released zombie recovers the gate (ONE ``stat_gate.recovered``
    INFO; the next consult revalidates normally with no fallback log).
    """
    lake = tmp_path
    _seed_bars(lake)
    # The literal env name (not an ``lsm`` attribute): the knob lives in the
    # stat_deadline leaf, and this file must still collect at the merge-base.
    monkeypatch.setenv("BRUTEX_LAKE_STAT_DEADLINE_S", "0.05")
    root = lsm.dataset_dir(lake, lsm.KIND_BARS, BARS_KEY)
    with _wedged_os_stat(monkeypatch, root) as release:
        with capture_logs() as logs, _hang_watchdog(5):
            first = lsm.load_valid_dataset_stats(lake, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert first is None
        assert _fallback_clauses(logs) == ["consult_deadline"]
        gate_warns = [e for e in logs if e["event"] == "stat_gate.deadline_exceeded"]
        assert len(gate_warns) == 1
        assert gate_warns[0]["key"] == str(lake)
        # A SECOND consult while wedged: instant guarded verdict, NO second
        # daemon (the gate seam counts), NO second gate WARN.
        guard_calls = {"n": 0}
        original_gate_run = lsm._SWEEP_GATE.run

        def _counting_run(*args: Any, **kwargs: Any) -> Any:
            guard_calls["n"] += 1
            return original_gate_run(*args, **kwargs)

        monkeypatch.setattr(lsm._SWEEP_GATE, "run", _counting_run)
        with capture_logs() as logs2:
            consult = lsm.load_valid_dataset_stats_guarded(lake, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert consult.stats is None
        assert consult.timed_out is True
        assert _fallback_clauses(logs2) == ["consult_deadline"]
        assert [e for e in logs2 if e["event"] == "stat_gate.deadline_exceeded"] == []
        assert guard_calls["n"] == 1  # run() invoked; its LATCH refused a daemon
        assert lsm._SWEEP_GATE.wedged(str(lake))
        release.set()
    # Post-release: the zombie clears the latch (ONE recovered INFO), then a
    # fresh consult revalidates the intact shard with NO fallback log.
    deadline = _monotonic_deadline(5.0)
    while lsm._SWEEP_GATE.wedged(str(lake)) and not deadline():
        pass
    assert not lsm._SWEEP_GATE.wedged(str(lake))
    with capture_logs() as logs3:
        recovered = lsm.load_valid_dataset_stats_guarded(lake, kind=lsm.KIND_BARS, key=BARS_KEY)
    assert recovered.stats is not None
    assert recovered.timed_out is False
    assert _fallback_clauses(logs3) == []


def _monotonic_deadline(seconds: float) -> Any:
    """A zero-arg 'expired?' probe — the bounded-poll rail (never an assertion sleep)."""
    import time

    end = time.monotonic() + seconds

    def _expired() -> bool:
        time.sleep(0.005)
        return time.monotonic() >= end

    return _expired


class TestStatsConsultShapes:
    def test_thin_wrapper_equivalence(self, tmp_path: Path) -> None:
        """``load_valid_dataset_stats(...) ≡ load_valid_dataset_stats_guarded(...).stats``.

        The back-compat pin: the thin public wrapper NEVER raises a new type
        and returns exactly the guarded consult's stats — for a validating
        shard AND for a drift fallback.
        """
        lake = tmp_path
        _seed_bars(lake)
        direct = lsm.load_valid_dataset_stats(lake, kind=lsm.KIND_BARS, key=BARS_KEY)
        guarded = lsm.load_valid_dataset_stats_guarded(lake, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert direct == guarded.stats
        assert direct is not None
        assert guarded.timed_out is False
        # Drift both ways: a touched file fails validation identically.
        dest = lsm.dataset_dir(lake, lsm.KIND_BARS, BARS_KEY) / "2026/07.parquet"
        os.utime(dest, ns=(1, 1))
        with capture_logs() as logs:
            assert lsm.load_valid_dataset_stats(lake, kind=lsm.KIND_BARS, key=BARS_KEY) is None
        assert _fallback_clauses(logs) == ["stat_token"]
        drifted = lsm.load_valid_dataset_stats_guarded(lake, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert drifted.stats is None
        assert drifted.timed_out is False  # data drift is NOT a timeout verdict

    def test_kill_switch_returns_consult_none_with_zero_gate_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kill switch off ⇒ silent ``StatsConsult(None)`` — no gate, no log."""
        lake = tmp_path
        _seed_bars(lake)
        monkeypatch.setenv(lsm.STATS_MANIFEST_ENV, lsm.STATS_MANIFEST_OFF)
        calls = {"n": 0}
        original = lsm._SWEEP_GATE.run

        def _counting(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(lsm._SWEEP_GATE, "run", _counting)
        with capture_logs() as logs:
            consult = lsm.load_valid_dataset_stats_guarded(lake, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert consult == lsm.StatsConsult(None)
        assert consult.timed_out is False
        assert calls["n"] == 0
        assert logs == []

    def test_malformed_key_raises_eagerly_on_the_caller(self, tmp_path: Path) -> None:
        """The ValueError programming-error contract is EAGER — before any guarded I/O."""
        with pytest.raises(ValueError, match="unknown stats-manifest kind"):
            lsm.load_valid_dataset_stats_guarded(tmp_path, kind="nope", key={})

    def test_guard_invocation_constant_in_dataset_depth(self, tmp_path: Path) -> None:
        """ONE gate run per consult regardless of D (the anti-per-file pin).

        A 6-month dataset must cost exactly as many gate invocations as a
        2-month one: 1. Kills the 'wrap each file_token_matches' mutant class
        (which would spawn D daemons per consult).
        """
        lake = tmp_path
        months = tuple((2026, m) for m in range(1, 7))
        _seed_bars(lake, months=months)
        calls = {"n": 0}
        original = lsm._SWEEP_GATE.run

        def _counting(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            return original(*args, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(lsm._SWEEP_GATE, "run", _counting)
            consult = lsm.load_valid_dataset_stats_guarded(lake, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert consult.stats is not None
        assert len(consult.stats.files) == 6
        assert calls["n"] == 1


class TestBoundedTokenSweep:
    def test_clean_pass_is_ok(self, tmp_path: Path) -> None:
        lake = tmp_path
        _seed_bars(lake)
        stats = lsm.load_valid_dataset_stats(lake, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is not None
        root = lsm.dataset_dir(lake, lsm.KIND_BARS, BARS_KEY)
        assert lsm.bounded_token_sweep(lake, root, stats.files) == (lsm.TOKEN_SWEEP_OK, None)

    def test_moved_returns_first_rel_in_iteration_order(self, tmp_path: Path) -> None:
        """The FIRST moved rel in the GIVEN order — today's exact byte-preserved payload."""
        lake = tmp_path
        _seed_bars(lake)
        stats = lsm.load_valid_dataset_stats(lake, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is not None
        root = lsm.dataset_dir(lake, lsm.KIND_BARS, BARS_KEY)
        for rel in ("2026/06.parquet", "2026/07.parquet"):
            os.utime(root / rel, ns=(7, 7))  # move BOTH tokens
        verdict, rel = lsm.bounded_token_sweep(lake, root, stats.files)
        assert (verdict, rel) == (lsm.TOKEN_SWEEP_MOVED, "2026/06.parquet")
        # Reversed input order → the OTHER file is first (order preserved).
        verdict2, rel2 = lsm.bounded_token_sweep(lake, root, tuple(reversed(stats.files)))
        assert (verdict2, rel2) == (lsm.TOKEN_SWEEP_MOVED, "2026/07.parquet")

    def test_wedged_sweep_times_out(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A wedged re-stat pass yields TIMEOUT (bounded), sharing the consult's latch."""
        lake = tmp_path
        _seed_bars(lake)
        stats = lsm.load_valid_dataset_stats(lake, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is not None
        monkeypatch.setenv("BRUTEX_LAKE_STAT_DEADLINE_S", "0.05")
        root = lsm.dataset_dir(lake, lsm.KIND_BARS, BARS_KEY)
        with _wedged_os_stat(monkeypatch, root):
            with _hang_watchdog(5):
                verdict, rel = lsm.bounded_token_sweep(lake, root, stats.files)
            assert (verdict, rel) == (lsm.TOKEN_SWEEP_TIMEOUT, None)
            # The SAME gate + key latched: a consult now fails fast too — a
            # mid-request wedge never pays a second daemon.
            consult = lsm.load_valid_dataset_stats_guarded(lake, kind=lsm.KIND_BARS, key=BARS_KEY)
            assert consult == lsm.StatsConsult(None, timed_out=True)
        deadline = _monotonic_deadline(5.0)
        while lsm._SWEEP_GATE.wedged(str(lake)) and not deadline():
            pass
        assert not lsm._SWEEP_GATE.wedged(str(lake))

    def test_dynamic_os_stat_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sweep resolves ``os.stat`` DYNAMICALLY (the bench stat-spy law).

        ``test_click_fs_ops_flat_in_lake_size`` monkeypatches ``os.stat``
        process-globally; a module-level ``real_stat = os.stat`` capture in
        the new code would blind the spy. Prove the sweep sees a patched
        stat: a spy counting the dataset files' stats must observe D calls.
        """
        lake = tmp_path
        _seed_bars(lake)
        stats = lsm.load_valid_dataset_stats(lake, kind=lsm.KIND_BARS, key=BARS_KEY)
        assert stats is not None
        root = lsm.dataset_dir(lake, lsm.KIND_BARS, BARS_KEY)
        counted = {"n": 0}
        real_stat = os.stat

        def _spy(path: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(path, (str, Path)) and str(path).startswith(str(root)):
                counted["n"] += 1
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", _spy)
        assert lsm.bounded_token_sweep(lake, root, stats.files) == (lsm.TOKEN_SWEEP_OK, None)
        assert counted["n"] == len(stats.files)


class TestRenderShardRustSeamRouting:
    """DEC-LAKE-RUST-STATS-MANIFEST-001 (MIGRATION.md #1914): the render_shard hook.

    Wheel-free (fake crates injected via ``sys.modules``) — the flag-ON arm
    builds the ints+isoformat-strings-only payload and routes through the
    :mod:`brutex.data.lake_rust_stats` seam; the flag-OFF default is
    byte-identical AND crate-untouched (the bomb-crate spy). The seam's own
    guard/burn-in/degrade matrix lives in ``tests/unit/test_lake_rust_stats.py``.
    """

    _CANONICAL = (
        '{"files":[{"max_ts":"2026-07-02T09:15:00+00:00","min_ts":"2026-07-01T09:15:00+00:00"'
        ',"mtime_ns":5,"raw_hash":null,"rel":"2026/07.parquet","rows":2,"size":9}],'
        '"identity_key":["timestamp"],"key":{"exchange":"NSE","interval":"1minute",'
        '"segment":"INDEX","symbol":"NIFTY"},"kind":"bars",'
        '"max_ts":"2026-07-02T09:15:00+00:00","min_ts":"2026-07-01T09:15:00+00:00",'
        '"row_count":2,"updated_utc":"2026-07-07T00:00:00+00:00","version":1}'
    )

    @pytest.fixture(autouse=True)
    def _fresh_seam(self, monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
        from brutex.data import lake_rust_stats

        monkeypatch.delenv(lake_rust_stats.LAKE_RUST_STATS_ENV, raising=False)
        lake_rust_stats.reset_lake_rust_stats_for_tests()
        yield
        lake_rust_stats.reset_lake_rust_stats_for_tests()

    @staticmethod
    def _stats() -> lsm.DatasetStats:
        row = lsm.StatsFileRow(
            rel="2026/07.parquet",
            rows=2,
            min_ts=_ts(2026, 7, 1),
            max_ts=_ts(2026, 7, 2),
            mtime_ns=5,
            size=9,
            raw_hash=None,
        )
        return lsm.build_dataset_stats(
            kind=lsm.KIND_BARS,
            key=BARS_KEY,
            files=[row],
            updated_utc="2026-07-07T00:00:00+00:00",
        )

    @staticmethod
    def _fake_crate(render: object) -> Any:
        import types

        module = types.ModuleType("brutex_lake")
        module.render_stats_shard_json = render  # type: ignore[attr-defined]
        return module

    def test_flag_off_is_byte_identical_and_never_touches_the_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bomb-crate spy: the default OFF arm must never import/touch brutex_lake.

        The spy raises the special-method-standard ``AttributeError`` (the
        CodeQL ``__getattr__`` contract) — but a bare AttributeError alone
        would DEFUSE the bomb: ``lake_rust_stats._resolve_crate`` probes the
        crate via ``getattr(crate, name, None)`` (which swallows
        AttributeError into the ``crate_unavailable`` latch) and
        AttributeError is an explicit ``_DEGRADE_ERRORS`` member (degrades to
        the oracle), so a broken OFF-arm invariant could still serve the
        canonical bytes and pass the byte assert. The RECORDED-touch assert
        below is therefore the un-swallowable teeth: ANY attribute access on
        the poisoned module fails the test loudly, however the exception is
        absorbed downstream.
        """
        import sys

        touched: list[str] = []

        class _Bomb:
            def __getattr__(self, name: str) -> object:
                touched.append(name)
                raise AttributeError(f"flag OFF touched brutex_lake.{name}")

        monkeypatch.setitem(sys.modules, "brutex_lake", _Bomb())
        assert lsm.render_shard(self._stats()) == self._CANONICAL
        assert touched == [], f"flag OFF touched brutex_lake: {touched}"

    def test_flag_on_burn_in_verifies_a_flipped_crate_against_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A flipped-byte crate through the REAL render_shard ships the oracle."""
        import sys

        from brutex.data import lake_rust_stats

        monkeypatch.setenv(lake_rust_stats.LAKE_RUST_STATS_ENV, "1")
        monkeypatch.setitem(
            sys.modules, "brutex_lake", self._fake_crate(render=lambda *a: self._CANONICAL + "X")
        )
        assert lsm.render_shard(self._stats()) == self._CANONICAL
        assert lake_rust_stats._DISABLED_REASON[0] == "burn_in_byte_mismatch"

    def test_flag_on_matching_crate_ships_its_string_and_preformats_the_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        from brutex.data import lake_rust_stats

        monkeypatch.setenv(lake_rust_stats.LAKE_RUST_STATS_ENV, "1")
        seen: list[tuple[object, ...]] = []

        def _echo(*args: object) -> str:
            seen.append(args)
            return self._CANONICAL

        monkeypatch.setitem(sys.modules, "brutex_lake", self._fake_crate(render=_echo))
        assert lsm.render_shard(self._stats()) == self._CANONICAL
        # The hook pre-formats isoformat strings — ints + strings only cross
        # (the DEC's FFI law; every timestamp via the SAME CPython isoformat
        # the stdlib payload dict uses).
        (call,) = seen
        assert call[0] == lsm.STATS_MANIFEST_VERSION
        assert call[1] == lsm.KIND_BARS
        assert call[5] == "2026-07-01T09:15:00+00:00"  # min_ts_iso
        assert call[7] == (
            (
                "2026/07.parquet",
                2,
                "2026-07-01T09:15:00+00:00",
                "2026-07-02T09:15:00+00:00",
                5,
                9,
                None,
            ),
        )
