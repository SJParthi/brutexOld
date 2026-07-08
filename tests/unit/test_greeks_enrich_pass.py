"""Unit + property tests for :mod:`brutex.data.greeks_enrich_pass`.

DEC-CLI-GREEKS-ENRICH-001 — the COMMON fenced Greeks-enrichment pass +
wheel-free lake target discovery. Every branch of the module is pinned here:
the fence contract (happy path / whole-pass fencing / the two unfenced HOST
halts / the ``BaseException`` carve-out), the ``fno.greeks_enrich.done``
13-field telemetry, the injectable-logger seam, the ``has_option_candles``
pre-gate, the chain-union windowed lake discovery (zero Parquet reads — every
tree fixture is 0-byte ``touch`` files), the fence-safe deferred target
builder, and the fail-loud ``BRUTEX_CLI_GREEKS_ENRICH`` env gate (the
DEC-RAM-BUDGETS-001 env convention).

Tests that must import :mod:`brutex.data.greeks_enrich` (to monkeypatch the
runner's call-time import, the sibling ``test_pull_historical.py`` idiom) use
``pytest.importorskip("trading_core")``; discovery / env-gate / missing-wheel
tests are deliberately wheel-free.
"""

# Whitebox pins on _stored_months / _first_day_of_next_month use the
# precedented file-top pragma (see the pyproject [tool.pyright] note):
# pyright: reportPrivateUsage=none

from __future__ import annotations

import importlib
import json
import sys
from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from brutex.data import greeks_enrich_pass as gep
from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt
from brutex.data.greeks_enrich_pass import (
    CLI_GREEKS_ENRICH_ENV,
    GREEKS_NOT_RUN,
    DiscoveredOptionWindow,
    GreeksEnrichOutcome,
    discover_option_windows,
    has_option_candles,
    lake_target_builder,
    resolve_cli_greeks_enrich,
    run_greeks_enrich_pass,
)
from brutex.data.partition import MAX_PARTITION_YEAR, MIN_PARTITION_YEAR
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from structlog.typing import FilteringBoundLogger

# ────────────────────────────────────────────────────────────────────────
# Shared fixtures / helpers
# ────────────────────────────────────────────────────────────────────────

#: One NIFTY Jan-2024 CE leg (expiry 2024-01-25) — the workhorse contract.
_CE = "NSE-NIFTY-25Jan24-21000-CE"
#: The sibling PE of the SAME (exchange, underlying, expiry) chain.
_PE = "NSE-NIFTY-25Jan24-21500-PE"
#: The chain's future — must NEVER be enumerated by discovery.
_FUT = "NSE-NIFTY-25Jan24-FUT"
#: A second expiry of the same underlying (2024-02-29, a real leap day).
_CE_FEB = "NSE-NIFTY-29Feb24-21000-CE"
#: A different underlying on the same expiry.
_BANK_CE = "NSE-BANKNIFTY-25Jan24-45000-CE"
#: A hyphenated underlying — decoded intact by ``parse_fno_symbol``.
_BAJAJ_CE = "NSE-BAJAJ-AUTO-25Jan24-9000-CE"
#: A BSE leg for cross-exchange ordering.
_SENSEX_CE = "BSE-SENSEX-25Jan24-72000-CE"

_EXPIRY_JAN = date(2024, 1, 25)
_EXPIRY_FEB = date(2024, 2, 29)


def _make_contract(
    lake_root: Path,
    symbol: str,
    months: Sequence[tuple[int, int]],
    *,
    exchange: str = "NSE",
    interval: str = "1minute",
) -> Path:
    """Create ``bars/<exchange>/FNO/<symbol>/<interval>/<yyyy>/<mm>.parquet`` stubs.

    0-byte ``touch`` files suffice — discovery reads directory METADATA only,
    never a Parquet byte (the module's own zero-reads contract).
    """
    contract_dir = lake_root / "bars" / exchange / "FNO" / symbol
    for year, month in months:
        year_dir = contract_dir / interval / f"{year:04d}"
        year_dir.mkdir(parents=True, exist_ok=True)
        (year_dir / f"{month:02d}.parquet").touch()
    if not months:
        contract_dir.mkdir(parents=True, exist_ok=True)
    return contract_dir


def _only(rows: tuple[DiscoveredOptionWindow, ...]) -> DiscoveredOptionWindow:
    """Assert exactly one discovered row and return it.

    An explicit length assertion instead of 1-tuple unpacking:
    ``discover_option_windows`` has a literal ``return ()`` branch, so
    CodeQL's tuple-length inference flags ``(row,) = ...`` as an error-tier
    "Mismatch in multiple assignment" (PR #1236 CodeQL gate).
    """
    assert len(rows) == 1
    return rows[0]


def _report(**overrides: int) -> Any:
    """Build a real ``GreeksEnrichReport`` (wheel required — callers importorskip)."""
    from brutex.data import greeks_enrich as ge

    fields: dict[str, int] = {
        "enriched": 0,
        "already_current": 0,
        "futures_skipped": 0,
        "undecodable": 0,
        "no_bars": 0,
        "errored": 0,
        "bars_written": 0,
        "bars_solved": 0,
        "bars_null": 0,
        "bars_reused": 0,
        "spot_reads": 0,
        "spot_hits": 0,
        "bars_skipped": 0,
    }
    fields.update(overrides)
    return ge.GreeksEnrichReport(**fields)


#: The exact 13 counter fields the ``fno.greeks_enrich.done`` event carries,
#: in the report's field order — the telemetry contract of the moved fence.
_DONE_FIELDS: tuple[str, ...] = (
    "enriched",
    "already_current",
    "futures_skipped",
    "undecodable",
    "no_bars",
    "errored",
    "bars_written",
    "bars_solved",
    "bars_null",
    "bars_reused",
    "spot_reads",
    "spot_hits",
    "bars_skipped",
)


class _RecorderLogger:
    """A minimal injected-logger double capturing (method, event, kwargs)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.calls.append(("info", event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self.calls.append(("error", event, kwargs))


def _events(logs: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    return [entry for entry in logs if entry["event"] == name]


def _no_targets() -> list[Any]:
    """A typed empty target builder for fence tests."""
    return []


# ────────────────────────────────────────────────────────────────────────
# Outcome type + sentinel
# ────────────────────────────────────────────────────────────────────────


class TestGreeksEnrichOutcome:
    def test_greeks_not_run_sentinel_shape(self) -> None:
        # The "did not run" sentinel is honestly distinct from ran-clean (True)
        # and failed (False) — DEC-GREEKS-ENRICH-OBSERVE-001 semantics, moved.
        assert GreeksEnrichOutcome(ok=None, error=None, errored_targets=0) == GREEKS_NOT_RUN
        assert GREEKS_NOT_RUN.ok is None
        assert GREEKS_NOT_RUN.error is None
        assert GREEKS_NOT_RUN.errored_targets == 0

    def test_outcome_is_frozen(self) -> None:
        outcome = GreeksEnrichOutcome(ok=True, error=None, errored_targets=1)
        with pytest.raises(FrozenInstanceError):
            outcome.ok = False  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────────
# run_greeks_enrich_pass — the fence, moved statement-for-statement
# ────────────────────────────────────────────────────────────────────────


class TestRunGreeksEnrichPass:
    def test_happy_path_emits_done_with_exact_13_fields_and_ok_true(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One ``done`` event carrying each of the 13 report counters verbatim."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge

        # 13 DISTINCT values so a swapped/dropped field can never pass.
        report = _report(**{name: idx + 1 for idx, name in enumerate(_DONE_FIELDS)})
        rate_sentinel = object()
        received: dict[str, Any] = {}

        def _fake_enrich(*, targets: object, lake_root: Path, rate_source: object) -> Any:
            received["targets"] = targets
            received["lake_root"] = lake_root
            received["rate_source"] = rate_source
            return report

        monkeypatch.setattr(ge, "resolve_risk_free_source", lambda: rate_sentinel)
        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _fake_enrich)
        built: list[Any] = ["T1", "T2"]
        with capture_logs() as logs:
            outcome = run_greeks_enrich_pass(build_targets=lambda: built, lake_root=tmp_path)
        done = _events(logs, "fno.greeks_enrich.done")
        assert len(done) == 1
        payload = {k: v for k, v in done[0].items() if k not in {"event", "log_level"}}
        assert payload == {name: idx + 1 for idx, name in enumerate(_DONE_FIELDS)}
        assert _events(logs, "fno.greeks_enrich.error") == []
        assert outcome == GreeksEnrichOutcome(ok=True, error=None, errored_targets=report.errored)
        # The driver received the MATERIALISED builder list + the kwargs verbatim.
        assert received["targets"] == built
        assert isinstance(received["targets"], list)
        assert received["lake_root"] is tmp_path
        assert received["rate_source"] is rate_sentinel

    def test_rate_source_resolved_before_targets_built_before_driver(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The dashboard's resolve → build → drive order is preserved."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge

        order: list[str] = []

        def _rate() -> object:
            order.append("rate")
            return object()

        def _build() -> list[Any]:
            order.append("build")
            return []

        def _drive(*, targets: object, lake_root: Path, rate_source: object) -> Any:
            del targets, lake_root, rate_source
            order.append("drive")
            return _report()

        monkeypatch.setattr(ge, "resolve_risk_free_source", _rate)
        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _drive)
        outcome = run_greeks_enrich_pass(build_targets=_build, lake_root=tmp_path)
        assert order == ["rate", "build", "drive"]
        assert outcome.ok is True

    def test_build_targets_error_is_fenced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A builder error degrades to ``ok=False`` — the discovery seam is fenced."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge

        def _never(**_kwargs: object) -> Any:  # pragma: no cover - must not run
            raise AssertionError("driver must not be reached")

        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _never)

        def _boom() -> list[Any]:
            raise RuntimeError("boom")

        with capture_logs() as logs:
            outcome = run_greeks_enrich_pass(build_targets=_boom, lake_root=tmp_path)
        assert outcome == GreeksEnrichOutcome(
            ok=False, error="RuntimeError: boom", errored_targets=0
        )
        errors = _events(logs, "fno.greeks_enrich.error")
        assert len(errors) == 1
        assert errors[0]["error"] == "boom"
        assert _events(logs, "fno.greeks_enrich.done") == []

    def test_missing_wheel_import_error_is_fenced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A missing trading_core wheel hits the fence, never a crash (#1188 pin)."""
        monkeypatch.setitem(sys.modules, "brutex.data.greeks_enrich", None)
        built = False

        def _build() -> list[Any]:  # pragma: no cover - import fails first
            nonlocal built
            built = True
            return []

        with capture_logs() as logs:
            outcome = run_greeks_enrich_pass(build_targets=_build, lake_root=tmp_path)
        assert outcome.ok is False
        assert outcome.error is not None
        # ModuleNotFoundError is the ImportError subclass a missing module raises;
        # the fence stamps the CONCRETE type name (f"{type(exc).__name__}: …").
        assert outcome.error.startswith(("ModuleNotFoundError:", "ImportError:"))
        assert outcome.errored_targets == 0
        assert built is False  # the wheel import precedes the builder
        assert len(_events(logs, "fno.greeks_enrich.error")) == 1
        assert _events(logs, "fno.greeks_enrich.done") == []

    def test_risk_free_error_is_fenced_stubbed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A broken rate source fails the pass LOUDLY — and before target build."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge
        from brutex.options.risk_free import RiskFreeError

        def _broken() -> object:
            raise RiskFreeError("broken csv")

        monkeypatch.setattr(ge, "resolve_risk_free_source", _broken)
        built = False

        def _build() -> list[Any]:  # pragma: no cover - rate resolution fails first
            nonlocal built
            built = True
            return []

        with capture_logs() as logs:
            outcome = run_greeks_enrich_pass(build_targets=_build, lake_root=tmp_path)
        assert outcome == GreeksEnrichOutcome(
            ok=False, error="RiskFreeError: broken csv", errored_targets=0
        )
        assert built is False
        assert len(_events(logs, "fno.greeks_enrich.error")) == 1

    def test_risk_free_error_is_fenced_real_resolver(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Second leg through the REAL resolver: a nonexistent CSV path is fenced."""
        pytest.importorskip("trading_core")
        monkeypatch.setenv("BRUTEX_RISK_FREE_CSV", str(tmp_path / "missing.csv"))
        with capture_logs() as logs:
            outcome = run_greeks_enrich_pass(build_targets=_no_targets, lake_root=tmp_path)
        assert outcome.ok is False
        assert outcome.error is not None
        assert outcome.error.startswith("RiskFreeError:")
        assert len(_events(logs, "fno.greeks_enrich.error")) == 1

    def test_driver_error_is_fenced(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge

        def _bad(**_kwargs: object) -> Any:
            raise ValueError("bad targets")

        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _bad)
        with capture_logs() as logs:
            outcome = run_greeks_enrich_pass(build_targets=_no_targets, lake_root=tmp_path)
        assert outcome == GreeksEnrichOutcome(
            ok=False, error="ValueError: bad targets", errored_targets=0
        )
        assert len(_events(logs, "fno.greeks_enrich.error")) == 1

    @pytest.mark.parametrize(
        "halt",
        [LakeWriteHalt("disk_full"), GreeksEnrichPoolBroken("worker died abruptly")],
        ids=["lake_write_halt", "pool_broken"],
    )
    def test_host_halt_reraises_unfenced_from_driver(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, halt: Exception
    ) -> None:
        """The two pull-wide HOST halts re-raise past the fence — zero events."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge

        def _halt(**_kwargs: object) -> Any:
            raise halt

        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _halt)
        with capture_logs() as logs, pytest.raises(type(halt)):
            run_greeks_enrich_pass(build_targets=_no_targets, lake_root=tmp_path)
        assert _events(logs, "fno.greeks_enrich.error") == []
        assert _events(logs, "fno.greeks_enrich.done") == []

    @pytest.mark.parametrize(
        "halt",
        [LakeWriteHalt("out_of_memory"), GreeksEnrichPoolBroken("pool died")],
        ids=["lake_write_halt", "pool_broken"],
    )
    def test_host_halt_reraises_unfenced_from_builder(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, halt: Exception
    ) -> None:
        """A halt raised by the BUILDER leg (discovery-time) also re-raises."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge

        def _never(**_kwargs: object) -> Any:  # pragma: no cover - must not run
            raise AssertionError("driver must not be reached")

        monkeypatch.setattr(ge, "enrich_greeks_for_targets", _never)

        def _build() -> list[Any]:
            raise halt

        with capture_logs() as logs, pytest.raises(type(halt)):
            run_greeks_enrich_pass(build_targets=_build, lake_root=tmp_path)
        assert _events(logs, "fno.greeks_enrich.error") == []

    def test_keyboard_interrupt_propagates_unfenced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``BaseException`` is NOT fenced — a Ctrl-C propagates raw (deliberate)."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge

        monkeypatch.setattr(ge, "resolve_risk_free_source", object)

        def _interrupt() -> list[Any]:
            raise KeyboardInterrupt

        with capture_logs() as logs, pytest.raises(KeyboardInterrupt):
            run_greeks_enrich_pass(build_targets=_interrupt, lake_root=tmp_path)
        assert _events(logs, "fno.greeks_enrich.error") == []
        assert _events(logs, "fno.greeks_enrich.done") == []

    def test_injected_logger_receives_done_event(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The ``log`` seam: an injected logger gets the events, the module one none."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge

        monkeypatch.setattr(ge, "resolve_risk_free_source", object)
        monkeypatch.setattr(ge, "enrich_greeks_for_targets", lambda **_kwargs: _report(errored=4))
        recorder = _RecorderLogger()
        with capture_logs() as logs:
            outcome = run_greeks_enrich_pass(
                build_targets=_no_targets,
                lake_root=tmp_path,
                log=cast("FilteringBoundLogger", recorder),
            )
        assert outcome == GreeksEnrichOutcome(ok=True, error=None, errored_targets=4)
        assert [(m, e) for m, e, _ in recorder.calls] == [("info", "fno.greeks_enrich.done")]
        assert recorder.calls[0][2]["errored"] == 4
        # The injected logger is NOT the structlog-configured one — nothing global.
        assert _events(logs, "fno.greeks_enrich.done") == []

    def test_injected_logger_receives_error_event(self, tmp_path: Path) -> None:
        """The injected-logger seam on the ERROR leg (wheel-free via poisoning)."""
        recorder = _RecorderLogger()
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "brutex.data.greeks_enrich", None)
            outcome = run_greeks_enrich_pass(
                build_targets=_no_targets,
                lake_root=tmp_path,
                log=cast("FilteringBoundLogger", recorder),
            )
        assert outcome.ok is False
        assert [(m, e) for m, e, _ in recorder.calls] == [("error", "fno.greeks_enrich.error")]


# ────────────────────────────────────────────────────────────────────────
# run_greeks_enrich_pass — the shared Greeks-overlay freshness marker
# (DEC-GREEKS-BULK-REENRICH-001 fix round 8, refuter-confirmed)
# ────────────────────────────────────────────────────────────────────────


class TestRevisionMarkerTouch:
    """A bar-writing pass moves the cross-process ``lake_revision`` token.

    Refuter-confirmed (fix round 8): the merge-absorbed
    DEC-CLI-GREEKS-ENRICH-001 pass — ``brutex enrich-greeks`` + the ``brutex
    fetch`` post-ledger tail, both through :func:`run_greeks_enrich_pass` —
    rewrote option iv/delta overlays while EVERY component of the composite
    ``lake_revision`` token (ledger stat | sweeps digest | Greeks marker)
    stayed byte-identical, so a concurrently-running dashboard kept serving
    the PRE-enrich Greeks from its generation-keyed caches indefinitely — the
    exact staleness class fix round 2 closed for the sibling ``brutex
    greeks-backfill``. The pass now touches the SHARED marker
    (:mod:`brutex.data.greeks_revision`) whenever it wrote bars.
    """

    def test_bar_writing_pass_touches_the_marker_and_moves_the_lake_revision_token(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pytest.importorskip("trading_core")
        from brutex.dashboard.lake_revision import lake_revision
        from brutex.data import greeks_enrich as ge
        from brutex.data.greeks_revision import revision_marker_path

        monkeypatch.setattr(ge, "resolve_risk_free_source", object)
        monkeypatch.setattr(
            ge,
            "enrich_greeks_for_targets",
            lambda **_kwargs: _report(enriched=1, bars_written=3, bars_solved=3),
        )
        before = lake_revision(tmp_path)
        outcome = run_greeks_enrich_pass(build_targets=_no_targets, lake_root=tmp_path)
        assert outcome.ok is True
        marker = revision_marker_path(tmp_path)
        assert marker.is_file()
        payload = json.loads(marker.read_text(encoding="utf-8"))
        assert payload["writer"] == "greeks_enrich_pass"
        assert payload["bars_written"] == 3
        after = lake_revision(tmp_path)
        assert before is not None and after is not None
        assert after != before  # the dashboard probe sees the write on its next poll

    def test_zero_write_pass_fabricates_no_freshness_signal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An all-reused / no-bars pass (``bars_written == 0``) leaves NO marker."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge
        from brutex.data.greeks_revision import revision_marker_path

        monkeypatch.setattr(ge, "resolve_risk_free_source", object)
        monkeypatch.setattr(
            ge, "enrich_greeks_for_targets", lambda **_kwargs: _report(already_current=2)
        )
        outcome = run_greeks_enrich_pass(build_targets=_no_targets, lake_root=tmp_path)
        assert outcome.ok is True
        assert not revision_marker_path(tmp_path).exists()

    def test_marker_failure_is_best_effort_and_never_flips_a_completed_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A stray FILE at the sidecar dir warns ONCE and the pass stays ok=True
        (the marker is a freshness signal, never data — the backfill posture)."""
        pytest.importorskip("trading_core")
        from brutex.data import greeks_enrich as ge
        from brutex.data.greeks_revision import GREEKS_SIDECAR_DIR_NAME

        monkeypatch.setattr(ge, "resolve_risk_free_source", object)
        monkeypatch.setattr(
            ge, "enrich_greeks_for_targets", lambda **_kwargs: _report(bars_written=5)
        )
        (tmp_path / GREEKS_SIDECAR_DIR_NAME).write_text("stray", encoding="utf-8")
        with capture_logs() as logs:
            outcome = run_greeks_enrich_pass(build_targets=_no_targets, lake_root=tmp_path)
        assert outcome == GreeksEnrichOutcome(ok=True, error=None, errored_targets=0)
        assert len(_events(logs, "fno.greeks_enrich.revision_marker_failed")) == 1
        assert _events(logs, "fno.greeks_enrich.error") == []


# ────────────────────────────────────────────────────────────────────────
# has_option_candles — the spot-only pre-gate
# ────────────────────────────────────────────────────────────────────────


class TestHasOptionCandles:
    def test_missing_bars_dir_is_false(self, tmp_path: Path) -> None:
        assert has_option_candles(tmp_path) is False

    def test_bars_as_file_is_false(self, tmp_path: Path) -> None:
        (tmp_path / "bars").touch()
        assert has_option_candles(tmp_path) is False

    def test_empty_bars_dir_is_false(self, tmp_path: Path) -> None:
        (tmp_path / "bars").mkdir()
        assert has_option_candles(tmp_path) is False

    def test_stray_file_in_bars_is_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "bars").mkdir()
        (tmp_path / "bars" / "README.txt").touch()
        assert has_option_candles(tmp_path) is False

    def test_exchange_without_fno_is_false(self, tmp_path: Path) -> None:
        # The dominant CLI case — a pure-spot lake (CASH segment only).
        (tmp_path / "bars" / "NSE" / "CASH" / "NSE-NIFTY").mkdir(parents=True)
        assert has_option_candles(tmp_path) is False

    def test_empty_fno_dir_is_false(self, tmp_path: Path) -> None:
        (tmp_path / "bars" / "NSE" / "FNO").mkdir(parents=True)
        assert has_option_candles(tmp_path) is False

    def test_one_contract_dir_is_true(self, tmp_path: Path) -> None:
        _make_contract(tmp_path, _CE, [(2024, 1)])
        assert has_option_candles(tmp_path) is True

    def test_second_exchange_carries_the_only_fno(self, tmp_path: Path) -> None:
        # First exchange has no FNO → the loop continues to the one that does.
        (tmp_path / "bars" / "AAA" / "CASH").mkdir(parents=True)
        _make_contract(tmp_path, _SENSEX_CE, [(2024, 1)], exchange="BSE")
        assert has_option_candles(tmp_path) is True

    def test_oserror_returns_false(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """The never-raises pin: an unreadable tree honestly reads False."""
        _make_contract(tmp_path, _CE, [(2024, 1)])

        def _boom(self: Path) -> Iterator[Path]:
            raise OSError("unreadable tree")

        monkeypatch.setattr(Path, "iterdir", _boom)
        assert has_option_candles(tmp_path) is False


# ────────────────────────────────────────────────────────────────────────
# _stored_months + _first_day_of_next_month — the month-span primitives
# ────────────────────────────────────────────────────────────────────────


class TestStoredMonths:
    def test_missing_interval_dir_is_empty(self, tmp_path: Path) -> None:
        assert gep._stored_months(tmp_path / "1minute") == frozenset()

    def test_junk_year_entries_skipped(self, tmp_path: Path) -> None:
        interval = tmp_path / "1minute"
        interval.mkdir()
        (interval / "2024").touch()  # a FILE named like a year — not a dir
        (interval / "202").mkdir()  # wrong width
        (interval / "20A4").mkdir()  # non-digit
        below = interval / f"{MIN_PARTITION_YEAR - 1:04d}"  # below the writer window
        below.mkdir()
        (below / "01.parquet").touch()  # a REAL month file — the bound must exclude it
        above = interval / f"{MAX_PARTITION_YEAR + 1:04d}"  # above the writer window
        above.mkdir()
        (above / "01.parquet").touch()  # a REAL month file — the bound must exclude it
        good = interval / "2025"
        good.mkdir()
        (good / "01.parquet").touch()
        assert gep._stored_months(interval) == frozenset({(2025, 1)})

    def test_out_of_window_year_with_month_files_is_excluded(self, tmp_path: Path) -> None:
        """Regression: the partition-year bound has EFFECT, not just coverage.

        Review round-2 (finding 1): the original junk-year fixtures created
        the out-of-window year dirs EMPTY, so a mutant deleting the
        ``MIN_PARTITION_YEAR <= year <= MAX_PARTITION_YEAR`` bound survived —
        admitting the dirs changed nothing observable. This test seeds REAL
        month files into out-of-window years (including the hostile
        ``9999/12.parquet``, which without the bound would become the stored
        maximum and make the month-after span endpoint ``date(10000, 1, 1)``
        unconstructible), so the frozenset equality kills the mutant.
        """
        interval = tmp_path / "1minute"
        interval.mkdir()
        below = interval / f"{MIN_PARTITION_YEAR - 1:04d}"
        below.mkdir()
        (below / "03.parquet").touch()
        above = interval / f"{MAX_PARTITION_YEAR + 1:04d}"
        above.mkdir()
        (above / "06.parquet").touch()
        hostile = interval / "9999"
        hostile.mkdir()
        (hostile / "12.parquet").touch()
        good = interval / "2025"
        good.mkdir()
        (good / "07.parquet").touch()
        assert gep._stored_months(interval) == frozenset({(2025, 7)})

    def test_junk_month_entries_skipped(self, tmp_path: Path) -> None:
        year_dir = tmp_path / "1minute" / "2024"
        year_dir.mkdir(parents=True)
        (year_dir / "notes.txt").touch()  # wrong suffix
        (year_dir / "1.parquet").touch()  # wrong stem width
        (year_dir / "0a.parquet").touch()  # non-digit stem
        (year_dir / "00.parquet").touch()  # month below 1
        (year_dir / "13.parquet").touch()  # month above 12
        (year_dir / "03.parquet").touch()
        (tmp_path / "1minute" / "2026").mkdir()  # a valid-looking EMPTY year dir
        assert gep._stored_months(tmp_path / "1minute") == frozenset({(2024, 3)})

    def test_non_ascii_digit_year_dirs_skipped_not_raised(self, tmp_path: Path) -> None:
        """Unicode-digit year dirs are JUNK to skip, never a discovery crash.

        Review round-1 regression (finding 3): ``"²024"`` (superscript two)
        passes ``str.isdigit()`` but ``int()`` raises ``ValueError`` — the old
        gate let it through and the raise killed the WHOLE pass; and the
        Arabic-Indic ``"٢٠٢٤"`` passes BOTH ``isdigit()`` and ``int()`` (as
        2024) yet is not a writer-canonical ASCII name — both must skip
        silently while the sibling canonical year survives.
        """
        interval = tmp_path / "1minute"
        interval.mkdir()
        (interval / "²024").mkdir()  # isdigit() True, int() ValueError
        (interval / "٢٠٢٤").mkdir()  # isdigit() True AND int() == 2024 — still junk
        good = interval / "2025"
        good.mkdir()
        (good / "01.parquet").touch()
        assert gep._stored_months(interval) == frozenset({(2025, 1)})

    def test_non_ascii_digit_month_stems_skipped_not_raised(self, tmp_path: Path) -> None:
        """Unicode-digit month stems skip silently too (the mirror of the year gate)."""
        year_dir = tmp_path / "1minute" / "2024"
        year_dir.mkdir(parents=True)
        (year_dir / "0¹.parquet").touch()  # isdigit() True, int() ValueError
        (year_dir / "٠٣.parquet").touch()  # isdigit() True AND int() == 3 — still junk
        (year_dir / "04.parquet").touch()
        assert gep._stored_months(tmp_path / "1minute") == frozenset({(2024, 4)})

    def test_first_day_of_next_month_rollover(self) -> None:
        assert gep._first_day_of_next_month(2024, 12) == date(2025, 1, 1)
        assert gep._first_day_of_next_month(2024, 1) == date(2024, 2, 1)


# ────────────────────────────────────────────────────────────────────────
# discover_option_windows — the bounded wheel-free lake walk
# ────────────────────────────────────────────────────────────────────────


class TestDiscoverOptionWindows:
    def test_missing_lake_returns_empty(self, tmp_path: Path) -> None:
        assert discover_option_windows(tmp_path) == ()

    def test_stray_entries_and_fno_less_exchange_skipped(self, tmp_path: Path) -> None:
        bars = tmp_path / "bars"
        bars.mkdir()
        (bars / "stray.txt").touch()  # non-dir in bars/
        (bars / "NSE" / "CASH").mkdir(parents=True)  # exchange without FNO/
        assert discover_option_windows(tmp_path) == ()

    def test_ce_pe_discovered_fut_undecodable_and_stray_skipped(self, tmp_path: Path) -> None:
        _make_contract(tmp_path, _CE, [(2024, 1)])
        _make_contract(tmp_path, _PE, [(2024, 1)])
        _make_contract(tmp_path, _FUT, [(2024, 1)])  # FUT never enumerated
        _make_contract(tmp_path, "junkdir", [(2024, 1)])  # undecodable name
        (tmp_path / "bars" / "NSE" / "FNO" / "stray.parquet").touch()  # non-dir
        rows = discover_option_windows(tmp_path)
        assert [row.symbol for row in rows] == [_CE, _PE]
        assert all(row.underlying == "NIFTY" for row in rows)
        assert all(row.expiry == _EXPIRY_JAN for row in rows)

    def test_unicode_digit_year_dir_never_kills_discovery(self, tmp_path: Path) -> None:
        """THE finding-3 whole-pass-kill regression: a hostile year-dir name in
        ONE contract must not raise out of discovery — the valid sibling
        contract is still discovered (junk-tolerance contract, Golden Rule #1)."""
        _make_contract(tmp_path, _CE, [(2024, 1)])
        poisoned = _make_contract(tmp_path, _PE, [])
        bad_year = poisoned / "1minute" / "²024"
        bad_year.mkdir(parents=True)
        (bad_year / "01.parquet").touch()
        rows = discover_option_windows(tmp_path)
        assert [row.symbol for row in rows] == [_CE]

    def test_contract_with_no_months_skipped(self, tmp_path: Path) -> None:
        # An empty 1minute/ tree AND a contract dir with no 1minute/ at all.
        empty = _make_contract(tmp_path, _CE, [])
        (empty / "1minute").mkdir()
        _make_contract(tmp_path, _PE, [])
        assert discover_option_windows(tmp_path) == ()

    def test_non_1minute_intervals_ignored(self, tmp_path: Path) -> None:
        # A derived-TF partition carries no Greeks overlay — only 1minute counts.
        _make_contract(tmp_path, _CE, [(2024, 1)], interval="5minute")
        assert discover_option_windows(tmp_path) == ()

    def test_chain_union_window_shared_across_strikes(self, tmp_path: Path) -> None:
        """The spot-join-memo shape pin: one window per (exchange, underlying, expiry)."""
        _make_contract(tmp_path, _CE, [(2024, 1)])
        _make_contract(tmp_path, _PE, [(2024, 3)])  # same chain, disjoint months
        rows = discover_option_windows(tmp_path)
        assert len(rows) == 2
        assert {(row.start, row.end) for row in rows} == {(date(2024, 1, 1), date(2024, 4, 1))}

    def test_two_expiries_same_underlying_distinct_windows(self, tmp_path: Path) -> None:
        _make_contract(tmp_path, _CE, [(2024, 1)])
        _make_contract(tmp_path, _CE_FEB, [(2024, 2)])
        rows = discover_option_windows(tmp_path)
        by_symbol = {row.symbol: row for row in rows}
        assert by_symbol[_CE].start == date(2024, 1, 1)
        assert by_symbol[_CE].end == date(2024, 2, 1)
        assert by_symbol[_CE_FEB].start == date(2024, 2, 1)
        assert by_symbol[_CE_FEB].end == date(2024, 3, 1)
        assert by_symbol[_CE].expiry == _EXPIRY_JAN
        assert by_symbol[_CE_FEB].expiry == _EXPIRY_FEB

    def test_year_rollover_window(self, tmp_path: Path) -> None:
        symbol = "NSE-NIFTY-30Jan25-21000-CE"
        _make_contract(tmp_path, symbol, [(2024, 12), (2025, 1)])
        row = _only(discover_option_windows(tmp_path))
        assert row.start == date(2024, 12, 1)
        assert row.end == date(2025, 2, 1)

    def test_clamp_start_inside_narrows(self, tmp_path: Path) -> None:
        _make_contract(tmp_path, _CE, [(2024, 1), (2024, 2)])
        row = _only(discover_option_windows(tmp_path, start=date(2024, 1, 15)))
        assert row.start == date(2024, 1, 15)
        assert row.end == date(2024, 3, 1)

    def test_clamp_start_before_span_is_noop(self, tmp_path: Path) -> None:
        _make_contract(tmp_path, _CE, [(2024, 1)])
        row = _only(discover_option_windows(tmp_path, start=date(2023, 12, 1)))
        assert row.start == date(2024, 1, 1)
        assert row.end == date(2024, 2, 1)

    def test_clamp_end_inside_narrows(self, tmp_path: Path) -> None:
        _make_contract(tmp_path, _CE, [(2024, 1), (2024, 2)])
        row = _only(discover_option_windows(tmp_path, end=date(2024, 2, 10)))
        assert row.start == date(2024, 1, 1)
        assert row.end == date(2024, 2, 10)

    def test_clamp_end_after_span_is_noop(self, tmp_path: Path) -> None:
        _make_contract(tmp_path, _CE, [(2024, 1)])
        row = _only(discover_option_windows(tmp_path, end=date(2024, 6, 1)))
        assert row.start == date(2024, 1, 1)
        assert row.end == date(2024, 2, 1)

    def test_clamp_both_straddling_narrows_both_ends(self, tmp_path: Path) -> None:
        _make_contract(tmp_path, _CE, [(2024, 1), (2024, 2), (2024, 3)])
        row = _only(
            discover_option_windows(tmp_path, start=date(2024, 1, 10), end=date(2024, 3, 20))
        )
        assert row.start == date(2024, 1, 10)
        assert row.end == date(2024, 3, 20)

    def test_disjoint_clamp_drops_whole_group(self, tmp_path: Path) -> None:
        _make_contract(tmp_path, _CE, [(2024, 1)])
        _make_contract(tmp_path, _PE, [(2024, 1)])
        assert discover_option_windows(tmp_path, start=date(2024, 6, 1), end=date(2024, 7, 1)) == ()

    def test_underlying_token_filter(self, tmp_path: Path) -> None:
        _make_contract(tmp_path, _CE, [(2024, 1)])
        _make_contract(tmp_path, _BANK_CE, [(2024, 1)])
        _make_contract(tmp_path, _BAJAJ_CE, [(2024, 1)])
        # In-set kept, out-of-set dropped.
        nifty_only = discover_option_windows(tmp_path, underlyings=frozenset({"NIFTY"}))
        assert [row.symbol for row in nifty_only] == [_CE]
        # A hyphenated underlying is decoded + matched INTACT.
        bajaj_only = discover_option_windows(tmp_path, underlyings=frozenset({"BAJAJ-AUTO"}))
        assert [row.symbol for row in bajaj_only] == [_BAJAJ_CE]
        assert bajaj_only[0].underlying == "BAJAJ-AUTO"
        # None = all underlyings.
        assert len(discover_option_windows(tmp_path)) == 3

    def test_deterministic_order(self, tmp_path: Path) -> None:
        """Sorted by (exchange, underlying, expiry, symbol) — not creation order."""
        # Deliberately created in a scrambled order.
        _make_contract(tmp_path, _CE_FEB, [(2024, 2)])
        _make_contract(tmp_path, _PE, [(2024, 1)])
        _make_contract(tmp_path, _SENSEX_CE, [(2024, 1)], exchange="BSE")
        _make_contract(tmp_path, _CE, [(2024, 1)])
        _make_contract(tmp_path, _BANK_CE, [(2024, 1)])
        rows = discover_option_windows(tmp_path)
        assert [(row.exchange, row.symbol) for row in rows] == [
            ("BSE", _SENSEX_CE),
            ("NSE", _BANK_CE),
            ("NSE", _CE),
            ("NSE", _PE),
            ("NSE", _CE_FEB),
        ]
        assert rows == discover_option_windows(tmp_path)  # stable across walks

    def test_exchange_taken_from_tree_dir_name(self, tmp_path: Path) -> None:
        """A drift-keyed partition is enriched where it physically SITS."""
        _make_contract(tmp_path, _CE, [(2024, 1)], exchange="WEIRD")
        row = _only(discover_option_windows(tmp_path))
        assert row.exchange == "WEIRD"
        assert row.symbol == _CE

    @settings(max_examples=30, deadline=None)
    @given(
        months=st.sets(
            st.tuples(st.integers(2024, 2025), st.integers(1, 12)),
            min_size=1,
            max_size=5,
        ),
        clamp_start=st.none() | st.dates(date(2023, 6, 1), date(2026, 6, 1)),
        clamp_end=st.none() | st.dates(date(2023, 6, 1), date(2026, 6, 1)),
    )
    def test_window_property(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        months: set[tuple[int, int]],
        clamp_start: date | None,
        clamp_end: date | None,
    ) -> None:
        """Random month sets + clamps: the window law holds; a clamp never widens."""
        lake = tmp_path_factory.mktemp("prop-lake")
        symbol = "NSE-NIFTY-30Jan25-21000-CE"
        _make_contract(lake, symbol, sorted(months))
        rows = discover_option_windows(lake, start=clamp_start, end=clamp_end)
        min_year, min_month = min(months)
        max_year, max_month = max(months)
        natural_start = date(min_year, min_month, 1)
        natural_end = gep._first_day_of_next_month(max_year, max_month)
        expected_start = max(natural_start, clamp_start) if clamp_start else natural_start
        expected_end = min(natural_end, clamp_end) if clamp_end else natural_end
        if expected_start >= expected_end:
            assert rows == ()
        else:
            row = _only(rows)
            assert (row.start, row.end) == (expected_start, expected_end)
            assert row.start < row.end  # never an empty window
            # A clamp only ever NARROWS the natural month span.
            assert row.start >= natural_start
            assert row.end <= natural_end
            if clamp_start is not None:
                assert row.start >= clamp_start
            if clamp_end is not None:
                assert row.end <= clamp_end


# ────────────────────────────────────────────────────────────────────────
# lake_target_builder — deferred, fence-safe target construction
# ────────────────────────────────────────────────────────────────────────


class TestLakeTargetBuilder:
    def test_builder_construction_is_wheel_free_calling_needs_wheel(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The dry-run enabler: CONSTRUCTING the builder never touches the wheel."""
        monkeypatch.setitem(sys.modules, "brutex.data.greeks_enrich", None)
        builder = lake_target_builder(tmp_path)  # must not raise
        with pytest.raises(ImportError):
            builder()  # the wheel import lives INSIDE the closure (→ the fence)

    def test_builder_maps_windows_to_targets_field_for_field(self, tmp_path: Path) -> None:
        pytest.importorskip("trading_core")
        from brutex.data.greeks_enrich import GreeksEnrichTarget

        _make_contract(tmp_path, _CE, [(2024, 1)])
        _make_contract(tmp_path, _PE, [(2024, 3)])
        windows = discover_option_windows(tmp_path)
        targets = lake_target_builder(tmp_path)()
        assert list(targets) == [
            GreeksEnrichTarget(
                exchange=window.exchange,
                symbol=window.symbol,
                start=window.start,
                end=window.end,
            )
            for window in windows
        ]

    def test_builder_threads_filter_and_clamp_kwargs(self, tmp_path: Path) -> None:
        pytest.importorskip("trading_core")
        _make_contract(tmp_path, _CE, [(2024, 1), (2024, 2)])
        _make_contract(tmp_path, _BANK_CE, [(2024, 1)])
        builder = lake_target_builder(
            tmp_path,
            underlyings=frozenset({"NIFTY"}),
            start=date(2024, 1, 10),
            end=date(2024, 2, 5),
        )
        (target,) = builder()
        assert target.symbol == _CE
        assert target.start == date(2024, 1, 10)
        assert target.end == date(2024, 2, 5)

    def test_builder_empty_lake_yields_no_targets(self, tmp_path: Path) -> None:
        pytest.importorskip("trading_core")
        assert list(lake_target_builder(tmp_path)()) == []


# ────────────────────────────────────────────────────────────────────────
# resolve_cli_greeks_enrich — the fail-loud env gate
# ────────────────────────────────────────────────────────────────────────


class TestResolveCliGreeksEnrich:
    @pytest.mark.parametrize(
        "env",
        [
            {},  # unset — default-ON
            {CLI_GREEKS_ENRICH_ENV: ""},
            {CLI_GREEKS_ENRICH_ENV: "   "},  # blank strips to unset
            {CLI_GREEKS_ENRICH_ENV: "1"},
            {CLI_GREEKS_ENRICH_ENV: "on"},
            {CLI_GREEKS_ENRICH_ENV: "ON"},
            {CLI_GREEKS_ENRICH_ENV: "true"},
            {CLI_GREEKS_ENRICH_ENV: " True "},
        ],
        ids=["unset", "empty", "blank", "1", "on", "ON", "true", "True-padded"],
    )
    def test_on_tokens_enable(self, env: dict[str, str]) -> None:
        assert resolve_cli_greeks_enrich(env) is True

    @pytest.mark.parametrize(
        "raw",
        ["0", "off", "OFF", "false", " False "],
        ids=["0", "off", "OFF", "false", "False-padded"],
    )
    def test_off_tokens_disable(self, raw: str) -> None:
        assert resolve_cli_greeks_enrich({CLI_GREEKS_ENRICH_ENV: raw}) is False

    @pytest.mark.parametrize("raw", ["banana", "yes", "2", "enable"])
    def test_junk_value_fails_loud(self, raw: str) -> None:
        # The DEC-RAM-BUDGETS-001 convention: junk is never silently coerced.
        with pytest.raises(ValueError, match="BRUTEX_CLI_GREEKS_ENRICH"):
            resolve_cli_greeks_enrich({CLI_GREEKS_ENRICH_ENV: raw})

    def test_junk_message_names_the_accepted_values(self) -> None:
        with pytest.raises(ValueError, match="'0', 'off', 'false'"):
            resolve_cli_greeks_enrich({CLI_GREEKS_ENRICH_ENV: "banana"})

    def test_none_env_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CLI_GREEKS_ENRICH_ENV, "off")
        assert resolve_cli_greeks_enrich() is False
        monkeypatch.delenv(CLI_GREEKS_ENRICH_ENV)
        assert resolve_cli_greeks_enrich() is True


# ────────────────────────────────────────────────────────────────────────
# Module hygiene — wheel-free at import (the #1188 discipline)
# ────────────────────────────────────────────────────────────────────────


class TestModuleHygiene:
    def test_module_imports_wheel_free(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fresh import succeeds with the wheel-heavy chain POISONED."""
        for name in ("trading_core", "brutex.options.greeks", "brutex.data.greeks_enrich"):
            monkeypatch.setitem(sys.modules, name, None)
        monkeypatch.delitem(sys.modules, "brutex.data.greeks_enrich_pass")
        fresh = importlib.import_module("brutex.data.greeks_enrich_pass")
        assert fresh is not gep  # genuinely re-executed, not the cached module
        assert callable(fresh.run_greeks_enrich_pass)
        assert callable(fresh.discover_option_windows)

    def test_window_row_is_frozen(self) -> None:
        row = DiscoveredOptionWindow(
            exchange="NSE",
            symbol=_CE,
            underlying="NIFTY",
            expiry=_EXPIRY_JAN,
            start=date(2024, 1, 1),
            end=date(2024, 2, 1),
        )
        with pytest.raises(FrozenInstanceError):
            row.symbol = "other"  # type: ignore[misc]
