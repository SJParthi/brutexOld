# pyright: reportPrivateUsage=none
"""F&O steps 3-6 bucket ordering in ``historical_pull`` (DEC-PULL-SIX-STEP-ORDER-001).

The 4-key ``(ledger-market, is_index)`` bucket sort replacing ``_MARKET_PULL_ORDER``:
decreed 3→4→5→6 ingest order, write/read-symmetric classification (alias-safe),
fail-closed-for-ORDER stock-side fallback (WARN + ``class_unresolved``), the legacy
``underlying_kinds=None`` two-bucket degrade, ledger-namespace byte-identity +
stored-bars/ledger-rows byte-parity vs the legacy path, STEP-major until-clean
(retries settle within a step before the next step's first vendor call), and
auth / mass-empty halt propagation (``pull.step.halted``). Fully offline.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import polars as pl
import pytest
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import (
    GROWW_FUTURE_COVERAGE_MARKET,
    GROWW_OPTION_COVERAGE_MARKET,
)
from brutex.adapters.ingest_common import StepOrderMonitor
from brutex.dashboard import historical_pull as hp
from brutex.dashboard.composite_pull import _underlying_kind_map
from brutex.dashboard.historical_pull import HistoricalPullReport, pull_contract_candles
from brutex.dashboard.pipeline_pull import PriorStepsVerdict
from brutex.data.registry_store import ContractRow, RegistryStore
from brutex.data.sink import BarSink, SinkAppendResult
from brutex.data.window_ledger_store import load_window_ledger
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from structlog.testing import capture_logs

pytest.importorskip("duckdb", reason="RegistryStore needs duckdb")

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_RUN_ID = "step-bucket-test"
_FETCHED = datetime(2026, 1, 1, tzinfo=UTC)
_FROM = date(2024, 1, 1)
_TO = date(2024, 1, 20)
_EXPIRY = date(2024, 1, 25)

#: The write/read-symmetric classification map (NIFTY is the index; ADANIENT the stock).
_KINDS: dict[str, bool] = {"NIFTY": True, "ADANIENT": False}

_NIFTY_FUT = "NSE-NIFTY-25Jan24-FUT"
_NIFTY_CE = "NSE-NIFTY-25Jan24-21000-CE"
_ADANI_FUT = "NSE-ADANIENT-25Jan24-FUT"
_ADANI_CE = "NSE-ADANIENT-25Jan24-3000-CE"


# ── offline fakes (mirroring tests/unit/test_pull_historical.py) ─────────────


def _candle_payload() -> dict[str, object]:
    """One bar (row-of-arrays Groww shape: ts, o, h, l, c, v, oi)."""
    return {"candles": [["2024-01-15T09:15:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]}


class GrowwAPIAuthenticationException(Exception):
    """A terminal auth error — sets ``auth_aborted`` and halts."""


class GrowwAPITimeoutException(Exception):
    """A RETRYABLE timeout — the window lands FAILED and is re-pulled."""


class _FakeSDK:
    """A recording ``GrowwSDK`` fake: canned candles + per-symbol raise budgets.

    ``raise_for`` maps a contract ``groww_symbol`` to ``(exception, times)`` — the
    error fires for that symbol's first *times* fetches, then the canned candle
    lands (so a transient retry can SUCCEED on the re-fetch).
    """

    def __init__(self, *, raise_for: dict[str, tuple[Exception, int]] | None = None) -> None:
        self._raise_for = dict(raise_for or {})
        self._raised: dict[str, int] = {}
        self.candle_symbols: list[str] = []

    def get_expiries(self, **_kw: object) -> dict[str, object]:
        """Never expected — the contracts come from the registry."""
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> dict[str, object]:
        """Never expected — the contracts come from the registry."""
        return {"contracts": []}

    def get_historical_candles(self, **kw: object) -> dict[str, object]:
        """Record the symbol; raise its budgeted error or return the canned bar."""
        symbol = str(kw["groww_symbol"])
        self.candle_symbols.append(symbol)
        budget = self._raise_for.get(symbol)
        if budget is not None:
            exc, times = budget
            fired = self._raised.get(symbol, 0)
            if fired < times:
                self._raised[symbol] = fired + 1
                raise exc
        return _candle_payload()


class _EmptySDK:
    """An all-empty vendor — every candle fetch returns zero bars (the R2 shape)."""

    def get_expiries(self, **_kw: object) -> dict[str, object]:
        """Never expected — the contracts come from the registry."""
        return {"expiries": []}

    def get_contracts(self, **_kw: object) -> dict[str, object]:
        """Never expected — the contracts come from the registry."""
        return {"contracts": []}

    def get_historical_candles(self, **_kw: object) -> dict[str, object]:
        """Honest empty — no bars, never fabricated."""
        return {"candles": []}


class _RecordingSink:
    """A minimal recording :class:`BarSink` capturing every append."""

    def __init__(self) -> None:
        self.appends: list[tuple[str, str, str, int]] = []

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[object],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        """Record ``(segment, symbol, interval, n_bars)``; report all bars appended."""
        self.appends.append((segment, symbol, interval, len(bars)))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


def _client(sdk: object) -> GrowwClient:
    """Wrap *sdk* in a no-sleep, single-attempt :class:`GrowwClient` (offline)."""
    return GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)


def _factory(sdk: object, sink: BarSink) -> tuple[GrowwClient, BarSink]:
    """Return a fixed ``(client, sink)`` pair for the injected ``client_factory``."""
    return _client(sdk), sink


def _real_sink(lake_root: Path) -> BarSink:
    """A real ``ParquetBarSink`` for the byte-parity golden."""
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    return ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))


def _seed_registry(tmp_path: Path, *, symbols: Sequence[tuple[str, str, str]]) -> RegistryStore:
    """A real temp registry seeded with ``(underlying, symbol, side)`` legs at _EXPIRY."""
    store = RegistryStore.open(tmp_path / "registry.duckdb")
    for underlying, symbol, side in symbols:
        store.upsert_contract(
            "NSE",
            underlying,
            _EXPIRY,
            symbol,
            side=side,
            strike=21000.0 if side in {"CE", "PE"} else None,
            run_id=_RUN_ID,
            fetched_at=_FETCHED,
        )
    return store


_FOUR_BUCKET_LEGS: tuple[tuple[str, str, str], ...] = (
    ("NIFTY", _NIFTY_FUT, "FUT"),
    ("NIFTY", _NIFTY_CE, "CE"),
    ("ADANIENT", _ADANI_FUT, "FUT"),
    ("ADANIENT", _ADANI_CE, "CE"),
)


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    """All captured events with ``event == name`` (in emission order)."""
    return [e for e in logs if e["event"] == name]


def _row(underlying: str, symbol: str, side: str) -> ContractRow:
    """A minimal :class:`ContractRow` for the pure classification helpers."""
    return ContractRow(
        exch="NSE",
        underlying=underlying,
        expiry_date=_EXPIRY,
        contract_symbol=symbol,
        strike=None if side == "FUT" else 21000.0,
        side=side,
        candle_status="none",
        last_run_id=_RUN_ID,
        fetched_at=_FETCHED,
    )


def _instrument(symbol: str, kind: InstrumentType) -> Instrument:
    """A minimal spot instrument for ``_underlying_kind_map``."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=1,
        symbol_name=symbol,
        display_name=None,
        instrument=kind,
        segment=Segment.INDEX if kind == InstrumentType.IDX else Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0 if kind == InstrumentType.IDX else 500,
        tick_size=Decimal("0") if kind == InstrumentType.IDX else Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


class TestStepBucketOrder:
    """The decreed 3→4→5→6 ingest order + ledger-namespace/byte parity."""

    def test_four_buckets_ingest_in_decreed_order(self, tmp_path: Path) -> None:
        registry = _seed_registry(tmp_path, symbols=_FOUR_BUCKET_LEGS)
        sdk = _FakeSDK()
        sink = _RecordingSink()
        with capture_logs() as logs:
            report = pull_contract_candles(
                registry.list_contracts(),
                lake_root=tmp_path / "lake",
                dry_run=False,
                intervals=("1minute",),
                registry=registry,
                from_date=_FROM,
                to_date=_TO,
                client_factory=lambda _root: _factory(sdk, sink),
                now=_NOW,
                underlying_kinds=_KINDS,
                # The HARD ordering oracle: any rank regression raises.
                step_monitor=StepOrderMonitor(raise_on_violation=True),
            )
        # The vendor fetch order IS the decreed step order (serial offline run).
        assert sdk.candle_symbols == [_NIFTY_FUT, _ADANI_FUT, _NIFTY_CE, _ADANI_CE]
        started = _events(logs, "pull.step.started")
        settled = _events(logs, "pull.step.settled")
        assert [e["step"] for e in started] == [
            "indices-futures",
            "stocks-futures",
            "indices-options",
            "stocks-options",
        ]
        assert [e["ordinal"] for e in started] == [3, 4, 5, 6]
        assert [e["step"] for e in settled] == [e["step"] for e in started]
        assert all(e["clean"] is True for e in settled)
        # settled(k) strictly precedes started(k+1) — step-major by construction.
        for k in range(3):
            assert logs.index(settled[k]) < logs.index(started[k + 1])
        assert report.contracts_completed == 4
        assert report.class_unresolved == 0

    def test_bucket_contract_sets_equal_per_market_sets(self, tmp_path: Path) -> None:
        """The 4 buckets REFINE the market split — per-market unions are unchanged."""
        registry = _seed_registry(tmp_path, symbols=_FOUR_BUCKET_LEGS)
        sdk = _FakeSDK()
        pull_contract_candles(
            registry.list_contracts(),
            lake_root=tmp_path / "lake",
            dry_run=False,
            intervals=("1minute",),
            registry=registry,
            from_date=_FROM,
            to_date=_TO,
            client_factory=lambda _root: _factory(sdk, _RecordingSink()),
            now=_NOW,
            underlying_kinds=_KINDS,
        )
        ledger = load_window_ledger(tmp_path / "lake")
        by_market: dict[str, set[str]] = {}
        for record in ledger.records():
            by_market.setdefault(record.market, set()).add(record.security_id)
        # Ledger market namespaces byte-identical to the legacy market split
        # (the no-mass-re-pull invariant): FUT legs under groww-future, CE under
        # groww-option — the is_index class NEVER leaks into the ledger key.
        assert by_market == {
            GROWW_FUTURE_COVERAGE_MARKET: {_NIFTY_FUT, _ADANI_FUT},
            GROWW_OPTION_COVERAGE_MARKET: {_NIFTY_CE, _ADANI_CE},
        }

    def test_byte_parity_with_legacy_order(self, tmp_path: Path) -> None:
        """GOLDEN: stored bars + ledger rows are byte-identical to the legacy path.

        The SAME selection pulled (a) with the six-step ``underlying_kinds``
        classification and (b) as a legacy ``underlying_kinds=None`` caller (the
        pre-change 2-bucket order) lands EXACTLY the same lake partitions with the
        same rows and the same normalized ledger rows — the ordering change moves
        WHEN a contract is fetched, never WHAT is stored (reorders, never
        re-fetches differently).
        """
        frames: dict[str, dict[str, pl.DataFrame]] = {}
        rows: dict[str, set[tuple[str, str, str, str, str, str, int]]] = {}
        for label, kinds in (("stepped", _KINDS), ("legacy", None)):
            root = tmp_path / label
            registry = _seed_registry(root, symbols=_FOUR_BUCKET_LEGS)
            pull_contract_candles(
                registry.list_contracts(),
                lake_root=root / "lake",
                dry_run=False,
                intervals=("1minute",),
                registry=registry,
                from_date=_FROM,
                to_date=_TO,
                client_factory=lambda _root: _factory(_FakeSDK(), _real_sink(_root)),
                now=_NOW,
                underlying_kinds=kinds,
            )
            frames[label] = {
                str(p.relative_to(root / "lake")): pl.read_parquet(p).sort("timestamp")
                for p in sorted((root / "lake" / "bars").rglob("*.parquet"))
            }
            rows[label] = {
                (
                    r.market,
                    r.security_id,
                    r.interval,
                    r.window_start,
                    r.window_end,
                    r.status,
                    r.bars,
                )
                for r in load_window_ledger(root / "lake").records()
            }
        assert set(frames["stepped"]) == set(frames["legacy"])
        for rel_path, frame in frames["stepped"].items():
            assert frame.equals(frames["legacy"][rel_path]), rel_path
        assert rows["stepped"] == rows["legacy"]


class TestStepBucketEdges:
    """Edges 3/6/7/13 + the legacy degrade + the unresolved-class fallback."""

    def test_futures_only_selection_has_no_options_steps(self, tmp_path: Path) -> None:
        """Edge 6: a future-but-no-options selection never mints an options bucket."""
        registry = _seed_registry(
            tmp_path,
            symbols=(("NIFTY", _NIFTY_FUT, "FUT"), ("ADANIENT", _ADANI_FUT, "FUT")),
        )
        with capture_logs() as logs:
            pull_contract_candles(
                registry.list_contracts(),
                lake_root=tmp_path / "lake",
                dry_run=False,
                intervals=("1minute",),
                registry=registry,
                from_date=_FROM,
                to_date=_TO,
                client_factory=lambda _root: _factory(_FakeSDK(), _RecordingSink()),
                now=_NOW,
                underlying_kinds=_KINDS,
            )
        started = _events(logs, "pull.step.started")
        assert [e["step"] for e in started] == ["indices-futures", "stocks-futures"]

    def test_far_expiry_all_skipped_bucket_absent(self, tmp_path: Path) -> None:
        """Edge 7: a not-yet-open selection has NO buckets — a SUCCESS-shaped no-op."""
        store = RegistryStore.open(tmp_path / "registry.duckdb")
        far_expiry = date(2030, 12, 31)  # window opens ~2030-12-01, envelope ends 2026
        store.upsert_contract(
            "NSE",
            "NIFTY",
            far_expiry,
            "NSE-NIFTY-31Dec30-FUT",
            side="FUT",
            strike=None,
            run_id=_RUN_ID,
            fetched_at=_FETCHED,
        )
        sdk = _FakeSDK()
        with capture_logs() as logs:
            report = pull_contract_candles(
                store.list_contracts(),
                lake_root=tmp_path / "lake",
                dry_run=False,
                intervals=("1minute",),
                registry=store,
                # NO explicit dates — the strict default window law applies.
                client_factory=lambda _root: _factory(sdk, _RecordingSink()),
                now=_NOW,
                underlying_kinds=_KINDS,
            )
        assert report.contracts_not_yet_in_window == 1
        assert report.still_failed_windows == 0
        assert report.auth_aborted is False
        assert sdk.candle_symbols == []  # zero vendor calls
        assert _events(logs, "pull.step.started") == []  # the bucket never exists

    def test_alias_and_hyphen_classification_join(self) -> None:
        """Edges 3/13: NIFTYJR/BAJAJ-AUTO classify via the resolved registry keys."""
        kinds = _underlying_kind_map(
            [
                _instrument("NIFTYJR", InstrumentType.IDX),
                _instrument("BAJAJ-AUTO", InstrumentType.EQ),
            ]
        )
        # The map keys are the RESOLVED registry keys the metadata phase wrote under.
        assert kinds == {"NIFTYNXT50": True, "BAJAJAUTO": False}
        # A registry row (written under the resolved key) joins by construction.
        fut_row = _row("NIFTYNXT50", "NSE-NIFTYNXT50-25Jan24-FUT", "FUT")
        bucket, resolved = hp._contract_bucket(fut_row, kinds)
        assert bucket == (GROWW_FUTURE_COVERAGE_MARKET, True)
        assert resolved is True
        opt_row = _row("BAJAJAUTO", "NSE-BAJAJAUTO-25Jan24-9000-CE", "CE")
        bucket, resolved = hp._contract_bucket(opt_row, kinds)
        assert bucket == (GROWW_OPTION_COVERAGE_MARKET, False)
        assert resolved is True

    def test_underlying_kind_map_first_wins_on_collision(self) -> None:
        """Two spot spellings sharing one F&O primary: the FIRST classification wins."""
        kinds = _underlying_kind_map(
            [
                _instrument("NIFTYJR", InstrumentType.IDX),
                _instrument("NIFTYNXT50", InstrumentType.EQ),  # same resolved key
            ]
        )
        assert kinds == {"NIFTYNXT50": True}

    def test_kinds_none_degrades_to_legacy_two_bucket_order(self, tmp_path: Path) -> None:
        """A legacy caller (no map): FUT-then-OPT, one INFO, zero unresolved noise."""
        registry = _seed_registry(tmp_path, symbols=_FOUR_BUCKET_LEGS)
        sdk = _FakeSDK()
        with capture_logs() as logs:
            report = pull_contract_candles(
                registry.list_contracts(),
                lake_root=tmp_path / "lake",
                dry_run=False,
                intervals=("1minute",),
                registry=registry,
                from_date=_FROM,
                to_date=_TO,
                client_factory=lambda _root: _factory(sdk, _RecordingSink()),
                now=_NOW,
                # underlying_kinds deliberately OMITTED — the legacy caller shape.
            )
        # Every FUT fetch precedes every option fetch (the legacy 2-bucket law).
        fut_positions = [
            i for i, s in enumerate(sdk.candle_symbols) if s in {_NIFTY_FUT, _ADANI_FUT}
        ]
        opt_positions = [i for i, s in enumerate(sdk.candle_symbols) if s in {_NIFTY_CE, _ADANI_CE}]
        assert max(fut_positions) < min(opt_positions)
        # One INFO announces the degrade; nothing is counted unresolved (no WARN).
        assert len(_events(logs, "fno.step_kinds_absent")) == 1
        assert _events(logs, "fno.step_unclassified") == []
        assert report.class_unresolved == 0
        # Everything classifies stock-side → only the stocks-labelled steps run.
        started = _events(logs, "pull.step.started")
        assert [e["step"] for e in started] == ["stocks-futures", "stocks-options"]

    def test_unknown_underlying_stock_side_warn_count_note(self, tmp_path: Path) -> None:
        """Fail-closed for ORDER, never for DATA: unknowns pull stock-side + honest count."""
        registry = _seed_registry(tmp_path, symbols=_FOUR_BUCKET_LEGS)
        sdk = _FakeSDK()
        with capture_logs() as logs:
            report = pull_contract_candles(
                registry.list_contracts(),
                lake_root=tmp_path / "lake",
                dry_run=False,
                intervals=("1minute",),
                registry=registry,
                from_date=_FROM,
                to_date=_TO,
                client_factory=lambda _root: _factory(sdk, _RecordingSink()),
                now=_NOW,
                underlying_kinds={"NIFTY": True},  # ADANIENT is absent → unresolved
            )
        # The unresolved legs are STILL pulled (never dropped, never a crash)…
        assert set(sdk.candle_symbols) == {_NIFTY_FUT, _NIFTY_CE, _ADANI_FUT, _ADANI_CE}
        # …ordered stock-side (the LATER bucket — never ahead of an index step).
        assert sdk.candle_symbols.index(_NIFTY_FUT) < sdk.candle_symbols.index(_ADANI_FUT)
        assert sdk.candle_symbols.index(_NIFTY_CE) < sdk.candle_symbols.index(_ADANI_CE)
        # ONE WARN per underlying; the report counts LEGS; the note names them.
        warns = _events(logs, "fno.step_unclassified")
        assert [w["underlying"] for w in warns] == ["ADANIENT"]
        assert report.class_unresolved == 2
        assert report.to_dict()["class_unresolved"] == 2
        assert "2 contract(s) with unresolved index/stock class (ordered as stocks)" in report.note

    def test_contract_bucket_kinds_none_is_resolved_stock_side(self) -> None:
        """``underlying_kinds=None`` classifies stock-side WITHOUT the unresolved flag."""
        bucket, resolved = hp._contract_bucket(_row("NIFTY", _NIFTY_FUT, "FUT"), None)
        assert bucket == (GROWW_FUTURE_COVERAGE_MARKET, False)
        assert resolved is True


class TestStepMajorUntilClean:
    """Step-scoped retries + auth / mass-empty halt propagation."""

    def test_step3_retry_lands_before_step4_first_fetch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transiently-failing step-3 window retries WITHIN step 3 (round-major pin)."""
        monkeypatch.setattr("brutex.adapters.ingest_common._time_sleep", lambda _s: None)
        registry = _seed_registry(tmp_path, symbols=_FOUR_BUCKET_LEGS)
        sdk = _FakeSDK(raise_for={_NIFTY_FUT: (GrowwAPITimeoutException("timed out"), 1)})
        report = pull_contract_candles(
            registry.list_contracts(),
            lake_root=tmp_path / "lake",
            dry_run=False,
            intervals=("1minute",),
            registry=registry,
            from_date=_FROM,
            to_date=_TO,
            client_factory=lambda _root: _factory(sdk, _RecordingSink()),
            now=_NOW,
            underlying_kinds=_KINDS,
            until_clean=True,
            step_monitor=StepOrderMonitor(raise_on_violation=True),
        )
        # The NIFTY-FUT retry (its SECOND fetch) lands BEFORE step 4's first fetch.
        nifty_fut_fetches = [i for i, s in enumerate(sdk.candle_symbols) if s == _NIFTY_FUT]
        assert len(nifty_fut_fetches) >= 2  # the transient + the successful retry
        first_adani_fut = sdk.candle_symbols.index(_ADANI_FUT)
        assert nifty_fut_fetches[-1] < first_adani_fut
        assert report.still_failed_windows == 0
        assert report.contracts_completed == 4

    def test_auth_halt_in_step3_halts_steps_4_to_6(self, tmp_path: Path) -> None:
        """Edge 8 (F&O arm): a dead token in step 3 halts steps 4-6 loudly."""
        registry = _seed_registry(tmp_path, symbols=_FOUR_BUCKET_LEGS)
        sdk = _FakeSDK(raise_for={_NIFTY_FUT: (GrowwAPIAuthenticationException("dead"), 99)})
        with capture_logs() as logs:
            report = pull_contract_candles(
                registry.list_contracts(),
                lake_root=tmp_path / "lake",
                dry_run=False,
                intervals=("1minute",),
                registry=registry,
                from_date=_FROM,
                to_date=_TO,
                client_factory=lambda _root: _factory(sdk, _RecordingSink()),
                now=_NOW,
                underlying_kinds=_KINDS,
            )
        assert report.auth_aborted is True
        # Steps 4-6 are each announced halted; none of their contracts fetched.
        halted = _events(logs, "pull.step.halted")
        assert [e["step"] for e in halted] == [
            "stocks-futures",
            "indices-options",
            "stocks-options",
        ]
        assert [e["ordinal"] for e in halted] == [4, 5, 6]
        assert all(e["reason"] == "auth_expired" for e in halted)
        assert set(sdk.candle_symbols) == {_NIFTY_FUT}
        started = _events(logs, "pull.step.started")
        assert [e["step"] for e in started] == ["indices-futures"]

    def test_mass_empty_halts_phase_and_announces_remaining_steps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tripped breaker halts the WHOLE phase; the un-started steps are announced."""
        monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")  # synchronous tails
        monkeypatch.setenv("BRUTEX_EMPTY_BREAKER_CONSECUTIVE", "0")
        monkeypatch.setenv("BRUTEX_EMPTY_BREAKER_MIN_OBSERVATIONS", "2")
        monkeypatch.setenv("BRUTEX_EMPTY_BREAKER_FRACTION", "1.0")
        monkeypatch.setenv("BRUTEX_EMPTY_BREAKER_FRACTION_MIN_SERIES", "1")
        # TWO stock futures so the trip fires INSIDE step 4 (per-bucket breaker:
        # step 3's single window is only its bucket's 1st observation).
        registry = _seed_registry(tmp_path, symbols=_FOUR_BUCKET_LEGS)
        registry.upsert_contract(
            "NSE",
            "ADANIENT",
            date(2024, 2, 29),
            "NSE-ADANIENT-29Feb24-FUT",
            side="FUT",
            strike=None,
            run_id=_RUN_ID,
            fetched_at=_FETCHED,
        )
        with capture_logs() as logs:
            report = pull_contract_candles(
                registry.list_contracts(),
                lake_root=tmp_path / "lake",
                dry_run=False,
                intervals=("1minute",),
                registry=registry,
                from_date=_FROM,
                to_date=_TO,
                client_factory=lambda _root: _factory(
                    cast("_FakeSDK", _EmptySDK()), _RecordingSink()
                ),
                now=_NOW,
                underlying_kinds=_KINDS,
            )
        assert report.mass_empty_cause is not None
        assert report.verdict.kind == "mass_empty_halt"
        assert report.phase1_clean is False
        started = _events(logs, "pull.step.started")
        assert [e["step"] for e in started] == ["indices-futures", "stocks-futures"]
        halted = _events(logs, "pull.step.halted")
        assert [e["step"] for e in halted] == ["indices-options", "stocks-options"]
        assert all(e["reason"] == "mass_empty_halt" for e in halted)
        # Committed work kept: step 3's settled window survives in the ledger.
        ledger_rows = load_window_ledger(tmp_path / "lake").records()
        assert any(r.security_id == _NIFTY_FUT for r in ledger_rows)


class TestReportSurface:
    """The new report field rides the dataclass + JSON surface."""

    def test_class_unresolved_default_zero(self) -> None:
        report = HistoricalPullReport(
            dry_run=True,
            intervals=("1minute",),
            contracts_selected=0,
            contracts_completed=0,
            bars_appended=0,
            windows=0,
            failed_pairs=0,
            still_failed_windows=0,
            auth_aborted=False,
            note="",
            phase1_clean=True,
            phase1_complete=(),
            start_dates={},
            minute_requested=True,
            minute_skipped=False,
        )
        assert report.class_unresolved == 0
        assert report.to_dict()["class_unresolved"] == 0


class TestGreeksStrictGate:
    """The strict Greeks arm at the seam (DEC-PULL-SIX-STEP-ORDER-001).

    Matrix over (prior_clean × F&O-local clean × strict): a strict-withheld
    pass never runs the enrichment (``_GREEKS_STRICT_WITHHELD`` + one WARN;
    the note reads "withheld (strict gate)", never FAILED); the cleanset /
    ``prior_steps=None`` paths keep today's behavior byte-identical.
    """

    @staticmethod
    def _enrich_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
        """Spy on ``_enrich_greeks_from_minute`` — records calls, returns ok."""
        calls: list[dict[str, object]] = []

        def _spy(**kwargs: object) -> object:
            calls.append(kwargs)
            return hp._GreeksEnrichOutcome(ok=True, error=None, errored_targets=0)

        monkeypatch.setattr(hp, "_enrich_greeks_from_minute", _spy)
        return calls

    def _pull(
        self,
        tmp_path: Path,
        *,
        sdk: object | None = None,
        prior_steps: PriorStepsVerdict | None = None,
    ) -> HistoricalPullReport:
        registry = _seed_registry(tmp_path, symbols=_FOUR_BUCKET_LEGS)
        return pull_contract_candles(
            registry.list_contracts(),
            lake_root=tmp_path / "lake",
            dry_run=False,
            intervals=("1minute",),
            registry=registry,
            from_date=_FROM,
            to_date=_TO,
            client_factory=lambda _root: _factory(sdk or _FakeSDK(), _RecordingSink()),
            now=_NOW,
            underlying_kinds=_KINDS,
            prior_steps=prior_steps,
        )

    def test_strict_all_clean_enriches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._enrich_spy(monkeypatch)
        report = self._pull(
            tmp_path,
            prior_steps=PriorStepsVerdict(spot_clean=True, metadata_clean=True, strict=True),
        )
        assert len(calls) == 1  # clean everywhere → the pass RUNS under strict
        assert report.greeks_enrich_ok is True
        assert "withheld" not in report.note

    def test_strict_prior_unclean_withholds_with_warn_and_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._enrich_spy(monkeypatch)
        with capture_logs() as logs:
            report = self._pull(
                tmp_path,
                prior_steps=PriorStepsVerdict(spot_clean=False, metadata_clean=True, strict=True),
            )
        assert calls == []  # WITHHELD — the pass never ran
        assert report.greeks_enrich_ok is None  # not-run, NEVER a FAILED verdict
        assert report.greeks_enrich_error is None
        assert ", Greeks enrichment withheld (strict gate) — re-attempted next pull" in report.note
        assert "Greeks enrichment FAILED" not in report.note
        warns = _events(logs, "pipeline.greeks.strict_skipped")
        assert len(warns) == 1
        assert warns[0]["prior_clean"] is False
        assert warns[0]["fno_clean"] is True

    def test_strict_fno_unclean_withholds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._enrich_spy(monkeypatch)
        sdk = _FakeSDK(raise_for={_NIFTY_FUT: (GrowwAPIAuthenticationException("dead"), 99)})
        with capture_logs() as logs:
            report = self._pull(
                tmp_path,
                sdk=sdk,
                prior_steps=PriorStepsVerdict(spot_clean=True, metadata_clean=True, strict=True),
            )
        assert report.auth_aborted is True
        assert calls == []  # the F&O phase itself is unclean → withheld
        warns = _events(logs, "pipeline.greeks.strict_skipped")
        assert len(warns) == 1
        assert warns[0]["prior_clean"] is True
        assert warns[0]["fno_clean"] is False

    def test_cleanset_prior_unclean_still_enriches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._enrich_spy(monkeypatch)
        report = self._pull(
            tmp_path,
            prior_steps=PriorStepsVerdict(spot_clean=False, metadata_clean=True, strict=False),
        )
        assert len(calls) == 1  # strict OFF ⇒ today's best-effort pass, byte-identical
        assert report.greeks_enrich_ok is True
        assert "withheld" not in report.note

    def test_prior_steps_none_enriches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._enrich_spy(monkeypatch)
        report = self._pull(tmp_path)  # the legacy / non-pipeline caller default
        assert len(calls) == 1
        assert report.greeks_enrich_ok is True
        assert "withheld" not in report.note
