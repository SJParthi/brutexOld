"""Unit tests for :mod:`brutex.data.volume_keepalive` (DEC-VOLUME-KEEPALIVE-001).

The dedicated 100% line+branch suite for the lake-volume keepalive daemon:
the three positive-finite-seconds env resolvers + the kill-switch resolver,
the ``VolumeKeepalive`` lifecycle (start idempotence, bounded stop + probe
unlink), the deadline-bounded probe (success / hanging probe / raising
probe), the wedge edge-latch (ONE ``wedged`` WARNING per episode + the
``recovered`` edge), and the fake-monotonic ``wedged_seconds`` arithmetic.
"""

# Whitebox tests of the module-private probe/latch helpers (sanctioned).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from brutex.data.volume_keepalive import (
    DEFAULT_KEEPALIVE_INTERVAL_S,
    DEFAULT_PROBE_DEADLINE_S,
    DEFAULT_WEDGE_HALT_S,
    KEEPALIVE_ENABLED_ENV,
    KEEPALIVE_INTERVAL_ENV,
    PROBE_DEADLINE_ENV,
    PROBE_FILENAME,
    WEDGE_HALT_ENV,
    VolumeKeepalive,
    resolve_keepalive_interval_seconds,
    resolve_probe_deadline_seconds,
    resolve_volume_keepalive_enabled,
    resolve_wedge_halt_seconds,
)
from structlog.testing import capture_logs

#: Wall-clock budget for every bounded wait in this file (generous for CI).
_BUDGET_S = 20.0


def _wait_until(predicate: Callable[[], bool], *, budget: float = _BUDGET_S) -> bool:
    """Poll zero-arg *predicate* until true or the budget expires."""
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ─────────────────────────────── env resolvers ────────────────────────────────


class TestResolveSeconds:
    """The three positive-finite resolvers share ONE fail-safe validation shape."""

    @pytest.mark.parametrize(
        ("resolver", "default"),
        [
            (resolve_keepalive_interval_seconds, DEFAULT_KEEPALIVE_INTERVAL_S),
            (resolve_probe_deadline_seconds, DEFAULT_PROBE_DEADLINE_S),
            (resolve_wedge_halt_seconds, DEFAULT_WEDGE_HALT_S),
        ],
    )
    @pytest.mark.parametrize("raw", ["", "   ", "notafloat", "0", "-5", "nan", "inf", "-inf"])
    def test_unusable_value_falls_back_to_default(
        self, resolver: Callable[[str | None], float], default: float, raw: str
    ) -> None:
        """Blank / non-numeric / non-positive / non-finite → the audited default."""
        assert resolver(raw) == default

    @pytest.mark.parametrize(
        ("resolver", "default"),
        [
            (resolve_keepalive_interval_seconds, DEFAULT_KEEPALIVE_INTERVAL_S),
            (resolve_probe_deadline_seconds, DEFAULT_PROBE_DEADLINE_S),
            (resolve_wedge_halt_seconds, DEFAULT_WEDGE_HALT_S),
        ],
    )
    def test_unset_reads_default(
        self,
        resolver: Callable[[str | None], float],
        default: float,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(KEEPALIVE_INTERVAL_ENV, raising=False)
        monkeypatch.delenv(PROBE_DEADLINE_ENV, raising=False)
        monkeypatch.delenv(WEDGE_HALT_ENV, raising=False)
        assert resolver(None) == default

    def test_valid_value_is_parsed(self) -> None:
        assert resolve_keepalive_interval_seconds("12.5") == 12.5
        assert resolve_probe_deadline_seconds("3") == 3.0
        assert resolve_wedge_halt_seconds("600") == 600.0

    def test_reads_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(KEEPALIVE_INTERVAL_ENV, "7")
        monkeypatch.setenv(PROBE_DEADLINE_ENV, "2")
        monkeypatch.setenv(WEDGE_HALT_ENV, "120")
        assert resolve_keepalive_interval_seconds() == 7.0
        assert resolve_probe_deadline_seconds() == 2.0
        assert resolve_wedge_halt_seconds() == 120.0


class TestResolveEnabled:
    """DEFAULT ON kill-switch — only an explicit falsy token disables the daemon."""

    @pytest.mark.parametrize("flag", [None, "", "  ", "1", "true", "on", "garbage"])
    def test_unset_or_non_falsy_is_enabled(
        self, flag: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(KEEPALIVE_ENABLED_ENV, raising=False)
        assert resolve_volume_keepalive_enabled(flag) is True

    @pytest.mark.parametrize("flag", ["off", "OFF", " off ", "0", "false", "FALSE", "no", "No"])
    def test_falsy_token_disables(self, flag: str) -> None:
        assert resolve_volume_keepalive_enabled(flag) is False

    def test_reads_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(KEEPALIVE_ENABLED_ENV, "off")
        assert resolve_volume_keepalive_enabled() is False
        monkeypatch.delenv(KEEPALIVE_ENABLED_ENV, raising=False)
        assert resolve_volume_keepalive_enabled() is True


# ───────────────────────────── construction guards ─────────────────────────────


class TestConstruction:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("interval_s", 0.0),
            ("interval_s", -1.0),
            ("interval_s", float("nan")),
            ("probe_deadline_s", 0.0),
            ("probe_deadline_s", float("inf")),
            ("halt_after_s", -900.0),
        ],
    )
    def test_rejects_non_positive_or_non_finite_bounds(
        self, tmp_path: Path, field: str, value: float
    ) -> None:
        bounds: dict[str, float] = {field: value}
        with pytest.raises(ValueError, match="must be positive and finite"):
            VolumeKeepalive(
                tmp_path / PROBE_FILENAME,
                interval_s=bounds.get("interval_s", 1.0),
                probe_deadline_s=bounds.get("probe_deadline_s", 1.0),
                halt_after_s=bounds.get("halt_after_s", 1.0),
            )

    def test_defaults_come_from_resolvers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(KEEPALIVE_INTERVAL_ENV, raising=False)
        monkeypatch.delenv(PROBE_DEADLINE_ENV, raising=False)
        monkeypatch.delenv(WEDGE_HALT_ENV, raising=False)
        keepalive = VolumeKeepalive(tmp_path / PROBE_FILENAME)
        assert keepalive.halt_after_s == DEFAULT_WEDGE_HALT_S
        assert keepalive.probe_path == tmp_path / PROBE_FILENAME
        assert not keepalive.wedged.is_set()
        assert keepalive.wedged_seconds() == 0.0

    def test_under_lake_root_creates_dir_and_probe_path(self, tmp_path: Path) -> None:
        lake_root = tmp_path / "lake" / "nested"
        keepalive = VolumeKeepalive.under_lake_root(lake_root)
        assert lake_root.is_dir()  # created so a first pull never false-wedges
        assert keepalive.probe_path == lake_root / PROBE_FILENAME


# ───────────────────────────── probe + wedge latch ──────────────────────────────


class TestProbeAndLatch:
    def test_probe_success_marks_healthy_and_writes_byte(self, tmp_path: Path) -> None:
        """The real default probe writes 1 byte + fsync; a clean probe stays healthy."""
        keepalive = VolumeKeepalive(tmp_path / PROBE_FILENAME, probe_deadline_s=5.0)
        keepalive._probe_once()
        assert not keepalive.wedged.is_set()
        assert keepalive.wedged_seconds() == 0.0
        assert (tmp_path / PROBE_FILENAME).read_bytes() == b"k"
        # Owner-only mode pin (CodeQL: never group/world-readable) — 0o600 create.
        mode = stat.S_IMODE((tmp_path / PROBE_FILENAME).stat().st_mode)
        assert mode & 0o077 == 0, f"probe file leaks group/world bits: {oct(mode)}"

    def test_raising_probe_marks_wedged_once_per_episode(self, tmp_path: Path) -> None:
        """An OSError probe wedges; the WARNING is edge-latched to ONE per episode."""

        def _bad_probe() -> None:
            raise OSError("mount vanished")

        keepalive = VolumeKeepalive(
            tmp_path / PROBE_FILENAME, probe_deadline_s=5.0, probe=_bad_probe
        )
        with capture_logs() as logs:
            keepalive._probe_once()
            keepalive._probe_once()  # second failure of the SAME episode — no re-log
        assert keepalive.wedged.is_set()
        wedged_events = [e for e in logs if e["event"] == "volume_keepalive.wedged"]
        assert len(wedged_events) == 1
        assert wedged_events[0]["error_type"] == "OSError"

    def test_hanging_probe_hits_deadline_and_marks_wedged(self, tmp_path: Path) -> None:
        """A probe that HANGS (never raises) is bounded by run_with_deadline → wedged."""
        release = threading.Event()

        def _hung_probe() -> None:
            release.wait(_BUDGET_S)

        keepalive = VolumeKeepalive(
            tmp_path / PROBE_FILENAME, probe_deadline_s=0.05, probe=_hung_probe
        )
        try:
            with capture_logs() as logs:
                keepalive._probe_once()
            assert keepalive.wedged.is_set()
            assert any(
                e["event"] == "volume_keepalive.wedged" and e["error_type"] == "DeadlineExceeded"
                for e in logs
            )
        finally:
            release.set()  # free the abandoned daemon probe worker

    def test_recovery_clears_wedge_and_logs_edge(self, tmp_path: Path) -> None:
        """Fail → succeed: the recovered edge fires once and the wedge fully resets."""
        fail = [True]

        def _flaky_probe() -> None:
            if fail[0]:
                raise OSError("spun down")

        keepalive = VolumeKeepalive(
            tmp_path / PROBE_FILENAME, probe_deadline_s=5.0, probe=_flaky_probe
        )
        keepalive._probe_once()
        assert keepalive.wedged.is_set()
        fail[0] = False
        with capture_logs() as logs:
            keepalive._probe_once()
            keepalive._probe_once()  # second healthy probe — the recovery edge fires ONCE
        assert not keepalive.wedged.is_set()
        assert keepalive.wedged_seconds() == 0.0
        recovered = [e for e in logs if e["event"] == "volume_keepalive.recovered"]
        assert len(recovered) == 1

    def test_wedged_seconds_grows_on_injected_monotonic(self, tmp_path: Path) -> None:
        clock = [100.0]
        keepalive = VolumeKeepalive(
            tmp_path / PROBE_FILENAME,
            probe_deadline_s=5.0,
            monotonic=lambda: clock[0],
            probe=lambda: None,
        )
        keepalive._mark_wedged(OSError("x"))
        clock[0] = 160.0
        assert keepalive.wedged_seconds() == 60.0
        # A fake-clock rewind clamps at 0.0 (never a negative duration).
        clock[0] = 50.0
        assert keepalive.wedged_seconds() == 0.0
        keepalive._mark_healthy()
        assert keepalive.wedged_seconds() == 0.0

    def test_repeat_wedge_keeps_first_episode_start(self, tmp_path: Path) -> None:
        """A second failed probe does NOT restart the episode clock (halt bound honest)."""
        clock = [0.0]
        keepalive = VolumeKeepalive(
            tmp_path / PROBE_FILENAME,
            probe_deadline_s=5.0,
            monotonic=lambda: clock[0],
            probe=lambda: None,
        )
        keepalive._mark_wedged(OSError("first"))
        clock[0] = 500.0
        keepalive._mark_wedged(OSError("still wedged"))
        clock[0] = 900.0
        assert keepalive.wedged_seconds() == 900.0


# ─────────────────────────────── lifecycle ────────────────────────────────


class TestLifecycle:
    def test_start_probe_stop_roundtrip(self, tmp_path: Path) -> None:
        """The daemon probes on its interval, stays healthy, and stop unlinks the probe."""
        keepalive = VolumeKeepalive(
            tmp_path / PROBE_FILENAME, interval_s=0.01, probe_deadline_s=5.0
        )
        keepalive.start()
        thread = keepalive._thread
        assert thread is not None
        assert thread.daemon
        assert thread.name == "volume-keepalive"
        assert _wait_until((tmp_path / PROBE_FILENAME).exists)
        assert not keepalive.wedged.is_set()
        keepalive.start()  # idempotent — the SAME thread keeps running
        assert keepalive._thread is thread
        keepalive.stop()
        assert not thread.is_alive()
        assert not (tmp_path / PROBE_FILENAME).exists()
        keepalive.stop()  # idempotent

    def test_daemon_wedges_then_recovers_end_to_end(self, tmp_path: Path) -> None:
        """The running daemon flips wedged on a failing probe and clears on recovery."""
        fail = [True]

        def _flaky_probe() -> None:
            if fail[0]:
                raise OSError("spun down")

        keepalive = VolumeKeepalive(
            tmp_path / PROBE_FILENAME,
            interval_s=0.01,
            probe_deadline_s=5.0,
            probe=_flaky_probe,
        )
        keepalive.start()
        try:
            assert _wait_until(keepalive.wedged.is_set)
            fail[0] = False
            assert _wait_until(lambda: not keepalive.wedged.is_set())
        finally:
            keepalive.stop()

    def test_stop_before_start_is_safe(self, tmp_path: Path) -> None:
        keepalive = VolumeKeepalive(tmp_path / PROBE_FILENAME)
        keepalive.stop()  # no thread to join, no probe file to unlink — no raise

    def test_stop_swallows_unlink_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unlink failure is logged + swallowed — stop can never raise into a drain."""
        keepalive = VolumeKeepalive(tmp_path / PROBE_FILENAME)

        def _boom(self: Path, *, missing_ok: bool = False) -> None:
            raise OSError("unlink denied")

        monkeypatch.setattr(Path, "unlink", _boom)
        with capture_logs() as logs:
            keepalive.stop()
        assert any(e["event"] == "volume_keepalive.probe_unlink_failed" for e in logs)
