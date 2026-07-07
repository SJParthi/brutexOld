"""Manifest-first dataset discovery through ``db_view.list_candle_datasets``.

DEC-LAKE-MANIFEST-O1-001 integration pins, on a lake seeded with the REAL
``ParquetWriter`` (spot INDEX + FNO FUT + FNO CE) plus a #911-style drift-key
directory:

* byte-identical dataset lists manifest-vs-walk (incl. filter permutations);
* restart simulation — a manifest-served discovery runs ZERO walk (spied);
* new-dataset upsert during a live append is visible WITHOUT a walk;
* heal/quarantine invalidation → graceful walk fallback + re-list;
* missing / stale / corrupt / truncated manifest → graceful walk fallback
  (never a 500, never a missed dataset);
* concurrent writers upserting distinct datasets stay consistent.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.dashboard import db_view
from brutex.data import lake_manifest as lm
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.window_ledger_store import ledger_path
from structlog.testing import capture_logs


def _bar(minute: int = 0) -> CandleBar:
    return CandleBar(
        timestamp=datetime(2024, 6, 3, 4, minute, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.50"),
        volume=10,
        open_interest=None,
    )


def _seed_ledger(lake: Path) -> None:
    target = ledger_path(lake)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"ledger-v1")


def _seed_lake(lake: Path) -> ParquetWriter:
    """A realistic mixed lake: spot INDEX (1m + 1d), FNO FUT + CE, a drift dir."""
    _seed_ledger(lake)
    writer = ParquetWriter(lake_root=lake)
    writer.write(
        exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute", bars=[_bar(0)]
    )
    writer.write(exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1day", bars=[_bar(1)])
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-26Jun2024-FUT",
        interval="1minute",
        bars=[_bar(2)],
    )
    writer.write(
        exchange="NSE",
        segment="FNO",
        symbol="NSE-NIFTY-26Jun2024-24000-CE",
        interval="1minute",
        bars=[_bar(3)],
    )
    # #911-style drift-key directory (a legacy "NSE-M&M" symbol dir): the walk
    # lists ANY symbol dir name holding a parquet — the manifest must too.
    drift = lake / "bars" / "NSE" / "CASH" / "NSE-M&M" / "1minute" / "2024"
    drift.mkdir(parents=True)
    (drift / "06.parquet").write_bytes(b"")  # existence is all the walk checks
    # Foreign / invalid coordinate dirs the walk SKIPS — the manifest must too.
    bogus = lake / "bars" / "MCX" / "INDEX" / "GOLD" / "1minute" / "2024"
    bogus.mkdir(parents=True)
    (bogus / "06.parquet").write_bytes(b"")
    bogus_tf = lake / "bars" / "NSE" / "INDEX" / "NIFTY" / "9lives" / "2024"
    bogus_tf.mkdir(parents=True)
    (bogus_tf / "06.parquet").write_bytes(b"")
    return writer


def _walk_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Spy on the legacy walk — counts real invocations, results untouched."""
    calls = {"n": 0}
    real = db_view._walk_candle_datasets

    def _counted(*args: Any, **kwargs: Any) -> list[dict[str, str]]:
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(db_view, "_walk_candle_datasets", _counted)
    return calls


class TestManifestFirstDiscovery:
    def test_walk_vs_manifest_byte_identity(self, tmp_path: Path) -> None:
        """The manifest-served list is byte-identical to the walk's — incl. drift keys."""
        _seed_lake(tmp_path)
        first = db_view.list_candle_datasets(tmp_path)  # walk + rebuild
        assert lm.manifest_meta_path(tmp_path).exists()
        second = db_view.list_candle_datasets(tmp_path)  # manifest-served
        assert second == first
        symbols = {d["symbol"] for d in first}
        assert "NSE-M&M" in symbols  # the drift-key dir stays listed
        assert not any(d["exchange"] == "MCX" for d in first)  # invalid exch skipped
        assert not any(d["interval"] == "9lives" for d in first)  # invalid tf skipped
        assert json.dumps(first, sort_keys=False) == json.dumps(second, sort_keys=False)

    def test_filter_permutations_match_walk(self, tmp_path: Path) -> None:
        """exclude_segments / intervals filters give identical results on either path."""
        _seed_lake(tmp_path)
        db_view.list_candle_datasets(tmp_path)  # build the manifest
        for kwargs in (
            {"exclude_segments": frozenset({"FNO"})},
            {"intervals": ("1minute",)},
            {"exclude_segments": frozenset({"FNO"}), "intervals": ("1day",)},
        ):
            served = db_view.list_candle_datasets(tmp_path, **kwargs)  # type: ignore[arg-type]
            walked = db_view._walk_candle_datasets(
                tmp_path,
                exclude_segments=kwargs.get("exclude_segments", frozenset()),  # type: ignore[arg-type]
                intervals=kwargs.get("intervals"),  # type: ignore[arg-type]
            )
            assert served == walked

    def test_restart_simulation_zero_walks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 'restarted process' (no in-process cache) serves discovery with NO walk.

        The manifest module holds ZERO process state — validity is decided
        entirely from the on-disk meta + ledger stat — so calling into a fresh
        spy IS the restart: nothing warm survives, yet the walk never runs.
        """
        _seed_lake(tmp_path)
        db_view.list_candle_datasets(tmp_path)  # 'previous process' built it
        calls = _walk_spy(monkeypatch)
        listed = db_view.list_candle_datasets(tmp_path)
        assert calls["n"] == 0
        assert {d["symbol"] for d in listed} >= {"NIFTY", "NSE-M&M"}

    def test_filtered_call_never_builds_manifest(self, tmp_path: Path) -> None:
        """A filtered walk sees a partial population — it must NOT persist one."""
        _seed_lake(tmp_path)
        db_view.list_candle_datasets(tmp_path, exclude_segments=frozenset({"FNO"}))
        assert not lm.manifest_meta_path(tmp_path).exists()
        db_view.list_candle_datasets(tmp_path, intervals=("1minute",))
        assert not lm.manifest_meta_path(tmp_path).exists()

    def test_stale_manifest_falls_back_and_rebuilds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ledger move (a pull) stales the manifest → one walk → served again."""
        writer = _seed_lake(tmp_path)
        db_view.list_candle_datasets(tmp_path)
        # A pull lands: new dataset + the ledger checkpoint moves.
        writer.write(
            exchange="BSE", segment="INDEX", symbol="SENSEX", interval="1minute", bars=[_bar(4)]
        )
        target = ledger_path(tmp_path)
        target.write_bytes(b"ledger-v2-moved")
        calls = _walk_spy(monkeypatch)
        listed = db_view.list_candle_datasets(tmp_path)
        assert calls["n"] == 1  # stale → exactly one walk + rebuild
        assert any(d["symbol"] == "SENSEX" for d in listed)
        assert db_view.list_candle_datasets(tmp_path) == listed
        assert calls["n"] == 1  # rebuilt → manifest-served again

    def test_corrupt_manifest_falls_back_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Corrupt/truncated manifest → walk fallback, never a raise, no lost dataset."""
        _seed_lake(tmp_path)
        expected = db_view.list_candle_datasets(tmp_path)
        lm.manifest_data_path(tmp_path).write_text("garbage{\n", encoding="utf-8")
        calls = _walk_spy(monkeypatch)
        assert db_view.list_candle_datasets(tmp_path) == expected
        assert calls["n"] == 1

    def test_new_dataset_upsert_visible_without_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live append that CREATES a dataset appears manifest-first (no walk).

        The prebuild shape: derived-TF writers create datasets WITHOUT moving
        the window ledger, and the writer-synchronous ``record_dataset`` keeps
        the manifest complete, so discovery stays walk-free.
        """
        writer = _seed_lake(tmp_path)
        db_view.list_candle_datasets(tmp_path)  # build the base
        writer.write(
            exchange="NSE", segment="INDEX", symbol="NIFTY", interval="5minute", bars=[_bar(5)]
        )
        calls = _walk_spy(monkeypatch)
        listed = db_view.list_candle_datasets(tmp_path)
        assert calls["n"] == 0
        assert {
            "kind": "spot",
            "exchange": "NSE",
            "segment": "INDEX",
            "symbol": "NIFTY",
            "interval": "5minute",
        } in listed

    def test_existing_dataset_month_growth_adds_no_row(self, tmp_path: Path) -> None:
        """A new month of an EXISTING dataset never grows the manifest."""
        writer = _seed_lake(tmp_path)
        db_view.list_candle_datasets(tmp_path)
        before = json.loads(lm.manifest_meta_path(tmp_path).read_text(encoding="utf-8"))
        writer.write(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[
                CandleBar(
                    timestamp=datetime(2024, 7, 1, 4, 0, tzinfo=UTC),
                    open=Decimal("100.00"),
                    high=Decimal("101.00"),
                    low=Decimal("99.00"),
                    close=Decimal("100.50"),
                    volume=10,
                    open_interest=None,
                )
            ],
        )
        after = json.loads(lm.manifest_meta_path(tmp_path).read_text(encoding="utf-8"))
        assert after["rows"] == before["rows"]

    def test_heal_quarantine_invalidates_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A remove-all heal (quarantine-MOVE) invalidates → next list walks honestly."""
        writer = _seed_lake(tmp_path)
        db_view.list_candle_datasets(tmp_path)
        dest = (
            tmp_path
            / "bars"
            / "NSE"
            / "FNO"
            / "NSE-NIFTY-26Jun2024-24000-CE"
            / "1minute"
            / "2024"
            / "06.parquet"
        )
        writer.heal_partition(
            dest,
            remove_timestamps=frozenset({_bar(3).timestamp}),
            quarantine_to=tmp_path / ".quarantine" / "t" / "06.parquet",
        )
        assert not lm.manifest_meta_path(tmp_path).exists()  # invalidated
        calls = _walk_spy(monkeypatch)
        listed = db_view.list_candle_datasets(tmp_path)
        assert calls["n"] == 1  # honest walk fallback
        assert not any(d["symbol"] == "NSE-NIFTY-26Jun2024-24000-CE" for d in listed)

    def test_concurrent_writers_upserting_stay_consistent(self, tmp_path: Path) -> None:
        """N threads creating distinct datasets: every row lands, manifest validates."""
        writer = _seed_lake(tmp_path)
        db_view.list_candle_datasets(tmp_path)  # base
        symbols = [f"SYM{i}" for i in range(8)]
        barrier = threading.Barrier(len(symbols))

        def _work(symbol: str) -> None:
            barrier.wait(timeout=10)
            writer.write(
                exchange="NSE", segment="CASH", symbol=symbol, interval="1minute", bars=[_bar(6)]
            )

        threads = [threading.Thread(target=_work, args=(s,)) for s in symbols]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        loaded = lm.load_manifest_datasets(tmp_path)
        assert loaded is not None  # rows == lines (the flock kept meta consistent)
        assert {r["symbol"] for r in loaded} >= set(symbols)
        assert db_view.list_candle_datasets(tmp_path) == db_view._walk_candle_datasets(
            tmp_path, exclude_segments=frozenset(), intervals=None
        )

    def test_walk_skipped_rebuild_when_token_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No freshness token (ledger stat error) → walk served, no manifest persisted."""
        _seed_lake(tmp_path)
        monkeypatch.setattr(db_view, "ledger_stat_token", lambda _lake: None)
        monkeypatch.setattr(lm, "ledger_stat_token", lambda _lake: None)
        listed = db_view.list_candle_datasets(tmp_path)
        assert any(d["symbol"] == "NIFTY" for d in listed)
        assert not lm.manifest_meta_path(tmp_path).exists()

    def test_mid_walk_ledger_move_rebuilds_stale_base_and_converges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ledger move DURING the walk → STALE-stamped base + one INFO event.

        DEC-O1-CLAIMS-HONESTY-001 round-6 review fold: the interim
        SKIP-on-mismatch design is REVERTED — the rebuild lands stamped with
        the PRE-walk token (never freshness the walk did not have; the
        load-side gate refuses to serve it as-is) because that base is the
        seed `record_dataset`'s write-side convergence layers onto. Fails
        before the fix: the SKIP left no base, so `record_dataset` no-oped
        (`record_skipped_no_base`) and the prebuild could never converge the
        manifest to served-hot after a mid-pull discovery.
        """
        _seed_lake(tmp_path)
        pre_walk_token = db_view.ledger_stat_token(tmp_path)
        real_walk = db_view._walk_candle_datasets

        def _walk_then_move(*args: Any, **kwargs: Any) -> list[dict[str, str]]:
            found = real_walk(*args, **kwargs)
            # The mid-walk race: a pull checkpoints the ledger DURING the walk.
            ledger_path(tmp_path).write_bytes(b"ledger-v2-moved-mid-walk")
            return found

        monkeypatch.setattr(db_view, "_walk_candle_datasets", _walk_then_move)
        with capture_logs() as logs:
            listed = db_view.list_candle_datasets(tmp_path)
        assert any(d["symbol"] == "NIFTY" for d in listed)  # walk result correct
        # The base IS persisted — stamped with the PRE-walk (now stale) token.
        meta = json.loads(lm.manifest_meta_path(tmp_path).read_text(encoding="utf-8"))
        assert meta["ledger_token"] == pre_walk_token
        # Never false-fresh: the stale stamp is refused for reads as-is.
        assert lm.load_manifest_datasets(tmp_path) is None
        stale = [e for e in logs if e["event"] == "db_view.manifest_rebuilt_stale"]
        assert len(stale) == 1
        assert stale[0]["pre_walk_token"] == pre_walk_token
        assert stale[0]["post_walk_token"] not in (None, pre_walk_token)
        # THE CONVERGENCE SEED (the fails-before pin): a prebuild-style
        # record layers onto the stale base + re-stamps the CURRENT token,
        # converging the manifest to served-hot with NO further walk.
        assert (
            lm.record_dataset(
                tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", interval="5minute"
            )
            is True
        )
        monkeypatch.setattr(db_view, "_walk_candle_datasets", real_walk)
        calls = _walk_spy(monkeypatch)
        converged = db_view.list_candle_datasets(tmp_path)
        assert calls["n"] == 0  # manifest-served — zero walks
        assert {
            "kind": "spot",
            "exchange": "NSE",
            "segment": "INDEX",
            "symbol": "NIFTY",
            "interval": "5minute",
        } in converged

    def test_post_walk_token_none_rebuilds_with_prewalk_stamp_and_logs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable post-walk stat (`None`) still seeds the base, observed.

        The re-read is OBSERVE-ONLY: it cannot prove quiescence, so the event
        fires, but the rebuild still lands stamped with the PRE-walk token
        (safe either way — the load-side gate compares against the CURRENT
        token at read time).
        """
        _seed_lake(tmp_path)
        real_token = db_view.ledger_stat_token(tmp_path)
        assert real_token is not None
        tokens = iter([real_token, None])
        # Patch the db_view REBINDING (the monkeypatch-seam the gate must call).
        monkeypatch.setattr(db_view, "ledger_stat_token", lambda _lake: next(tokens))
        with capture_logs() as logs:
            listed = db_view.list_candle_datasets(tmp_path)
        assert any(d["symbol"] == "NIFTY" for d in listed)
        meta = json.loads(lm.manifest_meta_path(tmp_path).read_text(encoding="utf-8"))
        assert meta["ledger_token"] == real_token  # the PRE-walk stamp
        stale = [e for e in logs if e["event"] == "db_view.manifest_rebuilt_stale"]
        assert len(stale) == 1
        assert stale[0]["pre_walk_token"] == real_token
        assert stale[0]["post_walk_token"] is None


def test_manifest_rows_with_invalid_coordinates_are_screened(tmp_path: Path) -> None:
    """Manifest rows failing the walk's dir-level validity screens are dropped.

    The walk never LISTS an invalid exchange/interval dir, so a served manifest
    must apply the SAME screens — a foreign row (e.g. hand-edited or from a
    future format drift) can never surface a coordinate the walk would hide.
    """
    _seed_ledger(tmp_path)
    token = lm.ledger_stat_token(tmp_path)
    assert token is not None
    rows = [
        {"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "1minute"},
        {"exchange": "MCX", "segment": "INDEX", "symbol": "GOLD", "interval": "1minute"},
        {"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "9lives"},
        {"exchange": "NSE", "segment": "BOGUS", "symbol": "NIFTY", "interval": "1minute"},
    ]
    assert lm.rebuild_manifest(tmp_path, rows, ledger_token=token) is True
    listed = db_view.list_candle_datasets(tmp_path)
    assert listed == [
        {
            "kind": "spot",
            "exchange": "NSE",
            "segment": "INDEX",
            "symbol": "NIFTY",
            "interval": "1minute",
        }
    ]
