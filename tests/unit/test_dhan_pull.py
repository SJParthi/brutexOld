"""Unit tests for the Dhan SPOT pull core (DEC-DHAN-PULL-SURFACE-001, ADR-0022).

Fully fake-driven — an injected fetch adapter + a fake daily client + a temp lake
root + an injected ``materialize`` seam + a fake AWS client — so
:mod:`brutex.dashboard.dhan_pull` is 100% line+branch with no boto3 / dhanhq /
network. The production credential + SDK build (``build_dhan_spot_clients`` + its
thin ``build_dhan_spot_adapter`` delegator) is the ONLY ``# pragma: no cover``
(it needs real AWS + the SDK); the production seam (``default_dhan_spot_pull``)
is driven here via its injectable ``clients_factory`` + ``materialize`` seams —
every chain branch (dry-run · daily-gate STOP · spot-fail prebuild gate · clean
full chain · observer kill-switch arms; DEC-DHAN-NATIVE-DAILY-001, implementing
`DEC-SCOPE-MINIMAL-CORE-001`).
"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import brutex.dashboard.dhan_pull as dhan_pull_module
import pytest
from brutex.adapters.dhan.constants import (
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT,
    DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT,
)
from brutex.adapters.dhan.daily_consistency import DHAN_DAILY_CONSISTENCY_ENV
from brutex.adapters.dhan.errors import DhanBadRequestError, DhanThrottleError
from brutex.adapters.dhan.ingest import DhanIngestReport, FetchOhlcv
from brutex.dashboard.dhan_pull import (
    DhanPrebuildOutcome,
    DhanPrebuildTarget,
    DhanPullReport,
    DhanPullRequest,
    DhanSpotClients,
    default_dhan_spot_pull,
    dhan_credentials_error,
    dhan_prebuild_targets,
    resolve_dhan_spot_security_ids,
    run_dhan_prebuild,
    run_dhan_spot_daily_pull,
    run_dhan_spot_pull,
)
from brutex.data.feed_root import feed_lake_root
from brutex.data.models import CandleBar
from brutex.data.timeframe_materialize import MaterializeResult

from tests.fakes.fake_aws_client import FakeAwsClient


def _bar(minute: int = 45) -> CandleBar:
    return CandleBar(
        timestamp=datetime(2022, 6, 2, 3, minute, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=0,
        open_interest=None,
    )


class _FakeAdapter:
    """A fake fetch adapter driven by a per-symbol scripted response queue."""

    def __init__(self, script: dict[str, list[object]]) -> None:
        self._script = script
        self.calls: list[str] = []

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        exchange: Any,
        interval: str,
        start: date,
        end: date,
    ) -> list[CandleBar]:
        _ = (exchange, interval, start, end)
        self.calls.append(symbol)
        response = self._script[symbol].pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


class _EmptyDaily:
    """A fake daily client that always answers the honest empty payload (clean stage)."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str]] = []

    def daily_candles(self, *, security_id: int, from_date: str, to_date: str) -> dict[str, object]:
        self.calls.append((security_id, from_date, to_date))
        return {}


class _RaisingDaily:
    """A fake daily client whose every call fails terminally-per-window (non-retryable)."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str]] = []

    def daily_candles(self, *, security_id: int, from_date: str, to_date: str) -> dict[str, object]:
        self.calls.append((security_id, from_date, to_date))
        raise DhanBadRequestError("813: bad id")


def _clients_factory(
    adapter: FetchOhlcv, daily: object | None = None
) -> tuple[Any, list[dict[str, object]]]:
    """Build a ``clients_factory`` seam over the fake pair; returns (factory, call-log)."""
    calls: list[dict[str, object]] = []
    daily_client = daily if daily is not None else _EmptyDaily()

    def _factory(**kwargs: object) -> DhanSpotClients:
        calls.append(dict(kwargs))
        return DhanSpotClients(fetch=adapter, daily=daily_client)  # type: ignore[arg-type]

    return _factory, calls


def _no_filter(bars: list[CandleBar]) -> SimpleNamespace:
    return SimpleNamespace(kept=list(bars))


# ─────────────────────────────────────────────────────────────────────────────
# DhanPrebuildOutcome / DhanPullReport
# ─────────────────────────────────────────────────────────────────────────────


def test_prebuild_outcome_ok_and_to_dict() -> None:
    clean = DhanPrebuildOutcome(materialized={"NIFTY": 20}, failed={})
    assert clean.ok is True
    assert clean.to_dict() == {"materialized": {"NIFTY": 20}, "failed": {}, "ok": True}

    dirty = DhanPrebuildOutcome(materialized={}, failed={"SENSEX": "OSError"})
    assert dirty.ok is False
    assert dirty.to_dict()["ok"] is False


def test_pull_report_phase_ok_and_to_dict_without_prebuild(tmp_path: Path) -> None:
    adapter = _FakeAdapter({"NIFTY": [[_bar()]]})
    spot = run_dhan_spot_pull(
        lake_root=tmp_path,
        adapter=adapter,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 2),
        end=date(2022, 6, 3),
        today=date(2022, 7, 1),  # frozen — the rolling floor must never eat these fixed 2022 ranges
        symbols=["NIFTY"],
        session_filter=_no_filter,
    )
    report = DhanPullReport(spot=spot, prebuild=None, dry_run=True)
    assert report.spot_phase_ok is True
    payload = report.to_dict()
    assert payload["feed"] == "dhan"
    assert payload["dry_run"] is True
    assert payload["prebuild"] is None
    assert payload["spot"]["total_bars"] == 1  # type: ignore[index]
    assert payload["spot"]["resolved_symbols"] == ["NIFTY"]  # type: ignore[index]


def test_pull_report_phase_ok_false_when_failed_windows(tmp_path: Path) -> None:
    # A terminal (non-retryable) failure leaves the window FAILED → phase not ok.
    adapter = _FakeAdapter({"NIFTY": [DhanThrottleError("a"), DhanThrottleError("b")]})
    spot = run_dhan_spot_pull(
        lake_root=tmp_path,
        adapter=adapter,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 2),
        end=date(2022, 6, 3),
        today=date(2022, 7, 1),  # frozen — the rolling floor must never eat these fixed 2022 ranges
        symbols=["NIFTY"],
        session_filter=_no_filter,
        max_rounds=2,
    )
    outcome = DhanPrebuildOutcome(materialized={"NIFTY": 5}, failed={})
    report = DhanPullReport(spot=spot, prebuild=outcome, dry_run=False)
    assert report.spot_phase_ok is False
    payload = report.to_dict()
    assert payload["spot_phase_ok"] is False
    assert payload["prebuild"] == {"materialized": {"NIFTY": 5}, "failed": {}, "ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# securityId resolution
# ─────────────────────────────────────────────────────────────────────────────


def test_resolve_dhan_spot_security_ids_uses_hardcoded_map() -> None:
    resolution = resolve_dhan_spot_security_ids()
    assert resolution.security_ids["NIFTY"] == 13
    assert resolution.security_ids["SENSEX"] == 51
    assert resolution.unresolved == ()


# ─────────────────────────────────────────────────────────────────────────────
# run_dhan_spot_pull — writes into feeds/dhan/
# ─────────────────────────────────────────────────────────────────────────────


def test_run_dhan_spot_pull_writes_into_feeds_dhan(tmp_path: Path) -> None:
    adapter = _FakeAdapter({"NIFTY": [[_bar(45), _bar(46)]]})
    report = run_dhan_spot_pull(
        lake_root=tmp_path,
        adapter=adapter,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 2),
        end=date(2022, 6, 3),
        today=date(2022, 7, 1),  # frozen — the rolling floor must never eat these fixed 2022 ranges
        symbols=["NIFTY"],
        session_filter=_no_filter,
        code_commit="deadbeef",
    )
    assert report.total_bars == 2
    assert report.failed_windows == 0
    assert adapter.calls == ["NIFTY"]
    # The bars land under the NESTED feeds/dhan/ tree (Groww bytes untouched).
    dhan_bars = tmp_path / "feeds" / "dhan" / "bars" / "NSE" / "INDEX" / "NIFTY"
    assert dhan_bars.exists()
    assert not (tmp_path / "bars").exists()  # nothing at the TOP root
    # The FeedAuditSink hash-chain landed for the non-default feed (Golden Rule #8).
    assert (tmp_path / "feeds" / "dhan" / ".audit" / "ingest-chain.jsonl").exists()


# ─────────────────────────────────────────────────────────────────────────────
# prebuild
# ─────────────────────────────────────────────────────────────────────────────


def test_dhan_prebuild_targets_exchange_attribution() -> None:
    targets = dhan_prebuild_targets({"NIFTY": 13, "SENSEX": 51})
    assert DhanPrebuildTarget(exchange="NSE", symbol="NIFTY") in targets
    assert DhanPrebuildTarget(exchange="BSE", symbol="SENSEX") in targets


def test_run_dhan_prebuild_success_and_crash_guard() -> None:
    calls: list[dict[str, object]] = []

    def _fake_materialize(**kwargs: object) -> MaterializeResult:
        calls.append(kwargs)
        if kwargs["symbol"] == "SENSEX":
            raise OSError("disk blip")
        return MaterializeResult(written={"5minute": 10, "1day": 1}, skipped=())

    outcome = run_dhan_prebuild(
        dhan_root=Path("/lake/feeds/dhan"),
        targets=[
            DhanPrebuildTarget(exchange="NSE", symbol="NIFTY"),
            DhanPrebuildTarget(exchange="BSE", symbol="SENSEX"),
        ],
        materialize=_fake_materialize,
    )
    assert outcome.materialized == {"NIFTY": 11}
    assert outcome.failed == {"SENSEX": "OSError"}
    assert outcome.ok is False
    # Bound to dhan_root + SPOT kind + dense INDEX flag. THE LAW
    # (DEC-DHAN-NATIVE-DAILY-001, implementing `DEC-SCOPE-MINIMAL-CORE-001`):
    # include_daily is HARDCODED False at the Dhan materialise call site — the
    # native-daily 1day partition is vendor truth, never re-derived from 1-minute.
    assert calls[0]["lake_root"] == Path("/lake/feeds/dhan")
    assert calls[0]["segment"] == "INDEX"
    assert calls[0]["dense_symbol"] is True
    assert calls[0]["include_daily"] is False
    assert calls[1]["include_daily"] is False  # every target, not just the first


def test_run_dhan_prebuild_dhan_root_is_nested() -> None:
    # The route binds feed_lake_root(top, feed="dhan") == <top>/feeds/dhan.
    assert feed_lake_root(Path("/lake"), feed="dhan") == Path("/lake/feeds/dhan")


# ─────────────────────────────────────────────────────────────────────────────
# credential gate
# ─────────────────────────────────────────────────────────────────────────────


def test_dhan_credentials_error_none_when_resolved() -> None:
    client = FakeAwsClient(
        parameters={
            DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT: "1100000000",
            DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT: "jwt.token.value",
        }
    )
    assert dhan_credentials_error(client=client) is None


def test_dhan_credentials_error_text_when_absent() -> None:
    client = FakeAwsClient(
        param_not_found=frozenset(
            {DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT, DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT}
        )
    )
    detail = dhan_credentials_error(client=client)
    assert detail is not None
    assert "AWS Parameter Store" in detail


# ─────────────────────────────────────────────────────────────────────────────
# request record
# ─────────────────────────────────────────────────────────────────────────────


def test_dhan_pull_request_record() -> None:
    req = DhanPullRequest(
        lake_root=Path("/lake"), start=date(2022, 6, 1), end=date(2022, 6, 30), dry_run=False
    )
    assert req.lake_root == Path("/lake")
    assert req.dry_run is False
    assert req.today is None  # defaulted — a headless caller resolves once in the spine


# ─────────────────────────────────────────────────────────────────────────────
# default_dhan_spot_pull — the injectable production seam (both gate branches)
# ─────────────────────────────────────────────────────────────────────────────


def _all_indices_script(response: object) -> dict[str, list[object]]:
    """One scripted *response* window per resolved index (the full spot index set)."""
    return {sym: [response] for sym in resolve_dhan_spot_security_ids().security_ids}


def _fake_materialize(**kwargs: object) -> MaterializeResult:
    _ = kwargs
    return MaterializeResult(written={"5minute": 3, "1day": 1}, skipped=())


def test_default_dhan_spot_pull_clean_runs_prebuild(tmp_path: Path) -> None:
    # TRUE gate branch: a clean daily stage + a clean spot phase → prebuild runs.
    adapter = _FakeAdapter(_all_indices_script([_bar()]))
    daily = _EmptyDaily()
    factory, factory_calls = _clients_factory(adapter, daily)

    request = DhanPullRequest(
        lake_root=tmp_path,
        start=date(2022, 6, 2),
        end=date(2022, 6, 3),
        dry_run=False,
        today=date(2022, 7, 1),
    )
    report = default_dhan_spot_pull(request, clients_factory=factory, materialize=_fake_materialize)
    assert len(factory_calls) == 1  # ONE client-pair build serves BOTH stages
    assert report.spot_phase_ok is True
    # DEC-DHAN-NATIVE-DAILY-001: the native-daily stage ran FIRST and cleanly.
    assert report.daily is not None
    assert report.daily_phase_ok is True
    assert report.stopped_after is None
    assert len(daily.calls) == len(resolve_dhan_spot_security_ids().security_ids)
    assert report.prebuild is not None
    assert report.prebuild.ok is True
    # every resolved index materialised its derived TFs (3 intraday + 1 in the fake)
    assert set(report.prebuild.materialized) == set(resolve_dhan_spot_security_ids().security_ids)
    assert all(v == 4 for v in report.prebuild.materialized.values())
    assert report.dry_run is False
    payload = report.to_dict()
    assert payload["daily_phase_ok"] is True
    assert payload["stopped_after"] is None
    assert payload["daily"] is not None


def test_default_dhan_spot_pull_failed_phase_skips_prebuild(tmp_path: Path) -> None:
    # FALSE gate branch: a FAILED window (terminal) leaves prebuild None (gated).
    script = _all_indices_script([_bar()])
    script["NIFTY"] = [DhanBadRequestError("813: bad id")]  # terminal → FAILED window
    adapter = _FakeAdapter(script)

    materialize_calls: list[object] = []
    factory, _factory_calls = _clients_factory(adapter)

    def _spy_materialize(**kwargs: object) -> MaterializeResult:
        materialize_calls.append(kwargs)
        return MaterializeResult(written={}, skipped=())

    # A REAL (wet) pull — a dry_run would short-circuit before fetching, so this gate
    # branch is exercised with dry_run=False.
    request = DhanPullRequest(
        lake_root=tmp_path,
        start=date(2022, 6, 2),
        end=date(2022, 6, 3),
        dry_run=False,
        today=date(2022, 7, 1),
    )
    report = default_dhan_spot_pull(request, clients_factory=factory, materialize=_spy_materialize)
    assert report.spot_phase_ok is False
    assert report.stopped_after is None  # the chain RAN — the 1-minute stage itself failed
    assert report.daily is not None
    assert report.daily_phase_ok is True
    assert report.prebuild is None
    assert materialize_calls == []  # prebuild never ran
    assert report.dry_run is False


def test_default_dhan_spot_pull_dry_run_builds_no_adapter_and_calls_no_vendor(
    tmp_path: Path,
) -> None:
    """HONEST dry-run: no client build, no vendor call, no prebuild, no write — Golden Rule #1."""
    adapter = _FakeAdapter(_all_indices_script([_bar()]))
    daily = _EmptyDaily()
    factory, factory_calls = _clients_factory(adapter, daily)
    materialize_calls: list[object] = []

    def _spy_materialize(**kwargs: object) -> MaterializeResult:
        materialize_calls.append(kwargs)
        return MaterializeResult(written={}, skipped=())

    request = DhanPullRequest(
        lake_root=tmp_path,
        start=date(2022, 6, 2),
        end=date(2022, 6, 3),
        dry_run=True,
        today=date(2022, 7, 1),
    )
    report = default_dhan_spot_pull(request, clients_factory=factory, materialize=_spy_materialize)
    # The clients_factory + materialize seams were NEVER invoked, and NO vendor call
    # was issued (BOTH fakes' call logs stay empty — daily AND 1-minute).
    assert factory_calls == []
    assert adapter.calls == []
    assert daily.calls == []
    assert materialize_calls == []
    # An HONEST report: dry_run true, zero bars/failures, prebuild gated off (None),
    # daily None (the native-daily stage was never planned — DEC-DHAN-NATIVE-DAILY-001).
    assert report.dry_run is True
    assert report.daily is None
    assert report.daily_phase_ok is True  # vacuous — no daily stage ran
    assert report.stopped_after is None
    assert report.spot.total_bars == 0
    assert report.spot.failed_windows == 0
    assert report.spot.outcomes == ()
    assert report.prebuild is None
    # The resolved / unresolved symbols carry the honest "would pull" plan.
    resolution = resolve_dhan_spot_security_ids()
    assert set(report.spot.resolved_symbols) == set(resolution.security_ids)
    assert report.spot.unresolved_symbols == resolution.unresolved
    # Nothing written to the lake (no feeds/ tree, no top bars/).
    assert not (tmp_path / "feeds").exists()
    assert not (tmp_path / "bars").exists()
    # The route payload is honest too.
    payload = report.to_dict()
    assert payload["dry_run"] is True
    assert payload["spot"]["total_bars"] == 0  # type: ignore[index]
    assert payload["prebuild"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Rolling-floor report surface + retention (DEC-DHAN-ROLLING-FLOOR-001).
# Frozen today = 2026-07-14 → floor 2021-07-21 (today−5y + 7d).
# ─────────────────────────────────────────────────────────────────────────────

_FROZEN_TODAY = date(2026, 7, 14)
_FROZEN_FLOOR = date(2021, 7, 21)


def _floor_bar(ts: datetime) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=0,
        open_interest=None,
    )


class _EmptyAdapter:
    def fetch_ohlcv(self, **kwargs: Any) -> list[CandleBar]:
        _ = kwargs
        return []


def test_clamped_pull_report_note_and_emit_on_clamp_keys(tmp_path: Path) -> None:
    report = run_dhan_spot_pull(
        lake_root=tmp_path,
        adapter=_EmptyAdapter(),
        security_ids={"NIFTY": 13},
        symbols=["NIFTY"],
        start=date(2020, 1, 1),  # the blank-date default — before the floor
        end=date(2021, 8, 21),
        session_filter=_no_filter,
        today=_FROZEN_TODAY,
    )
    pull_report = DhanPullReport(spot=report, prebuild=None, dry_run=False)
    assert pull_report.floor_clamped is True
    assert pull_report.note == (
        "dhan spot pipeline — start clamped 2020-01-01 → 2021-07-21 (Dhan rolling 5y floor)"
    )
    payload = pull_report.to_dict()
    assert payload["note"] == pull_report.note
    assert payload["floor"] == _FROZEN_FLOOR.isoformat()
    assert payload["effective_start"] == _FROZEN_FLOOR.isoformat()


def test_unclamped_pull_report_note_plain_no_floor_keys(tmp_path: Path) -> None:
    report = run_dhan_spot_pull(
        lake_root=tmp_path,
        adapter=_EmptyAdapter(),
        security_ids={"NIFTY": 13},
        symbols=["NIFTY"],
        start=date(2024, 1, 1),
        end=date(2024, 2, 1),
        session_filter=_no_filter,
        today=_FROZEN_TODAY,
    )
    pull_report = DhanPullReport(spot=report, prebuild=None, dry_run=False)
    assert pull_report.floor_clamped is False
    assert pull_report.note == "dhan spot pipeline"  # byte-identical, no clamp suffix
    payload = pull_report.to_dict()
    assert "floor" not in payload  # EMIT-ON-CLAMP: absent on an un-clamped pull
    assert "effective_start" not in payload


def test_entirely_pre_floor_report_note(tmp_path: Path) -> None:
    report = run_dhan_spot_pull(
        lake_root=tmp_path,
        adapter=_EmptyAdapter(),
        security_ids={"NIFTY": 13},
        symbols=["NIFTY"],
        start=date(2020, 1, 1),
        end=date(2020, 6, 1),  # wholly before the floor → zero windows
        session_filter=_no_filter,
        today=_FROZEN_TODAY,
    )
    pull_report = DhanPullReport(spot=report, prebuild=None, dry_run=False)
    assert pull_report.note == (
        "nothing to pull (requested range wholly before the Dhan rolling floor 2021-07-21)"
    )
    payload = pull_report.to_dict()
    assert payload["floor"] == _FROZEN_FLOOR.isoformat()
    assert "effective_start" not in payload  # nothing was planned


def test_dry_run_report_note_stays_plain_headline(tmp_path: Path) -> None:
    # A dry-run short-circuits BEFORE planning: no floor stamps, plain headline
    # (it echoes the REQUESTED range only — documented, design §1).
    request = DhanPullRequest(
        lake_root=tmp_path,
        start=date(2020, 1, 1),
        end=date(2021, 8, 21),
        dry_run=True,
        today=_FROZEN_TODAY,
    )
    report = default_dhan_spot_pull(request)
    assert report.spot.floor is None
    assert report.note == "dhan spot pipeline"
    assert "floor" not in report.to_dict()


def test_retention_pre_floor_bars_byte_untouched_by_clamped_repull(tmp_path: Path) -> None:
    """The retention property (DEC-DHAN-ROLLING-FLOOR-001, stated verbatim in the
    DEC): the floor governs VENDOR REQUESTS ONLY — bars already in ``feeds/dhan/``
    that age past the current floor are NEVER deleted (keep-first, append-only; no
    reaper exists or is authorized). A clamped re-pull leaves the pre-floor
    partition file BYTE-IDENTICAL.
    """
    from brutex.feeds.binding import build_feed_sink
    from brutex.feeds.dhan_binding import build_dhan_binding

    # Pre-seed a PRE-floor bar (2021-06-01 — before the 2021-07-21 floor) through
    # the production feed sink.
    sink = build_feed_sink(lake_root=tmp_path, binding=build_dhan_binding())
    pre_floor_ts = datetime(2021, 6, 1, 3, 45, tzinfo=UTC)
    sink.append(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1minute",
        bars=[_floor_bar(pre_floor_ts)],
    )
    dhan_root = feed_lake_root(tmp_path, feed="dhan")
    pre_floor_file = (
        dhan_root / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2021" / "06.parquet"
    )
    assert pre_floor_file.exists()
    before = pre_floor_file.read_bytes()

    # A CLAMPED re-pull over the full range (requests start AT the floor; the
    # adapter serves one post-floor bar).
    post_floor_ts = datetime(2021, 8, 2, 3, 45, tzinfo=UTC)

    class _OneBarAdapter:
        def fetch_ohlcv(self, **kwargs: Any) -> list[CandleBar]:
            start = kwargs["start"]
            assert start >= _FROZEN_FLOOR  # the tripwire holds here too
            if kwargs["start"] <= post_floor_ts.date() < kwargs["end"]:
                return [_floor_bar(post_floor_ts)]
            return []

    report = run_dhan_spot_pull(
        lake_root=tmp_path,
        adapter=_OneBarAdapter(),
        security_ids={"NIFTY": 13},
        symbols=["NIFTY"],
        start=date(2020, 1, 1),
        end=date(2021, 9, 1),
        session_filter=_no_filter,
        today=_FROZEN_TODAY,
    )
    assert report.total_bars == 1  # the post-floor bar landed
    assert pre_floor_file.read_bytes() == before  # pre-floor bytes UNTOUCHED


# ─────────────────────────────────────────────────────────────────────────────
# NATIVE-daily chain (DEC-DHAN-NATIVE-DAILY-001, implementing
# `DEC-SCOPE-MINIMAL-CORE-001`): daily FIRST · the FAILED-daily gate ·
# the structural include_daily=False pin · the consistency observer wiring.
# ─────────────────────────────────────────────────────────────────────────────


def _daily_payload(*epochs: int, close: float = 100.5) -> dict[str, object]:
    """A vendor-shaped daily column payload (one candle per epoch second)."""
    n = len(epochs)
    return {
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [close] * n,
        "volume": [0] * n,
        "timestamp": list(epochs),
    }


class _ScriptedDaily:
    """A fake daily client answering ONE fixed payload for every call."""

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.calls: list[tuple[int, str, str]] = []

    def daily_candles(self, *, security_id: int, from_date: str, to_date: str) -> dict[str, object]:
        self.calls.append((security_id, from_date, to_date))
        return self._payload


#: 2022-06-02 03:45 UTC == 09:15 IST on a regular NSE trading Thursday.
_EPOCH_2022_06_02 = int(datetime(2022, 6, 2, 3, 45, tzinfo=UTC).timestamp())


def test_run_dhan_spot_daily_pull_writes_native_1day(tmp_path: Path) -> None:
    daily = _ScriptedDaily(_daily_payload(_EPOCH_2022_06_02))
    report = run_dhan_spot_daily_pull(
        lake_root=tmp_path,
        client=daily,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 2),
        end=date(2022, 6, 3),
        symbols=["NIFTY"],
        today=date(2022, 7, 1),
        code_commit="deadbeef",
    )
    assert report.total_bars == 1
    assert report.failed_windows == 0
    assert daily.calls == [(13, "2022-06-02", "2022-06-03")]
    # The NATIVE 1day bar lands under the NESTED feeds/dhan/ tree, 1day partition.
    daily_file = (
        tmp_path
        / "feeds"
        / "dhan"
        / "bars"
        / "NSE"
        / "INDEX"
        / "NIFTY"
        / "1day"
        / "2022"
        / "06.parquet"
    )
    assert daily_file.exists()
    assert not (tmp_path / "bars").exists()  # nothing at the TOP root
    # The FeedAuditSink hash-chain landed for the daily stage too (Golden Rule #8).
    assert (tmp_path / "feeds" / "dhan" / ".audit" / "ingest-chain.jsonl").exists()


def test_default_dhan_spot_pull_daily_failed_stops_chain(tmp_path: Path) -> None:
    """THE GATE: a FAILED daily stage stops the chain BEFORE any 1-minute vendor call."""
    adapter = _FakeAdapter(_all_indices_script([_bar()]))
    daily = _RaisingDaily()
    factory, _factory_calls = _clients_factory(adapter, daily)
    materialize_calls: list[object] = []

    def _spy_materialize(**kwargs: object) -> MaterializeResult:
        materialize_calls.append(kwargs)
        return MaterializeResult(written={}, skipped=())

    request = DhanPullRequest(
        lake_root=tmp_path,
        start=date(2022, 6, 2),
        end=date(2022, 6, 3),
        dry_run=False,
        today=date(2022, 7, 1),
    )
    report = default_dhan_spot_pull(request, clients_factory=factory, materialize=_spy_materialize)
    assert report.stopped_after == "daily"
    assert report.daily is not None
    assert report.daily.failed_windows > 0
    assert report.daily_phase_ok is False
    # The 1-minute stage was NEVER attempted (zero fetch calls) and the empty
    # stopped report must never read as a clean 1-minute phase (Golden Rule #1).
    assert adapter.calls == []
    assert report.spot_phase_ok is False
    assert report.spot.total_bars == 0
    assert report.spot.outcomes == ()
    assert report.prebuild is None
    assert materialize_calls == []
    assert "STOPPED after the native-daily stage" in report.note
    assert "not attempted" in report.note
    payload = report.to_dict()
    assert payload["stopped_after"] == "daily"
    assert payload["daily_phase_ok"] is False
    assert payload["spot_phase_ok"] is False
    assert payload["prebuild"] is None
    assert payload["daily"] is not None
    assert payload["note"] == report.note


def test_pull_report_stopped_note_defensive_daily_none() -> None:
    # The `daily is None` arm of the stopped-note wording (defensive totality —
    # the production gate always carries the daily report).
    empty = DhanIngestReport(outcomes=(), resolved_symbols=(), unresolved_symbols=())
    report = DhanPullReport(
        spot=empty, prebuild=None, dry_run=False, daily=None, stopped_after="daily"
    )
    assert report.spot_phase_ok is False
    assert "0 failed daily window(s)" in report.note


def test_default_chain_runs_daily_before_minute(tmp_path: Path) -> None:
    """DEC-DHAN-NATIVE-DAILY-001 ordering law: EVERY daily call precedes EVERY 1-minute call."""
    events: list[str] = []

    class _OrderedDaily(_EmptyDaily):
        def daily_candles(self, **kwargs: Any) -> dict[str, object]:
            events.append("daily")
            return super().daily_candles(**kwargs)

    class _OrderedAdapter(_FakeAdapter):
        def fetch_ohlcv(self, **kwargs: Any) -> list[CandleBar]:
            events.append("spot")
            return super().fetch_ohlcv(**kwargs)

    adapter = _OrderedAdapter(_all_indices_script([_bar()]))
    factory, _calls = _clients_factory(adapter, _OrderedDaily())
    request = DhanPullRequest(
        lake_root=tmp_path,
        start=date(2022, 6, 2),
        end=date(2022, 6, 3),
        dry_run=False,
        today=date(2022, 7, 1),
    )
    report = default_dhan_spot_pull(request, clients_factory=factory, materialize=_fake_materialize)
    n = len(resolve_dhan_spot_security_ids().security_ids)
    assert events == ["daily"] * n + ["spot"] * n
    assert report.stopped_after is None


def test_run_dhan_prebuild_include_daily_structurally_false() -> None:
    """Wiring pin (DEC-DHAN-NATIVE-DAILY-001): ``include_daily=False`` is HARDCODED.

    The flag must be a literal ``False`` at the Dhan materialise call site and
    must NOT be caller-flippable (no ``include_daily`` parameter) — the native
    1day partition is vendor truth; the Groww law
    `DEC-1MINUTE-ONLY-DERIVE-1DAY-001` (derive 1day from 1m) stays Groww-only.
    """
    source = inspect.getsource(run_dhan_prebuild)
    assert "include_daily=False" in source
    assert "include_daily=True" not in source
    assert "include_daily" not in inspect.signature(run_dhan_prebuild).parameters


def test_prebuild_never_overwrites_native_daily_bytes(tmp_path: Path) -> None:
    """NEGATIVE pin (DEC-DHAN-NATIVE-DAILY-001): the REAL prebuild leaves the native
    1day partition BYTE-IDENTICAL while genuinely writing the intraday TFs — no
    derived-1day path executes on the Dhan chain."""
    # 1) the NATIVE daily stage writes a 1day bar whose close (555.5) DIFFERS from
    #    the 1-minute closes below — a leaked 1m→1day derivation would change bytes.
    daily = _ScriptedDaily(_daily_payload(_EPOCH_2022_06_02, close=555.5))
    run_dhan_spot_daily_pull(
        lake_root=tmp_path,
        client=daily,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 2),
        end=date(2022, 6, 3),
        symbols=["NIFTY"],
        today=date(2022, 7, 1),
    )
    dhan_root = feed_lake_root(tmp_path, feed="dhan")
    daily_file = dhan_root / "bars" / "NSE" / "INDEX" / "NIFTY" / "1day" / "2022" / "06.parquet"
    assert daily_file.exists()
    native = daily_file.read_bytes()
    # 2) the 1-minute stage lands real intraday bars beside it.
    adapter = _FakeAdapter({"NIFTY": [[_bar(45), _bar(46)]]})
    run_dhan_spot_pull(
        lake_root=tmp_path,
        adapter=adapter,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 2),
        end=date(2022, 6, 3),
        symbols=["NIFTY"],
        session_filter=_no_filter,
        today=date(2022, 7, 1),
    )
    # 3) the REAL materialise (the production default seam) derives INTRADAY only.
    outcome = run_dhan_prebuild(
        dhan_root=dhan_root,
        targets=[DhanPrebuildTarget(exchange="NSE", symbol="NIFTY")],
    )
    assert outcome.ok is True
    assert outcome.materialized["NIFTY"] > 0  # intraday TFs genuinely written
    assert (dhan_root / "bars" / "NSE" / "INDEX" / "NIFTY" / "5minute").exists()
    assert daily_file.read_bytes() == native  # native 1day bytes UNTOUCHED


# ─────────────────────────────────────────────────────────────────────────────
# Consistency-observer wiring (DEC-DHAN-NATIVE-DAILY-001 item 7).
# ─────────────────────────────────────────────────────────────────────────────


def test_consistency_junk_env_fails_loud_before_client_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DHAN_DAILY_CONSISTENCY_ENV, "junk")
    adapter = _FakeAdapter(_all_indices_script([_bar()]))
    factory, factory_calls = _clients_factory(adapter)
    request = DhanPullRequest(
        lake_root=tmp_path,
        start=date(2022, 6, 2),
        end=date(2022, 6, 3),
        dry_run=False,
        today=date(2022, 7, 1),
    )
    with pytest.raises(ValueError, match=DHAN_DAILY_CONSISTENCY_ENV):
        default_dhan_spot_pull(request, clients_factory=factory, materialize=_fake_materialize)
    # Resolved ONCE at entry — the junk failed BEFORE any client build / vendor call.
    assert factory_calls == []
    assert adapter.calls == []


def test_observer_wired_on_clean_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DHAN_DAILY_CONSISTENCY_ENV, raising=False)  # default ON
    observed: list[dict[str, object]] = []

    def _spy_observer(**kwargs: object) -> None:
        observed.append(dict(kwargs))

    monkeypatch.setattr(dhan_pull_module, "observe_daily_minute_consistency_guarded", _spy_observer)
    adapter = _FakeAdapter(_all_indices_script([_bar()]))
    factory, _calls = _clients_factory(adapter)
    request = DhanPullRequest(
        lake_root=tmp_path,
        start=date(2022, 6, 2),
        end=date(2022, 6, 3),
        dry_run=False,
        today=date(2022, 7, 1),
    )
    default_dhan_spot_pull(request, clients_factory=factory, materialize=_fake_materialize)
    assert len(observed) == 1
    call = observed[0]
    assert call["dhan_root"] == feed_lake_root(tmp_path, feed="dhan")
    assert call["start"] == date(2022, 6, 2)
    assert call["end"] == date(2022, 6, 3)
    targets = call["targets"]
    assert isinstance(targets, list)
    assert ("NSE", "NIFTY") in targets


def test_observer_skipped_when_switch_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DHAN_DAILY_CONSISTENCY_ENV, "off")
    observed: list[dict[str, object]] = []

    def _spy_observer(**kwargs: object) -> None:
        observed.append(dict(kwargs))  # pragma: no cover — must never run

    monkeypatch.setattr(dhan_pull_module, "observe_daily_minute_consistency_guarded", _spy_observer)
    adapter = _FakeAdapter(_all_indices_script([_bar()]))
    factory, _calls = _clients_factory(adapter)
    request = DhanPullRequest(
        lake_root=tmp_path,
        start=date(2022, 6, 2),
        end=date(2022, 6, 3),
        dry_run=False,
        today=date(2022, 7, 1),
    )
    report = default_dhan_spot_pull(request, clients_factory=factory, materialize=_fake_materialize)
    assert observed == []  # the kill switch genuinely skips the observer
    assert report.spot_phase_ok is True
