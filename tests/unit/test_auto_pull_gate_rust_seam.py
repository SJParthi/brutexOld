"""The ``auto_pull_gate`` × ``pull_state_rust`` kernel seam (DEC-RUST-PULL-STATE-001, flip phase).

Post-flip pins (the Python oracle ladder/window/parse bodies are DELETED —
the ``brutex_pull_state`` kernel is THE decision plane, fail-closed):

* the kernel verdict is SERVED at all three consult sites
  (deliberately-WRONG fakes — the only honest proof the kernel decided),
  incl. the market-hours BELT arm's code → verdict-builder mapping (the
  kernel computes the belt from the crate's own market-hours core; cargo
  ``market_hours_belt_stays_reachable`` pins its reachability);
* the session LOOKUP stays Python — a timing-less date answers ``False``
  BEFORE any kernel consult (the recording-bomb spy);
* every wheel fault (missing / stale / raising) and every poisoned result is
  a typed :class:`PullStateKernelError` RAISE — no oracle fallback exists;
* a SET ``BRUTEX_PULL_STATE_RUST`` knob refuses loudly
  (:class:`PullStateFlagRemovedError`) at every site — the removed-flag law;
* the fail-LOUD settle posture: the kernel's non-OK verdicts route to the
  CONSUMER's byte-identical ``ValueError`` compositions (kind-matched over
  the frozen golden matrix — real wheel);
* the facade accessors' refuse-poison gates, branch by branch;
* a cross-language drift pin on the Rust const vocabulary;
* real-wheel golden replay: the frozen fixture through the REAL kernels.
"""

from __future__ import annotations

import json
import sys
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
    PullStateFlagRemovedError,
    PullStateKernelError,
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

#: The kernel's OK settle pair for 17:30 IST — what a fake must answer so a
#: ``auto_pull_allowed(env=_ENV_1730)`` drive can reach the GATE consult
#: (post-flip the settle parse is kernel-only, so every fake carries it).
_SETTLE_OK_1730 = (SETTLE_CODE_OK, (17 * 60 + 30) * 60 * 1_000_000)

_JSON_FIXTURE = (
    Path(__file__).resolve().parents[1] / "golden" / "fixtures" / "auto_pull_gate_verdicts.json"
)


def _at(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    """An IST wall-clock instant on day *d* (the golden's packing law)."""
    return datetime.combine(d, time(hh, mm, ss), tzinfo=IST)


@pytest.fixture(autouse=True)
def _knob_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The removed opt-in knob must be UNSET for every non-refusal pin (a
    SET value refuses loudly by design — pinned in its own class below)."""
    monkeypatch.delenv(_FLAG, raising=False)


class _RecordingBomb:
    """A ``sys.modules`` plant that RECORDS every attribute touch.

    ``__getattr__`` raises :class:`AttributeError` (which the facade's
    3-arg ``getattr`` folds into the ``wheel_stale`` refusal), so the TEETH
    are the recorded ``touched`` list: the stays-Python pins assert it
    stays EMPTY while the fault pins prove a consult genuinely records.
    """

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> object:
        """Record *name* then raise (the stale-wheel shape)."""
        object.__getattribute__(self, "touched").append(name)
        raise AttributeError(name)


def _fake_kernel(**functions: object) -> SimpleNamespace:
    """A fake wheel; the settle kernel defaults to the OK-17:30 answer so a
    gate/muhurat fake can be driven through ``env=_ENV_1730``."""
    functions.setdefault("settle_token_verdict", lambda *a: _SETTLE_OK_1730)
    return SimpleNamespace(**functions)


# ── the boundary that stays Python (session lookup) ──────────────────────────


class TestSessionLookupStaysPython:
    def test_sessionless_date_short_circuits_before_the_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The session LOOKUP stays Python: a timing-less date (2026-11-08)
        answers honestly False BEFORE any kernel consult."""
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        assert muhurat_session_active(_at(date(2026, 11, 8), 18, 30)) is False
        assert bomb.touched == []


# ── the kernel verdict is SERVED (deliberately-wrong fakes) ──────────────────


class TestKernelServes:
    def test_gate_code_served_and_mapped_to_the_builders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every non-OK gate code maps to its verdict builder — a post-settle
        trading day would read ``ok``, so the WRONG fake being served proves
        the kernel decided."""
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
            verdict = auto_pull_allowed(now=_at(_TRADING_DAY, 18, 0), env=dict(_ENV_1730))
            assert (verdict.allowed, verdict.reason) == (allowed, reason), reason

    def test_gate_details_come_from_the_single_source_builders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The served detail strings are the Python builders' bytes — the
        BELT arm included (the kernel computes the belt INTERNALLY from the
        crate's one market-hours core; its detail stays single-sourced)."""
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(auto_gate_decision=lambda *a: (GATE_CODE_MARKET_HOURS_BLOCKED, False)),
        )
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
        """The coverage WARN rides the kernel's ``coverage_warn`` flag but is
        composed + emitted by Python (the one wording source)."""
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
        """The FFI packing law: Python-resolved calendar predicates + the
        microsecond-of-day instant/settle pair, nothing else."""
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
        """12:00 sits far OUTSIDE the 2024 session's margined window — the
        deliberately-wrong True being served proves the kernel arm."""
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(muhurat_window_active=lambda *a: True),
        )
        assert muhurat_session_active(_at(_MUHURAT_2024, 12, 0)) is True

    def test_settle_ok_arm_served(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A junk token served as 16:00 — proving the OK arm is kernel-decided."""
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(settle_token_verdict=lambda *a: (SETTLE_CODE_OK, 16 * 3600 * 1_000_000)),
        )
        assert resolve_settle_time({_SETTLE_ENV: "nonsense"}) == time(16, 0)

    def test_settle_non_ok_codes_map_to_the_consumer_refusals(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each non-OK verdict code maps to ITS Python-composed fail-loud
        ``ValueError`` — the kernel decides, the consumer owns every byte
        (the wrong fakes on a VALID token prove the code→message routing)."""
        for code, match in [
            (1, "not a strict HH:MM"),
            (2, "not a real wall-clock time"),
            (3, "before the market-hours guard's"),
        ]:
            monkeypatch.setitem(
                sys.modules,
                "brutex_pull_state",
                _fake_kernel(settle_token_verdict=lambda *a, _c=code: (_c, -1)),
            )
            with pytest.raises(ValueError, match=match):
                resolve_settle_time(dict(_ENV_1730))


# ── kernel faults are typed raises (fail-closed — no oracle exists) ──────────


class TestKernelFaultsFailClosed:
    def test_raising_gate_kernel_raises_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args: object) -> tuple[int, bool]:
            raise RuntimeError("kernel exploded")

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_gate_decision=_boom)
        )
        with pytest.raises(PullStateKernelError, match="kernel_error"):
            auto_pull_allowed(now=_at(_TRADING_DAY, 18, 0), env=dict(_ENV_1730))

    def test_poisoned_gate_verdict_raises_kernel_invalid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(auto_gate_decision=lambda *a: (7, False)),  # out-of-vocabulary
        )
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            auto_pull_allowed(now=_at(_TRADING_DAY, 12, 0), env=dict(_ENV_1730))

    def test_raising_muhurat_kernel_raises_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args: object) -> bool:
            raise ValueError("out of range")

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(muhurat_window_active=_boom)
        )
        with pytest.raises(PullStateKernelError, match="kernel_error"):
            muhurat_session_active(_at(_MUHURAT_2024, 18, 30))

    def test_raising_settle_kernel_raises_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args: object) -> tuple[int, int]:
            raise OverflowError("floor overflow")

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(settle_token_verdict=_boom)
        )
        with pytest.raises(PullStateKernelError, match="kernel_error"):
            resolve_settle_time(dict(_ENV_1730))

    def test_missing_wheel_raises_wheel_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)  # ImportError shape
        with pytest.raises(PullStateKernelError, match="wheel_missing"):
            auto_pull_allowed(now=_at(_TRADING_DAY, 18, 0), env=dict(_ENV_1730))

    def test_stale_wheel_bomb_raises_wheel_stale_and_records(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The AttributeError bomb reads as a stale wheel (the facade's 3-arg
        ``getattr`` folds it) — AND the touch is recorded, proving the
        stays-Python empty-list pins can never pass vacuously."""
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        with pytest.raises(PullStateKernelError, match="wheel_stale"):
            muhurat_session_active(_at(_MUHURAT_2024, 18, 30))
        assert "muhurat_window_active" in bomb.touched


# ── the removed-knob refusal law ─────────────────────────────────────────────


class TestFlagRemovedRefusal:
    @pytest.mark.parametrize("value", ["1", "0", "junk"])
    def test_every_site_refuses_when_the_removed_knob_is_set(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """ANY non-empty ``BRUTEX_PULL_STATE_RUST`` — truthy spellings
        included — refuses loudly at every consult site
        (DEC-RUST-PULL-STATE-001: the knob is removed, never honoured)."""
        monkeypatch.setenv(_FLAG, value)
        with pytest.raises(PullStateFlagRemovedError, match=_FLAG):
            auto_pull_allowed(now=_at(_TRADING_DAY, 18, 0), env=dict(_ENV_1730))
        with pytest.raises(PullStateFlagRemovedError, match=_FLAG):
            muhurat_session_active(_at(_MUHURAT_2024, 18, 30))
        with pytest.raises(PullStateFlagRemovedError, match=_FLAG):
            resolve_settle_time(dict(_ENV_1730))


# ── the facade accessors' refuse-poison gates, branch by branch ──────────────


class TestFacadeGateAccessor:
    def _install(self, monkeypatch: pytest.MonkeyPatch, result: object) -> None:
        """Plant a fake wheel whose gate kernel answers *result*."""
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(auto_gate_decision=lambda *a: result),
        )

    def _call(self) -> tuple[int, bool]:
        """Drive the facade accessor with a fixed benign input."""
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
    def test_poison_raises_kernel_invalid(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        self._install(monkeypatch, poison)
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            self._call()

    def test_wheel_missing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        with pytest.raises(PullStateKernelError, match="wheel_missing"):
            self._call()


class TestFacadeMuhuratAccessor:
    def test_valid_and_poison(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(muhurat_window_active=lambda *a: False)
        )
        assert rust_muhurat_window_active(0, 0, 0, 0, 0) is False
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(muhurat_window_active=lambda *a: 1)
        )
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            rust_muhurat_window_active(0, 0, 0, 0, 0)


class TestFacadeSettleAccessor:
    def _install(self, monkeypatch: pytest.MonkeyPatch, result: object) -> None:
        """Plant a fake wheel whose settle kernel answers *result*."""
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
        # The non-OK value slot is never consumed (the caller composes its
        # refusal off the CODE) — the gate deliberately leaves it alone.
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
    def test_poison_raises_kernel_invalid(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        self._install(monkeypatch, poison)
        with pytest.raises(PullStateKernelError, match="kernel_invalid"):
            rust_settle_token_verdict("17:30", 56_100_000_000)


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


# ── real-wheel golden replay (the installed wheel IS the decision plane) ─────


class TestRealWheelParity:
    """The frozen golden fixture replayed through the REAL kernels — the
    public functions are kernel-backed, so the replay IS the kernel pin."""

    def test_gate_golden_replay(self) -> None:
        table = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
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

    def test_muhurat_golden_replay(self) -> None:
        table = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        for row in table["muhurat_active"]:
            instant = datetime.combine(
                date.fromisoformat(row["date"]), time.fromisoformat(row["time"]), tzinfo=IST
            )
            assert muhurat_session_active(instant) is row["active"], row

    def test_settle_token_golden_replay(self) -> None:
        """Every frozen token: an OK verdict serves the recorded minute; a
        refusal raises ITS kind's byte-identical ``ValueError`` (the message
        kinds the pre-flip oracle froze — Python-composed, kernel-decided)."""
        kind_match = {
            "bad_shape": "not a strict HH:MM",
            "not_wall_clock": "not a real wall-clock time",
            "below_floor": "before the market-hours guard's",
        }
        table = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        for row in table["settle_tokens"]:
            token = row["token"]
            if row["verdict"] == "ok":
                value = resolve_settle_time({_SETTLE_ENV: token})
                assert value.hour * 60 + value.minute == row["minute"], row
            else:
                with pytest.raises(ValueError, match=kind_match[row["verdict"]]):
                    resolve_settle_time({_SETTLE_ENV: token})

    def test_kernel_range_refusal_is_a_typed_raise(self) -> None:
        """The real kernel's ValueError on an out-of-range input becomes the
        facade's typed ``kernel_error`` raise — fail-closed, never a silent
        verdict."""
        import brutex_pull_state

        with pytest.raises(ValueError, match="out of range"):
            brutex_pull_state.auto_gate_decision(True, True, False, -1, 0)
        with pytest.raises(PullStateKernelError, match="kernel_error"):
            rust_auto_gate_decision(
                trading_day=True,
                coverage_available=True,
                muhurat_listed=False,
                now_us_of_day=-1,
                settle_us_of_day=0,
            )
