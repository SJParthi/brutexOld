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
#
# Events are the 9-int decision-plane records (DEC-BADTICK-HEAD-SEED-001 +
# DEC-BADTICK-HILO-SCREEN-001): (kind, emit_at_row, session_first_row, a, b,
# c, d, e, f) — SKIP(0) carries (skipped, first_ref_row, first_skip_row,
# first_reason, n_high, n_low); HEAD_UNCONFIRMED(1) (row_0, row_1, 0...);
# MAJORITY_DROPPED(2) (skipped, kept_in_session, 0...).


def _skip(
    emit: int,
    sfr: int = 0,
    *,
    skipped: int = 1,
    ref: int = 0,
    skip: int = 1,
    reason: int = 0,
    n_high: int = 0,
    n_low: int = 0,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (0, emit, sfr, skipped, ref, skip, reason, n_high, n_low)


def _ok_result() -> ScanResult:
    """A well-formed clean-scan result over n_rows=4 (one finish SKIP event)."""
    return ([0, 2, 3], [_skip(4)], None)


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
        assert validate_scan(([0], [_skip(2, skip=1)], (3, 2)), 4) is True

    def test_valid_empty(self) -> None:
        assert validate_scan(([], [], None), 0) is True

    def test_valid_boundary_flush_at_kept_row(self) -> None:
        assert validate_scan(([0, 2], [_skip(2)], None), 3) is True

    def test_valid_head_vote_ref_after_skip(self) -> None:
        # The head-median law: the reference row may lie AFTER the skip row
        # (a poison FIRST bar judged against the median at row 2) — the old
        # `first_prev_row < first_skip_row` ordering constraint is GONE, and
        # ref == skip is equally legal (the median bar judged against its
        # own high/low).
        assert validate_scan(([1, 2, 3], [_skip(4, ref=2, skip=0)], None), 4) is True
        assert (
            validate_scan(([0, 2, 3], [_skip(4, ref=1, skip=1, reason=1, n_high=1)], None), 4)
            is True
        )

    def test_valid_shared_anchor_skip_then_majority(self) -> None:
        # A session's SKIP + MAJORITY_DROPPED share ONE anchor (non-decreasing
        # law, not strictly-ascending).
        result = (
            [1],
            [_skip(3, skipped=2), (2, 3, 0, 2, 1, 0, 0, 0, 0)],
            None,
        )
        assert validate_scan(result, 3) is True

    def test_valid_head_unconfirmed(self) -> None:
        assert validate_scan(([0, 1], [(1, 2, 0, 0, 1, 0, 0, 0, 0)], None), 2) is True

    def test_result_shape_not_tuple(self) -> None:
        self._refused([[], [], None], 4, "result_shape")

    def test_result_shape_wrong_arity(self) -> None:
        self._refused(([], []), 4, "result_shape")

    def test_column_shape_kept_not_list(self) -> None:
        self._refused(((0, 1), [], None), 4, "column_shape")

    def test_column_shape_warns_not_list(self) -> None:
        self._refused(([], (_skip(4),), None), 4, "column_shape")

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
        self._refused(([], [list(_skip(4))], None), 4, "event_shape")
        self._refused(([], [(4, 0, 1, 0, 1)], None), 4, "event_shape")  # the retired 5-int shape
        self._refused(([], [(*_skip(4), 0)], None), 4, "event_shape")

    def test_event_types(self) -> None:
        self._refused(([], [(0, 4, 0, 1.0, 0, 1, 0, 0, 0)], None), 4, "event_types")

    def test_event_order_decreasing_refused(self) -> None:
        self._refused(([0, 2], [_skip(3), _skip(2)], None), 4, "event_order")

    def test_event_negative_emit_refused(self) -> None:
        self._refused(([], [_skip(-1)], None), 4, "event_order")

    def test_event_past_finish_anchor(self) -> None:
        # emit_at_row may equal n_rows (finish) but never exceed it.
        assert validate_scan(([], [_skip(4)], None), 4) is True
        self._refused(([], [_skip(5)], None), 4, "event_order")

    def test_event_at_or_after_stop_row(self) -> None:
        # With a stop, NO event may anchor at/after the stop row — a finish
        # event (emit == n_rows) is structurally impossible under a stop.
        self._refused(([0], [_skip(3)], (3, 0)), 4, "event_order")
        self._refused(([0], [_skip(4)], (3, 0)), 4, "event_order")

    def test_event_kind_unknown_refused(self) -> None:
        self._refused(([], [(3, 4, 0, 1, 0, 1, 0, 0, 0)], None), 4, "event_kind")

    def test_session_first_range(self) -> None:
        self._refused(([], [_skip(4, sfr=4)], None), 4, "session_first_range")
        self._refused(([], [_skip(4, sfr=-1)], None), 4, "session_first_range")

    def test_skipped_floor(self) -> None:
        self._refused(([], [_skip(4, skipped=0)], None), 4, "skipped_floor")

    def test_offender_range(self) -> None:
        self._refused(([], [_skip(4, skip=4)], None), 4, "offender_range")  # skip >= n
        self._refused(([], [_skip(4, ref=4)], None), 4, "offender_range")  # ref >= n
        self._refused(([], [_skip(4, ref=-1)], None), 4, "offender_range")  # negative
        self._refused(([], [_skip(4, skip=-1)], None), 4, "offender_range")

    def test_reason_range(self) -> None:
        self._refused(([], [_skip(4, reason=3)], None), 4, "reason_range")
        self._refused(([], [_skip(4, reason=-1)], None), 4, "reason_range")

    def test_reason_tally(self) -> None:
        # n_high + n_low can never exceed the skipped count; negatives refuse.
        self._refused(([], [_skip(4, skipped=1, n_high=1, n_low=1)], None), 4, "reason_tally")
        self._refused(([], [_skip(4, n_high=-1)], None), 4, "reason_tally")
        self._refused(([], [_skip(4, n_low=-1)], None), 4, "reason_tally")

    def test_head_pair_range(self) -> None:
        self._refused(([], [(1, 4, 0, 1, 1, 0, 0, 0, 0)], None), 4, "head_pair_range")  # a == b
        self._refused(([], [(1, 4, 0, 2, 1, 0, 0, 0, 0)], None), 4, "head_pair_range")  # a > b
        self._refused(([], [(1, 4, 0, 0, 4, 0, 0, 0, 0)], None), 4, "head_pair_range")  # b >= n
        self._refused(([], [(1, 4, 0, -1, 1, 0, 0, 0, 0)], None), 4, "head_pair_range")

    def test_head_padding(self) -> None:
        self._refused(([], [(1, 4, 0, 0, 1, 1, 0, 0, 0)], None), 4, "head_padding")

    def test_majority_law(self) -> None:
        self._refused(([], [(2, 4, 0, 0, 0, 0, 0, 0, 0)], None), 4, "majority_law")  # skipped 0
        self._refused(([], [(2, 4, 0, 2, 2, 0, 0, 0, 0)], None), 4, "majority_law")  # not strict
        self._refused(([], [(2, 4, 0, 2, -1, 0, 0, 0, 0)], None), 4, "majority_law")  # kept < 0

    def test_majority_padding(self) -> None:
        self._refused(([], [(2, 4, 0, 2, 1, 0, 0, 1, 0)], None), 4, "majority_padding")

    def test_session_first_at_or_after_stop_row(self) -> None:
        # Round-1 review tightening: under a stop at row 3, an event whose
        # session_first_row is AT the stop row (in [0, n_rows) but not
        # strictly before the truncation point) is kernel-impossible and
        # must refuse — kills the row_bound -> n_rows reversion mutant.
        self._refused(([0], [_skip(2, sfr=3)], (3, 0)), 5, "session_first_range")

    def test_offender_at_or_after_stop_row(self) -> None:
        # Same tightening for the offender coordinates: first_skip_row at
        # the stop row refuses even though it is < n_rows.
        self._refused(([0], [_skip(2, skip=3)], (3, 0)), 5, "offender_range")
        self._refused(([0], [_skip(2, ref=3)], (3, 0)), 5, "offender_range")

    def test_head_pair_at_or_after_stop_row(self) -> None:
        self._refused(([0], [(1, 2, 0, 1, 3, 0, 0, 0, 0)], (3, 0)), 5, "head_pair_range")


# ── Leaf discipline + vocabulary mirror pins ────────────────────────────────


class TestLeafDiscipline:
    """AST pins: import leaf-ness + the inline flag vocabulary mirror."""

    def test_leaf_imports_only_from_the_allowlist(self) -> None:
        # Round-1 review hardening: an import ALLOWLIST (strictly stronger
        # than the original heavy-package denylist) — a stray third-party or
        # brutex.* import into the leaf fails this pin by construction.
        # The leaf's whole legal surface: stdlib (__future__/os/typing),
        # structlog, and the lazy in-function wheel import.
        allowed = {"__future__", "os", "typing", "structlog", "brutex_feed"}
        source = Path(feed_rust.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    assert top in allowed, alias.name
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, "relative imports forbidden in the leaf"
                top = (node.module or "").split(".")[0]
                assert top in allowed, node.module

    def test_truthy_falsy_mirror_sweep_lookahead(self) -> None:
        # The leaf may not IMPORT env_flag_tristate (verified cycle:
        # engine.sweep_lookahead imports brutex.feed.lookahead at module
        # scope) — so its inline vocabulary is mirror-PINNED instead.
        from brutex.engine import sweep_lookahead

        assert feed_rust._TRUTHY == sweep_lookahead._TRUTHY  # pyright: ignore[reportPrivateUsage]
        assert feed_rust._FALSY == sweep_lookahead._FALSY  # pyright: ignore[reportPrivateUsage]

    def test_event_vocabulary_mirrors_bad_tick(self) -> None:
        # The leaf may not IMPORT brutex.data (see the allowlist pin), so its
        # event-kind / reason-bound literals are mirror-PINNED against the
        # canonical bad_tick vocabulary — the two decision planes must speak
        # ONE event language (DEC-BADTICK-HEAD-SEED-001 +
        # DEC-BADTICK-HILO-SCREEN-001).
        from typing import get_args

        from brutex.data import bad_tick

        assert feed_rust._EVENT_KIND_SKIP == bad_tick.EVENT_KIND_SKIP  # pyright: ignore[reportPrivateUsage]
        assert (
            feed_rust._EVENT_KIND_HEAD_UNCONFIRMED  # pyright: ignore[reportPrivateUsage]
            == bad_tick.EVENT_KIND_HEAD_UNCONFIRMED
        )
        assert (
            feed_rust._EVENT_KIND_MAJORITY_DROPPED  # pyright: ignore[reportPrivateUsage]
            == bad_tick.EVENT_KIND_MAJORITY_DROPPED
        )
        assert feed_rust._MAX_REASON_INDEX == bad_tick.REASON_LOW  # pyright: ignore[reportPrivateUsage]
        assert len(get_args(bad_tick.ScreenWarnEvent)) == feed_rust._EVENT_ARITY  # pyright: ignore[reportPrivateUsage]
        assert len(get_args(feed_rust.ScanEvent)) == feed_rust._EVENT_ARITY  # pyright: ignore[reportPrivateUsage]


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
        # is an int — no float, no string, no timestamp ever crosses. Day 1
        # carries a post-head skip (its SKIP event flushes at the day-2
        # boundary, row 4 — BEFORE the stop); day 2's second row stops the
        # scan on a negative open (stop suppresses any later flush).
        day1 = 1_704_167_100_000_000  # 2024-01-02 03:45 UTC
        day2 = day1 + 86_400_000_000
        ts = [day1 + k * 60_000_000 for k in range(4)] + [day2, day2 + 60_000_000]
        result = kernel_module.scan_bars(  # type: ignore[attr-defined]
            ts,
            [100.0, 100.0, 100.0, 100.0, 50.0, -1.0],
            [101.0, 101.0, 101.0, 1001.0, 51.0, 1.0],
            [99.0, 99.0, 99.0, 999.0, 49.0, 1.0],
            [100.0, 100.0, 100.0, 1000.0, 50.0, 1.0],
            True,
            4.0,
        )
        kept, warns, stop = result
        assert warns, "the fixture must exercise the warn-event leaves"
        leaves: list[object] = list(kept)
        for event in warns:
            leaves.extend(event)
        assert stop is not None
        leaves.extend(stop)
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
