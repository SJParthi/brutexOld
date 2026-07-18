"""The rate-bridge-cache Rust seam (``DEC-RUST-PULL-STATE-001``, Wave 3H).

Covers the ``brutex/dashboard/rate_bridge_cache.py`` half of the Wave-3H
port — the design §2.3 MINIMAL kernel (serve arm + token revalidation; the
``NonBlockingFileMemoBase`` thread core is PERMANENT-PYTHON):

* FLAG OFF (default) is byte-identical AND provably never touches the wheel;
* the facade accessors ``rust_memo_serve_arm`` / ``rust_token_revalidates``
  — degrade tiers + refuse-poison vocabulary gates;
* the seam laws — a kernel arm is AUTHORITATIVE (an in-vocabulary answer is
  taken), every degrade falls back to the byte-identical oracle conditions,
  and the SINGLE-FLIGHT FAIL-SAFE BELT holds: a poisoned kernel answering
  OWNER while a refresh is in flight is FORCED to the follower serve —
  never a second refresh daemon;
* the SWEEP twin (``SweepProgressCache`` inherits the base) stays healthy
  under the flag with a missing wheel (degrade → oracle — the base seam can
  never break the sweep lane);
* FLAG ON with the REAL wheel (capability-probed): the WHOLE frozen cache
  golden replays arm-for-arm through the live module.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from brutex import pull_state_rust
from tests.golden import _rate_bridge_cache_harness as harness

FLAG_ENV = "BRUTEX_PULL_STATE_RUST"
FIXTURES = Path(__file__).resolve().parents[1] / "golden" / "fixtures"
JSON_FIXTURE = FIXTURES / "rate_bridge_cache_decisions.json"


@pytest.fixture(autouse=True)
def _clean_warn_registry() -> Any:
    pull_state_rust.reset_for_tests()
    yield
    pull_state_rust.reset_for_tests()


class _BombKernelModule:
    """Records every attribute access, then raises ``AttributeError``."""

    def __init__(self) -> None:
        self.touches: list[str] = []

    def __getattr__(self, name: str) -> object:
        object.__getattribute__(self, "touches").append(name)
        raise AttributeError(name)


def _install(monkeypatch: pytest.MonkeyPatch, **symbols: Any) -> None:
    monkeypatch.setitem(sys.modules, "brutex_pull_state", SimpleNamespace(**symbols))


def _cache(
    *, now: float = 1024.0, ttl: float = 2.0, token: str | None = "tok"
) -> tuple[Any, dict[str, int]]:
    calls = {"probe": 0, "read": 0, "guard": 0}
    return (
        harness._build_cache(now=now, ttl=ttl, token=token, calls=calls),  # noqa: SLF001
        calls,
    )


# ---------------------------------------------------------------------------
# Flag OFF — byte-identical oracle, wheel provably never touched.
# ---------------------------------------------------------------------------


class TestFlagOffNeverImports:
    @pytest.mark.parametrize("flag", [None, "0"])
    def test_serve_and_refresh_are_oracle_and_wheel_untouched(
        self, monkeypatch: pytest.MonkeyPatch, flag: str | None
    ) -> None:
        if flag is None:
            monkeypatch.delenv(FLAG_ENV, raising=False)
        else:
            monkeypatch.setenv(FLAG_ENV, flag)
        bomb = _BombKernelModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        cache, calls = _cache()
        cache._probed_at = 1023.0  # fresh — the TTL fast path
        result = cache.get(Path("/nonexistent-lake"))
        assert result.source == "cached" and calls["guard"] == 0
        cache._probed_at = None  # cold — the owner arm runs the refresh
        result = cache.get(Path("/nonexistent-lake"))
        assert result.source == "live" and calls["guard"] == 1
        assert bomb.touches == []


# ---------------------------------------------------------------------------
# The facade accessors — degrade tiers + refuse-poison gates.
# ---------------------------------------------------------------------------


class TestFacadeAccessors:
    def test_missing_wheel_degrades_wheel_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        with capture_logs() as logs:
            assert pull_state_rust.rust_memo_serve_arm(1.0, 2.0, False) is None
            assert pull_state_rust.rust_token_revalidates("a", "a") is None
        assert any(log.get("reason") == "wheel_missing" for log in logs)

    def test_stale_wheel_degrades_naming_the_symbols(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch)
        with capture_logs() as logs:
            assert pull_state_rust.rust_memo_serve_arm(None, 2.0, True) is None
        # The registry is warn-once PER REASON — reset between the consults
        # so both symbol namings are observable.
        pull_state_rust.reset_for_tests()
        with capture_logs() as logs2:
            assert pull_state_rust.rust_token_revalidates(None, None) is None
        assert any(
            log.get("reason") == "wheel_stale" and log.get("symbol") == "memo_serve_arm"
            for log in logs
        )
        assert any(
            log.get("reason") == "wheel_stale" and log.get("symbol") == "token_revalidates"
            for log in logs2
        )

    def test_raising_kernels_degrade_kernel_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args: object) -> object:
            raise RuntimeError("kernel exploded")

        _install(monkeypatch, memo_serve_arm=_boom, token_revalidates=_boom)
        with capture_logs() as logs:
            assert pull_state_rust.rust_memo_serve_arm(1.0, 2.0, False) is None
            assert pull_state_rust.rust_token_revalidates("a", "b") is None
        assert sum(1 for log in logs if log.get("reason") == "kernel_error") >= 1

    @pytest.mark.parametrize("poison", [None, True, -1, 3, 2.0, "0"])
    def test_memo_serve_arm_refuse_poison_gate(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        _install(monkeypatch, memo_serve_arm=lambda *_a: poison)
        with capture_logs() as logs:
            assert pull_state_rust.rust_memo_serve_arm(1.0, 2.0, False) is None
        assert any(log.get("reason") == "kernel_invalid" for log in logs)

    @pytest.mark.parametrize("poison", [None, 1, 0, "true", 1.0])
    def test_token_revalidates_refuse_poison_gate(
        self, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        _install(monkeypatch, token_revalidates=lambda *_a: poison)
        with capture_logs() as logs:
            assert pull_state_rust.rust_token_revalidates("a", "a") is None
        assert any(log.get("reason") == "kernel_invalid" for log in logs)

    def test_valid_answers_pass_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            memo_serve_arm=lambda *_a: 2,
            token_revalidates=lambda *_a: True,
        )
        assert pull_state_rust.rust_memo_serve_arm(None, 0.0, False) == 2
        assert pull_state_rust.rust_token_revalidates("a", "a") is True


# ---------------------------------------------------------------------------
# The seam laws (flag ON, injected kernels).
# ---------------------------------------------------------------------------


class TestSeamLaw:
    def test_kernel_arm_is_authoritative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An in-vocabulary CACHED arm is taken even where the oracle would
        refresh (proves the seam consults the kernel, not the oracle)."""
        monkeypatch.setenv(FLAG_ENV, "1")
        _install(monkeypatch, memo_serve_arm=lambda *_a: 0)
        cache, calls = _cache()
        cache._probed_at = 1000.0  # stale — the oracle would take OWNER
        result = cache.get(Path("/nonexistent-lake"))
        assert result.source == "cached" and calls["guard"] == 0

    def test_owner_while_inflight_is_belted_to_follower(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The single-flight fail-safe belt: a poisoned OWNER arm while a
        refresh is in flight serves the follower answer and NEVER spawns a
        second refresh (the safety-critical corruption a kernel must never
        be able to cause)."""
        monkeypatch.setenv(FLAG_ENV, "1")
        _install(monkeypatch, memo_serve_arm=lambda *_a: 2)  # always OWNER
        cache, calls = _cache()
        cache._probed_at = 1000.0
        cache._inflight = True
        result = cache.get(Path("/nonexistent-lake"))
        assert result.source == "unverified"
        assert calls["guard"] == 0  # no second daemon
        assert cache._inflight is True  # the flag is the daemon's to clear

    def test_degrade_falls_back_to_the_oracle_conditions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(FLAG_ENV, "1")
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)  # wheel missing
        cache, calls = _cache()
        cache._probed_at = 1023.0
        assert cache.get(Path("/nonexistent-lake")).source == "cached"
        cache._probed_at = 1000.0
        assert cache.get(Path("/nonexistent-lake")).source == "live"
        assert calls["guard"] == 1

    def test_token_kernel_is_authoritative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A kernel ``True`` on DIFFERING tokens keeps the memo (the
        kernel-authoritative revalidate path)."""
        monkeypatch.setenv(FLAG_ENV, "1")
        _install(monkeypatch, token_revalidates=lambda *_a: True)
        cache, calls = _cache(token="t2")
        cache._token = "t1"
        cache._refresh_body(Path("/nonexistent-lake"))
        assert calls["read"] == 0  # revalidated — no re-read
        assert cache._token == "t1"  # memoised token kept

    def test_token_degrade_falls_back_to_the_oracle_compare(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(FLAG_ENV, "1")
        _install(monkeypatch, token_revalidates=lambda *_a: 7)  # poison → oracle
        cache, calls = _cache(token="t1")
        cache._token = "t1"
        cache._refresh_body(Path("/nonexistent-lake"))
        assert calls["read"] == 0  # the oracle compare revalidates equal tokens
        cache2, calls2 = _cache(token="t2")
        cache2._token = "t1"
        _install(monkeypatch, token_revalidates=lambda *_a: 7)
        cache2._refresh_body(Path("/nonexistent-lake"))
        assert calls2["read"] == 1  # and rereads differing ones

    def test_sweep_twin_survives_the_flag_with_a_missing_wheel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The SWEEP lane's ``SweepProgressCache`` inherits the base seam:
        flag ON + missing wheel degrades to the byte-identical oracle — the
        base seam can never break the twin (the fix-6b parity contract)."""
        monkeypatch.setenv(FLAG_ENV, "1")
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        from brutex.dashboard.sweep_progress_cache import SweepProgressCache

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


# ---------------------------------------------------------------------------
# Flag ON — the REAL wheel (capability-probed; skips on a stale wheel).
# ---------------------------------------------------------------------------


def _require_wave3h_wheel() -> None:
    wheel = pytest.importorskip("brutex_pull_state")
    if not hasattr(wheel, "memo_serve_arm"):
        pytest.skip("installed brutex_pull_state wheel predates Wave 3H")


def test_real_wheel_replays_the_frozen_golden_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The WHOLE frozen cache golden (serve + token tables) replays
    arm-for-arm through the LIVE module with the flag ON."""
    _require_wave3h_wheel()
    monkeypatch.setenv(FLAG_ENV, "1")
    live = json.loads(json.dumps(harness.compute_payload(), allow_nan=False))
    frozen = json.loads(JSON_FIXTURE.read_text(encoding="utf-8"))
    assert live["serve"] == frozen["serve"]
    assert live["token"] == frozen["token"]


def test_real_wheel_kernel_differential_over_the_frozen_tsv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every frozen TSV row through the REAL kernels via the facade."""
    _require_wave3h_wheel()
    monkeypatch.setenv(FLAG_ENV, "1")
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
