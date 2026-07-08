# pyright: reportPrivateUsage=none
"""Tests for the CLI ``brutex fetch`` vendor-depth-floor wiring (DEC-CLI-DEPTH-FLOORS-001).

Closes DEC-VENDOR-FLOOR-LAZY-PROBE-001's named follow-up ("the CLI ``brutex
fetch`` path stays UNWIRED — no floor_store seam there; dashboard pulls only"):
``cmd_fetch`` now mirrors the dashboard build sites —

* the per-pull :class:`~brutex.adapters.groww.vendor_floor.VendorFloorLearner`
  is built over the PACED replica of the pull's adapter
  (``with_paced_client(adapter, pace)`` — the review-BLOCKER fold: a learner
  over the raw adapter would probe OUTSIDE the 8 r/s ceiling + 500/min
  governor) and threaded into ``run_groww_spot_ingest_with_retry``;
* a symbol with a PERSISTED learned floor (the SAME instrument-master
  ``schema_meta`` sidecar the dashboard writes — ``groww.vendor_floor.<sym>``)
  plans + fetches its 1-minute windows from ``max(GROWW_HISTORY_FLOOR,
  learned floor)`` via the existing per-symbol ``symbol_windows`` seam, with
  the DAILY interval exempt (a 1-minute-probed floor never clamps 1-day);
* ``--dry-run`` / ``--inspect`` open no store, build no learner, and thread
  ``None`` — byte-identical planning (the ``_no_persist`` arming gate).

Everything network-shaped is faked exactly like the sibling
``test_cli_retry_storm.py`` live-wiring suite (creds gate, instrument
resolution, adapter build, the ingest engine itself).
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from brutex import cli

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("polars")


# ── shared duck-typed fakes (the test_cli_retry_storm shapes) ────────────


@dataclass(frozen=True)
class _FakeIngestReport:
    """Duck-typed ``GrowwIngestReport`` surface the fetch summary printing reads."""

    windows: int
    bars_appended: int
    failed_pairs: int
    transient_failures: int
    still_failed_windows: int
    pairs: dict[tuple[str, str], Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _FakeIngestResult:
    """Duck-typed ``SpotIngestWithRetry`` (clean report, no retry round)."""

    report: _FakeIngestReport
    retry: None = None

    @property
    def still_failed_windows(self) -> int:
        return self.report.still_failed_windows


def _clean_result() -> _FakeIngestResult:
    return _FakeIngestResult(
        report=_FakeIngestReport(
            windows=1,
            bars_appended=375,
            failed_pairs=0,
            transient_failures=0,
            still_failed_windows=0,
        )
    )


class _EmptySDK:
    """``GrowwSDK``-shaped fake — every historical-candle window clean-empty."""

    def get_historical_candles(self, **_kwargs: object) -> Mapping[str, object]:
        return {"candles": []}

    def get_expiries(self, **_kwargs: object) -> Mapping[str, object]:
        return {}

    def get_contracts(self, **_kwargs: object) -> Mapping[str, object]:
        return {}


def _groww_adapter() -> Any:
    """A REAL ``GrowwMarketAdapter`` over the empty SDK (the paced-replica pin needs it)."""
    from brutex.adapters.groww.adapter import GrowwMarketAdapter
    from brutex.adapters.groww.client import GrowwClient
    from brutex.adapters.groww.constants import INSTRUMENT_CSV_COLUMNS
    from brutex.adapters.groww.instruments import load_index_from_csv

    header = ",".join(INSTRUMENT_CSV_COLUMNS)
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(
        exchange="NSE",
        exchange_token="NIFTY",
        trading_symbol="NIFTY",
        groww_symbol="NSE-NIFTY",
        name="NIFTY 50",
        instrument_type="IDX",
        segment="CASH",
    )
    row = ",".join(cells[c] for c in INSTRUMENT_CSV_COLUMNS)
    index = load_index_from_csv(f"{header}\n{row}\n")
    return GrowwMarketAdapter(
        client=GrowwClient(sdk=_EmptySDK(), sleep=lambda _s: None), index=index
    )


def _wire_live_fakes(monkeypatch: pytest.MonkeyPatch, adapter: Any, sink: Any) -> dict[str, Any]:
    """Fake the creds gate + instrument resolution + adapter build + ingest engine.

    Returns the ``seen`` dict the fake ingest records its kwargs into.
    """
    from brutex.universe import BRUTE_FORCE_INDICES

    instruments = [cli._synthetic_index_instrument(BRUTE_FORCE_INDICES[0])]
    monkeypatch.setattr(cli, "_resolve_brute_force_instruments", lambda lake: instruments)
    credentials = importlib.import_module("brutex.config.credentials")
    monkeypatch.setattr(credentials, "groww_credentials_error", lambda: None)
    monkeypatch.setattr(cli, "_build_live_groww_spot", lambda lake, catalog=None: (adapter, sink))

    run_ingest = importlib.import_module("brutex.adapters.groww.run_ingest")
    seen: dict[str, Any] = {}

    def fake_ingest(adapter_arg: Any, sink_arg: Any, **kwargs: Any) -> _FakeIngestResult:
        seen["adapter"] = adapter_arg
        seen["sink"] = sink_arg
        seen.update(kwargs)
        return _clean_result()

    monkeypatch.setattr(run_ingest, "run_groww_spot_ingest_with_retry", fake_ingest)
    return seen


# ── _TransientCliFloorStore (round-1 review MAJOR: never a whole-pull hold) ──


def test_transient_floor_store_roundtrips_meta_per_operation(tmp_path: Path) -> None:
    """The transient sidecar roundtrips the learned-floor rows (structural
    ``VendorFloorMetaStore`` conformance) with an open→op→close per call."""
    from brutex.adapters.groww.vendor_floor import vendor_floor_key

    store = cli._TransientCliFloorStore(tmp_path)
    assert store.path == tmp_path / "instruments.duckdb"
    assert store.get_meta(vendor_floor_key("NIFTY")) is None
    store.set_meta(vendor_floor_key("NIFTY"), "2024-02-01")
    assert store.get_meta(vendor_floor_key("NIFTY")) == "2024-02-01"
    assert (tmp_path / "instruments.duckdb").exists()


def test_transient_floor_store_never_holds_the_duckdb_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every get/set opens then CLOSES the store — no handle survives the call
    (the whole-pull hold was the round-1 review MAJOR)."""
    from brutex.adapters.groww.vendor_floor import vendor_floor_key
    from brutex.data.instrument_store import InstrumentStore

    live: list[object] = []
    real_open = InstrumentStore.open.__func__  # type: ignore[attr-defined]

    def _tracking_open(cls: type[InstrumentStore], path: Path, **kwargs: Any) -> Any:
        store = real_open(cls, path, **kwargs)
        real_close = store.close

        def _tracking_close() -> None:
            live.remove(store)
            real_close()

        live.append(store)
        monkeypatch.setattr(store, "close", _tracking_close, raising=False)
        return store

    monkeypatch.setattr(InstrumentStore, "open", classmethod(_tracking_open))
    store = cli._TransientCliFloorStore(tmp_path)
    assert live == []  # construction opens NOTHING
    store.set_meta(vendor_floor_key("NIFTY"), "2024-02-01")
    assert live == []  # closed before set_meta returned
    assert store.get_meta(vendor_floor_key("NIFTY")) == "2024-02-01"
    assert live == []  # closed before get_meta returned


def test_transient_floor_store_close_runs_even_when_the_op_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising op still closes the transient handle (the ``finally``)."""
    from brutex.data.instrument_store import InstrumentStore

    closed: list[bool] = []
    real_open = InstrumentStore.open.__func__  # type: ignore[attr-defined]

    def _tracking_open(cls: type[InstrumentStore], path: Path, **kwargs: Any) -> Any:
        store = real_open(cls, path, **kwargs)
        real_close = store.close
        monkeypatch.setattr(
            store, "close", lambda: (closed.append(True), real_close()), raising=False
        )
        monkeypatch.setattr(
            store,
            "get_meta",
            lambda _key: (_ for _ in ()).throw(RuntimeError("op bug")),
            raising=False,
        )
        return store

    monkeypatch.setattr(InstrumentStore, "open", classmethod(_tracking_open))
    with pytest.raises(RuntimeError, match="op bug"):
        cli._TransientCliFloorStore(tmp_path).get_meta("k")
    assert closed == [True]


def test_transient_floor_store_leaves_file_openable_by_a_second_process(
    tmp_path: Path,
) -> None:
    """The round-1 review MAJOR regression, proven cross-process: after the CLI
    floor store is constructed AND used, a SECOND process (a `brutex serve`
    boot / a dashboard) can still open ``instruments.duckdb`` read-write.
    FAILS BEFORE the fix: the old helper held one rw DuckDB connection (an
    exclusive cross-process file lock) for the whole fetch, so the subprocess
    open raised ``CatalogError: ... Could not set lock on file``."""
    import subprocess
    import sys

    from brutex.adapters.groww.vendor_floor import vendor_floor_key

    store = cli._TransientCliFloorStore(tmp_path)
    store.set_meta(vendor_floor_key("NIFTY"), "2024-02-01")  # the file exists + was used
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path\n"
                "from brutex.data.instrument_store import InstrumentStore\n"
                f"s = InstrumentStore.open(Path({str(tmp_path / 'instruments.duckdb')!r}))\n"
                "print('OPENED', s.get_meta('groww.vendor_floor.NIFTY'))\n"
                "s.close()\n"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,  # the assert below reports stderr on failure
    )
    assert probe.returncode == 0, probe.stderr
    assert "OPENED 2024-02-01" in probe.stdout


def test_transient_floor_store_per_op_failure_degrades_load_to_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honest concurrent envelope: while a peer holds the DuckDB file, a
    per-op open failure propagates to ``load_vendor_floors``, which degrades to
    ``{}`` with one WARN — the fetch is never blocked and later ops self-heal."""
    from brutex.adapters.groww.vendor_floor import load_vendor_floors
    from brutex.data import instrument_store
    from brutex.data.errors import CatalogError

    def _boom(*_a: object, **_kw: object) -> object:
        raise CatalogError("file locked by a running dashboard")

    monkeypatch.setattr(instrument_store.InstrumentStore, "open", _boom)
    store = cli._TransientCliFloorStore(tmp_path)
    assert load_vendor_floors(store, ["NIFTY", "BANKNIFTY"]) == {}


# ── cmd_fetch wiring: the persisting live pull ───────────────────────────


def test_cmd_fetch_live_builds_learner_over_paced_adapter_and_clamps_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persisting live pull (a) builds the learner over the PACED replica of
    the pull's adapter with the shared store + floor/today bounds, (b) threads it
    into the ingest engine, and (c) clamps a floor-learned symbol's 1-minute
    planning + fetching to ``max(GROWW_HISTORY_FLOOR, learned floor)`` while the
    1-day interval stays exempt (never clamped off a 1-minute-probed floor)."""
    from brutex.adapters.groww.adapter import GrowwMarketAdapter
    from brutex.adapters.groww.client import GrowwClient
    from brutex.adapters.groww.constants import GROWW_RATE_LIMIT_RPS_CEILING
    from brutex.adapters.groww.vendor_floor import vendor_floor_key
    from brutex.adapters.rate_common import RateController
    from brutex.dashboard.pull_controller import GROWW_HISTORY_FLOOR
    from brutex.data.instrument_store import InstrumentStore

    # Pre-seed a learned floor in the SAME schema_meta sidecar the dashboard
    # writes — the cross-entrypoint contract (a dashboard-learned floor clamps
    # the CLI pull too).
    with InstrumentStore.open(tmp_path / "instruments.duckdb") as seeded:
        seeded.set_meta(vendor_floor_key("NIFTY"), "2024-02-01")

    raw = _groww_adapter()
    seen = _wire_live_fakes(monkeypatch, raw, object())

    vendor_floor = importlib.import_module("brutex.adapters.groww.vendor_floor")
    sentinel_learner = object()
    maker_calls: list[dict[str, Any]] = []
    real_maker = vendor_floor.make_vendor_floor_learner

    def _recording_maker(adapter: Any, **kwargs: Any) -> Any:
        maker_calls.append({"adapter": adapter, **kwargs})
        # Build the real learner too (arg validity), but hand the spine the sentinel.
        real_maker(adapter, **kwargs)
        return sentinel_learner

    monkeypatch.setattr(vendor_floor, "make_vendor_floor_learner", _recording_maker)

    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-03-31",
        "all",  # 1minute + 1day — the daily-exemption pin needs both
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=5,
        auto_retry=False,
    )
    assert rc == cli.EXIT_OK

    # (a) The learner was built ONCE, over the PACED replica — never the raw
    # adapter (its client's pace is the CLI RateController's bound method at the
    # operator-directed 8 r/s ceiling; DEC-RATE-SINGLE-ADMISSION-001).
    assert len(maker_calls) == 1
    call = maker_calls[0]
    paced = call["adapter"]
    assert paced is not raw
    assert isinstance(paced, GrowwMarketAdapter)
    assert isinstance(paced.client, GrowwClient)
    controller = getattr(paced.client.pace, "__self__")  # noqa: B009
    assert isinstance(controller, RateController)
    assert controller.current_rps == GROWW_RATE_LIMIT_RPS_CEILING
    assert call["history_floor"] == GROWW_HISTORY_FLOOR
    assert call["today"] == date(2024, 3, 31)
    # The sidecar is the TRANSIENT wrapper (round-1 review MAJOR fold) — never
    # a held-open InstrumentStore riding the whole pull.
    assert isinstance(call["store"], cli._TransientCliFloorStore)
    assert not isinstance(call["store"], InstrumentStore)
    assert call["store"].path == tmp_path / "instruments.duckdb"

    # (b) The learner + the clamp reach the ingest engine — planned keys ==
    # fetched keys (the SAME map plan_windows tiled).
    assert seen["floor_learner"] is sentinel_learner
    assert seen["symbol_windows"] == {"NIFTY": (date(2024, 2, 1), date(2024, 3, 31))}

    # (c) The durable ledger planned the clamped 1-minute range — and the DAILY
    # interval kept the FULL range (plan_windows' daily exemption, unclamped).
    rows = list(seen["ledger"].records())
    minute_starts = sorted(r.window_start for r in rows if r.interval == "1minute")
    daily_starts = sorted(r.window_start for r in rows if r.interval == "1day")
    assert minute_starts and minute_starts[0] == "2024-02-01"  # clamped to the floor
    assert daily_starts and daily_starts[0] == "2024-01-02"  # daily exempt: full range


def test_cmd_fetch_live_floor_at_or_before_start_plans_full_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A learned floor at/before the requested start clamps NOTHING —
    ``symbol_windows`` resolves to ``None`` and planning is byte-identical."""
    from brutex.adapters.groww.vendor_floor import vendor_floor_key
    from brutex.data.instrument_store import InstrumentStore

    with InstrumentStore.open(tmp_path / "instruments.duckdb") as seeded:
        seeded.set_meta(vendor_floor_key("NIFTY"), "2023-06-01")  # before the start

    seen = _wire_live_fakes(monkeypatch, object(), object())
    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=False,
        inspect=False,
        rows_per_chunk=5,
        auto_retry=False,
    )
    assert rc == cli.EXIT_OK
    assert seen["symbol_windows"] is None  # never-narrow: no clamp, full range
    assert seen["floor_learner"] is not None  # the learner is still armed
    rows = list(seen["ledger"].records())
    assert sorted(r.window_start for r in rows)[0] == "2024-01-02"


# ── cmd_fetch arming gate: --inspect / --dry-run stay inert ──────────────


def test_cmd_fetch_inspect_builds_no_learner_and_opens_no_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--inspect`` (persistence-free) threads ``None`` for BOTH the learner and
    the clamp, and never even opens the sidecar (the ``_no_persist`` gate)."""
    vendor_floor = importlib.import_module("brutex.adapters.groww.vendor_floor")

    def _never_maker(*_a: object, **_kw: object) -> object:
        raise AssertionError("make_vendor_floor_learner must not run for --inspect")

    monkeypatch.setattr(vendor_floor, "make_vendor_floor_learner", _never_maker)
    seen = _wire_live_fakes(monkeypatch, object(), object())

    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=False,
        inspect=True,
        rows_per_chunk=5,
    )
    assert rc == cli.EXIT_OK
    assert seen["floor_learner"] is None
    assert seen["symbol_windows"] is None
    assert not (tmp_path / "instruments.duckdb").exists()  # sidecar never opened


def test_cmd_fetch_dry_run_builds_no_learner_and_opens_no_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dry-run`` (the synthetic offline pipeline) is equally inert — no store,
    no learner, no probe surface (the dashboard's dry-run arming gate, mirrored)."""
    vendor_floor = importlib.import_module("brutex.adapters.groww.vendor_floor")

    def _never_maker(*_a: object, **_kw: object) -> object:
        raise AssertionError("make_vendor_floor_learner must not run for --dry-run")

    monkeypatch.setattr(vendor_floor, "make_vendor_floor_learner", _never_maker)
    rc = cli.cmd_fetch(
        tmp_path,
        "2024-01-02",
        "2024-01-03",
        "1m",
        window_days=None,
        dry_run=True,
        inspect=False,
        rows_per_chunk=2,
    )
    assert rc == cli.EXIT_OK
    assert not (tmp_path / "instruments.duckdb").exists()  # sidecar never opened
