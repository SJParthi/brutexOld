"""Unit gate for :mod:`brutex.feed_rust` (DEC-FEED-RUST-CORE-001) — 100%
line+branch STANDALONE.

Covers the default-OFF flag matrix (Pattern A + the junk warn-once courtesy),
the lazy kernel resolution degrade tokens (``wheel_missing`` /
``wheel_stale`` — driven via ``sys.modules`` poisoning so they run with the
real wheel installed), EVERY refusal arm of the refuse-poison
``validate_scan`` gate, the warn-once registry semantics, the leaf-import AST
pin, the ``_TRUTHY``/``_FALSY`` mirror pin against ``sweep_lookahead``'s
vocabulary, and — wheel-gated — the integers-only FFI shape + the
length-mismatch ``ValueError`` edge.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from brutex import feed_rust
from brutex.feed_rust import (
    FEED_RUST_ENV,
    FEED_RUST_FALLBACK_EVENT,
    FEED_RUST_FLAG_EVENT,
    feed_rust_enabled,
    reset_for_tests,
    resolve_feed_kernel,
    validate_scan,
    warn_degrade_once,
)
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from brutex.feed_rust import ScanResult


@pytest.fixture(autouse=True)
def _fresh_warn_registry() -> None:
    """Isolate the per-process warn-once registry between tests."""
    reset_for_tests()


# ── Flag matrix (Pattern A: default OFF, only explicit truthy enables) ──────


class TestFeedRustEnabled:
    """``feed_rust_enabled`` — the default-OFF opt-in law."""

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", " on ", "  1  ", "On"])
    def test_explicit_truthy_enables(self, raw: str) -> None:
        assert feed_rust_enabled(raw) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "FALSE", " Off ", ""])
    def test_falsy_and_empty_disable(self, raw: str) -> None:
        assert feed_rust_enabled(raw) is False

    def test_unset_env_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(FEED_RUST_ENV, raising=False)
        assert feed_rust_enabled() is False

    def test_env_read_when_raw_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FEED_RUST_ENV, "1")
        assert feed_rust_enabled() is True
        monkeypatch.setenv(FEED_RUST_ENV, "off")
        assert feed_rust_enabled() is False

    def test_junk_disables_and_warns_exactly_once(self) -> None:
        with capture_logs() as logs:
            assert feed_rust_enabled("bananas") is False
            assert feed_rust_enabled("bananas") is False
        junk = [rec for rec in logs if rec["event"] == FEED_RUST_FLAG_EVENT]
        assert len(junk) == 1
        assert junk[0]["value"] == "bananas"
        assert junk[0]["env"] == FEED_RUST_ENV
        assert junk[0]["log_level"] == "warning"

    def test_whitespace_only_is_empty_not_junk(self) -> None:
        with capture_logs() as logs:
            assert feed_rust_enabled("   ") is False
        assert logs == []

    def test_reset_rearms_the_junk_warn(self) -> None:
        with capture_logs() as logs:
            feed_rust_enabled("junk-a")
            reset_for_tests()
            feed_rust_enabled("junk-b")
        junk = [rec for rec in logs if rec["event"] == FEED_RUST_FLAG_EVENT]
        assert [rec["value"] for rec in junk] == ["junk-a", "junk-b"]


# ── Warn-once degrade registry ──────────────────────────────────────────────


class TestWarnDegradeOnce:
    """``warn_degrade_once`` — one event per reason per process."""

    def test_warns_once_per_reason(self) -> None:
        with capture_logs() as logs:
            warn_degrade_once("schema_mismatch", n_rows=3)
            warn_degrade_once("schema_mismatch", n_rows=9)
            warn_degrade_once("null_column", n_rows=3)
        events = [rec for rec in logs if rec["event"] == FEED_RUST_FALLBACK_EVENT]
        assert [rec["reason"] for rec in events] == ["schema_mismatch", "null_column"]
        assert events[0]["n_rows"] == 3  # the FIRST occurrence's fields stick

    def test_kernel_error_logs_at_error_level(self) -> None:
        with capture_logs() as logs:
            warn_degrade_once("kernel_error", n_rows=1, error="ValueError('boom')")
            warn_degrade_once("wheel_missing")
        assert logs[0]["log_level"] == "error"
        assert logs[1]["log_level"] == "warning"

    def test_reset_for_tests_clears_the_registry(self) -> None:
        with capture_logs() as logs:
            warn_degrade_once("wheel_missing")
            reset_for_tests()
            warn_degrade_once("wheel_missing")
        assert len([rec for rec in logs if rec["event"] == FEED_RUST_FALLBACK_EVENT]) == 2


# ── Kernel resolution ───────────────────────────────────────────────────────


class TestResolveFeedKernel:
    """``resolve_feed_kernel`` — wheel present / missing / stale."""

    def test_wheel_missing_degrades_warn_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Poisoning sys.modules with None makes `import brutex_feed` raise
        # ImportError even when the real wheel IS installed.
        monkeypatch.setitem(sys.modules, "brutex_feed", None)
        with capture_logs() as logs:
            assert resolve_feed_kernel() is None
            assert resolve_feed_kernel() is None
        events = [rec for rec in logs if rec["event"] == FEED_RUST_FALLBACK_EVENT]
        assert [rec["reason"] for rec in events] == ["wheel_missing"]

    def test_wheel_stale_degrades_warn_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stale = types.ModuleType("brutex_feed")  # importable, no scan_bars
        monkeypatch.setitem(sys.modules, "brutex_feed", stale)
        with capture_logs() as logs:
            assert resolve_feed_kernel() is None
        events = [rec for rec in logs if rec["event"] == FEED_RUST_FALLBACK_EVENT]
        assert [rec["reason"] for rec in events] == ["wheel_stale"]

    def test_real_wheel_resolves_scan_bars(self) -> None:
        brutex_feed = pytest.importorskip(
            "brutex_feed", reason="brutex_feed wheel not installed — build via maturin"
        )
        kernel = resolve_feed_kernel()
        assert kernel is brutex_feed.scan_bars


# ── validate_scan — the refuse-poison gate, every arm ───────────────────────


def _ok_result() -> ScanResult:
    """A well-formed clean-scan result over n_rows=4 (one finish event)."""
    return ([0, 2, 3], [(4, 0, 1, 0, 1)], None)


class TestValidateScan:
    """``validate_scan`` — structural laws + every named refusal arm."""

    def _refused(self, result: object, n_rows: int, detail: str) -> None:
        """Assert *result* is refused with ONE kernel_invalid warn naming *detail*."""
        reset_for_tests()
        with capture_logs() as logs:
            assert validate_scan(result, n_rows) is False
        events = [rec for rec in logs if rec["event"] == FEED_RUST_FALLBACK_EVENT]
        assert len(events) == 1
        assert events[0]["reason"] == "kernel_invalid"
        assert events[0]["detail"] == detail

    def test_valid_clean_scan(self) -> None:
        with capture_logs() as logs:
            assert validate_scan(_ok_result(), 4) is True
        assert logs == []

    def test_valid_with_stop(self) -> None:
        # Kept + warn strictly before the stop row; no finish event.
        assert validate_scan(([0, 1], [(1, 0, 2, 0, 0 + 1)], (3, 2)), 4) is True

    def test_valid_empty(self) -> None:
        assert validate_scan(([], [], None), 0) is True

    def test_valid_boundary_flush_at_kept_row(self) -> None:
        assert validate_scan(([0, 2], [(2, 0, 1, 0, 1)], None), 3) is True

    def test_result_shape_not_tuple(self) -> None:
        self._refused([[], [], None], 4, "result_shape")

    def test_result_shape_wrong_arity(self) -> None:
        self._refused(([], []), 4, "result_shape")

    def test_column_shape_kept_not_list(self) -> None:
        self._refused(((0, 1), [], None), 4, "column_shape")

    def test_column_shape_warns_not_list(self) -> None:
        self._refused(([], ((4, 0, 1, 0, 1),), None), 4, "column_shape")

    def test_stop_shape(self) -> None:
        self._refused(([], [], [2, 1]), 4, "stop_shape")

    def test_stop_shape_wrong_arity(self) -> None:
        self._refused(([], [], (2,)), 4, "stop_shape")

    def test_stop_types(self) -> None:
        self._refused(([], [], (2.0, 1)), 4, "stop_types")
        self._refused(([], [], (2, "close")), 4, "stop_types")

    def test_stop_range_row(self) -> None:
        self._refused(([], [], (4, 0)), 4, "stop_range")
        self._refused(([], [], (-1, 0)), 4, "stop_range")

    def test_stop_range_field(self) -> None:
        self._refused(([], [], (0, 4)), 4, "stop_range")
        self._refused(([], [], (0, -1)), 4, "stop_range")

    def test_kept_type(self) -> None:
        self._refused(([0, 1.0], [], None), 4, "kept_type")

    def test_kept_order_not_ascending(self) -> None:
        self._refused(([1, 1], [], None), 4, "kept_order")
        self._refused(([2, 1], [], None), 4, "kept_order")

    def test_kept_negative_refused(self) -> None:
        # A negative index would silently WRAP Python list selection.
        self._refused(([-1, 0], [], None), 4, "kept_order")

    def test_kept_out_of_range(self) -> None:
        self._refused(([4], [], None), 4, "kept_order")

    def test_kept_at_or_after_stop_row(self) -> None:
        self._refused(([2], [], (2, 0)), 4, "kept_order")

    def test_event_shape(self) -> None:
        self._refused(([], [[4, 0, 1, 0, 1]], None), 4, "event_shape")
        self._refused(([], [(4, 0, 1, 0)], None), 4, "event_shape")

    def test_event_types(self) -> None:
        self._refused(([], [(4, 0, 1.0, 0, 1)], None), 4, "event_types")

    def test_event_order_not_ascending(self) -> None:
        self._refused(([0, 2], [(2, 0, 1, 0, 1), (2, 0, 1, 0, 1)], None), 4, "event_order")

    def test_event_negative_emit_refused(self) -> None:
        self._refused(([], [(-1, 0, 1, 0, 1)], None), 4, "event_order")

    def test_event_past_finish_anchor(self) -> None:
        # emit_at_row may equal n_rows (finish) but never exceed it.
        assert validate_scan(([], [(4, 0, 1, 0, 1)], None), 4) is True
        self._refused(([], [(5, 0, 1, 0, 1)], None), 4, "event_order")

    def test_event_at_or_after_stop_row(self) -> None:
        # With a stop, NO event may anchor at/after the stop row — a finish
        # event (emit == n_rows) is structurally impossible under a stop.
        self._refused(([0], [(3, 0, 1, 0, 1)], (3, 0)), 4, "event_order")
        self._refused(([0], [(4, 0, 1, 0, 1)], (3, 0)), 4, "event_order")

    def test_session_first_range(self) -> None:
        self._refused(([], [(4, 4, 1, 0, 1)], None), 4, "session_first_range")
        self._refused(([], [(4, -1, 1, 0, 1)], None), 4, "session_first_range")

    def test_skipped_floor(self) -> None:
        self._refused(([], [(4, 0, 0, 0, 1)], None), 4, "skipped_floor")

    def test_offender_range(self) -> None:
        self._refused(([], [(4, 0, 1, 1, 1)], None), 4, "offender_range")  # prev !< skip
        self._refused(([], [(4, 0, 1, 0, 4)], None), 4, "offender_range")  # skip >= n
        self._refused(([], [(4, 0, 1, -1, 1)], None), 4, "offender_range")  # negative


# ── Leaf discipline + vocabulary mirror pins ────────────────────────────────


class TestLeafDiscipline:
    """AST pins: import leaf-ness + the inline flag vocabulary mirror."""

    def test_leaf_imports_nothing_from_heavy_packages(self) -> None:
        source = Path(feed_rust.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = (
            "brutex.engine",
            "brutex.costs",
            "brutex.dashboard",
            "brutex.data",
            "brutex.feed",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden), alias.name
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith(forbidden), module

    def test_truthy_falsy_mirror_sweep_lookahead(self) -> None:
        # The leaf may not IMPORT env_flag_tristate (verified cycle:
        # engine.sweep_lookahead imports brutex.feed.lookahead at module
        # scope) — so its inline vocabulary is mirror-PINNED instead.
        from brutex.engine import sweep_lookahead

        assert feed_rust._TRUTHY == sweep_lookahead._TRUTHY  # pyright: ignore[reportPrivateUsage]
        assert feed_rust._FALSY == sweep_lookahead._FALSY  # pyright: ignore[reportPrivateUsage]


# ── Wheel-gated FFI pins ────────────────────────────────────────────────────


class TestKernelFfi:
    """Integers-only FFI shape + the length-mismatch edge (needs the wheel)."""

    @pytest.fixture
    def kernel_module(self) -> object:
        """The installed wheel, or a loud skip naming the rebuild remedy."""
        return pytest.importorskip(
            "brutex_feed", reason="brutex_feed wheel not installed — build via maturin"
        )

    def test_result_is_integers_only(self, kernel_module: object) -> None:
        # Walk a REAL kernel result with a warn event AND a stop: every leaf
        # is an int — no float, no string, no timestamp ever crosses.
        ts = [1_704_167_100_000_000 + k * 60_000_000 for k in range(4)]
        result = kernel_module.scan_bars(  # type: ignore[attr-defined]
            ts,
            [100.0, 100.0, 100.0, -1.0],
            [101.0, 101.0, 101.0, 1.0],
            [99.0, 99.0, 99.0, 1.0],
            [100.0, 1000.0, 100.5, 1.0],
            True,
            4.0,
        )
        kept, warns, stop = result
        leaves: list[object] = list(kept)
        for event in warns:
            leaves.extend(event)
        assert stop is not None
        leaves.extend(stop)
        assert leaves, "the walked result must not be trivially empty"
        for leaf in leaves:
            assert type(leaf) is int, repr(leaf)

    def test_length_mismatch_raises_value_error(self, kernel_module: object) -> None:
        with pytest.raises(ValueError, match="price column lengths diverge"):
            kernel_module.scan_bars([], [1.0], [1.0, 2.0], [1.0], [1.0], False, 4.0)  # type: ignore[attr-defined]
        with pytest.raises(ValueError, match="ts_us length"):
            kernel_module.scan_bars([], [1.0], [1.0], [1.0], [1.0], True, 4.0)  # type: ignore[attr-defined]
        with pytest.raises(ValueError, match="must be empty"):
            kernel_module.scan_bars(  # type: ignore[attr-defined]
                [1_704_167_100_000_000], [1.0], [1.0], [1.0], [1.0], False, 4.0
            )

    def test_abstain_returns_none(self, kernel_module: object) -> None:
        assert (
            kernel_module.scan_bars([-1], [1.0], [1.0], [1.0], [1.0], True, 4.0)  # type: ignore[attr-defined]
            is None
        )

    def test_shim_expected_symbols_match_expected_build_json(self) -> None:
        import json

        brutex_feed = pytest.importorskip(
            "brutex_feed", reason="brutex_feed wheel not installed — build via maturin"
        )
        expected_build = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "rust/brutex-feed/python/brutex_feed/EXPECTED_BUILD.json"
            ).read_text(encoding="utf-8")
        )
        shim_symbols = brutex_feed._EXPECTED_COMPILED_SYMBOLS  # pyright: ignore[reportPrivateUsage]
        assert sorted(shim_symbols) == expected_build["symbols"]
        assert brutex_feed.__build_crate_version__ == expected_build["crate_version"]
