"""The rate-bridge-cache Rust kernel path (``DEC-RUST-PULL-STATE-001``, Wave 3H).

Flip phase — covers the ``brutex/dashboard/rate_bridge_cache.py`` half of
the port: the TWO ported laws (the three-arm serve decision + the token
revalidation) are the kernel, no flag, no oracle; the
``NonBlockingFileMemoBase`` THREAD CORE (lock, deadline-guard daemon,
``_inflight`` clearing, degrade latch) is PERMANENT-PYTHON (deferred to
``DEC-RUST-PULL-GLUE-002``).

* the kernel arm is AUTHORITATIVE (an in-vocabulary answer is taken) and
  the SINGLE-FLIGHT FAIL-SAFE BELT holds: a poisoned kernel answering OWNER
  while a refresh is in flight is FORCED to the follower serve — never a
  second refresh daemon;
* a SERVE-path wheel fault RAISES the typed :class:`PullStateKernelError`
  out of ``get()`` (fail-closed, ``_inflight`` never leaked); a REFRESH-path
  fault folds into the PERMANENT-PYTHON base catch law
  (``refresh_failed`` WARN, memo kept, TTL-cadence retry);
* a still-SET removed ``BRUTEX_PULL_STATE_RUST`` knob raises
  :class:`PullStateFlagRemovedError`;
* the REAL wheel replays the WHOLE frozen cache golden arm-for-arm, and the
  frozen TSV rows replay through the facade.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from brutex import pull_state_rust
from brutex.dashboard.rate_bridge_cache import (
    MEMO_SOURCE_CACHED,
    MEMO_SOURCE_UNVERIFIED,
)
from brutex.dashboard.sweep_progress_cache import SweepProgressCache
from structlog.testing import capture_logs

from tests.golden import _rate_bridge_cache_harness as harness

FIXTURES = Path(__file__).resolve().parents[1] / "golden" / "fixtures"
JSON_FIXTURE = FIXTURES / "rate_bridge_cache_decisions.json"


def _install(monkeypatch: pytest.MonkeyPatch, **symbols: Any) -> None:
    """Plant a fake ``brutex_pull_state`` module exposing *symbols*."""
    monkeypatch.setitem(sys.modules, "brutex_pull_state", SimpleNamespace(**symbols))


def _cache(
    *,
    now: float = 1024.0,
    ttl: float = 2.0,
    token: str | None = "tok",  # noqa: S107 — a content-digest fixture, not a secret
) -> tuple[Any, dict[str, int]]:
    """A harness-built cache with counting probe/read/guard seams."""
    calls = {"probe": 0, "read": 0, "guard": 0}
    return (
        harness._build_cache(now=now, ttl=ttl, token=token, calls=calls),
        calls,
    )


# ---------------------------------------------------------------------------
# The kernel-authoritative laws (injected kernels).
# ---------------------------------------------------------------------------


class TestKernelAuthoritative:
    """The kernel arms decide the serve + revalidation — never Python."""

    def test_kernel_arm_is_authoritative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An in-vocabulary CACHED arm is taken even where the deleted
        oracle would have refreshed (proves the serve consults the kernel)."""
        _install(monkeypatch, memo_serve_arm=lambda *_a: 0)
        cache, calls = _cache()
        cache._probed_at = 1000.0  # stale — the pre-flip law would take OWNER
        result = cache.get(Path("/nonexistent-lake"))
        assert result.source == MEMO_SOURCE_CACHED and calls["guard"] == 0

    def test_owner_while_inflight_is_belted_to_follower(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The single-flight fail-safe belt: a poisoned OWNER arm while a
        refresh is in flight serves the follower answer and NEVER spawns a
        second refresh (the safety-critical corruption a kernel must never
        be able to cause)."""
        _install(monkeypatch, memo_serve_arm=lambda *_a: 2)  # always OWNER
        cache, calls = _cache()
        cache._probed_at = 1000.0
        cache._inflight = True
        result = cache.get(Path("/nonexistent-lake"))
        assert result.source == MEMO_SOURCE_UNVERIFIED
        assert calls["guard"] == 0  # no second daemon
        assert cache._inflight is True  # the flag is the daemon's to clear

    def test_token_kernel_is_authoritative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A kernel ``True`` on DIFFERING tokens keeps the memo (the
        kernel-authoritative revalidate path)."""
        _install(monkeypatch, token_revalidates=lambda *_a: True)
        cache, calls = _cache(token="t2")
        cache._token = "t1"
        cache._refresh_body(Path("/nonexistent-lake"))
        assert calls["read"] == 0  # revalidated — no re-read
        assert cache._token == "t1"  # memoised token kept

    def test_token_false_rereads_and_stores_the_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A kernel ``False`` re-reads and stores the freshly probed token."""
        _install(monkeypatch, token_revalidates=lambda *_a: False)
        cache, calls = _cache(token="t2")
        cache._token = "t1"
        cache._refresh_body(Path("/nonexistent-lake"))
        assert calls["read"] == 1
        assert cache._token == "t2"


class TestFailClosed:
    """Serve-path wheel faults raise; refresh-path faults fold into the base."""

    def test_missing_wheel_raises_out_of_get(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing wheel raises the typed error out of ``get()`` — the
        lock unwinds, ``_inflight`` never leaks (the next healthy call still
        owns the refresh)."""
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        cache, calls = _cache()
        cache._probed_at = 1023.0
        with pytest.raises(pull_state_rust.PullStateKernelError, match="wheel_missing"):
            cache.get(Path("/nonexistent-lake"))
        assert cache._inflight is False and calls["guard"] == 0

    def test_stale_wheel_raises_naming_the_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A wheel predating the symbol refuses naming it (``wheel_stale``)."""
        _install(monkeypatch)
        cache, _calls = _cache()
        with pytest.raises(pull_state_rust.PullStateKernelError, match="memo_serve_arm"):
            cache.get(Path("/nonexistent-lake"))

    @pytest.mark.parametrize("poison", [None, True, -1, 3, 2.0, "0"])
    def test_serve_arm_poison_raises_kernel_invalid(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        """Every out-of-vocabulary serve arm is refused (``kernel_invalid``)."""
        _install(monkeypatch, memo_serve_arm=lambda *_a: poison)
        cache, _calls = _cache()
        with pytest.raises(pull_state_rust.PullStateKernelError, match="kernel_invalid"):
            cache.get(Path("/nonexistent-lake"))

    def test_set_removed_flag_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A still-SET removed knob refuses loudly — truthy AND falsy alike."""
        cache, _calls = _cache()
        for value in ("1", "false"):
            monkeypatch.setenv(pull_state_rust.PULL_STATE_RUST_ENV, value)
            with pytest.raises(pull_state_rust.PullStateFlagRemovedError):
                cache.get(Path("/nonexistent-lake"))

    def test_refresh_path_fault_folds_into_the_base_catch_law(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A token-kernel fault on the refresh DAEMON folds into the
        PERMANENT-PYTHON base catch law: ONE ``refresh_failed`` WARN, memo
        kept, ``_probed_at`` advanced (TTL-cadence retry), ``_inflight``
        cleared — a daemon raise has nowhere louder to go."""

        def _poison_token(*_a: object) -> object:
            raise RuntimeError("token kernel bomb")

        _install(monkeypatch, memo_serve_arm=lambda *_a: 2, token_revalidates=_poison_token)
        cache, calls = _cache()
        cache._snapshot = harness._snapshot(7.0)
        with capture_logs() as logs:
            result = cache.get(Path("/nonexistent-lake"))
        events = [e for e in logs if e["event"] == "rate_bridge_cache.refresh_failed"]
        assert len(events) == 1
        assert events[0]["error_type"] == "PullStateKernelError"
        # The inline guard COMPLETED (its daemon ran + cleared the flag), so
        # the owner serve reads LIVE; the memo survived, the read never ran.
        assert result.snapshot is not None and result.snapshot.effective_rps == 7.0
        assert calls["read"] == 0
        assert cache._inflight is False
        assert cache._probed_at == 1024.0  # advanced — TTL-cadence retry


class TestSweepTwin:
    """The sweep twin inherits the base's kernel-backed serve law."""

    def test_sweep_twin_serves_through_the_real_kernel(self, tmp_path: Path) -> None:
        """``SweepProgressCache`` (the fix-6b twin) rides the SAME
        kernel-backed serve core with the REAL wheel — the flip covers both
        twins through the ONE base."""

        progress = tmp_path / "progress.json"
        progress.write_text("{}", encoding="utf-8")
        cache = SweepProgressCache(
            ttl_s=2.0,
            read_deadline_s=0.5,
            clock=lambda: 1024.0,
            reader=lambda _p: {"state": "running"},
            guard=lambda fn, _s: fn(),
        )
        stamped = cache.read(progress)
        assert stamped is not None
        assert stamped["state"] == "running"
        assert stamped["progress_source"] == "live"

    def test_sweep_twin_fails_closed_on_a_missing_wheel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flip phase: a missing wheel raises out of the twin's read too —
        fail-closed, the base seam has no oracle to fall back to."""
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)

        progress = tmp_path / "progress.json"
        progress.write_text("{}", encoding="utf-8")
        cache = SweepProgressCache(
            ttl_s=2.0,
            read_deadline_s=0.5,
            clock=lambda: 1024.0,
            reader=lambda _p: {"state": "running"},
            guard=lambda fn, _s: fn(),
        )
        with pytest.raises(pull_state_rust.PullStateKernelError, match="wheel_missing"):
            cache.read(progress)


# ---------------------------------------------------------------------------
# The REAL wheel — the frozen golden replays end-to-end.
# ---------------------------------------------------------------------------


def test_real_wheel_replays_the_frozen_golden() -> None:
    """The WHOLE frozen cache golden (serve + token tables) replays
    arm-for-arm through the LIVE (kernel-only) module."""
    live = json.loads(json.dumps(harness.compute_payload(), allow_nan=False))
    frozen = json.loads(JSON_FIXTURE.read_text(encoding="utf-8"))
    assert live["serve"] == frozen["serve"]
    assert live["token"] == frozen["token"]


def test_real_wheel_kernel_differential_over_the_frozen_tsv() -> None:
    """Every frozen TSV row through the REAL kernels via the facade."""
    serve_rows = token_rows = 0
    raw = (FIXTURES / "rate_bridge_cache_kernel_cases.tsv").read_text(encoding="utf-8")
    for line in raw.splitlines():
        if line.startswith("#") or not line:
            continue
        cells = line.split("\t")
        if cells[0] == "serve":
            age = None if cells[2] == "0" else harness.f64_from_bits(cells[3])
            arm = pull_state_rust.rust_memo_serve_arm(
                age, harness.f64_from_bits(cells[4]), cells[5] == "1"
            )
            assert arm == int(cells[6]), cells[1]
            serve_rows += 1
        else:
            assert cells[0] == "token"

            def _tok(cell: str) -> str | None:
                return None if cell == "\\N" else cell.removeprefix("s:")

            verdict = pull_state_rust.rust_token_revalidates(_tok(cells[3]), _tok(cells[2]))
            assert verdict is (cells[4] == "1"), cells[1]
            token_rows += 1
    assert serve_rows >= 12 and token_rows >= 6  # anti-vacuous
