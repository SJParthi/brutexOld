# pyright: reportPrivateUsage=none
"""Tests for `brutex.nightly.pipeline_binding` (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

Spy-pins the EXACT kwargs the headless closures pass into ``pull_all_spot`` /
``pull_expiries_contracts`` / ``pull_historical_for_underlyings`` /
``run_pipeline_prebuild_guarded`` (including the OMISSION set — no dashboard
hooks), the per-target ``_materialize`` closure kwargs, the ONE shared
``SessionRateController`` identity, target selection via
``select_pull_instruments`` over a fake store, ``nightly_metadata_years``
parity against the dashboard's ``_resolve_metadata_years`` over a permutation
grid, strict-gate env threading, the capability refusals, and the §3.4
PULL/PREBUILD report decomposition.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from brutex.dashboard.pipeline_prebuild import PrebuildOutcome, PrebuildTarget
from brutex.dashboard.pipeline_pull import PipelinePullReport
from brutex.dashboard.pull_controller import SessionRateController
from brutex.data.timeframe_materialize import PrebuildKind
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.nightly.chain import LinkStatus, NightlyLink
from brutex.nightly.pipeline_binding import (
    HeadlessPipeline,
    NightlyCapabilityError,
    build_headless_pipeline,
    decompose_pipeline_report,
    execute_headless_pipeline,
    nightly_metadata_years,
)


def _instrument(symbol: str, *, idx: bool, security_id: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX if idx else InstrumentType.EQ,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


class _FakeStore:
    """The lean InstrumentStore surface the binding consumes."""

    def __init__(self, candidates: list[Instrument], reference_only: frozenset[int]) -> None:
        self.candidates = candidates
        self.reference_only = reference_only
        self.closed = 0
        self.meta: dict[str, str] = {}

    def fno_eligible_underlyings(self) -> frozenset[tuple[Exchange, str]]:
        return frozenset(
            (inst.exch_id, inst.symbol_name)
            for inst in self.candidates
            if inst.instrument is InstrumentType.EQ
        )

    def list_index_equity_instruments(self) -> list[Instrument]:
        return list(self.candidates)

    def reference_only_security_ids(self) -> frozenset[int]:
        return self.reference_only

    def get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value

    def close(self) -> None:
        self.closed += 1


@dataclass
class _Spies:
    spot: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    historical: dict[str, Any] = field(default_factory=dict)
    prebuild: dict[str, Any] = field(default_factory=dict)
    materialize_calls: list[dict[str, Any]] = field(default_factory=list)
    footprint_calls: list[tuple[Path, Any]] = field(default_factory=list)
    registries: list[object] = field(default_factory=list)


_FIXED_NOW = datetime(2026, 7, 7, 21, 0, tzinfo=UTC)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    prebuild_outcome: PrebuildOutcome | None = None,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
) -> tuple[HeadlessPipeline, _Spies, _FakeStore]:
    """Build a fully spy-wired headless pipeline over a fake store."""
    spies = _Spies()
    lake = tmp_path / "lake"
    lake.mkdir(exist_ok=True)
    (lake / "instruments.duckdb").write_bytes(b"placeholder")

    candidates = [
        _instrument("NIFTY", idx=True, security_id=1),
        _instrument("RELIANCE", idx=False, security_id=2),
        _instrument("REFONLY", idx=False, security_id=3),
    ]
    store = _FakeStore(candidates, reference_only=frozenset({3}))

    def spy_spot(*args: Any, **kwargs: Any) -> str:
        spies.spot = {"args": args, "kwargs": kwargs}
        return "SPOT-REPORT"

    def spy_metadata(*args: Any, **kwargs: Any) -> str:
        spies.metadata = {"args": args, "kwargs": kwargs}
        return "META-REPORT"

    def spy_historical(*args: Any, **kwargs: Any) -> str:
        spies.historical = {"args": args, "kwargs": kwargs}
        return "HIST-REPORT"

    outcome = (
        PrebuildOutcome(symbols_built=2, total_bars=10, failed=(), failed_count=0, ok=True)
        if prebuild_outcome is None
        else prebuild_outcome
    )

    def spy_prebuild(**kwargs: Any) -> PrebuildOutcome:
        spies.prebuild = dict(kwargs)
        return outcome

    class _FakeRegistry:
        pass

    class _FakeRegistryStore:
        @classmethod
        @contextmanager
        def open(cls, path: Path) -> Any:
            registry = _FakeRegistry()
            spies.registries.append(registry)
            yield registry

    def spy_materialize(**kwargs: Any) -> str:
        spies.materialize_calls.append(dict(kwargs))
        return "MATERIALIZE-RESULT"

    def spy_footprint(lake_root: Path, set_meta: Any) -> None:
        spies.footprint_calls.append((lake_root, set_meta))

    monkeypatch.setattr("brutex.dashboard.pull_controller.pull_all_spot", spy_spot)
    monkeypatch.setattr("brutex.dashboard.metadata_pull.pull_expiries_contracts", spy_metadata)
    monkeypatch.setattr(
        "brutex.dashboard.composite_pull.pull_historical_for_underlyings", spy_historical
    )
    monkeypatch.setattr(
        "brutex.dashboard.pipeline_prebuild.run_pipeline_prebuild_guarded", spy_prebuild
    )
    monkeypatch.setattr("brutex.data.registry_store.RegistryStore", _FakeRegistryStore)
    monkeypatch.setattr(
        "brutex.data.timeframe_materialize.materialize_lake_timeframes", spy_materialize
    )
    monkeypatch.setattr("brutex.data.lake_footprint.record_lake_footprint", spy_footprint)
    monkeypatch.setattr(
        "brutex.data.lake_footprint.make_disk_guard",
        lambda _lake, _meta, env=None: "DISK-GUARD",
    )
    monkeypatch.setattr(
        "brutex.dashboard.prebuild_parallel.resolve_prebuild_workers",
        lambda *a, **k: 3,
    )
    monkeypatch.setattr("brutex.dashboard.db_view.list_fno_candle_datasets", lambda _lake: [])
    monkeypatch.setattr("brutex.dashboard.fno_cascade.build_fno_cascade", lambda _ds: "CASCADE")

    hp = build_headless_pipeline(
        lake_root=lake,
        dry_run=dry_run,
        from_date=None,
        to_date=None,
        env=env or {},
        store_opener=lambda _path: cast(Any, store),
        now=lambda: _FIXED_NOW,
    )
    return hp, spies, store


# ── nightly_metadata_years parity ────────────────────────────────────────


@pytest.mark.parametrize(
    ("from_date", "to_date"),
    [
        (None, None),
        ("2023-05-01", None),
        (None, "2024-02-02"),
        ("2022-01-01", "2025-12-31"),
        ("2024-06-15", "2024-06-16"),
    ],
)
def test_metadata_years_parity_with_dashboard(from_date: str | None, to_date: str | None) -> None:
    from brutex.dashboard.app import _resolve_metadata_years

    assert nightly_metadata_years(from_date, to_date) == _resolve_metadata_years(from_date, to_date)


def test_metadata_years_reversed_raises_like_dashboard() -> None:
    from brutex.dashboard.app import _resolve_metadata_years

    with pytest.raises(ValueError, match="empty year range"):
        nightly_metadata_years("2025-01-01", "2022-01-01")
    with pytest.raises(ValueError, match="empty year range"):
        _resolve_metadata_years("2025-01-01", "2022-01-01")


def test_metadata_years_injected_clock() -> None:
    assert nightly_metadata_years(None, None, now=lambda: _FIXED_NOW) == list(range(2020, 2027))


# ── build_headless_pipeline ──────────────────────────────────────────────


def test_spot_closure_kwargs_exact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hp, spies, store = _wire(monkeypatch, tmp_path)
    assert hp.run_spot() == "SPOT-REPORT"
    args = spies.spot["args"]
    kwargs = spies.spot["kwargs"]
    # Positional: the whole-universe targets (index first, then eligible stock;
    # REFERENCE_ONLY names stay IN the spot targets — the app.py law).
    assert [inst.symbol_name for inst in args[0]] == ["NIFTY", "REFONLY", "RELIANCE"]
    assert kwargs["lake_root"] == tmp_path / "lake"
    assert kwargs["dry_run"] is False
    assert kwargs["from_date"] is None
    assert kwargs["to_date"] is None
    assert kwargs["year"] is None
    assert kwargs["intervals"] == ("1minute",)
    assert isinstance(kwargs["session_rate"], SessionRateController)
    assert kwargs["auto_retry"] is True
    assert kwargs["until_clean"] is True
    assert kwargs["floor_store"] is store
    # DEC-GROWW-MARKET-HOURS-GUARD-001: a REAL (dry_run=False) build binds the
    # live-clock guard so a nightly pull halts if it runs into market hours.
    from brutex.calendars.market_hours import guard_active_default

    assert kwargs["market_closed"] is guard_active_default
    # The OMISSION set: no dashboard-only hooks ride the headless binding.
    assert set(kwargs) == {
        "lake_root",
        "dry_run",
        "from_date",
        "to_date",
        "year",
        "intervals",
        "session_rate",
        "auto_retry",
        "until_clean",
        "floor_store",
        "market_closed",
    }
    assert hp.targets == 3


def test_metadata_closure_kwargs_exact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hp, spies, store = _wire(monkeypatch, tmp_path)
    assert hp.run_metadata() == "META-REPORT"
    args = spies.metadata["args"]
    kwargs = spies.metadata["kwargs"]
    # Positional: TRADABLE only (reference-only ids filtered out).
    assert [inst.symbol_name for inst in args[0]] == ["NIFTY", "RELIANCE"]
    assert kwargs["registry"] is spies.registries[-1]
    assert kwargs["coverage_store"] is store
    assert kwargs["years"] == list(range(2020, 2027))
    assert kwargs["dry_run"] is False
    assert isinstance(kwargs["session_rate"], SessionRateController)
    assert set(kwargs) == {"registry", "coverage_store", "years", "dry_run", "session_rate"}


def test_historical_closure_kwargs_exact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hp, spies, store = _wire(monkeypatch, tmp_path)
    assert hp.run_historical(cast(Any, "PRIOR-STEPS")) == "HIST-REPORT"
    args = spies.historical["args"]
    kwargs = spies.historical["kwargs"]
    assert [inst.symbol_name for inst in args[0]] == ["NIFTY", "REFONLY", "RELIANCE"]
    assert kwargs["reference_only_ids"] == frozenset({3})
    assert kwargs["registry"] is spies.registries[-1]
    assert kwargs["coverage_store"] is store
    assert kwargs["lake_root"] == tmp_path / "lake"
    assert kwargs["dry_run"] is False
    assert kwargs["intervals"] == ("1minute",)
    assert kwargs["years"] == list(range(2020, 2027))
    assert isinstance(kwargs["session_rate"], SessionRateController)
    assert kwargs["until_clean"] is True
    # Blank dates ⇒ unbounded expiry scope (the live-edge law's None/None arm).
    assert kwargs["scope_start"] is None
    assert kwargs["scope_end"] is None
    assert kwargs["prior_steps"] == "PRIOR-STEPS"
    assert set(kwargs) == {
        "reference_only_ids",
        "registry",
        "coverage_store",
        "lake_root",
        "dry_run",
        "intervals",
        "years",
        "session_rate",
        "until_clean",
        "scope_start",
        "scope_end",
        "prior_steps",
    }


def test_one_shared_session_rate_controller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hp, spies, _store = _wire(monkeypatch, tmp_path)
    hp.run_spot()
    hp.run_metadata()
    hp.run_historical(cast(Any, None))
    a = spies.spot["kwargs"]["session_rate"]
    b = spies.metadata["kwargs"]["session_rate"]
    c = spies.historical["kwargs"]["session_rate"]
    assert a is b is c  # ONE controller shared by all three phases — never per-phase


def test_headless_binds_market_hours_guard_for_real_pull(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DEC-GROWW-MARKET-HOURS-GUARD-001: a REAL build binds the live-clock guard."""
    from brutex.calendars.market_hours import guard_active_default

    hp, _spies, _store = _wire(monkeypatch, tmp_path, dry_run=False)
    assert hp.market_closed is guard_active_default


def test_headless_dry_run_never_binds_market_hours_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dry-run build makes no vendor calls → the guard is None (never gates)."""
    hp, spies, _store = _wire(monkeypatch, tmp_path, dry_run=True)
    assert hp.market_closed is None
    # The dry-run spot closure threads None through to pull_all_spot too.
    hp.run_spot()
    assert spies.spot["kwargs"]["market_closed"] is None


def test_injected_session_rate_is_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lake = tmp_path / "lake"
    lake.mkdir()
    (lake / "instruments.duckdb").write_bytes(b"x")
    store = _FakeStore([_instrument("NIFTY", idx=True, security_id=1)], frozenset())
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "brutex.dashboard.pull_controller.pull_all_spot",
        lambda *a, **k: captured.update(k),
    )
    rate = SessionRateController()
    hp = build_headless_pipeline(
        lake_root=lake,
        store_opener=lambda _p: cast(Any, store),
        session_rate=rate,
    )
    hp.run_spot()
    assert captured["session_rate"] is rate


def test_prebuild_closure_kwargs_and_materialize(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hp, spies, store = _wire(monkeypatch, tmp_path)
    outcome = hp.run_prebuild(frozenset({"BADSTOCK"}))
    assert outcome.ok is True
    kwargs = spies.prebuild
    assert [inst.symbol_name for inst in kwargs["instruments"]] == [
        "NIFTY",
        "REFONLY",
        "RELIANCE",
    ]
    assert kwargs["source"] == "nightly"
    assert kwargs["exclude_symbols"] == frozenset({"BADSTOCK"})
    assert kwargs["lake_root"] == tmp_path / "lake"
    assert kwargs["workers"] == 3
    # 2026-07-07 21:00 UTC is 2026-07-08 02:30 IST — the cutoff crosses midnight.
    assert kwargs["daily_before"] == date(2026, 7, 8)
    assert kwargs["disk_guard"] == "DISK-GUARD"
    assert set(kwargs) == {
        "instruments",
        "cascade_or_error",
        "materialize",
        "source",
        "exclude_symbols",
        "lake_root",
        "workers",
        "daily_before",
        "disk_guard",
    }
    # The cascade thunk builds from the lake datasets.
    assert kwargs["cascade_or_error"]() == "CASCADE"
    # The per-target _materialize closure (NEVER materialize_lake_timeframes raw).
    target = PrebuildTarget(
        exchange="NSE", segment="INDEX", symbol="NIFTY", kind=PrebuildKind.SPOT, dense=True
    )
    assert kwargs["materialize"](target) == "MATERIALIZE-RESULT"
    [mat] = spies.materialize_calls
    assert mat == {
        "lake_root": tmp_path / "lake",
        "exchange": "NSE",
        "segment": "INDEX",
        "symbol": "NIFTY",
        "kind": PrebuildKind.SPOT,
        "include_daily": True,
        "daily_before": kwargs["daily_before"],
        "dense_symbol": True,
    }
    # Success + not disk-guarded ⇒ the observed footprint is recorded.
    assert spies.footprint_calls == [(tmp_path / "lake", store.set_meta)]


def test_prebuild_disk_guard_blocked_skips_footprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocked = PrebuildOutcome(
        symbols_built=0,
        total_bars=0,
        failed=(),
        failed_count=0,
        ok=False,
        disk_guard_blocked=True,
    )
    hp, spies, _store = _wire(monkeypatch, tmp_path, prebuild_outcome=blocked)
    outcome = hp.run_prebuild(frozenset())
    assert outcome.disk_guard_blocked is True
    assert spies.footprint_calls == []  # gated on `not disk_guard_blocked`


def test_strict_gate_env_threading(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hp, _spies, _store = _wire(monkeypatch, tmp_path, env={"BRUTEX_PIPELINE_STRICT_GATE": "1"})
    assert hp.strict_gate is True
    hp2, _s2, _st2 = _wire(monkeypatch, tmp_path, env={"BRUTEX_PIPELINE_STRICT_GATE": "0"})
    assert hp2.strict_gate is False


def test_strict_gate_junk_fails_loud_before_any_store_open(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="BRUTEX_PIPELINE_STRICT_GATE"):
        build_headless_pipeline(
            lake_root=tmp_path,  # no instruments.duckdb — proves the env check is FIRST
            env={"BRUTEX_PIPELINE_STRICT_GATE": "banana"},
        )


def test_capability_refusal_absent_master(tmp_path: Path) -> None:
    with pytest.raises(NightlyCapabilityError, match="no instrument master"):
        build_headless_pipeline(lake_root=tmp_path, env={})
    # No phantom instruments.duckdb was materialised by the probe.
    assert not (tmp_path / "instruments.duckdb").exists()


def test_capability_refusal_empty_master_closes_store(tmp_path: Path) -> None:
    (tmp_path / "instruments.duckdb").write_bytes(b"x")
    store = _FakeStore([], frozenset())
    with pytest.raises(NightlyCapabilityError, match="EMPTY"):
        build_headless_pipeline(
            lake_root=tmp_path, env={}, store_opener=lambda _p: cast(Any, store)
        )
    assert store.closed == 1  # never leaked


def test_close_seam_closes_the_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hp, _spies, store = _wire(monkeypatch, tmp_path)
    hp.close()
    assert store.closed == 1


def test_explicit_dates_thread_scope_and_years(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake = tmp_path / "lake"
    lake.mkdir()
    (lake / "instruments.duckdb").write_bytes(b"x")
    store = _FakeStore([_instrument("NIFTY", idx=True, security_id=1)], frozenset())
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "brutex.dashboard.composite_pull.pull_historical_for_underlyings",
        lambda *a, **k: captured.update(k),
    )

    class _Reg:
        @classmethod
        @contextmanager
        def open(cls, path: Path) -> Any:
            yield "REG"

    monkeypatch.setattr("brutex.data.registry_store.RegistryStore", _Reg)
    hp = build_headless_pipeline(
        lake_root=lake,
        from_date="2024-02-01",
        to_date="2024-03-31",
        store_opener=lambda _p: cast(Any, store),
    )
    hp.run_historical(cast(Any, None))
    # A genuinely-narrowed range scopes exactly (not default-shaped ⇒ kept).
    assert captured["scope_start"] == date(2024, 2, 1)
    assert captured["scope_end"] == date(2024, 3, 31)
    assert captured["years"] == [2024]


# ── §3.4 decomposition ───────────────────────────────────────────────────


@dataclass
class _FakeHistorical:
    bars_appended: int


@dataclass
class _FakeComposite:
    historical: _FakeHistorical | None


@dataclass
class _FakeReport:
    """The exact attribute surface `decompose_pipeline_report` reads."""

    ok: bool
    note: str
    stopped_after: str | None
    quarantined_count: int
    historical: _FakeComposite | None
    prebuild_built: int | None = None
    prebuild_crashed: str | None = None
    prebuild_disk_guarded: bool = False
    prebuild_failed: tuple[str, ...] = ()
    post_steps_strict_skipped: bool = False


def _report(**kwargs: Any) -> PipelinePullReport:
    defaults: dict[str, Any] = {
        "ok": True,
        "note": "pipeline complete",
        "stopped_after": None,
        "quarantined_count": 0,
        "historical": _FakeComposite(historical=_FakeHistorical(bars_appended=123)),
        "prebuild_built": 5,
    }
    defaults.update(kwargs)
    return cast(PipelinePullReport, _FakeReport(**defaults))


def test_decompose_green_report(monkeypatch: pytest.MonkeyPatch) -> None:
    result, tripwires = decompose_pipeline_report(_report(), started_utc=1.0, ended_utc=2.0)
    assert tripwires == ()
    assert result.status is LinkStatus.OK
    assert result.detail == "pipeline complete"
    assert result.counts == {"pull_quarantined": 0, "pull_bars": 123}
    [prebuild] = result.extra_outcomes
    assert prebuild.link is NightlyLink.PREBUILD
    assert prebuild.status is LinkStatus.OK
    assert prebuild.counts == {"prebuild_excluded": 0, "prebuild_built": 5}
    assert (prebuild.started_utc, prebuild.ended_utc) == (1.0, 2.0)


def test_decompose_stopped_before_prebuild_emits_no_extra() -> None:
    result, tripwires = decompose_pipeline_report(
        _report(
            ok=False,
            stopped_after="spot",
            note="stopped after spot",
            historical=None,
            prebuild_built=None,
        ),
        started_utc=1.0,
        ended_utc=2.0,
    )
    assert tripwires == ()
    assert result.status is LinkStatus.FAILED
    assert result.detail.startswith("stopped_after=spot; ")
    assert "pull_bars" not in result.counts  # honestly unknown, never 0
    assert result.extra_outcomes == ()  # sequencer synthesizes BLOCKED-by-PULL


@pytest.mark.parametrize(
    ("kwargs", "expect_detail"),
    [
        ({"prebuild_crashed": "ValueError", "prebuild_built": 0}, "prebuild CRASHED (ValueError)"),
        ({"prebuild_disk_guarded": True, "prebuild_built": None}, "disk guard"),
        ({"prebuild_failed": ("INFY", "TCS"), "prebuild_built": 7}, "failed symbols: INFY, TCS"),
    ],
)
def test_decompose_prebuild_failure_arms(kwargs: dict[str, Any], expect_detail: str) -> None:
    result, _tripwires = decompose_pipeline_report(
        _report(ok=False, **kwargs), started_utc=1.0, ended_utc=2.0
    )
    [prebuild] = result.extra_outcomes
    assert prebuild.status is LinkStatus.FAILED
    assert expect_detail in prebuild.detail


def test_decompose_strict_withheld_reads_skipped() -> None:
    result, tripwires = decompose_pipeline_report(
        _report(post_steps_strict_skipped=True), started_utc=1.0, ended_utc=2.0
    )
    assert tripwires == ()
    [prebuild] = result.extra_outcomes
    assert prebuild.status is LinkStatus.SKIPPED
    assert "withheld (strict gate)" in prebuild.detail


def test_decompose_negative_counts_fire_t3() -> None:
    _result, tripwires = decompose_pipeline_report(
        _report(
            quarantined_count=-1,
            historical=_FakeComposite(historical=_FakeHistorical(bars_appended=-5)),
            prebuild_built=-2,
        ),
        started_utc=1.0,
        ended_utc=2.0,
    )
    names = [t.name for t in tripwires]
    assert names == ["pull_report_coherence"] * 3
    details = " | ".join(t.detail for t in tripwires)
    assert "bars_appended -5" in details
    assert "quarantined_count -1" in details
    assert "prebuild_built -2" in details


def test_decompose_detail_truncated_to_500() -> None:
    result, _ = decompose_pipeline_report(
        _report(ok=False, stopped_after=None, note="x" * 600),
        started_utc=1.0,
        ended_utc=2.0,
    )
    assert len(result.detail) == 500


# ── execute_headless_pipeline ────────────────────────────────────────────


def test_execute_threads_the_four_phases(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_full_pipeline(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "PIPE-REPORT"

    monkeypatch.setattr("brutex.dashboard.pipeline_pull.run_full_pipeline", fake_run_full_pipeline)
    hp = HeadlessPipeline(
        run_spot=cast(Any, "S"),
        run_metadata=cast(Any, "M"),
        run_historical=cast(Any, "H"),
        run_prebuild=cast(Any, "P"),
        strict_gate=True,
        targets=3,
        close=lambda: None,
    )
    assert execute_headless_pipeline(hp) == "PIPE-REPORT"
    # warm_* movers deliberately ABSENT (default None inside run_full_pipeline).
    assert captured == {
        "run_spot": "S",
        "run_metadata": "M",
        "run_historical": "H",
        "run_prebuild": "P",
        "strict_gate": True,
        # DEC-GROWW-MARKET-HOURS-GUARD-001: the guard is threaded through
        # (None here — the test double is built without one).
        "market_closed": None,
    }
