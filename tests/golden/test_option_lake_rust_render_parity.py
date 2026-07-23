"""Rust-vs-Python OPTIONS-DEFAULTS parquet byte-parity corpus (roadmap PR-2).

DEC-RUST-DATALAKE-IO-ROADMAP-001 (MIGRATION.md #1891). Regenerates SIX of
the PR-2 probe's 7 cases (FEASIBLE-BY-LOCKSTEP 7/7 — retained as
o1-o4/o9/o10; the probe's seventh, the mid-boundary 130k multi-RG case, is
SUPERSEDED by the sharper exact-boundary pair below, its multi-RG coverage
kept by o9) EXTENDED to 12 cases with SIX new cases spanning the five
statistics-surface shape families the probe never drove (all-null nullable
columns · constant single-side Utf8 · the exact 122,880/122,881 RG boundary
PAIR o7+o8 · the ≥256-char binary-stats truncation-default sentinel ·
NaN/+inf Float64 stats), every write-path frame produced through the REAL
``OptionParquetWriter.write()`` / ``heal_partition()`` entries on temp lakes
(provenance law — the ONLY sanctioned hand-builds are the heal suites'
planting idiom for the hand-planted-only shapes o4/o12, then healed through
the REAL entry). Three assertion layers:

1. per-case seam-level byte parity — the captured seam frame rendered
   through the PRODUCTION ``_atomic_write_parquet`` flag-ON steady-state
   (crate bytes, no oracle cushion) vs the flag-OFF on-disk bytes, sha256 +
   raw bytes, with the PER-PROFILE vacuous-green guard (the engaged memo
   key carries the profile, so green MEANS the OPTIONS profile served crate
   bytes) + the per-case P1 pin (BytesIO oracle == on-disk path bytes);
2. whole-path ON/OFF parity through ``write()`` (partition bytes, ``added``
   counts, stats-manifest rows modulo the stat token's ``mtime_ns``);
3. whole-path ON/OFF parity through ``heal_partition`` AND a reassignment
   ``merge_partition_frame``, plus the R3 end-to-end ``heal_findings``
   batch twin over a dup+foreign+null-side poisoned tree (tree sha256 maps
   identical, HealReport field-identical, second sweep clean both arms).

Statistics-byte DIAGNOSTIC pins ((a)–(e) — the triage layer for bump-time
trips; byte equality stays the ONE gate): stats-present negative control ·
footer stats-shape (min/max + EXACT null_count where values exist, all-null
columns carry null_count==rows with NO min/max, distinct_count ABSENT) ·
RG-split ([122880] / [122880, 1] / [122880, 122880, 54240]) · the
long-string truncation sentinel (OBSERVED pinned-rev policy: 64-byte
truncate + max-increment — the probe's "None = untruncated" reading is
empirically corrected in pin (d)) · the NaN-omits-min/max policy.

Deliberately ABSENT cases, with reasons: the EMPTY frame (structurally
unreachable — ``write()`` returns 0 on empty bars, the healer's kept==0 arm
unlinks; the crate keeps a defence-in-depth zero-row cargo test); nested /
unclassifiable key dtypes (REFUSED upstream by the
DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 rounds-12/13/14 gates before any render);
synthetic multi-chunk frames (every seam frame is concat/unique/sort
output — probe-measured ``n_chunks == 1``; the crate ``rechunk_mut()``s
defensively — a disclosure, not a case).

REQUIRES the compiled crate + version lockstep — auto-SKIPS loudly when
either is missing; the ``.github/workflows/lake-rust-parity.yml`` lane (and
a local ``pip install rust/brutex-lake``) is where this suite has teeth.
Corpus byte pins are in-run oracles, not frozen goldens (no committed
parquet fixtures — same-process A/B against the live engine).

Whitebox (DEC-P0-017): drives the private ``_atomic_write_parquet`` seam +
the facade memo directly, so ``reportPrivateUsage`` is silenced.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import atexit
import hashlib
import io
import math
import os
import random
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:  # pragma: no cover — typing only (the runtime `pl` below is
    # an importorskip-resolved VARIABLE, not usable in type expressions)
    import polars

brutex_lake = pytest.importorskip(
    "brutex_lake",
    reason=(
        "brutex-lake crate not installed — the options byte-parity corpus runs in "
        "the lake-rust-parity.yml lane (or locally after `pip install rust/brutex-lake`)"
    ),
)
pl = pytest.importorskip("polars")

from brutex.data import lake_rust_render as lrr  # noqa: E402
from brutex.data import option_parquet_writer as opw  # noqa: E402
from brutex.data.models import OptionBar  # noqa: E402
from brutex.data.option_parquet_writer import (  # noqa: E402
    OptionParquetWriter,
    OptionSeriesCoordinate,
)

if brutex_lake.pinned_polars_version() != pl.__version__:
    pytest.skip(
        "version-guard mismatch: installed polars "
        f"{pl.__version__} != crate pin {brutex_lake.pinned_polars_version()} — "
        "the facade would refuse to engage (fail-closed by design); re-run after "
        "the lockstep ritual (docs/runbooks/lake-rust-render-lockstep.md)",
        allow_module_level=True,
    )

# R14 in-suite belt: the defaults row-group split rides these env vars on
# BOTH engines identically (parity holds per-identical-env), but the corpus
# must prove parity on the PRODUCTION split — assert-unset, never
# silently-unset (the lane carries the same check as a workflow step).
_MORSEL_ENVS = ("POLARS_IDEAL_SINK_MORSEL_SIZE_ROWS", "POLARS_IDEAL_SINK_MORSEL_SIZE_BYTES")
_set_morsels = [name for name in _MORSEL_ENVS if os.environ.get(name)]
if _set_morsels:
    pytest.fail(
        f"{_set_morsels} set in the environment — the corpus would prove parity on a "
        "NON-production row-group split; unset them (production never sets them)",
    )

#: The streaming sink's default RG split (crate DEFAULTS_ROW_GROUP_SPLIT twin).
_RG_SPLIT = 122_880

BASE_TS = datetime(2024, 3, 4, 3, 45, tzinfo=UTC)  # 09:15 IST, a March weekday

_PK = "NSE|NSE_FNO|13|WEEK|1|ATM+3"
_SERIES = "NSE__NSE_FNO__13__WEEK__1__ATM+3"

_COORD = OptionSeriesCoordinate(
    exch_id="NSE",
    exchange_segment="NSE_FNO",
    security_id=13,
    underlying_symbol="NIFTY",
    expiry_flag="WEEK",
    expiry_code=1,
    strike_offset="ATM+3",
)

#: o11's coordinate: a ≥256-char Utf8 column value (LEGAL — `_safe_segment`
#: constrains the partition KEY, not this column) so the corpus BYTES are
#: sensitive to the binary-statistics truncation default (OBSERVED at the
#: pinned rev: 64-byte truncate + max-increment even under the write
#: option's `binary_statistics_truncate_length=None` — pin (d); the most
#: plausible upstream drift, probe caveat 2).
_LONG_SYMBOL = "NIFTY-TRUNCATION-SENTINEL-" + "X" * 240
_LONG_COORD = OptionSeriesCoordinate(
    exch_id="NSE",
    exchange_segment="NSE_FNO",
    security_id=13,
    underlying_symbol=_LONG_SYMBOL,
    expiry_flag="WEEK",
    expiry_code=1,
    strike_offset="ATM+3",
)


def _mk_bar(
    i: int, side: str, *, sparse_nulls: bool, second_spacing: int | None = None
) -> OptionBar:
    """The probe generator's bar shape (seeded — deterministic regeneration)."""
    rng = random.Random(i * 7919 + (0 if side == "CE" else 1))
    base = Decimal(rng.randrange(1_000, 250_000)) / 100
    spread = Decimal(rng.randrange(0, 5_000)) / 100
    o = base
    h = base + spread
    lo = max(base - Decimal(rng.randrange(0, 5_000)) / 100, Decimal("0.05"))
    c = (lo + (h - lo) * Decimal(rng.randrange(0, 101)) / 100).quantize(Decimal("0.01"))
    h = max(h, o, c)
    lo = min(lo, o, c)
    null_row = sparse_nulls and rng.random() < 0.25
    ts = (
        BASE_TS + timedelta(minutes=i)
        if second_spacing is None
        else BASE_TS + timedelta(seconds=i * second_spacing)
    )
    return OptionBar(
        timestamp=ts,
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=rng.randrange(0, 5_000_000),
        open_interest=None if null_row else rng.randrange(0, 10_000_000),
        side=side,
        strike=None if null_row else Decimal(rng.randrange(20_000, 26_000)),
        spot=None if null_row else Decimal(rng.randrange(2_100_000, 2_300_000)) / 100,
        iv=None if null_row else Decimal(rng.randrange(500, 9_000)) / 10_000,
    )


def _bars_mix(
    n: int, *, start: int = 0, sparse_nulls: bool = True, second_spacing: int | None = None
) -> list[OptionBar]:
    return [
        _mk_bar(
            i,
            "CE" if i % 2 == 0 else "PE",
            sparse_nulls=sparse_nulls,
            second_spacing=second_spacing,
        )
        for i in range(start, start + n)
    ]


def _all_null_bar(i: int) -> OptionBar:
    b = _mk_bar(i, "CE" if i % 2 == 0 else "PE", sparse_nulls=False)
    return OptionBar(
        timestamp=b.timestamp,
        open=b.open,
        high=b.high,
        low=b.low,
        close=b.close,
        volume=b.volume,
        open_interest=None,
        side=b.side,
        strike=None,
        spot=None,
        iv=None,
    )


@dataclass
class _Case:
    """One corpus case: the seam-captured frame + the flag-OFF on-disk bytes."""

    name: str
    frame: polars.DataFrame
    off_bytes: bytes
    note: str
    expected_rgs: list[int] | None = None
    meta: dict[str, Any] = field(default_factory=dict)


_CASE_NAMES = [
    "o1_fresh_small_ce_pe_40",
    "o2_merge_overlap_50",
    "o3_heal_dedup_50",
    "o4_heal_nullside_keyed_removal_50",
    "o5_all_null_nullables_50",
    "o6_single_side_ce_only_60",
    "o7_rg_boundary_122880",
    "o8_rg_boundary_122881",
    "o9_multi_rg_300k",
    "o10_realistic_month_16500",
    "o11_long_string_stats_sentinel_40",
    "o12_nan_inf_iv_heal_40",
]

_CORPUS_CACHE: dict[str, _Case] = {}
_TMP_DIRS: list[str] = []


def _mkdtemp(prefix: str) -> Path:
    d = tempfile.mkdtemp(prefix=prefix)
    _TMP_DIRS.append(d)
    return Path(d)


@atexit.register
def _cleanup_tmp_dirs() -> None:  # pragma: no cover — process-exit housekeeping
    for d in _TMP_DIRS:
        shutil.rmtree(d, ignore_errors=True)


def _dest(lake: Path, series: str = _SERIES) -> Path:
    return lake / "options" / series / "2024-03.parquet"


def _build_corpus() -> dict[str, _Case]:
    """Build all 12 cases ONCE per session through the REAL writer entries.

    Generation runs flag-OFF (forced — the lane's heal-twin step exports the
    flag, this module must capture the LEGACY bytes) with a WRAPPING capture
    at the ``_atomic_write_parquet`` seam: the captured frame is the exact
    frame the production writer serialized, the on-disk bytes the compared
    Python bytes.
    """
    if _CORPUS_CACHE:
        return _CORPUS_CACHE
    mp = pytest.MonkeyPatch()
    captured: list[tuple[Path, polars.DataFrame]] = []
    real_awp = opw._atomic_write_parquet

    def _capturing(dest: Path, df: polars.DataFrame) -> None:
        captured.append((dest, df))
        real_awp(dest, df)

    # Explicit "0" (kill switch), NOT delenv — the wave default is ON, so an
    # unset OFF arm captures RUST bytes (a vacuous Rust-vs-Rust corpus compare).
    mp.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")
    mp.setattr(opw, "_atomic_write_parquet", _capturing)
    try:
        cases: dict[str, _Case] = {}

        def _take(name: str, note: str, **kwargs: Any) -> None:
            (dest, frame) = captured[-1]
            cases[name] = _Case(
                name=name, frame=frame, off_bytes=dest.read_bytes(), note=note, **kwargs
            )

        # ── o1..o4: the chained fresh → merge → heal → null-side sequence ──
        lake = _mkdtemp("pr2-o1-")
        writer = OptionParquetWriter(lake_root=lake)
        captured.clear()
        written = writer.write(partition_key=_PK, coordinate=_COORD, bars=_bars_mix(40))
        assert written > 0
        assert len(captured) == 1
        _take("o1_fresh_small_ce_pe_40", "fresh render, 40 CE/PE bars, sparse nulls")

        captured.clear()
        writer.write(partition_key=_PK, coordinate=_COORD, bars=_bars_mix(30, start=20))
        assert len(captured) == 1
        _take("o2_merge_overlap_50", "merge rewrite: existing 40 + 30 (20 dup keys)")

        dest = _dest(lake)
        stored = pl.read_parquet(dest, glob=False)
        pl.concat([stored, stored.slice(0, 7)]).write_parquet(dest)  # heal-suite planting
        qdir = lake / ".quarantine" / "corpus"
        qdir.mkdir(parents=True)
        captured.clear()
        res = writer.heal_partition(dest, quarantine_to=qdir / "2024-03-c.parquet")
        assert res.changed and res.dropped_duplicates == 7
        assert len(captured) == 1
        _take("o3_heal_dedup_50", "heal rewrite: keep-first dedup of 7 dups")

        null_side_row = stored.slice(0, 1).with_columns(
            pl.lit(None, dtype=pl.String).alias("side"),
            (pl.col("timestamp") + pl.duration(minutes=999)).alias("timestamp"),
        )
        pl.concat([stored, null_side_row, null_side_row]).write_parquet(dest)
        remove_key = (stored.get_column("side")[3], stored.get_column("timestamp")[3])
        captured.clear()
        res = writer.heal_partition(
            dest, remove_keys={remove_key}, quarantine_to=qdir / "2024-03-d.parquet"
        )
        assert res.changed and res.dropped_removed == 1 and res.dropped_duplicates == 1
        assert len(captured) == 1
        _take(
            "o4_heal_nullside_keyed_removal_50",
            "heal rewrite: null-side row kept (side null_count>0), 1 keyed removal",
        )

        # ── o5: fresh, all-null nullable columns (whole-column-null stats) ──
        lake5 = _mkdtemp("pr2-o5-")
        captured.clear()
        OptionParquetWriter(lake_root=lake5).write(
            partition_key=_PK, coordinate=_COORD, bars=[_all_null_bar(i) for i in range(50)]
        )
        assert len(captured) == 1
        _take(
            "o5_all_null_nullables_50",
            "fresh render, all-None OI/strike/spot/iv (min/max ABSENT, null_count=N)",
        )

        # ── o6: fresh, single-side CE only (constant Utf8 → min == max) ──
        lake6 = _mkdtemp("pr2-o6-")
        captured.clear()
        OptionParquetWriter(lake_root=lake6).write(
            partition_key=_PK,
            coordinate=_COORD,
            bars=[_mk_bar(i, "CE", sparse_nulls=True) for i in range(60)],
        )
        assert len(captured) == 1
        _take("o6_single_side_ce_only_60", "fresh render, constant Utf8 side (min==max)")

        # ── o7/o8/o9: the defaults RG-split boundaries (second-spaced, one month) ──
        for name, rows, spacing, rgs in (
            ("o7_rg_boundary_122880", _RG_SPLIT, 14, [_RG_SPLIT]),
            ("o8_rg_boundary_122881", _RG_SPLIT + 1, 14, [_RG_SPLIT, 1]),
            ("o9_multi_rg_300k", 300_000, 6, [_RG_SPLIT, _RG_SPLIT, 300_000 - 2 * _RG_SPLIT]),
        ):
            lake_big = _mkdtemp(f"pr2-{name[:3]}-")
            captured.clear()
            OptionParquetWriter(lake_root=lake_big).write(
                partition_key=_PK,
                coordinate=_COORD,
                bars=_bars_mix(rows, sparse_nulls=True, second_spacing=spacing),
            )
            assert len(captured) == 1
            _take(name, f"fresh render {rows} rows → row groups {rgs}", expected_rgs=rgs)

        # ── o10: realistic ~16.5k-row month file via an overlap merge ──
        lake10 = _mkdtemp("pr2-o10-")
        writer10 = OptionParquetWriter(lake_root=lake10)
        month_bars: list[OptionBar] = []
        for day in range(22):
            for m in range(375):
                i = day * 375 + m
                for side in ("CE", "PE"):
                    b = _mk_bar(i * 2 + (side == "PE"), side, sparse_nulls=True)
                    month_bars.append(
                        OptionBar(
                            timestamp=BASE_TS + timedelta(days=day, minutes=m),
                            open=b.open,
                            high=b.high,
                            low=b.low,
                            close=b.close,
                            volume=b.volume,
                            open_interest=b.open_interest,
                            side=side,
                            strike=b.strike,
                            spot=b.spot,
                            iv=b.iv,
                        )
                    )
        half = len(month_bars) // 2
        writer10.write(partition_key=_PK, coordinate=_COORD, bars=month_bars[:half])
        captured.clear()
        writer10.write(partition_key=_PK, coordinate=_COORD, bars=month_bars[half - 500 :])
        assert len(captured) == 1
        _take(
            "o10_realistic_month_16500",
            "realistic ~16.5k-row month file via overlap merge (the production shape)",
        )

        # ── o11: the ≥256-char binary-stats truncation-default sentinel ──
        lake11 = _mkdtemp("pr2-o11-")
        captured.clear()
        OptionParquetWriter(lake_root=lake11).write(
            partition_key=_PK, coordinate=_LONG_COORD, bars=_bars_mix(40)
        )
        assert len(captured) == 1
        _take(
            "o11_long_string_stats_sentinel_40",
            "fresh render, >=256-char underlying_symbol (truncation-default sentinel)",
        )

        # ── o12: NaN/+inf iv heal (hand-planted, healed through the REAL entry) ──
        lake12 = _mkdtemp("pr2-o12-")
        writer12 = OptionParquetWriter(lake_root=lake12)
        writer12.write(partition_key=_PK, coordinate=_COORD, bars=_bars_mix(40, sparse_nulls=False))
        dest12 = _dest(lake12)
        df12 = pl.read_parquet(dest12, glob=False)
        planted = df12.with_columns(
            pl.when(pl.int_range(pl.len()) == 0)
            .then(float("nan"))
            .when(pl.int_range(pl.len()) == 1)
            .then(float("inf"))
            .otherwise(pl.col("iv"))
            .alias("iv")
        )
        pl.concat([planted, planted.slice(5, 3)]).write_parquet(dest12)  # + 3 dup keys
        q12 = lake12 / ".quarantine" / "corpus"
        q12.mkdir(parents=True)
        captured.clear()
        res = writer12.heal_partition(dest12, quarantine_to=q12 / "2024-03.parquet")
        assert res.changed and res.dropped_duplicates == 3
        assert len(captured) == 1
        _take(
            "o12_nan_inf_iv_heal_40",
            "heal dup rewrite of a hand-planted NaN/+inf-iv file (non-finite stats)",
        )

        assert sorted(cases) == sorted(_CASE_NAMES)
        _CORPUS_CACHE.update(cases)
        return _CORPUS_CACHE
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def corpus() -> dict[str, _Case]:
    return _build_corpus()


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    lrr.reset_lake_rust_render_for_tests()
    yield
    lrr.reset_lake_rust_render_for_tests()


_OPTIONS_ENGAGED_KEY = lrr._engaged_memo_key(lrr.PROFILE_OPTIONS_DEFAULTS)


def _assert_options_crate_rendered() -> None:
    """The PER-PROFILE vacuous-green guard (the PR-1 refuter-round idiom).

    The warn-only degrade paths (``render_error`` / ``render_result_refused``
    / ``profile_unknown``) serve ORACLE bytes WITHOUT latching — without this
    witness a crate whose DEFAULTS pyfunction raises on every call would
    green the gate comparing python-to-python. Three legs: no latch; the
    OPTIONS profile's engaged memo key present (recorded ONLY after the
    crate returned genuine bytes on THIS profile — the per-test reset clears
    the memo); no fallback reason in the warn memo.
    """
    assert not lrr.rust_render_disabled(), "the Rust path latched OFF mid-corpus"
    assert _OPTIONS_ENGAGED_KEY in lrr._OBSERVABILITY_MEMO, (
        "lake_rust_render.engaged never fired for the OPTIONS profile — the crate "
        "never served a defaults render (vacuous green: both sides would be the oracle)"
    )
    fallbacks = {k for k in lrr._OBSERVABILITY_MEMO if not k.startswith(lrr._ENGAGED_KEY)}
    assert not fallbacks, f"oracle fallback fired mid-corpus: {sorted(fallbacks)}"


def _rust_bytes_via_seam(frame: polars.DataFrame, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """Render *frame* through the PRODUCTION seam, flag-ON steady-state.

    ``remaining=0`` means the on-disk bytes are the CRATE's output with no
    oracle compare in the path; the per-profile vacuous-green guard proves
    the crate genuinely produced them.
    """
    monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
    lrr.reset_lake_rust_render_for_tests(remaining=0)
    scratch = _mkdtemp("pr2-rust-") / "render.parquet"
    opw._atomic_write_parquet(scratch, frame)
    _assert_options_crate_rendered()
    return scratch.read_bytes()


# ---------------------------------------------------------------------------
# Layer 1 — per-case seam byte parity (+ the per-case P1 pin)
# ---------------------------------------------------------------------------


class TestSeamByteParity:
    @pytest.mark.parametrize("case_name", _CASE_NAMES)
    def test_corpus_case_bit_identical(
        self,
        case_name: str,
        corpus: dict[str, _Case],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        case = corpus[case_name]
        # P1 per-case pin: the BytesIO oracle is byte-representative of what
        # flag-OFF streamed to the tmp path (the on-disk bytes).
        assert opw._defaults_python_render(case.frame) == case.off_bytes
        rust = _rust_bytes_via_seam(case.frame, monkeypatch)
        py_sha = hashlib.sha256(case.off_bytes).hexdigest()
        rust_sha = hashlib.sha256(rust).hexdigest()
        verdict = "MATCH" if rust == case.off_bytes else "DIVERGE"
        with capsys.disabled():
            print(
                f"parity {case_name}: rows={case.frame.height} py_sha256={py_sha[:16]} "
                f"rust_sha256={rust_sha[:16]} bytes={len(case.off_bytes)} verdict={verdict}"
            )
        assert rust_sha == py_sha
        assert rust == case.off_bytes  # raw bytes, not just the digest

    def test_o3_heal_bytes_equal_o2_merge_bytes(self, corpus: dict[str, _Case]) -> None:
        """The probe's determinism datum: healing the dup-poisoned o2 file
        collapses back to EXACTLY the pre-poison content."""
        assert corpus["o3_heal_dedup_50"].off_bytes == corpus["o2_merge_overlap_50"].off_bytes

    def test_burn_in_path_agrees_and_never_autoreverts(
        self, corpus: dict[str, _Case], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The engaged burn-in compare passes on real defaults data."""
        case = corpus["o1_fresh_small_ce_pe_40"]
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        lrr.reset_lake_rust_render_for_tests()  # full burn-in budget
        scratch = _mkdtemp("pr2-burnin-") / "render.parquet"
        opw._atomic_write_parquet(scratch, case.frame)
        assert scratch.read_bytes() == case.off_bytes
        _assert_options_crate_rendered()
        assert not lrr.rust_render_disabled()


# ---------------------------------------------------------------------------
# Layer 2 — whole-path ON/OFF parity through write()
# ---------------------------------------------------------------------------


class TestWholePathWriteParity:
    def test_write_on_off_byte_and_manifest_parity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.data import lake_stats_manifest as lsm

        first = _bars_mix(40)
        second = _bars_mix(30, start=20)

        def _drive(lake: Path) -> tuple[int, int]:
            w = OptionParquetWriter(lake_root=lake)
            a1 = w.write(partition_key=_PK, coordinate=_COORD, bars=list(first))
            # Seed the shard from disk so the SECOND write's merge upserts it
            # (lazy accretion — the writer never creates an absent shard).
            dest = _dest(lake)
            stat = os.stat(dest)
            stored = pl.read_parquet(dest, glob=False)
            row = lsm.StatsFileRow(
                rel=dest.name,
                rows=stored.height,
                min_ts=stored.get_column("timestamp").min(),
                max_ts=stored.get_column("timestamp").max(),
                mtime_ns=stat.st_mtime_ns,
                size=stat.st_size,
                raw_hash=None,
            )
            assert lsm.seed_dataset_stats(
                lake, kind=lsm.KIND_OPTIONS, key={"series": _SERIES}, files=[row]
            )
            a2 = w.write(partition_key=_PK, coordinate=_COORD, bars=list(second))
            return a1, a2

        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")  # genuine oracle OFF arm
        off_lake = _mkdtemp("pr2-w-off-")
        off_added = _drive(off_lake)

        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        lrr.reset_lake_rust_render_for_tests(remaining=0)
        on_lake = _mkdtemp("pr2-w-on-")
        on_added = _drive(on_lake)
        _assert_options_crate_rendered()

        assert on_added == off_added
        off_files = sorted(
            p.relative_to(off_lake) for p in (off_lake / "options").rglob("*.parquet")
        )
        on_files = sorted(p.relative_to(on_lake) for p in (on_lake / "options").rglob("*.parquet"))
        assert off_files == on_files and off_files
        for rel in off_files:
            assert (on_lake / rel).read_bytes() == (off_lake / rel).read_bytes()
        # Stats-manifest rows equal modulo the stat token's mtime_ns (the two
        # writes land at different wall-clock instants; size/rows/bounds must
        # agree — asserting full-token equality would be a false gate).
        off_stats = lsm.load_shard_stats(off_lake, kind=lsm.KIND_OPTIONS, key={"series": _SERIES})
        on_stats = lsm.load_shard_stats(on_lake, kind=lsm.KIND_OPTIONS, key={"series": _SERIES})
        assert off_stats is not None and on_stats is not None
        (off_row,) = off_stats.files
        (on_row,) = on_stats.files
        assert (on_row.rel, on_row.rows, on_row.min_ts, on_row.max_ts, on_row.size) == (
            off_row.rel,
            off_row.rows,
            off_row.min_ts,
            off_row.max_ts,
            off_row.size,
        )
        assert on_row.raw_hash is None and off_row.raw_hash is None


# ---------------------------------------------------------------------------
# Layer 3 — whole-path ON/OFF parity through heal_partition /
# merge_partition_frame / the R3 heal_findings batch twin
# ---------------------------------------------------------------------------


def _tree_state(root: Path) -> dict[str, str]:
    """Recursive relpath → sha256 (the heal-suite idiom), MINUS ``.locks/``.

    The flock sidecar names are ``sha256(<absolute dest>)`` — path-derived,
    so two twin lakes at different tmp roots legitimately carry different
    (empty) lock filenames; comparing them would fail on apparatus, not
    data. Everything else — options tree, quarantine before-images, the
    heal sentinel — is compared byte-for-byte.
    """
    out: dict[str, str] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".locks/"):
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _now() -> datetime:
    return datetime(2024, 7, 1, tzinfo=UTC)  # a FIXED stamp — twin trees align


def _seed_poisoned_options_lake(lake: Path) -> None:
    """One options lake carrying dup + foreign-month + null-side poison,
    seeded through the REAL writer then hand-poisoned (the heal-suite idiom)."""
    writer = OptionParquetWriter(lake_root=lake)
    writer.write(partition_key=_PK, coordinate=_COORD, bars=_bars_mix(12))
    dest = _dest(lake)
    df = pl.read_parquet(dest, glob=False)
    drifted = df.slice(0, 2).with_columns((pl.col("close") + 999.0).alias("close"))
    foreign = df.slice(2, 1).with_columns(
        (pl.col("timestamp") + pl.duration(days=40)).alias("timestamp")  # → April
    )
    null_side = df.slice(3, 1).with_columns(
        pl.lit(None, dtype=pl.String).alias("side"),
        (pl.col("timestamp") + pl.duration(minutes=555)).alias("timestamp"),
    )
    pl.concat([df, drifted, foreign, null_side, null_side]).write_parquet(dest)


class TestWholePathHealParity:
    def test_heal_partition_on_off_twins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _seed(lake: Path) -> Path:
            w = OptionParquetWriter(lake_root=lake)
            w.write(partition_key=_PK, coordinate=_COORD, bars=_bars_mix(20))
            dest = _dest(lake)
            df = pl.read_parquet(dest, glob=False)
            pl.concat([df, df.slice(0, 4)]).write_parquet(dest)
            return dest

        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")  # genuine oracle OFF arm
        off_lake = _mkdtemp("pr2-h-off-")
        off_dest = _seed(off_lake)
        off_original = off_dest.read_bytes()
        off_res = OptionParquetWriter(lake_root=off_lake).heal_partition(
            off_dest, quarantine_to=off_lake / ".quarantine" / "t" / "2024-03.parquet"
        )

        on_lake = _mkdtemp("pr2-h-on-")
        on_dest = _seed(on_lake)
        assert on_dest.read_bytes() == off_original  # identical poisoned inputs
        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        lrr.reset_lake_rust_render_for_tests(remaining=0)
        on_res = OptionParquetWriter(lake_root=on_lake).heal_partition(
            on_dest, quarantine_to=on_lake / ".quarantine" / "t" / "2024-03.parquet"
        )
        _assert_options_crate_rendered()

        assert on_dest.read_bytes() == off_dest.read_bytes()  # healed bytes equal
        # Quarantine before-images equal AND equal the ORIGINAL bytes (the
        # byte-verbatim law is a parity assertion too).
        off_q = off_lake / ".quarantine" / "t" / "2024-03.parquet"
        on_q = on_lake / ".quarantine" / "t" / "2024-03.parquet"
        assert off_q.read_bytes() == on_q.read_bytes() == off_original
        assert (on_res.changed, on_res.kept, on_res.dropped_duplicates, on_res.dropped_removed) == (
            off_res.changed,
            off_res.kept,
            off_res.dropped_duplicates,
            off_res.dropped_removed,
        )

    def test_merge_partition_frame_on_off_twins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The reassignment write-home entry (the copy-forward leg's render)."""

        def _drive(lake: Path, flag_on: bool) -> tuple[Path, int]:
            w = OptionParquetWriter(lake_root=lake)
            w.write(partition_key=_PK, coordinate=_COORD, bars=_bars_mix(10))
            moved = opw._render_df(_COORD, _bars_mix(4, start=8))  # 2 overlap + 2 new
            dest = _dest(lake)
            if flag_on:
                lrr.reset_lake_rust_render_for_tests(remaining=0)
            added = w.merge_partition_frame(dest, moved)
            return dest, added

        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")  # genuine oracle OFF arm
        off_lake = _mkdtemp("pr2-m-off-")
        off_dest, off_added = _drive(off_lake, flag_on=False)

        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        on_lake = _mkdtemp("pr2-m-on-")
        on_dest, on_added = _drive(on_lake, flag_on=True)
        _assert_options_crate_rendered()
        assert on_added == off_added
        assert on_dest.read_bytes() == off_dest.read_bytes()

    def test_r3_whole_heal_findings_batch_on_off_twin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R3: an end-to-end ``heal_findings`` batch over a dup + foreign +
        null-side poisoned options tree — final tree sha256-identical,
        HealReport field-identical, second sweep clean on both arms.

        W-6a RE-SCOPE: the Python heal core (the render seam's former
        heal-batch consumer) is DELETED — both arms now run the ONE
        waved-crate heal, which renders natively and never reaches the
        render facade. The pin is consciously RETARGETED: the render flag
        is INERT on the waved heal path (trees byte-equal ON vs OFF, no
        crate-render engagement), and the heal itself converges. The render
        seam's own ON/OFF parity stays pinned by the write/heal_partition/
        merge_partition_frame twins above.
        """
        from brutex.data.lake_heal import HealReport, heal_findings
        from brutex.data.lake_invariants import scan_lake_invariants

        monkeypatch.delenv("BRUTEX_LAKE_WAVED_RUST", raising=False)

        def _drive(lake: Path, flag_on: bool) -> tuple[HealReport, dict[str, str], int]:
            _seed_poisoned_options_lake(lake)
            if flag_on:
                lrr.reset_lake_rust_render_for_tests(remaining=0)
            scan = scan_lake_invariants(lake)
            assert scan.findings, "the poisoned tree must produce findings"
            report = heal_findings(
                scan.findings,
                lake_root=lake,
                files_scanned=scan.files_scanned + scan.option_files_scanned,
                mode="auto",
                recall=False,
                now=_now,
            )
            second = scan_lake_invariants(lake)
            return report, _tree_state(lake), len(second.findings)

        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "0")  # genuine oracle OFF arm
        off_report, off_tree, off_second = _drive(_mkdtemp("pr2-r3-off-"), flag_on=False)

        monkeypatch.setenv(lrr.LAKE_RUST_RENDER_ENV, "1")
        on_report, on_tree, on_second = _drive(_mkdtemp("pr2-r3-on-"), flag_on=True)
        # The W-6a inertness half: the waved-crate heal renders NATIVELY —
        # the render facade must never have engaged on this path.
        assert _OPTIONS_ENGAGED_KEY not in lrr._OBSERVABILITY_MEMO, (
            "the waved-crate heal unexpectedly routed through the render "
            "facade — the W-6a native-render law broke"
        )

        assert on_tree == off_tree  # every file byte-identical, recursively
        assert (on_report.findings, on_report.healed, on_report.skipped, on_report.errors) == (
            off_report.findings,
            off_report.healed,
            off_report.skipped,
            off_report.errors,
        )
        assert on_report.errors == 0
        assert off_second == on_second == 0  # second sweep clean both arms


# ---------------------------------------------------------------------------
# Statistics-byte diagnostic pins (a)–(e) — the bump-time triage layer
# ---------------------------------------------------------------------------


def _footer(data: bytes) -> Any:
    pq = pytest.importorskip("pyarrow.parquet")
    return pq.ParquetFile(io.BytesIO(data)).metadata


class TestStatisticsPins:
    @pytest.mark.parametrize("case_name", ["o4_heal_nullside_keyed_removal_50", "o9_multi_rg_300k"])
    def test_a_stats_present_negative_control(
        self, case_name: str, corpus: dict[str, _Case]
    ) -> None:
        """(a) a ``statistics=False`` control render of the SAME frame DIFFERS
        — the corpus can never silently go stats-insensitive (the probe's
        −1,782 B / −6,408 B teeth, promoted to a standing assert)."""
        case = corpus[case_name]
        bio = io.BytesIO()
        case.frame.write_parquet(bio, statistics=False)
        control = bio.getvalue()
        assert control != case.off_bytes
        assert len(control) < len(case.off_bytes)  # the stats bytes are real

    def test_b_footer_stats_shape(self, corpus: dict[str, _Case]) -> None:
        """(b) min/max + EXACT null_count where values exist; an all-null
        column carries null_count == rows with NO min/max; distinct_count
        ABSENT everywhere (the defaults map ``distinct_count=False``)."""
        for case_name, all_null_cols in (
            ("o4_heal_nullside_keyed_removal_50", set()),
            ("o5_all_null_nullables_50", {"open_interest", "strike", "spot", "iv"}),
            ("o6_single_side_ce_only_60", set()),
        ):
            case = corpus[case_name]
            meta = _footer(case.off_bytes)
            frame = case.frame
            columns = list(frame.columns)
            for rg_idx in range(meta.num_row_groups):
                rg = meta.row_group(rg_idx)
                for col_idx in range(rg.num_columns):
                    col = rg.column(col_idx)
                    name = columns[col_idx]
                    stats = col.statistics
                    assert stats is not None, f"{case_name}:{name} missing statistics"
                    assert not stats.has_distinct_count, f"{case_name}:{name} distinct present"
                    assert stats.has_null_count
                    # EXACT null_count against the generator-truth frame.
                    assert stats.null_count == frame.get_column(name).null_count(), (
                        f"{case_name}:{name} null_count mismatch"
                    )
                    if name in all_null_cols:
                        assert not stats.has_min_max, f"{case_name}:{name} min/max on all-null"
                    elif name == "side" and case_name == "o6_single_side_ce_only_60":
                        assert stats.has_min_max
                        assert stats.min == stats.max == "CE"  # constant Utf8

    def test_c_rg_split_pins(self, corpus: dict[str, _Case]) -> None:
        """(c) the streaming sink's production-default split, as footer data."""
        for case_name in ("o7_rg_boundary_122880", "o8_rg_boundary_122881", "o9_multi_rg_300k"):
            case = corpus[case_name]
            meta = _footer(case.off_bytes)
            rgs = [meta.row_group(i).num_rows for i in range(meta.num_row_groups)]
            assert rgs == case.expected_rgs, f"{case_name}: RG split {rgs}"

    def test_d_long_string_truncation_sentinel(self, corpus: dict[str, _Case]) -> None:
        """(d) the binary-stats truncation SENTINEL — pins the OBSERVED
        pinned-rev policy as data.

        EMPIRICAL CORRECTION of the probe-report reading (verified
        2026-07-11 on the installed 1.42.1 wheel): the write option
        ``binary_statistics_truncate_length = None`` does NOT mean
        untruncated — polars-parquet resolves it to an INTERNAL default,
        observed as truncate-at-64-bytes with the max's last byte
        INCREMENTED (a valid exclusive-style upper bound; ≤64-byte strings
        ride verbatim). The parity gate is unaffected (wheel and crate share
        the policy — the o11 BYTES matched), and the sentinel keeps its
        purpose: the day a bump moves the truncation default, the corpus
        byte-FAILS and THIS pin names why in the same run."""
        case = corpus["o11_long_string_stats_sentinel_40"]
        meta = _footer(case.off_bytes)
        col_idx = case.frame.columns.index("underlying_symbol")
        stats = meta.row_group(0).column(col_idx).statistics
        assert stats is not None and stats.has_min_max
        assert len(_LONG_SYMBOL) >= 256
        assert stats.min == _LONG_SYMBOL[:64]  # truncated 64-byte prefix, verbatim
        # max = the truncated prefix with its LAST byte incremented (still a
        # correct upper bound for the column).
        assert stats.max == _LONG_SYMBOL[:63] + chr(ord(_LONG_SYMBOL[63]) + 1)
        assert len(stats.min) == len(stats.max) == 64

    def test_e_nan_carrying_column_omits_min_max(self, corpus: dict[str, _Case]) -> None:
        """(e) Float64 non-finite stats policy — pins the OBSERVED pinned-rev
        behavior as data: a NaN-CARRYING column omits min/max ENTIRELY
        (stronger than per-value NaN exclusion; a NaN can never appear in a
        stats page), while null_count stays exact. (An inf-only column keeps
        min/max with inf as max — probed 2026-07-11 — but o12's NaN removes
        the whole pair; the byte-parity layer still covers the inf/NaN VALUE
        encoding.)"""
        case = corpus["o12_nan_inf_iv_heal_40"]
        iv = case.frame.get_column("iv")
        values = [v for v in iv.to_list() if v is not None]
        assert any(math.isnan(v) for v in values)  # NaN present
        assert any(math.isinf(v) for v in values)  # +inf present
        meta = _footer(case.off_bytes)
        col_idx = case.frame.columns.index("iv")
        stats = meta.row_group(0).column(col_idx).statistics
        assert stats is not None
        assert not stats.has_min_max  # the NaN drops the WHOLE min/max pair
        assert stats.has_null_count
        assert stats.null_count == iv.null_count()
