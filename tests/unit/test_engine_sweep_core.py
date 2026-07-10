"""Unit tests for the Rust sweep-core Slice-1 seam (`brutex.engine.sweep_core`).

DESIGN_FINAL_RUST_SWEEP_CORE.md §5/§6 (Slice 1): flag + knob resolution (the
§12.2 env-flag law), the ENUM-column engagement conditions + every fallback
reason, the F27 window-length seam invariant (both directions), the
`enum_no_progress` / `enum_total_mismatch` belts (stubbed returns — both are
structurally unreachable through the real kernel), the enumeration burn-in
shadow (width-keyed windows, floor accounting, the mismatch → latch →
MID-TUPLE CONTINUATION contract), and the stall-budget guard incl. the
absent-symbol arm + the `_effective_stall_seconds` drift pin.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import sys
from collections.abc import Iterator, MutableMapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from brutex.data.models import CandleBar
from brutex.engine import sweep_core
from brutex.engine.combo_mask import count_combos, iter_masks_k_of_bits
from brutex.engine.sweep_core import (
    DEFAULT_ENUM_WORK_CELLS,
    ENUM_CELLS_LOWER_CLAMP,
    ENUM_CELLS_UPPER_CLAMP,
    EXPECTED_SWEEP_CORE_ABI_VERSION,
    MAX_ENUMERATED_CEIL,
    MAX_ENUMERATED_FLOOR,
    SWEEP_CORE_ENUM_CELLS_ENV,
    SWEEP_ENUM_SELF_CHECK_MASKS,
    SWEEP_ENUM_SELF_CHECK_RANGES_CAP,
    SWEEP_RUST_ENUM_ENV,
    EnumPlan,
    core_enumerate_kept,
    enum_shadow_masks_remaining,
    mask_hex,
    max_enumerated_for,
    reset_enum_self_check_for_tests,
    reset_observability_memo_for_tests,
    resolve_sweep_rust_enum_plan,
)
from structlog.testing import capture_logs

_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _fresh_seam_state() -> Iterator[None]:
    """Reset the process-global shadow window + latch + memo per test."""
    reset_enum_self_check_for_tests()
    reset_observability_memo_for_tests()
    yield
    reset_enum_self_check_for_tests()
    reset_observability_memo_for_tests()


def _bars(n: int = 8, *, start: datetime | None = None) -> list[CandleBar]:
    """A tiny deterministic regular-session bar window (optionally re-dated)."""
    base = datetime(2026, 1, 5, 9, 16, tzinfo=_IST) if start is None else start
    out: list[CandleBar] = []
    for i in range(n):
        ts = (base + timedelta(minutes=i)).astimezone(UTC)
        out.append(
            CandleBar(
                timestamp=ts,
                open=Decimal("100.00"),
                high=Decimal("101.00"),
                low=Decimal("99.00"),
                close=Decimal("100.50"),
                volume=10,
                open_interest=None,
            )
        )
    return out


def _call(
    *,
    bars: list[CandleBar] | None = None,
    bar_bits: list[int] | None = None,
    bits: list[int] | None = None,
    k: int = 2,
    min_hits: int = 0,
    digest_path: bool = True,
    plan: EnumPlan | None = None,
) -> sweep_core.EnumOutcome | None:
    """Drive `core_enumerate_kept` with small consistent defaults."""
    bars = _bars() if bars is None else bars
    bar_bits = [0b1111] * len(bars) if bar_bits is None else bar_bits
    bits = [0, 1, 2, 3] if bits is None else bits
    n_masks = count_combos(k, len(set(bits)))
    return core_enumerate_kept(
        bars=bars,
        bar_bits=bar_bits,
        condition_bits=bits,
        k=k,
        min_hits=min_hits,
        tf_minutes=1,
        n_masks=n_masks,
        digest_path=digest_path,
        plan=plan if plan is not None else EnumPlan(requested=True, enum_cells=1 << 28),
    )


def _fallback_reasons(logs: Sequence[MutableMapping[str, Any]]) -> list[str]:
    return [str(entry["reason"]) for entry in logs if entry["event"] == "sweep_rust_enum.fallback"]


class TestFlagResolution:
    """`resolve_sweep_rust_enum_plan` — the §12.2 tristate-arm policy."""

    def test_unset_defaults_off_without_knob_parse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(SWEEP_RUST_ENUM_ENV, raising=False)
        # A junk knob must be INERT with the flag unset (OFF-arm byte-identity).
        monkeypatch.setenv(SWEEP_CORE_ENUM_CELLS_ENV, "garbage")
        plan = resolve_sweep_rust_enum_plan()
        assert plan == EnumPlan(requested=False, enum_cells=DEFAULT_ENUM_WORK_CELLS)

    def test_whitespace_empty_defaults_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "   ")
        assert resolve_sweep_rust_enum_plan().requested is False

    def test_truthy_requests_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
        monkeypatch.delenv(SWEEP_CORE_ENUM_CELLS_ENV, raising=False)
        plan = resolve_sweep_rust_enum_plan()
        assert plan == EnumPlan(requested=True, enum_cells=DEFAULT_ENUM_WORK_CELLS)

    def test_falsy_is_the_silent_kill_switch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "off")
        with capture_logs() as logs:
            plan = resolve_sweep_rust_enum_plan()
        assert plan.requested is False
        assert _fallback_reasons(logs) == []

    def test_unrecognised_fails_safe_off_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "ture")
        with capture_logs() as logs:
            first = resolve_sweep_rust_enum_plan()
            second = resolve_sweep_rust_enum_plan()
        assert first.requested is False
        assert second.requested is False
        assert _fallback_reasons(logs) == ["flag_unrecognised"]  # warn-once


class TestEnumCellsKnob:
    """`BRUTEX_SWEEP_CORE_ENUM_CELLS` — fail-loud parse + dual clamp (§3)."""

    def _on(self, monkeypatch: pytest.MonkeyPatch, raw: str | None) -> EnumPlan:
        monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
        if raw is None:
            monkeypatch.delenv(SWEEP_CORE_ENUM_CELLS_ENV, raising=False)
        else:
            monkeypatch.setenv(SWEEP_CORE_ENUM_CELLS_ENV, raw)
        return resolve_sweep_rust_enum_plan()

    def test_unset_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._on(monkeypatch, None).enum_cells == DEFAULT_ENUM_WORK_CELLS

    def test_blank_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._on(monkeypatch, "  ").enum_cells == DEFAULT_ENUM_WORK_CELLS

    def test_valid_value_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._on(monkeypatch, str(1 << 20)).enum_cells == 1 << 20

    def test_junk_fails_loud_naming_the_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValueError, match=SWEEP_CORE_ENUM_CELLS_ENV):
            self._on(monkeypatch, "not-a-number")

    def test_oversized_clamps_upper_with_one_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with capture_logs() as logs:
            plan = self._on(monkeypatch, str(2**44))
            again = self._on(monkeypatch, str(2**44))
        assert plan.enum_cells == ENUM_CELLS_UPPER_CLAMP
        assert again.enum_cells == ENUM_CELLS_UPPER_CLAMP
        clamps = [e for e in logs if e["event"] == "sweep_core.work_cap_clamped"]
        assert len(clamps) == 1  # warn-once per direction
        assert clamps[0]["direction"] == "upper"

    def test_degenerate_clamps_lower_with_one_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with capture_logs() as logs:
            plan = self._on(monkeypatch, "1")
        assert plan.enum_cells == ENUM_CELLS_LOWER_CLAMP
        clamps = [e for e in logs if e["event"] == "sweep_core.work_cap_clamped"]
        assert len(clamps) == 1
        assert clamps[0]["direction"] == "lower"


class TestMaxEnumerated:
    """The §3 range bound — DIVISOR-floored, clamped [4096, 2**20]."""

    def test_zero_bars_is_total(self) -> None:
        # The round-5 `max(1, n_bars)` floor — never a ZeroDivisionError.
        assert max_enumerated_for(0, 1 << 28) == MAX_ENUMERATED_CEIL

    def test_small_window_hits_ceiling(self) -> None:
        assert max_enumerated_for(1, 1 << 28) == MAX_ENUMERATED_CEIL

    def test_deep_window_hits_floor(self) -> None:
        # 2**28 / 555_000 ≈ 483 < 4096 → the floor (the §1.3 honest-cost row).
        assert max_enumerated_for(555_000, 1 << 28) == MAX_ENUMERATED_FLOOR

    def test_mid_window_divides(self) -> None:
        assert max_enumerated_for(48_000, 1 << 28) == (1 << 28) // 48_000


class TestMaskHex:
    """§12 MASK-SERIALIZER LAW — masks serialize as 32-nibble hex strings."""

    def test_hex_shape(self) -> None:
        assert mask_hex(0) == "0" * 32
        assert mask_hex(5) == "0" * 31 + "5"
        assert mask_hex((1 << 100) | 1) == f"{(1 << 100) | 1:032x}"
        assert len(mask_hex((1 << 128) - 1)) == 32

    def test_no_bare_int_mask_serialization_in_the_new_surfaces(self) -> None:
        """The §12 grep pin: sweep_core.py + sweep_core.rs introduce NO bare-int
        mask serialization — every mask-valued log field routes via mask_hex."""
        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        py_src = (repo / "brutex" / "engine" / "sweep_core.py").read_text()
        rs_src = (repo / "rust" / "fno-math" / "src" / "sweep_core.rs").read_text()
        # The one sanctioned serializer exists…
        assert 'f"{mask:032x}"' in py_src
        # …and no f-string interpolates a mask value raw (bare-int surface).
        for banned in ("{mask}", "{masks}", "{kept}", "{py_kept}"):
            assert banned not in py_src
        # The Rust core formats NO mask value into any string (its error
        # strings carry indices/counts only — grep for the format sigil next
        # to the mask variable names).
        for banned in ("{mask}", "{mask:", "{kept"):
            assert banned not in rs_src


class TestShadowState:
    """The per-width shadow window accounting + reset seam (§5)."""

    def test_fresh_windows_per_width(self) -> None:
        assert enum_shadow_masks_remaining(64) == SWEEP_ENUM_SELF_CHECK_MASKS
        assert enum_shadow_masks_remaining(128) == SWEEP_ENUM_SELF_CHECK_MASKS

    def test_unknown_width_reads_zero(self) -> None:
        assert enum_shadow_masks_remaining(32) == 0

    def test_ranges_exhausted_reads_zero(self) -> None:
        reset_enum_self_check_for_tests(width=64, ranges_cap_remaining=0)
        assert enum_shadow_masks_remaining(64) == 0
        assert enum_shadow_masks_remaining(128) == SWEEP_ENUM_SELF_CHECK_MASKS

    def test_reset_specific_width_only(self) -> None:
        reset_enum_self_check_for_tests(width=64, masks_remaining=7)
        assert enum_shadow_masks_remaining(64) == 7
        assert enum_shadow_masks_remaining(128) == SWEEP_ENUM_SELF_CHECK_MASKS

    def test_consume_floor_unmet_warns_once(self) -> None:
        reset_enum_self_check_for_tests(width=64, masks_remaining=100, ranges_cap_remaining=1)
        with capture_logs() as logs:
            sweep_core._consume_shadow_range(64, 10)
        assert enum_shadow_masks_remaining(64) == 0  # window closed via ranges cap
        events = [e for e in logs if e.get("reason") == "enum_shadow_floor_unmet"]
        assert len(events) == 1
        assert events[0]["masks_unmet"] == 90
        # Its OWN event name — the kernel's results still ship, so this is
        # deliberately NOT a `fallback` event (module-docstring law).
        assert events[0]["event"] == "sweep_rust_enum.shadow_floor_unmet"
        assert _fallback_reasons(logs) == []

    def test_consume_floor_unmet_warns_per_width(self) -> None:
        # The memo is keyed per WIDTH (the `_warn_clamp` per-direction
        # pattern): a u64 floor-unmet close must never suppress the later
        # u128 one — the width-keyed degenerate-stream signal feeds the
        # flip-ADR residual list (§5 audit C51).
        reset_enum_self_check_for_tests(masks_remaining=100, ranges_cap_remaining=1)
        with capture_logs() as logs:
            sweep_core._consume_shadow_range(64, 10)
            sweep_core._consume_shadow_range(128, 10)
            # A repeat close on an already-warned width stays memoised.
            sweep_core._consume_shadow_range(64, 0)
        events = [e for e in logs if e["event"] == "sweep_rust_enum.shadow_floor_unmet"]
        assert [e["width"] for e in events] == [64, 128]

    def test_consume_floor_met_no_warn(self) -> None:
        reset_enum_self_check_for_tests(width=64, masks_remaining=5, ranges_cap_remaining=1)
        with capture_logs() as logs:
            sweep_core._consume_shadow_range(64, 5)
        assert _fallback_reasons(logs) == []
        assert enum_shadow_masks_remaining(64) == 0

    def test_latch_is_once(self) -> None:
        with capture_logs() as logs:
            sweep_core._latch_enum(64, start_rank=0)
            sweep_core._latch_enum(64, start_rank=1)
        errors = [e for e in logs if e["event"] == "sweep_rust_enum.kernel_autoreverted"]
        assert len(errors) == 1


class TestEngagementRefusals:
    """The §5 ENUM-column conditions, in precedence order, each warned."""

    def test_not_requested_is_silent_none(self) -> None:
        with capture_logs() as logs:
            outcome = _call(plan=EnumPlan(requested=False, enum_cells=1 << 28))
        assert outcome is None
        assert logs == []

    def test_legacy_dedup_path_refused_first(self) -> None:
        # The FALLBACK-REASON PRECEDENCE LAW golden: the legacy fixture's ONE
        # pinned warn is `legacy_dedup_path` (digest-path is listed first).
        with capture_logs() as logs:
            outcome = _call(digest_path=False)
        assert outcome is None
        assert _fallback_reasons(logs) == ["legacy_dedup_path"]

    def test_wheel_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sweep_core, "_load_kernel_module", lambda: None)
        with capture_logs() as logs:
            assert _call() is None
        assert _fallback_reasons(logs) == ["wheel_missing"]

    def test_wheel_missing_via_real_import_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `sys.modules[name] = None` makes `import fno_math` raise ImportError
        # — the loader's except arm, exercised for real.
        monkeypatch.setitem(sys.modules, "fno_math", None)
        assert sweep_core._load_kernel_module() is None

    def test_wheel_stale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Stale:
            ResidentSweepCore = object  # ResidentSweepCoreU128 + ABI missing

        stale = _Stale()
        monkeypatch.setattr(sweep_core, "_load_kernel_module", lambda: stale)
        with capture_logs() as logs:
            assert _call() is None
        assert _fallback_reasons(logs) == ["wheel_stale"]

    def test_abi_version_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Future:
            ResidentSweepCore = object
            ResidentSweepCoreU128 = object
            SWEEP_CORE_ABI_VERSION = EXPECTED_SWEEP_CORE_ABI_VERSION + 1

        future = _Future()
        monkeypatch.setattr(sweep_core, "_load_kernel_module", lambda: future)
        with capture_logs() as logs:
            assert _call() is None
        assert _fallback_reasons(logs) == ["abi_version_mismatch"]

    def test_width_beyond_u128_refused(self) -> None:
        with capture_logs() as logs:
            outcome = _call(bar_bits=[1 << 128] * 8)
        assert outcome is None
        assert _fallback_reasons(logs) == ["width_beyond_u128"]

    def test_kernel_disabled_after_latch(self) -> None:
        reset_enum_self_check_for_tests(width=64, latched="enum_shadow_mismatch")
        with capture_logs() as logs:
            assert _call() is None
        assert _fallback_reasons(logs) == ["kernel_disabled"]

    def test_kernel_disabled_is_width_keyed(self) -> None:
        # A u64 latch must NOT starve a u128 tuple (§5 round-5 width keying).
        reset_enum_self_check_for_tests(width=64, latched="enum_shadow_mismatch")
        outcome = _call(bar_bits=[(1 << 100) | 0b11] * 8, bits=[0, 1, 100], k=2)
        assert outcome is not None

    def test_stall_budget_exceeded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.engine import sweep_parallel

        # A synthetic near-zero deadline: the fresh shadow window's enum term
        # (~2.7 s at 8 bars — remaining floor + the one-range overshoot slop)
        # exceeds 0.5 × deadline.
        monkeypatch.setattr(sweep_parallel, "resolve_stall_seconds", lambda: 1e-6)
        monkeypatch.setattr(sweep_parallel, "resolve_stall_min_mask_bars_per_sec", lambda: 1e18)
        with capture_logs() as logs:
            assert _call() is None
        assert _fallback_reasons(logs) == ["stall_budget_exceeded"]

    def test_stall_guard_unavailable_when_symbols_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine import sweep_parallel

        monkeypatch.delattr(sweep_parallel, "resolve_stall_seconds")
        monkeypatch.delattr(sweep_parallel, "resolve_stall_min_mask_bars_per_sec")
        monkeypatch.delattr(sweep_parallel, "DEFAULT_STALL_SECONDS")
        with capture_logs() as logs:
            assert _call() is None
        assert _fallback_reasons(logs) == ["stall_guard_unavailable"]

    def test_flat_deadline_arm_engages_on_resolver_less_tree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The §5 round-6 ABSENT-SYMBOL arm: resolvers gone, DEFAULT_STALL_SECONDS
        # present → the flat deadline serves and the tuple ENGAGES.
        from brutex.engine import sweep_parallel

        monkeypatch.delattr(sweep_parallel, "resolve_stall_seconds")
        monkeypatch.delattr(sweep_parallel, "resolve_stall_min_mask_bars_per_sec")
        assert sweep_core._effective_stall_deadline(10, 10) == float(
            sweep_parallel.DEFAULT_STALL_SECONDS
        )
        outcome = _call()
        assert outcome is not None

    def test_stall_guard_total_evaluation_refuses_on_escape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.engine import sweep_parallel

        def _boom() -> float:
            raise RuntimeError("wedged resolver")

        monkeypatch.setattr(sweep_parallel, "resolve_stall_seconds", _boom)
        with capture_logs() as logs:
            assert _call() is None
        assert _fallback_reasons(logs) == ["stall_guard_unavailable"]

    def test_stall_guard_memory_error_reraises_bare(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The design's "MemoryError re-raises BARE everywhere" carve-out: the
        # guard's TOTAL evaluation must never swallow an OOM into the
        # stall_guard_unavailable refusal (the engine OOM global-stop law).
        from brutex.engine import sweep_parallel

        def _oom() -> float:
            raise MemoryError("stub")

        monkeypatch.setattr(sweep_parallel, "resolve_stall_seconds", _oom)
        with pytest.raises(MemoryError):
            _call()

    def test_stall_budget_prices_one_range_overshoot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The enum term prices the WORST case: remaining floor PLUS one
        # range's overshoot (the final open range is compared WHOLE). With 1
        # mask remaining and max_enumerated at the 2**20 ceiling (8 bars),
        # the priced term is ~2.5 s — a 1.0 s deadline (budget 0.5 s) fits
        # the bare remainder (~2.4 µs) but NOT the overshoot, so it REFUSES.
        from brutex.engine import sweep_parallel

        reset_enum_self_check_for_tests(width=64, masks_remaining=1)
        monkeypatch.setattr(sweep_parallel, "resolve_stall_seconds", lambda: 1.0)
        monkeypatch.setattr(sweep_parallel, "resolve_stall_min_mask_bars_per_sec", lambda: 1e18)
        with capture_logs() as logs:
            assert _call() is None
        assert _fallback_reasons(logs) == ["stall_budget_exceeded"]

    def test_stall_budget_zero_priced_when_window_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A CLOSED shadow window prices ZERO (no slop term): even a near-zero
        # deadline engages — the overshoot slop applies only while shadowing.
        from brutex.engine import sweep_parallel

        reset_enum_self_check_for_tests(width=64, masks_remaining=0)
        monkeypatch.setattr(sweep_parallel, "resolve_stall_seconds", lambda: 1e-6)
        monkeypatch.setattr(sweep_parallel, "resolve_stall_min_mask_bars_per_sec", lambda: 1e18)
        outcome = _call()
        assert outcome is not None

    def test_input_derivation_error_falls_back(self) -> None:
        # Out-of-coverage bar dates (2031 — past the published NSE holiday
        # span; a REAL production shape per the #1294 ingest abstain-and-KEEP
        # law): the session-calendar raise inside derive_kernel_inputs must
        # REFUSE to the spine (which then reproduces today's exact per-tuple
        # behavior — the #904 SweepComboError skip), never propagate raw past
        # the grid drivers' SweepComboError-only quarantine as a grid abort.
        bars = _bars(start=datetime(2031, 1, 6, 9, 16, tzinfo=_IST))
        with capture_logs() as logs:
            assert _call(bars=bars) is None
        assert _fallback_reasons(logs) == ["input_derivation_error"]

    def test_derivation_memory_error_reraises_bare(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The derivation's TOTAL catch carves out MemoryError (bare re-raise).
        def _oom(bars: object, tf_minutes: object) -> object:
            raise MemoryError("stub")

        monkeypatch.setattr(sweep_core, "derive_kernel_inputs", _oom)
        with pytest.raises(MemoryError):
            _call()

    def test_kernel_error_at_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Raising:
            def __init__(self, *args: object) -> None:
                raise ValueError("column length mismatch")

        class _Module:
            ResidentSweepCore = _Raising
            ResidentSweepCoreU128 = _Raising
            SWEEP_CORE_ABI_VERSION = EXPECTED_SWEEP_CORE_ABI_VERSION

        module = _Module()
        monkeypatch.setattr(sweep_core, "_load_kernel_module", lambda: module)
        with capture_logs() as logs:
            assert _call() is None
        assert _fallback_reasons(logs) == ["kernel_error"]

    def test_bars_length_mismatch_on_lying_core(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # §1.3 F27: a stubbed n_bars()-lying core trips `bars_length_mismatch`
        # deterministically — a marshalling defect can never silently shrink
        # a non-empty Python window.
        class _Lying:
            def __init__(self, *args: object) -> None:
                pass

            def n_bars(self) -> int:
                return 0

        class _Module:
            ResidentSweepCore = _Lying
            ResidentSweepCoreU128 = _Lying
            SWEEP_CORE_ABI_VERSION = EXPECTED_SWEEP_CORE_ABI_VERSION

        module = _Module()
        monkeypatch.setattr(sweep_core, "_load_kernel_module", lambda: module)
        with capture_logs() as logs:
            assert _call() is None
        assert _fallback_reasons(logs) == ["bars_length_mismatch"]

    def test_engaged_event_once_per_process(self) -> None:
        with capture_logs() as logs:
            assert _call() is not None
            assert _call() is not None
        engaged = [e for e in logs if e["event"] == "sweep_rust_enum.engaged"]
        assert len(engaged) == 1


class _StubCore:
    """A scripted `enumerate_prefilter` core for the belt/shadow stubs."""

    def __init__(self, n_bars: int, script: list[tuple[int, int, int, bool, int, list[int]]]):
        self._n_bars = n_bars
        self._script = list(script)
        self.calls: list[int] = []

    def n_bars(self) -> int:
        return self._n_bars

    def enumerate_prefilter(
        self,
        condition_bits: list[int],
        k: int,
        conflict_pairs: list[tuple[int, int]],
        min_hits: int,
        start_rank: int,
        max_enumerated: int,
        max_work_cells: int,
    ) -> tuple[int, int, int, bool, int, list[int]]:
        self.calls.append(start_rank)
        return self._script.pop(0)


def _stub_module(monkeypatch: pytest.MonkeyPatch, core: object) -> None:
    class _Cls:
        def __new__(cls, *args: object) -> Any:
            return core

    class _Module:
        ResidentSweepCore = _Cls
        ResidentSweepCoreU128 = _Cls
        SWEEP_CORE_ABI_VERSION = EXPECTED_SWEEP_CORE_ABI_VERSION

    module = _Module()
    monkeypatch.setattr(sweep_core, "_load_kernel_module", lambda: module)


class TestDriverBelts:
    """`enum_no_progress` + `enum_total_mismatch` + in-loop kernel_error."""

    def test_enum_no_progress_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Structurally unreachable through the real kernel (the ≥1-progress
        # law) — the stubbed zero-progress return must fall back, never spin.
        core = _StubCore(8, [(0, 0, 0, False, 0, [])])
        _stub_module(monkeypatch, core)
        with capture_logs() as logs:
            assert _call() is None
        assert "enum_no_progress" in _fallback_reasons(logs)

    def test_enum_total_mismatch_on_wrong_sum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A self-consistent WRONG total (§1.3 round-4): exhausted with fewer
        # enumerated than the driver's independent count_combos total.
        reset_enum_self_check_for_tests(masks_remaining=0)  # shadow closed
        core = _StubCore(8, [(3, 0, 3, True, 0, [3, 5, 6])])
        _stub_module(monkeypatch, core)
        with capture_logs() as logs:
            assert _call() is None  # C(4,2) = 6 != 3
        assert "enum_total_mismatch" in _fallback_reasons(logs)

    def test_enum_total_mismatch_on_wrong_final_rank(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reset_enum_self_check_for_tests(masks_remaining=0)
        core = _StubCore(8, [(6, 0, 5, True, 0, [3, 5, 9, 6, 10, 12])])
        _stub_module(monkeypatch, core)
        with capture_logs() as logs:
            assert _call() is None  # Σ enumerated == 6 but final rank 5 != 6
        assert "enum_total_mismatch" in _fallback_reasons(logs)

    def test_kernel_error_mid_enumerate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _RaisingCore:
            def n_bars(self) -> int:
                return 8

            def enumerate_prefilter(self, *args: object) -> object:
                raise OverflowError("stub")

        _stub_module(monkeypatch, _RaisingCore())
        with capture_logs() as logs:
            assert _call() is None
        assert "kernel_error" in _fallback_reasons(logs)


class TestShadowMismatch:
    """The §5 mismatch → ship-Python-lists → latch → MID-TUPLE CONTINUATION."""

    def _poisoned_module(self, monkeypatch: pytest.MonkeyPatch, poison_rank: int) -> None:
        """A real-kernel wrapper that DROPS the first kept mask of the range
        starting at `poison_rank` (the in-range kept-set divergence class)."""
        import fno_math

        real_cls = fno_math.ResidentSweepCore

        class _PoisonedCore:
            def __init__(self, *args: Any) -> None:
                self._real = real_cls(*args)

            def n_bars(self) -> int:
                return self._real.n_bars()

            def enumerate_prefilter(self, *args: Any) -> Any:
                enum_r, pruned, next_rank, exhausted, hits, masks = self._real.enumerate_prefilter(
                    *args
                )
                if args[4] == poison_rank and masks:
                    masks = masks[1:]
                return enum_r, pruned, next_rank, exhausted, hits, masks

        class _Module:
            ResidentSweepCore = _PoisonedCore
            ResidentSweepCoreU128 = _PoisonedCore
            SWEEP_CORE_ABI_VERSION = EXPECTED_SWEEP_CORE_ABI_VERSION

        module = _Module()
        monkeypatch.setattr(sweep_core, "_load_kernel_module", lambda: module)

    def test_mismatch_ships_python_lists_and_completes_byte_identical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The §7.4 pin: a poisoned mid-tuple range completes the tuple
        byte-identical to the spine with the mixed-provenance reconciliation
        passing, plus ONE autorevert ERROR + the per-width latch."""
        bits = list(range(10))
        k = 3  # C(10,3) = 120 masks
        bars = _bars(4)
        bar_bits = [0b1111111111, 0b1111111111, 0b1, 0b0]
        # Force multiple ranges: tiny cells budget (max_enum floor 4096 but
        # cells cap binds: cells=2**16 lower clamp, n_bars=4 → 2**16/4 = 16384
        # masks/call — too big. Use a stub max? Instead: poison rank 0 (the
        # FIRST range) — continuation drains the whole remainder.
        self._poisoned_module(monkeypatch, poison_rank=0)
        expected_pruned, expected_kept = sweep_core._python_prefilter(
            list(iter_masks_k_of_bits(bits, k)), bar_bits, 2
        )
        with capture_logs() as logs:
            outcome = _call(bars=bars, bar_bits=bar_bits, bits=bits, k=k, min_hits=2)
        assert outcome is not None
        assert outcome.enumerated == count_combos(k, len(bits))
        assert outcome.conflict_pruned == expected_pruned
        assert outcome.kept == expected_kept
        errors = [e for e in logs if e["event"] == "sweep_rust_enum.kernel_autoreverted"]
        assert len(errors) == 1
        assert errors[0]["width"] == 64
        # The latch holds: the NEXT tuple refuses with kernel_disabled.
        with capture_logs() as logs2:
            assert _call() is None
        assert _fallback_reasons(logs2) == ["kernel_disabled"]

    def test_second_range_poison_mid_stream_continuation_byte_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deterministic MID-STREAM twin of the rank-0 pin above: poison
        the SECOND range (start_rank 4096 of the C(16,6)=8008 two-range
        tuple), exercising the riskiest continuation arithmetic — resuming
        the SAME iter_masks stream AFTER a core-shipped prior range, at
        `rank + len(python_window)`, with mixed-provenance reconciliation."""
        bits = list(range(16))
        k = 6
        n_masks = count_combos(k, 16)
        assert n_masks == 8008  # max_enum = 2**16/16 = 4096 → ranges at 0, 4096
        self._poisoned_module(monkeypatch, poison_rank=4096)
        expected_pruned, expected_kept = sweep_core._python_prefilter(
            list(iter_masks_k_of_bits(bits, k)), [0b1] * 16, 0
        )
        with capture_logs() as logs:
            outcome = _call(
                bars=_bars(16),
                bar_bits=[0b1] * 16,
                bits=bits,
                k=k,
                plan=EnumPlan(requested=True, enum_cells=sweep_core.ENUM_CELLS_LOWER_CLAMP),
            )
        assert outcome is not None
        assert outcome.enumerated == n_masks
        assert outcome.conflict_pruned == expected_pruned
        assert outcome.kept == expected_kept  # byte-identical to the pure spine
        errors = [e for e in logs if e["event"] == "sweep_rust_enum.kernel_autoreverted"]
        assert len(errors) == 1
        assert errors[0]["start_rank"] == 4096  # genuinely the SECOND range
        with capture_logs() as logs2:
            assert _call() is None  # the latch holds
        assert _fallback_reasons(logs2) == ["kernel_disabled"]

    def test_match_consumes_shadow_window(self) -> None:
        # C(4,2) = 6 masks over the real kernel: one clean range consumes 6
        # shadowed masks + 1 range slot at width 64 only.
        outcome = _call()
        assert outcome is not None
        assert enum_shadow_masks_remaining(64) == SWEEP_ENUM_SELF_CHECK_MASKS - 6
        assert enum_shadow_masks_remaining(128) == SWEEP_ENUM_SELF_CHECK_MASKS
        assert sweep_core._SHADOW_RANGES_REMAINING[64] == SWEEP_ENUM_SELF_CHECK_RANGES_CAP - 1

    def test_closed_window_skips_shadow(self) -> None:
        reset_enum_self_check_for_tests(width=64, masks_remaining=0)
        outcome = _call()
        assert outcome is not None
        assert sweep_core._SHADOW_RANGES_REMAINING[64] == SWEEP_ENUM_SELF_CHECK_RANGES_CAP

    def test_multi_range_tuple_shadows_every_range_in_lockstep(self) -> None:
        # C(16, 6) = 8008 masks at max_enum 4096 (enum_cells at the lower
        # clamp, 16 bars) -> TWO ranges, BOTH shadowed off ONE Python oracle
        # stream held across calls (the §5 lockstep contract).
        bits = list(range(16))
        k = 6
        n_masks = count_combos(k, 16)
        assert n_masks == 8008
        outcome = _call(
            bars=_bars(16),
            bar_bits=[0b1] * 16,
            bits=bits,
            k=k,
            plan=EnumPlan(requested=True, enum_cells=sweep_core.ENUM_CELLS_LOWER_CLAMP),
        )
        assert outcome is not None
        assert outcome.enumerated == n_masks
        assert enum_shadow_masks_remaining(64) == SWEEP_ENUM_SELF_CHECK_MASKS - n_masks
        assert sweep_core._SHADOW_RANGES_REMAINING[64] == SWEEP_ENUM_SELF_CHECK_RANGES_CAP - 2

    def test_window_stays_open_across_calls_until_floor(self) -> None:
        # The accumulate-ACROSS-calls law: two 6-mask tuples consume 12.
        assert _call() is not None
        assert _call() is not None
        assert enum_shadow_masks_remaining(64) == SWEEP_ENUM_SELF_CHECK_MASKS - 12

    def test_u128_tuple_runs_a_fresh_width_window(self) -> None:
        # §7.4: a process that consumed (part of) the u64 window then engaging
        # a [64,128)-bit tuple shadows under the FRESH u128 window.
        assert _call() is not None
        outcome = _call(bar_bits=[(1 << 100) | 0b11] * 8, bits=[0, 1, 100], k=2)
        assert outcome is not None
        assert enum_shadow_masks_remaining(128) == SWEEP_ENUM_SELF_CHECK_MASKS - 3

    def test_python_window_shorter_than_claim_is_a_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A core OVER-claiming enumerated (islice exhausts first) latches too.
        core = _StubCore(8, [(7, 0, 7, True, 0, [3, 5, 9, 6, 10, 12, 99])])
        _stub_module(monkeypatch, core)
        with capture_logs() as logs:
            outcome = _call()  # C(4,2)=6 < claimed 7
        # Python window (6 masks) ships; continuation is empty; totals check
        # passes on the PYTHON side (6 == 6) → the outcome serves.
        assert outcome is not None
        assert outcome.enumerated == 6
        errors = [e for e in logs if e["event"] == "sweep_rust_enum.kernel_autoreverted"]
        assert len(errors) == 1


class TestStallDeadlineDriftPin:
    """The seam's recomputation ≡ `ParallelGridDriver._effective_stall_seconds`.

    The §5 drift pin — scoped `skipif` on symbol absence (the round-6
    absent-symbol arm has its own dedicated tests above).
    """

    def test_recomputation_matches_the_real_driver_formula(self) -> None:
        from brutex.engine import sweep_parallel

        driver_cls = getattr(sweep_parallel, "ParallelGridDriver", None)
        method = getattr(driver_cls, "_effective_stall_seconds", None)
        if method is None:  # pragma: no cover - pre-#1291 tree only
            pytest.skip("_effective_stall_seconds absent (pre-#1291 tree)")
        stall = sweep_parallel.resolve_stall_seconds()
        rate = sweep_parallel.resolve_stall_min_mask_bars_per_sec()

        class _Self:
            _stall_seconds = stall
            _stall_min_mask_bars_per_sec = rate

        for n_masks, n_bars in [(1, 1), (1770, 48_000), (50_063_860, 555_000), (0, 0), (1, 0)]:
            ours = sweep_core._effective_stall_deadline(n_masks, n_bars)
            theirs = method(_Self(), float(n_masks) * float(n_bars))
            assert ours == theirs


class TestPurePrefilterHelpers:
    """`_python_prefilter` + `_drain_python_remainder` + `_select_width`."""

    def test_prefilter_min_hits_zero(self) -> None:
        from brutex.vocabulary import has_conflict

        masks = list(iter_masks_k_of_bits([0, 1, 2], 2))
        pruned, kept = sweep_core._python_prefilter(masks, [0b111], 0)
        expected_kept = [m for m in masks if not has_conflict(m)]
        assert pruned == len(masks) - len(expected_kept)
        assert kept == expected_kept

    def test_prefilter_counts_and_boundary(self) -> None:
        # Single-bit masks are conflict-free by construction (pairs need 2).
        bar_bits = [0b11, 0b11, 0b01, 0b00]
        pruned, kept = sweep_core._python_prefilter([0b01, 0b10], bar_bits, 2)
        assert pruned == 0
        assert kept == [0b01, 0b10]  # hits 3/2, both >= 2 (strict >=)
        pruned, kept = sweep_core._python_prefilter([0b01, 0b10], bar_bits, 3)
        assert pruned == 0
        assert kept == [0b01]  # 2 < 3 dropped at the strict boundary

    def test_prefilter_all_conflicted_skips_count(self) -> None:
        from brutex.vocabulary import CONFLICT_PAIRS

        a, b = sorted(CONFLICT_PAIRS)[0]
        conflicted = (1 << a) | (1 << b)
        pruned, kept = sweep_core._python_prefilter([conflicted], [0b1], 5)
        assert (pruned, kept) == (1, [])

    def test_drain_remainder_chunks(self) -> None:
        from brutex.vocabulary import has_conflict

        masks = list(iter_masks_k_of_bits([0, 1, 2, 3], 2))
        enumerated, pruned, kept = sweep_core._drain_python_remainder(iter(masks), [0b1111], 0, 2)
        expected_kept = [m for m in masks if not has_conflict(m)]
        assert enumerated == 6
        assert pruned == 6 - len(expected_kept)
        assert kept == expected_kept

    def test_drain_remainder_empty(self) -> None:
        assert sweep_core._drain_python_remainder(iter([]), [0b1], 0, 0) == (0, 0, [])

    def test_select_width_empty_safe(self) -> None:
        # The §5 round-5 `default=-1` totalisation: bits=[] contributes 0.
        assert sweep_core._select_width([0b1], [], []) == 1
        assert sweep_core._select_width([], [], []) == 0
        assert sweep_core._select_width([1 << 63], [], []) == 64
        assert sweep_core._select_width([0b1], [64], []) == 65
        assert sweep_core._select_width([0b1], [], [70]) == 71


class TestK0Vector:
    """§1.3 clause (a): bits=[]/k=0 ENGAGES and serves the k=0 vector."""

    def test_k0_empty_bits_engages(self) -> None:
        outcome = _call(bits=[], k=0)
        assert outcome is not None
        assert outcome.enumerated == 1
        assert outcome.conflict_pruned == 0
        assert outcome.kept == [0]

    def test_k0_nonempty_bits(self) -> None:
        outcome = _call(bits=[0, 1, 2], k=0, min_hits=1)
        assert outcome is not None
        assert outcome.kept == [0]  # (bar & 0) == 0 hits every bar
        assert outcome.hits_counted == 1


class TestHitsCountedSeamPin:
    """§1.3 MIN-HITS SHORT-CIRCUIT LAW — the decoded in-band value."""

    def test_zero_when_min_hits_zero(self) -> None:
        outcome = _call(min_hits=0)
        assert outcome is not None
        assert outcome.hits_counted == 0

    def test_conflict_free_count_when_min_hits_positive(self) -> None:
        outcome = _call(bits=[0, 1, 2, 3], k=2, min_hits=1)
        assert outcome is not None
        assert outcome.hits_counted == outcome.enumerated - outcome.conflict_pruned


class TestSweepRunWiring:
    """The `run_persisted_sweep` enum-arm wiring (the §1.4 call shape) —
    covered from the engine glob so the per-module gate sees the arm."""

    @staticmethod
    def _run(tmp_path: object, tag: str, *, digest: str | None) -> object:
        from pathlib import Path

        from brutex.data.option_dedup import OptionDedupIndex
        from brutex.engine.results_store import ResultsStore
        from brutex.engine.sweep_run import ComboDedup, SweepRunConfig, run_persisted_sweep
        from brutex.orchestrator.audit import AuditWriter
        from brutex.strategies.types import Side

        base = Path(str(tmp_path))
        cfg = SweepRunConfig(
            instrument="NIFTY",
            tf="1m",
            direction=Side.BUY,
            mode="A",
            condition_bits=[0, 1, 2, 3, 4],
            k=2,
            min_hits=1,
            top_k=5,
            data_snapshot_hash="snap0001",
            indicator_version="ind-v1",
            code_commit_sha="deadbeef",
            tf_minutes=1,
            sl_paisa=80,
            tp_paisa=50,
        )
        store = ResultsStore(path=base / f"{tag}.parquet")
        dedup = ComboDedup(OptionDedupIndex(capacity=10_000, fpr_target=1e-6))
        audit = AuditWriter(path=base / f"{tag}.jsonl")
        with audit:
            return run_persisted_sweep(
                _bars(60), cfg, store=store, dedup=dedup, audit=audit, trial_stats_digest=digest
            )

    def test_enum_arm_summary_matches_off_arm(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SWEEP_RUST_ENUM_ENV, raising=False)
        off = self._run(tmp_path, "off", digest="b" * 16)
        monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
        reset_enum_self_check_for_tests()
        with capture_logs() as logs:
            on = self._run(tmp_path, "on", digest="b" * 16)
        assert on == off  # counters + hashes byte-identical (the twin law)
        assert [e for e in logs if e["event"] == "sweep_rust_enum.engaged"]

    def test_requested_but_refused_serves_the_spine(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Flag ON + legacy (no-digest) path → the seam refuses and the
        # verbatim Python loop serves (the wiring's None branch).
        monkeypatch.delenv(SWEEP_RUST_ENUM_ENV, raising=False)
        off = self._run(tmp_path, "legacy-off", digest=None)
        monkeypatch.setenv(SWEEP_RUST_ENUM_ENV, "1")
        with capture_logs() as logs:
            on = self._run(tmp_path, "legacy-on", digest=None)
        assert on == off
        reasons = [e["reason"] for e in logs if e["event"] == "sweep_rust_enum.fallback"]
        assert reasons == ["legacy_dedup_path"]
