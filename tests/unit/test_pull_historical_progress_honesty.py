# pyright: reportPrivateUsage=none
"""DEC-PULL-PROGRESS-HONESTY-001 — live-progress wiring through ``pull_contract_candles``.

Fails-before framing (verified on origin/main ``23dbf08``):

* **Frozen bars.** In async (fetch-pipeline-wired) mode every per-pair
  ``IngestReport`` is producer-resolved with ``bars_appended == 0`` — the real
  counts are folded by the background consumer — so a dashboard bars counter had
  nothing to read and froze for the whole F&O phase. The pinned invariant here: a
  pair folded once contributes its delta EXACTLY once, in BOTH modes (async: the
  fold carries the real delta while the per-pair tick adds an honest 0; sync: the
  per-pair tick carries the real bars and no fold exists).
* **Polluted denominator.** The durable WindowLedger is CROSS-RUN and CROSS-MARKET
  (spot + F&O share it), so an unfiltered fold would count the spot phase's
  windows in the F&O denominator — the planted-spot-row regression pins the
  market/interval filter.

Offline throughout: the fake ``GrowwSDK`` + recording sink + real temp
``RegistryStore`` shapes of ``test_pull_historical.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from brutex.dashboard.historical_pull import pull_contract_candles
from brutex.data.fetch_coverage import WindowKey
from brutex.data.registry_store import RegistryStore
from brutex.data.sink import BarSink, SinkAppendResult
from brutex.data.window_ledger_store import begin_pull_ledger, end_pull_ledger

pytest.importorskip("duckdb", reason="RegistryStore needs duckdb")

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_FROM = date(2024, 1, 1)
_TO = date(2024, 1, 20)
_RUN_ID = "progress-honesty-test"
_FETCHED = datetime(2026, 1, 1, tzinfo=UTC)


class _FakeSDK:
    """Canned-candle Groww SDK fake (one bar per fetch) — offline, deterministic.

    Carries the full ``GrowwSDK`` Protocol surface; the enumeration methods MUST
    never be called (the contracts come from the registry, not a re-enumeration).
    """

    def __init__(self) -> None:
        self.candle_symbols: list[str] = []

    def get_expiries(self, **_kw: object) -> dict[str, object]:
        raise AssertionError("chain enumeration must never run in this pull")

    def get_contracts(self, **_kw: object) -> dict[str, object]:
        raise AssertionError("chain enumeration must never run in this pull")

    def get_historical_candles(self, **kw: object) -> dict[str, object]:
        self.candle_symbols.append(str(kw["groww_symbol"]))
        return {"candles": [["2024-01-15T09:15:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]}


class _RecordingSink:
    """Minimal recording :class:`BarSink` reporting every bar appended."""

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
        self.appends.append((segment, symbol, interval, len(bars)))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


def _client_factory(sdk: _FakeSDK, sink: _RecordingSink) -> object:
    from brutex.adapters.groww.client import GrowwClient

    def _factory(_root: Path) -> tuple[GrowwClient, BarSink]:
        return GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1), sink

    return _factory


@pytest.fixture
def registry(tmp_path: Path) -> RegistryStore:
    """A real temp registry seeded with the 3-leg NIFTY chain (CE + PE + FUT)."""
    store = RegistryStore.open(tmp_path / "registry.duckdb")
    expiry = date(2024, 1, 25)
    for symbol, side, strike in (
        ("NSE-NIFTY-25Jan24-21000-CE", "CE", 21000.0),
        ("NSE-NIFTY-25Jan24-21000-PE", "PE", 21000.0),
        ("NSE-NIFTY-25Jan24-FUT", "FUT", None),
    ):
        store.upsert_contract(
            "NSE",
            "NIFTY",
            expiry,
            symbol,
            side=side,
            strike=strike,
            run_id=_RUN_ID,
            fetched_at=_FETCHED,
        )
    return store


@pytest.fixture
def tmp_lake(tmp_path: Path) -> Path:
    root = tmp_path / "lake"
    root.mkdir()
    return root


def _pull(
    registry: RegistryStore,
    *,
    lake_root: Path,
    dry_run: bool,
    on_bars: object = None,
    on_ledger_sync: object = None,
) -> object:
    sdk = _FakeSDK()
    sink = _RecordingSink()
    return pull_contract_candles(
        registry.list_contracts(underlying="NIFTY"),
        lake_root=lake_root,
        dry_run=dry_run,
        intervals=("1day",),
        registry=registry,
        from_date=_FROM,
        to_date=_TO,
        client_factory=_client_factory(sdk, sink),  # type: ignore[arg-type]
        now=_NOW,
        on_bars=on_bars,  # type: ignore[arg-type]
        on_ledger_sync=on_ledger_sync,  # type: ignore[arg-type]
    )


# ── item 2: the live bars counter — exactly-once in BOTH modes ─────────────────


def test_sync_mode_on_bars_carries_the_per_pair_real_bars(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
) -> None:
    """SYNC mode (pipeline OFF): the per-pair report bars are the ONLY on_bars source.

    Fails-before: dropping ``pair_progress`` alone would have re-frozen the
    sync-mode bars headline (the per-pair advance no longer counted them) — the
    ``_progress`` forward is what keeps sync mode live.
    """
    monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")
    deltas: list[int] = []
    report = _pull(registry, lake_root=tmp_lake, dry_run=False, on_bars=deltas.append)
    bars_appended = getattr(report, "bars_appended", None)
    assert bars_appended == 3  # one canned bar per contract
    assert sum(deltas) == 3  # exactly once each — never doubled
    assert [d for d in deltas if d > 0] == [1, 1, 1]


def test_async_mode_on_bars_advances_by_exactly_the_folded_deltas(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
) -> None:
    """ASYNC mode (pipeline default ON): the fold deltas drive the counter, once each.

    Fails-before (the frozen-counter bug): the async per-pair reports carry 0
    bars, so before this seam the counter never advanced during the F&O phase.
    Now the pipeline consumer's fold invokes on_bars with each window's real
    delta, while the per-pair ticks forward honest 0s — the SUM equals the
    reconciled report total exactly (a double count would read 6).
    """
    monkeypatch.delenv("BRUTEX_GROWW_FETCH_PIPELINE", raising=False)  # default ON
    deltas: list[int] = []
    report = _pull(registry, lake_root=tmp_lake, dry_run=False, on_bars=deltas.append)
    assert getattr(report, "bars_appended", None) == 3
    assert sum(deltas) == 3  # the no-double-count invariant
    assert [d for d in deltas if d > 0] == [1, 1, 1]  # one real delta per folded window


# ── item 3: the honest, F&O-scope-filtered ledger denominator ──────────────────


def test_on_ledger_sync_folds_only_the_pulls_own_fno_scope(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, tmp_lake: Path
) -> None:
    """A planted SPOT ledger row never pollutes the F&O progress denominator.

    Fails-before: ``progress_counts()`` was unfiltered, so the cross-run,
    cross-market ledger's spot windows would inflate the F&O total (here 4
    instead of 3) — the exact stale-illusion the market/interval filter kills.
    """
    monkeypatch.setenv("BRUTEX_GROWW_FETCH_PIPELINE", "off")
    # Plant a durable SPOT row in the SAME ledger the pull will reopen.
    seeded = begin_pull_ledger(tmp_lake, dry_run=False)
    assert seeded is not None
    seeded.record(
        WindowKey(
            market="groww",
            security_id="NIFTY",
            interval="1minute",
            start=_FROM,
            end=_TO,
        ),
        raw_bars=999,
        error_type=None,
        bars_appended=999,
        now=_NOW,
    )
    end_pull_ledger(seeded, tmp_lake, dry_run=False)

    syncs: list[tuple[int, int, int, int, str]] = []

    def _on_sync(resolved: int, total: int, bars: int, failed: int, current: str) -> None:
        syncs.append((resolved, total, bars, failed, current))

    _pull(registry, lake_root=tmp_lake, dry_run=False, on_ledger_sync=_on_sync)
    assert syncs, "the final post-drain push must always fire on a live ledger"
    # EVERY push is F&O-scoped: the planted spot window (and its 999 bars) never
    # appears in any denominator/bars figure.
    assert all(total <= 3 for _res, total, _bars, _failed, _cur in syncs)
    resolved, total, bars, failed, _current = syncs[-1]
    assert (resolved, total, failed) == (3, 3, 0)
    assert bars == 3  # never 3 + the spot 999


def test_dry_run_never_fires_ledger_sync_and_on_bars_is_harmless(
    registry: RegistryStore, tmp_lake: Path
) -> None:
    """A dry-run has NO ledger — the sync hook must stay silent (no fabricated counts)."""
    syncs: list[tuple[int, int, int, int, str]] = []
    deltas: list[int] = []

    def _on_sync(resolved: int, total: int, bars: int, failed: int, current: str) -> None:
        syncs.append((resolved, total, bars, failed, current))

    report = _pull(
        registry,
        lake_root=tmp_lake,
        dry_run=True,
        on_bars=deltas.append,
        on_ledger_sync=_on_sync,
    )
    assert syncs == []  # no ledger → an honest silence, never invented totals
    # The injected real-shaped factory still lands bars, so on_bars ticks per pair.
    assert sum(deltas) == getattr(report, "bars_appended", None) == 3
