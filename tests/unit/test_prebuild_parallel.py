"""Unit tests for the parallel prebuild primitive (DEC-HARDWARE-ENVELOPE-001).

100% line + branch of :mod:`brutex.dashboard.prebuild_parallel` — the worker,
the resolver, and the thread-pool driver (in-process kill switch AND the real
:class:`~concurrent.futures.ThreadPoolExecutor` fan-out, plus per-symbol
quarantine + pull-wide global-stop propagation). All offline — the materialiser
is either monkeypatched or injected, so no real lake / polars is needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.dashboard import prebuild_parallel as pp
from brutex.data.errors import LakeWriteHalt
from brutex.data.timeframe_materialize import MaterializeResult, PrebuildKind


def _item(symbol: str = "NIFTY", *, kind: PrebuildKind = PrebuildKind.SPOT) -> pp.PrebuildWorkItem:
    return pp.PrebuildWorkItem(
        lake_root=Path("/tmp/lake"),
        exchange="NSE",
        segment="INDEX",
        symbol=symbol,
        kind=kind,
        include_daily=True,
        daily_before=None,
        incremental=None,
    )


def _ok(symbol: str) -> pp.PrebuildWorkResult:
    return pp.PrebuildWorkResult(
        symbol=symbol, result=MaterializeResult(written={"5m": 3}, skipped=()), error=None
    )


# ────────────────────────────────────────────────────────────────────────
# materialize_work_item
# ────────────────────────────────────────────────────────────────────────


class TestMaterializeWorkItem:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _fake(**kwargs: object) -> MaterializeResult:
            captured.update(kwargs)
            return MaterializeResult(written={"5m": 7}, skipped=())

        monkeypatch.setattr("brutex.data.timeframe_materialize.materialize_lake_timeframes", _fake)
        res = pp.materialize_work_item(_item("BANKNIFTY"))
        assert res.symbol == "BANKNIFTY"
        assert res.error is None
        assert res.result is not None
        assert res.result.total_bars == 7
        # The item's coordinates + flags are threaded into the materialiser.
        assert captured["symbol"] == "BANKNIFTY"
        assert captured["include_daily"] is True

    def test_per_symbol_data_error_quarantined(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(**_kwargs: object) -> MaterializeResult:
            raise ValueError("no stored 1minute bars")

        monkeypatch.setattr("brutex.data.timeframe_materialize.materialize_lake_timeframes", _raise)
        res = pp.materialize_work_item(_item("SENSEX"))
        assert res.result is None
        assert res.error is not None
        assert res.error.startswith("ValueError: ")

    def test_global_stop_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _halt(**_kwargs: object) -> MaterializeResult:
            raise LakeWriteHalt("disk_full")

        monkeypatch.setattr("brutex.data.timeframe_materialize.materialize_lake_timeframes", _halt)
        with pytest.raises(LakeWriteHalt):
            pp.materialize_work_item(_item())


# ────────────────────────────────────────────────────────────────────────
# resolve_prebuild_workers
# ────────────────────────────────────────────────────────────────────────


class TestResolveWorkers:
    def test_env_explicit(self) -> None:
        assert pp.resolve_prebuild_workers({"BRUTEX_PREBUILD_WORKERS": "4"}) == 4

    def test_env_one_is_kill_switch(self) -> None:
        assert pp.resolve_prebuild_workers({"BRUTEX_PREBUILD_WORKERS": "1"}) == 1

    def test_unset_uses_auto(self) -> None:
        assert pp.resolve_prebuild_workers({}, workers_fn=lambda: 8) == 8

    def test_blank_uses_auto(self) -> None:
        assert (
            pp.resolve_prebuild_workers({"BRUTEX_PREBUILD_WORKERS": "  "}, workers_fn=lambda: 6)
            == 6
        )

    def test_invalid_non_integer_falls_back_to_auto(self) -> None:
        assert (
            pp.resolve_prebuild_workers({"BRUTEX_PREBUILD_WORKERS": "x"}, workers_fn=lambda: 5) == 5
        )

    def test_invalid_below_one_falls_back_to_auto(self) -> None:
        assert (
            pp.resolve_prebuild_workers({"BRUTEX_PREBUILD_WORKERS": "0"}, workers_fn=lambda: 5) == 5
        )

    def test_tiny_target_demotes(self) -> None:
        assert pp.resolve_prebuild_workers({}, target_count=1, workers_fn=lambda: 8) == 1

    def test_tiny_target_but_explicit_env_not_demoted(self) -> None:
        got = pp.resolve_prebuild_workers({"BRUTEX_PREBUILD_WORKERS": "4"}, target_count=1)
        assert got == 4

    def test_default_env_is_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_PREBUILD_WORKERS", raising=False)
        got = pp.resolve_prebuild_workers(target_count=5, workers_fn=lambda: 7)
        assert got == 7

    def test_default_workers_fn_is_cpu_pool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pp, "cpu_pool_workers", lambda _env: 9)
        assert pp.resolve_prebuild_workers({}) == 9


# ────────────────────────────────────────────────────────────────────────
# run_work_items
# ────────────────────────────────────────────────────────────────────────


class TestRunWorkItems:
    def test_sequential_kill_switch(self) -> None:
        items = [_item("A"), _item("B")]
        results = pp.run_work_items(items, workers=1, materialize_fn=lambda it: _ok(it.symbol))
        assert [r.symbol for r in results] == ["A", "B"]

    def test_single_item_stays_in_process(self) -> None:
        called: list[str] = []

        def _fn(it: pp.PrebuildWorkItem) -> pp.PrebuildWorkResult:
            called.append(it.symbol)
            return _ok(it.symbol)

        results = pp.run_work_items([_item("ONLY")], workers=8, materialize_fn=_fn)
        assert [r.symbol for r in results] == ["ONLY"]
        assert called == ["ONLY"]

    def test_default_materialize_fn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # workers=1 + default materialize_fn → exercises materialize_work_item path.
        monkeypatch.setattr(
            "brutex.data.timeframe_materialize.materialize_lake_timeframes",
            lambda **_k: MaterializeResult(written={"5m": 1}, skipped=()),
        )
        results = pp.run_work_items([_item("X")], workers=1)
        assert results[0].result is not None

    def test_thread_pool_preserves_input_order(self) -> None:
        items = [_item(str(i)) for i in range(6)]

        def _fn(it: pp.PrebuildWorkItem) -> pp.PrebuildWorkResult:
            return _ok(it.symbol)

        results = pp.run_work_items(items, workers=3, materialize_fn=_fn)
        assert [r.symbol for r in results] == [str(i) for i in range(6)]

    def test_thread_pool_propagates_global_stop(self) -> None:
        items = [_item("A"), _item("B"), _item("C")]

        def _fn(it: pp.PrebuildWorkItem) -> pp.PrebuildWorkResult:
            if it.symbol == "B":
                raise LakeWriteHalt("out_of_memory")
            return _ok(it.symbol)

        with pytest.raises(LakeWriteHalt):
            pp.run_work_items(items, workers=3, materialize_fn=_fn)

    def test_thread_pool_quarantine_result_carried(self) -> None:
        items = [_item("A"), _item("B")]

        def _fn(it: pp.PrebuildWorkItem) -> pp.PrebuildWorkResult:
            if it.symbol == "B":
                return pp.PrebuildWorkResult(symbol="B", result=None, error="ValueError: boom")
            return _ok(it.symbol)

        results = pp.run_work_items(items, workers=2, materialize_fn=_fn)
        assert results[0].error is None
        assert results[1].error == "ValueError: boom"
