"""READER-PARITY gates for the G10 window-ledger Rust render (twin-lake battery).

DEC-LAKE-RUST-LEDGERSTORE-CONVERT-001 (MIGRATION.md #1918). The conversion's
standard is ROW-EQUIVALENCE + reader-behavioral parity, never bytes (the
#1917 spike's measured-infeasible verdict), so THIS suite is the standard's
teeth: TWIN LAKES — one written with the flag OFF (pyarrow bytes), one with
the flag ON in the post-burn-in steady state (REAL crate bytes, no oracle in
the path) — carrying IDENTICAL logical records, driven through the REAL
production read paths:

(a) tri-reader ordered-row equivalence (pyarrow · polars · DuckDB);
(b) the ``dedup_latest_source_sql`` base+delta fold with ALL FOUR mix arms
    (pure-py · pure-rust · py-base+rust-deltas · rust-base+py-deltas);
    ``query_window_ledger_page`` payload-identical WITH the FALLBACK-ABSENCE
    pin (a rust base must never silently defer to the legacy parse);
    ``_window_counts_total`` + the ``_window_counts_since``
    ``last_attempt_at >= ?`` ISO-STRING compare arm;
(c) ``load_window_ledger`` / ``window_ledger_rows_cached`` round-trips;
(d) the legacy-17-col schema-probe outcome equality;
(e) footer-inertness (DuckDB description identical) + the ANTI-VACUOUS raw
    bytes-differ assert (the gates test the REAL divergence, not a no-op);
(f) the EMPTY-ledger watch-item (polars 0 row groups vs pyarrow's 1 — every
    reader identical off the FOOTER schema; measured PASS, so the
    pre-authorized empty-input narrowing was NOT needed);
(g) the stat-token EXPECTED-move pin (a rewrite MUST move the token — never
    an equality pin) + the delta torn-read retry self-heal over a
    rust-rendered lake.

Skips cleanly when the merged ``brutex_lake`` wheel is absent (the
lake-rust-parity lane installs it; main CI stays wheel-free).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

brutex_lake = pytest.importorskip(
    "brutex_lake", reason="the merged rust/brutex-lake wheel is not installed"
)
pl = pytest.importorskip("polars")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
duckdb = pytest.importorskip("duckdb")

from brutex.dashboard import db_view  # noqa: E402
from brutex.data import lake_rust_ledger_render  # noqa: E402
from brutex.data import window_ledger_store as wls  # noqa: E402
from brutex.data.fetch_coverage import WindowKey  # noqa: E402
from brutex.data.window_ledger import WindowLedger  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator

_T0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _key(i: int) -> WindowKey:
    return WindowKey(
        market="groww" if i % 2 == 0 else "groww-option",
        security_id=f"SYM{i:02d}",
        interval="1minute" if i % 2 == 0 else "1day",
        start=date(2025, 1, 1 + (i % 3) * 9),
        end=date(2025, 1, 31 - (i % 4)),
    )


def _set_arm(monkeypatch: pytest.MonkeyPatch, *, rust: bool) -> None:
    """Point the writer at one arm: OFF = pyarrow oracle; ON = steady-state crate."""
    if rust:
        monkeypatch.setenv(lake_rust_ledger_render.LAKE_RUST_LEDGER_RENDER_ENV, "1")
        lake_rust_ledger_render.reset_lake_rust_ledger_render_for_tests(remaining=0)
    else:
        monkeypatch.delenv(lake_rust_ledger_render.LAKE_RUST_LEDGER_RENDER_ENV, raising=False)
        lake_rust_ledger_render.reset_lake_rust_ledger_render_for_tests()


def _seed_base(lake: Path) -> None:
    """The base save: 6 keys across markets/intervals/statuses + a PLANNED row."""
    ledger = WindowLedger()
    for i in range(6):
        ledger.record(
            _key(i),
            raw_bars=5 if i % 3 else 0,
            error_type="GrowwTimeoutError" if i % 4 == 3 else None,
            bars_appended=5 if i % 3 else 0,
            bars_net_new=3 if i % 3 else None,
            now=_T0 + timedelta(minutes=i),
        )
    ledger.plan(_key(6), now=_T0)  # the last_attempt_at-None arm
    wls.save_window_ledger(ledger, lake)


def _seed_deltas(lake: Path) -> None:
    """Two deltas: a brand-new key, then a FAILED demotion of a base SUCCESS."""
    live = wls.load_window_ledger(lake)
    live.record(
        _key(7), raw_bars=7, error_type=None, bars_appended=7, now=_T0 + timedelta(minutes=30)
    )
    assert wls.append_ledger_delta(live, lake) == 1
    live.record(
        _key(1), raw_bars=0, error_type="timeout", bars_appended=0, now=_T0 + timedelta(minutes=40)
    )
    assert wls.append_ledger_delta(live, lake) >= 1


def _build_lake(
    lake: Path, monkeypatch: pytest.MonkeyPatch, *, base_rust: bool, delta_rust: bool
) -> Path:
    lake.mkdir(parents=True, exist_ok=True)
    _set_arm(monkeypatch, rust=base_rust)
    _seed_base(lake)
    _set_arm(monkeypatch, rust=delta_rust)
    _seed_deltas(lake)
    # Anti-vacuous latch guard: a latched/autoreverted seam would have shipped
    # ORACLE bytes and made every parity below trivially true.
    assert lake_rust_ledger_render.rust_ledger_render_disabled() is False
    return lake


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(lake_rust_ledger_render.LAKE_RUST_LEDGER_RENDER_ENV, raising=False)
    lake_rust_ledger_render.reset_lake_rust_ledger_render_for_tests()
    monkeypatch.setattr(db_view, "_LEDGER_READ_CACHE", db_view._make_ledger_read_cache())
    yield
    lake_rust_ledger_render.reset_lake_rust_ledger_render_for_tests()


@pytest.fixture
def twin_lakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """(py_lake, rust_lake) — identical records, opposite writer arms."""
    py_lake = _build_lake(tmp_path / "py", monkeypatch, base_rust=False, delta_rust=False)
    rust_lake = _build_lake(tmp_path / "rust", monkeypatch, base_rust=True, delta_rust=True)
    _set_arm(monkeypatch, rust=False)
    return py_lake, rust_lake


def _ledger_files(lake: Path) -> list[Path]:
    return [wls.ledger_path(lake), *wls.list_ledger_deltas(lake)]


def _fold_rows(lake: Path) -> list[tuple[Any, ...]]:
    """The base+delta fold through the REAL scan source — ordered full rows."""
    source = db_view._ledger_scan_source(wls.ledger_path(lake))
    select_sql = ", ".join(f'"{name}"' for name in db_view._LEDGER_ROW_COLUMNS)
    order_sql = ", ".join(f'"{name}"' for name in db_view._WINDOW_KEY_COLUMNS)
    with duckdb.connect() as conn:
        return conn.execute(
            f"SELECT {select_sql} FROM {source.from_sql} ORDER BY {order_sql}",  # noqa: S608
            list(source.params),
        ).fetchall()


class TestTriReaderRowEquivalence:
    def test_every_file_reads_identically_through_all_three_readers(
        self, twin_lakes: tuple[Path, Path]
    ) -> None:
        py_lake, rust_lake = twin_lakes
        py_files = _ledger_files(py_lake)
        rust_files = _ledger_files(rust_lake)
        assert len(py_files) == len(rust_files) == 3  # base + 2 deltas
        for py_file, rust_file in zip(py_files, rust_files, strict=True):
            # pyarrow — ordered per-row dicts (row ORDER preserved, not just set).
            py_rows = pq.read_table(py_file).to_pylist()
            rust_rows = pq.read_table(rust_file).to_pylist()
            assert py_rows == rust_rows and py_rows
            # polars — ordered tuples.
            assert pl.read_parquet(py_file).rows() == pl.read_parquet(rust_file).rows()
            # DuckDB — ordered by the 5-part window key (SQL scan order over a
            # single file is storage order, but the key sort is the contract).
            with duckdb.connect() as conn:
                order_sql = ", ".join(f'"{c}"' for c in db_view._WINDOW_KEY_COLUMNS)
                py_sql = conn.execute(
                    f"SELECT * FROM read_parquet(?) ORDER BY {order_sql}",  # noqa: S608
                    [py_file.as_posix()],
                ).fetchall()
                rust_sql = conn.execute(
                    f"SELECT * FROM read_parquet(?) ORDER BY {order_sql}",  # noqa: S608
                    [rust_file.as_posix()],
                ).fetchall()
            assert py_sql == rust_sql


class TestDedupLatestFoldMixArms:
    def test_all_four_base_delta_mix_arms_fold_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pure-py · pure-rust · py-base+rust-deltas · rust-base+py-deltas —
        the latest-wins fold (base always ranks LAST; deltas newest-first)
        returns IDENTICAL rows over every writer-arm combination."""
        arms = {
            "pure_py": (False, False),
            "pure_rust": (True, True),
            "py_base_rust_deltas": (False, True),
            "rust_base_py_deltas": (True, False),
        }
        folded: dict[str, list[tuple[Any, ...]]] = {}
        for name, (base_rust, delta_rust) in arms.items():
            lake = _build_lake(
                tmp_path / name, monkeypatch, base_rust=base_rust, delta_rust=delta_rust
            )
            folded[name] = _fold_rows(lake)
        reference = folded["pure_py"]
        assert reference and len(reference) == 8  # 6 base + 1 planned + 1 delta-new
        # The demotion delta genuinely bites (anti-vacuous): SYM01 reads FAILED.
        demoted = [r for r in reference if r[1] == "SYM01"]
        assert demoted and demoted[0][5] == "FAILED"
        for name, rows in folded.items():
            assert rows == reference, f"mix arm {name} diverged from the pure-py fold"

    def test_empty_base_plus_delta_folds_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An EMPTY base (0 rows; on the rust arm ZERO row groups) UNION'd with
        a delta through the REAL fold path — the review-round-1 gap: the
        empty-base watch-item and the fold arms never met. The
        ``read_parquet(?, union_by_name=true, filename=true,
        file_row_number=true)`` scan must union a 0-row-group polars base with
        a delta identically to the pyarrow arm, and the page query must serve
        it fast-path (NO fallback log)."""
        lakes: dict[str, Path] = {}
        for name, rust in (("py", False), ("rust", True)):
            lake = tmp_path / name
            lake.mkdir()
            _set_arm(monkeypatch, rust=rust)
            wls.save_window_ledger(WindowLedger(), lake)  # the EMPTY base
            live = wls.load_window_ledger(lake)
            live.record(_key(0), raw_bars=5, error_type=None, bars_appended=5, now=_T0)
            assert wls.append_ledger_delta(live, lake) == 1
            assert lake_rust_ledger_render.rust_ledger_render_disabled() is False
            lakes[name] = lake
        _set_arm(monkeypatch, rust=False)
        # Anti-vacuous: the rust base is genuinely the 0-ROW-GROUP shape (the
        # #1917 empty-file structure divergence) — the union below unions it.
        assert pq.read_metadata(wls.ledger_path(lakes["rust"])).num_row_groups == 0
        assert pq.read_metadata(wls.ledger_path(lakes["py"])).num_row_groups == 1
        # The REAL fold path (db_view._ledger_scan_source →
        # lake_read.dedup_latest_source_sql): rows identical py-arm vs rust-arm.
        py_fold = _fold_rows(lakes["py"])
        rust_fold = _fold_rows(lakes["rust"])
        assert py_fold == rust_fold
        assert len(py_fold) == 1 and py_fold[0][1] == "SYM00"  # the delta row
        # And the page query serves BOTH fast-path — no fallback/retry log.
        for lake in lakes.values():
            monkeypatch.setattr(db_view, "_LEDGER_READ_CACHE", db_view._make_ledger_read_cache())
            with duckdb.connect() as conn, capture_logs() as logs:
                page, total = db_view.query_window_ledger_page(conn, lake, limit=10, offset=0)
            assert [e for e in logs if e["event"].startswith("window_ledger_page.")] == []
            assert total == 1 and page == tuple(py_fold)


class TestWindowLedgerPageParity:
    def test_page_payload_identical_and_no_fallback_on_the_rust_base(
        self, twin_lakes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        py_lake, rust_lake = twin_lakes
        pages: dict[str, tuple[Any, ...]] = {}
        for name, lake in (("py", py_lake), ("rust", rust_lake)):
            monkeypatch.setattr(db_view, "_LEDGER_READ_CACHE", db_view._make_ledger_read_cache())
            with duckdb.connect() as conn, capture_logs() as logs:
                page, total = db_view.query_window_ledger_page(conn, lake, limit=500, offset=0)
            # THE FALLBACK-ABSENCE PIN: a rust base must never silently defer
            # to the legacy parse (the perf-silent trap — a fallback would
            # STILL return correct rows, so only the LOG proves the fast path
            # genuinely served; the 17-col schema probe passed).
            fallbacks = [
                e
                for e in logs
                if e["event"]
                in (
                    "window_ledger_page.fallback",
                    "window_ledger_page.duckdb_retry",
                    "window_ledger_page.duckdb_fallback",
                )
            ]
            assert fallbacks == [], f"{name} lake deferred/faltered: {fallbacks}"
            assert total == 8 and page
            pages[name] = page
            # Payload equals the legacy cached-parse slice (fast == legacy).
            assert page == db_view.window_ledger_rows_cached(lake)
        assert pages["py"] == pages["rust"]

    def test_counts_total_and_since_identical(self, twin_lakes: tuple[Path, Path]) -> None:
        py_lake, rust_lake = twin_lakes
        # The since instant sits BETWEEN record stamps so the ISO-STRING
        # compare arm genuinely filters (anti-vacuous below).
        since_iso = (_T0 + timedelta(minutes=25)).isoformat()
        results: dict[str, tuple[Any, ...]] = {}
        for name, lake in (("py", py_lake), ("rust", rust_lake)):
            source = db_view._ledger_scan_source(wls.ledger_path(lake))
            with duckdb.connect() as conn:
                total = db_view._window_counts_total(conn, source)
                since = db_view._window_counts_since(conn, source, since_iso)
            results[name] = (total, since)
        assert results["py"] == results["rust"]
        (_, py_total), (_, py_since) = results["py"][0], results["py"][1]
        assert py_total == 8
        # `last_attempt_at >= ?` over the VARCHAR ISO strings: only the two
        # delta-recorded rows (minutes 30 + 40) are on/after minute 25.
        assert py_since == 2

    def test_torn_read_retry_self_heals_over_the_rust_lake(
        self, twin_lakes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The delta save-time prune-race degrade law over RUST-rendered
        files: the first attempt's duckdb.Error retries once over a FRESH
        listing and serves the correct page (never a 500, never divergence)."""
        _, rust_lake = twin_lakes
        monkeypatch.setattr(db_view, "_LEDGER_READ_CACHE", db_view._make_ledger_read_cache())

        class _FlakyConn:
            def __init__(self) -> None:
                self._real = duckdb.connect()
                self.failures_left = 1

            def execute(self, sql: str, params: Any = None) -> Any:
                if self.failures_left > 0:
                    self.failures_left -= 1
                    raise duckdb.Error("simulated torn read (delta pruned mid-scan)")
                return self._real.execute(sql, params)

        conn = _FlakyConn()
        page, total = db_view.query_window_ledger_page(
            conn, rust_lake, limit=500, offset=0, retry_sleep=lambda _s: None
        )
        assert total == 8
        assert page == db_view.window_ledger_rows_cached(rust_lake)


class TestLoadRoundTrip:
    def test_load_window_ledger_rows_identical(self, twin_lakes: tuple[Path, Path]) -> None:
        py_lake, rust_lake = twin_lakes
        py_rows = wls.load_window_ledger(py_lake).to_rows()
        rust_rows = wls.load_window_ledger(rust_lake).to_rows()
        assert py_rows == rust_rows and len(py_rows) == 8
        assert wls.load_window_ledger(py_lake).load_failed is False
        assert wls.load_window_ledger(rust_lake).load_failed is False

    def test_window_ledger_rows_cached_identical(
        self, twin_lakes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        py_lake, rust_lake = twin_lakes
        monkeypatch.setattr(db_view, "_LEDGER_READ_CACHE", db_view._make_ledger_read_cache())
        assert db_view.window_ledger_rows_cached(py_lake) == db_view.window_ledger_rows_cached(
            rust_lake
        )


class TestSchemaProbeAndFooterInertness:
    def test_sixteen_col_probe_outcome_identical(self, twin_lakes: tuple[Path, Path]) -> None:
        # The legacy-12-col defer law: BOTH writers emit the full 17-column
        # schema, so the LIMIT 0 description passes the probe on both — the
        # probe OUTCOME (not just the row payload) is pinned equal.
        py_lake, rust_lake = twin_lakes
        with duckdb.connect() as conn:
            descs = []
            for lake in (py_lake, rust_lake):
                desc = conn.execute(
                    "SELECT * FROM read_parquet(?) LIMIT 0",
                    [wls.ledger_path(lake).as_posix()],
                ).description
                assert desc is not None
                descs.append([(col[0], col[1]) for col in desc])
        assert descs[0] == descs[1]
        names = {name for name, _ in descs[0]}
        assert set(db_view._LEDGER_ROW_COLUMNS).issubset(names)

    def test_footer_inert_to_duckdb_and_bytes_genuinely_differ(
        self, twin_lakes: tuple[Path, Path]
    ) -> None:
        py_lake, rust_lake = twin_lakes
        with duckdb.connect() as conn:
            described = []
            for lake in (py_lake, rust_lake):
                described.append(
                    conn.execute(
                        "DESCRIBE SELECT * FROM read_parquet(?)",
                        [wls.ledger_path(lake).as_posix()],
                    ).fetchall()
                )
        assert described[0] == described[1]
        # ANTI-VACUOUS: the raw bytes DO differ (the #1917 encoder-identity
        # divergence is REAL in this very fixture — the parity above tested
        # something), and the rust file is genuinely polars-authored.
        py_bytes = wls.ledger_path(py_lake).read_bytes()
        rust_bytes = wls.ledger_path(rust_lake).read_bytes()
        assert py_bytes != rust_bytes
        py_md = pq.read_metadata(wls.ledger_path(py_lake))
        rust_md = pq.read_metadata(wls.ledger_path(rust_lake))
        assert py_md.created_by.startswith("parquet-cpp-arrow")
        assert not rust_md.created_by.startswith("parquet-cpp-arrow")


class TestEmptyLedgerWatchItem:
    def test_empty_ledger_reads_identically_everywhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The MEASURED empty-file arm: polars writes ZERO row groups where
        pyarrow writes one empty group — but the 17 columns live in the
        FOOTER schema, so every reader (pyarrow · polars · DuckDB describe +
        read · the store loader · the page query) is identical. Measured
        PASS ⇒ the ONE pre-authorized narrowing (special-casing EMPTY input
        to the Python oracle) was NOT needed and is NOT taken."""
        lakes: dict[str, Path] = {}
        for name, rust in (("py", False), ("rust", True)):
            lake = tmp_path / name
            lake.mkdir()
            _set_arm(monkeypatch, rust=rust)
            wls.save_window_ledger(WindowLedger(), lake)
            assert lake_rust_ledger_render.rust_ledger_render_disabled() is False
            lakes[name] = lake
        _set_arm(monkeypatch, rust=False)
        py_file = wls.ledger_path(lakes["py"])
        rust_file = wls.ledger_path(lakes["rust"])
        # The documented structure divergence — asserted so a future polars
        # that starts writing an empty group re-opens this arm consciously.
        assert pq.read_metadata(py_file).num_row_groups == 1
        assert pq.read_metadata(rust_file).num_row_groups == 0
        for reader_rows in (
            lambda f: pq.read_table(f).to_pylist(),
            lambda f: pl.read_parquet(f).rows(),
        ):
            assert reader_rows(py_file) == reader_rows(rust_file) == []
        assert pq.read_table(py_file).schema.names == pq.read_table(rust_file).schema.names
        with duckdb.connect() as conn:
            descs = []
            for f in (py_file, rust_file):
                desc = conn.execute(
                    "SELECT * FROM read_parquet(?) LIMIT 0", [f.as_posix()]
                ).description
                assert desc is not None
                descs.append([(col[0], col[1]) for col in desc])
                assert conn.execute(
                    "SELECT COUNT(*) FROM read_parquet(?)", [f.as_posix()]
                ).fetchone() == (0,)
            assert descs[0] == descs[1] and len(descs[0]) == 17
        for lake in lakes.values():
            assert wls.load_window_ledger(lake).to_rows() == []
            monkeypatch.setattr(db_view, "_LEDGER_READ_CACHE", db_view._make_ledger_read_cache())
            with duckdb.connect() as conn2:
                page, total = db_view.query_window_ledger_page(conn2, lake, limit=10, offset=0)
            assert (page, total) == ((), 0)


class TestStatTokenExpectedMove:
    def test_rewrite_moves_the_stamp_token_expected(
        self, twin_lakes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stat-token pin is EXPECTED-MOVE, never equality: the tokens
        (base mtime + delta name set) key freshness caches, and a flag-ON
        rewrite moving them is exactly the invalidation the readers rely on
        (the #1918 no-consumer-hashes-bytes finding — tokens are stat-shaped,
        not content-hashes, so the encoder swap is invisible to them EXCEPT
        as an ordinary rewrite)."""
        _, rust_lake = twin_lakes
        before = db_view._ledger_stamp_token(wls.ledger_path(rust_lake))
        _set_arm(monkeypatch, rust=True)
        live = wls.load_window_ledger(rust_lake)
        live.record(
            _key(2), raw_bars=9, error_type=None, bars_appended=9, now=_T0 + timedelta(hours=2)
        )
        wls.save_window_ledger(live, rust_lake)
        _set_arm(monkeypatch, rust=False)
        after = db_view._ledger_stamp_token(wls.ledger_path(rust_lake))
        assert before != after  # EXPECTED move — a full save moves base mtime + prunes deltas
