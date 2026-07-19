"""Self-test law (§7): teeth smoke (3 poisons, gating) + the full poison battery.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1958). A verifier that cannot
demonstrate it detects planted poison ON THIS BOX withholds all real-lake
verdicts (exit 2). The twin lake is built with the REAL production writers
(``ParquetWriter``, the real resamplers, the real ledger/bridge writers —
never hand-written parquet: the #1631 fixture-only blindspot is the enemy);
poisons are surgical edits ON TOP of real-writer output.
"""

from __future__ import annotations

import shutil
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import polars as pl
import structlog

from brutex.acceptance.context import AcceptanceContext
from brutex.acceptance.model import TeethVerdict, Verdict

_LOG = structlog.get_logger(__name__)

#: IST session used by the twin builder (regular day, 31 bars is plenty).
_TWIN_BARS_PER_DAY: Final[int] = 31

SMOKE_POISONS: Final[int] = 3


@dataclass(frozen=True)
class PoisonOutcome:
    """One planted poison and whether its detector actually fired."""

    name: str
    detected: bool
    detail: str


@dataclass(frozen=True)
class SelfTestResult:
    """The self-test block of the report (§5.2 ``self_test``)."""

    mode: str  # "smoke" | "full" | "skipped"
    poisons_planted: int
    poisons_detected: int
    false_fails_on_clean: int
    verdict: TeethVerdict
    duration_s: float
    outcomes: tuple[PoisonOutcome, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        """Project to the frozen report-schema shape."""
        return {
            "mode": self.mode,
            "poisons_planted": self.poisons_planted,
            "poisons_detected": self.poisons_detected,
            "false_fails_on_clean": self.false_fails_on_clean,
            "verdict": str(self.verdict),
            "duration_s": round(self.duration_s, 3),
        }


def skipped_selftest() -> SelfTestResult:
    """The ``--no-self-test`` record — teeth not demonstrated this run."""
    return SelfTestResult(
        mode="skipped",
        poisons_planted=0,
        poisons_detected=0,
        false_fails_on_clean=0,
        verdict=TeethVerdict.TEETH_SKIPPED,
        duration_s=0.0,
    )


def twin_sessions(now_utc: datetime) -> tuple[date, date]:
    """Two recent REAL NSE sessions strictly before today (twin bar days)."""
    from brutex.calendars.nse import previous_trading_session

    today = (now_utc.astimezone(UTC) + timedelta(hours=5, minutes=30)).date()
    d2 = previous_trading_session(today)
    if d2 is None:  # calendar coverage exhausted — pin a known 2026 session pair
        return date(2026, 7, 8), date(2026, 7, 9)
    d1 = previous_trading_session(d2)
    if d1 is None:
        return date(2026, 7, 8), d2
    return d1, d2


def _twin_bars(day: date, *, base: float) -> list[Any]:
    """One session of 2dp-snapped 1-minute bars starting 09:15 IST."""
    from brutex.data.models import CandleBar

    open_utc = datetime.combine(day, time(3, 45), tzinfo=UTC)  # 09:15 IST
    bars = []
    for i in range(_TWIN_BARS_PER_DAY):
        px = Decimal(f"{base + i * 0.05:.2f}")
        bars.append(
            CandleBar(
                timestamp=open_utc + timedelta(minutes=i),
                open=px,
                high=px + Decimal("0.10"),
                low=px - Decimal("0.10"),
                close=px + Decimal("0.05"),
                volume=10 + i,
                open_interest=None,
            )
        )
    return bars


def _write_grouped(
    writer: Any, *, symbol: str, interval: str, bars: list[Any], segment: str = "INDEX"
) -> None:
    """Write bars through the real writer, grouped by (year, month) partition."""
    by_month: dict[tuple[int, int], list[Any]] = {}
    for bar in bars:
        by_month.setdefault((bar.timestamp.year, bar.timestamp.month), []).append(bar)
    for _cell, group in sorted(by_month.items()):
        writer.write(exchange="NSE", segment=segment, symbol=symbol, interval=interval, bars=group)


def build_twin_lake(
    root: Path,
    *,
    days: tuple[date, date],
    symbols: tuple[str, ...] = ("NIFTY", "BANKNIFTY", "INDIAVIX"),
    stock_symbols: tuple[str, ...] = (),
) -> None:
    """Build the tiny twin lake with the REAL writers + real resamplers.

    Per symbol: two sessions of 1-minute bars, the derived ``5minute``
    partition (production ``resample_1m_to_tf``) and the derived ``1day``
    partition (production ``resample_1m_to_daily``) — all landed through
    ``ParquetWriter``. *stock_symbols* land under the ``CASH`` segment —
    NON-engine coordinates, so the battery can prove the census leg has a
    tooth of its OWN (round-4 B4-3; default empty keeps every existing twin
    byte-identical).
    """
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.spot_daily_resample import frame_to_candle_bars, resample_1m_to_daily
    from brutex.data.tf_autoderive import read_lake_bars_frame
    from brutex.data.tf_resampler import resample_1m_to_tf

    writer = ParquetWriter(lake_root=root)
    cells = [(symbol, "INDEX") for symbol in symbols]
    cells.extend((symbol, "CASH") for symbol in stock_symbols)
    for offset, (symbol, segment) in enumerate(cells):
        # One write per session day — bars may span calendar months and the
        # writer (correctly) refuses a multi-partition batch.
        for day_index, day in enumerate(days):
            writer.write(
                exchange="NSE",
                segment=segment,
                symbol=symbol,
                interval="1minute",
                bars=_twin_bars(day, base=100.0 + offset * 50 + day_index * 10),
            )
        symbol_dir = root / "bars" / "NSE" / segment / symbol
        df_1m = read_lake_bars_frame(symbol_dir, "1minute")
        df_5m = resample_1m_to_tf(df_1m, 5)
        _write_grouped(
            writer,
            symbol=symbol,
            interval="5minute",
            bars=frame_to_candle_bars(df_5m, absent_open_interest=None),
            segment=segment,
        )
        daily = resample_1m_to_daily(df_1m, before=days[1] + timedelta(days=1))
        _write_grouped(writer, symbol=symbol, interval="1day", bars=daily, segment=segment)


def _month_file(
    root: Path, symbol: str, interval: str, day: date, *, segment: str = "INDEX"
) -> Path:
    """Return the lake month-partition path for a symbol/interval/day.

    Returns
    -------
    Path
        The ``<root>/bars/.../<yyyy>/<mm>.parquet`` partition path.
    """
    return (
        root
        / "bars"
        / "NSE"
        / segment
        / symbol
        / interval
        / f"{day.year:04d}"
        / f"{day.month:02d}.parquet"
    )


def poison_duplicate_row(root: Path, *, symbol: str, day: date, segment: str = "INDEX") -> None:
    """S1 — append a duplicate-timestamp row (surgical edit on writer output)."""
    path = _month_file(root, symbol, "1minute", day, segment=segment)
    frame = pl.read_parquet(path, glob=False)
    pl.concat([frame, frame.head(1)]).write_parquet(path)


def poison_tf_high_x10(root: Path, *, symbol: str, day: date) -> None:
    """S2 — scale one stored 5m bar's HIGH x10 (the laundering class)."""
    path = _month_file(root, symbol, "5minute", day)
    frame = pl.read_parquet(path, glob=False)
    high = frame.get_column("high").to_list()
    high[0] = high[0] * 10
    frame.with_columns(pl.Series("high", high)).write_parquet(path)


def poison_1day_value(root: Path, *, symbol: str, day: date, segment: str = "INDEX") -> None:
    """B18 — corrupt a DERIVABLE, non-forming stored ``1day`` close.

    Kills the tf.daily VALUE-equality comparator (``stored_vals != got``) — the
    core of the NEW 1day compare (inventory GAP 1). Finds the stored bar whose
    IST date is *day* (a midnight-IST bar is 18:30 UTC on the prior day, so it
    can live in either the day's or the previous UTC-month file) and bumps its
    close, so the re-derive from the untouched 1m disagrees.
    """
    day_dir = root / "bars" / "NSE" / segment / symbol / "1day"
    target_ist = day
    for path in sorted(day_dir.rglob("*.parquet")):
        frame = pl.read_parquet(path, glob=False)
        stamps = frame.get_column("timestamp").to_list()
        ist_dates = [(ts + timedelta(hours=5, minutes=30)).date() for ts in stamps]
        if target_ist not in ist_dates:
            continue
        idx = ist_dates.index(target_ist)
        closes = frame.get_column("close").to_list()
        closes[idx] = float(closes[idx]) + 100.0
        frame.with_columns(pl.Series("close", closes)).write_parquet(path)
        return
    raise RuntimeError(f"no stored 1day bar for IST date {target_ist} to poison")


def poison_1m_bad_tick_x10(root: Path, *, symbol: str, day: date) -> None:
    """B19 — scale one NON-first in-session 1-minute close x10 (the #1616 class).

    Kills the ``sanity.bad_tick`` predicate (``is_bad_tick`` on the 1-minute
    close-to-close ratio) — distinct from the tf x10 poison (a 5m HIGH the tf
    freshness leg catches). x10 of a 2dp value stays 2dp, so it never trips the
    tick-grid law; the bar is self-consistent so price-sanity passes too.
    """
    path = _month_file(root, symbol, "1minute", day)
    frame = pl.read_parquet(path, glob=False).sort("timestamp")
    closes = frame.get_column("close").to_list()
    # The twin always stores _TWIN_BARS_PER_DAY (31) bars per session, so bar 2
    # and its in-session predecessor bar 1 always exist.
    closes[2] = float(closes[1]) * 10.0  # bar 2 vs its in-session predecessor
    frame.with_columns(pl.Series("close", closes)).write_parquet(path)


def poison_preopen_bar(root: Path, *, symbol: str, day: date) -> None:
    """B20 — append a 08:00-IST (pre-open) 1-minute bar on a trading day.

    Kills the ``sanity.session_window`` predicate (a bar whose IST minute is
    before the session open). A pre-open bar on a REAL trading date isolates
    the window law without tripping the non-trading-date arm or contiguity.
    """
    path = _month_file(root, symbol, "1minute", day)
    frame = pl.read_parquet(path, glob=False)
    head = frame.head(1)
    stamp = head.get_column("timestamp").to_list()[0]
    preopen = stamp.replace(hour=2, minute=30)  # 08:00 IST = 02:30 UTC same date
    pl.concat([frame, head.with_columns(pl.Series("timestamp", [preopen]))]).write_parquet(path)


def _twin_context(
    root: Path, *, now_utc: datetime, client_factory: Callable[[], Any] | None = None
) -> AcceptanceContext:
    """Build an ``AcceptanceContext`` over a self-test twin lake.

    Returns
    -------
    AcceptanceContext
        A context pinned to ``root`` with the fixed self-test seed/clock.
    """
    return AcceptanceContext(
        lake_root=root,
        seed="selftest",
        sample_n=50,
        months=6,
        now_utc=lambda: now_utc,
        client_factory=client_factory,
    )


def _check_fails(check: Any) -> bool:
    """Return whether a check result carries a FAIL verdict.

    Returns
    -------
    bool
        ``True`` if ``check.verdict`` is ``Verdict.FAIL``.
    """
    return check.verdict is Verdict.FAIL


def run_smoke(*, now_utc: datetime | None = None) -> SelfTestResult:
    """The ALWAYS-ON gating teeth smoke: 3 poisons, one per detector family.

    Any missed poison OR any false FAIL on the clean twin ⇒ ``TEETH_FAILED``
    (exit 2, real-lake verdicts withheld).
    """
    import tempfile

    from brutex.acceptance.checks import movers as movers_check
    from brutex.acceptance.checks import tf as tf_check
    from brutex.acceptance.checks import unique as unique_check
    from brutex.acceptance.independent import independent_spot_row, read_1m_bars

    start = _time.perf_counter()
    now = now_utc or datetime.now(UTC)
    days = twin_sessions(now)
    outcomes: list[PoisonOutcome] = []
    with tempfile.TemporaryDirectory(prefix="brutex-acceptance-selftest-") as tmp:
        root = Path(tmp) / "twin"
        build_twin_lake(root, days=days)
        ctx_clean = _twin_context(root, now_utc=now)
        false_fails = sum(
            1
            for check in (unique_check.run(ctx_clean), tf_check.run(ctx_clean))
            if _check_fails(check)
        )
        # S1 — duplicate row: the composed uniqueness census must FAIL it.
        poison_duplicate_row(root, symbol="NIFTY", day=days[1])
        s1 = _check_fails(unique_check.run(_twin_context(root, now_utc=now)))
        outcomes.append(PoisonOutcome("dup_row", s1, "unique.census must FAIL a planted dup"))
        # S2 — 5m HIGH x10: the composed tf freshness must FAIL it.
        poison_tf_high_x10(root, symbol="BANKNIFTY", day=days[0])
        s2 = _check_fails(tf_check.run(_twin_context(root, now_utc=now)))
        outcomes.append(PoisonOutcome("tf_high_x10", s2, "tf.intraday must FAIL a scaled bar"))
        # S3 — one perturbed movers cell via the comparator injection seam.
        bars = read_1m_bars(root / "bars" / "NSE" / "INDEX" / "NIFTY")
        ind_row = independent_spot_row("NIFTY", bars, day=days[1], cutoff_minute=None)
        if ind_row is None:  # pragma: no cover — the twin always stores day-2 bars
            raise RuntimeError("selftest twin lost its day-2 bars")
        from brutex.acceptance.independent import format_inr_2dp

        page_row = {
            "symbol": "NIFTY",
            "ltp": ind_row.ltp_display,
            "volume": ind_row.volume + 1,  # the ONE planted perturbation
            "value_cr": ind_row.value_cr_display,
            "pct_change": None if ind_row.pct is None else f"{float(ind_row.pct):.10f}",
            "change": None if ind_row.chng is None else format_inr_2dp(ind_row.chng),
        }
        s3 = bool(movers_check.compare_spot_cells(page_row, ind_row))
        outcomes.append(
            PoisonOutcome("movers_cell", s3, "compare_spot_cells must flag a perturbed cell")
        )
    detected = sum(1 for o in outcomes if o.detected)
    verdict = (
        TeethVerdict.TEETH_PROVEN
        if detected == SMOKE_POISONS and false_fails == 0
        else TeethVerdict.TEETH_FAILED
    )
    result = SelfTestResult(
        mode="smoke",
        poisons_planted=SMOKE_POISONS,
        poisons_detected=detected,
        false_fails_on_clean=false_fails,
        verdict=verdict,
        duration_s=_time.perf_counter() - start,
        outcomes=tuple(outcomes),
    )
    _LOG.info(
        "acceptance.selftest",
        mode="smoke",
        verdict=str(verdict),
        detected=detected,
        false_fails=false_fails,
    )
    return result


# ────────────────────────────────────────────────────────────────────────
# Full battery (CI + --self-test)
# ────────────────────────────────────────────────────────────────────────


def _fresh_copy(clean: Path, dest_root: Path, name: str) -> Path:
    """Copy the clean twin lake to a fresh named sibling directory.

    Returns
    -------
    Path
        The path of the newly copied ``dest_root/name`` lake.
    """
    dest = dest_root / name
    shutil.copytree(clean, dest)
    return dest


def _seed_ledger(
    root: Path,
    *,
    day: date,
    bars: int = 31,
    bars_net_new: int | None = 0,
    empty_retries: int | None = 0,
    transient_retries: int | None = 0,
    in_pull_fetches: int = 1,
    last_attempt_at: str | None = None,
) -> None:
    """One settled SUCCESS spot row through the REAL ledger store."""
    from brutex.data.window_ledger import WindowLedger, WindowLedgerRecord
    from brutex.data.window_ledger_store import save_window_ledger

    record = WindowLedgerRecord(
        market="groww",
        security_id="NIFTY",
        interval="1minute",
        window_start=day.isoformat(),
        window_end=(day + timedelta(days=1)).isoformat(),
        status="SUCCESS",
        bars=bars,
        attempts=1,
        last_error=None,
        first_seen_at=f"{day.isoformat()}T18:00:00+00:00",
        last_attempt_at=last_attempt_at or f"{day.isoformat()}T18:00:00+00:00",
        empty_streak=0,
        in_pull_fetches=in_pull_fetches,
        bars_net_new=bars_net_new,
        empty_retries=empty_retries,
        transient_retries=transient_retries,
    )
    ledger = WindowLedger.from_rows([tuple(getattr(record, f) for f in _LEDGER_FIELDS)])
    save_window_ledger(ledger, root)


_LEDGER_FIELDS: Final[tuple[str, ...]] = (
    "market",
    "security_id",
    "interval",
    "window_start",
    "window_end",
    "status",
    "bars",
    "attempts",
    "last_error",
    "first_seen_at",
    "last_attempt_at",
    "empty_streak",
    "in_pull_fetches",
    "bars_net_new",
    "empty_retries",
    "transient_retries",
    # DEC-STREAK-PROVENANCE-001 (club-P, PR #1547) appended the provenance
    # column LAST — the mirror follows the ledger's appended-LAST law.
    "streak_provenance",
)


def run_full_battery(
    *,
    now_utc: datetime | None = None,
) -> SelfTestResult:
    """The FULL battery (20 poisons) — CI mandatory, ``--self-test`` on demand.

    The battery twin additionally carries ONE non-engine CASH symbol
    (round-4 B4-3) so the census leg and the engine leg each have a tooth of
    their OWN: poison 16 is detectable ONLY by ``unique.census`` (a
    non-engine dup — an always-PASS census sabotage is MISSED) and poison 17
    ONLY by ``unique.engine`` (an out-of-months-bound engine dup — an
    always-clean engine sabotage is MISSED).

    Per-leg tooth roster — round-5 F2 (the on-box battery is the operator's
    trust demo, so EVERY real check leg is either battery-toothed here or
    named-unit-gate-killed below; a bare "TEETH_PROVEN" is never overclaimed):

    * ``unique.census`` / ``unique.engine`` — poisons 1/4/16/17.
    * ``tf.intraday`` — poisons 2 (5m HIGH x10) + 14 (grid, via sanity).
    * ``tf.daily`` — poison 5 ``orphan_1day_stored`` (NOT-derivable path) +
      poison 18 ``tampered_1day_close`` (the VALUE-equality comparator).
    * ``pull.net_new`` / ``pull.counters`` / ``pull.presence`` — poisons 6/7/8.
    * ``rate.*`` — poisons 9 (over-cap bridge) + 10 (in-window attempt).
    * ``sanity.price`` — poisons 11/12; ``sanity.tick_grid`` — poison 14;
      ``sanity.contiguity`` — poison 13; ``sanity.bad_tick`` — poison 19
      (1-minute x10); ``sanity.session_window`` — poison 20 (pre-open bar).

    HONEST unit-gate-killed legs (NOT battery-toothed — a robust twin poison is
    genuinely impractical, so the killer is the named unit gate that drives the
    COMPOSED check against the sabotage and fails, measured):

    * ``tf.daily`` FORMING branch (``ist_date >= before``) — a reliable
      forming-day twin bar is clock- AND month-boundary-dependent (the cutoff
      is ``today`` pre-close vs ``today+1`` post-close, and a forming date can
      fall outside the ``--months`` file window), so it is unit-gate-killed by
      ``tests/unit/test_acceptance_checks_tf.py::TestTfCheck::
      test_forming_day_stored_fails`` (injected clock, deterministic).
    * ``pull.replan_zero`` (planner-vacuity guard) — a vacuous planner is a
      CODE mutation, not a data poison, so it is unit-gate-killed by
      ``tests/unit/test_acceptance_checks_pull.py::TestPullCheck::
      test_lobotomized_planner_fails_vacuous`` (a monkeypatched always-empty
      planner ⇒ FAIL) plus the positive
      ``test_settled_fixture_replan_zero_and_tail_planned``.
    * ``movers.*`` — the movers teeth (smoke S3; battery poisons 3
      ``movers_cell`` + 15 ``prevclose_hole_unstamped``) drive the COMPARATOR
      SEAM directly, never the composed ``movers_check.run`` wiring — the
      designated killer for a comparator-discard wiring sabotage is the unit
      gate ``tests/unit/test_acceptance_checks_movers.py::TestMoversRealWire::
      test_tamper_tooth_volume_cell`` (it drives the COMPOSED ``movers.run``
      against a mutated real wire and fails under the sabotage, measured),
      disclosed here rather than paying an instrument-master +
      in-process-app twin per battery run.
    """
    import tempfile

    from brutex.acceptance.checks import movers as movers_check
    from brutex.acceptance.checks import pull as pull_check
    from brutex.acceptance.checks import rate as rate_check
    from brutex.acceptance.checks import sanity as sanity_check
    from brutex.acceptance.checks import tf as tf_check
    from brutex.acceptance.checks import unique as unique_check
    from brutex.acceptance.independent import independent_spot_row, read_1m_bars

    start = _time.perf_counter()
    now = now_utc or datetime.now(UTC)
    days = twin_sessions(now)
    outcomes: list[PoisonOutcome] = []
    with tempfile.TemporaryDirectory(prefix="brutex-acceptance-battery-") as tmp:
        base = Path(tmp)
        clean = base / "clean"
        # ONE non-engine CASH symbol joins the battery twin (round-4 B4-3) —
        # poisons 16/17 need coordinates only ONE of the two unique legs sees.
        build_twin_lake(clean, days=days, stock_symbols=("RELIANCE",))
        ctx_clean = _twin_context(clean, now_utc=now)
        false_fails = sum(
            1
            for check in (
                unique_check.run(ctx_clean),
                tf_check.run(ctx_clean),
                sanity_check.run(ctx_clean),
            )
            if _check_fails(check)
        )

        def scenario(name: str, detail: str, plant_and_probe: Callable[[Path], bool]) -> None:
            root = _fresh_copy(clean, base, name)
            try:
                detected = plant_and_probe(root)
            except Exception as exc:
                detected = False
                detail = f"{detail} (detector crashed: {type(exc).__name__}: {exc})"
            outcomes.append(PoisonOutcome(name, detected, detail))

        def _unique_fails(root: Path) -> bool:
            return _check_fails(unique_check.run(_twin_context(root, now_utc=now)))

        def _tf_fails(root: Path) -> bool:
            return _check_fails(tf_check.run(_twin_context(root, now_utc=now)))

        def _sanity_fails(root: Path) -> bool:
            return _check_fails(sanity_check.run(_twin_context(root, now_utc=now)))

        def _pull_fails(root: Path) -> bool:
            return _check_fails(pull_check.run(_twin_context(root, now_utc=now)))

        # 1-2: the smoke pair, re-proven inside the battery.
        def _p1(root: Path) -> bool:
            poison_duplicate_row(root, symbol="NIFTY", day=days[1])
            return _unique_fails(root)

        scenario("dup_row", "unique must FAIL a duplicate-timestamp row", _p1)

        def _p2(root: Path) -> bool:
            poison_tf_high_x10(root, symbol="BANKNIFTY", day=days[0])
            return _tf_fails(root)

        scenario("tf_high_x10", "tf must FAIL a x10-scaled stored 5m bar", _p2)

        # 3: the movers comparator seam (module attr — the inversion tooth's hook).
        def _p3(root: Path) -> bool:
            bars = read_1m_bars(root / "bars" / "NSE" / "INDEX" / "NIFTY")
            row = independent_spot_row("NIFTY", bars, day=days[1], cutoff_minute=None)
            if row is None:  # pragma: no cover — the twin always stores day-2 bars
                raise RuntimeError("battery twin lost its day-2 bars")
            page = {
                "symbol": "NIFTY",
                "ltp": row.ltp_display,
                "volume": row.volume,
                "value_cr": "0.00" if row.value_cr_display != "0.00" else "1.00",
                "pct_change": None if row.pct is None else str(float(row.pct)),
                "change": None,
            }
            return bool(movers_check.compare_spot_cells(page, row))

        scenario("movers_cell", "comparator must flag a perturbed value_cr cell", _p3)

        # 4: a foreign-month row.
        def _p4(root: Path) -> bool:
            path = _month_file(root, "NIFTY", "1minute", days[1])
            frame = pl.read_parquet(path, glob=False)
            ts = frame.get_column("timestamp").to_list()
            ts[0] = ts[0] - timedelta(days=40)
            frame.with_columns(pl.Series("timestamp", ts)).write_parquet(path)
            return _unique_fails(root)

        scenario("foreign_month_row", "unique must FAIL a foreign-month row", _p4)

        # 5: a stored 1day bar with NO 1m backing (the not-derivable path).
        def _p5(root: Path) -> bool:
            from brutex.data.models import CandleBar
            from brutex.data.parquet_writer import ParquetWriter

            # A PAST (non-forming) IST date with no 1m bars → the compare cannot
            # re-derive it → tf.daily `divergent` ("stored bar not derivable").
            # (The FORMING branch itself is unit-gate-killed — see the docstring.)
            orphan_ist = days[0] - timedelta(days=3)
            stamp = datetime.combine(orphan_ist - timedelta(days=1), time(18, 30), tzinfo=UTC)
            ParquetWriter(lake_root=root).write(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1day",
                bars=[
                    CandleBar(
                        timestamp=stamp,
                        open=Decimal("1.00"),
                        high=Decimal("2.00"),
                        low=Decimal("0.50"),
                        close=Decimal("1.50"),
                        volume=1,
                        open_interest=None,
                    )
                ],
            )
            return _tf_fails(root)

        scenario(
            "orphan_1day_stored",
            "tf.daily must FAIL a stored 1day bar with no 1m backing (not-derivable)",
            _p5,
        )

        # 6: settled-row bars_net_new > bars.
        def _p6(root: Path) -> bool:
            _seed_ledger(root, day=days[1], bars=3, bars_net_new=7)
            return _pull_fails(root)

        scenario("net_new_gt_bars", "pull must FAIL bars_net_new > bars", _p6)

        # 7: counter-invariant violation.
        def _p7(root: Path) -> bool:
            _seed_ledger(
                root,
                day=days[1],
                empty_retries=1,
                transient_retries=1,
                in_pull_fetches=1,
            )
            return _pull_fails(root)

        scenario("counter_invariant", "pull must FAIL the retry-split invariant", _p7)

        # 8: deleted partition behind a SUCCESS row.
        def _p8(root: Path) -> bool:
            _seed_ledger(root, day=days[1])
            _month_file(root, "NIFTY", "1minute", days[1]).unlink()
            return _pull_fails(root)

        scenario("deleted_partition", "pull must FAIL a SUCCESS row with no backing file", _p8)

        # 9: an over-cap rate-bridge snapshot (real publisher).
        def _p9(root: Path) -> bool:
            from brutex.data.rate_bridge import publish_rate_snapshot

            publish_rate_snapshot(
                root,
                effective_rps=9.5,
                current_rps=9.5,
                recent_minute=120,
                recent_429=0,
                ceiling_per_min=500,
                wall_time=now.timestamp(),
                monotonic=0.0,
            )
            return _check_fails(rate_check.run(_twin_context(root, now_utc=now)))

        scenario("over_cap_bridge", "rate must FAIL a persisted over-cap snapshot", _p9)

        # 10: an in-market-hours vendor attempt stamp — on a trading day AT or
        # AFTER the guard's landing date (earlier stamps are honestly excluded).
        def _p10(root: Path) -> bool:
            from brutex.calendars.nse import is_trading_day

            probe = max(days[1], rate_check.MARKET_HOURS_GUARD_LANDED)
            while not is_trading_day(probe):  # weekends walk to the next session
                probe += timedelta(days=1)
            in_window = f"{probe.isoformat()}T04:30:00+00:00"  # 10:00 IST
            _seed_ledger(root, day=days[1], last_attempt_at=in_window)
            return _check_fails(rate_check.run(_twin_context(root, now_utc=now)))

        scenario("in_window_attempt", "rate must FAIL an 08:45-15:35 IST attempt", _p10)

        # 11-12: price-sanity poisons.
        def _price_poison(root: Path, column: str, value: float) -> bool:
            path = _month_file(root, "NIFTY", "1minute", days[1])
            frame = pl.read_parquet(path, glob=False)
            values = frame.get_column(column).to_list()
            values[0] = value
            frame.with_columns(pl.Series(column, values)).write_parquet(path)
            return _sanity_fails(root)

        scenario(
            "negative_low",
            "sanity must FAIL a negative low",
            lambda r: _price_poison(r, "low", -5.0),
        )
        scenario(
            "high_lt_low",
            "sanity must FAIL high < low",
            lambda r: _price_poison(r, "high", 0.01),
        )

        # 13: a 7-session interior hole.
        def _p13(root: Path) -> bool:
            from brutex.data.parquet_writer import ParquetWriter

            far_day = days[0] - timedelta(days=16)
            ParquetWriter(lake_root=root).write(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                bars=_twin_bars(far_day, base=90.0),
            )
            return _sanity_fails(root)

        scenario("interior_hole", "sanity must FAIL a >5-session interior hole", _p13)

        # 14: a 3-decimal price (tick-grid law).
        scenario(
            "three_decimal_price",
            "sanity must FAIL a 3-decimal price",
            lambda r: _price_poison(r, "close", 100.005),
        )

        # 15: prev-close hole without its stamp (comparator seam).
        def _p15(root: Path) -> bool:
            bars = read_1m_bars(root / "bars" / "NSE" / "INDEX" / "NIFTY")
            row = independent_spot_row("NIFTY", bars, day=days[1], cutoff_minute=None)
            if row is None:  # pragma: no cover — the twin always stores day-2 bars
                raise RuntimeError("battery twin lost its day-2 bars")
            from dataclasses import replace

            holed = replace(row, prev_close_date=days[0] - timedelta(days=10))
            page = {"symbol": "NIFTY", "prev_close_contiguous": None}
            return movers_check.contiguity_stamp_mismatch(page, holed, day=days[1]) is not None

        scenario("prevclose_hole_unstamped", "comparator must flag a missing hole stamp", _p15)

        # 16: a dup on a NON-engine sampled symbol — census-ONLY detectable
        # (round-4 B4-3: an unconditional-PASS census sabotage previously
        # survived the battery because every dup poison sat on an ENGINE
        # coordinate the engine leg caught anyway).
        def _p16(root: Path) -> bool:
            poison_duplicate_row(root, symbol="RELIANCE", day=days[1], segment="CASH")
            check = unique_check.run(_twin_context(root, now_utc=now))
            subs = {s.id: s for s in check.sub_checks}
            return (
                subs["unique.census"].verdict is Verdict.FAIL
                and subs["unique.engine"].verdict is Verdict.PASS
            )

        scenario(
            "census_nonengine_dup",
            "unique.census ALONE must FAIL a non-engine dup (engine leg stays PASS)",
            _p16,
        )

        # 17: a dup in an OLD month on an ENGINE coordinate — engine-ONLY
        # detectable (the month-bounded census cannot see it; the full-history
        # engine leg must — an always-clean engine sabotage is MISSED here).
        def _p17(root: Path) -> bool:
            from brutex.data.parquet_writer import ParquetWriter

            old_day = days[0] - timedelta(days=240)
            ParquetWriter(lake_root=root).write(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                bars=_twin_bars(old_day, base=95.0),
            )
            poison_duplicate_row(root, symbol="NIFTY", day=old_day)
            check = unique_check.run(_twin_context(root, now_utc=now))
            subs = {s.id: s for s in check.sub_checks}
            return (
                subs["unique.engine"].verdict is Verdict.FAIL
                and subs["unique.census"].verdict is Verdict.PASS
            )

        scenario(
            "engine_oldmonth_dup",
            "unique.engine ALONE must FAIL an out-of-months-bound engine dup",
            _p17,
        )

        # 18: a value-corrupt DERIVABLE stored 1day bar — tf.daily VALUE tooth
        # (round-5 F2: the 1day value-equality comparator was unit-gate-only).
        def _p18(root: Path) -> bool:
            poison_1day_value(root, symbol="NIFTY", day=days[0])
            check = tf_check.run(_twin_context(root, now_utc=now))
            daily = next(s for s in check.sub_checks if s.id == "tf.daily")
            return daily.verdict is Verdict.FAIL

        scenario(
            "tampered_1day_close",
            "tf.daily ALONE must FAIL a value-corrupt derivable 1day bar",
            _p18,
        )

        # 19: a 1-minute close x10 — sanity.bad_tick tooth (round-5 F2).
        def _p19(root: Path) -> bool:
            poison_1m_bad_tick_x10(root, symbol="NIFTY", day=days[1])
            check = sanity_check.run(_twin_context(root, now_utc=now))
            sub = next(s for s in check.sub_checks if s.id == "sanity.bad_tick")
            return sub.verdict is Verdict.FAIL

        scenario(
            "bad_tick_x10_1m",
            "sanity.bad_tick ALONE must FAIL a x10-scaled 1-minute close",
            _p19,
        )

        # 20: a pre-open bar — sanity.session_window tooth (round-5 F2).
        def _p20(root: Path) -> bool:
            poison_preopen_bar(root, symbol="NIFTY", day=days[1])
            check = sanity_check.run(_twin_context(root, now_utc=now))
            sub = next(s for s in check.sub_checks if s.id == "sanity.session_window")
            return sub.verdict is Verdict.FAIL

        scenario(
            "preopen_bar",
            "sanity.session_window ALONE must FAIL an 08:00-IST pre-open bar",
            _p20,
        )

    detected = sum(1 for o in outcomes if o.detected)
    verdict = (
        TeethVerdict.TEETH_PROVEN
        if detected == len(outcomes) and false_fails == 0
        else TeethVerdict.TEETH_FAILED
    )
    result = SelfTestResult(
        mode="full",
        poisons_planted=len(outcomes),
        poisons_detected=detected,
        false_fails_on_clean=false_fails,
        verdict=verdict,
        duration_s=_time.perf_counter() - start,
        outcomes=tuple(outcomes),
    )
    _LOG.info(
        "acceptance.selftest",
        mode="full",
        verdict=str(verdict),
        detected=detected,
        planted=len(outcomes),
        false_fails=false_fails,
    )
    return result


def render_battery_table(result: SelfTestResult) -> str:
    """The per-poison detection table (``--self-test-only`` output)."""
    lines = [f"self-test ({result.mode}): {result.verdict}"]
    for outcome in result.outcomes:
        mark = "DETECTED" if outcome.detected else "MISSED"
        lines.append(f"  {outcome.name:<24} {mark:<9} {outcome.detail}")
    lines.append(
        f"  planted={result.poisons_planted} detected={result.poisons_detected} "
        f"false_fails_on_clean={result.false_fails_on_clean} "
        f"duration={result.duration_s:.1f}s"
    )
    return "\n".join(lines) + "\n"
