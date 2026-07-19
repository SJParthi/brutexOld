"""Unit tests for :mod:`brutex.nightly.decay` (LANE W5, DEC-NIGHTLY-DECAY-R48-001).

100% line + branch of the R48 decay leg: env knobs, run discovery over the
contract-run-roots law, the engine-scope coordinate resolver, the
data-driven trailing-session trim, and the REAL leg over a REAL tmp lake
(raw-planted partitions + a real DuckDB catalog through
``load_lake_slice`` + the REAL Stage-1A + trade-sim re-sim) — including
the fail-before/pass-after decay proof (a planted decayed finalist FLAGS,
a healthy one does NOT), the decay-diff.json sidecar, the aggregated
Telegram, and every guarded degrade.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import structlog.testing

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
pytest.importorskip("duckdb", reason="Catalog needs duckdb")

from brutex.data.models import CandleBar
from brutex.nightly import decay
from brutex.nightly.decay import (
    DECAY_DIFF_FILENAME,
    DECAY_ENABLED_ENV,
    DECAY_TRAILING_SESSIONS,
    DECAY_TRAILING_SESSIONS_ENV,
    DiscoveredRun,
    discover_top100_runs,
    entry_join_key,
    instrument_coords,
    nightly_decay_enabled,
    resolve_trailing_sessions,
    run_decay_leg,
    run_decay_leg_guarded,
    trailing_session_bars,
)
from brutex.nightly.decay_math import DECAY_MIN_TRADES_ENV, DECAY_RUST_ENV

if TYPE_CHECKING:
    from collections.abc import Iterator

from tests.fakes.top100_fixture import (
    FIXTURE_NOW as NOW,
)
from tests.fakes.top100_fixture import (
    make_entry as _entry,
)
from tests.fakes.top100_fixture import (
    plant_index_lake as _plant_lake,
)
from tests.fakes.top100_fixture import (
    write_top100_fixture as write_artifact,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for var in (
        DECAY_ENABLED_ENV,
        DECAY_TRAILING_SESSIONS_ENV,
        DECAY_MIN_TRADES_ENV,
        DECAY_RUST_ENV,
    ):
        monkeypatch.delenv(var, raising=False)
    return


@pytest.fixture
def stamp_filename() -> str:
    from brutex.engine.top100_artifact import TOP100_STAMP_FILENAME

    return str(TOP100_STAMP_FILENAME)


class TestEnvKnobs:
    def test_enabled_default_on(self) -> None:
        assert nightly_decay_enabled() is True

    def test_enabled_blank_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DECAY_ENABLED_ENV, "  ")
        assert nightly_decay_enabled() is True

    def test_enabled_falsy_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DECAY_ENABLED_ENV, "off")
        assert nightly_decay_enabled() is False

    def test_enabled_junk_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DECAY_ENABLED_ENV, "maybe")
        with pytest.raises(ValueError, match=DECAY_ENABLED_ENV):
            nightly_decay_enabled()

    def test_sessions_default(self) -> None:
        assert resolve_trailing_sessions() == DECAY_TRAILING_SESSIONS == 63

    def test_sessions_blank_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DECAY_TRAILING_SESSIONS_ENV, "")
        assert resolve_trailing_sessions() == DECAY_TRAILING_SESSIONS

    def test_sessions_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DECAY_TRAILING_SESSIONS_ENV, "10")
        assert resolve_trailing_sessions() == 10

    def test_sessions_junk_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DECAY_TRAILING_SESSIONS_ENV, "many")
        with pytest.raises(ValueError, match="not an integer"):
            resolve_trailing_sessions()

    def test_sessions_below_two_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(DECAY_TRAILING_SESSIONS_ENV, "1")
        with pytest.raises(ValueError, match="must be >= 2"):
            resolve_trailing_sessions()


class TestDiscovery:
    def test_empty_results_root(self, tmp_path: Path) -> None:
        assert discover_top100_runs(tmp_path, limit=2) == ()

    def test_newest_first_and_limit(self, tmp_path: Path) -> None:
        write_artifact(tmp_path, "a" * 64, [_entry(1)], mtime=1000.0)
        write_artifact(tmp_path, "b" * 64, [_entry(1)], mtime=2000.0)
        write_artifact(tmp_path, "c" * 64, [_entry(1)], mtime=1500.0)
        runs = discover_top100_runs(tmp_path, limit=2)
        assert [r.run_id for r in runs] == ["b" * 64, "c" * 64]
        assert all(isinstance(r, DiscoveredRun) for r in runs)

    def test_dir_without_artifact_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "sweep_runs" / "empty-run").mkdir(parents=True)
        (tmp_path / "sweep_runs" / "stray-file").write_text("x", encoding="utf-8")
        assert discover_top100_runs(tmp_path, limit=5) == ()

    def test_stat_oserror_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The sort-key stat on top100.json raises → the run is skipped.

        The FIRST top100.json stat (``is_file``) succeeds so the loop
        reaches the inner ``suppress(OSError)``; the SECOND (the sort-key
        ``.stat()``) raises — the run is dropped, never a leg crash.
        """
        write_artifact(tmp_path, "a" * 64, [_entry(1)])
        real_stat = Path.stat
        calls = {"n": 0}

        def _flaky(self: Path, **kwargs: object) -> object:
            if self.name == "top100.json":
                calls["n"] += 1
                if calls["n"] > 1:
                    raise OSError("stat refused")
            return real_stat(self, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "stat", _flaky)
        assert discover_top100_runs(tmp_path, limit=5) == ()
        assert calls["n"] == 2

    def test_sidecar_write_does_not_rekey_discovery(self, tmp_path: Path) -> None:
        """Round-1 MINOR #2 regression: discovery keys on the IMMUTABLE
        top100.json file mtime, NOT the run-dir mtime — a later sidecar
        write into a PRIOR night's run dir (run-delta.json /
        decay-diff.json bump the DIR mtime) must never re-select it."""
        import os

        dir_a = write_artifact(tmp_path, "a" * 64, [_entry(1)])
        write_artifact(tmp_path, "b" * 64, [_entry(1)])
        runs = discover_top100_runs(tmp_path, limit=1)
        assert [r.run_id for r in runs] == ["b" * 64]
        # A sidecar lands in run "a" and bumps the DIR mtime far past "b".
        (dir_a / "run-delta.json").write_text("{}", encoding="utf-8")
        future = dir_a.stat().st_mtime + 10_000.0
        os.utime(dir_a, (future, future))
        runs = discover_top100_runs(tmp_path, limit=1)
        assert [r.run_id for r in runs] == ["b" * 64]


class TestCoordsAndKeys:
    def test_engine_triples(self) -> None:
        assert instrument_coords("NIFTY") == ("NSE", "INDEX")
        assert instrument_coords("BANKNIFTY") == ("NSE", "INDEX")
        assert instrument_coords("SENSEX") == ("BSE", "INDEX")

    def test_out_of_scope_is_none(self) -> None:
        assert instrument_coords("NSE-NIFTY-FUT") is None
        assert instrument_coords("RELIANCE") is None

    def test_join_key_carries_identity_minus_snapshot(self) -> None:
        entry = _entry(1)
        key = entry_join_key(entry)
        assert key == f"{'0' * 32}|bull|NIFTY|1minute|None|None"
        # The per-combo run_id (snapshot + commit keyed) is NOT in the key.
        assert str(entry["run_id"]) not in key

    def test_join_key_missing_arm(self) -> None:
        entry = _entry(1)
        entry["arm"] = "junk"
        assert entry_join_key(entry).endswith("|None|None")


class TestTrailingTrim:
    @staticmethod
    def _bar(ts: datetime) -> CandleBar:
        price = Decimal("100.00")
        return CandleBar(
            timestamp=ts,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=1,
            open_interest=None,
        )

    def test_fewer_sessions_returns_all(self) -> None:
        bars = [self._bar(datetime(2024, 6, 3, 4, m, tzinfo=UTC)) for m in range(3)]
        assert trailing_session_bars(bars, 5) == bars

    def test_trims_to_last_sessions(self) -> None:
        bars = [self._bar(datetime(2024, 6, day, 4, 0, tzinfo=UTC)) for day in (3, 4, 5, 6)]
        trimmed = trailing_session_bars(bars, 2)
        assert [b.timestamp.day for b in trimmed] == [5, 6]

    def test_empty(self) -> None:
        assert trailing_session_bars([], 5) == []


class TestRunDecayLeg:
    def test_no_artifact_skips(self, tmp_path: Path) -> None:
        block = run_decay_leg(lake_root=tmp_path / "lake", results_root=tmp_path, now_utc=NOW)
        assert block == {"status": "skipped", "reason": "no_top100_artifact"}

    def test_invalid_artifact_skips(self, tmp_path: Path, stamp_filename: str) -> None:
        run_dir = write_artifact(tmp_path, "a" * 64, [_entry(1)])
        (run_dir / stamp_filename).unlink()  # stamp gone → load_top100 None
        block = run_decay_leg(lake_root=tmp_path / "lake", results_root=tmp_path, now_utc=NOW)
        assert block["status"] == "skipped"
        assert block["reason"] == "top100_invalid"

    def test_decayed_flags_and_healthy_does_not(self, tmp_path: Path) -> None:
        """THE R48 proof: a decayed finalist FLAGS, a healthy one does not.

        Same lake, same bars: the BULL mask-0 finalist loses every trailing
        session (trailing Sharpe << the 0.70× discovery floor ⇒ DECAYED);
        the BEAR one wins them (trailing Sharpe above the floor ⇒ healthy).
        """
        lake = tmp_path / "lake"
        _plant_lake(lake)
        write_artifact(
            tmp_path,
            "a" * 64,
            [
                _entry(1, direction="bull", sharpe=2.0, max_dd_paisa=100_000),
                _entry(2, direction="bear", sharpe=0.5, max_dd_paisa=100_000),
            ],
        )

        class _Alerter:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def strategy_decayed(self, **kwargs: object) -> bool:
                self.calls.append(dict(kwargs))
                return True

        alerter = _Alerter()
        block = run_decay_leg(lake_root=lake, results_root=tmp_path, alerter=alerter, now_utc=NOW)
        assert block["status"] == "ok"
        assert block["checked"] == 2
        assert block["decayed"] == 1
        entries = block["entries"]
        assert isinstance(entries, list)
        by_rank = {e["rank"]: e for e in entries}
        assert by_rank[1]["verdict"] == "decayed"
        assert by_rank[1]["retire_flag"] is True
        assert by_rank[2]["verdict"] == "healthy"
        assert "retire_flag" not in by_rank[2]
        trailing = by_rank[1]["trailing"]
        assert isinstance(trailing, dict)
        assert trailing["n_trades"] >= 10
        assert isinstance(trailing["sharpe"], float) and trailing["sharpe"] < 0.0
        # ONE aggregated alert naming the run.
        assert len(alerter.calls) == 1
        assert alerter.calls[0]["decayed"] == 1
        # The durable sidecar landed beside top100.json.
        diff_path = Path(str(block["diff_path"]))
        assert diff_path.name == DECAY_DIFF_FILENAME
        diff = json.loads(diff_path.read_text(encoding="utf-8"))
        assert diff["schema"] == "brutex.nightly.decay_diff"
        assert diff["decayed"] == 1
        # R13: the artifact + stamp are byte-untouched (annotation only).
        raw = (diff_path.parent / "top100.json").read_bytes()
        stamp = json.loads((diff_path.parent / "top100.stamp.json").read_text(encoding="utf-8"))
        assert hashlib.sha256(raw).hexdigest() == stamp["content_hash"]

    def test_healthy_lake_no_alert(self, tmp_path: Path) -> None:
        """The pass-after control: zero decayed ⇒ zero Telegram pushes."""
        lake = tmp_path / "lake"
        _plant_lake(lake)
        write_artifact(tmp_path, "a" * 64, [_entry(1, direction="bear", sharpe=0.5)])

        class _Bomb:
            def strategy_decayed(self, **kwargs: object) -> bool:
                raise AssertionError("no alert on a healthy run")

        block = run_decay_leg(lake_root=lake, results_root=tmp_path, alerter=_Bomb(), now_utc=NOW)
        assert block["status"] == "ok"
        assert block["decayed"] == 0

    def test_skip_reasons_counted(self, tmp_path: Path) -> None:
        """Out-of-scope instrument / unknown tf / bad mask are per-entry skips."""
        lake = tmp_path / "lake"
        _plant_lake(lake)
        write_artifact(
            tmp_path,
            "a" * 64,
            [
                _entry(1, instrument="RELIANCE"),  # out of engine scope
                _entry(2, tf="banana"),  # unknown lake label
                _entry(3, mask_hex="zz"),  # per-entry error isolation
                _entry(4, direction="bear", sharpe=0.5),  # healthy control
            ],
        )
        block = run_decay_leg(lake_root=lake, results_root=tmp_path, now_utc=NOW)
        assert block["status"] == "ok"
        assert block["skipped_entries"] == 3
        assert block["checked"] == 1
        entries = block["entries"]
        assert isinstance(entries, list)
        reasons = {e["rank"]: e.get("reason") for e in entries}
        assert reasons[1] == "out_of_scope_or_unknown_tf"
        assert reasons[2] == "out_of_scope_or_unknown_tf"
        assert str(reasons[3]).startswith("entry_error:")

    def test_no_trailing_bars_skips(self, tmp_path: Path) -> None:
        """An in-scope instrument with an EMPTY lake window skips honestly."""
        lake = tmp_path / "lake"
        lake.mkdir()
        write_artifact(tmp_path, "a" * 64, [_entry(1)])
        block = run_decay_leg(lake_root=lake, results_root=tmp_path, now_utc=NOW)
        assert block["status"] == "ok"
        entries = block["entries"]
        assert isinstance(entries, list)
        assert entries[0]["reason"] == "no_trailing_bars"

    def test_abstain_no_discovery(self, tmp_path: Path) -> None:
        """A null discovery Sharpe abstains — never judged, never decayed."""
        lake = tmp_path / "lake"
        _plant_lake(lake)
        write_artifact(tmp_path, "a" * 64, [_entry(1, sharpe=None)])
        block = run_decay_leg(lake_root=lake, results_root=tmp_path, now_utc=NOW)
        entries = block["entries"]
        assert isinstance(entries, list)
        assert entries[0]["verdict"] == "abstain_no_discovery"
        assert block["abstained"] == 1

    def test_diff_write_failure_is_soft(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        _plant_lake(lake)
        write_artifact(tmp_path, "a" * 64, [_entry(1, direction="bear", sharpe=0.5)])

        def _refuse(path: Path, write: object) -> None:
            raise OSError("disk says no")

        monkeypatch.setattr(decay, "atomic_write_simple", _refuse)
        with structlog.testing.capture_logs() as logs:
            block = run_decay_leg(lake_root=lake, results_root=tmp_path, now_utc=NOW)
        assert block["status"] == "ok"
        assert block["diff_path"] is None
        assert any(e["event"] == "nightly.decay.diff_write_failed" for e in logs)

    def test_alerter_raise_is_suppressed(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _plant_lake(lake)
        write_artifact(tmp_path, "a" * 64, [_entry(1, direction="bull", sharpe=2.0)])

        class _Raises:
            def strategy_decayed(self, **kwargs: object) -> bool:
                raise RuntimeError("telegram down")

        block = run_decay_leg(lake_root=lake, results_root=tmp_path, alerter=_Raises(), now_utc=NOW)
        assert block["status"] == "ok"  # best-effort — never a leg failure


class TestGuardedWrapper:
    def test_disabled(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(DECAY_ENABLED_ENV, "0")
        block = run_decay_leg_guarded(lake_root=tmp_path, results_root=tmp_path)
        assert block == {"status": "disabled"}

    def test_flag_junk_is_env_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(DECAY_ENABLED_ENV, "junk")
        block = run_decay_leg_guarded(lake_root=tmp_path, results_root=tmp_path)
        assert block["status"] == "env_error"
        assert DECAY_ENABLED_ENV in str(block["error"])

    def test_knob_junk_is_env_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(DECAY_TRAILING_SESSIONS_ENV, "junk")
        write_artifact(tmp_path, "a" * 64, [_entry(1)])
        block = run_decay_leg_guarded(lake_root=tmp_path / "lake", results_root=tmp_path)
        assert block["status"] == "env_error"

    def test_unexpected_error_is_fail_open(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def _boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("leg exploded")

        monkeypatch.setattr(decay, "discover_top100_runs", _boom)
        block = run_decay_leg_guarded(lake_root=tmp_path, results_root=tmp_path)
        assert block["status"] == "error"
        assert block["error_type"] == "RuntimeError"

    def test_success_passthrough(self, tmp_path: Path) -> None:
        block = run_decay_leg_guarded(lake_root=tmp_path, results_root=tmp_path)
        assert block == {"status": "skipped", "reason": "no_top100_artifact"}
