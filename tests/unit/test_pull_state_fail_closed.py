"""FAIL-CLOSED proof for the flipped pull-state guard chain
(DEC-RUST-PULL-STATE-001, flip phase).

THE LAW under test, end to end: the shared-TOTP vendor-account guard
(DEC-GROWW-MARKET-HOURS-GUARD-001 — no Groww vendor pulls 08:45-15:35 IST
on a trading day; ONE Groww account shared with TickVault's live feed) must
NEVER fail OPEN. With the Python oracles deleted, a missing/broken
``brutex_pull_state`` wheel must make every guard surface RAISE the typed
``PullStateKernelError`` — a pull is REFUSED (loudly, diagnosably), never
silently allowed.

Every test poisons the wheel import (``sys.modules["brutex_pull_state"] =
None`` ⇒ ImportError — the mechanism is pinned in
``tests/unit/test_pull_state_rust.py``) and drives a REAL production guard
surface.
"""

from __future__ import annotations

import sys
from datetime import datetime

import pytest
from brutex.calendars import market_hours
from brutex.calendars.nse import IST
from brutex.pull_state_rust import PullStateFlagRemovedError, PullStateKernelError

#: A verified NSE trading WEEKDAY (Thu 2026-05-07) — in-window instants on it
#: are the guard's BLOCK cases; out-of-window instants its ALLOW cases.
_TRADING_WEEKDAY = datetime(2026, 5, 7, tzinfo=IST)


def _at(hh: int, mm: int = 0) -> datetime:
    """A trading-weekday IST instant at the given wall-clock time."""
    return _TRADING_WEEKDAY.replace(hour=hh, minute=mm)


@pytest.fixture(autouse=True)
def _poisoned_wheel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file runs with the wheel import FAILING."""
    monkeypatch.delenv("BRUTEX_PULL_STATE_RUST", raising=False)
    monkeypatch.setitem(sys.modules, "brutex_pull_state", None)  # import ⇒ ImportError


class TestPullAllowedNowFailsClosed:
    """(a) The pure predicate: raises, never returns True."""

    @pytest.mark.parametrize(("hh", "mm"), [(10, 0), (1, 30), (20, 0)])
    def test_raises_never_returns(self, hh: int, mm: int) -> None:
        """The guard must never fail OPEN: with no kernel, EVERY instant —
        in-window AND out-of-window alike — raises instead of answering."""
        with pytest.raises(PullStateKernelError):
            market_hours.pull_allowed_now(_at(hh, mm), True)

    def test_non_trading_day_also_raises(self) -> None:
        """Even the always-allowed non-trading arm is kernel-served now —
        the fail-closed posture is total, never a partial Python shortcut."""
        with pytest.raises(PullStateKernelError):
            market_hours.pull_allowed_now(_at(12, 0), False)

    def test_set_removed_knob_also_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The SET-knob refusal reaches the guard chain ahead of the wheel."""
        monkeypatch.setenv("BRUTEX_PULL_STATE_RUST", "junk")
        with pytest.raises(PullStateFlagRemovedError):
            market_hours.pull_allowed_now(_at(10, 0), True)


class TestGuardBindingsFailClosed:
    """(b) The live-clock bindings propagate the error — never 'allowed'."""

    def test_pull_allowed_default_propagates(self) -> None:
        """``pull_allowed_default`` must never answer ``True`` wheel-less —
        the vendor-account guard would silently open."""
        with pytest.raises(PullStateKernelError):
            market_hours.pull_allowed_default(now=_at(10, 0))

    def test_guard_active_default_propagates(self) -> None:
        """``guard_active_default`` (the START-gate consult) must never answer
        ``False`` (= 'not blocked') wheel-less — that IS the fail-open."""
        with pytest.raises(PullStateKernelError):
            market_hours.guard_active_default(now=_at(10, 0), env={})

    def test_guard_object_blocked_propagates(self) -> None:
        """The mid-run ``MarketHoursGuard.blocked()`` consult fails closed too."""
        guard = market_hours.MarketHoursGuard(enabled=True, clock=lambda: _at(10, 0))
        with pytest.raises(PullStateKernelError):
            guard.blocked()

    def test_disabled_guard_env_short_circuits_without_the_kernel(self) -> None:
        """HONEST scope pin: the operator kill switch (``off``) short-circuits
        BEFORE the kernel — disabling the guard is an explicit, fail-loud env
        choice, not a wheel-fault fallback."""
        assert (
            market_hours.guard_active_default(
                now=_at(10, 0), env={market_hours.MARKET_HOURS_GUARD_ENV: "off"}
            )
            is False
        )


class TestBackgroundPullJobStartFailsClosed:
    """(c) The production START gate: the worker is NEVER invoked."""

    def test_start_gate_raises_and_never_spawns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A JOB_KIND_PULL start with the DEFAULT market-hours guard and no
        kernel RAISES out of ``start()`` — the worker fn is never invoked,
        nothing is spawned: the vendor pull is refused, never fail-open
        launched (the shared-TOTP account law)."""
        from brutex.dashboard import pull_jobs
        from brutex.dashboard.pull_progress import PullProgress

        # The unit-test posture of tests/unit/test_pull_jobs.py: no real
        # cross-process flock under the production lake root (the guard gate
        # fires before the locks anyway — this is defence in depth).
        monkeypatch.setattr(pull_jobs, "_default_lake_root_resolver", lambda: None)
        # The suite-wide conftest hermeticity belt sets the guard kill switch
        # to "0"; this test needs the guard ENABLED so guard_active_default
        # actually consults the (poisoned) kernel.
        monkeypatch.setenv(market_hours.MARKET_HOURS_GUARD_ENV, "1")

        spawned: list[object] = []
        invoked: list[object] = []

        def work(_on_progress: object) -> None:
            invoked.append(_on_progress)

        job = pull_jobs.BackgroundPullJob(progress=PullProgress(), spawn=spawned.append)
        with pytest.raises(PullStateKernelError):
            job.start(work, total=1)
        assert invoked == []  # the worker target was never called
        assert spawned == []  # nothing was handed to the spawner
        assert job.running is False  # no run was latched

    def test_exempt_start_never_consults_the_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HONEST scope pin: a ``market_hours_exempt=True`` start (zero vendor
        calls by contract) bypasses the gate — the fail-closed law binds the
        VENDOR-pull arm, exactly as designed."""
        from brutex.dashboard import pull_jobs
        from brutex.dashboard.pull_progress import PullProgress

        monkeypatch.setattr(pull_jobs, "_default_lake_root_resolver", lambda: None)
        monkeypatch.setenv(market_hours.MARKET_HOURS_GUARD_ENV, "1")

        ran: list[str] = []
        job = pull_jobs.BackgroundPullJob(progress=PullProgress(), spawn=lambda fn: fn())
        assert job.start(lambda _p: ran.append("ran"), total=1, market_hours_exempt=True) is True
        assert ran == ["ran"]


class TestResolveMarketClosedFailsClosed:
    """(d) The mid-run belt: the bound predicate's CALL raises."""

    def test_enabled_predicate_call_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``resolve_market_closed`` (guard enabled) binds a clock-only
        predicate whose CALL raises wheel-less — a mid-run window check can
        never silently read 'market open, keep pulling'."""
        predicate = market_hours.resolve_market_closed(env={})
        monkeypatch.setattr("brutex.calendars.market_hours.now_ist", lambda: _at(10, 0))
        with pytest.raises(PullStateKernelError):
            predicate()

    def test_disabled_predicate_stays_inert(self) -> None:
        """HONEST scope pin: the disabled arm is the constant-False predicate —
        an explicit operator choice, kernel never consulted."""
        predicate = market_hours.resolve_market_closed(
            env={market_hours.MARKET_HOURS_GUARD_ENV: "off"}
        )
        assert predicate() is False


class TestAutoGateConsultFailsClosed:
    """(e) The --auto gate's kernel consult chokepoint."""

    def test_rust_auto_gate_decision_raises(self) -> None:
        """The gate-consult chokepoint fails closed: ``rust_auto_gate_decision``
        raises wheel-less, so a kernel-backed ``auto_pull_allowed`` can never
        answer 'ok' without the kernel.

        NOTE: ``brutex.calendars.auto_pull_gate`` still carries its flag-gated
        seam until the sibling worker lands its flip — the module-level
        ``auto_pull_allowed`` fail-closed pin lands with that module's own
        flip; THIS pin proves the facade chokepoint it will bind to.
        """
        from brutex.pull_state_rust import rust_auto_gate_decision

        with pytest.raises(PullStateKernelError):
            rust_auto_gate_decision(
                trading_day=True,
                coverage_available=True,
                muhurat_listed=False,
                now_us_of_day=18 * 3_600_000_000,
                settle_us_of_day=17 * 3_600_000_000 + 30 * 60_000_000,
            )
