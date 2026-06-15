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


def test_tickcapture_on_tick_accepts_groww_meta_arg(tmp_path) -> None:
    """REGRESSION: Groww invokes the callback WITH a ``meta`` arg (07-feed.md).

    A 0-arg ``_on_tick`` crashed EVERY live tick at the open with
    ``_on_tick() takes 1 positional argument but 2 were given``. The callback
    must accept + ignore the meta (it pulls the full snapshot itself). This
    fires the callback BOTH with the meta (Groww) and with no arg (the 0-arg
    convention) and asserts both capture a record. The two reads return DIFFERENT
    values so the delta prune (DEC-LIVE-DELTA-CAPTURE-001) writes BOTH — the
    point under test is the callback signature, not the delta.
    """
    snapshots = iter([{"NIFTY": 100.0}, {"NIFTY": 101.0}])
    store = TickStore.open(tmp_path / "t.jsonl")
    capture = TickCapture(
        store=store,
        subscribe=lambda _cb: None,
        read_snapshot=lambda: next(snapshots),
        clock=_clock_seq(),
    )
    capture._on_tick({"feed_type": "index_value", "exchange": "NSE"})  # Groww 1-arg call
    capture._on_tick()  # the 0-arg convention still works
    store.close()
    assert capture.count == 2


def _delta_capture(store: TickStore, snapshots: list[dict[str, Any]], clock) -> TickCapture:
    """A `TickCapture` fed *snapshots* in order, firing one callback per snapshot."""
    feed = _FakeFeed(snapshots)
    capture = TickCapture(
        store=store,
        subscribe=lambda cb: feed.subscribe_index_value([], on_data_received=cb),
        read_snapshot=feed.get_index_value,
        clock=clock,
    )
    capture.start()
    for _ in snapshots:
        feed.fire()
    return capture


def test_tickcapture_delta_writes_only_changes(tmp_path) -> None:
    """A sequence with unchanged + partial-change callbacks writes ONLY the deltas.

    DEC-LIVE-DELTA-CAPTURE-001: an identical callback writes NOTHING (no record,
    counter frozen); a partial change writes only the moved instruments.
    """
    path = tmp_path / "t.jsonl"
    snapshots = [
        {"NIFTY": 100.0, "BANKNIFTY": 200.0},  # seq 0 — first, both kept
        {"NIFTY": 100.0, "BANKNIFTY": 200.0},  # identical — written NOTHING
        {"NIFTY": 101.0, "BANKNIFTY": 200.0},  # NIFTY moved — only NIFTY
        {"NIFTY": 101.0, "BANKNIFTY": 201.0},  # BANKNIFTY moved — only BANKNIFTY
    ]
    store = TickStore.open(path)
    capture = _delta_capture(store, snapshots, _clock_seq())
    store.close()

    written = [r.snapshot for r in TickStore.read(path)]
    assert written == [
        {"NIFTY": 100.0, "BANKNIFTY": 200.0},
        {"NIFTY": 101.0},
        {"BANKNIFTY": 201.0},
    ]
    assert capture.count == 3  # the identical callback did NOT advance the counter
    assert [r.seq for r in TickStore.read(path)] == [0, 1, 2]  # seq only on writes


def test_tickcapture_delta_all_identical_writes_nothing(tmp_path) -> None:
    """If every callback after the first is identical, only the first is written."""
    path = tmp_path / "t.jsonl"
    store = TickStore.open(path)
    capture = _delta_capture(store, [{"N": 1.0}, {"N": 1.0}, {"N": 1.0}], _clock_seq())
    store.close()
    assert capture.count == 1
    assert [r.snapshot for r in TickStore.read(path)] == [{"N": 1.0}]


def test_golden_delta_candles_identical_to_full(tmp_path) -> None:
    """GOLDEN no-data-loss: delta-built candles == full-snapshot-built candles.

    A representative sequence of FULL snapshots is (a) captured the OLD way (every
    snapshot written verbatim) and (b) captured the NEW delta way; the 1-minute
    candles built from each stream MUST be byte-identical — proof the delta loses
    no tick (DEC-LIVE-DELTA-CAPTURE-001).
    """
    from brutex.live.candle_builder import build_minute_candles
    from brutex.live.tick import iter_ticks

    # A sequence spanning two session minutes, with repeats, partial moves, and a
    # value that rises then FALLS within a minute (so high/low/open/close all
    # matter) and a value that goes quiet (unchanged) before the minute ends.
    base = datetime(2026, 6, 15, 3, 45, 5, tzinfo=UTC)  # 09:15:05 IST
    timeline: list[tuple[datetime, dict[str, Any]]] = [
        (base, {"NIFTY": 100.0, "BANKNIFTY": 200.0}),
        (base.replace(second=15), {"NIFTY": 100.0, "BANKNIFTY": 200.0}),  # both quiet
        (base.replace(second=25), {"NIFTY": 105.0, "BANKNIFTY": 200.0}),  # NIFTY up
        (base.replace(second=35), {"NIFTY": 99.0, "BANKNIFTY": 200.0}),  # NIFTY down (low)
        (base.replace(second=45), {"NIFTY": 102.0, "BANKNIFTY": 200.0}),  # NIFTY close
        (base.replace(second=55), {"NIFTY": 102.0, "BANKNIFTY": 200.0}),  # all quiet
        # next minute — BANKNIFTY finally moves, NIFTY stays put across the boundary
        (base.replace(minute=46, second=5), {"NIFTY": 102.0, "BANKNIFTY": 207.0}),
        (base.replace(minute=46, second=20), {"NIFTY": 102.0, "BANKNIFTY": 203.0}),
        (base.replace(minute=46, second=40), {"NIFTY": 102.0, "BANKNIFTY": 203.0}),
    ]
    snapshots = [snap for _ts, snap in timeline]
    stamps = [ts for ts, _snap in timeline]

    # (a) OLD behaviour — write every full snapshot verbatim.
    full_path = tmp_path / "full.jsonl"
    with TickStore.open(full_path) as full_store:
        for ts, snap in timeline:
            full_store.append(snap, recv_ts=ts)
    full_candles = build_minute_candles(iter_ticks(TickStore.read(full_path)))

    # (b) NEW behaviour — the real TickCapture delta path, SAME recv_ts per call.
    delta_path = tmp_path / "delta.jsonl"
    stamp_iter = iter(stamps)
    store = TickStore.open(delta_path)
    _delta_capture(store, snapshots, lambda: next(stamp_iter))
    store.close()
    delta_candles = build_minute_candles(iter_ticks(TickStore.read(delta_path)))

    assert delta_candles == full_candles
    # sanity: the delta really WAS smaller (it dropped redundant records).
    assert len(TickStore.read(delta_path)) < len(TickStore.read(full_path))


@pytest.mark.parametrize("seed", range(12))
def test_golden_delta_candles_identical_randomized(tmp_path, seed: int) -> None:
    """GOLDEN (randomized): across many random tick streams, delta candles == full.

    Generates a pseudo-random sequence of full snapshots over several session
    minutes for three instruments — each callback independently either repeats or
    jumps a price — and asserts the delta-stream candles are byte-identical to the
    full-stream candles for EVERY instrument. This stresses the per-minute
    keyframe invariant (instruments quiet across minute boundaries) broadly, not
    just the one hand-built case (DEC-LIVE-DELTA-CAPTURE-001).
    """
    import random

    from brutex.live.candle_builder import build_minute_candles
    from brutex.live.tick import iter_ticks

    rng = random.Random(seed)  # deterministic test fixture, not crypto
    instruments = ["NIFTY", "BANKNIFTY", "SENSEX"]
    prices = {name: float(rng.randint(100, 500)) for name in instruments}
    start = datetime(2026, 6, 15, 3, 45, 2, tzinfo=UTC)  # inside the session window

    timeline: list[tuple[datetime, dict[str, Any]]] = []
    ts = start
    for _ in range(rng.randint(40, 90)):
        for name in instruments:
            if rng.random() < 0.4:  # 40% of callbacks move this instrument
                prices[name] = float(rng.randint(100, 500))
        timeline.append((ts, dict(prices)))
        ts += timedelta(seconds=rng.choice([3, 7, 11, 19, 31]))  # cross minute lines

    snapshots = [snap for _ts, snap in timeline]
    stamps = [t for t, _snap in timeline]

    full_path = tmp_path / "full.jsonl"
    with TickStore.open(full_path) as full_store:
        for t, snap in timeline:
            full_store.append(snap, recv_ts=t)
    full_candles = build_minute_candles(iter_ticks(TickStore.read(full_path)))

    delta_path = tmp_path / "delta.jsonl"
    stamp_iter = iter(stamps)
    with TickStore.open(delta_path) as store:
        _delta_capture(store, snapshots, lambda: next(stamp_iter))
    delta_candles = build_minute_candles(iter_ticks(TickStore.read(delta_path)))

    assert delta_candles == full_candles


def test_tickcapture_emits_heartbeats(tmp_path) -> None:
    """A long-enough window emits a progress heartbeat (tracking/monitoring)."""
    feed = _FakeFeed([{"N": 1.0}, {"N": 2.0}])
    store = TickStore.open(tmp_path / "t.jsonl")
    capture = TickCapture(
        store=store,
        subscribe=lambda cb: feed.subscribe_index_value([], on_data_received=cb),
        read_snapshot=feed.get_index_value,
        clock=_clock_seq(),
    )
    # start=0.0; first loop now=0.5 ≥ next_beat 0.1 → one heartbeat; then break at 1.5.
    monotonic = iter([0.0, 0.5, 1.5]).__next__
    n = capture.run_for(
        1.0, sleep=lambda _s: feed.fire(), monotonic=monotonic, heartbeat_interval=0.1
    )
    store.close()
    assert n >= 1 and capture.heartbeats == 1


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


def test_cmd_lake_persists_ticks_and_candles(tmp_path) -> None:
    from brutex.live.lake import read_live_candles, read_live_ticks

    src = tmp_path / "t.jsonl"
    with TickStore.open(src) as store:
        store.append(
            {"NSE:IDX:NIFTY": 22500.0},
            recv_ts=datetime(2026, 6, 15, 3, 45, 10, tzinfo=UTC),
            recv_ns=1,
        )
    lake_root = tmp_path / "lake"
    rc = main(
        ["lake", "--in", str(src), "--lake-root", str(lake_root), "--run-id", "r1"],
        deps=_noop_deps(),
    )
    assert rc == 0
    assert read_live_ticks(lake_root).shape[0] == 1
    assert read_live_candles(lake_root).shape[0] == 1


def test_cmd_verify_all_ticking_returns_0(tmp_path) -> None:
    feed = _FakeFeed([{"NSE:IDX:NIFTY": 22500.0}, {"NSE:IDX:NIFTY": 22501.0}])
    deps = _deps_with(feed, monotonic_vals=[0.0, 0.4, 0.8, 1.2])
    rc = main(
        [
            "verify",
            "--out",
            str(tmp_path / "v.jsonl"),
            "--report",
            str(tmp_path / "v.md"),
            "--seconds",
            "1",
            "--feed",
            "index",
        ],
        deps=deps,
    )
    assert rc == 0  # requested 1, observed 1 → all ticking
    assert "subscription self-test" in (tmp_path / "v.md").read_text(encoding="utf-8")


def test_cmd_verify_some_silent_returns_1(tmp_path) -> None:
    feed = _FakeFeed([{"NSE:IDX:NIFTY": 1.0}, {"NSE:IDX:NIFTY": 2.0}])
    it = iter([0.0, 0.4, 0.8, 1.2])
    deps = LiveDeps(
        feed_builder=lambda: feed,
        instrument_resolver=lambda s, k: [
            {"exchange": "NSE", "segment": "IDX", "exchange_token": "1"},
            {"exchange": "NSE", "segment": "IDX", "exchange_token": "2"},
        ],
        clock=_clock_seq(),
        sleep=lambda _s: feed.fire(),
        monotonic=lambda: next(it),
    )
    rc = main(
        [
            "verify",
            "--out",
            str(tmp_path / "v.jsonl"),
            "--report",
            str(tmp_path / "v.md"),
            "--seconds",
            "1",
        ],
        deps=deps,
    )
    assert rc == 1  # requested 2, observed 1 → some silent


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


# ─────────────────── dual-feed routing (--feed both) ──────────────


def _idx(token: str) -> dict[str, Any]:
    return {"exchange": "NSE", "segment": "IDX", "exchange_token": token, "instrument_type": "IDX"}


def _eq(token: str) -> dict[str, Any]:
    return {"exchange": "NSE", "segment": "CASH", "exchange_token": token, "instrument_type": "EQ"}


def test_classify_feed_instruments_routes_and_strips_tag() -> None:
    index_rows, ltp_rows = cli.classify_feed_instruments([_idx("26000"), _eq("100"), _eq("200")])
    # indices → index-value list; equities → ltp list; the routing tag is dropped.
    assert index_rows == [{"exchange": "NSE", "segment": "IDX", "exchange_token": "26000"}]
    assert ltp_rows == [
        {"exchange": "NSE", "segment": "CASH", "exchange_token": "100"},
        {"exchange": "NSE", "segment": "CASH", "exchange_token": "200"},
    ]
    assert all("instrument_type" not in row for row in (*index_rows, *ltp_rows))


def test_classify_feed_instruments_defaults_untagged_to_ltp() -> None:
    # A row with no ``instrument_type`` is treated as a tradable (ltp) instrument.
    index_rows, ltp_rows = cli.classify_feed_instruments(
        [{"exchange": "NSE", "segment": "CASH", "exchange_token": "9"}]
    )
    assert index_rows == []
    assert ltp_rows == [{"exchange": "NSE", "segment": "CASH", "exchange_token": "9"}]


def test_merge_snapshots_unions_and_overrides() -> None:
    assert cli.merge_snapshots({"A": 1.0}, {"B": 2.0}) == {"A": 1.0, "B": 2.0}
    assert cli.merge_snapshots({"A": 1.0}, {"A": 2.0}) == {"A": 2.0}  # last writer wins
    assert cli.merge_snapshots() == {}


class _DualFakeFeed:
    """Records the index-value AND ltp subscriptions separately (for --feed both)."""

    def __init__(self) -> None:
        self.index_sub: list[Any] | None = None
        self.ltp_sub: list[Any] | None = None
        self.cbs: list[Any] = []

    def subscribe_index_value(self, instruments, on_data_received) -> None:
        self.index_sub = list(instruments)
        self.cbs.append(on_data_received)

    def subscribe_ltp(self, instruments, on_data_received) -> None:
        self.ltp_sub = list(instruments)
        self.cbs.append(on_data_received)

    def get_index_value(self) -> dict[str, Any]:
        return {"NSE:IDX:NIFTY": 100.0}

    def get_ltp(self) -> dict[str, Any]:
        return {"NSE:EQ:RELIANCE": 200.0}


def test_make_subscribe_both_routes_each_group_to_its_feed() -> None:
    feed = _DualFakeFeed()
    cb = lambda: None  # noqa: E731 - tiny sentinel callback
    cli._make_subscribe(feed, [_idx("26000"), _eq("100")], "both")(cb)
    assert feed.index_sub == [{"exchange": "NSE", "segment": "IDX", "exchange_token": "26000"}]
    assert feed.ltp_sub == [{"exchange": "NSE", "segment": "CASH", "exchange_token": "100"}]
    assert feed.cbs == [cb, cb]  # the SAME callback feeds both subscriptions


def test_make_subscribe_both_skips_empty_groups() -> None:
    only_idx = _DualFakeFeed()
    cli._make_subscribe(only_idx, [_idx("26000")], "both")(lambda: None)
    assert only_idx.index_sub == [{"exchange": "NSE", "segment": "IDX", "exchange_token": "26000"}]
    assert only_idx.ltp_sub is None  # no equities → ltp feed never touched

    only_eq = _DualFakeFeed()
    cli._make_subscribe(only_eq, [_eq("100")], "both")(lambda: None)
    assert only_eq.index_sub is None  # no indices → index feed never touched
    assert only_eq.ltp_sub == [{"exchange": "NSE", "segment": "CASH", "exchange_token": "100"}]

    empty = _DualFakeFeed()
    cli._make_subscribe(empty, [], "both")(lambda: None)
    assert empty.index_sub is None and empty.ltp_sub is None


def test_make_snapshot_reader_both_merges_index_and_ltp() -> None:
    feed = _DualFakeFeed()
    reader = cli._make_snapshot_reader(feed, "both")
    assert reader() == {"NSE:IDX:NIFTY": 100.0, "NSE:EQ:RELIANCE": 200.0}


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
