"""SQL-side latest-wins ledger delta fold (DEC-LEDGER-DELTA-O-PAGE-001, sub-part a).

The ``/coverage/windows`` + pull-progress DuckDB fast paths no longer DEFER to
the O(records) Python fold while mid-pull ledger deltas are pending — they fold
``base + deltas`` latest-wins IN SQL via
:func:`brutex.data.lake_read.dedup_latest_source_sql`, provably equal to
``load_window_ledger``'s Python dict fold (base first, ascending delta-filename
replay, NEWER-FILE-wins per 5-field window key — never ``merge_ledger_rows``,
so demotions survive). This suite pins:

* T-a1  latest-wins upsert chain across three deltas (one row served, newest wins);
* T-a2  demotion survival (base SUCCESS + newer delta FAILED → FAILED);
* T-a3  ghost-row safety (filters apply OUTSIDE the fold subquery);
* T-a4  a lake rooted under a path literally containing ``deltas`` folds correctly
        (bound base-path equality, never a LIKE marker);
* T-a5  the save-time prune race self-heals through the retry's fresh re-listing;
* T-a6  a persistently malformed delta degrades to the fold-aware legacy path
        (window payload) / the Python-fold counts (phase progress) — never a 500,
        never blanked phases mid-pull;
* T-a7  a legacy 12-col base (± deltas) defers WHOLESALE via the base-only probe
        (the ``union_by_name`` NULL-fill → ``int(None)`` crash is unreachable);
* T-a8  a base-less delta-only lake is served by the FAST path;
* T-a9  the mid-pull fast payload equals the Python fold field-for-field on
        counts/count/matched/capped + row SET (the sanctioned within-tier row
        ORDER convergence to the DuckDB contract is asserted explicitly);
* T-a10 the steady-state (no-deltas) SQL text is BYTE-identical to the pre-fold
        fast path (spy conn) + payload golden vs the legacy fold;
* count-cache teeth: composite-stamp keys recount on a delta append AND a
  cross-process base save, never on a mark_dirty/generation storm;
* a hypothesis parity oracle: random upsert sequences across base + N deltas →
  SQL fold ≡ the reference Python dict fold, record-for-record;
* drift pins: ``_WINDOW_KEY_COLUMNS == window_ledger_store._COLUMNS[:5]`` and
  ``append_ledger_delta`` writing the full 16-column schema (the base-less
  probe-skip's safety precondition).

Tests MAY import ``window_ledger_store`` (the module itself must not — the
pinned no-import edge, verified by ``test_db_view_no_window_ledger_store_import``).
"""

from __future__ import annotations

# Whitebox viewer tests read module-private helpers by design (DEC-P0-017 convention).
# pyright: reportPrivateUsage=false
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest
from brutex.dashboard.db_view import (
    _DUCKDB_RETRY_DELAY_S,
    _UNKNOWN_WINDOW_STATUS,
    _WINDOW_KEY_COLUMNS,
    WINDOW_PAGE_CACHE_CAPACITY,
    _build_window_payload_duckdb,
    _close_cursor,
    _compute_cursor,
    _ledger_scan_source,
    _ledger_stamp_token,
    _window_counts_from_records,
    _window_counts_total,
    _window_severity_order_sql,
    _window_symbol_map_token,
    build_window_payload,
    make_window_payload_caches,
    phase_progress_payload,
    query_window_ledger_page,
)
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data import window_ledger_store as wls
from brutex.data.fetch_coverage import WindowKey
from brutex.data.lake_read import dedup_latest_source_sql
from brutex.data.window_ledger import WindowLedger
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

_NOW = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
_ISO = "2026-06-08T12:00:00+00:00"


def _window_key(security_id: str, interval: str = "1minute") -> WindowKey:
    return WindowKey(
        market="groww",
        security_id=security_id,
        interval=interval,
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )


def _row(
    security_id: str,
    status: str,
    *,
    interval: str = "1minute",
    bars: int = 0,
    attempts: int = 1,
) -> tuple[Any, ...]:
    """One 16-field ledger row tuple (the exact ``_COLUMNS`` shape).

    The trailing ``empty_retries``/``transient_retries`` pair is ``None`` — the
    honest-unknown optional-column default (DEC-RETRY-COUNTER-SPLIT-001,
    MIGRATION.md #1585), exactly what a pre-split ledger row loads as.
    """
    return (
        "groww",
        security_id,
        interval,
        "2021-01-01",
        "2021-04-01",
        status,
        bars,
        attempts,
        None,
        _ISO,
        None if status == "PLANNED" else _ISO,
        0,
        0 if status == "PLANNED" else 1,
        None,
        None,
        None,
    )


def _write_base(lake: Path, rows: Sequence[tuple[Any, ...]]) -> None:
    """Write a REAL 16-column base ledger Parquet directly (full status control)."""
    import pyarrow.parquet as pq

    path = wls.ledger_path(lake)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(wls._rows_to_table(list(rows)), path)  # type: ignore[no-untyped-call, unused-ignore]


def _write_delta(lake: Path, rows: Sequence[tuple[Any, ...]]) -> Path:
    """Append one REAL 16-column delta Parquet via the store's own name minting."""
    import pyarrow.parquet as pq

    directory = wls.deltas_dir(lake)
    directory.mkdir(parents=True, exist_ok=True)
    path = wls._next_delta_path(directory)
    pq.write_table(wls._rows_to_table(list(rows)), path)  # type: ignore[no-untyped-call, unused-ignore]
    return path


def _record_delta(lake: Path, security_id: str, *, raw_bars: int = 7) -> None:
    """Append a delta through the REAL production writer (``append_ledger_delta``)."""
    live = wls.load_window_ledger(lake)
    live.record(
        _window_key(security_id),
        raw_bars=raw_bars,
        error_type=None,
        bars_appended=raw_bars,
        now=_NOW,
    )
    assert wls.append_ledger_delta(live, lake) == 1


def _events(logs: Sequence[Mapping[str, Any]], event: str) -> list[Mapping[str, Any]]:
    return [entry for entry in logs if entry["event"] == event]


class _SpyConn:
    """A pass-through DuckDB connection that records every (sql, params) pair."""

    def __init__(self) -> None:
        self._real = duckdb.connect()
        self.calls: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> Any:
        self.calls.append((sql, params))
        return self._real.execute(sql, params)


# ── drift pins ────────────────────────────────────────────────────────────────


def test_window_key_columns_pinned_to_the_store_schema() -> None:
    """The fold identity MUST be the store's leading 5 columns (the ``row[0:5]`` key)."""
    assert wls._COLUMNS[:5] == _WINDOW_KEY_COLUMNS


def test_append_ledger_delta_emits_all_14_columns(tmp_path: Path) -> None:
    """The base-less probe-skip's precondition: EVERY delta carries the full schema."""
    import pyarrow.parquet as pq

    ledger = WindowLedger()
    ledger.record(_window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
    assert wls.append_ledger_delta(ledger, tmp_path) == 1
    (delta,) = wls.list_ledger_deltas(tmp_path)
    schema = pq.read_schema(delta)  # type: ignore[no-untyped-call, unused-ignore]
    assert tuple(schema.names) == wls._COLUMNS


def test_db_view_no_window_ledger_store_import() -> None:
    """The pinned module edge: db_view must NOT import window_ledger_store."""
    import ast

    source = (
        Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "db_view.py"
    ).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert (
                node.module != "brutex.data.window_ledger_store"
                or node.level != 0
                or (
                    # function-local lazy imports are the sanctioned exception —
                    # module-LEVEL edges are what CodeQL py/cyclic-import flags.
                    node.col_offset > 0
                )
            ), "module-level db_view → window_ledger_store import edge"


def test_db_view_no_lake_revision_import() -> None:
    """The pinned module edge: db_view must NOT import lake_revision — AT ANY LEVEL.

    CodeQL ``py/cyclic-import`` alert 690: the module-level
    ``db_view → lake_revision`` import (this PR's original home for
    ``revision_generation``) began a real 4-module cycle — ``lake_revision``
    lazily imports ``window_ledger_store`` (``_ledger_revision``), whose
    cold-start reconcile lazily imports ``coverage_reconcile``, which lazily
    imports ``db_view`` back. The bridge was therefore homed in the
    ``movers_cache`` LEAF (beside the cache whose generations it feeds).
    Unlike the ``window_ledger_store`` pin above, function-local imports are
    NOT exempted here: db_view has zero need for ANY ``lake_revision`` edge,
    and every closing edge of the flagged cycle was itself function-level —
    so a "lazy" re-add would re-enter the same cycle graph.
    """
    import ast

    source = (
        Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "db_view.py"
    ).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "brutex.dashboard.lake_revision" or node.level != 0, (
                "db_view → lake_revision import edge (CodeQL py/cyclic-import, alert 690)"
            )
            assert not (
                node.level == 0
                and node.module == "brutex.dashboard"
                and any(alias.name == "lake_revision" for alias in node.names)
            ), "db_view → lake_revision import edge via `from brutex.dashboard import …`"
        if isinstance(node, ast.Import):
            assert all(alias.name != "brutex.dashboard.lake_revision" for alias in node.names), (
                "db_view → lake_revision import edge (plain `import`)"
            )


# ── scan-source assembly + stamp token ────────────────────────────────────────


class TestLedgerScanSource:
    def test_steady_state_is_the_byte_identical_read_parquet_literal(self, tmp_path: Path) -> None:
        _write_base(tmp_path, [_row("NIFTY", "SUCCESS")])
        source = _ledger_scan_source(wls.ledger_path(tmp_path))
        assert source.from_sql == "read_parquet(?)"
        assert source.params == (wls.ledger_path(tmp_path).as_posix(),)

    def test_deltas_with_base_bind_files_then_marker(self, tmp_path: Path) -> None:
        _write_base(tmp_path, [_row("NIFTY", "SUCCESS")])
        delta = _write_delta(tmp_path, [_row("BANKNIFTY", "SUCCESS")])
        source = _ledger_scan_source(wls.ledger_path(tmp_path))
        base = wls.ledger_path(tmp_path).as_posix()
        assert source.from_sql == dedup_latest_source_sql(_WINDOW_KEY_COLUMNS)
        assert source.params == ([base, delta.as_posix()], base)

    def test_base_less_deltas_still_bind_the_marker(self, tmp_path: Path) -> None:
        delta = _write_delta(tmp_path, [_row("NIFTY", "SUCCESS")])
        source = _ledger_scan_source(wls.ledger_path(tmp_path))
        base = wls.ledger_path(tmp_path).as_posix()
        # The absent base is NOT in the file list, but the marker binds anyway —
        # it matches nothing and the deltas order among themselves (one code path).
        assert source.params == ([delta.as_posix()], base)

    def test_stamp_token_moves_on_append_and_save_never_on_nothing(self, tmp_path: Path) -> None:
        _write_base(tmp_path, [_row("NIFTY", "SUCCESS")])
        path = wls.ledger_path(tmp_path)
        first = _ledger_stamp_token(path)
        assert _ledger_stamp_token(path) == first  # unchanged ledger → same token
        _write_delta(tmp_path, [_row("BANKNIFTY", "SUCCESS")])
        second = _ledger_stamp_token(path)
        assert second != first  # a delta append moves the token
        _write_base(tmp_path, [_row("NIFTY", "SUCCESS")])
        assert _ledger_stamp_token(path) != second  # a base save moves it too

    def test_stamp_token_missing_base_stamps_zero(self, tmp_path: Path) -> None:
        token = _ledger_stamp_token(wls.ledger_path(tmp_path))
        assert token.startswith("0.0.")


# ── T-a1 / T-a2 / T-a3 / T-a4 — fold correctness on the real engine ──────────


class TestLatestWinsFold:
    def test_upsert_chain_across_three_deltas_newest_wins_once(self, tmp_path: Path) -> None:
        """T-a1: base PLANNED → EMPTY → FAILED → SUCCESS reads as ONE SUCCESS row."""
        _write_base(tmp_path, [_row("NIFTY", "PLANNED", attempts=0)])
        _write_delta(tmp_path, [_row("NIFTY", "EMPTY")])
        _write_delta(tmp_path, [_row("NIFTY", "FAILED")])
        _write_delta(tmp_path, [_row("NIFTY", "SUCCESS", bars=9)])
        payload = build_window_payload(tmp_path, conn=duckdb.connect())
        assert payload["count"] == 1
        assert payload["counts"]["spot"] == {"SUCCESS": 1}
        (row,) = payload["windows"]
        assert row["status"] == "SUCCESS"
        assert row["bars"] == 9

    def test_demotion_survives_the_fold(self, tmp_path: Path) -> None:
        """T-a2: base SUCCESS + newer delta FAILED → FAILED (the merge_ledger_rows teeth:
        a more-settled-wins merge would resurrect the SUCCESS — the WAL fold must not)."""
        _write_base(tmp_path, [_row("NIFTY", "SUCCESS", bars=5)])
        _write_delta(tmp_path, [_row("NIFTY", "FAILED")])
        payload = build_window_payload(tmp_path, conn=duckdb.connect())
        (row,) = payload["windows"]
        assert row["status"] == "FAILED"
        assert payload["counts"]["spot"] == {"FAILED": 1}

    def test_ghost_row_never_resurrects_through_a_filter(self, tmp_path: Path) -> None:
        """T-a3: WHERE applies OUTSIDE the fold — the superseded SUCCESS version of a
        demoted window can never match ``?status=SUCCESS``."""
        _write_base(tmp_path, [_row("NIFTY", "SUCCESS", bars=5)])
        _write_delta(tmp_path, [_row("NIFTY", "FAILED")])
        payload = build_window_payload(tmp_path, conn=duckdb.connect(), status="SUCCESS")
        assert payload["matched"] == 0
        assert payload["windows"] == []

    def test_lake_rooted_under_a_deltas_path_folds_correctly(self, tmp_path: Path) -> None:
        """T-a4: a lake whose OWN absolute path contains ``deltas`` — the trap that
        broke a LIKE-marker design — still demotes correctly (bound path equality)."""
        lake = tmp_path / "deltas" / "lake"
        lake.mkdir(parents=True)
        _write_base(lake, [_row("NIFTY", "SUCCESS", bars=5)])
        _write_delta(lake, [_row("NIFTY", "FAILED")])
        payload = build_window_payload(lake, conn=duckdb.connect())
        (row,) = payload["windows"]
        assert row["status"] == "FAILED"

    def test_base_filename_sorting_above_deltas_still_loses(self, tmp_path: Path) -> None:
        """The 'l' > 'd' lexicography trap: ``ledger.parquet`` outsorts ``deltas/000…``
        under a plain ``filename DESC`` — the bound base marker must force it last."""
        _write_base(tmp_path, [_row("NIFTY", "SUCCESS", bars=5)])
        delta = _write_delta(tmp_path, [_row("NIFTY", "FAILED")])
        assert wls.ledger_path(tmp_path).as_posix() > delta.as_posix()  # the trap is REAL
        (row,) = build_window_payload(tmp_path, conn=duckdb.connect())["windows"]
        assert row["status"] == "FAILED"


# ── T-a5 — save-time prune race self-heals via the retry's re-listing ─────────


class _OneShotDuckdbError:
    """Raise ``duckdb.Error`` on the first call, then delegate to the real counts."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, conn: Any, source: Any) -> tuple[dict[str, dict[str, int]], int]:
        self.calls += 1
        if self.calls == 1:
            raise duckdb.InvalidInputException("delta unlinked mid-query")
        return _window_counts_total(conn, source)


def test_prune_race_retry_relists_and_serves_the_subsumed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-a5: the first attempt dies on a (simulated) prune-race ``duckdb.Error``;
    the injected sleeper performs a REAL ``save_window_ledger`` (which subsumes +
    prunes the deltas); the retry re-lists inside ``_ledger_scan_source`` and
    serves the subsumed base-only state — one ``window_payload.duckdb_retry``,
    zero fallbacks."""
    ledger = WindowLedger()
    ledger.record(_window_key("NIFTY"), raw_bars=5, error_type=None, bars_appended=5, now=_NOW)
    wls.save_window_ledger(ledger, tmp_path)
    _record_delta(tmp_path, "BANKNIFTY")
    assert wls.list_ledger_deltas(tmp_path)  # the race precondition: deltas pending
    flaky = _OneShotDuckdbError()
    monkeypatch.setattr("brutex.dashboard.db_view._window_counts_total", flaky)

    def _prune(_delay: float) -> None:
        wls.save_window_ledger(wls.load_window_ledger(tmp_path), tmp_path)

    with capture_logs() as logs:
        payload = build_window_payload(tmp_path, conn=duckdb.connect(), retry_sleep=_prune)
    assert wls.list_ledger_deltas(tmp_path) == []  # the save really pruned
    assert {w["symbol"] for w in payload["windows"]} == {"NIFTY", "BANKNIFTY"}
    assert payload["count"] == 2
    assert len(_events(logs, "window_payload.duckdb_retry")) == 1
    assert _events(logs, "window_payload.duckdb_fallback") == []
    assert flaky.calls == 2  # fail once, retried success over the fresh listing
    # The retried answer equals the subsumed on-disk state's Python fold.
    assert payload == build_window_payload(tmp_path)


# ── T-a6 — persistently malformed delta degrades honestly ─────────────────────


class TestMalformedDeltaDegrade:
    @staticmethod
    def _poisoned_lake(lake: Path) -> None:
        _write_base(lake, [_row("NIFTY", "SUCCESS", bars=5)])
        _write_delta(lake, [_row("BANKNIFTY", "SUCCESS", bars=3)])
        directory = wls.deltas_dir(lake)
        garbage = wls._next_delta_path(directory)
        garbage.write_bytes(b"definitely not a parquet file")

    def test_window_payload_falls_back_to_the_fold_aware_legacy_path(self, tmp_path: Path) -> None:
        """T-a6 (window payload): both SQL attempts die on the garbage delta; the
        legacy Python fold SKIPS it file-granularly and serves every good row."""
        self._poisoned_lake(tmp_path)
        sleeps: list[float] = []
        with capture_logs() as logs:
            payload = build_window_payload(
                tmp_path, conn=duckdb.connect(), retry_sleep=sleeps.append
            )
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]
        assert payload["source"] == "ledger"
        assert {w["symbol"] for w in payload["windows"]} == {"NIFTY", "BANKNIFTY"}
        assert len(_events(logs, "window_payload.duckdb_retry")) == 1
        assert len(_events(logs, "window_payload.duckdb_fallback")) == 1
        assert len(_events(logs, "window_ledger.delta_load_failed")) == 1  # the skip

    def test_phase_progress_serves_python_fold_counts_not_blank(self, tmp_path: Path) -> None:
        """T-a6 (phase progress): with deltas pending, the double failure serves the
        resilient Python-fold counts — the pull-progress phases are never blanked."""
        self._poisoned_lake(tmp_path)
        sleeps: list[float] = []
        with capture_logs() as logs:
            rows = phase_progress_payload(
                duckdb.connect(),
                wls.ledger_path(tmp_path),
                current_phase="spot",
                retry_sleep=sleeps.append,
            )
        assert sleeps == [_DUCKDB_RETRY_DELAY_S]
        spot = next(r for r in rows if r["phase"] == "spot")
        assert spot["done"] == 2  # NIFTY (base) + BANKNIFTY (good delta)
        (fallback,) = _events(logs, "phase_progress.duckdb_fallback")
        assert fallback["fallback"] == "records"

    def test_phase_progress_base_only_keeps_the_honest_empty(self, tmp_path: Path) -> None:
        """T-a6 (base-only arm): a garbage BASE with NO deltas keeps today's ``[]``."""
        path = wls.ledger_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"definitely not a parquet file")
        sleeps: list[float] = []
        with capture_logs() as logs:
            rows = phase_progress_payload(
                duckdb.connect(), path, current_phase="spot", retry_sleep=sleeps.append
            )
        assert rows == []
        (fallback,) = _events(logs, "phase_progress.duckdb_fallback")
        assert fallback["fallback"] == "empty"


# ── T-a7 — legacy 12-col base defers WHOLESALE (probe stays base-only) ────────


def _write_legacy_base(lake: Path, security_id: str) -> None:
    """An 11-core-column pre-R1 base Parquet (no empty_streak/in_pull_fetches)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema([(name, pa.string()) for name in wls._CORE_COLUMNS[:6]])
    table = pa.table(
        {
            "market": ["groww"],
            "security_id": [security_id],
            "interval": ["1minute"],
            "window_start": ["2021-01-01"],
            "window_end": ["2021-04-01"],
            "status": ["EMPTY"],
        },
        schema=schema,
    ).append_column("bars", pa.array([0], pa.int64()))
    table = table.append_column("attempts", pa.array([5], pa.int64()))
    table = table.append_column("last_error", pa.array([None], pa.string()))
    table = table.append_column("first_seen_at", pa.array([_ISO], pa.string()))
    table = table.append_column("last_attempt_at", pa.array([_ISO], pa.string()))
    path = wls.ledger_path(lake)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)  # type: ignore[no-untyped-call, unused-ignore]


def test_legacy_base_with_deltas_defers_to_the_pyarrow_derive_path(
    tmp_path: Path,
) -> None:
    """T-a7: a legacy base + 14-col deltas → the BASE-only probe raises the
    deterministic ValueError → the WHOLE payload defers to the pyarrow path
    (which derives empty_streak/in_pull_fetches per row) — the union NULL-fill
    ``int(None)`` crash is structurally unreachable, and the sleeper never fires."""
    _write_legacy_base(tmp_path, "EMPTYCO")
    _write_delta(tmp_path, [_row("BANKNIFTY", "SUCCESS", bars=3)])
    sleeps: list[float] = []
    with capture_logs() as logs:
        payload = build_window_payload(
            tmp_path, conn=duckdb.connect(), status=None, retry_sleep=sleeps.append
        )
    assert sleeps == []  # deterministic shape error — never retried
    by_symbol = {w["symbol"]: w for w in payload["windows"]}
    assert set(by_symbol) == {"EMPTYCO", "BANKNIFTY"}
    assert by_symbol["EMPTYCO"]["empty_streak"] == 5  # derived (EMPTY → attempts)
    (fallback,) = _events(logs, "window_payload.duckdb_fallback")
    assert fallback["error_type"] == "ValueError"


# ── T-a8 — base-less delta-only lake rides the FAST path ─────────────────────


def test_base_less_delta_only_lake_served_by_the_fast_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-a8: a first pull's delta-only checkpoint is served by the SQL fold —
    the probe is SKIPPED (no base) and the legacy Python parse is NEVER touched."""
    _record_delta(tmp_path, "NIFTY")

    def _boom(*_a: object, **_k: object) -> tuple[Any, ...]:
        raise AssertionError("the fast path must not fall back to the Python fold")

    monkeypatch.setattr("brutex.dashboard.db_view._ledger_records_cached", _boom)
    payload = build_window_payload(tmp_path, conn=duckdb.connect())
    assert payload["source"] == "ledger"
    assert [w["symbol"] for w in payload["windows"]] == ["NIFTY"]
    assert payload["counts"]["spot"] == {"SUCCESS": 1}


# ── T-a9 — mid-pull fast path ≡ Python fold (fields + row set; order pinned) ──


def test_midpull_fast_path_equals_python_fold_on_fields_and_row_set(
    tmp_path: Path,
) -> None:
    """T-a9: counts/count/matched/capped and the row SET equal the Python fold;
    the within-severity-tier row ORDER converges to the documented steady-state
    DuckDB contract (security_id, interval, window_start, window_end) — the
    SANCTIONED change, asserted explicitly."""
    _write_base(
        tmp_path,
        [
            _row("NIFTY", "SUCCESS", bars=5),
            _row("ZEEL", "SUCCESS", bars=2),
            _row("SENSEX", "PLANNED", attempts=0),
        ],
    )
    _write_delta(tmp_path, [_row("BANKNIFTY", "FAILED"), _row("ZEEL", "EMPTY")])
    _write_delta(tmp_path, [_row("INFY", "SUCCESS", bars=1)])
    fast = build_window_payload(tmp_path, conn=duckdb.connect())
    legacy = build_window_payload(tmp_path)  # conn=None → the Python fold
    for field in ("source", "count", "counts", "matched", "capped", "empty_threshold"):
        assert fast[field] == legacy[field], field
    fast_rows = {tuple(sorted(w.items())) for w in fast["windows"]}
    legacy_rows = {tuple(sorted(w.items())) for w in legacy["windows"]}
    assert fast_rows == legacy_rows
    # The DuckDB order contract: severity tier first, then the 4-column key.
    assert [w["symbol"] for w in fast["windows"]] == ["BANKNIFTY", "ZEEL", "INFY", "NIFTY"]


def test_counts_since_pull_start_now_present_mid_pull(tmp_path: Path) -> None:
    """Sanctioned improvement: the per-run counts appear mid-pull too (the
    defer-to-legacy path omitted them)."""
    _write_base(tmp_path, [_row("NIFTY", "SUCCESS", bars=5)])
    _write_delta(tmp_path, [_row("BANKNIFTY", "SUCCESS", bars=3)])
    payload = build_window_payload(
        tmp_path, conn=duckdb.connect(), since="2026-06-08T11:00:00+00:00"
    )
    assert payload["pull_start_utc"] == "2026-06-08T11:00:00+00:00"
    assert payload["counts_since_pull_start"]["spot"] == {"SUCCESS": 2}


# ── T-a10 — steady-state SQL text byte-pin + payload golden ──────────────────


def test_steady_state_sql_text_is_byte_identical(tmp_path: Path) -> None:
    """T-a10: with NO deltas the fast path emits today's EXACT SQL strings with
    single-string binds — the zero-drift proof for the steady state."""
    _write_base(tmp_path, [_row("NIFTY", "SUCCESS", bars=5)])
    spy = _SpyConn()
    payload = build_window_payload(tmp_path, conn=spy)
    glob = wls.ledger_path(tmp_path).as_posix()
    expected_page = (
        f"SELECT * FROM read_parquet(?) WHERE status != ? ORDER BY "  # noqa: S608 — expected-text fixture, never executed as a query here
        f"{_window_severity_order_sql()}, "
        '"security_id", "interval", "window_start", "window_end" LIMIT ? OFFSET ?'
    )
    assert spy.calls == [
        ("SELECT * FROM read_parquet(?) LIMIT 0", [glob]),
        ("SELECT market, status, COUNT(*) FROM read_parquet(?) GROUP BY market, status", [glob]),
        (expected_page, [glob, "PLANNED", 20_000, 0]),
    ]
    # Payload golden: byte-parity with the legacy Python fold.
    assert payload == build_window_payload(tmp_path)


# ── count-cache teeth — the composite stamp token is the COMPLETE signal ──────


def test_count_cache_recounts_on_stamp_moves_never_on_generation_storms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_base(tmp_path, [_row("NIFTY", "SUCCESS", bars=5)])
    conn = duckdb.connect()
    cache = MoversSnapshotCache()
    calls = {"n": 0}
    original = _window_counts_total

    def _counting(c: object, source: object) -> tuple[dict[str, dict[str, int]], int]:
        calls["n"] += 1
        return original(c, source)  # type: ignore[arg-type]

    monkeypatch.setattr("brutex.dashboard.db_view._window_counts_total", _counting)
    # A generation/mark_dirty STORM at a fixed stamp → ONE count, pure hits.
    for generation in range(1, 21):
        build_window_payload(tmp_path, conn=conn, count_cache=cache, generation=generation)
    assert calls["n"] == 1
    # A mid-pull delta APPEND (base mtime untouched) → exactly one recount.
    _write_delta(tmp_path, [_row("BANKNIFTY", "SUCCESS", bars=3)])
    build_window_payload(tmp_path, conn=conn, count_cache=cache, generation=1)
    build_window_payload(tmp_path, conn=conn, count_cache=cache, generation=2)
    assert calls["n"] == 2
    # A CLI-style CROSS-PROCESS full save (new base mtime + pruned deltas) → recount.
    wls.save_window_ledger(wls.load_window_ledger(tmp_path), tmp_path)
    build_window_payload(tmp_path, conn=conn, count_cache=cache, generation=2)
    assert calls["n"] == 3


def test_phase_progress_shares_the_same_stamp_key_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two routes share ONE warm entry: a window-payload count warms the
    phase-progress poll at the same stamp (and vice versa)."""
    _write_base(tmp_path, [_row("NIFTY", "SUCCESS", bars=5)])
    _write_delta(tmp_path, [_row("BANKNIFTY", "SUCCESS", bars=3)])
    conn = duckdb.connect()
    cache = MoversSnapshotCache()
    calls = {"n": 0}
    original = _window_counts_total

    def _counting(c: object, source: object) -> tuple[dict[str, dict[str, int]], int]:
        calls["n"] += 1
        return original(c, source)  # type: ignore[arg-type]

    monkeypatch.setattr("brutex.dashboard.db_view._window_counts_total", _counting)
    build_window_payload(tmp_path, conn=conn, count_cache=cache, generation=1)
    rows = phase_progress_payload(
        conn, wls.ledger_path(tmp_path), current_phase="spot", count_cache=cache, generation=2
    )
    assert calls["n"] == 1  # the phase poll was a pure HIT on the shared entry
    spot = next(r for r in rows if r["phase"] == "spot")
    assert spot["done"] == 2  # and it sees the delta row (the SQL fold answered)


def test_phase_progress_midpull_sql_fold_no_python_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-pull the phase rows come from the SQL fold — ZERO Python ledger parses
    (the deleted O(records) delta branch can never silently return)."""
    _write_base(tmp_path, [_row("NIFTY", "SUCCESS", bars=5)])
    _write_delta(tmp_path, [_row("BANKNIFTY", "SUCCESS", bars=3)])

    def _boom(*_a: object, **_k: object) -> Any:
        raise AssertionError("phase progress must not parse the ledger in Python mid-pull")

    monkeypatch.setattr("brutex.data.window_ledger_store.load_window_ledger", _boom)
    rows = phase_progress_payload(duckdb.connect(), wls.ledger_path(tmp_path), current_phase="spot")
    spot = next(r for r in rows if r["phase"] == "spot")
    assert spot["done"] == 2


# ── hypothesis parity oracle — SQL fold ≡ Python dict fold ────────────────────

_KEYS = ("S0", "S1", "S2", "S3")
_STATUSES = ("PLANNED", "SUCCESS", "EMPTY", "FAILED", "SKIPPED")

_segment = st.dictionaries(
    st.sampled_from(_KEYS), st.sampled_from(_STATUSES), min_size=0, max_size=len(_KEYS)
)


@settings(max_examples=25, deadline=None)
@given(base=_segment, deltas=st.lists(_segment, min_size=0, max_size=3))
def test_property_sql_fold_equals_python_fold(
    tmp_path_factory: pytest.TempPathFactory,
    base: dict[str, str],
    deltas: list[dict[str, str]],
) -> None:
    """Random upsert sequences across base + N deltas: the SQL fold's record set
    and GROUP BY counts equal ``load_window_ledger``'s Python dict fold."""
    if not base and not any(deltas):
        return  # no files at all — both readers are gated off upstream
    lake = tmp_path_factory.mktemp("fold")
    if base:
        _write_base(lake, [_row(k, s) for k, s in sorted(base.items())])
    for segment in deltas:
        if segment:
            _write_delta(lake, [_row(k, s) for k, s in sorted(segment.items())])
    source = _ledger_scan_source(wls.ledger_path(lake))
    conn = duckdb.connect()
    sql_rows = conn.execute(
        f"SELECT security_id, status FROM {source.from_sql} ORDER BY security_id",  # noqa: S608
        list(source.params),
    ).fetchall()
    python_records = tuple(wls.load_window_ledger(lake).records())
    assert sorted((str(a), str(b)) for a, b in sql_rows) == sorted(
        (r.security_id, r.status) for r in python_records
    )
    sql_counts, sql_total = _window_counts_total(conn, source)
    assert sql_counts == _window_counts_from_records(python_records)
    assert sql_total == len(python_records)


# ── direct builder — the mid-pull count path with count_cache=None ────────────


def test_direct_builder_midpull_with_since_uncached(tmp_path: Path) -> None:
    """The builder's uncached mid-pull arm: fold source + per-run counts, no cache."""
    _write_base(tmp_path, [_row("NIFTY", "SUCCESS", bars=5)])
    _write_delta(tmp_path, [_row("BANKNIFTY", "FAILED")])
    payload = _build_window_payload_duckdb(
        duckdb.connect(),
        wls.ledger_path(tmp_path),
        symbol_for_id=None,
        market=None,
        status=None,
        limit=100,
        offset=0,
        count_cache=None,
        generation=0,
        since="2026-06-08T11:00:00+00:00",
    )
    assert payload["counts"]["spot"] == {"SUCCESS": 1, "FAILED": 1}
    assert payload["counts_since_pull_start"]["spot"] == {"SUCCESS": 1, "FAILED": 1}


# ── PACK-L1: stamp-generation keying, the mid-pull floor + the page-row cache ─
# (DEC-WINDOWS-MIDPULL-FLOOR-001)


class _FamilyCountingCache:
    """A real :class:`MoversSnapshotCache` (injectable — the floor tests pass the
    production-shaped floored instance) that counts recomputes per key FAMILY
    (``window-counts`` / ``window-counts-since`` / ``window-page``)."""

    def __init__(self, inner: MoversSnapshotCache | None = None) -> None:
        self.inner = inner if inner is not None else MoversSnapshotCache()
        self.computes: dict[str, int] = {}

    def get_or_compute(self, key: str, *, generation: int, compute: Any) -> Any:
        family = key.split("|", 1)[0]

        def _wrapped() -> Any:
            self.computes[family] = self.computes.get(family, 0) + 1
            return compute()

        return self.inner.get_or_compute(key, generation=generation, compute=_wrapped)


def test_page_rows_cached_within_stamp_and_recomputed_on_stamp_move(tmp_path: Path) -> None:
    """The row PAGE is cached per stamp (fails pre-fix: the page query re-ran the
    whole O(total rows + delta footers) fold on EVERY poll — only the counts were
    cached), and ONE delta append (stamp move) recomputes it exactly once."""
    _write_base(tmp_path, [_row("NIFTY", "SUCCESS", bars=5)])
    _write_delta(tmp_path, [_row("BANKNIFTY", "SUCCESS", bars=3)])
    conn = duckdb.connect()
    cache = _FamilyCountingCache()
    first = build_window_payload(
        tmp_path, conn=conn, count_cache=cache, page_cache=cache, generation=0
    )
    assert cache.computes == {"window-counts": 1, "window-page": 1}
    for _ in range(5):
        repeat = build_window_payload(
            tmp_path, conn=conn, count_cache=cache, page_cache=cache, generation=0
        )
        assert repeat["windows"] == first["windows"]
    assert cache.computes == {"window-counts": 1, "window-page": 1}  # pure hits
    _write_delta(tmp_path, [_row("SENSEX", "FAILED")])  # the stamp moves
    moved = build_window_payload(
        tmp_path, conn=conn, count_cache=cache, page_cache=cache, generation=0
    )
    assert cache.computes == {"window-counts": 2, "window-page": 2}
    assert any(r["symbol"] == "SENSEX" for r in moved["windows"])  # fresh rows served


def test_page_cache_fresh_compute_byte_identical(tmp_path: Path) -> None:
    """The cached-arm FRESH compute is byte-identical to the uncached arm across
    filter/page permutations (the change-2 payload pin)."""
    _write_base(
        tmp_path,
        [
            _row("NIFTY", "SUCCESS", bars=5),
            _row("BANKNIFTY", "FAILED"),
            _row("SENSEX", "EMPTY"),
        ],
    )
    _write_delta(tmp_path, [_row("RELIANCE", "SUCCESS", bars=2)])
    conn = duckdb.connect()
    permutations: list[dict[str, Any]] = [
        {},
        {"market": "spot"},
        {"status": "FAILED"},
        {"market": "spot", "status": "SUCCESS"},
        {"limit": 2},
        {"limit": 1, "offset": 1},
        {"market": "nonsense"},
    ]
    for kwargs in permutations:
        uncached_arm = build_window_payload(
            tmp_path, conn=conn, count_cache=None, generation=0, **kwargs
        )
        # Every injection arm — both caches (the production wiring), counts-only,
        # page-only — must produce the identical fresh payload.
        both = build_window_payload(
            tmp_path,
            conn=conn,
            count_cache=MoversSnapshotCache(),
            page_cache=MoversSnapshotCache(),
            generation=0,
            **kwargs,
        )
        counts_only = build_window_payload(
            tmp_path, conn=conn, count_cache=MoversSnapshotCache(), generation=0, **kwargs
        )
        page_only = build_window_payload(
            tmp_path, conn=conn, page_cache=MoversSnapshotCache(), generation=0, **kwargs
        )
        for arm_name, arm in (
            ("both", both),
            ("counts_only", counts_only),
            ("page_only", page_only),
        ):
            assert arm == uncached_arm, f"{arm_name}-vs-uncached drift for {kwargs}"


def test_page_cache_distinct_filter_page_coordinates_never_collide(tmp_path: Path) -> None:
    """Every (market, status, limit, offset) coordinate caches its OWN page —
    repeat polls of two different coordinates each serve their own rows."""
    _write_base(
        tmp_path,
        [
            _row("NIFTY", "SUCCESS", bars=5),
            _row("BANKNIFTY", "FAILED"),
        ],
    )
    _write_delta(tmp_path, [_row("SENSEX", "EMPTY")])
    conn = duckdb.connect()
    cache = MoversSnapshotCache()
    failed = build_window_payload(
        tmp_path, conn=conn, count_cache=cache, page_cache=cache, generation=0, status="FAILED"
    )
    empty = build_window_payload(
        tmp_path, conn=conn, count_cache=cache, page_cache=cache, generation=0, status="EMPTY"
    )
    paged = build_window_payload(
        tmp_path, conn=conn, count_cache=cache, page_cache=cache, generation=0, limit=1, offset=1
    )
    assert [r["symbol"] for r in failed["windows"]] == ["BANKNIFTY"]
    assert [r["symbol"] for r in empty["windows"]] == ["SENSEX"]
    assert len(paged["windows"]) == 1
    # Repeat each coordinate — served from ITS own cached page, never a sibling's.
    assert (
        build_window_payload(
            tmp_path, conn=conn, count_cache=cache, page_cache=cache, generation=0, status="FAILED"
        )
        == failed
    )
    assert (
        build_window_payload(
            tmp_path, conn=conn, count_cache=cache, page_cache=cache, generation=0, status="EMPTY"
        )
        == empty
    )
    assert (
        build_window_payload(
            tmp_path,
            conn=conn,
            count_cache=cache,
            page_cache=cache,
            generation=0,
            limit=1,
            offset=1,
        )
        == paged
    )


def test_symbol_map_token_content_sensitive_order_free() -> None:
    """`_window_symbol_map_token`: None/empty key identically ("0" — both project
    raw coordinates); content moves the token; insertion order does not."""
    assert _window_symbol_map_token(None) == "0"
    assert _window_symbol_map_token({}) == "0"
    a = _window_symbol_map_token({"25": "BANKNIFTY", "13": "NIFTY"})
    b = _window_symbol_map_token({"13": "NIFTY", "25": "BANKNIFTY"})
    assert a == b  # order-independent
    assert a != _window_symbol_map_token({"25": "BANKNIFTY", "13": "MIDCPNIFTY"})  # value moves it
    assert a != _window_symbol_map_token({"25": "BANKNIFTY"})  # entry set moves it


def test_page_cache_symbol_map_change_rekeys_within_one_stamp(tmp_path: Path) -> None:
    """An instrument-master reload (a CHANGED id→symbol map) re-keys the page
    within one ledger stamp — a cached page can never serve the old display
    names (Golden Rule #1); the original map's page stays a hit."""
    _write_base(tmp_path, [_row("NIFTY", "SUCCESS", bars=5)])
    _write_delta(tmp_path, [_row("BANKNIFTY", "SUCCESS", bars=3)])
    conn = duckdb.connect()
    cache = _FamilyCountingCache()
    map_a = {"13": "NIFTY"}
    map_b = {"13": "RENAMED"}
    build_window_payload(
        tmp_path, conn=conn, count_cache=cache, page_cache=cache, generation=0, symbol_for_id=map_a
    )
    assert cache.computes["window-page"] == 1
    build_window_payload(
        tmp_path, conn=conn, count_cache=cache, page_cache=cache, generation=0, symbol_for_id=map_b
    )
    assert cache.computes["window-page"] == 2  # a different map content = a different page key
    build_window_payload(
        tmp_path, conn=conn, count_cache=cache, page_cache=cache, generation=0, symbol_for_id=map_a
    )
    assert cache.computes["window-page"] == 2  # …and the first map's page is still warm
    assert cache.computes["window-counts"] == 1  # counts carry no symbols — ONE entry throughout


def test_midpull_floor_throttles_recomputes_bounded_and_logged(tmp_path: Path) -> None:
    """THE change-1 teeth (fails pre-fix): with the PRODUCTION-shaped cache pair
    (`make_window_payload_caches` — the `_LEDGER_READ_CACHE` probe/floor shape:
    constant-True stamp-moved probe + the 120 s cap), a ledger stamp move
    within the #920 floor serves the PRIOR stamp's counts+page STALE (zero
    recomputes, ``movers_cache.stale_served`` logged — bounded + visible), and
    the floor RELEASES once the entries age past it. The churn here is
    writer-agnostic — exactly the CROSS-PROCESS CLI shape too (no in-process
    pull exists in this test; the constant-True probe floors it regardless —
    the round-2 review's option (a), closing the probe asymmetry vs the
    round-1 ``_pull_live`` wiring).

    Pre-fix this test fails at the first stale assertion: the stamp token lived
    IN the cache key (fixed ``generation=0``), so a stamp move was a cache MISS
    — a new key — and the adaptive floor (which fires only on a generation
    mismatch of one stable key) structurally never engaged.
    """
    _write_base(tmp_path, [_row("NIFTY", "SUCCESS", bars=5)])
    _write_delta(tmp_path, [_row("BANKNIFTY", "SUCCESS", bars=3)])
    conn = duckdb.connect()
    now = {"t": 100.0}
    # The SWR revalidator runs INLINE (the injectable swr_runner seam —
    # DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001) so the past-floor
    # serve-last-known + background-store sequence is deterministic.
    counts_inner, page_inner = make_window_payload_caches(
        clock=lambda: now["t"], swr_runner=lambda job: job()
    )
    counts = _FamilyCountingCache(counts_inner)
    page = _FamilyCountingCache(page_inner)
    first = build_window_payload(
        tmp_path, conn=conn, count_cache=counts, page_cache=page, generation=0
    )
    assert counts.computes == {"window-counts": 1}
    assert page.computes == {"window-page": 1}
    # A delta append moves the stamp (a CLI-style cross-process flush — no pull
    # anywhere) — the entries are age-0, inside the min(max(2 s, own duration),
    # 120 s) floor → the PRIOR stamp is served stale, ZERO recomputes, logged.
    _write_delta(tmp_path, [_row("SENSEX", "FAILED")])
    with capture_logs() as logs:
        stale = build_window_payload(
            tmp_path, conn=conn, count_cache=counts, page_cache=page, generation=0
        )
    assert counts.computes == {"window-counts": 1}
    assert page.computes == {"window-page": 1}
    assert stale["counts"] == first["counts"]  # the bounded-stale serve (no SENSEX yet)
    assert not any(r["symbol"] == "SENSEX" for r in stale["windows"])
    assert _events(logs, "movers_cache.stale_served")  # never a silent illusion
    # The floor AGES OUT (past even the 120 s cap, whatever the measured
    # durations) → the SWR arm serves the PRIOR payload ONCE MORE (never a
    # blocking inline fold) while the revalidators — inline here — land the
    # fresh fold of BOTH families; the NEXT poll serves the delta row.
    now["t"] += 121.0
    aged_stale = build_window_payload(
        tmp_path, conn=conn, count_cache=counts, page_cache=page, generation=0
    )
    assert counts.computes == {"window-counts": 2}  # the revalidate DID fold fresh…
    assert page.computes == {"window-page": 2}
    assert not any(r["symbol"] == "SENSEX" for r in aged_stale["windows"])  # …served next
    fresh = build_window_payload(
        tmp_path, conn=conn, count_cache=counts, page_cache=page, generation=0
    )
    assert counts.computes == {"window-counts": 2}  # the landed store — no re-fold
    assert page.computes == {"window-page": 2}
    assert any(r["symbol"] == "SENSEX" for r in fresh["windows"])


def test_phase_progress_counts_ride_the_same_floor(tmp_path: Path) -> None:
    """The pull-progress poll shares the floored counts entry: a stamp move
    within the floor serves the prior counts (zero recomputes), the stale serve
    is logged, and the floor releases on aging — the same bounded envelope as
    the windows page (production counts instance via the factory)."""
    _write_base(tmp_path, [_row("NIFTY", "SUCCESS", bars=5)])
    conn = duckdb.connect()
    now = {"t": 50.0}
    counts_inner, _page_inner = make_window_payload_caches(
        clock=lambda: now["t"], swr_runner=lambda job: job()
    )
    cache = _FamilyCountingCache(counts_inner)
    path = wls.ledger_path(tmp_path)
    rows = phase_progress_payload(conn, path, current_phase="spot", count_cache=cache, generation=0)
    assert next(r for r in rows if r["phase"] == "spot")["done"] == 1
    assert cache.computes == {"window-counts": 1}
    _write_delta(tmp_path, [_row("BANKNIFTY", "SUCCESS", bars=3)])  # the stamp moves
    with capture_logs() as logs:
        stale_rows = phase_progress_payload(
            conn, path, current_phase="spot", count_cache=cache, generation=0
        )
    assert cache.computes == {"window-counts": 1}  # floor-stale serve, no recount
    assert next(r for r in stale_rows if r["phase"] == "spot")["done"] == 1  # prior stamp
    assert _events(logs, "movers_cache.stale_served")
    now["t"] += 121.0  # past the 120 s cap → the SWR arm: one more last-known
    # serve while the (inline) revalidator lands the fresh counts.
    swr_rows = phase_progress_payload(
        conn, path, current_phase="spot", count_cache=cache, generation=0
    )
    assert cache.computes == {"window-counts": 2}  # the revalidate folded fresh
    assert next(r for r in swr_rows if r["phase"] == "spot")["done"] == 1  # last-known
    fresh_rows = phase_progress_payload(
        conn, path, current_phase="spot", count_cache=cache, generation=0
    )
    assert cache.computes == {"window-counts": 2}  # the landed store — no re-fold
    assert next(r for r in fresh_rows if r["phase"] == "spot")["done"] == 2


def test_make_window_payload_caches_dedicated_small_page_instance() -> None:
    """The round-2 MEDIUM fold's structural pin: the factory returns TWO
    distinct instances — the page cache SMALL (`WINDOW_PAGE_CACHE_CAPACITY`,
    bounding retained pages to ≤ capacity × min(limit, WINDOW_ROWS_CAP) rows,
    and structurally unable to LRU-evict the counts entries) — both riding the
    `_LEDGER_READ_CACHE` probe/floor shape (constant-True probe, floor on,
    120 s cap)."""
    counts_cache, page_cache = make_window_payload_caches()
    assert counts_cache is not page_cache
    assert page_cache.capacity == WINDOW_PAGE_CACHE_CAPACITY == 8
    assert counts_cache.capacity == MoversSnapshotCache().capacity  # the 256 default
    for instance in (counts_cache, page_cache):
        assert instance._adaptive_floor is True
        assert instance._adaptive_floor_cap_s == 120.0
        probe = instance._pull_active_probe
        assert probe is not None and probe() is True  # constant-True stamp-moved probe


def test_unknown_status_values_collapse_to_one_page_key(tmp_path: Path) -> None:
    """Round-2 hardening: every NOT-allow-listed ?status= value collapses onto
    the ONE `_UNKNOWN_WINDOW_STATUS` sentinel BEFORE key minting — N distinct
    bogus values cost ONE page compute + ONE counts entry (no key churn), serve
    empty rows / matched=0 (legacy parity, pinned against the uncached arm),
    and a KNOWN status still pages normally."""
    _write_base(tmp_path, [_row("NIFTY", "SUCCESS", bars=5), _row("BANKNIFTY", "FAILED")])
    _write_delta(tmp_path, [_row("SENSEX", "EMPTY")])
    conn = duckdb.connect()
    cache = _FamilyCountingCache()
    bogus = ("BOGUS", "definitely-not-a-status", "%%%", "planned-ish", "__UNKNOWN_STATUS__")
    payloads = [
        build_window_payload(
            tmp_path, conn=conn, count_cache=cache, page_cache=cache, generation=0, status=value
        )
        for value in bogus
    ]
    assert cache.computes == {"window-counts": 1, "window-page": 1}  # ONE key for all five
    uncached = build_window_payload(tmp_path, conn=conn, status="BOGUS")
    for payload in payloads:
        assert payload["windows"] == []
        assert payload["matched"] == 0
        assert payload == uncached  # legacy parity, cached == uncached
    # A KNOWN status is untouched by the allow-list: real rows, its own key.
    failed = build_window_payload(
        tmp_path, conn=conn, count_cache=cache, page_cache=cache, generation=0, status="FAILED"
    )
    assert [r["symbol"] for r in failed["windows"]] == ["BANKNIFTY"]
    assert cache.computes == {"window-counts": 1, "window-page": 2}
    assert _UNKNOWN_WINDOW_STATUS not in repr(failed)  # the sentinel never leaks into a payload


class TestComputeCursor:
    """`_compute_cursor` (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001): the
    flagged window caches may run a compute on a BACKGROUND revalidator daemon
    concurrent with the request thread's statements on the SAME shared DuckDB
    connection — whose execute→fetch pending-result state is per-connection
    (reproduced in-suite: a background counts fold fetched the request's
    16-column page rows). Every cached compute therefore rides an isolated
    cursor; fakes without `cursor()` fall back to the conn itself."""

    def test_real_conn_yields_an_independent_cursor(self) -> None:
        conn = duckdb.connect()
        cursor = _compute_cursor(conn)
        assert cursor is not conn  # statement-isolated — its own pending state
        assert cursor.execute("SELECT 42").fetchone() == (42,)

    def test_conn_without_cursor_falls_back(self) -> None:
        spy = _SpyConn()  # the suite's fake — records calls, has no cursor()
        assert _compute_cursor(spy) is spy

    def test_raising_cursor_falls_back(self) -> None:
        class _Wedged:
            def cursor(self) -> Any:
                raise RuntimeError("connection closed")

        wedged = _Wedged()
        assert _compute_cursor(wedged) is wedged

    def test_background_revalidate_never_corrupts_the_request_statements(
        self, tmp_path: Path
    ) -> None:
        """THE interleave regression (fails without `_compute_cursor`): a
        revalidator folding counts on the shared conn CONCURRENT with the
        request thread's page query corrupted each other's pending results
        (`too many values to unpack (expected 3)`). With isolation, heavy
        stamp churn + concurrent polls stay clean."""
        _write_base(tmp_path, [_row("NIFTY", "SUCCESS", bars=5)])
        conn = duckdb.connect()
        counts_cache, page_cache = make_window_payload_caches()  # REAL daemon runner
        errors: list[BaseException] = []

        def _poll() -> None:
            try:
                for _ in range(10):
                    build_window_payload(
                        tmp_path,
                        conn=conn,
                        count_cache=counts_cache,
                        page_cache=page_cache,
                        generation=0,
                    )
            except BaseException as exc:
                errors.append(exc)

        import threading as _threading
        import time as _time

        pollers = [_threading.Thread(target=_poll, daemon=True) for _ in range(4)]
        for t in pollers:
            t.start()
        # Churn the stamp while the pollers run (the mid-pull delta cadence).
        for i in range(6):
            _write_delta(tmp_path, [_row(f"SYM{i}", "SUCCESS", bars=1)])
            _time.sleep(0.02)
        for t in pollers:
            t.join(timeout=30)
        assert not errors, f"interleaved statements corrupted a result: {errors[0]!r}"

    def test_store_locked_read_conn_yields_a_fresh_locked_view(self, tmp_path: Path) -> None:
        """THE round-1 BLOCKER pin: the PRODUCTION conn (`store.connection`, a
        `_LockedReadConn`) must never fall back to itself — `cursor()` yields
        a FRESH view with its own hold state. Pre-fix, `_LockedReadConn` had
        no `cursor()`, so `_compute_cursor` silently returned the ONE
        single-thread-per-view instance the SWR revalidator daemons then
        shared with the request thread — corrupting `_held` cross-thread and
        leaking the store read lock (a permanent dashboard-read freeze, the
        exact class this branch exists to fix)."""
        from brutex.data.instrument_store import InstrumentStore

        store = InstrumentStore.open(tmp_path / "instruments.duckdb")
        try:
            view = store.connection
            isolated = _compute_cursor(view)
            assert isolated is not view, "production store conn fell back to the shared view"
            assert type(isolated) is type(view)
            assert isolated.execute("SELECT 42").fetchone() == (42,)
        finally:
            store.close()

    def test_concurrent_compute_on_store_conn_waits_never_corrupts(self, tmp_path: Path) -> None:
        """The refuter's deterministic held-lock interleave, at db_view's own
        seam: the request view HOLDS the read lock mid-statement (executed,
        not yet fetched) while a background compute rides
        `_compute_cursor(view)`. Pre-fix (same shared view) the background
        `execute()` released the holder's lock from a NON-owner thread
        (`RuntimeError: cannot release un-acquired lock`) and the holder then
        LEAKED its acquisition — every later read deadlocked. Post-fix the
        fresh view WAITS, completes cleanly, and the lock ends FREE."""
        import threading as _threading

        from brutex.data.instrument_store import InstrumentStore

        store = InstrumentStore.open(tmp_path / "instruments.duckdb")
        try:
            view = store.connection
            view.execute("SELECT 1")  # the request thread's live hold — no fetch yet
            errors: list[BaseException] = []
            finished = _threading.Event()

            def _revalidator() -> None:
                try:
                    assert _compute_cursor(view).execute("SELECT 2").fetchone() == (2,)
                except BaseException as exc:  # the oracle collects everything
                    errors.append(exc)
                finally:
                    finished.set()

            reval = _threading.Thread(target=_revalidator, daemon=True)
            reval.start()
            # The isolated view must WAIT on the read lock, never break the holder.
            assert not finished.wait(timeout=0.3), f"finished against a held lock: {errors!r}"
            assert view.fetchone() == (1,)  # the holder completes + releases
            assert finished.wait(timeout=5.0)
            reval.join(timeout=5.0)
            assert not errors, f"background compute corrupted the shared hold: {errors[0]!r}"
            # The lock is FREE: a fresh view's read completes within the bound.
            done = _threading.Event()

            def _probe() -> None:
                assert store.connection.execute("SELECT 3").fetchone() == (3,)
                done.set()

            probe = _threading.Thread(target=_probe, daemon=True)
            probe.start()
            assert done.wait(timeout=5.0), "store read lock leaked — later reads deadlock"
            probe.join(timeout=5.0)
        finally:
            store.close()

    def test_legacy_probe_raise_never_leaks_the_store_read_lock(self, tmp_path: Path) -> None:
        """The `_close_cursor` hygiene regression: the legacy-shape probe
        raises AFTER a description-only read, leaving its `_LockedReadConn`
        view HOLDING the store read lock — without the compute's `finally`
        close, that hold is stranded forever (deadlock on a legacy-shape
        ledger; pre-fix this leaked on BOTH shapes — the shared request view
        and, with `cursor()` alone, the per-compute view)."""
        import threading as _threading

        from brutex.data.instrument_store import InstrumentStore

        _write_legacy_base(tmp_path, "EMPTYCO")
        store = InstrumentStore.open(tmp_path / "instruments.duckdb")
        try:
            rows, total = query_window_ledger_page(store.connection, tmp_path, limit=100, offset=0)
            assert total == 1  # served by the byte-identical legacy parse
            assert len(rows) == 1
            done = _threading.Event()

            def _probe() -> None:
                assert store.connection.execute("SELECT 42").fetchone() == (42,)
                done.set()

            probe = _threading.Thread(target=_probe, daemon=True)
            probe.start()
            assert done.wait(timeout=5.0), (
                "the legacy-shape probe's description-only raise stranded the "
                "store read lock — later dashboard reads deadlock"
            )
            probe.join(timeout=5.0)
        finally:
            store.close()

    def test_close_cursor_never_closes_the_shared_conn(self) -> None:
        """The fallback arm (`cursor is conn` — the caller's own connection)
        must never be closed out from under the request thread."""

        class _Conn:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        conn = _Conn()
        _close_cursor(conn, conn)
        assert not conn.closed

    def test_close_cursor_tolerates_missing_and_raising_close(self) -> None:
        """Cleanup is best-effort: a cursor without `close()` is a no-op and a
        raising `close()` is swallowed — hygiene never masks the compute's own
        outcome or 500s a poll."""

        class _NoClose:
            pass

        _close_cursor(_NoClose(), object())  # no close attr — a clean no-op

        class _Boom:
            def close(self) -> None:
                raise RuntimeError("wedged mid-close")

        _close_cursor(_Boom(), object())  # swallowed
