"""Capability-gated dispatch pins for the >=64-bit (u128) Stage-2 count path.

DEC-SUPERTREND-CONDITIONS-001 (the vocab-66 SuperTrend implemented-flip, crate
0.5.0). With bits 64/65 LIVE, production Stage-1A ``bar_bits`` genuinely exceed
u64 on every SuperTrend-warm bar (the state fires one of the pair on every
non-equality bar past the 9-bar warm-up), so the Stage-2 count path's
``>= 64``-bit dispatch is a PRODUCTION surface — no longer future headroom.

A parallel lane (``claude/rust-u128-enum-kernel``) is building
``fno_math.ResidentBarsU128`` + a u128 count face + a Rust-first ``>= 64``
dispatch in :mod:`brutex.engine.combo_mask` (numpy retained as the parity
oracle only). This module is the merge hard-gate (a) counterpart — a
CAPABILITY GATE on the kernel's PRESENCE flips between the two laws
automatically, so the lane's landing arms the Rust-first pin with ZERO edits
here (and ``combo_mask.py`` itself is deliberately untouched by the flip
branch — the lane owns it):

* kernel ABSENT (today): ``>= 64``-bit counts route to the bit-exact two-limb
  numpy reference (``count_supermasks_reference`` for the free function; the
  resident numpy backing for :class:`~brutex.engine.combo_mask.ResidentBarBuffer`)
  — pinned ENGAGED + bit-exact vs an independent scalar reference;
* kernel PRESENT (post-lane): the SAME calls are Rust-served with the numpy
  counting path NEVER engaging (both numpy counting entry points are spied to
  raise) and stay bit-exact vs the same independent scalar reference.

Exactly ONE arm runs per environment, selected by
``hasattr(fno_math, "ResidentBarsU128")`` alone — correctness is never
skipped/xfailed on either arm (the skipif gates on kernel PRESENCE only).

LANE INTERFACE (coordinator addendum — the lane's DRAFT PR #1633, head
a97642a, unmerged): its documented supported test surface, once merged, is the
free ``combo_mask.dispatch_kernel_kind(bar_bits, masks)``,
``ResidentBarBuffer.kernel_kind`` / ``.wide_kernel_active``, the module
constants ``KERNEL_RUST_U64`` / ``KERNEL_RUST_U128`` / ``KERNEL_NUMPY`` =
``("rust-u64", "rust-u128", "numpy")``, and the typed
``WideKernelUnavailableError`` (a RuntimeError subclass) on a ``>= 64``
wheel-missing. Arm 1 PREFERS those documented observables when present
(``kernel_kind == "rust-u128"`` on a >= 64 window, literal-string compares)
and keeps the monkeypatch numpy spy as the defensive fallback law for a
kernel-present-but-observables-absent build; today's tree has neither, so all
of arm 1 stays cleanly gated off. The lane's own benches/parity suites
(``tests/bench/test_engine_combo_mask_wide_kernel_o1.py`` +
``tests/property/test_supermask_kernel_parity.py``) are deliberately NOT
duplicated here — the end-to-end 66-bit grid bench composes above them.

Plus the u64-fast-path RETIREMENT pins: a real Stage-1A window where bit 64/65
fires produces ``bar_bits > 2**64 - 1`` (the u128 regime is LIVE post-flip),
and :class:`~brutex.engine.rust_trade_sim.ResidentTradeSim` serves such a
window via the WIDE kernel (``fno_math.ResidentSweepSimU128``) from
construction — the trade-sim side is already Rust-wide today.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Final

import pytest
from brutex.data.models import CandleBar
from brutex.engine import combo_mask
from brutex.engine.combo_mask import U64_MASK, ResidentBarBuffer
from brutex.engine.stage1a import build_bar_bits
from brutex.vocabulary import bits as B


def _probe_wide_kernel_present() -> bool:
    """The capability gate — the ONE selector both arms key on. The lane's
    landing (fno_math gaining ``ResidentBarsU128``) flips the arms
    automatically; a wheel-less environment counts as ABSENT (the numpy law
    is exactly what a wheel-less box runs today)."""
    try:
        import fno_math
    except ImportError:  # pragma: no cover - wheel-less environment
        return False
    return hasattr(fno_math, "ResidentBarsU128")


_WIDE_KERNEL_PRESENT: Final[bool] = _probe_wide_kernel_present()

# ── independent scalar reference (computed in-test, imports nothing) ─────────


def _scalar_counts(bar_bits: Sequence[int], masks: Sequence[int]) -> list[int]:
    """Pure-Python scalar SUPERSET count — the independent oracle for BOTH arms.

    Deliberately NOT :func:`combo_mask.count_supermasks_reference` (which arm 1
    spies to raise): plain Python-int ``(bar & mask) == mask`` per pair, exact
    over arbitrary widths.
    """
    return [sum(1 for b in bar_bits if (b & m) == m) for m in masks]


# ── deterministic >=64-bit window + masks (both limbs genuinely exercised) ───

#: A dense wide bar: low-limb bits 0-19 + high-limb bits 64/65/70.
_WIDE_BAR: Final[int] = ((1 << 20) - 1) | (1 << 64) | (1 << 65) | (1 << 70)
#: The mixed window — wide bars, a u64-only bar, the ST pair alone, and zero.
_WINDOW: Final[tuple[int, ...]] = (
    _WIDE_BAR,
    (1 << 64) | 0b1011,
    (1 << 65) | 0b0101,
    0b1111,  # u64-only bar — its presence must not re-route wide masks
    0,
)
#: Masks spanning the dispatch space: wide-only, mixed-limb, u64-only, empty.
_MASKS: Final[tuple[int, ...]] = (
    1 << 64,
    1 << 65,
    (1 << 64) | 0b0011,
    (1 << 70) | (1 << 5),
    0b0101,
    0,
)


def _expected() -> list[int]:
    return _scalar_counts(_WINDOW, _MASKS)


def test_fixture_genuinely_wide_and_nonvacuous() -> None:
    """Teeth for both arms: the window/masks force >=64-bit dispatch and the
    expected counts are non-trivial (never an all-zero vacuous compare)."""
    assert any(b > U64_MASK for b in _WINDOW)
    assert any(m > U64_MASK for m in _MASKS)
    expected = _expected()
    assert expected == [2, 2, 2, 1, 3, 5]
    assert sum(expected) > 0


# ── arm 1: kernel PRESENT — Rust-first, numpy never engages ──────────────────


@pytest.mark.skipif(
    not _WIDE_KERNEL_PRESENT,
    reason=(
        "fno_math.ResidentBarsU128 absent (the u128 enum-kernel lane has not "
        "landed) — today's numpy-reference law is pinned by the "
        "*_pending_wide_kernel arm below, which always runs here"
    ),
)
class TestWideDispatchRustFirst:
    """Post-lane law: >=64-bit counts are Rust-served, zero numpy fallback."""

    def _spy_numpy_dead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make EVERY numpy counting entry point raise if engaged.

        ``count_supermasks_reference`` is the free-function fallback;
        ``_count_two_limb`` is the numpy counting primitive every current
        resident-buffer numpy path funnels through. ``raising=False`` keeps
        the spy robust if the lane deletes/renames a helper (setting an
        unused attribute is inert).
        """

        def _boom(*_a: object, **_k: object) -> list[int]:
            raise AssertionError(
                "numpy reference engaged on a >=64-bit count with "
                "fno_math.ResidentBarsU128 present — the Rust-first dispatch "
                "law (merge hard-gate (a)) is broken"
            )

        monkeypatch.setattr(combo_mask, "count_supermasks_reference", _boom, raising=False)
        monkeypatch.setattr(combo_mask, "_count_two_limb", _boom, raising=False)

    def test_free_count_wide_is_rust_served_and_bit_exact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._spy_numpy_dead(monkeypatch)
        assert combo_mask.count_supermasks(list(_WINDOW), list(_MASKS)) == _expected()

    def test_resident_buffer_wide_is_rust_served_and_bit_exact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        buf = ResidentBarBuffer(list(_WINDOW))
        self._spy_numpy_dead(monkeypatch)
        assert buf.count_supermasks(list(_MASKS)) == _expected()
        assert buf.count_supermask(1 << 64) == 2

    def test_u64_window_wide_mask_is_rust_served_and_bit_exact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A u64-resident window asked a WIDE mask also never touches numpy."""
        window = [0b1111, 0b1010, 0]
        buf = ResidentBarBuffer(window)
        self._spy_numpy_dead(monkeypatch)
        masks = [1 << 64, 0b1010, 0]
        assert buf.count_supermasks(masks) == _scalar_counts(window, masks) == [0, 2, 3]

    def test_documented_lane_observables_classify_the_wide_window(self) -> None:
        """PREFERRED probe (PR #1633 interface): the documented observables
        classify a >= 64 window as Rust-u128-served, literal-string compared.

        When the kernel is present but the observables are absent (a
        kernel-present-but-pre-#1633 build), this test SKIPS with the reason
        below and the monkeypatch-spy tests above remain the defensive
        zero-fallback law — the coordinator-specified fallback ordering.
        """
        if not hasattr(combo_mask, "dispatch_kernel_kind"):
            pytest.skip(
                "kernel present but the #1633 observables "
                "(dispatch_kernel_kind/kernel_kind/constants) are absent — "
                "the monkeypatch-spy tests above are the defensive fallback law"
            )
        # Constants compared as the LITERAL documented strings.
        assert getattr(combo_mask, "KERNEL_RUST_U64") == "rust-u64"  # noqa: B009
        assert getattr(combo_mask, "KERNEL_RUST_U128") == "rust-u128"  # noqa: B009
        assert getattr(combo_mask, "KERNEL_NUMPY") == "numpy"  # noqa: B009
        dispatch = getattr(combo_mask, "dispatch_kernel_kind")  # noqa: B009
        assert dispatch(list(_WINDOW), list(_MASKS)) == "rust-u128"
        buf = ResidentBarBuffer(list(_WINDOW))
        assert getattr(buf, "kernel_kind") == "rust-u128"  # noqa: B009
        assert getattr(buf, "wide_kernel_active") is True  # noqa: B009
        # The typed >= 64 wheel-missing raise is a RuntimeError subclass.
        err = getattr(combo_mask, "WideKernelUnavailableError")  # noqa: B009
        assert isinstance(err, type) and issubclass(err, RuntimeError)


# ── arm 2: kernel ABSENT (today) — the numpy reference IS the documented law ─


@pytest.mark.skipif(
    _WIDE_KERNEL_PRESENT,
    reason=(
        "the u128 enum kernel landed — the Rust-first arm above owns the "
        ">=64-bit dispatch law now (this arm pinned the pre-lane numpy route)"
    ),
)
class TestWideDispatchPendingWideKernel:
    """Today's law, pinned honestly: >=64-bit counts route to the bit-exact
    two-limb numpy reference. NOT a permanent contract — the lane's landing
    flips this arm off and arms the Rust-first pins above automatically."""

    def test_free_count_wide_routes_to_reference_pending_wide_kernel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[int, int]] = []
        real = combo_mask.count_supermasks_reference

        def _spy(bar_bits: Sequence[int], masks: Sequence[int]) -> list[int]:
            calls.append((len(bar_bits), len(masks)))
            return real(bar_bits, masks)

        monkeypatch.setattr(combo_mask, "count_supermasks_reference", _spy)
        got = combo_mask.count_supermasks(list(_WINDOW), list(_MASKS))
        assert got == _expected()
        assert calls == [(len(_WINDOW), len(_MASKS))], (
            "the >=64-bit free count did not route through "
            "count_supermasks_reference — the documented pre-lane law moved "
            "without fno_math.ResidentBarsU128 landing"
        )

    def test_resident_buffer_wide_window_uses_numpy_backing_pending_wide_kernel(self) -> None:
        buf = ResidentBarBuffer(list(_WINDOW))
        # DEC-P0-017 friend access: the numpy backing is the documented
        # pre-lane law for a >=64-bit window (no Rust handle is built).
        assert buf._rust is None  # pyright: ignore[reportPrivateUsage]
        assert buf.count_supermasks(list(_MASKS)) == _expected()
        assert buf.count_supermask(1 << 64) == 2

    def test_u64_backed_buffer_serves_wide_mask_via_two_limb_pending_wide_kernel(self) -> None:
        """A Rust-u64-backed buffer asked a WIDE mask serves it two-limb,
        bit-exact (the kept-raw-bars route) — never a truncated u64 count."""
        window = [0b1111, 0b1010, 0]
        buf = ResidentBarBuffer(window)
        masks = [1 << 64, 0b1010, 0]
        assert buf.count_supermasks(masks) == _scalar_counts(window, masks) == [0, 2, 3]


# ── u64 fast-path retirement: the u128 regime is LIVE in production ──────────

_IST = timezone(timedelta(hours=5, minutes=30))


def _flat_st_bars(n: int, *, day: int = 2) -> list[CandleBar]:
    """``n`` range-2 flat bars (101/99/100) on 2024-01-``day`` — the SuperTrend
    warm-up shape from ``tests/integration/test_stage1a_vwap_bos_bits_fire.py``:
    TR = 200 paisa every bar, the ATR-10 seed lands at bar 9 with the upper
    band above the close ⇒ bit 65 fires from bar 9 on.
    """
    out: list[CandleBar] = []
    for m in range(n):
        ist = datetime(2024, 1, day, 9, 15, tzinfo=_IST) + timedelta(minutes=m)
        out.append(
            CandleBar(
                timestamp=ist.astimezone(UTC),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=1_000,
                open_interest=None,
            )
        )
    return out


def test_supertrend_fire_makes_production_bar_bits_exceed_u64() -> None:
    """The FROZEN retirement pin: post-flip, real Stage-1A output leaves the
    u64 domain the moment the SuperTrend state warms (bar 9) — the u64
    fast path is no longer the production regime for warm windows.
    """
    masks = build_bar_bits(_flat_st_bars(12))
    # Warm-up bars carry only <64 bits — each fits u64 (the pre-flip regime).
    assert all(m <= U64_MASK for m in masks[:9])
    # From bar 9 the ST pair fires on every (non-equality) bar: bar_bits are
    # genuinely wider than u64 — the u128 regime is LIVE, not headroom.
    assert all(m > U64_MASK for m in masks[9:])
    assert masks[9] & (1 << B.BIT_CLOSE_BELOW_SUPERTREND)


def test_rust_trade_sim_serves_the_st_wide_window_via_the_wide_kernel() -> None:
    """The trade-sim twin: a REAL ST-firing Stage-1A window is served by the
    WIDE kernel (``ResidentSweepSimU128``) from construction — mirroring
    ``test_engine_rust_trade_sim.py::test_bar_bits_wide_window_served_by_u128_kernel``
    but over PRODUCTION bar_bits, not a synthetic ``1 << 100`` plant.
    """
    pytest.importorskip("fno_math", reason="the resident trade-sim kernel needs the built wheel")
    from brutex.engine.combo_mask import mask_to_entry_signals
    from brutex.engine.rust_trade_sim import ResidentTradeSim
    from brutex.engine.trade_sim import simulate_trades
    from brutex.strategies.types import Side

    bars = _flat_st_bars(20)
    bits = build_bar_bits(bars)
    assert any(b > U64_MASK for b in bits)  # teeth: the window is genuinely wide

    sim = ResidentTradeSim(bars, bits, tf_minutes=1, exec_bars_1m=bars)
    assert sim.rust_active is True
    # DEC-P0-017 friend access: wide-from-construction, the u128 sibling class.
    assert sim._rust_is_wide is True  # pyright: ignore[reportPrivateUsage]
    assert type(sim._rust).__name__ == "ResidentSweepSimU128"  # pyright: ignore[reportPrivateUsage]

    # Anti-vacuity: a mask made of the LIVE ST bit alone opens (and closes) a
    # real trade through the wide kernel, bit-identical to the Python oracle.
    mask = 1 << B.BIT_CLOSE_BELOW_SUPERTREND
    got = sim.simulate_masks([mask], direction=Side.BUY)
    expected = [
        simulate_trades(
            bars,
            mask_to_entry_signals(bits, mask),
            direction=Side.BUY,
            tf_minutes=1,
            exec_bars_1m=bars,
            stop_loss_paisa=None,
            take_profit_paisa=None,
        )
    ]
    assert got == expected
    assert len(got[0]) >= 1, "the ST-bit mask never produced a trade — vacuous pin"
