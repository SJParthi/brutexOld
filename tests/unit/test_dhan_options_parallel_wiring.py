"""Unit tests for the parallel options-ingest wiring (DEC-DHAN-PARALLEL-INGEST-001).

Deterministic, synchronous-fake tests for the per-SERIES fan-out in
:func:`brutex.adapters.dhan.option_universe_ingest.run_dhan_options_ingest`
(DEC-DHAN-OPTION-SERIES-FANOUT-001):

* the injected ``run_parallel`` seam dispatches one job PER SERIES (so a single
  underlying's whole fan saturates the pool, not just one worker);
* ``pull_id`` is re-bound inside each series worker so the inner ``ingest.*``
  events keep the pull's correlation id across the pool boundary;
* the per-underlying ``ingest.underlying`` rollup + ``on_progress`` still fire
  ONCE per underlying in a DETERMINISTIC ``security_id``-sorted post-pass (Rule
  #4), regardless of which series finished first;
* a non-DhanError escaping a worker surfaces (a real bug is never swallowed),
  while a terminal DhanError on one series stays quarantined.

No real threads: a synchronous ``run_parallel`` fake runs the fetcher inline.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date

import pytest
from brutex.adapters.dhan.errors import DhanBadRequestError
from brutex.adapters.dhan.option_universe_ingest import (
    OptionUnderlying,
    run_dhan_options_ingest,
)
from brutex.data.models import OptionBar
from brutex.observability.correlation import pull_id_scope
from brutex.orchestrator.universe_phases import ItemResult
from structlog.testing import capture_logs

_START = date(2021, 1, 1)
_END = date(2021, 1, 8)
_TS_IN = 1609732800  # 2021-01-04 09:30 IST → in session


def _payload() -> dict[str, object]:
    return {
        "data": {
            "ce": {
                "timestamp": [_TS_IN],
                "open": [100],
                "high": [110],
                "low": [90],
                "close": [105],
                "volume": [250],
                "oi": [1200],
                "strike": [17500],
                "spot": [17480.5],
                "iv": [0.15],
            },
            "pe": None,
        }
    }


class _OkClient:
    """Returns the same one-bar payload for every call (counts calls)."""

    def __init__(self) -> None:
        self.calls = 0

    def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
        del endpoint, body
        self.calls += 1
        return _payload()


class _Sink:
    """Counts appended bars per series (the OptionBarSink seam)."""

    def __init__(self) -> None:
        self.series = 0

    def append_option_bars(
        self,
        *,
        security_id: str,
        exch_id: str,
        underlying_symbol: str,
        exchange_segment: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        drv_option_type: str,
        bars: Sequence[OptionBar],
    ) -> int:
        del security_id, exch_id, underlying_symbol, exchange_segment
        del expiry_flag, expiry_code, strike, drv_option_type
        self.series += 1
        return len(bars)


def _underlying(security_id: str, *, offsets: int = 0) -> OptionUnderlying:
    return OptionUnderlying(
        security_id=security_id,
        exch_id="NSE",
        underlying_symbol=f"U{security_id}",
        exchange_segment="NSE_FNO",
        instrument="OPTIDX",
        strike_offsets=offsets,
        # MONTH (1 flag × 3 codes 1..3 × 1 strike × 2 types = 6 series). This
        # mechanics test is depth-agnostic; MONTH keeps the stable 3-code depth
        # (WEEK now fans 1..8, DEC-DHAN-WEEKLY-EXPIRY-DEPTH-001).
        expiry_flags=("MONTH",),
    )


def _sync_run_parallel[ItemT, ResultT](
    fn: Callable[[ItemT], ResultT],
    items: Sequence[ItemT],
) -> list[ItemResult[ItemT, ResultT]]:
    """A synchronous ``RunParallel`` fake: run *fn* inline, capture exceptions."""
    out: list[ItemResult[ItemT, ResultT]] = []
    for item in items:
        try:
            value = fn(item)
        except Exception as exc:  # mirror PoolRunParallel's capture
            out.append(ItemResult(item=item, ok=False, result=None, exc=exc))
        else:
            out.append(ItemResult(item=item, ok=True, result=value, exc=None))
    return out


class TestRunParallelSeam:
    def test_runner_dispatches_one_job_per_series(self) -> None:
        # DEC-DHAN-OPTION-SERIES-FANOUT-001: the runner now sees one job PER SERIES
        # (not per underlying). Each job is (idx, underlying, flag, code, strike,
        # type) — so a single underlying's whole fan saturates the pool.
        dispatched: list[str] = []

        def _runner(fn, items):
            for item in items:
                dispatched.append(item[1].security_id)
            return _sync_run_parallel(fn, items)

        client, sink = _OkClient(), _Sink()
        report = run_dhan_options_ingest(
            client,
            sink,  # type: ignore[arg-type]
            underlyings=[_underlying("13"), _underlying("25")],
            start=_START,
            end=_END,
            run_parallel=_runner,
        )
        # 2 underlyings × (1 flag × 3 codes × 1 strike × 2 types) = 12 SERIES jobs,
        # in underlying input order.
        assert len(dispatched) == 12
        assert dispatched == ["13"] * 6 + ["25"] * 6
        assert report.series_count == 12
        assert report.failed_series == 0

    def test_single_underlying_fans_all_its_series(self) -> None:
        # The fix's load-bearing proof: ONE underlying produces MANY runner jobs
        # (was exactly 1 → sequential → ~2 r/s), so the pool can saturate ≤5 r/s.
        seen: list[object] = []

        def _runner(fn, items):
            seen.extend(items)
            return _sync_run_parallel(fn, items)

        client, sink = _OkClient(), _Sink()
        report = run_dhan_options_ingest(
            client,
            sink,  # type: ignore[arg-type]
            underlyings=[_underlying("13")],
            start=_START,
            end=_END,
            run_parallel=_runner,
        )
        assert len(seen) == 6  # 6 series jobs for ONE underlying — NOT 1
        assert report.series_count == 6

    def test_default_runner_when_omitted(self) -> None:
        # No run_parallel → the real bounded PoolRunParallel runs it (threads).
        client, sink = _OkClient(), _Sink()
        report = run_dhan_options_ingest(
            client,
            sink,  # type: ignore[arg-type]
            underlyings=[_underlying("13")],
            start=_START,
            end=_END,
        )
        assert report.series_count == 6


class TestPullIdPropagation:
    def test_pull_id_propagates_into_workers(self) -> None:
        # Run each worker in a FRESH context (no inherited pull_id), proving the
        # worker's own re-bind is what stamps the inner events.
        import contextvars

        def _runner(fn, items):
            results = []
            for item in items:
                ctx = contextvars.Context()
                results.append(ctx.run(lambda it=item: _one(fn, it)))
            return results

        def _one(fn, item):
            try:
                value = fn(item)
            except Exception as exc:
                return ItemResult(item=item, ok=False, result=None, exc=exc)
            return ItemResult(item=item, ok=True, result=value, exc=None)

        client, sink = _OkClient(), _Sink()
        with capture_logs() as logs, pull_id_scope("optpull99999"):
            run_dhan_options_ingest(
                client,
                sink,  # type: ignore[arg-type]
                underlyings=[_underlying("13"), _underlying("25")],
                start=_START,
                end=_END,
                run_parallel=_runner,
            )
        # The inner series spine's ingest.* events all carry the bound id (so the
        # worker re-bound it — without that, a fresh context would log pull_id="").
        ingest_events = [e for e in logs if str(e["event"]).startswith("ingest.")]
        assert ingest_events  # the spine logged something
        assert all(e.get("pull_id") == "optpull99999" for e in ingest_events)


class TestDeterministicRollup:
    def test_rollup_and_progress_sorted_by_security_id(self) -> None:
        progress: list[tuple[str, int, bool]] = []

        def _reversed_runner(fn, items):
            # Complete in REVERSE order; the rollup + on_progress must still be
            # emitted in security_id-sorted order (Rule #4).
            return list(reversed(_sync_run_parallel(fn, items)))

        client, sink = _OkClient(), _Sink()
        with capture_logs() as logs, pull_id_scope("optroll00001"):
            run_dhan_options_ingest(
                client,
                sink,  # type: ignore[arg-type]
                underlyings=[_underlying("70"), _underlying("13"), _underlying("42")],
                start=_START,
                end=_END,
                on_progress=lambda sid, bars, failed: progress.append((sid, bars, failed)),
                run_parallel=_reversed_runner,
            )
        rollups = [e for e in logs if e["event"] == "ingest.underlying"]
        assert [e["security_id"] for e in rollups] == ["13", "42", "70"]
        assert all(e["pull_id"] == "optroll00001" for e in rollups)
        # on_progress fired once per underlying, in the same sorted order.
        assert [p[0] for p in progress] == ["13", "42", "70"]
        assert all(p[2] is False for p in progress)  # none failed


class TestQuarantineAndSurface:
    def test_terminal_dhan_error_quarantined_per_series(self) -> None:
        # A client that fails for ONE underlying's calls → all its series are
        # quarantined; the other underlying completes; the run finishes.
        class _OnePoisonClient:
            def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
                del endpoint
                # Body carries the securityId of the underlying being fetched.
                if str(body.get("securityId")) == "BAD":
                    raise DhanBadRequestError("DH boom")
                return _payload()

        sink = _Sink()
        with capture_logs() as logs:
            report = run_dhan_options_ingest(
                _OnePoisonClient(),
                sink,  # type: ignore[arg-type]
                underlyings=[_underlying("GOOD"), _underlying("BAD")],
                start=_START,
                end=_END,
                run_parallel=_sync_run_parallel,
            )
        # BAD's 6 series are quarantined; GOOD's 6 complete.
        assert report.failed_series == 6
        assert report.series_count == 6
        assert all(f.security_id == "BAD" for f in report.failures)
        bad_rollup = [
            e for e in logs if e["event"] == "ingest.underlying" and e["security_id"] == "BAD"
        ]
        assert bad_rollup[0]["series_failed"] == 6
        assert bad_rollup[0]["series_completed"] == 0

    def test_non_dhan_error_surfaces(self) -> None:
        # A genuine bug (ValueError) escaping a worker must NOT be swallowed —
        # only DhanError is quarantined; everything else propagates.
        class _BuggyClient:
            def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
                del endpoint, body
                raise ValueError("a real bug, not a vendor error")

        sink = _Sink()
        with pytest.raises(ValueError, match="a real bug"):
            run_dhan_options_ingest(
                _BuggyClient(),
                sink,  # type: ignore[arg-type]
                underlyings=[_underlying("13")],
                start=_START,
                end=_END,
                run_parallel=_sync_run_parallel,
            )
