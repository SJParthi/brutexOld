"""READER-PARITY gates for the CANONICAL window-ledger render (twin-lake battery).

DEC-RUST-RUNID-STORES-001 — ADAPTED from the DEC-LAKE-RUST-LEDGERSTORE-CONVERT-001
(#1918) flag-arm battery per the coordinator ruling of 2026-07-18 (the #1918
render-plane arm is SUPERSEDED-IN-PART: the ``brutex_identity`` kernel's
parquet-rs render is unconditionally the ONE production writer — there is no
flag arm left to twin against). The suite's SUBSTANCE is kept and re-targeted:
the #1917 cross-encoder ROW FIXED POINT stays the BINDING correctness standard
— bytes canon moved, row truth did not — so TWIN LAKES still drive every REAL
production read path, now as CANONICAL vs LEGACY:

* the CANONICAL lake — written through the REAL production
  ``save_window_ledger`` / ``append_ledger_delta`` (kernel parquet-rs bytes);
* the LEGACY twin — the SAME files re-encoded row-preserving through pyarrow's
  zero-kwarg ``pq.write_table`` (the retired writer's encoder + its snappy
  default), i.e. the exact byte class every pre-conversion lake on disk holds.

Gates (the #1918 battery, re-targeted):

(a) tri-reader ordered-row equivalence (pyarrow · polars · DuckDB) per file;
(b) the ``dedup_latest_source_sql`` base+delta fold with ALL FOUR mix arms
    (pure-canonical · pure-legacy · legacy-base+canonical-deltas ·
    canonical-base+legacy-deltas); ``query_window_ledger_page``
    payload-identical WITH the FALLBACK-ABSENCE pin (a canonical base must
    never silently defer to the legacy parse);
    ``_window_counts_total`` + the ``_window_counts_since``
    ``last_attempt_at >= ?`` ISO-STRING compare arm;
(c) ``load_window_ledger`` / ``window_ledger_rows_cached`` round-trips;
(d) the 16-col schema-probe outcome equality;
(e) footer-inertness (DuckDB description identical) + the ANTI-VACUOUS raw
    bytes-differ assert (the gates test the REAL encoder divergence —
    ``parquet-rs`` vs ``parquet-cpp-arrow`` — not a no-op);
(f) the EMPTY-ledger arm (the kernel writes ONE empty row group — measured —
    and every reader is identical off the FOOTER schema);
(g) the stat-token EXPECTED-move pin (a rewrite MUST move the token — never
    an equality pin) + the delta torn-read retry self-heal over the
    canonical lake.

Runs wherever the hard ``brutex_identity`` wheel + pyarrow/polars/duckdb are
installed (main CI included — no optional wheel, no flag, no skip arm beyond
the plain dependency importorskips).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

pl = pytest.importorskip("polars")
pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
duckdb = pytest.importorskip("duckdb")

from brutex.dashboard import db_view  # noqa: E402
from brutex.data import window_ledger_store as wls  # noqa: E402
from brutex.data.fetch_coverage import WindowKey  # noqa: E402
from brutex.data.window_ledger import WindowLedger  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

if TYPE_CHECKING:
    pass

_T0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _key(i: int) -> WindowKey:
    return WindowKey(
        market="groww" if i % 2 == 0 else "groww-option",
        security_id=f"SYM{i:02d}",
        interval="1minute" if i % 2 == 0 else "1day",
        start=date(2025, 1, 1 + (i % 3) * 9),
        end=date(2025, 1, 31 - (i % 4)),
    )


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


def _legacy_reencode(path: Path) -> None:
    """Re-encode one canonical file IN PLACE through the RETIRED writer's encoder.

    ``pq.read_table`` → zero-kwarg ``pq.write_table`` — pyarrow authorship
    (``created_by`` "parquet-cpp-arrow"), the snappy default, one row group:
    exactly the byte class the retired ``_rows_to_table`` writer minted, with
    the ROW CONTENT preserved verbatim (the fixed point under test).
    """
    table = pq.read_table(path)
    pq.write_table(table, path)  # type: ignore[no-untyped-call, unused-ignore]


def _build_lake(lake: Path, *, legacy_base: bool = False, legacy_deltas: bool = False) -> Path:
    """One seeded lake through the REAL production writer, optionally re-encoded.

    The production write path is UNCONDITIONALLY the ``brutex_identity``
    kernel render; the legacy arms are minted AFTERWARD by row-preserving
    re-encode (never a second production writer — there is none)."""
    lake.mkdir(parents=True, exist_ok=True)
    _seed_base(lake)
    _seed_deltas(lake)
    if legacy_base:
        _legacy_reencode(wls.ledger_path(lake))
    if legacy_deltas:
        for delta in wls.list_ledger_deltas(lake):
            _legacy_reencode(delta)
    return lake


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hermetic: the RETIRED render flag would trip the store's loud
    # require_ledger_render_env_unset refusal (its own suite pins that law).
    monkeypatch.delenv(wls.LEDGER_RENDER_ENV, raising=False)
    monkeypatch.setattr(db_view, "_LEDGER_READ_CACHE", db_view._make_ledger_read_cache())


@pytest.fixture
def twin_lakes(tmp_path: Path) -> tuple[Path, Path]:
    """(legacy_lake, canonical_lake) — identical records, opposite encoders."""
    legacy_lake = _build_lake(tmp_path / "legacy", legacy_base=True, legacy_deltas=True)
    canonical_lake = _build_lake(tmp_path / "canonical")
    return legacy_lake, canonical_lake


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
        legacy_lake, canonical_lake = twin_lakes
        legacy_files = _ledger_files(legacy_lake)
        canonical_files = _ledger_files(canonical_lake)
        assert len(legacy_files) == len(canonical_files) == 3  # base + 2 deltas
        for legacy_file, canonical_file in zip(legacy_files, canonical_files, strict=True):
            # pyarrow — ordered per-row dicts (row ORDER preserved, not just set).
            legacy_rows = pq.read_table(legacy_file).to_pylist()
            canonical_rows = pq.read_table(canonical_file).to_pylist()
            assert legacy_rows == canonical_rows and canonical_rows
            # polars — ordered tuples.
            assert pl.read_parquet(legacy_file).rows() == pl.read_parquet(canonical_file).rows()
            # DuckDB — ordered by the 5-part window key (SQL scan order over a
            # single file is storage order, but the key sort is the contract).
            with duckdb.connect() as conn:
                order_sql = ", ".join(f'"{c}"' for c in db_view._WINDOW_KEY_COLUMNS)
                legacy_sql = conn.execute(
                    f"SELECT * FROM read_parquet(?) ORDER BY {order_sql}",  # noqa: S608
                    [legacy_file.as_posix()],
                ).fetchall()
                canonical_sql = conn.execute(
                    f"SELECT * FROM read_parquet(?) ORDER BY {order_sql}",  # noqa: S608
                    [canonical_file.as_posix()],
                ).fetchall()
            assert legacy_sql == canonical_sql


class TestDedupLatestFoldMixArms:
    def test_all_four_base_delta_mix_arms_fold_identically(self, tmp_path: Path) -> None:
        """pure-canonical · pure-legacy · legacy-base+canonical-deltas ·
        canonical-base+legacy-deltas — the latest-wins fold (base always
        ranks LAST; deltas newest-first) returns IDENTICAL rows over every
        encoder-mix combination (the exact mixed-lake shape a pre-conversion
        base overlaid by post-conversion deltas produces in production)."""
        arms = {
            "pure_canonical": (False, False),
            "pure_legacy": (True, True),
            "legacy_base_canonical_deltas": (True, False),
            "canonical_base_legacy_deltas": (False, True),
        }
        folded: dict[str, list[tuple[Any, ...]]] = {}
        for name, (legacy_base, legacy_deltas) in arms.items():
            lake = _build_lake(
                tmp_path / name, legacy_base=legacy_base, legacy_deltas=legacy_deltas
            )
            folded[name] = _fold_rows(lake)
        reference = folded["pure_canonical"]
        assert reference and len(reference) == 8  # 6 base + 1 planned + 1 delta-new
        # The demotion delta genuinely bites (anti-vacuous): SYM01 reads FAILED.
        demoted = [r for r in reference if r[1] == "SYM01"]
        assert demoted and demoted[0][5] == "FAILED"
        for name, rows in folded.items():
            assert rows == reference, f"mix arm {name} diverged from the canonical fold"

    def test_empty_base_plus_delta_folds_identically(self, tmp_path: Path) -> None:
        """An EMPTY base UNION'd with a delta through the REAL fold path: the
        ``read_parquet(?, union_by_name=true, filename=true,
        file_row_number=true)`` scan must union an empty canonical base with
        a delta identically to the legacy-encoded twin, and the page query
        must serve BOTH fast-path (NO fallback log)."""
        lakes: dict[str, Path] = {}
        for name, legacy in (("canonical", False), ("legacy", True)):
            lake = tmp_path / name
            lake.mkdir()
            wls.save_window_ledger(WindowLedger(), lake)  # the EMPTY base
            live = wls.load_window_ledger(lake)
            live.record(_key(0), raw_bars=5, error_type=None, bars_appended=5, now=_T0)
            assert wls.append_ledger_delta(live, lake) == 1
            if legacy:
                _legacy_reencode(wls.ledger_path(lake))
                for delta in wls.list_ledger_deltas(lake):
                    _legacy_reencode(delta)
            lakes[name] = lake
        # Both empty bases carry ONE (empty) row group — the kernel matches
        # the retired pyarrow writer's structure here (measured; the polars
        # 0-row-group divergence of the retired #1918 seam is gone with it).
        assert pq.read_metadata(wls.ledger_path(lakes["canonical"])).num_row_groups == 1
        assert pq.read_metadata(wls.ledger_path(lakes["legacy"])).num_row_groups == 1
        # The REAL fold path (db_view._ledger_scan_source →
        # lake_read.dedup_latest_source_sql): rows identical across encoders.
        canonical_fold = _fold_rows(lakes["canonical"])
        legacy_fold = _fold_rows(lakes["legacy"])
        assert canonical_fold == legacy_fold
        assert len(canonical_fold) == 1 and canonical_fold[0][1] == "SYM00"  # the delta row
        # And the page query serves BOTH fast-path — no fallback/retry log.
        for lake in lakes.values():
            db_view._reset_ledger_read_cache()
            with duckdb.connect() as conn, capture_logs() as logs:
                page, total = db_view.query_window_ledger_page(conn, lake, limit=10, offset=0)
            assert [e for e in logs if e["event"].startswith("window_ledger_page.")] == []
            assert total == 1 and page == tuple(canonical_fold)


class TestWindowLedgerPageParity:
    def test_page_payload_identical_and_no_fallback_on_the_canonical_base(
        self, twin_lakes: tuple[Path, Path]
    ) -> None:
        legacy_lake, canonical_lake = twin_lakes
        pages: dict[str, tuple[Any, ...]] = {}
        for name, lake in (("legacy", legacy_lake), ("canonical", canonical_lake)):
            db_view._reset_ledger_read_cache()
            with duckdb.connect() as conn, capture_logs() as logs:
                page, total = db_view.query_window_ledger_page(conn, lake, limit=500, offset=0)
            # THE FALLBACK-ABSENCE PIN: a canonical base must never silently
            # defer to the legacy parse (the perf-silent trap — a fallback
            # would STILL return correct rows, so only the LOG proves the
            # fast path genuinely served; the 16-col schema probe passed).
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
        assert pages["legacy"] == pages["canonical"]

    def test_counts_total_and_since_identical(self, twin_lakes: tuple[Path, Path]) -> None:
        legacy_lake, canonical_lake = twin_lakes
        # The since instant sits BETWEEN record stamps so the ISO-STRING
        # compare arm genuinely filters (anti-vacuous below).
        since_iso = (_T0 + timedelta(minutes=25)).isoformat()
        results: dict[str, tuple[Any, ...]] = {}
        for name, lake in (("legacy", legacy_lake), ("canonical", canonical_lake)):
            source = db_view._ledger_scan_source(wls.ledger_path(lake))
            with duckdb.connect() as conn:
                total = db_view._window_counts_total(conn, source)
                since = db_view._window_counts_since(conn, source, since_iso)
            results[name] = (total, since)
        assert results["legacy"] == results["canonical"]
        (_, py_total), (_, py_since) = results["canonical"][0], results["canonical"][1]
        assert py_total == 8
        # `last_attempt_at >= ?` over the VARCHAR ISO strings: only the two
        # delta-recorded rows (minutes 30 + 40) are on/after minute 25.
        assert py_since == 2

    def test_torn_read_retry_self_heals_over_the_canonical_lake(
        self, twin_lakes: tuple[Path, Path]
    ) -> None:
        """The delta save-time prune-race degrade law over KERNEL-rendered
        files: the first attempt's duckdb.Error retries once over a FRESH
        listing and serves the correct page (never a 500, never divergence)."""
        _, canonical_lake = twin_lakes
        db_view._reset_ledger_read_cache()

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
            conn, canonical_lake, limit=500, offset=0, retry_sleep=lambda _s: None
        )
        assert total == 8
        assert page == db_view.window_ledger_rows_cached(canonical_lake)


class TestLoadRoundTrip:
    def test_load_window_ledger_rows_identical(self, twin_lakes: tuple[Path, Path]) -> None:
        legacy_lake, canonical_lake = twin_lakes
        legacy_rows = wls.load_window_ledger(legacy_lake).to_rows()
        canonical_rows = wls.load_window_ledger(canonical_lake).to_rows()
        assert legacy_rows == canonical_rows and len(canonical_rows) == 8
        assert wls.load_window_ledger(legacy_lake).load_failed is False
        assert wls.load_window_ledger(canonical_lake).load_failed is False

    def test_window_ledger_rows_cached_identical(self, twin_lakes: tuple[Path, Path]) -> None:
        legacy_lake, canonical_lake = twin_lakes
        db_view._reset_ledger_read_cache()
        assert db_view.window_ledger_rows_cached(legacy_lake) == db_view.window_ledger_rows_cached(
            canonical_lake
        )


class TestSchemaProbeAndFooterInertness:
    def test_sixteen_col_probe_outcome_identical(self, twin_lakes: tuple[Path, Path]) -> None:
        # The legacy-12-col defer law: BOTH encoders carry the full 16-column
        # schema, so the LIMIT 0 description passes the probe on both — the
        # probe OUTCOME (not just the row payload) is pinned equal.
        legacy_lake, canonical_lake = twin_lakes
        with duckdb.connect() as conn:
            descs = []
            for lake in (legacy_lake, canonical_lake):
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
        legacy_lake, canonical_lake = twin_lakes
        with duckdb.connect() as conn:
            described = []
            for lake in (legacy_lake, canonical_lake):
                described.append(
                    conn.execute(
                        "DESCRIBE SELECT * FROM read_parquet(?)",
                        [wls.ledger_path(lake).as_posix()],
                    ).fetchall()
                )
        assert described[0] == described[1]
        # ANTI-VACUOUS: the raw bytes DO differ (the #1917 encoder-identity
        # divergence is REAL in this very fixture — the parity above tested
        # something), and each file is genuinely its own encoder's.
        legacy_bytes = wls.ledger_path(legacy_lake).read_bytes()
        canonical_bytes = wls.ledger_path(canonical_lake).read_bytes()
        assert legacy_bytes != canonical_bytes
        legacy_md = pq.read_metadata(wls.ledger_path(legacy_lake))
        canonical_md = pq.read_metadata(wls.ledger_path(canonical_lake))
        assert legacy_md.created_by.startswith("parquet-cpp-arrow")
        assert canonical_md.created_by.startswith("parquet-rs")


class TestEmptyLedgerWatchItem:
    def test_empty_ledger_reads_identically_everywhere(self, tmp_path: Path) -> None:
        """The EMPTY-ledger arm: the kernel writes ONE empty row group for a
        0-row save (measured — matching the retired pyarrow writer's
        structure, so the retired #1918 polars 0-row-group watch-item is
        moot), and every reader (pyarrow · polars · DuckDB describe + read ·
        the store loader · the page query) reads the empty canonical file
        and its legacy re-encode identically off the FOOTER schema."""
        lakes: dict[str, Path] = {}
        for name, legacy in (("canonical", False), ("legacy", True)):
            lake = tmp_path / name
            lake.mkdir()
            wls.save_window_ledger(WindowLedger(), lake)
            if legacy:
                _legacy_reencode(wls.ledger_path(lake))
            lakes[name] = lake
        canonical_file = wls.ledger_path(lakes["canonical"])
        legacy_file = wls.ledger_path(lakes["legacy"])
        assert pq.read_metadata(canonical_file).num_row_groups == 1
        assert pq.read_metadata(legacy_file).num_row_groups == 1
        for reader_rows in (
            lambda f: pq.read_table(f).to_pylist(),
            lambda f: pl.read_parquet(f).rows(),
        ):
            assert reader_rows(canonical_file) == reader_rows(legacy_file) == []
        assert pq.read_table(canonical_file).schema.names == pq.read_table(legacy_file).schema.names
        with duckdb.connect() as conn:
            descs = []
            for f in (canonical_file, legacy_file):
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
            db_view._reset_ledger_read_cache()
            with duckdb.connect() as conn2:
                page, total = db_view.query_window_ledger_page(conn2, lake, limit=10, offset=0)
            assert (page, total) == ((), 0)


class TestStatTokenExpectedMove:
    def test_rewrite_moves_the_stamp_token_expected(self, twin_lakes: tuple[Path, Path]) -> None:
        """The stat-token pin is EXPECTED-MOVE, never equality: the tokens
        (base mtime + delta name set) key freshness caches, and a rewrite
        moving them is exactly the invalidation the readers rely on (the
        #1918 no-consumer-hashes-bytes finding — tokens are stat-shaped, not
        content-hashes, so the encoder re-baseline is invisible to them
        EXCEPT as an ordinary rewrite)."""
        _, canonical_lake = twin_lakes
        before = db_view._ledger_stamp_token(wls.ledger_path(canonical_lake))
        live = wls.load_window_ledger(canonical_lake)
        live.record(
            _key(2), raw_bars=9, error_type=None, bars_appended=9, now=_T0 + timedelta(hours=2)
        )
        wls.save_window_ledger(live, canonical_lake)
        after = db_view._ledger_stamp_token(wls.ledger_path(canonical_lake))
        assert before != after  # EXPECTED move — a full save moves base mtime + prunes deltas
