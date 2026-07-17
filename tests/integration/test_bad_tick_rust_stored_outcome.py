"""OFF-vs-ON STORED-OUTCOME parity for the frame bad-tick routing (SLICE T24 PR-2).

DEC-DATA-RUST-INTEGRITY-DETECTORS-001. The seam suite pins FRAME-LEVEL
parity (kept rows + warn streams over the hostile corpus); this suite widens
the gate to the STORED OUTCOME the screen actually reaches in production —
the derived-partition WRITE path (DEC-READ-GUARD-PARITY-001: the prebuild's
1-minute read runs ``screen_frame_bad_ticks`` before every derived-TF
resample, so a screened-out spike must never poison a stored derived
bucket):

* a real-writer lake whose stored 1-minute legitimately holds ONE
  self-consistent ~5x in-session spike (write-side guards accept it — the
  exact class the screen exists for) is prebuilt through the REAL
  ``materialize_lake_timeframes`` flag OFF then
  ``BRUTEX_FEED_RUST_FRAME=1`` in the post-burn-in steady state
  (``remaining=0`` — the kernel decisions genuinely SHIP), and the stored
  bytes of EVERY file under ``bars/`` (the 1-minute source AND the derived
  5-minute partition) are asserted sha256-IDENTICAL, with the screen proven
  to have FIRED in both arms (anti-vacuous) and the
  DEC-DERIVED-FRESHNESS-SCREEN-PARITY-001 verdict reading "fresh" on both;
* a poisoned keep-all kernel must FLIP the stored derived bytes (the spike
  freezes into its 5-minute bucket) — the teeth proving the parity gate
  genuinely bites on the stored outcome.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import brutex.data.bad_tick_rust as btr
import brutex.data.detectors_rust as dr
import pytest
from brutex.data import tf_autoderive
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.data.timeframe_materialize import PrebuildKind, materialize_lake_timeframes
from structlog.testing import capture_logs

from tests.fakes.feed_scan_crate import fake_feed_crate, fake_scan_bars

pl = pytest.importorskip("polars")


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The OFF arm must be genuinely all-OFF even under an armed lane
    # environment: the frame flag + the kill switch AND every brutex-lake
    # detector-law flag (the sink append's raw-integrity hash + the spot
    # observe classifies would otherwise route mid-"OFF" — the SLICE T24
    # PR-2 isolation hardening, mirroring the PR-1 stored-outcome template).
    monkeypatch.delenv(btr.FEED_RUST_FRAME_ENV, raising=False)
    monkeypatch.delenv("BRUTEX_BAD_TICK_SCREEN", raising=False)
    monkeypatch.delenv(dr.LAKE_RUST_RAW_INTEGRITY_ENV, raising=False)
    monkeypatch.delenv(dr.LAKE_RUST_INTERIOR_GAP_ENV, raising=False)
    monkeypatch.delenv(dr.LAKE_RUST_PARTIAL_WINDOW_ENV, raising=False)
    monkeypatch.delenv(dr.LAKE_RUST_TODAY_COMPLETENESS_ENV, raising=False)
    btr.reset_bad_tick_rust_for_tests()
    yield
    btr.reset_bad_tick_rust_for_tests()


def _seed_lake_1m_spike(lake_root: Path) -> None:
    """Write in-session 1-minute bars with ONE self-consistent ~5x spike bar.

    The ``test_tf_autoderive.py`` fixture shape (DEC-SWEEP-DATA-EDGE-GUARDS-001
    / #1932): the 09:25 IST spike bar has every OHLC field scaled together —
    internally ordered, strictly positive — so it passes every write-side
    guard and is genuinely STORED; only the read-boundary screen catches it.
    """
    ist = timezone(timedelta(hours=5, minutes=30))
    bars: list[CandleBar] = []
    for i in range(20):
        ts = datetime(2024, 1, 2, 9, 16 + i, tzinfo=ist).astimezone(UTC)
        close = Decimal("500.00") if i == 9 else Decimal("100.00") + Decimal(i) / 10
        bars.append(
            CandleBar(
                timestamp=ts,
                open=close - Decimal("0.05"),
                high=close + Decimal("0.05"),
                low=close - Decimal("0.10"),
                close=close,
                volume=1000,
                open_interest=None,
            )
        )
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    sink.append(exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=bars)


def _run_production_prebuild(lake_root: Path) -> dict[str, str]:
    """Seed the spike lake + run the REAL prebuild primitive; return stored shas.

    ``materialize_lake_timeframes`` is the production derived-TF writer (the
    #1145 pipeline prebuild's per-symbol primitive); its 1-minute read runs
    the frame bad-tick screen, so the stored 5-minute partition IS the
    screen's stored outcome. Returns the sha256 of every file under
    ``bars/`` keyed by relative path — the 1-minute source AND the derived
    partition compare byte-for-byte across arms.
    """
    _seed_lake_1m_spike(lake_root)
    materialize_lake_timeframes(
        lake_root=lake_root,
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        kind=PrebuildKind.SPOT,
        tfs=["5m"],
        incremental=False,  # the forced FULL derive — never the stamp skip
    )
    bars_root = lake_root / "bars"
    return {
        str(path.relative_to(lake_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(bars_root.rglob("*"))
        if path.is_file()
    }


def _derived_frame(lake_root: Path) -> object:
    months = sorted(lake_root.glob("bars/NSE/INDEX/NIFTY/5minute/**/*.parquet"))
    assert len(months) == 1  # the derive genuinely stored ONE month partition
    return pl.read_parquet(months[0])


def _verdicts(lake_root: Path) -> dict[tuple[str, str], str]:
    return tf_autoderive.lake_derived_freshness_verdicts(
        lake_root,
        exchange="NSE",
        segment="INDEX",
        symbols=["NIFTY"],
        intervals=["5minute"],
    )


@pytest.mark.parametrize("kernel_source", ["fake", "real"])
def test_prebuild_stored_outcome_identical_off_vs_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kernel_source: str
) -> None:
    """The derived-partition bytes are IDENTICAL OFF vs ON — and the screen FIRED."""
    # --- OFF arm (the default: flag deleted by the autouse fixture). ---
    with capture_logs() as off_logs:
        off_files = _run_production_prebuild(tmp_path / "off_lake")
    # ANTI-VACUOUS: the screen genuinely caught the stored spike on the OFF
    # arm — one prebuild-attributed warn, and the derived partition holds NO
    # trace of the 500.00 bar (its bucket high stays in the clean band).
    off_warns = [e for e in off_logs if e["event"] == "feed.bad_tick_skipped"]
    assert len(off_warns) == 1
    assert str(off_warns[0]["source"]).startswith("prebuild:")
    off_frame = _derived_frame(tmp_path / "off_lake")
    assert off_frame.get_column("high").max() < 200.0  # type: ignore[attr-defined,operator]
    assert not any(e["event"] == "bad_tick_rust.engaged" for e in off_logs)
    assert _verdicts(tmp_path / "off_lake") == {("NIFTY", "5minute"): "fresh"}

    # --- ON arm (the frame flag, steady state, real-or-fake kernel). ---
    monkeypatch.setenv(btr.FEED_RUST_FRAME_ENV, "1")
    if kernel_source == "real":
        pytest.importorskip("brutex_feed")
        calls: dict[str, int] | None = None
    else:
        calls = {"scan": 0}

        def _counting_scan(*args: object) -> object:
            assert calls is not None
            calls["scan"] += 1
            return fake_scan_bars(*args)  # type: ignore[arg-type]

        monkeypatch.setitem(sys.modules, "brutex_feed", fake_feed_crate(scan_bars=_counting_scan))
    # Steady state: the kernel decisions genuinely SHIP (no burn-in oracle in
    # the serving path) — the strongest stored-outcome claim.
    btr.reset_bad_tick_rust_for_tests(remaining=0)
    with capture_logs() as on_logs:
        on_files = _run_production_prebuild(tmp_path / "on_lake")
    # The kernel genuinely ENGAGED (never a silently-degraded oracle-only
    # run) and nothing latched/autoreverted mid-run.
    assert any(e["event"] == "bad_tick_rust.engaged" for e in on_logs)
    assert btr.frame_screen_rust_disabled() is False
    if calls is not None:
        assert calls["scan"] > 0

    # (1) STORED BYTES: the whole bars tree — 1-minute source AND the derived
    # 5-minute partition — is byte-identical (paths AND content).
    assert on_files == off_files
    assert any("5minute" in rel for rel in off_files)  # the derive genuinely stored
    # (2) The warn stream parity held on the write path too.
    on_warns = [e for e in on_logs if e["event"] == "feed.bad_tick_skipped"]
    assert len(on_warns) == 1
    assert on_warns[0]["skipped"] == off_warns[0]["skipped"]
    # (3) The #1932 screened-vs-screened freshness verdict agrees across arms
    # (the gate's re-derivation ALSO routes through the armed kernel here).
    assert _verdicts(tmp_path / "on_lake") == {("NIFTY", "5minute"): "fresh"}


def test_gate_teeth_poisoned_kernel_flips_the_stored_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANTI-VACUOUS teeth: a keep-all kernel visibly FLIPS the stored derived bytes.

    A steady-state (post-burn-in) kernel that unscreens the spike must freeze
    the 500.00 bar into its stored 5-minute bucket — proving the parity
    assertions above genuinely bite on the STORED outcome (they are not an
    oracle-vs-oracle tautology). Production carries the burn-in autorevert
    against exactly this; ``remaining=0`` bypasses it BY DESIGN here to
    demonstrate the gate's sensitivity.
    """
    with capture_logs():
        off_files = _run_production_prebuild(tmp_path / "off_lake")
    assert _derived_frame(tmp_path / "off_lake").get_column("high").max() < 200.0  # type: ignore[attr-defined,operator]

    monkeypatch.setenv(btr.FEED_RUST_FRAME_ENV, "1")
    poisoned = fake_feed_crate(
        # Keep EVERY row, no warns, no positivity stop — the unscreened arm.
        scan_bars=lambda ts_us, o, h, l, c, enabled, ratio: (list(range(len(c))), [], None)
    )
    monkeypatch.setitem(sys.modules, "brutex_feed", poisoned)
    btr.reset_bad_tick_rust_for_tests(remaining=0)  # bypass the burn-in shield
    with capture_logs():
        on_files = _run_production_prebuild(tmp_path / "on_lake")

    # The poisoned kernel laundered the spike into the STORED derived bucket:
    # the derived bytes DIVERGE (the 1-minute source — written pre-screen —
    # stays identical) and the poisoned bucket carries the 500.00 high.
    assert on_files != off_files
    one_minute = {rel: sha for rel, sha in off_files.items() if "1minute" in rel}
    assert one_minute == {rel: sha for rel, sha in on_files.items() if "1minute" in rel}
    assert {rel for rel in on_files if "5minute" in rel} == {
        rel for rel in off_files if "5minute" in rel
    }
    assert any(on_files[rel] != off_files[rel] for rel in on_files if "5minute" in rel)
    assert _derived_frame(tmp_path / "on_lake").get_column("high").max() >= 500.0  # type: ignore[attr-defined,operator]
