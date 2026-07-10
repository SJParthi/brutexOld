"""Rust-backed Apriori frontier seam — the native-kernel alternative arm.

DEC-SWEEP-APRIORI-RUST-001 (Slice-1b of the ``rust/fno-math`` sweep-core
conversion). This module is the thin Python seam over the native-Rust Apriori
frontier kernel (``fno_math.apriori_frontier_u64``,
``rust/fno-math/src/sweep_apriori_core.rs``): it validates inputs, calls the
kernel, emits the per-level census, and raises the frozen typed errors — so the
Rust arm is **bit-for-bit identical** to the FROZEN Python oracle
:func:`brutex.engine.sweep_apriori.generate_apriori_candidates`
(DEC-SWEEP-APRIORI-TUPLE-001).

## The composed, doubly-default-OFF gate

The Rust arm engages iff BOTH:

* ``BRUTEX_SWEEP_APRIORI`` is truthy (the Apriori path is on at all —
  :func:`brutex.engine.sweep_apriori.apriori_enabled`), AND
* ``BRUTEX_SWEEP_RUST_APRIORI`` is truthy (this native-kernel arm is on —
  :func:`apriori_rust_enabled`; the design §5 tristate arm flag, fail-SAFE OFF +
  warn-once on an unrecognised value via the COMMON
  :func:`brutex.engine.sweep_lookahead.env_flag_tristate`).

With EITHER flag off the caller keeps its existing path byte-identically: the
frozen Python Apriori generator (flag 1 on, flag 2 off) or the brute-force
enumeration (flag 1 off). :func:`apriori_use_rust` is the ONE composed
predicate the caller consults.

## Single-source parity discipline

Every EXACT-message / EXACT-type surface is single-sourced from the FROZEN
module: the ``>= 64``-bit u128 refusal (:func:`~brutex.engine.sweep_apriori._raise_unsupported_bit`),
the deterministic mask·bar breaker error
(:func:`~brutex.engine.sweep_apriori._raise_budget`), the per-level census
(:func:`~brutex.engine.sweep_apriori._emit_census`), the default mask·bar cap
resolution (:func:`~brutex.engine.sweep_apriori.resolve_max_mask_bars`), and the
incremental-check stride (``_BUDGET_CHECK_STRIDE``). The remaining validation
``ValueError`` messages are replicated here and PINNED byte-equal to the frozen
generator by a golden (a bad input raises the identical message + type on both
paths).

## Native counting — the injected ``count_hits`` is intentionally not called

The kernel computes superset hit-counts natively (``(bar & mask) == mask``,
bit-identical to :func:`brutex.engine.combo_mask.count_supermasks` — the frozen
default counter). It therefore does NOT invoke an injected ``count_hits``
callback; the parameter is accepted for drop-in signature compatibility with the
frozen generator. In production the injected counter IS that superset counter
(``run_persisted_sweep._count_hits`` — the resident kernel / ``count_supermasks``),
so native counting is bit-identical (the injected-counter strict-``zip`` contract
is a Python-generator-specific surface with no Rust analogue).

## Honest u64 envelope

The kernel operates on ``u64`` masks + a ``u64`` mask·bar budget. When the
resolved ``max_mask_bars`` or ``n_bars`` exceeds ``2**64 - 1`` this seam
transparently DELEGATES to the frozen Python generator (arbitrary-precision
integers), so parity is exact inside the ``u64`` envelope and honestly delegated
beyond it. The ``< 64``-bit condition guard (raised BEFORE any counting) keeps
the 60-bit production vocabulary always inside the envelope.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from typing import Final

import structlog

from brutex.engine import sweep_apriori as _apriori
from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.vocabulary.conflicts import CONFLICT_PAIRS

__all__ = [
    "SWEEP_RUST_APRIORI_ENV",
    "apriori_rust_enabled",
    "apriori_use_rust",
    "generate_apriori_candidates_rust",
]

_LOG = structlog.get_logger(__name__)

#: The tri-state env flag enabling the native-Rust Apriori arm (default OFF).
#: Truthy literals (``1/true/yes/on``, case-insensitive) enable it; an
#: unrecognised value is a fail-SAFE OFF + a one-time WARN (the design §5 arm
#: posture, the sibling of ``BRUTEX_SWEEP_RUST_ENUM`` / ``BRUTEX_SWEEP_RUST_CORE``).
SWEEP_RUST_APRIORI_ENV: Final[str] = "BRUTEX_SWEEP_RUST_APRIORI"

#: The single ``uint64`` limb width — the maximum (exclusive) condition-bit
#: index the ``u64`` frontier can represent. Mirrors the frozen module's
#: ``_U64_BITS`` (a stable, never-monkeypatched constant kept local so the
#: validation messages carry no cross-module private access).
_U64_BITS: Final[int] = 64

#: The largest ``u64`` value — the kernel's mask·bar-budget + n_bars envelope.
#: Beyond it this seam delegates to the frozen big-int Python generator.
_U64_MAX: Final[int] = (1 << 64) - 1

#: Process-once guard for the arm-flag unrecognised-value WARN (fail-safe OFF,
#: warned exactly once per process so a mistyped value is visible without a log
#: storm). A single-key mutable dict (not a bare ``global`` rebind) — the
#: read/write race is benign (at most one extra WARN), never a correctness
#: surface. Tests reset ``_ARM_FLAG_WARN_STATE["warned"] = False``.
_ARM_FLAG_WARN_STATE: dict[str, bool] = {"warned": False}


def apriori_rust_enabled() -> bool:
    """Return ``True`` iff ``BRUTEX_SWEEP_RUST_APRIORI`` is set to a truthy literal.

    Truthy literals are ``1`` / ``true`` / ``yes`` / ``on`` (case-insensitive,
    surrounding whitespace ignored). An UNSET / EMPTY value is OFF silently; an
    unrecognised value is OFF plus a one-time ``sweep.apriori.rust.flag_unrecognised``
    WARN (fail-SAFE default — the native arm never silently engages on a typo).
    Classification rides the COMMON
    :func:`brutex.engine.sweep_lookahead.env_flag_tristate`.

    Returns
    -------
    bool
        Whether the native-Rust Apriori arm is enabled for this process.
    """
    raw = os.environ.get(SWEEP_RUST_APRIORI_ENV)
    if raw is None:
        return False
    verdict = env_flag_tristate(raw)
    if verdict is True:
        return True
    if verdict is None and not _ARM_FLAG_WARN_STATE["warned"]:
        # Unrecognised (neither truthy nor falsy, non-empty) → fail-safe OFF,
        # warned exactly once so the operator sees the mistype.
        _ARM_FLAG_WARN_STATE["warned"] = True
        _LOG.warning(
            "sweep.apriori.rust.flag_unrecognised",
            env=SWEEP_RUST_APRIORI_ENV,
            value=repr(raw),
            resolved="off",
        )
    return False


def apriori_use_rust() -> bool:
    """Return ``True`` iff BOTH the Apriori path AND the native-Rust arm are on.

    The ONE composed gate the caller consults: ``BRUTEX_SWEEP_APRIORI`` truthy
    (:func:`brutex.engine.sweep_apriori.apriori_enabled`) AND
    ``BRUTEX_SWEEP_RUST_APRIORI`` truthy (:func:`apriori_rust_enabled`). Default
    OFF twice over — with either flag off the caller keeps its byte-identical
    Python (frozen-generator) or brute-force path.
    """
    return _apriori.apriori_enabled() and apriori_rust_enabled()


def _conflict_pair_masks() -> list[int]:
    """The ``CONFLICT_PAIRS`` as pre-packed ``(1 << a) | (1 << b)`` masks.

    The kernel's ``has_conflict`` is a superset test against these two-bit masks
    — the exact ``brutex.vocabulary.conflicts.has_conflict`` semantics, with the
    pairs injected as DATA (never re-derived Rust-side).
    """
    return [(1 << a) | (1 << b) for a, b in CONFLICT_PAIRS]


def generate_apriori_candidates_rust(
    bar_bits: Sequence[int],
    *,
    condition_bits: Sequence[int],
    k: int,
    min_hits: int,
    count_hits: Callable[[Sequence[int]], list[int]] | None = None,
    n_bars: int | None = None,
    root_mod: tuple[int, int] = (0, 1),
    max_mask_bars: int | None = None,
    cell_label: str = "",
) -> list[int]:
    """Native-Rust drop-in for :func:`~brutex.engine.sweep_apriori.generate_apriori_candidates`.

    Bit-for-bit identical to the frozen Python generator for every input inside
    the ``u64`` envelope: same pruned survivor set, same lexicographic-by-position
    emission order, same per-level census events, and the same deterministic
    mask·bar breaker trip (decision AND reported count). Signature-identical to
    the frozen generator so it is a genuine drop-in; ``count_hits`` is accepted
    but not invoked (the kernel counts supersets natively — see the module
    docstring).

    Parameters mirror the frozen generator exactly. Validation, the ``>= 64``-bit
    u128 refusal, the census, and the breaker error are single-sourced from the
    frozen module. Beyond the ``u64`` envelope (``max_mask_bars`` or ``n_bars``
    exceeding ``2**64 - 1``) this transparently delegates to the frozen Python
    generator.

    Returns
    -------
    list[int]
        The pruned, shard-owned level-``k`` masks in lexicographic-by-position
        order — identical to the frozen generator's output.

    Raises
    ------
    ValueError
        A condition bit is negative, ``k`` is out of range, ``root_mod`` is
        malformed, ``n_bars`` is negative, or an explicit ``max_mask_bars`` is
        ``<= 0`` — the identical caller-contract messages the frozen generator
        raises (golden-pinned byte-equal).
    brutex.engine.sweep_run.SweepAprioriUnsupportedError
        A condition bit is ``>= 64`` (the frozen u128 refusal, reused verbatim).
    brutex.engine.sweep_run.SweepAprioriBudgetError
        The mask·bar product strictly exceeds ``max_mask_bars`` (the frozen
        breaker error, reused verbatim with the identical count).
    """
    unique_bits = sorted(set(condition_bits))
    for bit in unique_bits:
        if bit < 0:
            raise ValueError(
                f"generate_apriori_candidates: condition bit {bit} is negative "
                f"(out of range [0, {_U64_BITS}))"
            )
        if bit >= _U64_BITS:
            # Single-sourced frozen u128 refusal (SweepAprioriUnsupportedError,
            # a SweepComboError subclass → graceful TUPLE_FAILED).
            _apriori._raise_unsupported_bit(bit)  # pyright: ignore[reportPrivateUsage]
    n = len(unique_bits)
    if not 0 <= k <= n:
        raise ValueError(
            f"generate_apriori_candidates: invalid k={k} for {n} unique bits (require 0 <= k <= n)"
        )
    w, big_w = root_mod
    if big_w < 1 or not 0 <= w < big_w:
        raise ValueError(
            f"generate_apriori_candidates: root_mod {root_mod!r} invalid "
            f"(require W >= 1 and 0 <= w < W)"
        )
    resolved_n_bars = len(bar_bits) if n_bars is None else n_bars
    if resolved_n_bars < 0:
        raise ValueError(f"generate_apriori_candidates: n_bars must be >= 0, got {resolved_n_bars}")
    limit = _apriori.resolve_max_mask_bars() if max_mask_bars is None else max_mask_bars
    if limit <= 0:
        raise ValueError(f"generate_apriori_candidates: max_mask_bars must be > 0, got {limit}")

    # Honest envelope: the kernel takes a u64 budget + u64 n_bars. Beyond it,
    # delegate to the frozen big-int generator (parity exact inside u64, honest
    # delegation outside). Validation above already ran identically, so the
    # delegated call re-validates harmlessly (idempotent, same messages).
    if limit > _U64_MAX or resolved_n_bars > _U64_MAX:
        return _apriori.generate_apriori_candidates(
            bar_bits,
            condition_bits=condition_bits,
            k=k,
            min_hits=min_hits,
            count_hits=count_hits,
            n_bars=n_bars,
            root_mod=root_mod,
            max_mask_bars=max_mask_bars,
            cell_label=cell_label,
        )

    # Lazy import so a Python-only run (either flag off) never touches the wheel.
    import fno_math

    masks, census, budget_trip = fno_math.apriori_frontier_u64(
        list(bar_bits),
        unique_bits,
        k,
        min_hits,
        resolved_n_bars,
        w,
        big_w,
        limit,
        _conflict_pair_masks(),
        # Read dynamically (never import-bound) so a test monkeypatch of the
        # frozen stride is honoured — the trip DECISION is stride-invariant, but
        # the reported running count follows the stride, matching the oracle.
        _apriori._BUDGET_CHECK_STRIDE,  # pyright: ignore[reportPrivateUsage]
    )

    # Emit the pre-trip census records via the frozen emitter (identical events).
    for level, candidates, frequent in census:
        _apriori._emit_census(  # pyright: ignore[reportPrivateUsage]
            cell_label=cell_label,
            k=k,
            level=level,
            candidates=candidates,
            min_hits=min_hits,
            n_bars=resolved_n_bars,
            frequent=(None if frequent < 0 else frequent),
        )

    if budget_trip is not None:
        # Single-sourced frozen breaker error (the seam owns the exact message;
        # the count is the kernel's — the running strided count or |C_j|).
        _apriori._raise_budget(  # pyright: ignore[reportPrivateUsage]
            cell_label, k, budget_trip, resolved_n_bars, limit
        )

    return masks
