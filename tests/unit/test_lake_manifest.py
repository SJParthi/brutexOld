"""Unit tests for ``brutex.data.lake_manifest`` (DEC-LAKE-MANIFEST-O1-001).

The durable write-time dataset manifest: token/paths helpers, the
load → validate → dedup read path (every fail-closed branch), the
walk-rebuild (union-with-existing keep-first), the O(1) writer-side
``record_dataset`` upsert, and the heal-side ``invalidate_manifest`` —
including every best-effort error path (a manifest failure must log +
invalidate, NEVER raise into the pull).

Whitebox: exercises private helpers (``_parse_lines`` / ``_unlink_manifest``)
per the DEC-P0-017 convention, so ``reportPrivateUsage`` is silenced.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from brutex.data import lake_manifest as lm
from brutex.data.errors import PartitionLockTimeout
from brutex.data.window_ledger_store import ledger_path
from structlog.testing import capture_logs


def _seed_ledger(lake: Path, content: bytes = b"ledger-v1") -> None:
    """Create the window-ledger file the freshness token stats."""
    target = ledger_path(lake)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def _move_ledger(lake: Path) -> None:
    """Move the ledger token deterministically (size + mtime both change)."""
    target = ledger_path(lake)
    target.write_bytes(target.read_bytes() + b"+")
    st = os.stat(target)
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def _row(symbol: str, interval: str = "1minute", segment: str = "INDEX") -> dict[str, str]:
    return {"exchange": "NSE", "segment": segment, "symbol": symbol, "interval": interval}


class TestPathsAndToken:
    def test_manifest_paths_layout(self, tmp_path: Path) -> None:
        """The manifest lives under ``.manifest/`` — outside bars/ + options/."""
        assert lm.manifest_dir(tmp_path) == tmp_path / ".manifest"
        assert lm.manifest_data_path(tmp_path) == tmp_path / ".manifest" / "datasets.jsonl"
        assert lm.manifest_meta_path(tmp_path) == tmp_path / ".manifest" / "datasets.meta.json"

    def test_token_absent_ledger(self, tmp_path: Path) -> None:
        assert lm.ledger_stat_token(tmp_path) == lm.ABSENT_LEDGER_TOKEN

    def test_token_present_ledger_moves_on_write(self, tmp_path: Path) -> None:
        _seed_ledger(tmp_path)
        first = lm.ledger_stat_token(tmp_path)
        assert first is not None and first != lm.ABSENT_LEDGER_TOKEN
        _move_ledger(tmp_path)
        assert lm.ledger_stat_token(tmp_path) != first

    def test_token_oserror_is_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_path: object) -> object:
            raise PermissionError("stat denied")

        monkeypatch.setattr(lm.os, "stat", _boom)
        with capture_logs() as logs:
            assert lm.ledger_stat_token(tmp_path) is None
        assert any(e["event"] == "lake_manifest.ledger_stat_failed" for e in logs)


class TestRebuildAndLoad:
    def test_roundtrip_sorted_and_deduped(self, tmp_path: Path) -> None:
        """rebuild → load returns the rows sorted by the 4 keys, duplicates collapsed."""
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        rows = [_row("NIFTY"), _row("BANKNIFTY"), _row("NIFTY")]  # dup NIFTY
        assert lm.rebuild_manifest(tmp_path, rows, ledger_token=token) is True
        loaded = lm.load_manifest_datasets(tmp_path)
        assert loaded == [_row("BANKNIFTY"), _row("NIFTY")]

    def test_empty_manifest_is_valid(self, tmp_path: Path) -> None:
        """An empty lake's manifest validates and serves [] (no walk needed)."""
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        assert lm.rebuild_manifest(tmp_path, [], ledger_token=token) is True
        assert lm.load_manifest_datasets(tmp_path) == []

    def test_absent_ledger_token_roundtrips(self, tmp_path: Path) -> None:
        """A ledger-less lake keys at the stable ``absent`` sentinel (still served)."""
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        assert token == lm.ABSENT_LEDGER_TOKEN
        assert lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token) is True
        assert lm.load_manifest_datasets(tmp_path) == [_row("NIFTY")]

    def test_missing_manifest_loads_none(self, tmp_path: Path) -> None:
        _seed_ledger(tmp_path)
        assert lm.load_manifest_datasets(tmp_path) is None

    def test_stale_token_loads_none(self, tmp_path: Path) -> None:
        """A ledger move after the stamp (a pull landed) reads as stale — walk fallback."""
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)
        _move_ledger(tmp_path)
        with capture_logs() as logs:
            assert lm.load_manifest_datasets(tmp_path) is None
        assert any(e["event"] == "lake_manifest.stale" for e in logs)

    def test_pre_walk_token_stales_writes_during_walk(self, tmp_path: Path) -> None:
        """Rebuild stamped with the PRE-walk token: a mid-walk write stales it."""
        _seed_ledger(tmp_path)
        pre_walk = lm.ledger_stat_token(tmp_path)
        assert pre_walk is not None
        _move_ledger(tmp_path)  # a vendor write lands DURING the walk
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=pre_walk)
        assert lm.load_manifest_datasets(tmp_path) is None  # never claims freshness

    def test_row_count_mismatch_loads_none(self, tmp_path: Path) -> None:
        """A torn append / truncation (lines != meta.rows) fails closed."""
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)
        with lm.manifest_data_path(tmp_path).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_row("GHOST")) + "\n")  # bypasses the meta bump
        with capture_logs() as logs:
            assert lm.load_manifest_datasets(tmp_path) is None
        assert any(e["event"] == "lake_manifest.row_count_mismatch" for e in logs)

    @pytest.mark.parametrize(
        "bad_line",
        [
            "not-json{",
            '"a-json-string-not-an-object"',
            json.dumps({"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY"}),  # no interval
            json.dumps({"exchange": "NSE", "segment": "INDEX", "symbol": 7, "interval": "1day"}),
            json.dumps({"exchange": "NSE", "segment": "INDEX", "symbol": "", "interval": "1day"}),
        ],
    )
    def test_corrupt_line_loads_none(self, tmp_path: Path, bad_line: str) -> None:
        """ANY malformed line invalidates the whole manifest — never a partial read."""
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)
        good = lm.manifest_data_path(tmp_path).read_text(encoding="utf-8")
        lm.manifest_data_path(tmp_path).write_text(
            good.replace(good.splitlines()[0], bad_line), encoding="utf-8"
        )
        assert lm.load_manifest_datasets(tmp_path) is None

    @pytest.mark.parametrize(
        "meta_text",
        [
            "not-json{",
            json.dumps(["a", "list"]),
            json.dumps({"version": 999, "ledger_token": "t", "rows": 1}),
            json.dumps({"version": 1, "ledger_token": 7, "rows": 1}),
            json.dumps({"version": 1, "ledger_token": "t", "rows": "1"}),
            json.dumps({"version": 1, "ledger_token": "t", "rows": -1}),
        ],
    )
    def test_bad_meta_loads_none(self, tmp_path: Path, meta_text: str) -> None:
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)
        lm.manifest_meta_path(tmp_path).write_text(meta_text, encoding="utf-8")
        assert lm.load_manifest_datasets(tmp_path) is None

    def test_meta_read_oserror_loads_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)
        real_read_text = Path.read_text

        def _boom(self: Path, *args: Any, **kwargs: Any) -> str:
            if self.name == lm.MANIFEST_META_FILENAME:
                raise PermissionError("meta denied")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _boom)
        with capture_logs() as logs:
            assert lm.load_manifest_datasets(tmp_path) is None
        assert any(e["event"] == "lake_manifest.meta_read_failed" for e in logs)

    def test_data_missing_with_meta_loads_none(self, tmp_path: Path) -> None:
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)
        lm.manifest_data_path(tmp_path).unlink()
        with capture_logs() as logs:
            assert lm.load_manifest_datasets(tmp_path) is None
        assert any(e["event"] == "lake_manifest.data_missing_with_meta" for e in logs)

    def test_ledger_token_error_loads_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable ledger (no freshness signal) refuses to serve the manifest."""
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)
        monkeypatch.setattr(lm, "ledger_stat_token", lambda _lake: None)
        assert lm.load_manifest_datasets(tmp_path) is None

    def test_lock_failure_loads_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)

        def _timeout(*_args: Any, **_kwargs: Any) -> Any:
            raise PartitionLockTimeout(0.1)

        monkeypatch.setattr(lm, "partition_write_lock", _timeout)
        with capture_logs() as logs:
            assert lm.load_manifest_datasets(tmp_path) is None
        assert any(e["event"] == "lake_manifest.load_failed" for e in logs)

    def test_rebuild_unions_existing_rows_keep_first(self, tmp_path: Path) -> None:
        """A concurrent writer's row recorded after the walk survives the rebuild."""
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY"), _row("EXTRA")], ledger_token=token)
        # A NEW walk that did not see EXTRA (created after its dir was visited)
        # must not drop the writer-recorded on-disk row.
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)
        assert lm.load_manifest_datasets(tmp_path) == [_row("EXTRA"), _row("NIFTY")]

    def test_rebuild_skips_corrupt_existing(self, tmp_path: Path) -> None:
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.manifest_dir(tmp_path).mkdir(parents=True)
        lm.manifest_data_path(tmp_path).write_text("garbage{\n", encoding="utf-8")
        assert lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token) is True
        assert lm.load_manifest_datasets(tmp_path) == [_row("NIFTY")]

    def test_rebuild_write_failure_invalidates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed rewrite leaves NO servable manifest (never half-written)."""
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("disk error")

        monkeypatch.setattr(lm, "atomic_write_simple", _boom)
        with capture_logs() as logs:
            assert lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token) is False
        assert any(e["event"] == "lake_manifest.rebuild_failed" for e in logs)
        monkeypatch.undo()
        assert lm.load_manifest_datasets(tmp_path) is None

    def test_rebuild_malformed_dataset_mapping_fails_safely(self, tmp_path: Path) -> None:
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        bad: list[dict[str, str]] = [{"exchange": "NSE"}]  # missing the other keys
        assert lm.rebuild_manifest(tmp_path, bad, ledger_token=token) is False
        assert lm.load_manifest_datasets(tmp_path) is None


class TestRecordDataset:
    def test_no_base_is_a_noop(self, tmp_path: Path) -> None:
        """Appends only layer onto a walk-built base — a missing manifest stays missing."""
        _seed_ledger(tmp_path)
        assert (
            lm.record_dataset(
                tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute"
            )
            is False
        )
        assert not lm.manifest_meta_path(tmp_path).exists()
        assert not lm.manifest_data_path(tmp_path).exists()

    def test_record_appends_and_restamps_token(self, tmp_path: Path) -> None:
        """The upsert adds the row AND re-stamps the CURRENT ledger token (prebuild converge)."""
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)
        _move_ledger(tmp_path)  # a pull moved the ledger since the stamp
        assert lm.load_manifest_datasets(tmp_path) is None  # stale…
        assert (
            lm.record_dataset(
                tmp_path, exchange="NSE", segment="INDEX", symbol="NEWSYM", interval="1minute"
            )
            is True
        )
        # …but the record re-stamped the CURRENT token, so the manifest —
        # complete again by writer-synchronous construction — serves hot.
        assert lm.load_manifest_datasets(tmp_path) == [_row("NEWSYM"), _row("NIFTY")]

    def test_record_token_probe_error_keeps_prior_stamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)
        monkeypatch.setattr(lm, "ledger_stat_token", lambda _lake: None)
        assert (
            lm.record_dataset(
                tmp_path, exchange="NSE", segment="INDEX", symbol="NEWSYM", interval="1minute"
            )
            is True
        )
        meta = json.loads(lm.manifest_meta_path(tmp_path).read_text(encoding="utf-8"))
        assert meta["ledger_token"] == token  # honest: prior stamp kept, not fabricated
        assert meta["rows"] == 2

    def test_record_data_missing_with_meta_invalidates(self, tmp_path: Path) -> None:
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)
        lm.manifest_data_path(tmp_path).unlink()
        assert (
            lm.record_dataset(
                tmp_path, exchange="NSE", segment="INDEX", symbol="NEWSYM", interval="1minute"
            )
            is False
        )
        assert not lm.manifest_meta_path(tmp_path).exists()

    def test_record_oserror_invalidates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("append failed")

        monkeypatch.setattr(lm.os, "fsync", _boom)
        with capture_logs() as logs:
            assert (
                lm.record_dataset(
                    tmp_path, exchange="NSE", segment="INDEX", symbol="NEWSYM", interval="1minute"
                )
                is False
            )
        assert any(e["event"] == "lake_manifest.record_failed" for e in logs)
        assert not lm.manifest_meta_path(tmp_path).exists()  # invalidated, fail-closed

    def test_record_lock_timeout_is_best_effort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_ledger(tmp_path)

        def _timeout(*_args: Any, **_kwargs: Any) -> Any:
            raise PartitionLockTimeout(0.1)

        monkeypatch.setattr(lm, "partition_write_lock", _timeout)
        assert (
            lm.record_dataset(
                tmp_path, exchange="NSE", segment="INDEX", symbol="NIFTY", interval="1minute"
            )
            is False
        )


class TestInvalidate:
    def test_invalidate_removes_both_and_is_idempotent(self, tmp_path: Path) -> None:
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)
        lm.invalidate_manifest(tmp_path)
        assert not lm.manifest_meta_path(tmp_path).exists()
        assert not lm.manifest_data_path(tmp_path).exists()
        lm.invalidate_manifest(tmp_path)  # idempotent — nothing to remove, no raise

    def test_invalidate_lock_failure_still_unlinks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)

        def _timeout(*_args: Any, **_kwargs: Any) -> Any:
            raise PartitionLockTimeout(0.1)

        monkeypatch.setattr(lm, "partition_write_lock", _timeout)
        with capture_logs() as logs:
            lm.invalidate_manifest(tmp_path)
        assert any(e["event"] == "lake_manifest.invalidate_lock_failed" for e in logs)
        assert not lm.manifest_meta_path(tmp_path).exists()
        assert not lm.manifest_data_path(tmp_path).exists()

    def test_unlink_oserror_is_logged_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_ledger(tmp_path)
        token = lm.ledger_stat_token(tmp_path)
        assert token is not None
        lm.rebuild_manifest(tmp_path, [_row("NIFTY")], ledger_token=token)
        real_unlink = Path.unlink

        def _boom(self: Path, *args: Any, **kwargs: Any) -> None:
            if self.name == lm.MANIFEST_META_FILENAME:
                raise PermissionError("unlink denied")
            real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _boom)
        with capture_logs() as logs:
            lm.invalidate_manifest(tmp_path)
        assert any(e["event"] == "lake_manifest.unlink_failed" for e in logs)
        # The DATA file was still removed (meta-first order failed here, but the
        # survivor pair is meta-without-data → load fails closed).
        assert not lm.manifest_data_path(tmp_path).exists()
        assert lm.load_manifest_datasets(tmp_path) is None

    def test_parse_lines_empty_text(self) -> None:
        assert lm._parse_lines("") == []


def test_ledger_file_path_pins_window_ledger_store(tmp_path: Path) -> None:
    """The leaf twin MUST equal ``window_ledger_store.ledger_path`` — the pin.

    ``lake_manifest`` deliberately does NOT import ``window_ledger_store``
    (the ledger/calendar import graph closed the CodeQL ``py/cyclic-import``
    loop on PR #1177 — see ``_LEDGER_DIRNAME``), so THIS test is the guard
    that the duplicated path constants can never drift from the ONE canonical
    definition (the ``lake_revision._SWEEPS_DIRNAME`` precedent).
    """
    assert lm.ledger_file_path(tmp_path) == ledger_path(tmp_path)
