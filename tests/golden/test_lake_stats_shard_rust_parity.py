"""Rust↔Python byte-parity corpus for the stats-shard render (DEC-LAKE-RUST-STATS-MANIFEST-001).

The §5.4 corpus: the frozen ``_EXPECTED_BARS_SHARD`` golden on BOTH arms
(the cargo twin hardcodes the same literal — the ADR-0020 row-4
cross-language byte pin), options-kind / empty / non-ASCII / Unicode-digit
/ at-cap / i64-boundary shards, the OverflowError + lone-surrogate degrade
laws, the hypothesis differential, and the G7 partition-stem grid mirror.

Skips cleanly when the wheel is absent OR predates the stats-shard export
(a PR-1-era install) — the ``lake-rust-parity`` lane builds the wheel and
runs this file for real (the ``test_lake_rust_render_parity`` posture).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import types
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

brutex_lake = pytest.importorskip(
    "brutex_lake",
    reason="brutex-lake wheel not installed — the lake-rust-parity lane builds it",
)
if not hasattr(brutex_lake, "render_stats_shard_json"):  # pragma: no cover - stale-wheel guard
    pytest.skip(
        "installed brutex_lake predates render_stats_shard_json (PR-1-era wheel) — "
        "rebuild via `pip install rust/brutex-lake`",
        allow_module_level=True,
    )

from brutex.config.rust_strict import (  # noqa: E402
    RUST_STRICT_ENV,
    RustFallbackRefused,
)
from brutex.data import lake_rust_stats as lrs  # noqa: E402
from brutex.data import lake_stats_manifest as lsm  # noqa: E402
from brutex.data import partition  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Generator

pytestmark = pytest.mark.golden

# The frozen schema-freeze literal — copied VERBATIM from
# tests/golden/test_lake_stats_manifest_golden.py (never regenerated; the
# cargo twin `frozen_bars_shard_golden_byte_identical` hardcodes the same
# bytes in rust/brutex-lake/src/stats_shard.rs).
_EXPECTED_BARS_SHARD = (
    '{"files":['
    '{"max_ts":"2026-06-30T10:00:00+00:00","min_ts":"2026-06-01T03:45:00+00:00",'
    '"mtime_ns":1751000000000000001,"raw_hash":"'
    + "a"
    * 64
    + '","rel":"2026/06.parquet","rows":5,"size":1024},'
    '{"max_ts":"2026-07-15T09:59:00+00:00","min_ts":"2026-07-01T03:45:00+00:00",'
    '"mtime_ns":1751500000000000002,"raw_hash":null,"rel":"2026/07.parquet","rows":3,'
    '"size":2048}],'
    '"identity_key":["timestamp"],'
    '"key":{"exchange":"NSE","interval":"1minute","segment":"INDEX","symbol":"NIFTY"},'
    '"kind":"bars",'
    '"max_ts":"2026-07-15T09:59:00+00:00",'
    '"min_ts":"2026-06-01T03:45:00+00:00",'
    '"row_count":8,'
    '"updated_utc":"2026-07-06T00:00:00+00:00",'
    '"version":1}'
)


def _row(
    rel: str,
    rows: int,
    min_ts: datetime,
    max_ts: datetime,
    mtime_ns: int,
    size: int,
    raw_hash: str | None,
) -> lsm.StatsFileRow:
    return lsm.StatsFileRow(
        rel=rel,
        rows=rows,
        min_ts=min_ts,
        max_ts=max_ts,
        mtime_ns=mtime_ns,
        size=size,
        raw_hash=raw_hash,
    )


def _golden_stats() -> lsm.DatasetStats:
    june = _row(
        "2026/06.parquet",
        5,
        datetime(2026, 6, 1, 3, 45, tzinfo=UTC),
        datetime(2026, 6, 30, 10, 0, tzinfo=UTC),
        1751000000000000001,
        1024,
        "a" * 64,
    )
    july = _row(
        "2026/07.parquet",
        3,
        datetime(2026, 7, 1, 3, 45, tzinfo=UTC),
        datetime(2026, 7, 15, 9, 59, tzinfo=UTC),
        1751500000000000002,
        2048,
        None,
    )
    return lsm.build_dataset_stats(
        kind=lsm.KIND_BARS,
        key={"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "1minute"},
        files=[july, june],  # deliberately unsorted — the factory sorts
        updated_utc="2026-07-06T00:00:00+00:00",
    )


def _python_body(stats: lsm.DatasetStats) -> str:
    """The stdlib arm — the flag must RESOLVE OFF so ``render_shard`` runs
    the stdlib body.

    Under the default-ON wave (DEC-RUST-DEFAULT-ON-WAVE-001) an *unset* flag
    routes to the crate, so the OFF context is "resolves falsy" (the suite
    belt's explicit ``'0'``, pinned per-test by ``_off_arm_baseline`` below),
    NOT "``is None``" — the pre-wave assumption. Assert the resolved verdict,
    not the raw env value.
    """
    assert not lrs.lake_rust_stats_enabled()
    return lsm.render_shard(stats)


@contextmanager
def _rust_arm() -> Generator[None]:
    """Flag ON + post-burn-in steady state, restored afterwards.

    ``remaining=0`` ships the CRATE string with no oracle in the path — an
    armed burn-in would cushion a divergence by shipping oracle bytes
    (the ``test_lake_rust_render_parity`` fresh-process-view law).
    """
    # Save-and-restore the PRIOR value (the ``_off_arm_baseline`` '0'), not a
    # bare pop-to-unset: under the default-ON wave an unset flag routes to the
    # crate, so popping would leak ON into a following ``_python_body`` (e.g.
    # the next hypothesis example in the same test invocation, where the
    # per-test autouse baseline does not re-run). DEC-RUST-DEFAULT-ON-WAVE-001.
    prior = os.environ.get(lrs.LAKE_RUST_STATS_ENV)
    os.environ[lrs.LAKE_RUST_STATS_ENV] = "1"
    lrs.reset_lake_rust_stats_for_tests(remaining=0)
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop(lrs.LAKE_RUST_STATS_ENV, None)
        else:
            os.environ[lrs.LAKE_RUST_STATS_ENV] = prior
        lrs.reset_lake_rust_stats_for_tests()


def _rust_body(stats: lsm.DatasetStats) -> str:
    """The crate arm, PROVEN engaged: no fallback event, no latch."""
    with _rust_arm(), capture_logs() as logs:
        body = lsm.render_shard(stats)
    events = [entry["event"] for entry in logs]
    assert "lake_rust_stats.engaged" in events, f"crate never engaged: {events}"
    assert "lake_rust_stats.fallback" not in events, f"crate degraded: {logs}"
    assert not lrs.rust_stats_disabled()
    return body


def _assert_parity(stats: lsm.DatasetStats) -> None:
    python_body = _python_body(stats)
    rust_body = _rust_body(stats)
    assert rust_body == python_body
    assert (
        hashlib.sha256(rust_body.encode("utf-8")).hexdigest()
        == hashlib.sha256(python_body.encode("utf-8")).hexdigest()
    )


@pytest.fixture(autouse=True)
def _off_arm_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic OFF baseline per test under the default-ON wave.

    The suite-wide conftest belt (DEC-RUST-DEFAULT-ON-WAVE-001) sets
    ``BRUTEX_LAKE_RUST_STATS_MANIFEST='0'`` so an unset flag no longer means
    OFF (the wave default is ON). ``_rust_arm`` pops the flag to *unset* on
    exit, which — under the wave — would leak the ON default into the next
    test's ``_python_body``. Pin the belt's explicit ``'0'`` before every
    test so the stdlib arm is deterministic regardless of test order; the
    ``_rust_arm`` context still sets ``'1'`` explicitly for the crate arm.
    """
    monkeypatch.setenv(lrs.LAKE_RUST_STATS_ENV, "0")


class TestShardByteParity:
    def test_frozen_bars_golden_both_arms(self) -> None:
        stats = _golden_stats()
        assert _python_body(stats) == _EXPECTED_BARS_SHARD
        assert _rust_body(stats) == _EXPECTED_BARS_SHARD

    def test_options_kind_shard(self) -> None:
        row = _row(
            "2026-07.parquet",
            4,
            datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
            datetime(2026, 7, 15, 9, 59, tzinfo=UTC),
            77,
            555,
            None,
        )
        stats = lsm.build_dataset_stats(
            kind=lsm.KIND_OPTIONS,
            key={"series": "NSE-NIFTY-30Jul26-24000-CE"},
            files=[row],
            updated_utc="2026-07-16T00:00:00+00:00",
        )
        _assert_parity(stats)

    def test_empty_files_minimal_shard(self) -> None:
        stats = lsm.DatasetStats(
            kind=lsm.KIND_BARS,
            key={"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "1minute"},
            identity_key=("timestamp",),
            row_count=0,
            min_ts=datetime(2026, 7, 1, tzinfo=UTC),
            max_ts=datetime(2026, 7, 1, tzinfo=UTC),
            files=(),
            updated_utc="2026-07-16T00:00:00+00:00",
        )
        _assert_parity(stats)

    def test_non_ascii_and_control_chars_escape_identically(self) -> None:
        hostile = 'é ₹ 😀 "quote" back\\slash\nnew\tline \x1b[0m del\x7f slash/kept'
        row = _row(
            hostile,
            1,
            datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
            datetime(2026, 7, 1, 9, 16, tzinfo=UTC),
            1,
            2,
            "😀" * 8,
        )
        stats = lsm.DatasetStats(
            kind=lsm.KIND_OPTIONS,
            key={"series": hostile},
            identity_key=("side", "timestamp"),
            row_count=1,
            min_ts=datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
            max_ts=datetime(2026, 7, 1, 9, 16, tzinfo=UTC),
            files=(row,),
            updated_utc=hostile,  # caller-supplied text — escaper must handle it hostile
        )
        _assert_parity(stats)

    def test_unicode_digit_rels_render_parity_holds(self) -> None:
        """Render is grammar-blind — the re.ASCII law lives in the VALIDATOR."""
        row = _row(
            "٢٠٢٣-١٢.parquet",
            1,
            datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
            datetime(2026, 7, 1, 9, 16, tzinfo=UTC),
            1,
            2,
            None,
        )
        stats = lsm.DatasetStats(
            kind=lsm.KIND_OPTIONS,
            key={"series": "S"},
            identity_key=("side", "timestamp"),
            row_count=1,
            min_ts=datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
            max_ts=datetime(2026, 7, 1, 9, 16, tzinfo=UTC),
            files=(row,),
            updated_utc="2026-07-16T00:00:00+00:00",
        )
        _assert_parity(stats)
        # ... and the Python VALIDATOR grammar still rejects the same rel
        # (the ASCII-only digit law this corpus documents for any future
        # Rust-side rel parsing).
        assert lsm.parse_rel_month(lsm.KIND_OPTIONS, "٢٠٢٣-١٢.parquet") is None

    def test_microsecond_and_zero_microsecond_isoformat_parity(self) -> None:
        """CPython isoformat: 6-digit micros only when nonzero — both arms."""
        row = _row(
            "2026/07.parquet",
            1,
            datetime(2026, 7, 1, 9, 15, 0, 1, tzinfo=UTC),
            datetime(2026, 7, 1, 9, 16, 30, 999999, tzinfo=UTC),
            1,
            2,
            None,
        )
        stats = lsm.build_dataset_stats(
            kind=lsm.KIND_BARS,
            key={"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "1minute"},
            files=[row],
            updated_utc="2026-07-16T00:00:00.000001+00:00",
        )
        assert '"min_ts":"2026-07-01T09:15:00.000001+00:00"' in _python_body(stats)
        _assert_parity(stats)

    def test_mtime_ns_i64_max_boundary_parity(self) -> None:
        row = _row(
            "2026/07.parquet",
            1,
            datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
            datetime(2026, 7, 1, 9, 16, tzinfo=UTC),
            2**63 - 1,
            2,
            None,
        )
        stats = lsm.build_dataset_stats(
            kind=lsm.KIND_BARS,
            key={"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "1minute"},
            files=[row],
            updated_utc="2026-07-16T00:00:00+00:00",
        )
        stats_body = _rust_body(stats)
        assert '"mtime_ns":9223372036854775807' in stats_body
        assert stats_body == _python_body(stats)

    def test_at_cap_16mib_shard_parity(self) -> None:
        """≈16 MiB body — the MAX_SHARD_BYTES loader-cap scale, both arms.

        No wall-clock gate (shared CI): the render is O(payload) on BOTH
        arms — this pins byte equality at multi-MiB scale, honestly not a
        speed claim.
        """
        template_rows = []
        base = datetime(2020, 1, 1, 9, 15, tzinfo=UTC)
        # ~180 rendered bytes/row → ~110k rows safely crosses 16 MiB.
        for i in range(110_000):
            template_rows.append(
                _row(
                    f"{2020 + i // 12000:04d}/{(i % 12) + 1:02d}.parquet",
                    i,
                    base,
                    base,
                    1_751_000_000_000_000_000 + i,
                    i * 7,
                    None,
                )
            )
        stats = lsm.DatasetStats(
            kind=lsm.KIND_BARS,
            key={"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "1minute"},
            identity_key=("timestamp",),
            row_count=sum(r.rows for r in template_rows),
            min_ts=base,
            max_ts=base,
            files=tuple(template_rows),
            updated_utc="2026-07-16T00:00:00+00:00",
        )
        python_body = _python_body(stats)
        assert len(python_body.encode("utf-8")) > lsm.MAX_SHARD_BYTES
        rust_body = _rust_body(stats)
        assert (
            hashlib.sha256(rust_body.encode("utf-8")).hexdigest()
            == hashlib.sha256(python_body.encode("utf-8")).hexdigest()
        )
        assert rust_body == python_body


class TestDegradeLaws:
    def _one_row_stats(
        self, *, mtime_ns: int = 1, rel: str = "2026/07.parquet"
    ) -> lsm.DatasetStats:
        # DIRECT construction — the factory validates the rel grammar, and
        # the degrade laws exercise render-total inputs the factory refuses
        # (render itself is grammar-blind by design).
        ts_min = datetime(2026, 7, 1, 9, 15, tzinfo=UTC)
        ts_max = datetime(2026, 7, 1, 9, 16, tzinfo=UTC)
        return lsm.DatasetStats(
            kind=lsm.KIND_BARS,
            key={"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "1minute"},
            identity_key=("timestamp",),
            row_count=1,
            min_ts=ts_min,
            max_ts=ts_max,
            files=(_row(rel, 1, ts_min, ts_max, mtime_ns, 2, None),),
            updated_utc="2026-07-16T00:00:00+00:00",
        )

    def test_mtime_ns_beyond_i64_degrades_to_python_with_one_warn(self) -> None:
        """2**63 is unrepresentable crate-side — the oracle serves, strict or not.

        DEC-RUST-STRICT-NO-FALLBACK-001 CLASS C: a filesystem may legitimately
        report an ``mtime_ns`` outside ``i64``, so the crate never claimed this
        input and the stdlib render is the intended handler — NOT a Rust fault.
        ``_beyond_crate`` classifies it BEFORE the FFI under the exempt
        ``render_input_beyond_crate`` reason, and the WARN NAMES the field, so
        the fault-only ``render_error`` token keeps its honest meaning.

        Fails-before-proven: with the gate removed this arrives as
        ``render_error`` and REFUSES under the shipped strict default.
        """
        stats = self._one_row_stats(mtime_ns=2**63)
        expected = _python_body(stats)
        with _rust_arm(), capture_logs() as logs:
            body = lsm.render_shard(stats)
        assert body == expected
        warns = [entry for entry in logs if entry["event"] == "lake_rust_stats.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "render_input_beyond_crate"
        assert warns[0]["field"] == "files[0].mtime_ns"
        assert not lrs.rust_stats_disabled()  # degrade, never a latch

    def test_lone_surrogate_string_degrades_to_python(self) -> None:
        """A lone surrogate cannot cross PyO3 — CLASS C, exempt from strict.

        Legal in a Python ``str``, illegal in Rust's UTF-8 ``String``; CPython
        escapes it as ``\\ud800`` and that render is correct, so the oracle is
        the intended handler here too.
        """
        stats = self._one_row_stats(rel="bad\ud800rel")
        expected = _python_body(stats)  # CPython escapes it as \ud800
        assert "\\ud800" in expected
        with _rust_arm(), capture_logs() as logs:
            body = lsm.render_shard(stats)
        assert body == expected
        warns = [entry for entry in logs if entry["event"] == "lake_rust_stats.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "render_input_beyond_crate"
        assert warns[0]["field"] == "files[0].rel"

    def test_crate_fault_still_refuses_under_strict(self) -> None:
        """The class-C gate must NOT weaken the fault-only ``render_error``.

        A crate that RAN and raised is still a loud refusal — the gate only
        reclassifies inputs the crate never claimed.
        """
        stats = self._one_row_stats()
        broken = types.SimpleNamespace(
            render_stats_shard_json=lambda *args: (_ for _ in ()).throw(RuntimeError("boom")),
            option_partition_stem=lambda *args: "x",
            validate_year_month=lambda *args: None,
        )
        with (
            _rust_arm(),
            pytest.MonkeyPatch.context() as mp,
        ):
            mp.delenv(RUST_STRICT_ENV, raising=False)
            mp.setitem(sys.modules, "brutex_lake", broken)
            with pytest.raises(RustFallbackRefused, match="render_error") as excinfo:
                lsm.render_shard(stats)
        text = str(excinfo.value)
        assert "brutex.data.lake_rust_stats" in text
        assert f"{RUST_STRICT_ENV}=0" in text


class TestHypothesisDifferential:
    @settings(max_examples=50, deadline=None)
    @given(
        rel=st.text(max_size=40),
        series=st.text(min_size=1, max_size=24),
        updated=st.text(max_size=40),
        raw_hash=st.one_of(st.none(), st.text(max_size=64)),
        rows=st.integers(min_value=0, max_value=2**63 - 1),
        size=st.integers(min_value=0, max_value=2**63 - 1),
        mtime=st.integers(min_value=-(2**63), max_value=2**63 - 1),
        micros=st.integers(min_value=0, max_value=999_999),
    )
    def test_random_shards_render_identically(
        self,
        rel: str,
        series: str,
        updated: str,
        raw_hash: str | None,
        rows: int,
        size: int,
        mtime: int,
        micros: int,
    ) -> None:
        ts = datetime(2026, 7, 1, 9, 15, 0, micros, tzinfo=UTC)
        stats = lsm.DatasetStats(
            kind=lsm.KIND_OPTIONS,
            key={"series": series},
            identity_key=("side", "timestamp"),
            row_count=rows,
            min_ts=ts,
            max_ts=ts,
            files=(_row(rel, rows, ts, ts, mtime, size, raw_hash),),
            updated_utc=updated,
        )
        python_body = _python_body(stats)
        assert json.loads(python_body)  # stays valid JSON under hostile text
        assert _rust_body(stats) == python_body


class TestPartitionStemGridMirror:
    """The G7 rider's cross-language grid — the cargo twin
    (`option_partition_stem_matches_python_grid` in
    rust/brutex-lake/src/stats_shard.rs) pins the SAME literals."""

    def test_python_stem_matches_the_pinned_grid(self) -> None:
        assert partition.option_partition_stem(1, 1) == "0001-01"
        assert partition.option_partition_stem(9999, 12) == "9999-12"
        assert partition.option_partition_stem(2024, 6) == "2024-06"
        assert partition.option_partition_stem(2026, 7) == "2026-07"

    def test_python_validate_year_month_messages_match_the_pinned_literals(self) -> None:
        """NIT-2 drift pin: the Python twin's refusal messages equal the
        cargo-pinned literals (`validate_year_month_bounds_and_messages` in
        rust/brutex-lake/src/stats_shard.rs) — the cross-language mirror
        WITHOUT an FFI crossing, message-exact."""
        partition._validate_year_month(2000, 1)  # in-bounds floor: no raise
        partition._validate_year_month(2100, 12)  # in-bounds ceiling: no raise
        cases = (
            (1999, 1, "year must be a 4-digit Gregorian year in [2000, 2100], got 1999"),
            (2101, 1, "year must be a 4-digit Gregorian year in [2000, 2100], got 2101"),
            (2026, 0, "month must be in [1, 12], got 0"),
            (2026, 13, "month must be in [1, 12], got 13"),
        )
        for year, month, expected in cases:
            with pytest.raises(partition.PartitionError) as excinfo:
                partition._validate_year_month(year, month)
            assert str(excinfo.value) == expected
