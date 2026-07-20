# pyright: reportPrivateUsage=none
"""The schedule.py ``brutex_ops`` seams (DEC-NIGHTLY-RUST-CORE-001).

Flag-OFF bomb-spy byte-identity + flag-ON routed parity against the
retained ``_*_python`` oracles (plist render golden included) over the
faithful wheel-free fake crate + the refuse/degrade arms (non-ASCII
``--at`` refusal, poisoned shapes). Stem-matched so the per-module
coverage gate folds this suite into ``brutex/nightly/schedule.py``'s
field.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from brutex.nightly import nightly_rust
from brutex.nightly.schedule import (
    DEFAULT_LABEL,
    _parse_at_python,
    _pmset_wake_command_python,
    _render_launchd_plist_python,
    _seconds_since_scheduled_fire_python,
    parse_at,
    pmset_wake_command,
    render_launchd_plist,
    seconds_since_scheduled_fire,
)
from structlog.testing import capture_logs

from tests.fakes.fake_brutex_ops import build_fake_crate

if TYPE_CHECKING:
    from collections.abc import Iterator


class _BombModule(types.ModuleType):
    """Records + refuses ANY attribute access (the CODEQL bomb shape).

    ``__getattr__`` honours the special-method contract (AttributeError,
    never AssertionError — the ``test_fno_symbol_rust.py`` precedent); the
    TEETH are the ``touched`` ledger asserted EMPTY after the OFF-arm walk.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.touched.append(name)
        raise AttributeError(f"flag-OFF path touched the crate: {name}")


_PLIST_ENV = {
    "BRUTEX_LAKE_ROOT": "/Volumes/lake",
    "BRUTEX_NIGHTLY_BACKUP_ROOT": "/Volumes/backups",
}


def _plist_pair(
    *,
    label: str = DEFAULT_LABEL,
    env_vars: dict[str, str] | None = None,
    log_stem: str = "nightly-launchd",
) -> tuple[str, str]:
    """(routed, oracle) plist renders over IDENTICAL typed arguments."""
    env = _PLIST_ENV if env_vars is None else env_vars
    args = ["/opt/bin/brutex", "nightly", "run"]
    log_dir = Path("/Users/op/.brutex/logs")
    routed = render_launchd_plist(
        label=label,
        program_arguments=args,
        hour=1,
        minute=30,
        env_vars=env,
        log_dir=log_dir,
        log_stem=log_stem,
    )
    oracle = _render_launchd_plist_python(
        label=label,
        program_arguments=args,
        hour=1,
        minute=30,
        env_vars=env,
        log_dir=log_dir,
        log_stem=log_stem,
    )
    return routed, oracle


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(nightly_rust.NIGHTLY_RUST_ENV, raising=False)
    nightly_rust.reset_nightly_rust_for_tests()
    yield
    nightly_rust.reset_nightly_rust_for_tests()


def _flag_on(monkeypatch: pytest.MonkeyPatch, crate: types.ModuleType) -> None:
    monkeypatch.setenv(nightly_rust.NIGHTLY_RUST_ENV, "1")
    monkeypatch.setitem(sys.modules, "brutex_ops", crate)


class TestFlagOffBombSpy:
    def test_off_arm_matches_oracles_and_never_touches_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bomb = _BombModule("brutex_ops")
        monkeypatch.setitem(sys.modules, "brutex_ops", bomb)
        assert parse_at("01:30") == _parse_at_python("01:30") == (1, 30)
        assert pmset_wake_command(1, 30) == _pmset_wake_command_python(1, 30, 5)
        now = datetime(2026, 7, 19, 2, 0, 0)
        assert seconds_since_scheduled_fire(
            hour=1, minute=30, now_local=now
        ) == _seconds_since_scheduled_fire_python(hour=1, minute=30, now_local=now)
        routed, oracle = _plist_pair()
        assert routed == oracle
        assert bomb.touched == []

    def test_off_arm_junk_parse_raises_the_exact_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bomb = _BombModule("brutex_ops")
        monkeypatch.setitem(sys.modules, "brutex_ops", bomb)
        with pytest.raises(ValueError, match=r"--at must be HH:MM \(24h\), got 'noon'"):
            parse_at("noon")
        assert bomb.touched == []


class TestFlagOnParity:
    @pytest.mark.parametrize("text", ["01:30", "23:59", " 9:05 ", "0:00", "20:00"])
    def test_parse_at_parity(self, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
        _flag_on(monkeypatch, build_fake_crate())
        with capture_logs():
            assert parse_at(text) == _parse_at_python(text)

    @pytest.mark.parametrize("junk", ["24:00", "1:5", "01-30", "noon", "", "01:60"])
    def test_parse_at_junk_raises_the_exact_message_on_the_routed_arm(
        self, monkeypatch: pytest.MonkeyPatch, junk: str
    ) -> None:
        _flag_on(monkeypatch, build_fake_crate())
        with capture_logs(), pytest.raises(ValueError, match="--at must be HH:MM"):
            parse_at(junk)

    def test_parse_at_non_ascii_degrades_to_the_oracle_decision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CPython's un-flagged \\d matches Unicode digits — the crate refuses
        such candidates and the ORACLE (the genuine Python law) decides."""
        _flag_on(monkeypatch, build_fake_crate())
        text = "0٣:05"  # ARABIC-INDIC DIGIT THREE in the hour token
        with capture_logs() as logs:
            assert parse_at(text) == _parse_at_python(text) == (3, 5)
        warns = [entry for entry in logs if entry["event"] == "nightly_rust.fallback"]
        assert warns and warns[0]["reason"] == "parse_at_error"

    @pytest.mark.parametrize(("hour", "minute"), [(1, 30), (0, 2), (0, 5), (23, 59), (13, 0)])
    def test_pmset_parity(self, monkeypatch: pytest.MonkeyPatch, hour: int, minute: int) -> None:
        _flag_on(monkeypatch, build_fake_crate())
        with capture_logs():
            assert pmset_wake_command(hour, minute) == _pmset_wake_command_python(hour, minute, 5)
        with capture_logs():
            assert pmset_wake_command(hour, minute, lead_minutes=90) == _pmset_wake_command_python(
                hour, minute, 90
            )

    @pytest.mark.parametrize(
        "now",
        [
            datetime(2026, 7, 19, 2, 0, 0),
            datetime(2026, 7, 19, 1, 30, 0),
            datetime(2026, 7, 19, 1, 29, 59, 999_999),
            datetime(2026, 8, 1, 0, 0, 30, 250_000),
        ],
        ids=["past", "exact", "just-before", "month-edge"],
    )
    def test_seconds_since_fire_parity_is_bit_exact(
        self, monkeypatch: pytest.MonkeyPatch, now: datetime
    ) -> None:
        _flag_on(monkeypatch, build_fake_crate())
        with capture_logs():
            routed = seconds_since_scheduled_fire(hour=1, minute=30, now_local=now)
        oracle = _seconds_since_scheduled_fire_python(hour=1, minute=30, now_local=now)
        assert routed == oracle
        assert isinstance(routed, float)

    def test_seconds_since_fire_out_of_range_degrades_and_oracle_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """hour=24: the crate refuses; the oracle raises Python's own
        ``datetime.replace`` ValueError — the genuine law surface."""
        _flag_on(monkeypatch, build_fake_crate())
        now = datetime(2026, 7, 19, 2, 0, 0)
        with capture_logs(), pytest.raises(ValueError, match="hour must be in 0..23"):
            seconds_since_scheduled_fire(hour=24, minute=0, now_local=now)

    def test_plist_parity_env_and_empty_env_and_custom_stem(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(monkeypatch, build_fake_crate())
        with capture_logs():
            routed, oracle = _plist_pair()
            assert routed == oracle
            routed, oracle = _plist_pair(env_vars={}, label="com.brutex.<t>&co")
            assert routed == oracle
            rendered, oracle = _plist_pair(log_stem="crossverify-launchd")
            assert rendered == oracle
            assert "crossverify-launchd.out.log" in rendered


class TestFlagOnRefuseAndDegrade:
    def test_poisoned_plist_shape_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(
            monkeypatch,
            build_fake_crate(nightly_render_launchd_plist=lambda *args: b"plist"),
        )
        with capture_logs() as logs:
            routed, oracle = _plist_pair()
            assert routed == oracle
        warns = [entry for entry in logs if entry["event"] == "nightly_rust.fallback"]
        assert warns and warns[0]["reason"] == "render_launchd_plist_result_refused"

    def test_poisoned_seconds_shape_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(
            monkeypatch,
            build_fake_crate(nightly_seconds_since_fire=lambda *args: "1800.0"),
        )
        now = datetime(2026, 7, 19, 2, 0, 0)
        with capture_logs() as logs:
            assert seconds_since_scheduled_fire(
                hour=1, minute=30, now_local=now
            ) == _seconds_since_scheduled_fire_python(hour=1, minute=30, now_local=now)
        warns = [entry for entry in logs if entry["event"] == "nightly_rust.fallback"]
        assert warns and warns[0]["reason"] == "seconds_since_scheduled_fire_result_refused"

    def test_poisoned_parse_shape_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(
            monkeypatch,
            build_fake_crate(nightly_parse_at=lambda text: (True, 30)),
        )
        with capture_logs() as logs:
            assert parse_at("01:30") == (1, 30)
        warns = [entry for entry in logs if entry["event"] == "nightly_rust.fallback"]
        assert warns and warns[0]["reason"] == "parse_at_result_refused"

    def test_wrong_pmset_autoreverts_via_burn_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(
            monkeypatch,
            build_fake_crate(nightly_pmset_wake_command=lambda *args: "sudo wrong"),
        )
        with capture_logs() as logs:
            assert pmset_wake_command(1, 30) == _pmset_wake_command_python(1, 30, 5)
        assert nightly_rust.nightly_rust_disabled() is True
        assert any(entry["event"] == "nightly_rust.kernel_autoreverted" for entry in logs)
