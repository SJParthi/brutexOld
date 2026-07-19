"""Unit suite for the ``lake_rust_merge`` facade (ADR-0020 PR-3).

DEC-LAKE-RUST-MERGE-DEDUP-001 (MIGRATION.md #1911 — allocator-final).
Crate-FREE and polars-light: a fake ``brutex_lake`` module + an injected
side-channel ``decision_ipc`` drive every guard / burn-in / refuse-poison /
degrade path of :mod:`brutex.data.lake_rust_merge` standalone (the 100%
line+branch gate). Real-crate end-to-end parity lives in
``tests/golden/test_lake_rust_merge_parity.py``; the ``_merge_write`` seam
wiring lives in ``tests/unit/test_parquet_writer_rust_merge.py``.

Whitebox (DEC-P0-017): exercises the private latches/helpers directly.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import sys
import types
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.data import lake_rust_merge as lrm
from brutex.data.models import CandleBar, FnoGreeksBar
from brutex.data.parquet_writer import BarConflict, ParquetWriter
from structlog.testing import capture_logs


@pytest.fixture(autouse=True)
def _fresh_process_view() -> Iterator[None]:
    """Reset the latch + burn-in budget + warn memo around every test."""
    lrm.reset_lake_rust_merge_for_tests()
    yield
    lrm.reset_lake_rust_merge_for_tests()


def _bar(minute: int, close: str = "100.50", *, volume: int = 1_000) -> CandleBar:
    return CandleBar(
        timestamp=datetime(2024, 1, 1, 4, minute, tzinfo=UTC),
        open=Decimal("100.25"),
        high=Decimal("101.00"),
        low=Decimal("99.75"),
        close=Decimal(close),
        volume=volume,
        open_interest=None,
    )


def _greeks_bar(minute: int, iv: float | None = 0.21) -> FnoGreeksBar:
    return FnoGreeksBar(
        timestamp=datetime(2024, 1, 1, 4, minute, tzinfo=UTC),
        open=Decimal("100.25"),
        high=Decimal("101.00"),
        low=Decimal("99.75"),
        close=Decimal("100.50"),
        volume=1_000,
        open_interest=25,
        iv=iv,
    )


def _stored(*bars: CandleBar) -> dict[datetime, CandleBar]:
    return {b.timestamp: b for b in bars}


# The verbatim Python fold (the production oracle) — bound off a throwaway
# writer so instance-level monkeypatch semantics stay identical to the seam.
def _python_fold(tmp_path_factory: pytest.TempPathFactory) -> lrm.PythonFold:
    writer = ParquetWriter(lake_root=tmp_path_factory.mktemp("oracle-lake"))
    return writer._merge_fold


@pytest.fixture
def python_fold(tmp_path_factory: pytest.TempPathFactory) -> lrm.PythonFold:
    return _python_fold(tmp_path_factory)


def _reference_actions(
    stored: list[CandleBar], batch: list[CandleBar], keep_first: bool
) -> list[int]:
    """A correct action stream — the fold law replayed over the side-channel."""
    writer = ParquetWriter(lake_root=Path("/nonexistent-never-touched"))
    winners: dict[datetime, CandleBar] = {}
    for bar in stored:
        winners.setdefault(bar.timestamp, bar)
    actions: list[int] = []
    for bar in batch:
        prev = winners.get(bar.timestamp)
        if prev is None:
            winners[bar.timestamp] = bar
            actions.append(lrm.ACTION_INSERT)
        elif not writer._bar_values_equal(prev, bar):
            if keep_first:
                actions.append(lrm.ACTION_CONFLICT)
            else:
                winners[bar.timestamp] = bar
                actions.append(lrm.ACTION_OVERWRITE)
        else:
            from brutex.data.parquet_writer import _overlay_equal

            if _overlay_equal(prev, bar) or keep_first:
                actions.append(lrm.ACTION_SKIP)
            else:
                winners[bar.timestamp] = bar
                actions.append(lrm.ACTION_OVERWRITE)
    return actions


class _SideChannel:
    """Injected ``decision_ipc`` stand-in: records the two bar lists in call
    order (stored first, batch second — the facade's documented order) so a
    fake crate can decide over REAL bars without any polars IPC parsing."""

    def __init__(self) -> None:
        self.calls: list[list[CandleBar]] = []

    def __call__(self, bars: list[CandleBar]) -> bytes:
        self.calls.append(list(bars))
        return f"payload-{len(self.calls)}".encode()


def _fake_crate(
    *,
    pinned: str | Callable[[], str] = "",
    decide: Any = None,
    zstd: str | Callable[[], str] = "1.5.7",
    channel: _SideChannel | None = None,
) -> types.ModuleType:
    """Build a fake ``brutex_lake`` module with injectable behaviour.

    ``pinned=""`` defaults to the installed polars version (the version
    guard passes); ``decide=None`` computes CORRECT actions off *channel*.
    """
    module = types.ModuleType("brutex_lake")
    if pinned == "":
        import polars as pl

        resolved_pin: Any = lambda: pl.__version__  # noqa: E731 - test stub
    elif callable(pinned):
        resolved_pin = pinned
    else:
        resolved_pin = lambda: pinned  # noqa: E731 - test stub
    module.pinned_polars_version = resolved_pin  # type: ignore[attr-defined]
    if callable(zstd):
        module.linked_zstd_version = zstd  # type: ignore[attr-defined]
    else:
        module.linked_zstd_version = lambda: zstd  # type: ignore[attr-defined]
    if decide is None:
        assert channel is not None
        bound_channel = channel

        def _correct_decide(stored_ipc: bytes, batch_ipc: bytes, keep_first: bool) -> list[int]:
            stored, batch = bound_channel.calls[-2], bound_channel.calls[-1]
            return _reference_actions(stored, batch, keep_first)

        decide = _correct_decide
    module.merge_decision_from_ipc = decide  # type: ignore[attr-defined]
    return module


def _install_crate(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType | None) -> None:
    monkeypatch.setitem(sys.modules, "brutex_lake", module)


def _engage(
    monkeypatch: pytest.MonkeyPatch,
    *,
    decide: Any = None,
    pinned: str | Callable[[], str] = "",
    zstd: str | Callable[[], str] = "1.5.7",
) -> _SideChannel:
    """Arm the flag + install a fake crate; return the side channel."""
    channel = _SideChannel()
    _install_crate(
        monkeypatch, _fake_crate(pinned=pinned, decide=decide, zstd=zstd, channel=channel)
    )
    monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")
    return channel


def _run(
    python_fold: lrm.PythonFold,
    channel: _SideChannel,
    merged: dict[datetime, CandleBar],
    new_bars: list[CandleBar],
    *,
    merge_mode: str = "keep-first",
) -> tuple[dict[datetime, CandleBar], int, bool, list[BarConflict]]:
    return lrm.merge_fold(
        merged,
        new_bars,
        merge_mode=merge_mode,
        python_fold=python_fold,
        decision_ipc=channel,
    )


# ---------------------------------------------------------------------------
# The flag law
# ---------------------------------------------------------------------------


class TestFlag:
    def test_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(lrm.LAKE_RUST_MERGE_ENV, raising=False)
        assert lrm.lake_rust_merge_enabled() is False

    @pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
    def test_truthy(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, raw)
        assert lrm.lake_rust_merge_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "No", " off "])
    def test_falsy(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, raw)
        assert lrm.lake_rust_merge_enabled() is False

    def test_whitespace_only_is_silent_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "   ")
        with capture_logs() as logs:
            assert lrm.lake_rust_merge_enabled() is False
        assert logs == []

    def test_junk_is_loud_off_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "ture")
        with capture_logs() as logs:
            assert lrm.lake_rust_merge_enabled() is False
            assert lrm.lake_rust_merge_enabled() is False
        fallbacks = [e for e in logs if e["event"] == "lake_rust_merge.fallback"]
        assert len(fallbacks) == 1
        assert fallbacks[0]["reason"] == "flag_unrecognised"

    def test_action_vocabulary_pinned(self) -> None:
        # MUST mirror rust/brutex-lake/src/merge.rs ACTION_* (its own
        # `action_constants_are_the_pinned_vocabulary` cargo test).
        assert (
            lrm.ACTION_SKIP,
            lrm.ACTION_INSERT,
            lrm.ACTION_OVERWRITE,
            lrm.ACTION_CONFLICT,
        ) == (0, 1, 2, 3)


# ---------------------------------------------------------------------------
# Guard layers — every failure serves the verbatim Python fold IN PLACE
# ---------------------------------------------------------------------------


class TestGuards:
    def test_latched_process_runs_python_fold_in_place(
        self, monkeypatch: pytest.MonkeyPatch, python_fold: lrm.PythonFold
    ) -> None:
        channel = _engage(monkeypatch)
        lrm._disable("crate_unavailable")
        merged = _stored(_bar(0))
        out, added, rewrite, conflicts = _run(python_fold, channel, merged, [_bar(1)])
        assert out is merged  # in-place — the flag-OFF shape
        assert (added, rewrite, conflicts) == (1, False, [])
        assert channel.calls == []  # no IPC built on a latched process

    def test_absent_crate_latches_and_serves_python(
        self, monkeypatch: pytest.MonkeyPatch, python_fold: lrm.PythonFold
    ) -> None:
        _install_crate(monkeypatch, None)  # import poison → ImportError
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")
        merged = _stored(_bar(0))
        with capture_logs() as logs:
            out, added, _, _ = lrm.merge_fold(
                merged,
                [_bar(1)],
                merge_mode="keep-first",
                python_fold=python_fold,
                decision_ipc=_SideChannel(),
            )
        assert out is merged and added == 1
        assert lrm.rust_merge_disabled()
        events = [e for e in logs if e["event"] == "lake_rust_merge.rust_path_disabled"]
        assert events and events[0]["reason"] == "crate_unavailable"

    def test_pre_pr3_wheel_missing_merge_fn_latches(
        self, monkeypatch: pytest.MonkeyPatch, python_fold: lrm.PythonFold
    ) -> None:
        module = _fake_crate(channel=_SideChannel())
        del module.merge_decision_from_ipc  # pyright: ignore[reportAttributeAccessIssue]
        _install_crate(monkeypatch, module)
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")
        merged = _stored(_bar(0))
        out, added, _, _ = lrm.merge_fold(
            merged,
            [_bar(1)],
            merge_mode="keep-first",
            python_fold=python_fold,
            decision_ipc=_SideChannel(),
        )
        assert out is merged and added == 1
        assert lrm.rust_merge_disabled()

    def test_metadata_raise_latches(
        self, monkeypatch: pytest.MonkeyPatch, python_fold: lrm.PythonFold
    ) -> None:
        def boom() -> str:
            raise RuntimeError("stale install")

        channel = _SideChannel()
        _install_crate(monkeypatch, _fake_crate(pinned=boom, channel=channel))
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")
        merged = _stored(_bar(0))
        out, added, _, _ = _run(python_fold, channel, merged, [_bar(1)])
        assert out is merged and added == 1
        assert lrm.rust_merge_disabled()

    def test_metadata_memoryerror_reraises(
        self, monkeypatch: pytest.MonkeyPatch, python_fold: lrm.PythonFold
    ) -> None:
        def boom() -> str:
            raise MemoryError("oom in metadata")

        channel = _SideChannel()
        _install_crate(monkeypatch, _fake_crate(pinned=boom, channel=channel))
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")
        with pytest.raises(MemoryError):
            _run(python_fold, channel, _stored(_bar(0)), [_bar(1)])

    def test_version_mismatch_latches(
        self, monkeypatch: pytest.MonkeyPatch, python_fold: lrm.PythonFold
    ) -> None:
        channel = _SideChannel()
        _install_crate(monkeypatch, _fake_crate(pinned="0.0.0", channel=channel))
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")
        merged = _stored(_bar(0))
        with capture_logs() as logs:
            out, added, _, _ = _run(python_fold, channel, merged, [_bar(1)])
        assert out is merged and added == 1
        events = [e for e in logs if e["event"] == "lake_rust_merge.rust_path_disabled"]
        assert events and events[0]["reason"] == "polars_version_mismatch"

    def test_polars_import_failure_degrades(
        self, monkeypatch: pytest.MonkeyPatch, python_fold: lrm.PythonFold
    ) -> None:
        channel = _engage(monkeypatch)
        monkeypatch.setitem(sys.modules, "polars", None)  # import poison
        merged = _stored(_bar(0))
        with capture_logs() as logs:
            out, added, _, _ = _run(python_fold, channel, merged, [_bar(1)])
        assert out is merged and added == 1
        assert not lrm.rust_merge_disabled()  # degrade, not a latch
        fallbacks = [e for e in logs if e["event"] == "lake_rust_merge.fallback"]
        assert fallbacks and fallbacks[0]["reason"] == "merge_error"

    def test_zstd_metadata_raise_after_decision_serves_python(
        self, monkeypatch: pytest.MonkeyPatch, python_fold: lrm.PythonFold
    ) -> None:
        def boom() -> str:
            raise RuntimeError("zstd probe broken")

        channel = _SideChannel()
        _install_crate(monkeypatch, _fake_crate(zstd=boom, channel=channel))
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")
        merged = _stored(_bar(0))
        out, added, _, _ = _run(python_fold, channel, merged, [_bar(1)])
        assert out is merged and added == 1
        assert lrm.rust_merge_disabled()


# ---------------------------------------------------------------------------
# Degrade / escape / panic-belt on the crate call
# ---------------------------------------------------------------------------


class TestCrateCallFailures:
    @pytest.mark.parametrize(
        "exc", [ValueError("bad ipc"), RuntimeError("panic mapped"), TypeError("conv")]
    )
    def test_degrade_errors_serve_python_warn_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
        python_fold: lrm.PythonFold,
        exc: Exception,
    ) -> None:
        def boom(*_a: object) -> list[int]:
            raise exc

        channel = _engage(monkeypatch, decide=boom)
        merged = _stored(_bar(0))
        with capture_logs() as logs:
            out, added, _, _ = _run(python_fold, channel, merged, [_bar(1)])
            _run(python_fold, channel, merged, [_bar(2)])
        assert out is merged and added == 1
        assert not lrm.rust_merge_disabled()
        fallbacks = [e for e in logs if e["event"] == "lake_rust_merge.fallback"]
        assert len(fallbacks) == 1 and fallbacks[0]["reason"] == "merge_error"

    def test_polars_error_from_decision_ipc_degrades(
        self, monkeypatch: pytest.MonkeyPatch, python_fold: lrm.PythonFold
    ) -> None:
        import polars.exceptions as pl_exc

        _engage(monkeypatch)

        def poisoned_ipc(bars: list[CandleBar]) -> bytes:
            raise pl_exc.ComputeError("frame build failed")

        merged = _stored(_bar(0))
        out, added, _, _ = lrm.merge_fold(
            merged,
            [_bar(1)],
            merge_mode="keep-first",
            python_fold=python_fold,
            decision_ipc=poisoned_ipc,
        )
        assert out is merged and added == 1

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_reraises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        python_fold: lrm.PythonFold,
        exc_type: type[BaseException],
    ) -> None:
        def boom(*_a: object) -> list[int]:
            raise exc_type("escape")

        channel = _engage(monkeypatch, decide=boom)
        with pytest.raises(exc_type):
            _run(python_fold, channel, _stored(_bar(0)), [_bar(1)])
        assert not lrm.rust_merge_disabled()

    def test_escaped_panic_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch, python_fold: lrm.PythonFold
    ) -> None:
        import polars.exceptions as pl_exc

        def boom(*_a: object) -> list[int]:
            raise pl_exc.PanicException("simulated escaped panic")

        channel = _engage(monkeypatch, decide=boom)
        merged = _stored(_bar(0))
        with capture_logs() as logs:
            out, added, _, _ = _run(python_fold, channel, merged, [_bar(1)])
        assert out is merged and added == 1
        events = [e for e in logs if e["event"] == "lake_rust_merge.rust_path_disabled"]
        assert events and events[0]["reason"] == "crate_panic"


# ---------------------------------------------------------------------------
# Refuse-poison gate — a poisoned decision can only degrade, never corrupt
# ---------------------------------------------------------------------------


class TestRefusePoison:
    @pytest.mark.parametrize(
        ("decide", "label"),
        [
            (lambda *_a: b"not a list", "non-list"),
            (lambda *_a: [0, 0], "wrong length"),
            (lambda *_a: ["1"], "non-int action"),
            (lambda *_a: [7], "unknown code"),
        ],
    )
    def test_malformed_payload_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        python_fold: lrm.PythonFold,
        decide: Any,
        label: str,
    ) -> None:
        channel = _engage(monkeypatch, decide=decide)
        merged = _stored(_bar(0))
        with capture_logs() as logs:
            out, added, rewrite, conflicts = _run(python_fold, channel, merged, [_bar(1)])
        assert out is merged, label
        assert (added, rewrite, conflicts) == (1, False, [])
        fallbacks = [e for e in logs if e["event"] == "lake_rust_merge.fallback"]
        assert fallbacks and fallbacks[0]["reason"] == "decision_refused"
        assert not lrm.rust_merge_disabled()

    @pytest.mark.parametrize(
        ("actions", "merge_mode", "batch_minute", "label"),
        [
            ([lrm.ACTION_INSERT], "keep-first", 0, "INSERT at a stored ts"),
            ([lrm.ACTION_OVERWRITE], "keep-first", 0, "OVERWRITE under keep-first"),
            ([lrm.ACTION_OVERWRITE], "latest-wins", 1, "OVERWRITE at an absent ts"),
            ([lrm.ACTION_CONFLICT], "latest-wins", 0, "CONFLICT under latest-wins"),
            ([lrm.ACTION_CONFLICT], "keep-first", 1, "CONFLICT at an absent ts"),
            ([lrm.ACTION_SKIP], "keep-first", 1, "SKIP at an absent ts"),
        ],
    )
    def test_law_contradicting_actions_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        python_fold: lrm.PythonFold,
        actions: list[int],
        merge_mode: str,
        batch_minute: int,
        label: str,
    ) -> None:
        channel = _engage(monkeypatch, decide=lambda *_a: list(actions))
        merged = _stored(_bar(0))
        snapshot = dict(merged)
        out, added, rewrite, conflicts = _run(
            python_fold, channel, merged, [_bar(batch_minute, volume=9_999)], merge_mode=merge_mode
        )
        assert out is merged, label
        # The caller's dict was never half-mutated by the poisoned decision:
        # the outcome is exactly the Python fold's.
        oracle = dict(snapshot)
        o_added, o_rewrite, o_conflicts = python_fold(
            oracle, [_bar(batch_minute, volume=9_999)], merge_mode=merge_mode
        )
        assert (added, rewrite) == (o_added, o_rewrite)
        assert [(c.timestamp, c.stored, c.vendor) for c in conflicts] == [
            (c.timestamp, c.stored, c.vendor) for c in o_conflicts
        ]
        assert out == oracle


# ---------------------------------------------------------------------------
# Engaged path — correct decisions, burn-in, shape gate, autorevert
# ---------------------------------------------------------------------------


class TestEngaged:
    def test_engaged_matches_python_fold_and_logs_once(
        self, monkeypatch: pytest.MonkeyPatch, python_fold: lrm.PythonFold
    ) -> None:
        channel = _engage(monkeypatch)
        stored_bar = _bar(0)
        merged = _stored(stored_bar)
        new = [_bar(0), _bar(0, volume=9_999), _bar(1)]
        with capture_logs() as logs:
            out, added, rewrite, conflicts = _run(python_fold, channel, merged, new)
        assert out is not merged  # engaged path works on a copy
        assert (added, rewrite) == (1, False)
        assert len(conflicts) == 1
        assert conflicts[0].stored is stored_bar and conflicts[0].vendor is new[1]
        assert out[new[2].timestamp] is new[2]
        assert [e["event"] for e in logs].count("lake_rust_merge.engaged") == 1
        # Second engaged call: no second engaged INFO.
        with capture_logs() as logs2:
            _run(python_fold, channel, dict(out), [_bar(2)])
        assert "lake_rust_merge.engaged" not in [e["event"] for e in logs2]

    def test_latest_wins_overwrite(
        self, monkeypatch: pytest.MonkeyPatch, python_fold: lrm.PythonFold
    ) -> None:
        channel = _engage(monkeypatch)
        merged = _stored(_bar(0))
        restate = _bar(0, volume=9_999)
        out, added, rewrite, conflicts = _run(
            python_fold, channel, merged, [restate], merge_mode="latest-wins"
        )
        assert (added, rewrite, conflicts) == (0, True, [])
        assert out[restate.timestamp] is restate

    def test_burn_in_budget_decrements_and_completes(
        self, monkeypatch: pytest.MonkeyPatch, python_fold: lrm.PythonFold
    ) -> None:
        channel = _engage(monkeypatch)
        lrm.reset_lake_rust_merge_for_tests(remaining=1)
        with capture_logs() as logs:
            _run(python_fold, channel, _stored(_bar(0)), [_bar(1)])
        assert lrm._BURN_IN_REMAINING[0] == 0
        assert "lake_rust_merge.burn_in_complete" in [e["event"] for e in logs]

    def test_shape_gate_compares_new_shape_after_budget_spent(
        self, monkeypatch: pytest.MonkeyPatch, python_fold: lrm.PythonFold
    ) -> None:
        channel = _engage(monkeypatch)
        lrm.reset_lake_rust_merge_for_tests(remaining=1)
        _run(python_fold, channel, _stored(_bar(0)), [_bar(1)])  # spends the budget
        assert lrm._BURN_IN_REMAINING[0] == 0

        calls: list[str] = []
        real_fold = python_fold

        def spying_fold(
            merged: dict[datetime, CandleBar],
            new_bars: list[CandleBar],
            *,
            merge_mode: str,
        ) -> tuple[int, bool, list[BarConflict]]:
            calls.append(merge_mode)
            return real_fold(merged, new_bars, merge_mode=merge_mode)

        # A NEW shape (latest-wins) with zero budget still burn-in-compares…
        _run(
            python_fold=spying_fold,
            channel=channel,
            merged=_stored(_bar(0)),
            new_bars=[_bar(2)],
            merge_mode="latest-wins",
        )
        assert calls == ["latest-wins"]
        # …and the SAME shape thereafter is steady state (no oracle call).
        _run(
            python_fold=spying_fold,
            channel=channel,
            merged=_stored(_bar(0)),
            new_bars=[_bar(3)],
            merge_mode="latest-wins",
        )
        assert calls == ["latest-wins"]

    def test_steady_state_never_calls_python_fold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        channel = _engage(monkeypatch)
        lrm.reset_lake_rust_merge_for_tests(remaining=0)

        def bomb_fold(
            merged: dict[datetime, CandleBar],
            new_bars: list[CandleBar],
            *,
            merge_mode: str,
        ) -> tuple[int, bool, list[BarConflict]]:
            raise AssertionError("oracle must not run in steady state")

        _out, added, rewrite, conflicts = lrm.merge_fold(
            _stored(_bar(0)),
            [_bar(1)],
            merge_mode="keep-first",
            python_fold=bomb_fold,
            decision_ipc=channel,
        )
        assert (added, rewrite, conflicts) == (1, False, [])

    def test_burn_in_mismatch_autoreverts_and_ships_python_result(
        self, monkeypatch: pytest.MonkeyPatch, python_fold: lrm.PythonFold
    ) -> None:
        # A subtly-wrong crate: flips a genuine conflict into a silent skip.
        def wrong(stored_ipc: bytes, batch_ipc: bytes, keep_first: bool) -> list[int]:
            return [lrm.ACTION_SKIP]

        channel = _engage(monkeypatch, decide=wrong)
        stored_bar = _bar(0)
        merged = _stored(stored_bar)
        vendor = _bar(0, volume=9_999)
        with capture_logs() as logs:
            out, added, rewrite, conflicts = _run(python_fold, channel, merged, [vendor])
        # The PYTHON result shipped: the conflict is recorded.
        assert len(conflicts) == 1
        assert conflicts[0].stored is stored_bar and conflicts[0].vendor is vendor
        assert (added, rewrite) == (0, False)
        assert out[stored_bar.timestamp] is stored_bar
        assert lrm.rust_merge_disabled()
        errors = [e for e in logs if e["event"] == "lake_rust_merge.merge_autoreverted"]
        assert len(errors) == 1 and errors[0]["reason"] == "burn_in_fold_mismatch"
        # The NEXT call short-circuits on the latch (python fold, no IPC).
        before = len(channel.calls)
        _run(python_fold, channel, dict(out), [_bar(5)])
        assert len(channel.calls) == before

    def test_shape_token_covers_greeks_arms(self) -> None:
        plain = _stored(_bar(0))
        enriched = _stored(_greeks_bar(0))
        assert lrm._shape_token(plain, [_bar(1)], "keep-first") == (
            "keep-first|stored_greeks=False|batch_greeks=False"
        )
        assert lrm._shape_token(enriched, [_greeks_bar(1)], "latest-wins") == (
            "latest-wins|stored_greeks=True|batch_greeks=True"
        )


# ---------------------------------------------------------------------------
# _fold_results_equal — every mismatch arm
# ---------------------------------------------------------------------------


class TestFoldResultsEqual:
    def _base(
        self,
    ) -> tuple[dict[datetime, CandleBar], int, bool, list[BarConflict]]:
        stored = _bar(0)
        vendor = _bar(0, volume=9_999)
        return (
            _stored(stored),
            1,
            False,
            [BarConflict(timestamp=vendor.timestamp, stored=stored, vendor=vendor)],
        )

    def test_equal(self) -> None:
        merged, added, rewrite, conflicts = self._base()
        a = (dict(merged), added, rewrite, list(conflicts))
        b = (dict(merged), added, rewrite, list(conflicts))
        assert lrm._fold_results_equal(a, b)

    def test_added_mismatch(self) -> None:
        merged, added, rewrite, conflicts = self._base()
        assert not lrm._fold_results_equal(
            (dict(merged), added + 1, rewrite, list(conflicts)),
            (dict(merged), added, rewrite, list(conflicts)),
        )

    def test_rewrite_mismatch(self) -> None:
        merged, added, _rewrite, conflicts = self._base()
        assert not lrm._fold_results_equal(
            (dict(merged), added, True, list(conflicts)),
            (dict(merged), added, False, list(conflicts)),
        )

    def test_conflict_count_mismatch(self) -> None:
        merged, added, rewrite, conflicts = self._base()
        assert not lrm._fold_results_equal(
            (dict(merged), added, rewrite, []),
            (dict(merged), added, rewrite, list(conflicts)),
        )

    def test_key_set_mismatch(self) -> None:
        merged, added, rewrite, conflicts = self._base()
        extra = dict(merged)
        extra[_bar(9).timestamp] = _bar(9)
        assert not lrm._fold_results_equal(
            (extra, added, rewrite, list(conflicts)),
            (dict(merged), added, rewrite, list(conflicts)),
        )

    def test_winner_identity_mismatch(self) -> None:
        merged, added, rewrite, conflicts = self._base()
        twin = dict(merged)
        ts = next(iter(twin))
        twin[ts] = _bar(0)  # equal VALUE, different OBJECT — must fail (identity law)
        assert not lrm._fold_results_equal(
            (twin, added, rewrite, list(conflicts)),
            (dict(merged), added, rewrite, list(conflicts)),
        )

    def test_conflict_identity_mismatch(self) -> None:
        merged, added, rewrite, conflicts = self._base()
        other = BarConflict(
            timestamp=conflicts[0].timestamp,
            stored=conflicts[0].stored,
            vendor=_bar(0, volume=9_999),  # equal value, different object
        )
        assert not lrm._fold_results_equal(
            (dict(merged), added, rewrite, [other]),
            (dict(merged), added, rewrite, list(conflicts)),
        )


# ---------------------------------------------------------------------------
# Latch/warn plumbing
# ---------------------------------------------------------------------------


class TestLatchPlumbing:
    def test_disable_is_idempotent(self) -> None:
        with capture_logs() as logs:
            lrm._disable("crate_unavailable")
            lrm._disable("crate_panic")  # second reason ignored — latch set
        assert lrm._DISABLED_REASON[0] == "crate_unavailable"
        events = [e for e in logs if e["event"] == "lake_rust_merge.rust_path_disabled"]
        assert len(events) == 1

    def test_reset_restores_fresh_view(self) -> None:
        lrm._disable("crate_unavailable")
        lrm._BURN_IN_SHAPES_SEEN.add("token")
        lrm.reset_lake_rust_merge_for_tests()
        assert not lrm.rust_merge_disabled()
        assert lrm._BURN_IN_REMAINING[0] == vars(lrm)["BURN_IN_SAMPLES"]
        assert not lrm._BURN_IN_SHAPES_SEEN
        assert lrm._BURN_IN_SHAPE_GATE[0] is True
        lrm.reset_lake_rust_merge_for_tests(remaining=0)
        assert lrm._BURN_IN_SHAPE_GATE[0] is False

    def test_shared_helpers_are_imported_not_copied(self) -> None:
        # §11 COMMON: the flag classifier + catch sets are lake_rust_render's.
        from brutex.data import lake_rust_render as lrr

        namespace = vars(lrm)
        assert namespace["env_flag_tristate"] is lrr.env_flag_tristate
        assert namespace["degrade_errors"] is lrr.degrade_errors
        assert namespace["panic_belt_errors"] is lrr.panic_belt_errors
        assert namespace["BURN_IN_SAMPLES"] is lrr.BURN_IN_SAMPLES
