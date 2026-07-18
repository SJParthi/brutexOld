"""The ``auto_pull_gate`` × ``pull_state_rust`` seam (Wave 2D, ``DEC-RUST-PULL-STATE-001``).

Pins, in the merged wave convention (the ``test_pull_scope_rust_seam``
shape):

* flag OFF (unset AND explicit falsy) is byte-identical and NEVER imports
  the kernel — the recording-bomb spy, with :class:`TestBombTeeth` proving
  the spy genuinely records on a flag-ON consult;
* flag ON SERVES the kernel verdict at all three consult sites
  (deliberately-WRONG fakes — the only honest proof of the kernel arm),
  incl. the flag-ON twin of the market-hours BELT arm (the kernel computes
  the belt from the crate's own market-hours core, so the oracle-side
  ``pull_allowed_default`` monkeypatch seam is Python-arm-only — the
  disclosed DEC-LAKE-RUST-LOCKS-ATOMIC-001 monkeypatch-seam class);
* every degrade (raising kernel · poisoned shapes/vocabulary) falls to the
  byte-identical Python oracle with the named warn-once;
* the fail-LOUD settle posture never moves: a non-OK kernel verdict routes
  to the oracle, which composes + raises the byte-identical ``ValueError``;
* the facade accessors' refuse-poison gates, branch by branch;
* a cross-language drift pin on the Rust const vocabulary;
* real-wheel parity (importorskip + capability-probed on the Wave-2D
  symbols — SKIPS until a wheel carrying them is installed): the frozen
  golden fixture replayed flag-ON through the REAL kernels.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace

import pytest
from brutex import pull_state_rust
from brutex.calendars import auto_pull_gate
from brutex.calendars.auto_pull_gate import (
    auto_pull_allowed,
    muhurat_session_active,
    resolve_settle_time,
)
from brutex.calendars.nse import IST
from brutex.pull_state_rust import (
    GATE_CODE_BEFORE_SETTLE,
    GATE_CODE_MARKET_HOURS_BLOCKED,
    GATE_CODE_MUHURAT_DEFERRAL,
    GATE_CODE_NON_TRADING_DAY,
    GATE_CODE_OK,
    SETTLE_CODE_OK,
    rust_auto_gate_decision,
    rust_muhurat_window_active,
    rust_settle_token_verdict,
)
from structlog.testing import capture_logs

_FLAG = pull_state_rust.PULL_STATE_RUST_ENV
_SETTLE_ENV = auto_pull_gate.PULL_ALL_SETTLE_ENV
_ENV_1730 = {_SETTLE_ENV: "17:30"}

#: A regular trading Tuesday (the golden's anchor date).
_TRADING_DAY = date(2024, 1, 2)
#: The 2024 Muhurat date (holiday-listed; session 18:00-19:00).
_MUHURAT_2024 = date(2024, 11, 1)

_JSON_FIXTURE = (
    Path(__file__).resolve().parents[1] / "golden" / "fixtures" / "auto_pull_gate_verdicts.json"
)


def _at(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm, ss), tzinfo=IST)


@pytest.fixture(autouse=True)
def _clean_warn_registry() -> Iterator[None]:
    pull_state_rust.reset_for_tests()
    yield
    pull_state_rust.reset_for_tests()


class _RecordingBomb:
    """A ``sys.modules`` plant that RECORDS every attribute touch.

    ``__getattr__`` raises :class:`AttributeError` (the special-method-
    appropriate type), so the TEETH are the recorded ``touched`` list: the
    flag-OFF pins assert it stays EMPTY, and :class:`TestBombTeeth` proves
    a flag-ON consult genuinely records (the spy cannot go silently blind).
    """

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> object:
        object.__getattribute__(self, "touched").append(name)
        raise AttributeError(name)


def _fake_kernel(**functions: object) -> SimpleNamespace:
    return SimpleNamespace(**functions)


# ── flag OFF: byte-identical + never imports (the bomb spy) ──────────────────


class TestFlagOffNeverTouchesKernel:
    def test_all_three_sites_flag_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_FLAG, raising=False)
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        verdict = auto_pull_allowed(now=_at(_TRADING_DAY, 18, 0), env=dict(_ENV_1730))
        assert (verdict.allowed, verdict.reason) == (True, "ok")
        assert muhurat_session_active(_at(_MUHURAT_2024, 18, 30)) is True
        assert resolve_settle_time(dict(_ENV_1730)) == time(17, 30)
        with pytest.raises(ValueError, match="not a strict HH:MM"):
            resolve_settle_time({_SETTLE_ENV: "junk"})
        assert bomb.touched == []

    def test_explicit_falsy_flag_also_short_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "0")
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        verdict = auto_pull_allowed(now=_at(_TRADING_DAY, 12, 0), env=dict(_ENV_1730))
        assert (verdict.allowed, verdict.reason) == (False, "before_settle")
        assert muhurat_session_active(_at(_TRADING_DAY, 18, 30)) is False
        assert bomb.touched == []


class TestBombTeeth:
    def test_flag_on_consult_records_the_touch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The spy has TEETH: a flag-ON consult genuinely records (so the
        flag-OFF empty-list assertions above can never pass vacuously)."""
        monkeypatch.setenv(_FLAG, "1")
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        # The AttributeError-raising bomb reads as a stale wheel → the
        # oracle still serves (never a crash) — AND the touch is recorded.
        verdict = auto_pull_allowed(now=_at(_TRADING_DAY, 18, 0), env=dict(_ENV_1730))
        assert (verdict.allowed, verdict.reason) == (True, "ok")
        assert "auto_gate_decision" in bomb.touched

    def test_sessionless_date_short_circuits_before_the_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The session LOOKUP stays Python: a timing-less date (2026-11-08)
        answers honestly False BEFORE any kernel consult — flag ON."""
        monkeypatch.setenv(_FLAG, "1")
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        assert muhurat_session_active(_at(date(2026, 11, 8), 18, 30)) is False
        assert bomb.touched == []


# ── flag ON: the kernel verdict is SERVED (deliberately-wrong fakes) ─────────


class TestFlagOnServesKernel:
    def test_gate_code_served_and_mapped_to_the_builders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        cases = [
            (GATE_CODE_NON_TRADING_DAY, False, "non_trading_day"),
            (GATE_CODE_MUHURAT_DEFERRAL, False, "muhurat_deferral"),
            (GATE_CODE_BEFORE_SETTLE, False, "before_settle"),
            (GATE_CODE_MARKET_HOURS_BLOCKED, False, "market_hours_blocked"),
        ]
        for code, allowed, reason in cases:
            monkeypatch.setitem(
                sys.modules,
                "brutex_pull_state",
                _fake_kernel(auto_gate_decision=lambda *a, _c=code: (_c, False)),
            )
            # A post-settle regular trading day reads "ok" on the oracle —
            # the deliberately-WRONG fake verdict being served proves the
            # kernel arm decided.
            verdict = auto_pull_allowed(now=_at(_TRADING_DAY, 18, 0), env=dict(_ENV_1730))
            assert (verdict.allowed, verdict.reason) == (allowed, reason), reason

    def test_gate_details_come_from_the_single_source_builders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(auto_gate_decision=lambda *a: (GATE_CODE_MARKET_HOURS_BLOCKED, False)),
        )
        # The flag-ON twin of the oracle's belt arm: the kernel computes the
        # belt INTERNALLY (the crate's one market-hours core), so the
        # Python-side pull_allowed_default monkeypatch seam is oracle-only —
        # here the served detail is pinned byte-equal to the builder's.
        verdict = auto_pull_allowed(now=_at(_TRADING_DAY, 18, 0), env=dict(_ENV_1730))
        assert verdict.detail == "inside the NSE market-hours pull window — deferred"
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(auto_gate_decision=lambda *a: (GATE_CODE_BEFORE_SETTLE, False)),
        )
        verdict = auto_pull_allowed(now=_at(_TRADING_DAY, 18, 0), env=dict(_ENV_1730))
        assert verdict.detail == (
            f"waiting for post-close settle time 17:30 IST (operator override {_SETTLE_ENV})"
        )

    def test_gate_coverage_warn_fired_by_python(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(auto_gate_decision=lambda *a: (GATE_CODE_OK, True)),
        )
        with capture_logs() as logs:
            verdict = auto_pull_allowed(now=_at(_TRADING_DAY, 18, 0), env=dict(_ENV_1730))
        assert (verdict.allowed, verdict.reason) == (True, "ok")
        events = [e for e in logs if e["event"] == "auto_pull_gate.calendar_coverage_exhausted"]
        assert len(events) == 1
        assert events[0]["date"] == _TRADING_DAY.isoformat()

    def test_gate_packs_resolved_predicates_and_instants(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        seen: list[tuple[object, ...]] = []

        def _spy(*args: object) -> tuple[int, bool]:
            seen.append(args)
            return (GATE_CODE_OK, False)

        monkeypatch.setitem(sys.modules, "brutex_pull_state", _fake_kernel(auto_gate_decision=_spy))
        auto_pull_allowed(now=_at(_MUHURAT_2024, 18, 30, 15), env=dict(_ENV_1730))
        # 2024-11-01 is a holiday-listed Muhurat date: trading False,
        # coverage True, muhurat True; 18:30:15 → µs-of-day; settle 17:30.
        assert seen == [
            (
                False,
                True,
                True,
                ((18 * 60 + 30) * 60 + 15) * 1_000_000,
                (17 * 60 + 30) * 60 * 1_000_000,
            )
        ]

    def test_muhurat_window_verdict_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(muhurat_window_active=lambda *a: True),
        )
        # 12:00 sits far OUTSIDE the 2024 session's margined window — the
        # deliberately-wrong True being served proves the kernel arm.
        assert muhurat_session_active(_at(_MUHURAT_2024, 12, 0)) is True

    def test_settle_ok_arm_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(settle_token_verdict=lambda *a: (SETTLE_CODE_OK, 16 * 3600 * 1_000_000)),
        )
        # A junk token served as 16:00 — proving the OK arm is kernel-decided.
        assert resolve_settle_time({_SETTLE_ENV: "nonsense"}) == time(16, 0)

    def test_settle_non_ok_routes_to_the_oracle_never_a_kernel_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        # A fake claiming bad_shape for a VALID token: the seam routes to
        # the oracle, which parses fine — never a fabricated refusal.
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(settle_token_verdict=lambda *a: (1, -1)),
        )
        assert resolve_settle_time(dict(_ENV_1730)) == time(17, 30)
        # And for genuine junk the oracle composes the byte-identical
        # fail-loud ValueError (the posture never moves).
        with pytest.raises(ValueError, match="not a strict HH:MM"):
            resolve_settle_time({_SETTLE_ENV: "junk"})
        with pytest.raises(ValueError, match="not a real wall-clock time"):
            resolve_settle_time({_SETTLE_ENV: "25:99"})
        with pytest.raises(ValueError, match="before the market-hours guard's"):
            resolve_settle_time({_SETTLE_ENV: "15:34"})


# ── flag ON: degrades (raising kernels · poisoned results) ───────────────────


class TestFlagOnDegrades:
    def test_raising_gate_kernel_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")

        def _boom(*args: object) -> tuple[int, bool]:
            raise RuntimeError("kernel exploded")

        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(
                auto_gate_decision=_boom,
                # A non-OK settle verdict routes the tier-1 parse to the
                # oracle silently, keeping the warn stream to the gate event;
                # the oracle ladder's step-4 belt consults pull_allowed_us
                # (the Wave-1 seam) — served truthfully here.
                settle_token_verdict=lambda *a: (1, -1),
                pull_allowed_us=lambda *a: True,
            ),
        )
        with capture_logs() as logs:
            verdict = auto_pull_allowed(now=_at(_TRADING_DAY, 18, 0), env=dict(_ENV_1730))
        assert (verdict.allowed, verdict.reason) == (True, "ok")
        events = [e for e in logs if e["event"] == pull_state_rust.PULL_STATE_RUST_FALLBACK_EVENT]
        assert [e["reason"] for e in events] == ["kernel_error"]
        assert events[0]["symbol"] == "auto_gate_decision"

    def test_poisoned_gate_verdict_degrades_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(
                auto_gate_decision=lambda *a: (7, False),  # out-of-vocabulary
                settle_token_verdict=lambda *a: (1, -1),
            ),
        )
        with capture_logs() as logs:
            verdict = auto_pull_allowed(now=_at(_TRADING_DAY, 12, 0), env=dict(_ENV_1730))
        assert (verdict.allowed, verdict.reason) == (False, "before_settle")
        assert [e["reason"] for e in logs if "reason" in e] == ["kernel_invalid"]

    def test_raising_muhurat_kernel_degrades_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")

        def _boom(*args: object) -> bool:
            raise ValueError("out of range")

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(muhurat_window_active=_boom)
        )
        assert muhurat_session_active(_at(_MUHURAT_2024, 18, 30)) is True

    def test_raising_settle_kernel_degrades_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")

        def _boom(*args: object) -> tuple[int, int]:
            raise OverflowError("floor overflow")

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(settle_token_verdict=_boom)
        )
        assert resolve_settle_time(dict(_ENV_1730)) == time(17, 30)


# ── the facade accessors' refuse-poison gates, branch by branch ──────────────


class TestFacadeGateAccessor:
    def _install(self, monkeypatch: pytest.MonkeyPatch, result: object) -> None:
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(auto_gate_decision=lambda *a: result),
        )

    def _call(self) -> tuple[int, bool] | None:
        return rust_auto_gate_decision(
            trading_day=True,
            coverage_available=True,
            muhurat_listed=False,
            now_us_of_day=0,
            settle_us_of_day=0,
        )

    def test_valid_pairs_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for pair in [(GATE_CODE_OK, False), (GATE_CODE_MARKET_HOURS_BLOCKED, True)]:
            self._install(monkeypatch, pair)
            assert self._call() == pair

    @pytest.mark.parametrize(
        "poison",
        [
            3,  # not a tuple
            (GATE_CODE_OK,),  # wrong arity
            (GATE_CODE_OK, False, True),  # wrong arity
            (True, False),  # bool code pun (bool is an int subclass)
            (7, False),  # out-of-vocabulary code
            (-1, False),  # out-of-vocabulary code
            (GATE_CODE_OK, 1),  # int-for-bool warn pun
            ("ok", False),  # str code
        ],
    )
    def test_poison_degrades_with_kernel_invalid(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        self._install(monkeypatch, poison)
        with capture_logs() as logs:
            assert self._call() is None
        assert [e["reason"] for e in logs if "reason" in e] == ["kernel_invalid"]

    def test_wheel_missing_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", _RecordingBomb())
        assert self._call() is None


class TestFacadeMuhuratAccessor:
    def test_valid_and_poison(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(muhurat_window_active=lambda *a: False)
        )
        assert rust_muhurat_window_active(0, 0, 0, 0, 0) is False
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(muhurat_window_active=lambda *a: 1)
        )
        with capture_logs() as logs:
            assert rust_muhurat_window_active(0, 0, 0, 0, 0) is None
        assert [e["reason"] for e in logs if "reason" in e] == ["kernel_invalid"]


class TestFacadeSettleAccessor:
    def _install(self, monkeypatch: pytest.MonkeyPatch, result: object) -> None:
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(settle_token_verdict=lambda *a: result),
        )

    def test_ok_pair_passes_when_minute_granular(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(monkeypatch, (SETTLE_CODE_OK, 63_000_000_000))
        assert rust_settle_token_verdict("17:30", 56_100_000_000) == (
            SETTLE_CODE_OK,
            63_000_000_000,
        )

    def test_non_ok_pair_passes_value_unconstrained(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The non-OK value slot is never consumed (the seam routes to the
        # oracle regardless) — the gate deliberately leaves it unconstrained.
        self._install(monkeypatch, (3, 12345))
        assert rust_settle_token_verdict("15:34", 56_100_000_000) == (3, 12345)

    @pytest.mark.parametrize(
        "poison",
        [
            "ok",  # not a tuple
            (SETTLE_CODE_OK,),  # wrong arity
            (True, 0),  # bool code pun
            (9, -1),  # out-of-vocabulary code
            (SETTLE_CODE_OK, True),  # bool value pun
            (SETTLE_CODE_OK, -60_000_000),  # OK value below range
            (SETTLE_CODE_OK, 86_400_000_000),  # OK value past the day
            (SETTLE_CODE_OK, 61_000_000),  # OK value off the minute grid
            (SETTLE_CODE_OK, "17:30"),  # str value
        ],
    )
    def test_poison_degrades_with_kernel_invalid(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        self._install(monkeypatch, poison)
        with capture_logs() as logs:
            assert rust_settle_token_verdict("17:30", 56_100_000_000) is None
        assert [e["reason"] for e in logs if "reason" in e] == ["kernel_invalid"]


# ── cross-language drift pin (the anchor-pin convention) ─────────────────────


class TestCrossLanguageCodeDriftPin:
    def test_rust_const_vocabulary_mirrors_the_facade(self) -> None:
        """The Rust const values must equal the facade's code vocabulary —
        a source-text pin (the pull_scope anchor-pin shape), so a silent
        renumber on either side fails loudly."""
        src = (
            Path(__file__).resolve().parents[2]
            / "rust"
            / "brutex-pull-state"
            / "src"
            / "auto_pull_gate.rs"
        ).read_text(encoding="utf-8")
        expected = [
            f"pub const GATE_OK: u8 = {GATE_CODE_OK};",
            f"pub const GATE_NON_TRADING_DAY: u8 = {GATE_CODE_NON_TRADING_DAY};",
            f"pub const GATE_MUHURAT_DEFERRAL: u8 = {GATE_CODE_MUHURAT_DEFERRAL};",
            f"pub const GATE_BEFORE_SETTLE: u8 = {GATE_CODE_BEFORE_SETTLE};",
            f"pub const GATE_MARKET_HOURS_BLOCKED: u8 = {GATE_CODE_MARKET_HOURS_BLOCKED};",
            f"pub const SETTLE_OK: u8 = {SETTLE_CODE_OK};",
            "pub const SETTLE_BAD_SHAPE: u8 = 1;",
            "pub const SETTLE_NOT_WALL_CLOCK: u8 = 2;",
            "pub const SETTLE_BELOW_FLOOR: u8 = 3;",
            "pub const SETTLE_NO_VALUE: i64 = -1;",
        ]
        for line in expected:
            assert line in src, line


# ── real-wheel parity (skips until a Wave-2D wheel is installed) ─────────────


def _require_wave2d_wheel() -> object:
    wheel = pytest.importorskip("brutex_pull_state", reason="brutex_pull_state wheel not installed")
    if not hasattr(wheel, "auto_gate_decision"):
        pytest.skip("installed brutex_pull_state wheel predates Wave 2D (no auto_gate_decision)")
    return wheel


class TestRealWheelParity:
    """Flag-ON vs the frozen golden — importorskip'd + capability-probed, so
    these SKIP until a wheel carrying the Wave-2D symbols is installed."""

    def test_gate_golden_replay_flag_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _require_wave2d_wheel()
        table = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        monkeypatch.setenv(_FLAG, "1")
        for env_value, rows in [
            (table["settle_env"][_SETTLE_ENV], table["cases"]),
            ("15:35", table["cases_settle_1535"]),
        ]:
            for row in rows:
                instant = datetime.combine(
                    date.fromisoformat(row["date"]),
                    time.fromisoformat(row["time"]),
                    tzinfo=IST,
                )
                verdict = auto_pull_allowed(now=instant, env={_SETTLE_ENV: env_value})
                assert (verdict.allowed, verdict.reason) == (row["allowed"], row["reason"]), row

    def test_muhurat_golden_replay_flag_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _require_wave2d_wheel()
        table = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        monkeypatch.setenv(_FLAG, "1")
        for row in table["muhurat_active"]:
            instant = datetime.combine(
                date.fromisoformat(row["date"]), time.fromisoformat(row["time"]), tzinfo=IST
            )
            assert muhurat_session_active(instant) is row["active"], row

    def test_settle_token_golden_replay_flag_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _require_wave2d_wheel()
        table = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        for row in table["settle_tokens"]:
            token = row["token"]
            # Both arms must agree byte-for-byte: value OR the raised text.
            monkeypatch.setenv(_FLAG, "0")
            try:
                off_value: time | None = resolve_settle_time({_SETTLE_ENV: token})
                off_error = None
            except ValueError as exc:
                off_value, off_error = None, str(exc)
            monkeypatch.setenv(_FLAG, "1")
            try:
                on_value: time | None = resolve_settle_time({_SETTLE_ENV: token})
                on_error = None
            except ValueError as exc:
                on_value, on_error = None, str(exc)
            assert (on_value, on_error) == (off_value, off_error), row
            if row["verdict"] == "ok":
                assert on_value is not None
                assert on_value.hour * 60 + on_value.minute == row["minute"], row

    def test_kernel_range_refusal_degrades_not_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real kernel's ValueError on an out-of-range input is caught by
        the facade belt (kernel_error) — the seam never raises."""
        wheel = _require_wave2d_wheel()
        with pytest.raises(ValueError, match="out of range"):
            wheel.auto_gate_decision(True, True, False, -1, 0)  # type: ignore[attr-defined]
        with capture_logs() as logs:
            assert (
                rust_auto_gate_decision(
                    trading_day=True,
                    coverage_available=True,
                    muhurat_listed=False,
                    now_us_of_day=-1,
                    settle_us_of_day=0,
                )
                is None
            )
        assert [e["reason"] for e in logs if "reason" in e] == ["kernel_error"]
