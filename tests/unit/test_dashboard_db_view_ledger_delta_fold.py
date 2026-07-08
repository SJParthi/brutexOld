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
  ``append_ledger_delta`` writing the full 17-column schema — trailing
  ``empty_retries``/``transient_retries`` per DEC-RETRY-COUNTER-SPLIT-001 then
  ``streak_provenance`` per DEC-STREAK-PROVENANCE-001 (the base-less
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
    _WINDOW_KEY_COLUMNS,
    _build_window_payload_duckdb,
    _ledger_scan_source,
    _ledger_stamp_token,
    _window_counts_from_records,
    _window_counts_total,
    _window_severity_order_sql,
    build_window_payload,
    phase_progress_payload,
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
    """One 17-field ledger row tuple (the exact ``_COLUMNS`` shape).

    The trailing ``empty_retries``/``transient_retries`` pair is ``None`` — the
    honest-unknown optional-column default (DEC-RETRY-COUNTER-SPLIT-001,
    MIGRATION.md #1585), exactly what a pre-split ledger row loads as — and the
    LAST ``streak_provenance`` cell (DEC-STREAK-PROVENANCE-001) rides the same
    ``bars_net_new`` tri-state convention: ``None`` here (legacy-unknown),
    matching what a pre-provenance writer's row loads as.
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
        None,
    )


def _write_base(lake: Path, rows: Sequence[tuple[Any, ...]]) -> None:
    """Write a REAL 17-column base ledger Parquet directly (full status control)."""
    import pyarrow.parquet as pq

    path = wls.ledger_path(lake)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(wls._rows_to_table(list(rows)), path)  # type: ignore[no-untyped-call, unused-ignore]


def _write_delta(lake: Path, rows: Sequence[tuple[Any, ...]]) -> Path:
    """Append one REAL 17-column delta Parquet via the store's own name minting."""
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
