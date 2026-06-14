"""End-to-end tests for the ``python -m brutex.live`` CLI + capture loop.

Every command handler is driven through :func:`brutex.live.__main__.main` with an
injected :class:`~brutex.live.__main__.LiveDeps` carrying a FAKE feed — no
``growwapi``, no network, no real time. Covers
:mod:`brutex.live.capture` and :mod:`brutex.live.__main__`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from brutex.live import __main__ as cli
from brutex.live.__main__ import LiveDeps, main
from brutex.live.capture import TickCapture, _utc_now
from brutex.live.serde import write_candles
from brutex.live.store import TickStore

_OPEN_UTC = datetime(2026, 6, 15, 3, 45, tzinfo=UTC)


def _bar(close: str, *, h: int, m: int):
    from brutex.data.models import CandleBar

    price = Decimal(close)
    return CandleBar(
        timestamp=datetime(2026, 6, 15, h, m, tzinfo=UTC),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=0,
        open_interest=None,
    )


class _FakeFeed:
    """Records the subscription + replays canned snapshots on each callback."""

    def __init__(self, snapshots: list[dict[str, Any]]) -> None:
        self._snapshots = snapshots
        self._idx = 0
        self.subscribed: list[Any] | None = None
        self._cb = None

    def subscribe_index_value(self, instruments, on_data_received) -> None:
        self.subscribed = list(instruments)
        self._cb = on_data_received

    subscribe_ltp = subscribe_index_value

    def get_index_value(self) -> dict[str, Any]:
        snap = self._snapshots[min(self._idx, len(self._snapshots) - 1)]
        self._idx += 1
        return snap

    def get_ltp(self) -> dict[str, Any]:
        return self.get_index_value()

    def fire(self) -> None:
        assert self._cb is not None
        self._cb()


def _clock_seq(step_s: float = 1.0):
    state = {"t": _OPEN_UTC}

    def clock() -> datetime:
        now = state["t"]
        state["t"] = now + timedelta(seconds=step_s)
        return now

    return clock


def _deps_with(feed: _FakeFeed, *, monotonic_vals: list[float]) -> LiveDeps:
    it = iter(monotonic_vals)
    return LiveDeps(
        feed_builder=lambda: feed,
        instrument_resolver=lambda symbols, kind: [
            {"exchange": "NSE", "segment": "IDX", "exchange_token": "26000"}
        ],
        clock=_clock_seq(),
        sleep=lambda _s: feed.fire(),
        monotonic=lambda: next(it),
    )


# ───────────────────────────── capture loop ──────────────────────


def test_tickcapture_records_and_counts(tmp_path) -> None:
    feed = _FakeFeed([{"NIFTY": 100.0}, {"NIFTY": 101.0}])
    store = TickStore.open(tmp_path / "t.jsonl")
    captured: list = []
    capture = TickCapture(
        store=store,
        subscribe=lambda cb: feed.subscribe_index_value([], on_data_received=cb),
        read_snapshot=feed.get_index_value,
        clock=_clock_seq(),
        on_record=captured.append,
    )
    n = capture.run_for(
        1.0, sleep=lambda _s: feed.fire(), monotonic=iter([0.0, 0.4, 0.8, 1.2]).__next__
    )
    store.close()
    assert n == 2 and capture.count == 2
    assert len(captured) == 2
    assert [r.snapshot for r in TickStore.read(tmp_path / "t.jsonl")] == [
        {"NIFTY": 100.0},
        {"NIFTY": 101.0},
    ]


def test_utc_now_is_tz_aware() -> None:
    assert _utc_now().tzinfo is not None


# ───────────────────────────── capture cmd ───────────────────────


def test_cmd_capture_index_and_ltp(tmp_path) -> None:
    out = tmp_path / "ticks.jsonl"
    feed = _FakeFeed([{"NSE:IDX:NIFTY": 22500.0}, {"NSE:IDX:NIFTY": 22501.0}])
    deps = _deps_with(feed, monotonic_vals=[0.0, 0.4, 0.8, 1.2])
    rc = main(["capture", "--out", str(out), "--seconds", "1", "--feed", "index"], deps=deps)
    assert rc == 0
    assert feed.subscribed == [{"exchange": "NSE", "segment": "IDX", "exchange_token": "26000"}]
    assert len(TickStore.read(out)) == 2

    feed2 = _FakeFeed([{"NSE:NIFTY": 1.0}, {"NSE:NIFTY": 2.0}])
    deps2 = _deps_with(feed2, monotonic_vals=[0.0, 0.4, 0.8, 1.2])
    assert (
        main(
            ["capture", "--out", str(out), "--seconds", "1", "--feed", "ltp", "--resume"],
            deps=deps2,
        )
        == 0
    )


# ───────────────────────────── cadence cmd ───────────────────────


def test_cmd_cadence(tmp_path) -> None:
    path = tmp_path / "t.jsonl"
    with TickStore.open(path) as store:
        for i in range(3):
            store.append({"N": i}, recv_ts=_OPEN_UTC + timedelta(milliseconds=50 * i))
    assert main(["cadence", "--in", str(path)], deps=_noop_deps()) == 0


# ───────────────────────────── build cmd ─────────────────────────


def test_cmd_build_with_and_without_date(tmp_path) -> None:
    src = tmp_path / "t.jsonl"
    with TickStore.open(src) as store:
        store.append({"NIFTY": 22500.0}, recv_ts=datetime(2026, 6, 15, 3, 45, 10, tzinfo=UTC))
        store.append({"NIFTY": 22510.0}, recv_ts=datetime(2026, 6, 15, 3, 45, 50, tzinfo=UTC))
    out = tmp_path / "candles.json"
    assert main(["build", "--in", str(src), "--out", str(out)], deps=_noop_deps()) == 0
    from brutex.live.serde import read_candles

    candles = read_candles(out)
    assert candles["NIFTY"][0].open == Decimal("22500.0")
    # with an explicit IST session date.
    assert (
        main(
            ["build", "--in", str(src), "--out", str(out), "--date", "2026-06-15"],
            deps=_noop_deps(),
        )
        == 0
    )


# ───────────────────────────── parity cmd ────────────────────────


def test_cmd_parity_identical_and_divergent(tmp_path) -> None:
    live = tmp_path / "live.json"
    backtest = tmp_path / "bt.json"
    report = tmp_path / "report.md"
    write_candles(live, {"NIFTY": [_bar("100", h=3, m=45)]})
    write_candles(backtest, {"NIFTY": [_bar("100", h=3, m=45)]})
    rc = main(
        ["parity", "--live", str(live), "--backtest", str(backtest), "--out", str(report)],
        deps=_noop_deps(),
    )
    assert rc == 0 and "IDENTICAL" in report.read_text(encoding="utf-8")

    write_candles(backtest, {"NIFTY": [_bar("999", h=3, m=45)]})
    assert (
        main(
            ["parity", "--live", str(live), "--backtest", str(backtest), "--out", str(report)],
            deps=_noop_deps(),
        )
        == 1
    )


# ───────────────────────── probe-connections cmd ─────────────────


def test_cmd_probe_connections(tmp_path) -> None:
    calls = {"n": 0}

    def failing_builder() -> object:
        calls["n"] += 1
        if calls["n"] >= 3:
            raise RuntimeError("ceiling")
        return object()

    deps = LiveDeps(
        feed_builder=failing_builder,
        instrument_resolver=lambda s, k: [],
    )
    assert main(["probe-connections", "--max", "5"], deps=deps) == 0


# ──────────────────────── helpers + real-deps branch ─────────────


def _noop_deps() -> LiveDeps:
    return LiveDeps(
        feed_builder=lambda: _FakeFeed([{}]),
        instrument_resolver=lambda s, k: [],
    )


def test_make_subscribe_and_reader_dispatch() -> None:
    feed = _FakeFeed([{"x": 1.0}])
    cli._make_subscribe(feed, [{"exchange": "NSE"}], "ltp")(lambda: None)
    assert feed.subscribed == [{"exchange": "NSE"}]
    assert cli._make_snapshot_reader(feed, "ltp") == feed.get_ltp
    assert cli._make_snapshot_reader(feed, "index") == feed.get_index_value


def test_main_defaults_to_real_deps(monkeypatch, tmp_path) -> None:
    """The ``deps is None`` branch resolves the real bundle (patched here)."""
    monkeypatch.setattr(cli, "_real_deps", _noop_deps)
    path = tmp_path / "t.jsonl"
    with TickStore.open(path) as store:
        store.append({"N": 1}, recv_ts=_OPEN_UTC)
    assert main(["cadence", "--in", str(path)]) == 0


def test_main_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        main([], deps=_noop_deps())
