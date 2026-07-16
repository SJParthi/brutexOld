"""ON-vs-OFF parity corpus for the Rust resample seam (ADR-0020 slice G5).

Drives the REAL ``resample_1m_to_tf`` both arms — flag OFF (the verbatim
Python body) vs flag ON (the ``brutex_lake`` crate through the facade, in
the post-burn-in steady state with the ORACLE BOMBED so any silent degrade
fails loudly) — over the 9-case corpus of DEC-LAKE-RUST-RESAMPLE-001
(ledger #1913): regular day · Muhurat evening session · month edge ·
holey day · volume-zero · OI/Greeks overlay carry-last · 09:15-open
collapse · tf=1 identity · single bar. Equality is DTYPE-EXACT (schema
equality AND ``DataFrame.equals`` — the #1795b comparator), across ALL 21
timeframes per case.

Plus the whole-prebuild A/B: ``materialize_lake_timeframes`` over a REAL
``ParquetBarSink`` lake, flag ON vs OFF → derived partition bytes
sha256-EQUAL (the writer is downstream of the resample, so VALUE parity ⇒
byte parity — the load-bearing claim, proven not assumed).

Requires the built wheel; skips (with the named remedy) when the crate is
absent, pre-Wave-B (no resample face), or version-guard-mismatched — the
lake-rust-parity.yml lane force-aligns the wheel so none of those skips can
fire there.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

brutex_lake = pytest.importorskip(
    "brutex_lake",
    reason=(
        "brutex-lake crate not installed — the resample parity corpus runs in "
        "the lake-rust-parity.yml lane (or locally after `pip install rust/brutex-lake`)"
    ),
)
pl = pytest.importorskip("polars")

from brutex.data import lake_rust_resample as lrs  # noqa: E402
from brutex.data import tf_resampler  # noqa: E402
from brutex.data.timeframes import ALL_TF_MINUTES  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator

if not hasattr(brutex_lake, "resample_1m_ipc"):
    pytest.skip(
        "installed brutex_lake wheel predates the Wave-B resample face — "
        "rebuild the wheel from rust/brutex-lake (pip install rust/brutex-lake)",
        allow_module_level=True,
    )

if brutex_lake.pinned_polars_version() != pl.__version__:
    pytest.skip(
        "version-guard mismatch: installed polars "
        f"{pl.__version__} != crate pin {brutex_lake.pinned_polars_version()} — "
        "the facade would refuse to engage (fail-closed by design); re-run after "
        "the lockstep ritual (docs/runbooks/lake-rust-render-lockstep.md)",
        allow_module_level=True,
    )


@pytest.fixture(autouse=True)
def _fresh_seam() -> Iterator[None]:
    lrs.reset_lake_rust_resample_for_tests()
    yield
    lrs.reset_lake_rust_resample_for_tests()


# ---------------------------------------------------------------------------
# corpus builders (deterministic; UTC-tz-aware unless noted)
# ---------------------------------------------------------------------------

_OPEN_UTC = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)  # Tue 2024-01-02 09:15 IST

_GREEKS_OVERLAY_DTYPES: dict[str, Any] = {
    "iv": pl.Float64,
    "delta": pl.Float64,
    "gamma": pl.Float64,
    "theta": pl.Float64,
    "vega": pl.Float64,
    "rho": pl.Float64,
    "spot_at_bar": pl.Float64,
    "t_years_used": pl.Float64,
    "rate_used": pl.Float64,
    "greeks_provenance_id": pl.Int32,
}


def _frame(
    instants: list[datetime],
    *,
    volumes: list[int] | None = None,
    oi: list[int | None] | None = None,
    overlay: bool = False,
    naive: bool = False,
) -> pl.DataFrame:
    n = len(instants)
    dtype = pl.Datetime("us") if naive else pl.Datetime("us", "UTC")
    ts = [i.replace(tzinfo=None) if naive else i for i in instants]
    data: dict[str, Any] = {
        "ts_utc": ts,
        "open": [100.0 + i for i in range(n)],
        "high": [110.0 + i for i in range(n)],
        "low": [90.0 + i for i in range(n)],
        "close": [105.0 + i for i in range(n)],
        "volume": volumes if volumes is not None else [10 + i for i in range(n)],
    }
    if oi is not None:
        data["open_interest"] = oi
    if overlay:
        for j, (name, _dt) in enumerate(_GREEKS_OVERLAY_DTYPES.items()):
            if name == "greeks_provenance_id":
                data[name] = [(i % 3) + 1 for i in range(n)]
            else:
                # An honest stamped-NA minute stays null (never backfilled).
                data[name] = [None if (i + j) % 5 == 0 else 0.1 * (i + j + 1) for i in range(n)]
    df = pl.DataFrame(data).with_columns(pl.col("ts_utc").cast(dtype))
    if overlay:
        df = df.with_columns(
            *[pl.col(name).cast(dtype_) for name, dtype_ in _GREEKS_OVERLAY_DTYPES.items()]
        )
    return df


def _minutes(start: datetime, count: int) -> list[datetime]:
    return [start + timedelta(minutes=i) for i in range(count)]


def _corpus() -> dict[str, pl.DataFrame]:
    """The 9 committed corpus cases (a-i)."""
    regular = _frame(_minutes(_OPEN_UTC, 375))  # (a) full regular session
    muhurat = _frame(  # (b) 2025-10-21 evening Muhurat shape (18:15 IST on)
        _minutes(datetime(2025, 10, 21, 12, 45, tzinfo=UTC), 60)
    )
    month_edge = _frame(  # (c) 00:15 IST on the 1st -> prior-UTC-month labels
        _minutes(datetime(2024, 5, 31, 18, 45, tzinfo=UTC), 30)
    )
    holey_instants = [
        _OPEN_UTC + timedelta(minutes=m)
        for m in [0, 1, 2, 17, 18, 90, 91, 92, 200, 374]  # interior gaps
    ]
    holey = _frame(holey_instants)  # (d) buckets absent, never forward-filled
    volume_zero = _frame(_minutes(_OPEN_UTC, 20), volumes=[0] * 20)  # (e) sum=0
    overlay = _frame(  # (f) OI carry-last + Greeks overlay .last()
        _minutes(_OPEN_UTC, 40),
        oi=[None if i % 7 == 0 else 1_000 + i for i in range(40)],
        overlay=True,
    )
    open_collapse = _frame(_minutes(_OPEN_UTC, 2), naive=True)  # (g) §1c law, naive dtype arm
    single = _frame([_OPEN_UTC + timedelta(minutes=33)])  # (i) single bar
    return {
        "a_regular_day": regular,
        "b_muhurat_evening": muhurat,
        "c_month_edge": month_edge,
        "d_holey_day": holey,
        "e_volume_zero": volume_zero,
        "f_oi_greeks_overlay": overlay,
        "g_open_collapse": open_collapse,
        # (h) tf=1 identity is asserted per-case below (1 ∈ ALL_TF_MINUTES).
        "i_single_bar": single,
    }


def _bomb_oracle(*_args: object, **_kwargs: object) -> pl.DataFrame:
    raise AssertionError(
        "the Python oracle was invoked on the engaged steady-state path — "
        "the crate degraded/refused instead of serving (parity run is vacuous)"
    )


# ---------------------------------------------------------------------------
# corpus parity — every case x all 21 TFs, dtype-exact
# ---------------------------------------------------------------------------


class TestResampleCorpusParity:
    @pytest.mark.parametrize("case", sorted(_corpus()))
    def test_on_equals_off_across_all_21_tfs(
        self, monkeypatch: pytest.MonkeyPatch, case: str
    ) -> None:
        df_1m = _corpus()[case]
        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)
        expected = {tf: tf_resampler.resample_1m_to_tf(df_1m, tf) for tf in ALL_TF_MINUTES}

        # ON arm: steady state (no burn-in cushion) + the oracle BOMBED so a
        # silent degrade cannot fake parity by serving Python frames.
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        monkeypatch.setattr(tf_resampler, "_resample_1m_to_tf_python", _bomb_oracle)
        for tf in ALL_TF_MINUTES:
            got = tf_resampler.resample_1m_to_tf(df_1m, tf)
            assert got.schema == expected[tf].schema, f"{case} tf={tf}: schema drift"
            assert got.equals(expected[tf]), f"{case} tf={tf}: value/row-order drift"
        assert lrs.rust_resample_disabled() is False

    def test_burn_in_comparator_passes_on_the_real_corpus(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With the burn-in ARMED (fresh process view), every corpus resample
        # must survive the dtype-exact self-check — no autorevert, budget
        # genuinely consumed (proves the crate frames are byte-for-byte the
        # oracle's through the comparator that guards production).
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        lrs.reset_lake_rust_resample_for_tests()
        before = lrs._BURN_IN_REMAINING[0]
        df_1m = _corpus()["a_regular_day"]
        for tf in (2, 5, 15, 375):
            tf_resampler.resample_1m_to_tf(df_1m, tf)
        assert lrs.rust_resample_disabled() is False
        assert lrs._BURN_IN_REMAINING[0] == before - 4

    def test_identity_tf1_round_trips_the_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # (h) explicitly: the identity arm crosses the FFI and must come
        # back frame-equal (never byte-equal — a fresh IPC serialisation).
        df_1m = _corpus()["f_oi_greeks_overlay"]
        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        monkeypatch.setattr(tf_resampler, "_resample_1m_to_tf_python", _bomb_oracle)
        got = tf_resampler.resample_1m_to_tf(df_1m, 1)
        assert got.schema == df_1m.schema
        assert got.equals(df_1m)


# ---------------------------------------------------------------------------
# whole-prebuild A/B — derived partition bytes sha256-equal ON vs OFF
# ---------------------------------------------------------------------------

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_SYMBOL = "NSE-NIFTY"


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
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        interval="1minute",
        bars=bars,
    )


def _materialize(lake_root: Path) -> None:
    from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes

    materialize_lake_timeframes(
        lake_root=lake_root,
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol=_SYMBOL,
        kind=PrebuildKind.SPOT,
    )


def _derived_hashes(lake_root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(lake_root.rglob("*.parquet")):
        rel = path.relative_to(lake_root).as_posix()
        if "/1minute/" in f"/{rel}":
            continue  # the seeded base — identical by construction
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class TestWholePrebuildByteParity:
    def test_materialized_partition_bytes_equal_on_vs_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake_off = tmp_path / "lake-off"
        lake_on = tmp_path / "lake-on"

        monkeypatch.delenv(lrs.LAKE_RUST_RESAMPLE_ENV, raising=False)
        _seed_lake(lake_off)
        _materialize(lake_off)
        off_hashes = _derived_hashes(lake_off)

        monkeypatch.setenv(lrs.LAKE_RUST_RESAMPLE_ENV, "1")
        lrs.reset_lake_rust_resample_for_tests(remaining=0)
        _seed_lake(lake_on)
        _materialize(lake_on)
        on_hashes = _derived_hashes(lake_on)

        assert lrs.rust_resample_disabled() is False, (
            f"the Rust path latched OFF mid-prebuild ({lrs._DISABLED_REASON[0]}) — "
            "the A/B would compare Python against Python (vacuous)"
        )
        assert off_hashes, "no derived partitions were written — the A/B is vacuous"
        assert on_hashes == off_hashes
