"""Unit tests for the Dhan SPOT pull core (DEC-DHAN-PULL-SURFACE-001, ADR-0022).

Fully fake-driven — an injected fetch adapter + a temp lake root + an injected
``materialize`` seam + a fake AWS client — so :mod:`brutex.dashboard.dhan_pull` is
100% line+branch with no boto3 / dhanhq / network. The production credential + SDK
build (``build_dhan_spot_adapter``) is the ONLY ``# pragma: no cover`` (it needs real
AWS + the SDK); the production seam (``default_dhan_spot_pull``) is driven here via its
injectable ``adapter_factory`` + ``materialize`` seams (both prebuild-gate branches).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from brutex.adapters.dhan.constants import (
    DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT,
    DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT,
)
from brutex.adapters.dhan.errors import DhanBadRequestError, DhanThrottleError
from brutex.adapters.dhan.ingest import FetchOhlcv
from brutex.dashboard.dhan_pull import (
    DhanPrebuildOutcome,
    DhanPrebuildTarget,
    DhanPullReport,
    DhanPullRequest,
    default_dhan_spot_pull,
    dhan_credentials_error,
    dhan_prebuild_targets,
    resolve_dhan_spot_security_ids,
    run_dhan_prebuild,
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
    # Bound to dhan_root + SPOT kind + dense INDEX flag.
    assert calls[0]["lake_root"] == Path("/lake/feeds/dhan")
    assert calls[0]["segment"] == "INDEX"
    assert calls[0]["dense_symbol"] is True
    assert calls[0]["include_daily"] is True


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
    # TRUE gate branch: a clean spot phase (failed_windows == 0) → prebuild runs.
    adapter = _FakeAdapter(_all_indices_script([_bar()]))

    def _factory(**kwargs: object) -> FetchOhlcv:
        _ = kwargs  # security_ids=...
        return adapter

    request = DhanPullRequest(
        lake_root=tmp_path, start=date(2022, 6, 2), end=date(2022, 6, 3), dry_run=False
    )
    report = default_dhan_spot_pull(
        request, adapter_factory=_factory, materialize=_fake_materialize
    )
    assert report.spot_phase_ok is True
    assert report.prebuild is not None
    assert report.prebuild.ok is True
    # every resolved index materialised its derived TFs (3 + 1)
    assert set(report.prebuild.materialized) == set(resolve_dhan_spot_security_ids().security_ids)
    assert all(v == 4 for v in report.prebuild.materialized.values())
    assert report.dry_run is False


def test_default_dhan_spot_pull_failed_phase_skips_prebuild(tmp_path: Path) -> None:
    # FALSE gate branch: a FAILED window (terminal) leaves prebuild None (gated).
    script = _all_indices_script([_bar()])
    script["NIFTY"] = [DhanBadRequestError("813: bad id")]  # terminal → FAILED window
    adapter = _FakeAdapter(script)

    materialize_calls: list[object] = []

    def _factory(**kwargs: object) -> FetchOhlcv:
        _ = kwargs
        return adapter

    def _spy_materialize(**kwargs: object) -> MaterializeResult:
        materialize_calls.append(kwargs)
        return MaterializeResult(written={}, skipped=())

    # A REAL (wet) pull — a dry_run would short-circuit before fetching, so this gate
    # branch is exercised with dry_run=False.
    request = DhanPullRequest(
        lake_root=tmp_path, start=date(2022, 6, 2), end=date(2022, 6, 3), dry_run=False
    )
    report = default_dhan_spot_pull(request, adapter_factory=_factory, materialize=_spy_materialize)
    assert report.spot_phase_ok is False
    assert report.prebuild is None
    assert materialize_calls == []  # prebuild never ran
    assert report.dry_run is False


def test_default_dhan_spot_pull_dry_run_builds_no_adapter_and_calls_no_vendor(
    tmp_path: Path,
) -> None:
    """HONEST dry-run: no adapter build, no vendor call, no prebuild, no write — Golden Rule #1."""
    built: list[FetchOhlcv] = []
    adapter = _FakeAdapter(_all_indices_script([_bar()]))
    materialize_calls: list[object] = []

    def _spy_factory(**kwargs: object) -> FetchOhlcv:
        _ = kwargs  # security_ids=...
        built.append(adapter)
        return adapter

    def _spy_materialize(**kwargs: object) -> MaterializeResult:
        materialize_calls.append(kwargs)
        return MaterializeResult(written={}, skipped=())

    request = DhanPullRequest(
        lake_root=tmp_path, start=date(2022, 6, 2), end=date(2022, 6, 3), dry_run=True
    )
    report = default_dhan_spot_pull(
        request, adapter_factory=_spy_factory, materialize=_spy_materialize
    )
    # The adapter_factory + materialize seams were NEVER invoked, and NO vendor call
    # was issued (the fake adapter's per-symbol call log stays empty).
    assert built == []
    assert adapter.calls == []
    assert materialize_calls == []
    # An HONEST report: dry_run true, zero bars/failures, prebuild gated off (None).
    assert report.dry_run is True
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
