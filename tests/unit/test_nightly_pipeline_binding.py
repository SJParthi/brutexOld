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
    cascade_args: list[tuple[Any, ...]] = field(default_factory=list)
    fno_walks: list[str] = field(default_factory=list)


_FIXED_NOW = datetime(2026, 7, 7, 21, 0, tzinfo=UTC)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    prebuild_outcome: PrebuildOutcome | None = None,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
    scope: str = "full",
    registry_builder: Any = None,
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
    monkeypatch.setattr(
        "brutex.dashboard.db_view.list_fno_candle_datasets",
        lambda _lake: spies.fno_walks.append("walk") or [],
    )

    def spy_cascade(datasets: Any) -> str:
        spies.cascade_args.append(tuple(datasets))
        return "CASCADE"

    monkeypatch.setattr("brutex.dashboard.fno_cascade.build_fno_cascade", spy_cascade)

    hp = build_headless_pipeline(
        lake_root=lake,
        dry_run=dry_run,
        from_date=None,
        to_date=None,
        env=env or {},
        store_opener=lambda _path: cast(Any, store),
        now=lambda: _FIXED_NOW,
        scope=scope,
        # DEC-FEED-REGISTRY-COMMON-001: None keeps the default (Groww-only)
        # registry build; a builder is called with THIS lake root.
        registry=registry_builder(lake) if registry_builder is not None else None,
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
    # DEC-GROWW-MARKET-HOURS-GUARD-001: a REAL (dry_run=False) build binds ONE
    # once-resolved clock-only predicate (env parsed once at build, fail-loud) and
    # threads the SAME bound object into every phase — so a junk env set mid-pull can
    # never crash the long nightly run with a per-window re-parse.
    assert kwargs["market_closed"] is hp.market_closed
    assert callable(kwargs["market_closed"])
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
    # DEC-GROWW-MARKET-HOURS-GUARD-001 MAJOR-2: the metadata phase gets the SAME
    # once-resolved belt so a phase CROSSING 08:45 halts mid-phase (per-cell guard).
    assert kwargs["market_closed"] is hp.market_closed
    assert set(kwargs) == {
        "registry",
        "coverage_store",
        "years",
        "dry_run",
        "session_rate",
        "market_closed",
    }


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
    """DEC-GROWW-MARKET-HOURS-GUARD-001: a REAL build binds the ONCE-resolved clock-only
    belt (env parsed once at build, not per-window) — the MINOR fix vs the per-call
    ``guard_active_default``. With the env unset (guard enabled) it is the clock-only
    ``_market_clock_blocked`` predicate. (The suite-wide conftest hermeticity default
    sets the kill switch to ``0``; the enabled arm is restored explicitly here —
    ``delenv`` = the production unset-⇒-enabled default this test pins.)"""
    from brutex.calendars.market_hours import MARKET_HOURS_GUARD_ENV, _market_clock_blocked

    monkeypatch.delenv(MARKET_HOURS_GUARD_ENV, raising=False)
    hp, _spies, _store = _wire(monkeypatch, tmp_path, dry_run=False)
    assert hp.market_closed is _market_clock_blocked


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
        # DEC-FEED-REGISTRY-COMMON-001 (MIGRATION.md #1903): the resolved
        # vendor token is threaded verbatim — a directly-built double carries
        # the "groww" default, which equals the sequencer's own default.
        "vendor": "groww",
    }


# ── scope=spot-only (DEC-PULL-SCOPED-ALL-ONECLICK-001) ───────────────────


def test_scope_full_default_parity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The explicit ``scope="full"`` binds the SAME real F&O closures as the
    scope-less default (the default-parity pin — B §5)."""
    hp, spies, _store = _wire(monkeypatch, tmp_path, scope="full")
    assert hp.run_metadata() == "META-REPORT"
    assert spies.metadata["kwargs"]["dry_run"] is False
    from brutex.dashboard.pipeline_pull import PriorStepsVerdict

    prior = PriorStepsVerdict(spot_clean=True, metadata_clean=True, strict=False)
    assert hp.run_historical(prior) == "HIST-REPORT"
    assert spies.historical["kwargs"]["prior_steps"] is prior
    hp.run_prebuild(frozenset())
    spies.prebuild["cascade_or_error"]()  # force the captured cascade factory
    assert spies.fno_walks == ["walk"]  # the full arm enumerates the F&O lake
    assert spies.cascade_args == [()]  # empty because the walk spy returns []


def test_scope_spot_only_swaps_fno_closures_for_noops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """spot-only: the two F&O closures ARE the pull_scope no-op builders —
    the real spines + the registry are NEVER touched; the prebuild rides an
    EMPTY cascade (no F&O dataset walk)."""
    from brutex.dashboard.pull_scope import (
        SPOT_ONLY_HISTORICAL_NOTE,
        SPOT_ONLY_METADATA_NOTE,
    )

    hp, spies, _store = _wire(monkeypatch, tmp_path, scope="spot-only")
    meta = hp.run_metadata()
    assert meta.note == SPOT_ONLY_METADATA_NOTE
    assert meta.expiries_persisted == 0
    assert spies.metadata == {}  # the real metadata spine never ran
    assert spies.registries == []  # the registry store was never opened
    from brutex.dashboard.pipeline_pull import PriorStepsVerdict

    hist = hp.run_historical(PriorStepsVerdict(spot_clean=True, metadata_clean=True, strict=False))
    assert hist.note == SPOT_ONLY_HISTORICAL_NOTE
    assert hist.historical is None
    assert spies.historical == {}  # the real composite spine never ran
    hp.run_prebuild(frozenset())
    spies.prebuild["cascade_or_error"]()  # force the captured cascade factory
    assert spies.cascade_args == [()]  # EMPTY cascade — spot-only never re-derives F&O
    assert spies.fno_walks == []  # the F&O dataset walk never ran


def test_scope_spot_only_spot_closure_byte_identical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The SPOT closure's kwargs are IDENTICAL between the two scope arms
    (kwarg-for-kwarg — until_clean / floor_store / market_closed all shared)."""
    hp_full, spies_full, _s1 = _wire(monkeypatch, tmp_path, scope="full")
    hp_full.run_spot()
    hp_spot, spies_spot, _s2 = _wire(monkeypatch, tmp_path, scope="spot-only")
    hp_spot.run_spot()
    assert set(spies_full.spot["kwargs"]) == set(spies_spot.spot["kwargs"])
    assert spies_spot.spot["kwargs"]["until_clean"] is True
    assert spies_spot.spot["kwargs"]["auto_retry"] is True


def test_scope_spot_only_composes_muhurat_belt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The spot-only REAL build's market_closed is the COMPOSED predicate
    (guard verdict OR muhurat_session_active) — a live Muhurat session blocks
    even though the trading-day-keyed guard reads the holiday as non-trading;
    a dry-run build stays None (compose passes None through)."""
    from datetime import datetime as _dt

    hp, _spies, _store = _wire(monkeypatch, tmp_path, scope="spot-only")
    assert hp.market_closed is not None
    # Freeze the belt's clock INSIDE a listed Muhurat session (2024-11-01
    # evening) and the guard's own clock OUTSIDE the market-hours window: the
    # bare guard says open (holiday => non-trading), the composed belt blocks.
    monkeypatch.setattr("brutex.calendars.auto_pull_gate.now_ist", lambda: _dt(2024, 11, 1, 18, 30))
    monkeypatch.setattr("brutex.calendars.market_hours.now_ist", lambda: _dt(2024, 11, 1, 18, 30))
    assert hp.market_closed() is True  # the Muhurat arm fired
    monkeypatch.setattr("brutex.calendars.auto_pull_gate.now_ist", lambda: _dt(2024, 11, 1, 20, 0))
    assert hp.market_closed() is False  # past close+5min — open again
    hp_dry, _sp, _st = _wire(monkeypatch, tmp_path, dry_run=True, scope="spot-only")
    assert hp_dry.market_closed is None  # dry-run parity (None passthrough)


def test_scope_spot_only_guard_off_market_closed_is_bare_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[refuter-r1 MINOR-B — kills the M-I hardcoded-env mutant, nightly arm]
    With the guard kill switch OFF in the BUILD env, the spot-only binding's
    ``market_closed`` IS the guard's own never-blocked base (the compose
    returned it UNCHANGED) and reads False even at a frozen Muhurat instant —
    the operator's ``BRUTEX_MARKET_HOURS_GUARD=0`` genuinely disarms the belt
    at the WIRING level. A compose call site hardcoding ``env={"…": "1"}``
    would wrap the base and read True here."""
    from datetime import datetime as _dt

    from brutex.calendars.market_hours import _market_never_blocked

    hp, _spies, _store = _wire(
        monkeypatch, tmp_path, scope="spot-only", env={"BRUTEX_MARKET_HOURS_GUARD": "0"}
    )
    assert hp.market_closed is not None
    assert hp.market_closed is _market_never_blocked  # base returned UNCHANGED
    frozen = _dt(2024, 11, 1, 18, 30)  # inside the listed evening session
    monkeypatch.setattr("brutex.calendars.auto_pull_gate.now_ist", lambda: frozen)
    monkeypatch.setattr("brutex.calendars.market_hours.now_ist", lambda: frozen)
    assert hp.market_closed() is False  # guard off ⇒ belt off — never a block


def test_scope_spot_only_dry_run_threads_into_noop_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[refuter-r1 r2 NIT-6, nightly arm] the binding threads its dry_run
    flag into the bound no-op builders — a dry-run report's nested F&O
    sub-reports never read ``dry_run: false``. The wet build stays False."""
    from brutex.dashboard.pipeline_pull import PriorStepsVerdict

    hp_dry, _sp, _st = _wire(monkeypatch, tmp_path, dry_run=True, scope="spot-only")
    assert hp_dry.run_metadata().dry_run is True
    prior = PriorStepsVerdict(spot_clean=True, metadata_clean=True, strict=False)
    assert hp_dry.run_historical(prior).dry_run is True
    hp_wet, _sp2, _st2 = _wire(monkeypatch, tmp_path, scope="spot-only")
    assert hp_wet.run_metadata().dry_run is False
    assert hp_wet.run_historical(prior).dry_run is False


def test_scope_fno_only_refused_no_headless_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[round-6 two-button split] the registry-valid ``fno-only`` token has NO
    headless binding (dashboard-only by ruling — the manual ▶ Pull F&O
    button): the build REFUSES loudly instead of silently running the FULL
    chain (the B H9 never-widen law). The ``pull-fno`` CLI twin is a named
    one-line follow-up (#1779)."""
    with pytest.raises(ValueError, match="no headless binding"):
        _wire(monkeypatch, tmp_path, scope="fno-only")


def test_scope_unknown_token_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown pull scope"):
        _wire(monkeypatch, tmp_path, scope="spotonly")


# ── the feed-vendor registry consumer (DEC-FEED-REGISTRY-COMMON-001) ─────
#
# MIGRATION.md #1903: build_headless_pipeline resolves its vendor from the
# registry's PRIMARY profile. With only Groww registered (the default build)
# the behavior is BYTE-IDENTICAL to the pre-registry binding — the parity pin
# below proves it over the REAL run_full_pipeline sequencer.


class TestVendorRegistryConsumer:
    """The nightly/CLI chain is the FIRST production registry consumer."""

    def test_default_build_resolves_groww_primary(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """registry=None → the default (Groww-only) build; vendor + domain carried."""
        from brutex.data.write_domain import PRIMARY_DOMAIN_NAME, primary_write_domain

        hp, _spies, _store = _wire(monkeypatch, tmp_path)
        assert hp.vendor == "groww"
        assert hp.write_domain is not None
        assert hp.write_domain.primary is True
        assert hp.write_domain.name == PRIMARY_DOMAIN_NAME
        assert (
            hp.write_domain.resolved_root == primary_write_domain(tmp_path / "lake").resolved_root
        )

    def test_injected_registry_with_extra_feed_keeps_primary(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A testfeed extra in the injected registry leaves the PRIMARY resolution unchanged."""
        from brutex.feeds.registry_build import build_default_registry

        from tests.fakes.vendor_profiles import synthetic_feed_profile

        hp, _spies, _store = _wire(
            monkeypatch,
            tmp_path,
            registry_builder=lambda lake: build_default_registry(
                lake, extra_profiles=[synthetic_feed_profile]
            ),
        )
        assert hp.vendor == "groww"
        assert hp.write_domain is not None and hp.write_domain.primary is True

    def test_primaryless_injected_registry_is_typed_refusal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A feed-only registry (no primary) refuses the build LOUDLY (typed)."""
        from brutex.feeds.vendor_profile import VendorProfileError, VendorProfileRegistry

        from tests.fakes.vendor_profiles import synthetic_feed_profile

        def _feed_only(lake: Path) -> VendorProfileRegistry:
            registry = VendorProfileRegistry()
            registry.register(synthetic_feed_profile(lake))
            return registry

        with pytest.raises(VendorProfileError, match="no PRIMARY profile registered"):
            _wire(monkeypatch, tmp_path, registry_builder=_feed_only)

    def test_default_registry_threads_the_chain_rate_holder(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ADR-0021 §12 invariant 2: ONE rate stack per chain — the injected
        session_rate holder IS the profile's rate-factory product (identity)."""
        captured: dict[str, Any] = {}
        real_build: Any = None

        def _spy_build(lake_root: Path, **kwargs: Any) -> Any:
            registry = real_build(lake_root, **kwargs)
            captured["product"] = registry.primary().rate_factory()
            return registry

        from brutex.feeds import registry_build as rb

        real_build = rb.build_default_registry
        monkeypatch.setattr("brutex.feeds.registry_build.build_default_registry", _spy_build)
        # _wire builds with session_rate=None → the function mints ONE holder;
        # rebuild here with an explicit holder to pin identity end-to-end.
        rate = SessionRateController()
        lake = tmp_path / "lake"
        lake.mkdir(exist_ok=True)
        (lake / "instruments.duckdb").write_bytes(b"placeholder")
        store = _FakeStore(
            [_instrument("NIFTY", idx=True, security_id=1)], reference_only=frozenset()
        )
        hp = build_headless_pipeline(
            lake_root=lake,
            dry_run=True,
            env={},
            store_opener=lambda _p: cast(Any, store),
            now=lambda: _FIXED_NOW,
            session_rate=rate,
        )
        assert hp.vendor == "groww"
        assert captured["product"] is rate

    def test_default_inert_report_parity_over_real_sequencer(self, tmp_path: Path) -> None:
        """THE DEFAULT-INERT PARITY PIN (the WS-B frozen-parity pattern).

        The SAME real-shaped phase fakes are run (a) through the PRE-change
        call shape — ``run_full_pipeline`` with NO ``vendor`` kwarg — and
        (b) through the post-change consumer path — ``execute_headless_pipeline``
        over a :class:`HeadlessPipeline` whose vendor was resolved from the
        default (Groww-only) registry. The two ``PipelinePullReport`` dicts
        must be EQUAL: with only Groww registered the registry wiring is
        byte-inert (vendor="groww" == the sequencer's own default).
        """
        from dataclasses import asdict

        from brutex.dashboard.pipeline_pull import run_full_pipeline
        from brutex.feeds.registry_build import build_default_registry

        def _phases() -> dict[str, Any]:
            from tests.unit.test_pipeline_pull import (
                _composite,
                _historical_inner,
                _metadata,
                _outcome,
                _spot,
            )

            return {
                "run_spot": lambda: _spot(),  # noqa: PLW0108 — a FRESH report per call
                "run_metadata": lambda: _metadata(),  # noqa: PLW0108 — a FRESH report per call
                "run_historical": lambda _prior: _composite(historical=_historical_inner()),
                "run_prebuild": lambda _exclude: _outcome(),
            }

        pre_change = run_full_pipeline(**_phases(), strict_gate=False, market_closed=None)

        profile = build_default_registry(tmp_path).primary()
        hp = HeadlessPipeline(
            **_phases(),
            strict_gate=False,
            targets=1,
            close=lambda: None,
            market_closed=None,
            vendor=profile.vendor,
            write_domain=profile.write_domain,
        )
        post_change = execute_headless_pipeline(hp)

        assert asdict(post_change) == asdict(pre_change)
