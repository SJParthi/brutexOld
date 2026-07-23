"""ON-vs-OFF parity corpus for the T11 resample tails (real wheel).

DEC-LAKE-RUST-RESAMPLE-DAILY-001 (MIGRATION.md #1946): drives the REAL
public entry points both arms — flag OFF (the verbatim Python bodies) vs
flag ON (the ``brutex_lake`` crate through the facades, post-burn-in steady
state with the ORACLES BOMBED so a silent degrade fails loudly) — over:

* the DAILY corpus at ``CandleBar`` strength INCLUDING ``Decimal`` reprs
  (fno carry-last OI + null-last edge · spot OHLCV-6 · forming-session
  drop boundary · multi-session multi-day · month edge · Muhurat evening
  session · volume-zero · single bar);
* the SESSION-cutoff face across TFs × cutoffs (boundary-exact);
* the BATCH face across ALL 21 TFs (dtype-exact) + the ONE-crossing law;
* the whole-prebuild sha256 A/B (``materialize_lake_timeframes`` over a
  REAL ``ParquetBarSink`` lake) with the daily flag ON — and with BOTH
  resample flags ON — derived partition bytes equal to the all-OFF lake.

Requires the built wheel; skips (with the named remedy) when the crate is
absent, pre-T11 (no daily face), or version-guard-mismatched.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import contextlib
import hashlib
import os
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

brutex_lake = pytest.importorskip(
    "brutex_lake",
    reason=(
        "brutex-lake crate not installed — the T11 parity corpus runs in the "
        "lake-rust-parity.yml lane (or locally after `pip install rust/brutex-lake`)"
    ),
)
pl = pytest.importorskip("polars")

from brutex.data import (  # noqa: E402
    fno_daily_resample,
    fno_resample,
    spot_daily_resample,
    tf_resampler,
)
from brutex.data import lake_rust_resample as lrs  # noqa: E402
from brutex.data import lake_rust_resample_daily as lrd  # noqa: E402
from brutex.data.timeframes import ALL_TF_MINUTES  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from brutex.data.models import CandleBar

for _face in ("resample_daily_ipc", "resample_session_ipc"):
    if not hasattr(brutex_lake, _face):
        pytest.skip(
            f"installed brutex_lake wheel predates the T11 {_face} face — "
            "rebuild the wheel from rust/brutex-lake (pip install rust/brutex-lake)",
            allow_module_level=True,
        )

if brutex_lake.pinned_polars_version() != pl.__version__:
    pytest.skip(
        "version-guard mismatch: installed polars "
        f"{pl.__version__} != crate pin {brutex_lake.pinned_polars_version()} — "
        "the facades would refuse to engage (fail-closed by design); re-run after "
        "the lockstep ritual (docs/runbooks/lake-rust-render-lockstep.md)",
        allow_module_level=True,
    )


@pytest.fixture(autouse=True)
def _fresh_seams() -> Iterator[None]:
    lrs.reset_lake_rust_resample_for_tests()
    lrd.reset_lake_rust_resample_daily_for_tests()
    yield
    lrs.reset_lake_rust_resample_for_tests()
    lrd.reset_lake_rust_resample_daily_for_tests()


# ---------------------------------------------------------------------------
# corpus builders (deterministic; UTC-tz-aware)
# ---------------------------------------------------------------------------

_OPEN_UTC = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)  # Tue 2024-01-02 09:15 IST


def _frame(
    instants: list[datetime],
    *,
    volumes: list[int] | None = None,
    oi: list[int | None] | None = None,
) -> Any:
    n = len(instants)
    data: dict[str, Any] = {
        "ts_utc": instants,
        # Deliberately AWKWARD binary floats (100.1, +0.07 steps) so the
        # Decimal(str(x)) repr law is genuinely exercised, plus 2dp lake
        # shapes — the crate's selections must be bit-copies.
        "open": [100.1 + 0.07 * i for i in range(n)],
        "high": [110.55 + 0.07 * i for i in range(n)],
        "low": [90.05 + 0.07 * i for i in range(n)],
        "close": [105.35 + 0.07 * i for i in range(n)],
        "volume": volumes if volumes is not None else [10 + i for i in range(n)],
    }
    if oi is not None:
        data["open_interest"] = oi
    return pl.DataFrame(data).with_columns(pl.col("ts_utc").cast(pl.Datetime("us", "UTC")))


def _minutes(start: datetime, count: int) -> list[datetime]:
    return [start + timedelta(minutes=i) for i in range(count)]


def _daily_corpus() -> dict[str, tuple[Any, date]]:
    """(frame, before) per daily case — shared by the spot + fno arms."""
    two_sessions = _minutes(_OPEN_UTC, 40) + _minutes(_OPEN_UTC + timedelta(days=1), 40)
    multi = _minutes(_OPEN_UTC, 30) + _minutes(_OPEN_UTC + timedelta(days=1), 30)
    multi += _minutes(_OPEN_UTC + timedelta(days=2), 30)
    month_edge = _minutes(datetime(2024, 5, 31, 18, 45, tzinfo=UTC), 20)  # 00:15 IST Jun 1
    muhurat = _minutes(datetime(2025, 10, 21, 12, 45, tzinfo=UTC), 60)  # 18:15 IST evening
    return {
        "a_two_sessions": (_frame(two_sessions), date(2024, 1, 4)),
        "b_forming_dropped": (_frame(two_sessions), date(2024, 1, 3)),  # 2nd forming
        "c_all_forming": (_frame(two_sessions), date(2024, 1, 2)),  # nothing survives
        "d_multi_session": (_frame(multi), date(2024, 1, 5)),
        "e_month_edge": (_frame(month_edge), date(2024, 6, 2)),
        "f_muhurat_evening": (_frame(muhurat), date(2025, 10, 22)),
        "g_volume_zero": (_frame(_minutes(_OPEN_UTC, 20), volumes=[0] * 20), date(2024, 1, 3)),
        "h_single_bar": (_frame([_OPEN_UTC + timedelta(minutes=33)]), date(2024, 1, 3)),
    }


def _with_oi(df: Any) -> Any:
    n = df.height
    oi: list[int | None] = [None if i % 7 == 0 else 1_000 + i for i in range(n)]
    # Null the LAST row's OI in one shape via the % law when it lands there;
    # additionally force a null last-of-session edge on the first session by
    # construction in the dedicated assertion below.
    return df.with_columns(pl.Series("open_interest", oi, dtype=pl.Int64))


def _bomb(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError(
        "the Python oracle was invoked on the engaged steady-state path — "
        "the crate degraded/refused instead of serving (parity run is vacuous)"
    )


def _assert_bars_equal(got: list[CandleBar], expected: list[CandleBar], label: str) -> None:
    assert len(got) == len(expected), f"{label}: bar count drift"
    for i, (g, e) in enumerate(zip(got, expected, strict=True)):
        assert g == e, f"{label}[{i}]: value drift"
        assert g.timestamp == e.timestamp, f"{label}[{i}]: stamp drift"
        assert str(g.open) == str(e.open), f"{label}[{i}]: open repr drift"
        assert str(g.high) == str(e.high), f"{label}[{i}]: high repr drift"
        assert str(g.low) == str(e.low), f"{label}[{i}]: low repr drift"
        assert str(g.close) == str(e.close), f"{label}[{i}]: close repr drift"
        assert g.volume == e.volume, f"{label}[{i}]: volume drift"
        assert g.open_interest == e.open_interest, f"{label}[{i}]: OI drift"


# ---------------------------------------------------------------------------
# daily parity — CandleBar strength incl. Decimal reprs
# ---------------------------------------------------------------------------


class TestDailyParity:
    @pytest.mark.parametrize("case", sorted(_daily_corpus()))
    def test_spot_daily_on_equals_off(self, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
        df, before = _daily_corpus()[case]
        monkeypatch.delenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, raising=False)
        expected = spot_daily_resample.resample_1m_to_daily(df, before=before)

        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")
        lrd.reset_lake_rust_resample_daily_for_tests(remaining=0)
        monkeypatch.setattr(spot_daily_resample, "_resample_daily_frame_python", _bomb)
        got = spot_daily_resample.resample_1m_to_daily(df, before=before)
        assert lrd.rust_resample_daily_disabled() is False
        _assert_bars_equal(got, expected, f"spot:{case}")

    @pytest.mark.parametrize("case", sorted(_daily_corpus()))
    def test_fno_daily_on_equals_off_with_carry_last_oi(
        self, monkeypatch: pytest.MonkeyPatch, case: str
    ) -> None:
        base, before = _daily_corpus()[case]
        df = _with_oi(base)
        monkeypatch.delenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, raising=False)
        expected = fno_daily_resample.resample_1m_to_fno_daily(df, before=before)

        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")
        lrd.reset_lake_rust_resample_daily_for_tests(remaining=0)
        monkeypatch.setattr(fno_daily_resample, "_fno_daily_frame_python", _bomb)
        got = fno_daily_resample.resample_1m_to_fno_daily(df, before=before)
        assert lrd.rust_resample_daily_disabled() is False
        _assert_bars_equal(got, expected, f"fno:{case}")

    def test_fno_null_last_of_session_oi_stays_none_on_both_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        instants = _minutes(_OPEN_UTC, 3)
        df = _frame(instants, oi=[1_000, 1_001, None])  # the LAST bar's OI is null
        before = date(2024, 1, 3)
        monkeypatch.delenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, raising=False)
        expected = fno_daily_resample.resample_1m_to_fno_daily(df, before=before)
        assert expected[0].open_interest is None, "the honest-edge law (fixture sanity)"

        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")
        lrd.reset_lake_rust_resample_daily_for_tests(remaining=0)
        monkeypatch.setattr(fno_daily_resample, "_fno_daily_frame_python", _bomb)
        got = fno_daily_resample.resample_1m_to_fno_daily(df, before=before)
        _assert_bars_equal(got, expected, "fno:null_last_oi")

    def test_daily_burn_in_comparator_passes_on_the_real_corpus(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With the burn-in ARMED (fresh process view), the daily derives
        # survive the dtype-exact self-check — no autorevert, budget
        # genuinely consumed.
        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")
        lrd.reset_lake_rust_resample_daily_for_tests()
        before_budget = lrd._BURN_IN_REMAINING[0]
        df, before = _daily_corpus()["a_two_sessions"]
        spot_daily_resample.resample_1m_to_daily(df, before=before)
        fno_daily_resample.resample_1m_to_fno_daily(_with_oi(df), before=before)
        assert lrd.rust_resample_daily_disabled() is False
        assert lrd._BURN_IN_REMAINING[0] == before_budget - 2


# ---------------------------------------------------------------------------
# session-cutoff parity — boundary-exact across TFs × cutoffs
# ---------------------------------------------------------------------------


class TestSessionCutoffParity:
    @pytest.mark.parametrize("tf", [2, 5, 15, 60])
    def test_on_equals_off_across_cutoffs(self, monkeypatch: pytest.MonkeyPatch, tf: int) -> None:
        instants = _minutes(_OPEN_UTC, 90)
        lake_df = _frame(instants, oi=[2_000 + i for i in range(90)]).rename(
            {"ts_utc": "timestamp"}
        )
        # Cutoffs: before every close · the first bucket's exact close ·
        # mid-session · aligned + misaligned · whole-day.
        cutoffs = [0, 555 + tf, 555 + tf + 1, 600, 601, 929]
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)
        expected = {
            cutoff: fno_resample.resample_session_to_tf(lake_df, tf, cutoff_min=cutoff)
            for cutoff in cutoffs
        }

        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        monkeypatch.setattr(fno_resample, "_resample_1m_to_tf_python", _bomb)
        for cutoff in cutoffs:
            got = fno_resample.resample_session_to_tf(lake_df, tf, cutoff_min=cutoff)
            assert got.schema == expected[cutoff].schema, f"tf={tf} cutoff={cutoff}"
            assert got.equals(expected[cutoff]), f"tf={tf} cutoff={cutoff}"
        assert lrs.rust_resample_disabled() is False


# ---------------------------------------------------------------------------
# batch-face parity — all 21 TFs, one crossing
# ---------------------------------------------------------------------------


class TestBatchFaceParity:
    def test_resample_all_21_tfs_on_equals_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        df = _with_oi(_frame(_minutes(_OPEN_UTC, 375)))
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)
        expected = tf_resampler.resample_all_21_tfs(df)

        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        monkeypatch.setattr(tf_resampler, "_resample_1m_to_tf_python", _bomb)
        got = tf_resampler.resample_all_21_tfs(df)
        assert lrs.rust_resample_disabled() is False
        assert list(got.keys()) == list(expected.keys()) == list(ALL_TF_MINUTES)
        for tf in ALL_TF_MINUTES:
            assert got[tf].schema == expected[tf].schema, f"tf={tf}: schema drift"
            assert got[tf].equals(expected[tf]), f"tf={tf}: value/row-order drift"

    def test_batch_face_pays_one_crossing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        df = _frame(_minutes(_OPEN_UTC, 30))
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        batch_calls: list[int] = []
        real_batch = brutex_lake.resample_all_ipc

        def _counting_batch(ipc: bytes, tfs: list[int], oi: bool) -> list[bytes]:
            batch_calls.append(len(tfs))
            return real_batch(ipc, tfs, oi)

        single_calls: list[int] = []
        real_single = brutex_lake.resample_1m_ipc

        def _counting_single(ipc: bytes, tf: int, oi: bool) -> bytes:
            single_calls.append(tf)
            return real_single(ipc, tf, oi)

        monkeypatch.setattr(brutex_lake, "resample_all_ipc", _counting_batch)
        monkeypatch.setattr(brutex_lake, "resample_1m_ipc", _counting_single)
        tf_resampler.resample_all_21_tfs(df)
        assert batch_calls == [len(ALL_TF_MINUTES)], "ONE crossing per 21-TF derive"
        assert single_calls == [], "no per-TF fan-out"


# ---------------------------------------------------------------------------
# whole-prebuild A/B — derived partition bytes equal, daily arm included
# ---------------------------------------------------------------------------

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NSE-NIFTY"
# The F&O daily writer path (round-1 fold FIX 3): a FUT contract rides the
# OI-aware resamplers + the carry-last-OI F&O daily derive — the SECOND
# daily writer the sha256 A/B must cover.
_FNO_SEGMENT = "FNO"
_FNO_SYMBOL = "NSE-NIFTY-25Jan24-FUT"


def _seed_lake(lake_root: Path) -> None:
    from brutex.data.models import CandleBar
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    bars = [
        CandleBar(
            timestamp=_OPEN_UTC + timedelta(minutes=i),
            open=Decimal(str(100.0 + i)),
            high=Decimal(str(110.0 + i)),
            low=Decimal(str(90.0 + i)),
            close=Decimal(str(105.0 + i)),
            volume=10 + i,
            open_interest=0,
        )
        for i in range(375)
    ]
    fno_bars = [
        CandleBar(
            timestamp=_OPEN_UTC + timedelta(minutes=i),
            open=Decimal(str(200.05 + i)),
            high=Decimal(str(210.6 + i)),
            low=Decimal(str(190.15 + i)),
            close=Decimal(str(205.4 + i)),
            volume=20 + i,
            open_interest=5_000 + i,
        )
        for i in range(375)
    ]
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval="1minute",
        bars=bars,
    )
    sink.append(
        exchange=_EXCHANGE,
        segment=_FNO_SEGMENT,
        symbol=_FNO_SYMBOL,
        interval="1minute",
        bars=fno_bars,
    )


def _materialize(lake_root: Path) -> None:
    from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes

    materialize_lake_timeframes(
        lake_root=lake_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
        # The daily arm rides the SAME prebuild entry (include_daily=True —
        # the pipeline's production shape), with an explicit complete-session
        # cutoff so the fixture session's 1day bar is derived on both lakes.
        include_daily=True,
        daily_before=date(2024, 1, 3),
    )
    materialize_lake_timeframes(
        lake_root=lake_root,
        exchange=_EXCHANGE,
        segment=_FNO_SEGMENT,
        symbol=_FNO_SYMBOL,
        # FUTURES = the OI-aware derive + the carry-last-OI F&O daily writer
        # (`resample_1m_to_fno_daily_positional` via `daily_resample_fn_for`)
        # — CE/PE stay prebuild-retired (DEC-OPTIONS-PREBUILD-RETIRE-001),
        # so FUT is the F&O daily production shape.
        kind=PrebuildKind.FUTURES,
        include_daily=True,
        daily_before=date(2024, 1, 3),
    )


def _derived_hashes(lake_root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(lake_root.rglob("*.parquet")):
        rel = path.relative_to(lake_root).as_posix()
        if "/1minute/" in f"/{rel}":
            continue  # the seeded base — identical by construction
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class TestWholePrebuildByteParityDailyArm:
    def test_materialized_bytes_equal_daily_on_vs_all_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake_off = tmp_path / "lake-off"
        lake_daily_on = tmp_path / "lake-daily-on"

        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)
        monkeypatch.delenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, raising=False)
        _seed_lake(lake_off)
        _materialize(lake_off)
        off_hashes = _derived_hashes(lake_off)

        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")
        lrd.reset_lake_rust_resample_daily_for_tests(remaining=0)
        _seed_lake(lake_daily_on)
        _materialize(lake_daily_on)
        daily_on_hashes = _derived_hashes(lake_daily_on)

        assert lrd.rust_resample_daily_disabled() is False, (
            f"the daily Rust path latched OFF mid-prebuild ({lrd._DISABLED_REASON[0]}) — "
            "the A/B would compare Python against Python (vacuous)"
        )
        # Non-vacuity BOTH daily writer paths (round-1 fold FIX 3): the spot
        # midnight-IST daily AND the carry-last-OI F&O daily each materialized.
        for seg in (_SEGMENT, _FNO_SEGMENT):
            assert any(f"/{seg}/" in f"/{rel}" and "/1day/" in f"/{rel}" for rel in off_hashes), (
                f"no derived {seg} 1day partition was written — the daily A/B is vacuous"
            )
        assert daily_on_hashes == off_hashes

    def test_materialized_bytes_equal_both_flags_on_vs_all_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake_off = tmp_path / "lake-off"
        lake_both_on = tmp_path / "lake-both-on"

        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)
        monkeypatch.delenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, raising=False)
        _seed_lake(lake_off)
        _materialize(lake_off)
        off_hashes = _derived_hashes(lake_off)

        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        lrd.reset_lake_rust_resample_daily_for_tests(remaining=0)
        _seed_lake(lake_both_on)
        _materialize(lake_both_on)
        both_on_hashes = _derived_hashes(lake_both_on)

        assert lrs.rust_resample_disabled() is False
        assert lrd.rust_resample_daily_disabled() is False
        assert off_hashes, "no derived partitions were written — the A/B is vacuous"
        for seg in (_SEGMENT, _FNO_SEGMENT):
            assert any(f"/{seg}/" in f"/{rel}" and "/1day/" in f"/{rel}" for rel in off_hashes), (
                f"no derived {seg} 1day partition was written — the daily A/B is vacuous"
            )
        assert both_on_hashes == off_hashes


# ---------------------------------------------------------------------------
# naive-frame local-TZ law — the #1946 disclosure, TZ-controlled pins
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _forced_local_tz(tz: str) -> Generator[None]:
    """Force the process-local timezone (TZ env + ``tzset``), restoring it always."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset unavailable on this platform — the TZ pins are POSIX-only")
    prior = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prior
        time.tzset()


def _require_local_utc_offset(allowed: set[timedelta]) -> None:
    """Skip when the requested TZ did not resolve (no IANA zone files on the box)."""
    actual = datetime.now(UTC).astimezone().utcoffset()
    if actual not in allowed:
        pytest.skip(
            f"system tzdata did not resolve the requested TZ (local offset {actual}) — "
            "the TZ pin needs IANA zone files"
        )


def _naive(df: Any) -> Any:
    """Strip the tz — same wall values, naive ``Datetime(us)`` dtype."""
    return df.with_columns(pl.col("ts_utc").dt.replace_time_zone(None))


def _bars_from_stamped_frame(stamped: Any) -> list[CandleBar]:
    """Materialize a daily FRAME with the ON arm's exact per-row law."""
    from brutex.data.models import CandleBar

    has_oi = "open_interest" in stamped.columns
    bars: list[CandleBar] = []
    for row in stamped.iter_rows(named=True):
        raw_oi = row["open_interest"] if has_oi else None
        bars.append(
            CandleBar(
                timestamp=row["ts_utc"],
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=int(row["volume"]),
                open_interest=None if raw_oi is None else int(raw_oi),
            )
        )
    return bars


class TestNaiveFrameLocalTzLaw:
    """The round-1 fold's FIX 2 — the #1946 naive-ts disclosure, pinned.

    PRODUCTION frames are tz-aware UTC (every lake read boundary), where the
    per-row OFF body, the vectorized oracle ``_fno_daily_frame_python`` and
    the crate ON arm are provably equal (the whole parity corpus above). On a
    NAIVE-timestamp frame the per-row OFF body is local-TZ-dependent
    (``astimezone`` reads a naive datetime as SYSTEM-LOCAL wall time) while
    the oracle + the crate read naive-as-physical-UTC (TZ-independent) — the
    disclosed PRE-EXISTING local-TZ dependence. These pins control the TZ
    explicitly (TZ env + ``tzset``) so both the agreement envelope AND the
    divergence are locked — a future silent change trips a test.

    MEASURED correction of the review note's premise: the daily label is
    pinned at the 09:15-IST day anchor (03:45 naive wall — one bucket per
    IST day), so under TZ=America/New_York the per-row reading is
    03:45−(−05:00)+05:30 = 14:15 IST, the SAME calendar date — western TZs
    CANNOT flip the derived IST date. Divergence requires a local offset
    STRICTLY EAST of +09:15 (03:45 wall read as local lands the IST instant
    on the PREVIOUS date); the pin uses TZ=Australia/Brisbane (+10:00,
    DST-free — deterministic year-round).
    """

    @pytest.mark.parametrize("tz", ["UTC", "Asia/Kolkata", "America/New_York"])
    def test_naive_fno_frame_all_three_arms_agree_without_date_skew(
        self, monkeypatch: pytest.MonkeyPatch, tz: str
    ) -> None:
        offsets = {
            "UTC": {timedelta(0)},
            "Asia/Kolkata": {timedelta(hours=5, minutes=30)},
            # EST/EDT both land the 03:45 label on the same IST date.
            "America/New_York": {timedelta(hours=-5), timedelta(hours=-4)},
        }[tz]
        df = _naive(
            _with_oi(_frame(_minutes(_OPEN_UTC, 40) + _minutes(_OPEN_UTC + timedelta(days=1), 40)))
        )
        before = date(2024, 1, 4)
        with _forced_local_tz(tz):
            _require_local_utc_offset(offsets)
            monkeypatch.delenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, raising=False)
            off_bars = fno_daily_resample.resample_1m_to_fno_daily(df, before=before)
            oracle_bars = _bars_from_stamped_frame(
                fno_daily_resample._fno_daily_frame_python(df, before)
            )
            monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")
            lrd.reset_lake_rust_resample_daily_for_tests(remaining=0)
            monkeypatch.setattr(fno_daily_resample, "_fno_daily_frame_python", _bomb)
            on_bars = fno_daily_resample.resample_1m_to_fno_daily(df, before=before)
            assert lrd.rust_resample_daily_disabled() is False
        assert len(off_bars) == 2, "fixture sanity: two complete sessions survive"
        _assert_bars_equal(off_bars, oracle_bars, f"naive:{tz}:off-vs-oracle")
        _assert_bars_equal(on_bars, oracle_bars, f"naive:{tz}:on-vs-oracle")
        _assert_bars_equal(off_bars, on_bars, f"naive:{tz}:off-vs-on")

    def test_naive_fno_frame_per_row_off_body_diverges_east_of_ist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE DOCUMENTED-DIVERGENCE PIN (the #1946 disclosure asserted as
        # CURRENT behavior — the disclosed pre-existing local-TZ dependence
        # of the per-row OFF body; a future silent change trips this test).
        # Under TZ=Australia/Brisbane (+10:00): the Jan-2 session's label
        # (naive wall 2024-01-02 03:45) reads as 2024-01-01 17:45 UTC →
        # 23:15 IST 2024-01-01 — the per-row arm derives IST date Jan 1,
        # ONE DAY EARLIER than the naive-as-UTC oracle/crate (Jan 2).
        df = _naive(_with_oi(_frame(_minutes(_OPEN_UTC, 40))))
        with _forced_local_tz("Australia/Brisbane"):
            _require_local_utc_offset({timedelta(hours=10)})
            # The per-row OFF body via the explicit "0" kill switch (the
            # default flipped ON — DEC-RUST-DEFAULT-ON-WAVE-001).
            monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "0")
            # Shape 1 (stamp shift): both arms keep the session, stamped a
            # day apart — per-row midnight-IST Jan 1 vs vectorized Jan 2.
            off_kept = fno_daily_resample.resample_1m_to_fno_daily(df, before=date(2024, 1, 3))
            oracle_kept = _bars_from_stamped_frame(
                fno_daily_resample._fno_daily_frame_python(df, date(2024, 1, 3))
            )
            # Shape 2 (forming-session keep — the sharper divergence): with
            # before = the session's own IST date the vectorized law DROPS
            # the still-forming session while the per-row arm (reading the
            # label a day earlier) KEEPS it.
            off_forming = fno_daily_resample.resample_1m_to_fno_daily(df, before=date(2024, 1, 2))
            oracle_forming = fno_daily_resample._fno_daily_frame_python(df, date(2024, 1, 2))
            monkeypatch.setenv(lrd.LAKE_RUST_RESAMPLE_DAILY_ENV, "1")
            lrd.reset_lake_rust_resample_daily_for_tests(remaining=0)
            monkeypatch.setattr(fno_daily_resample, "_fno_daily_frame_python", _bomb)
            on_kept = fno_daily_resample.resample_1m_to_fno_daily(df, before=date(2024, 1, 3))
            assert lrd.rust_resample_daily_disabled() is False
        assert len(off_kept) == len(oracle_kept) == 1
        # midnight IST Jan 1 == 2023-12-31 18:30 UTC (per-row, local-TZ read);
        # midnight IST Jan 2 == 2024-01-01 18:30 UTC (naive-as-UTC law).
        assert off_kept[0].timestamp == datetime(2023, 12, 31, 18, 30, tzinfo=UTC)
        assert oracle_kept[0].timestamp == datetime(2024, 1, 1, 18, 30, tzinfo=UTC)
        assert off_kept[0].timestamp != oracle_kept[0].timestamp
        # Everything BUT the stamp matches — same aggregation either way.
        assert str(off_kept[0].open) == str(oracle_kept[0].open)
        assert off_kept[0].volume == oracle_kept[0].volume
        assert off_kept[0].open_interest == oracle_kept[0].open_interest
        # The forming-session shape: per-row keeps, vectorized drops.
        assert len(off_forming) == 1
        assert oracle_forming.height == 0
        # The crate ON arm stays naive-as-UTC — TZ-INDEPENDENT, equal to the
        # oracle (NOT to the per-row OFF arm on this out-of-convention frame).
        _assert_bars_equal(on_kept, oracle_kept, "naive:Brisbane:on-vs-oracle")
