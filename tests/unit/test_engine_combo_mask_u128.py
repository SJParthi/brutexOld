"""u128 kernel-dispatch coverage for ``brutex.engine.combo_mask``.

DEC-ENUM-KERNEL-U128-001 (supersedes the DEC-VOCAB-U128-CODEGEN-001 two-limb
ROUTING these tests originally pinned): the production ``count_supermasks`` /
``ResidentBarBuffer`` now width-dispatch to the **u128 Rust twin kernel**
(``fno_math.count_supermasks_u128s`` / ``fno_math.ResidentBarsU128``) whenever
any mask/bar sets a bit ``>= 64`` — the numpy two-limb path is retained
strictly as the parity ORACLE (plus the pre-existing <=64-bit stale-wheel
construction fallback). These tests pin:

* the >=64-bit branches route to the u128 Rust kernel and stay bit-exact
  against the two-limb reference + the Python-int scalar oracle;
* the LOUD-FAIL law — a module missing the u128 twins at a >=64-bit dispatch
  raises the typed :class:`WideKernelUnavailableError` naming
  DEC-ENUM-KERNEL-U128-001 + the rebuild remediation, NEVER a silent numpy
  demotion (the deliberate inversion of #1549's oracle degrade);
* the u64 path is untouched — the wide kernel is NEVER invoked for <=64-bit
  inputs (bomb-spy pinned) and the <=64-bit stale-wheel numpy construction
  fallback survives byte-identically;
* the lazy wide-kernel promotion (one instance-level build + ONE
  ``combo_mask.wide_kernel_promoted`` INFO) and the ``kernel_kind`` /
  ``wide_kernel_active`` / ``dispatch_kernel_kind`` observables (the
  supported no-monkeypatch test surface, mirrored from #1549's
  ``rust_active``).
"""

from __future__ import annotations

import fno_math
import pytest
import structlog.testing
from brutex.engine.combo_mask import (
    KERNEL_NUMPY,
    KERNEL_RUST_U64,
    KERNEL_RUST_U128,
    U64_MASK,
    U128_MASK,
    ResidentBarBuffer,
    WideKernelUnavailableError,
    count_supermasks,
    count_supermasks_reference,
    dispatch_kernel_kind,
    evaluate_mask,
)

# A bar + mask that BOTH set a bit >= 64 (the u128 headroom) and a bit < 64.
_HI_BAR = (1 << 70) | (1 << 80) | (1 << 3) | (1 << 1)
_HI_MASK = (1 << 70) | (1 << 1)


def test_u64_mask_constant() -> None:
    """``U64_MASK`` is the low-limb mask (all 64 low bits set)."""
    assert U64_MASK == (1 << 64) - 1


def test_u128_mask_constant() -> None:
    """``U128_MASK`` is the u128 ceiling (all 128 bits set)."""
    assert U128_MASK == (1 << 128) - 1


def test_kernel_label_constants_literal_values() -> None:
    """The three ``KERNEL_*`` labels are pinned at the VALUE level.

    These strings are the PR-B-facing supported test surface (`__all__`
    exports a downstream vocab-66 pin will compare against literally), so a
    symbol-to-symbol comparison alone is not enough — a constant-value swap
    (e.g. ``KERNEL_RUST_U128 = "rust-u64"``) would ship a lying
    ``kernel_kind`` / ``dispatch_kernel_kind`` observable that no
    symbol-level assertion catches (the round-2 P2/P9 surviving mutants).
    Pin the literals AND their pairwise distinctness.
    """
    assert (KERNEL_RUST_U64, KERNEL_RUST_U128, KERNEL_NUMPY) == (
        "rust-u64",
        "rust-u128",
        "numpy",
    )
    assert len({KERNEL_RUST_U64, KERNEL_RUST_U128, KERNEL_NUMPY}) == 3


# ─────────────────────────────────────────────────────────────────
# Free dispatch — >=64-bit inputs route to the u128 Rust kernel
# ─────────────────────────────────────────────────────────────────


def test_count_supermasks_routes_high_bit_to_u128_kernel() -> None:
    """A mask with a bit >= 64 routes to the u128 Rust kernel (no truncation).

    The production ``count_supermasks`` guard detects a u128 mask/bar and
    dispatches to ``fno_math.count_supermasks_u128s`` instead of the u64
    kernel (which would drop the high bits). The two-limb numpy reference is
    the parity oracle the counts must equal bit-for-bit.
    """
    bars = [_HI_BAR, 1 << 70, 0b1010]
    masks = [_HI_MASK, 1 << 70, 0]
    got = count_supermasks(bars, masks)
    assert got == count_supermasks_reference(bars, masks)
    # Hand-verified: _HI_BAR ⊇ _HI_MASK (1), only _HI_BAR & (1<<70)-bar ⊇ (1<<70) (2),
    # every bar ⊇ 0 (3).
    assert got == [1, 2, 3]
    assert dispatch_kernel_kind(bars, masks) == KERNEL_RUST_U128


def test_count_supermasks_high_bar_with_low_mask_routes_too() -> None:
    """A high-bit BAR (even with a low mask) also routes wide (the OR arm)."""
    bars = [1 << 90, 0b11]
    masks = [0b01]  # a u64-fitting mask, but a bar exceeds u64
    got = count_supermasks(bars, masks)
    assert got == count_supermasks_reference(bars, masks)
    assert got == [1]  # only the 0b11 bar is a superset of 0b01
    assert dispatch_kernel_kind(bars, masks) == KERNEL_RUST_U128


def test_count_supermasks_wide_matches_direct_u128_kernel() -> None:
    """The facade's wide arm serves the EXACT ``count_supermasks_u128s`` output."""
    bars = [_HI_BAR, _HI_MASK, (1 << 100) | 1, 0]
    masks = [_HI_MASK, 1 << 100, 1, 0]
    assert count_supermasks(bars, masks) == list(fno_math.count_supermasks_u128s(bars, masks))


def test_dispatch_kernel_kind_classifier() -> None:
    """``dispatch_kernel_kind``: wide masks OR wide bars → u128; else u64."""
    assert dispatch_kernel_kind([1, 2], [3]) == KERNEL_RUST_U64
    assert dispatch_kernel_kind([1 << 70], [1]) == KERNEL_RUST_U128
    assert dispatch_kernel_kind([1], [1 << 70]) == KERNEL_RUST_U128
    assert dispatch_kernel_kind([], []) == KERNEL_RUST_U64
    # Bit 63 is still u64-fit; bit 64 is the first wide bit.
    assert dispatch_kernel_kind([1 << 63], [1 << 63]) == KERNEL_RUST_U64
    assert dispatch_kernel_kind([1 << 64], []) == KERNEL_RUST_U128


# ─────────────────────────────────────────────────────────────────
# The LOUD-FAIL law (DEC-ENUM-KERNEL-U128-001 — the #1549 inversion)
# ─────────────────────────────────────────────────────────────────


def test_free_wide_dispatch_raises_typed_error_when_kernel_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module missing ``count_supermasks_u128s`` REFUSES a wide call loudly.

    The typed, greppable :class:`WideKernelUnavailableError` names the DEC +
    the reason token + the rebuild remediation — never a silent numpy
    demotion of a >=64-bit grid sweep (PR-B's zero-fallback CI pin asserts
    exactly this exception class).
    """
    monkeypatch.delattr(fno_math, "count_supermasks_u128s")
    with pytest.raises(WideKernelUnavailableError) as excinfo:
        count_supermasks([_HI_BAR], [_HI_MASK])
    message = str(excinfo.value)
    assert "DEC-ENUM-KERNEL-U128-001" in message
    assert "wheel_missing_u128" in message
    assert "maturin develop --release" in message
    assert "count_supermasks_u128s" in message


def test_free_u64_dispatch_unaffected_by_missing_wide_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """<=64-bit calls never touch the wide kernel — they succeed without it."""
    monkeypatch.delattr(fno_math, "count_supermasks_u128s")
    assert count_supermasks([0b11, 0b111, 0b1000, 0], [0b11, 0b1000, 0]) == [2, 1, 4]


def test_wide_bar_construction_raises_typed_error_when_kernel_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wide-bar ``ResidentBarBuffer`` with no ``ResidentBarsU128`` refuses."""
    monkeypatch.delattr(fno_math, "ResidentBarsU128")
    with pytest.raises(WideKernelUnavailableError) as excinfo:
        ResidentBarBuffer([_HI_BAR, 0b1010])
    assert "ResidentBarsU128" in str(excinfo.value)
    assert "wheel_missing_u128" in str(excinfo.value)


def test_wide_mask_promotion_raises_typed_error_when_kernel_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wide MASK batch on a u64-resident window refuses without the twin.

    The buffer itself constructs fine (u64 bars) and keeps serving u64
    batches; only the >=64-bit batch raises — never a numpy demotion.
    """
    monkeypatch.delattr(fno_math, "ResidentBarsU128")
    buf = ResidentBarBuffer([0b111, 0b101])
    assert buf.count_supermasks([0b101]) == [2]  # u64 arm still serves
    with pytest.raises(WideKernelUnavailableError):
        buf.count_supermasks([1 << 70])
    assert buf.wide_kernel_active is False


def test_wide_kernel_error_is_its_own_greppable_class() -> None:
    """The error type contract PR-B pins against: RuntimeError subclass."""
    assert issubclass(WideKernelUnavailableError, RuntimeError)
    assert WideKernelUnavailableError.__name__ == "WideKernelUnavailableError"


# ─────────────────────────────────────────────────────────────────
# u64 path untouched — the wide kernel is NEVER invoked for <=64 inputs
# ─────────────────────────────────────────────────────────────────


class _Bomb:
    """Explodes on ANY use — proves the wide kernel is never touched."""

    def __call__(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("wide kernel invoked for a <=64-bit input")


def test_wide_kernel_never_invoked_for_u64_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bomb-spy: u64-fit flows never construct/call the u128 twins.

    With BOTH wide entry points replaced by bombs, the free dispatch and the
    resident buffer must serve every <=64-bit batch untouched — the byte-
    identical-u64-path law of DEC-ENUM-KERNEL-U128-001.
    """
    monkeypatch.setattr(fno_math, "count_supermasks_u128s", _Bomb())
    monkeypatch.setattr(fno_math, "ResidentBarsU128", _Bomb())
    # U64_MASK itself is the EXACT width boundary — a `>` -> `>=` mutant on
    # the width predicate would (wrongly) classify it wide and trip the bomb.
    bars = [0b111, 0b101, 0b010, 1 << 63, U64_MASK]
    masks = [0b101, 1 << 63, U64_MASK, 0]
    assert count_supermasks(bars, masks) == count_supermasks_reference(bars, masks)
    buf = ResidentBarBuffer(bars)
    assert buf.kernel_kind == KERNEL_RUST_U64
    assert buf.count_supermasks(masks) == count_supermasks_reference(bars, masks)
    # Supersets of 0b101: {0b111, 0b101, U64_MASK} = 3.
    assert buf.count_supermask(0b101) == 3
    assert buf.wide_kernel_active is False


# ─────────────────────────────────────────────────────────────────
# ResidentBarBuffer — width dispatch + promotion + observables
# ─────────────────────────────────────────────────────────────────


def test_resident_buffer_rust_path_for_u64_bars() -> None:
    """All-u64 bars build the u64 Rust resident kernel (fast path)."""
    rb = ResidentBarBuffer([0b111, 0b101, 0b010])
    assert rb.n_bars == 3
    assert rb.kernel_kind == KERNEL_RUST_U64
    assert rb.wide_kernel_active is False
    # Supersets of 0b101: {0b111,0b101}=2; of 0b010: {0b111,0b010}=2; of 0: all 3.
    assert rb.count_supermasks([0b101, 0b010, 0]) == [2, 2, 3]
    assert rb.count_supermask(0b101) == 2


def test_resident_buffer_wide_bars_build_u128_kernel() -> None:
    """A bar with a bit >= 64 builds the u128 Rust kernel (never numpy)."""
    bars = [_HI_BAR, 1 << 70, 0b1010]
    rb = ResidentBarBuffer(bars)
    assert rb.n_bars == 3
    assert rb.kernel_kind == KERNEL_RUST_U128
    assert rb.wide_kernel_active is True
    assert rb.count_supermasks([_HI_MASK, 1 << 70, 0]) == [1, 2, 3]


def test_resident_buffer_wide_bars_serve_u64_masks_via_wide_kernel() -> None:
    """A wide-bar window serves u64-fit batches on its resident u128 kernel."""
    bars = [_HI_BAR, 1 << 70, 0b1010]
    rb = ResidentBarBuffer(bars)
    masks = [0b1010, 0b10, 0]
    assert rb.count_supermasks(masks) == count_supermasks_reference(bars, masks)
    assert rb.count_supermasks([]) == []
    assert rb.kernel_kind == KERNEL_RUST_U128


def test_resident_buffer_rust_backed_promotes_on_wide_mask() -> None:
    """A u64-backed buffer asked for a u128 mask promotes to the wide kernel.

    The lazy promotion (the wide-promotion shape of #1549 — there via
    ``_derive_kernel_inputs``; here ``_promote_wide_kernel``): built at
    most once from the kept raw bars, announced with ONE
    ``combo_mask.wide_kernel_promoted`` INFO, memoised for later batches —
    and ``kernel_kind`` stays u64 (the u64 arm keeps serving u64 batches).
    """
    bars = [0b111, 0b101, 0b010]
    rb = ResidentBarBuffer(bars)
    assert rb.wide_kernel_active is False
    with structlog.testing.capture_logs() as logs:
        assert rb.count_supermasks([1 << 70, 0b101]) == [0, 2]
        assert rb.count_supermasks([1 << 71]) == [0]  # memoised — no rebuild
    promoted = [entry for entry in logs if entry["event"] == "combo_mask.wide_kernel_promoted"]
    assert len(promoted) == 1
    assert promoted[0]["n_bars"] == 3
    assert rb.wide_kernel_active is True
    assert rb.kernel_kind == KERNEL_RUST_U64  # fixed at construction
    # u64 batches still serve identically after the promotion.
    assert rb.count_supermasks([0b101, 0]) == [2, 3]


def test_resident_buffer_numpy_fallback_promotes_on_wide_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even the <=64-bit numpy-degraded buffer serves a wide batch via Rust.

    A stale module missing ``ResidentBars`` (the pre-existing u64
    construction degrade) but carrying ``ResidentBarsU128`` still promotes a
    >=64-bit batch onto the Rust wide kernel — numpy NEVER serves a wide
    batch (the LOUD-FAIL law's positive arm).
    """

    def _raise(_bars: object) -> object:
        raise AttributeError("ResidentBars unavailable in this wheel")

    monkeypatch.setattr("brutex.engine.combo_mask.fno_math.ResidentBars", _raise)
    bars = [0b111, 0b101, 0b010]
    rb = ResidentBarBuffer(bars)
    assert rb.kernel_kind == KERNEL_NUMPY
    assert rb.count_supermasks([1 << 70, 0b101]) == [0, 2]
    assert rb.wide_kernel_active is True


def test_resident_buffer_falls_back_when_rust_kernel_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The <=64-bit AttributeError construction degrade is BYTE-IDENTICAL.

    The pre-DEC seam preserved: when ``fno_math.ResidentBars`` cannot be
    constructed (an older/incompatible module missing the resident type), a
    u64-fit buffer degrades to the two-limb numpy backing and still serves
    bit-exact superset counts for u64-fit batches — no crash, no truncation.
    """

    def _raise(_bars: object) -> object:
        raise AttributeError("ResidentBars unavailable in this wheel")

    monkeypatch.setattr("brutex.engine.combo_mask.fno_math.ResidentBars", _raise)
    bars = [0b111, 0b101, 0b010]  # all fit u64 -> takes the try/except path
    rb = ResidentBarBuffer(bars)
    assert rb.kernel_kind == KERNEL_NUMPY
    assert rb.wide_kernel_active is False
    assert rb.n_bars == 3
    assert rb.count_supermasks([0b101, 0b010, 0]) == [2, 2, 3]


def test_resident_buffer_numpy_path_empty_masks_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A numpy-degraded buffer with no masks returns ``[]`` (early return)."""

    def _raise(_bars: object) -> object:
        raise AttributeError("ResidentBars unavailable in this wheel")

    monkeypatch.setattr("brutex.engine.combo_mask.fno_math.ResidentBars", _raise)
    rb = ResidentBarBuffer([0b101, 0b111])
    assert rb.kernel_kind == KERNEL_NUMPY
    assert rb.count_supermasks([]) == []


def test_resident_buffer_numpy_path_empty_window_returns_zeros(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A numpy-degraded EMPTY buffer returns one ``0`` per u64-fit mask."""

    def _raise(_bars: object) -> object:
        raise AttributeError("ResidentBars unavailable in this wheel")

    monkeypatch.setattr("brutex.engine.combo_mask.fno_math.ResidentBars", _raise)
    rb = ResidentBarBuffer([])
    assert rb.n_bars == 0
    assert rb.kernel_kind == KERNEL_NUMPY
    assert rb.count_supermasks([0b1, 0b11, 0]) == [0, 0, 0]


def test_resident_buffer_empty_window_wide_mask_served_by_wide_kernel() -> None:
    """An empty u64-built window promoted by a wide mask counts 0 per mask."""
    rb = ResidentBarBuffer([])
    assert rb.n_bars == 0
    assert rb.count_supermasks([1 << 70, 0b1, 0]) == [0, 0, 0]
    assert rb.wide_kernel_active is True


def test_resident_buffer_defensive_limb_rebuild_totality_arm() -> None:
    """The numpy tail's rebuild arm is a covered defensive TOTALITY arm.

    Structurally unreachable today (every construction arm leaving both
    kernels ``None`` stores the limbs) — driven here by hand-mutating an
    instance, pinning the fail-soft rebuild-from-raw behaviour rather than a
    crash (the house direct-unit-covered totality-arm idiom).
    """

    rb = ResidentBarBuffer([0b111, 0b101, 0b010])
    rb._rust = None  # hand-mutate into the impossible state
    rb._bar_lo = None
    rb._bar_hi = None
    assert rb.count_supermasks([0b101, 0]) == [2, 3]


# ─────────────────────────────────────────────────────────────────
# Beyond-u128 / negative — the preserved OverflowError refusal
# ─────────────────────────────────────────────────────────────────


def test_beyond_u128_raises_overflow_error_free_dispatch() -> None:
    """A value past bit 127 refuses with ``OverflowError`` at the FFI edge.

    The SAME exception class the numpy ``uint64`` hi-limb cast raised for
    this shape pre-DEC — no triple-limb path is invented (both the kernel
    and the oracle refuse beyond-u128 identically).
    """
    with pytest.raises(OverflowError):
        count_supermasks([1 << 128], [1])
    with pytest.raises(OverflowError):
        count_supermasks([1], [U128_MASK + 1])
    # The numpy oracle refuses the same shape with the same class.
    with pytest.raises(OverflowError):
        count_supermasks_reference([1 << 128], [1])


def test_beyond_u128_raises_overflow_error_resident_buffer() -> None:
    """Beyond-u128 bars/masks refuse with ``OverflowError`` on the buffer too."""
    with pytest.raises(OverflowError):
        ResidentBarBuffer([1 << 130])
    rb = ResidentBarBuffer([0b101])
    with pytest.raises(OverflowError):
        rb.count_supermasks([(1 << 129) | 1])


def test_negative_mask_raises_overflow_error() -> None:
    """A negative value (API misuse) refuses at the u64 FFI edge as before."""
    with pytest.raises(OverflowError):
        count_supermasks([1], [-1])


# ─────────────────────────────────────────────────────────────────
# Cross-view parity
# ─────────────────────────────────────────────────────────────────


def test_three_view_u128_parity() -> None:
    """numpy two-limb oracle == production count == Python-int scalar, on u128.

    The core parity oracle extended to 128 bits: for a set of u128
    bars/masks the vectorised reference, the production ``count_supermasks``
    (the u128 Rust kernel), and the scalar ``(bar & mask) == mask`` all
    agree.
    """
    bars = [_HI_BAR, _HI_MASK, (1 << 100) | 1, 0]
    masks = [_HI_MASK, 1 << 100, 1, 0]
    ref = count_supermasks_reference(bars, masks)
    prod = count_supermasks(bars, masks)
    scalar = [sum(1 for b in bars if evaluate_mask(b, m)) for m in masks]
    assert ref == prod == scalar
