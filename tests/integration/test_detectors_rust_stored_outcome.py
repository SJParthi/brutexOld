"""OFF-vs-ON STORED-OUTCOME parity for the T24 detector routing (SLICE T24 PR-1).

DEC-DATA-RUST-INTEGRITY-DETECTORS-001 (MIGRATION.md #1948). The three seam
suites pin LAW-LEVEL parity (verdict structures / runs lists / digest hex);
this suite widens the gate to the STORED OUTCOME the laws actually reach in
production:

* ``interior_gap_detect`` + ``partial_window_detect`` feed the spot ingest
  tail's #1031 DEMOTION machinery
  (``ingest._process_window_tail`` → ``WindowLedger.record(degraded=…)``) —
  so a real-writer pull over (a) a confirmed interior multi-session hole and
  (b) a trailing truncation is driven TWICE, all three flags OFF then ON, and
  the STORED parquet bytes + the LEDGER records (status + demotion reasons)
  are asserted IDENTICAL, with the demotions proven to have FIRED in BOTH
  arms (anti-vacuous — the hole/truncation was actually caught).
* ``raw_integrity`` digests land in the durable sidecar store
  (``make_capture_hook`` → ``append_record`` → ``raw_integrity/<shard>.jsonl``)
  — the stored shard lines are asserted identical OFF vs ON (modulo the
  wall-clock ``captured_at``) and the stored ``payload_hash`` is pinned to
  the frozen golden digest.

The ON arm runs in the post-burn-in steady state (``remaining=0`` — the Rust
value genuinely SHIPS, no oracle in the serving path) and is parametrized
over the shared fake crate (always runnable; call-counted so engagement is
proven, never assumed) and the REAL built wheel (``importorskip``-gated —
exercised by the lake-rust-parity lane, which installs the wheel).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import brutex.data.detectors_rust as dr
import pytest
from brutex.adapters.groww.ingest import ingest_spot
from brutex.data.interior_gap_detect import INTERIOR_GAP
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.partial_window_detect import SUSPECTED_PARTIAL
from brutex.data.raw_integrity_store import integrity_dir, make_capture_hook
from brutex.data.sink import ParquetBarSink
from brutex.data.window_ledger import SKIPPED, SUCCESS, WindowLedger
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from structlog.testing import capture_logs

from tests.fakes.detectors_crate import (
    fake_blake2b_hex,
    fake_classify_partial_window_verdict,
    fake_detectors_crate,
    fake_interior_barless_runs,
)

# ---------------------------------------------------------------------------
# Fixtures — the EXACT #1031 demotion shapes (TestIngestSpotCompletenessRepull's
# proven date grid: 04:05 UTC = 09:35 IST, in the [09:15, 15:30) session).
# ---------------------------------------------------------------------------

#: The three routing flags — armed/disarmed TOGETHER (the production shape).
_FLAG_ENVS: tuple[str, ...] = (
    dr.LAKE_RUST_RAW_INTEGRITY_ENV,
    dr.LAKE_RUST_INTERIOR_GAP_ENV,
    dr.LAKE_RUST_PARTIAL_WINDOW_ENV,
)

#: A fixed injected clock (the existing suite's 2026 clock) so BOTH arms
#: classify against the identical "today" — full determinism across arms.
_NOW_IST = datetime(2026, 6, 19, 8, 0, tzinfo=UTC)

#: The frozen golden payload + digest — byte-identical to
#: ``test_raw_integrity.py``'s ``test_hash_golden_frozen_value`` contract
#: (re-pinned here at the STORED-sidecar level).
_PAYLOAD: dict[str, object] = {
    "candles": [
        ["2024-01-01T09:15:00", 100.0, 110.0, 90.0, 105.0, 777, 42],
        ["2024-01-01T09:16:00", 105.0, 112.0, 104.0, 111.0, 800, 43],
    ]
}
_GOLDEN_DIGEST = "6efde60fa62320bf1b15eda6b132ac27b7f687f3ff731b17b2e204426b8e38c5"


def _bar(ts: datetime) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=1,
        open_interest=None,
    )


def _inst(symbol: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


#: (a) Interior multi-session hole bookended by real bars (>5 sessions).
_GAPPY_BARS: tuple[CandleBar, ...] = (
    _bar(datetime(2021, 3, 1, 4, 5, tzinfo=UTC)),
    _bar(datetime(2021, 3, 2, 4, 5, tzinfo=UTC)),
    _bar(datetime(2021, 3, 26, 4, 5, tzinfo=UTC)),
    _bar(datetime(2021, 3, 29, 4, 5, tzinfo=UTC)),
)
#: (b) Trailing truncation — bars stop on day 2 of the window.
_TRUNCATED_BARS: tuple[CandleBar, ...] = (
    _bar(datetime(2021, 3, 1, 4, 5, tzinfo=UTC)),
    _bar(datetime(2021, 3, 2, 4, 5, tzinfo=UTC)),
)


class _Adapter:
    """Fake MarketAdapter returning a fixed bar list per fetch (the ingest-suite idiom)."""

    def __init__(self, bars: Sequence[CandleBar]) -> None:
        self._bars = list(bars)

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval, start, end
        return list(self._bars)


@pytest.fixture(autouse=True)
def _isolated_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for env in _FLAG_ENVS:
        monkeypatch.delenv(env, raising=False)
    dr.reset_detectors_rust_for_tests()
    yield
    dr.reset_detectors_rust_for_tests()


def _arm_flags_on(monkeypatch: pytest.MonkeyPatch, crate_source: str) -> dict[str, int] | None:
    """Arm all three flags in the post-burn-in steady state; return fake-crate call counters.

    ``crate_source == "real"`` requires the built wheel (skip when absent —
    the lake-rust-parity lane installs it); ``"fake"`` installs the shared
    law-faithful fake crate wrapped with CALL COUNTERS so engagement is
    PROVEN, never assumed (a silently-degrading ON arm would otherwise make
    this an oracle-vs-oracle vacuous parity).
    """
    for env in _FLAG_ENVS:
        monkeypatch.setenv(env, "1")
    if crate_source == "real":
        pytest.importorskip("brutex_lake")
        counters: dict[str, int] | None = None
    else:
        counters = {"digest": 0, "interior": 0, "partial": 0}

        def _counting_digest(data: bytes, digest_size: int) -> str:
            assert counters is not None
            counters["digest"] += 1
            return fake_blake2b_hex(data, digest_size)

        def _counting_interior(
            covered: Sequence[int], interior_sessions: Sequence[int]
        ) -> list[tuple[int, int, int]]:
            assert counters is not None
            counters["interior"] += 1
            return fake_interior_barless_runs(covered, interior_sessions)

        def _counting_partial(*args: object) -> tuple[str, int | None, int | None, int] | None:
            assert counters is not None
            counters["partial"] += 1
            return fake_classify_partial_window_verdict(*args)  # type: ignore[arg-type]

        crate = fake_detectors_crate(
            blake2b_hex=_counting_digest,
            interior_runs=_counting_interior,
            classify_partial=_counting_partial,
        )
        monkeypatch.setitem(sys.modules, "brutex_lake", crate)
    # Steady state: the Rust value genuinely SHIPS (no burn-in oracle in the
    # serving path) — the strongest stored-outcome claim.
    dr.reset_detectors_rust_for_tests(remaining=0)
    return counters


# ---------------------------------------------------------------------------
# The production pull driver + stored-outcome projections.
# ---------------------------------------------------------------------------


def _run_production_pull(lake_root: Path) -> tuple[dict[str, str], list[dict[str, object]]]:
    """ONE deterministic real-writer pull over the two demotion shapes.

    Drives the REAL production consumer chain the detectors feed —
    ``ingest_spot`` with ``detect_completeness_gaps=True`` over an INDEX
    instrument, a REAL :class:`ParquetBarSink` (real parquet bytes on disk)
    and a REAL :class:`WindowLedger` (the #1031 demotion authority).

    Returns ``(stored_files, ledger_rows)`` — the sha256 of every file under
    ``bars/`` keyed by relative path, and the ledger records normalised for
    cross-arm comparison (the two wall-clock stamp fields dropped; every
    OTHER field — status, ``last_error`` demotion reason, bars, counters —
    compared verbatim).
    """
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    ledger = WindowLedger()
    # (a) One 29-day 1-minute window with a confirmed interior hole.
    ingest_spot(
        _Adapter(_GAPPY_BARS),  # type: ignore[arg-type]
        sink,
        instrument=_inst("NIFTY"),
        interval="1minute",
        start=date(2021, 3, 1),
        end=date(2021, 3, 30),
        ledger=ledger,
        detect_completeness_gaps=True,
        now_ist=lambda: _NOW_IST,
    )
    # (b) A trailing truncation over ONE 30-day window [2021-03-01, 2021-03-31)
    # — a single window per symbol keeps the pull fully deterministic (two
    # windows of one series race their keep-first dedup order across the
    # parallel window workers, flipping WHICH window reads bars_net_new=0).
    ingest_spot(
        _Adapter(_TRUNCATED_BARS),  # type: ignore[arg-type]
        sink,
        instrument=_inst("BANKNIFTY"),
        interval="1minute",
        start=date(2021, 3, 1),
        end=date(2021, 3, 31),
        ledger=ledger,
        detect_completeness_gaps=True,
        now_ist=lambda: _NOW_IST,
    )
    bars_root = lake_root / "bars"
    stored_files = {
        str(path.relative_to(lake_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(bars_root.rglob("*"))
        if path.is_file()
    }
    rows: list[dict[str, object]] = []
    for rec in ledger.records():
        row = dataclasses.asdict(rec)
        # The ONLY wall-clock fields — everything else compares verbatim.
        row.pop("first_seen_at")
        row.pop("last_attempt_at")
        rows.append(row)
    rows.sort(key=lambda r: (str(r["security_id"]), str(r["window_start"]), str(r["interval"])))
    return stored_files, rows


def _demotion_counts(rows: list[dict[str, object]]) -> Counter[str]:
    """Count the demoted (non-skippable) rows per recorded demotion reason."""
    return Counter(
        str(row["last_error"])
        for row in rows
        if row["status"] not in {SUCCESS, SKIPPED}
        and row["last_error"] in {INTERIOR_GAP, SUSPECTED_PARTIAL}
    )


# ---------------------------------------------------------------------------
# The stored-outcome parity gates.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("crate_source", ["fake", "real"])
def test_demotion_stored_outcome_identical_off_vs_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crate_source: str
) -> None:
    """Stored parquet bytes + ledger demotions are IDENTICAL OFF vs ON — and FIRED."""
    # --- OFF arm (the default: flags deleted by the autouse fixture). ---
    with capture_logs() as off_logs:
        off_files, off_rows = _run_production_pull(tmp_path / "off_lake")
    assert not any(e["event"] == "detectors_rust.engaged" for e in off_logs)
    off_demotions = _demotion_counts(off_rows)
    # ANTI-VACUOUS: both demotion classes genuinely fired on the OFF arm.
    assert off_demotions[INTERIOR_GAP] >= 1
    assert off_demotions[SUSPECTED_PARTIAL] >= 1

    # --- ON arm (all three flags, steady state, real-or-fake crate). ---
    counters = _arm_flags_on(monkeypatch, crate_source)
    with capture_logs() as on_logs:
        on_files, on_rows = _run_production_pull(tmp_path / "on_lake")
    # The crate genuinely ENGAGED (never a silently-degraded oracle-only run) …
    assert any(e["event"] == "detectors_rust.engaged" for e in on_logs)
    # … and nothing latched/autoreverted mid-run (parity held internally too).
    assert dr.rust_detectors_disabled() is False
    if counters is not None:
        assert counters["interior"] > 0
        assert counters["partial"] > 0

    # (1) STORED BYTES: the bars tree is byte-identical (paths AND content).
    assert on_files == off_files
    assert off_files  # the pull genuinely stored parquet files
    # (2) LEDGER: every record identical — status + demotion reason included.
    assert on_rows == off_rows
    # (3) ANTI-VACUOUS ON: the demotions fired identically in the ON arm.
    assert _demotion_counts(on_rows) == off_demotions


def test_gate_teeth_poisoned_crate_flips_the_stored_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANTI-VACUOUS teeth: a law-divergent crate visibly FLIPS the ledger outcome.

    A steady-state (post-burn-in) crate that claims "no interior runs" must
    make the gappy window settle un-demoted — proving the parity assertions
    above genuinely bite on the STORED outcome (they are not an
    oracle-vs-oracle tautology). Production carries the burn-in autorevert
    against exactly this; ``remaining=0`` bypasses it BY DESIGN here to
    demonstrate the gate's sensitivity.
    """
    with capture_logs():
        _, off_rows = _run_production_pull(tmp_path / "off_lake")
    assert _demotion_counts(off_rows)[INTERIOR_GAP] >= 1

    for env in _FLAG_ENVS:
        monkeypatch.setenv(env, "1")
    poisoned = fake_detectors_crate(interior_runs=lambda covered, interior: [])
    monkeypatch.setitem(sys.modules, "brutex_lake", poisoned)
    dr.reset_detectors_rust_for_tests(remaining=0)  # bypass the burn-in shield
    with capture_logs():
        _, on_rows = _run_production_pull(tmp_path / "on_lake")

    # The poisoned law laundered the hole — the demotion did NOT fire and the
    # ledger rows DIVERGE: exactly the failure shape the parity test catches.
    assert _demotion_counts(on_rows)[INTERIOR_GAP] == 0
    assert on_rows != off_rows


@pytest.mark.parametrize("crate_source", ["fake", "real"])
def test_raw_integrity_stored_sidecar_identical_off_vs_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crate_source: str
) -> None:
    """The durable raw-integrity sidecar lines are IDENTICAL OFF vs ON (golden-pinned)."""

    def _capture_lines(lake_root: Path) -> list[dict[str, object]]:
        # The REAL production consumer: the per-symbol capture hook the live
        # build sites inject (build_record → append_record → the shard file).
        hook = make_capture_hook(
            lake_root, exchange="NSE", segment="CASH", groww_symbol="NSE-NIFTY"
        )
        hook(_PAYLOAD, "1minute", "2024-01-01 09:15:00", "2024-01-01 09:30:00", "NSE-NIFTY")
        shards = sorted(integrity_dir(lake_root).glob("*.jsonl"))
        assert len(shards) == 1  # one symbol → one shard, actually persisted
        lines: list[dict[str, object]] = []
        for raw in shards[0].read_text(encoding="utf-8").splitlines():
            obj = json.loads(raw)
            assert isinstance(obj, dict)
            # The ONLY wall-clock field — everything else compares verbatim.
            obj.pop("captured_at")
            lines.append(obj)
        return lines

    off_lines = _capture_lines(tmp_path / "off_lake")

    counters = _arm_flags_on(monkeypatch, crate_source)
    on_lines = _capture_lines(tmp_path / "on_lake")
    assert dr.rust_detectors_disabled() is False
    if counters is not None:
        assert counters["digest"] > 0  # the routed digest genuinely served

    assert on_lines == off_lines
    assert len(off_lines) == 1
    # The STORED digest is the frozen golden — the byte-level anti-vacuity pin
    # (a wrong-but-equal-on-both-arms digest cannot hide behind the parity).
    assert off_lines[0]["payload_hash"] == _GOLDEN_DIGEST
