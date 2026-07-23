"""``BRUTEX_META_GATE`` flag + the Rust meta-kernel facade
(DEC-META-LABEL-ANNOTATE-001 + DEC-ABSTAIN-GATE-STREAM-001).

The optional ``brutex_meta`` wheel (``rust/brutex-meta``) carries the two
O(1)-per-element inference kernels — the logistic P(win) scorer and the
abstention reason-bitmask gate. This module owns everything about *whether
and how* an annotation pass may consult them (the ``feed_rust.py`` facade
template + the lake facades' day-one burn-in posture):

* :func:`meta_gate_enabled` — the default-OFF opt-in flag (Pattern A: ONLY
  an explicit truthy enables; unset/empty/falsy are OFF; non-empty junk is
  OFF + one warn).
* :func:`score_probabilities` / :func:`abstain_reason_masks` — the routed
  entries: flag OFF (the default) serves the retained Python oracles
  (:func:`brutex.meta.logistic.score_logistic`,
  :func:`brutex.meta.abstain.abstain_masks_python`) and NEVER imports the
  crate (bomb-crate spy-pinned); flag ON resolves the wheel lazily
  (``wheel_missing`` / ``wheel_stale`` warn-once degrades), refuse-poison
  validates every kernel output, and bit-compares the first
  :data:`BURN_IN_CALLS` calls per kernel per process against the oracle —
  ANY mismatch ships the ORACLE result, latches the facade OFF for the
  process and logs ONE :data:`META_RUST_AUTOREVERT_EVENT` ERROR (the
  ship-the-oracle autorevert law).
* Narrow degrade set → oracle with a capped warn-once per reason;
  ``MemoryError`` / ``KeyboardInterrupt`` / ``SystemExit`` always re-raise
  RAW (global stops are never swallowed).
* :func:`reset_meta_rust_for_tests` — the test isolation seam.

NOT a leaf: this module imports ``env_flag_tristate`` from
``brutex.engine.sweep_lookahead`` (the ONE flag-vocabulary home — verified
cycle-free: ``sweep_lookahead`` → ``brutex.feed.lookahead`` never reaches
``brutex.meta``), per the leaf rule the ``feed_rust.py`` docstring records
(only a module imported BY the engine's own import chain must inline the
vocabulary; this one is not).
"""

from __future__ import annotations

import math
import os
import struct
from typing import TYPE_CHECKING, Final

import structlog

from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.meta.abstain import ABSTAIN_REASON_ORDER, abstain_masks_python
from brutex.meta.logistic import score_logistic as _score_logistic_oracle

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence

_LOG = structlog.get_logger(__name__)

__all__ = [
    "BURN_IN_CALLS",
    "META_GATE_ENV",
    "META_RUST_AUTOREVERT_EVENT",
    "META_RUST_FALLBACK_EVENT",
    "META_RUST_FLAG_EVENT",
    "abstain_reason_masks",
    "meta_gate_enabled",
    "reset_meta_rust_for_tests",
    "score_probabilities",
]

#: The environment flag (default ON per DEC-RUST-DEFAULT-ON-WAVE-001;
#: falsy is the explicit kill switch).
META_GATE_ENV: Final[str] = "BRUTEX_META_GATE"

#: ONE structlog event per degrade reason per process (``reason=`` field).
META_RUST_FALLBACK_EVENT: Final[str] = "meta_rust.fallback"

#: ONE structlog event per process for a non-empty unrecognised flag value.
META_RUST_FLAG_EVENT: Final[str] = "meta_rust.flag_unrecognised"

#: The burn-in mismatch ERROR — the process latches OFF after it.
META_RUST_AUTOREVERT_EVENT: Final[str] = "meta_rust.autoreverted"

#: Bit-compared calls per kernel per process before the kernel serves alone
#: (the #1531/#1532 burn-in law, day-one per the lake-facade posture).
BURN_IN_CALLS: Final[int] = 16

#: The highest legal abstain bitmask (5 reason bits).
_MAX_ABSTAIN_MASK: Final[int] = (1 << len(ABSTAIN_REASON_ORDER)) - 1

#: Degrade-to-oracle exception set (narrow — a genuine code bug outside it
#: propagates; MemoryError/KeyboardInterrupt/SystemExit re-raise raw above).
_DEGRADE_ERRORS: Final[tuple[type[BaseException], ...]] = (
    ValueError,
    RuntimeError,
    TypeError,
    OverflowError,
    AttributeError,
)

#: Warn-once registry (reasons already logged this process).
_WARNED: Final[set[str]] = set()

#: Mutable facade state: per-kernel burn-in progress + the process latch.
_STATE: Final[dict[str, int | bool]] = {
    "score_burn_in": 0,
    "abstain_burn_in": 0,
    "latched": False,
}


def reset_meta_rust_for_tests() -> None:
    """Clear the warn-once registry, burn-in counters and latch (tests)."""
    _WARNED.clear()
    _STATE["score_burn_in"] = 0
    _STATE["abstain_burn_in"] = 0
    _STATE["latched"] = False


def _warn_once(reason: str, **fields: object) -> None:
    """Log ONE :data:`META_RUST_FALLBACK_EVENT` per *reason* per process."""
    # Unlocked check-then-add (the feed_rust precedent): a racing thread can
    # at worst double-log one line — never a storm, never a miss.
    if reason in _WARNED:
        return
    _WARNED.add(reason)
    _LOG.warning(META_RUST_FALLBACK_EVENT, reason=reason, **fields)


def meta_gate_enabled(raw: str | None = None) -> bool:
    """Return whether the Rust meta kernels are ENABLED (default ``True``).

    Via the COMMON :func:`env_flag_tristate`: a truthy literal enables, a
    falsy literal is the explicit kill switch, unset/whitespace-empty falls
    to the default — ON per DEC-RUST-DEFAULT-ON-WAVE-001 (operator
    directive 2026-07-23). A NON-EMPTY unrecognised value gets one
    :data:`META_RUST_FLAG_EVENT` warning per process and fails SAFE to OFF
    (an operator typo is surfaced, never silently armed).
    """
    value = os.environ.get(META_GATE_ENV) if raw is None else raw
    if value is None:
        return True
    verdict = env_flag_tristate(value)
    if verdict is not None:
        return verdict
    if value.strip():
        if META_RUST_FLAG_EVENT not in _WARNED:
            _WARNED.add(META_RUST_FLAG_EVENT)
            # The env VALUE is operator-typed junk — repr() it (the #907 law).
            _LOG.warning(META_RUST_FLAG_EVENT, env=META_GATE_ENV, value=repr(value))
        return False
    return True


def _resolve_kernel() -> object | None:
    """Import the ``brutex_meta`` wheel lazily, or ``None`` (warn-once).

    A missing wheel (ImportError — including the shim's own stale-wheel
    handshake refusal — or the AttributeError shape a partially initialised
    module can raise) degrades as ``wheel_missing``; an importable wheel
    missing either kernel symbol degrades as ``wheel_stale``.
    """
    import importlib

    try:
        compiled = importlib.import_module("brutex_meta")
    except (ImportError, AttributeError):
        _warn_once("wheel_missing")
        return None
    if getattr(compiled, "score_logistic", None) is None or (
        getattr(compiled, "abstain_masks", None) is None
    ):
        _warn_once("wheel_stale")
        return None
    return compiled


def _latch_autorevert(kernel_name: str) -> None:
    """Latch the facade OFF for the process + log ONE ERROR (autorevert)."""
    _STATE["latched"] = True
    _LOG.error(META_RUST_AUTOREVERT_EVENT, kernel=kernel_name)


def _bits(values: Sequence[float]) -> bytes:
    """Bit-exact packing for the burn-in compare (signed zeros included)."""
    return struct.pack(f"<{len(values)}d", *values)


def score_probabilities(
    features: Sequence[float],
    n_features: int,
    weights: Sequence[float],
    bias: float,
) -> list[float]:
    """P(win) per row of a flattened row-major feature batch — the routed entry.

    Flag OFF (default) / wheel absent / latched: the Python oracle serves
    (bit-comparable by construction). Flag ON: the Rust kernel serves after
    refuse-poison validation, bit-compared against the oracle for the first
    :data:`BURN_IN_CALLS` calls per process (mismatch → ship the ORACLE +
    latch + one ERROR). Structural input junk raises ``ValueError`` from
    the ORACLE on every path (the caller's contract is arm-independent).
    """
    if not meta_gate_enabled() or _STATE["latched"]:
        return _score_logistic_oracle(features, n_features, weights, bias)
    kernel = _resolve_kernel()
    if kernel is None:
        return _score_logistic_oracle(features, n_features, weights, bias)
    try:
        rust = kernel.score_logistic(list(features), n_features, list(weights), bias)  # type: ignore[attr-defined]
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except _DEGRADE_ERRORS as exc:
        _warn_once("kernel_error", kernel="score_logistic", error=repr(str(exc)))
        return _score_logistic_oracle(features, n_features, weights, bias)
    n_rows = len(features) // n_features if n_features else 0
    if not _valid_probabilities(rust, n_rows, features, weights, bias):
        return _score_logistic_oracle(features, n_features, weights, bias)
    result = [float(p) for p in rust]
    burn_in = int(_STATE["score_burn_in"])
    if burn_in < BURN_IN_CALLS:
        oracle = _score_logistic_oracle(features, n_features, weights, bias)
        if _bits(oracle) != _bits(result):
            _latch_autorevert("score_logistic")
            return oracle
        _STATE["score_burn_in"] = burn_in + 1
    return result


def _valid_probabilities(
    result: object,
    n_rows: int,
    features: Sequence[float],
    weights: Sequence[float],
    bias: float,
) -> bool:
    """Refuse-poison gate over a scorer result (the ``validate_scan`` law).

    Structural laws (proven for well-formed inputs by the crate's own
    suite — a violation can only mean a stale/corrupt wheel): a list of
    floats, one per row; and when EVERY input is finite, every probability
    is finite and inside ``[0, 1]``. Any violation emits one
    ``kernel_invalid`` warn and the caller degrades to the oracle.
    """
    if not isinstance(result, list) or len(result) != n_rows:
        _warn_once("kernel_invalid", kernel="score_logistic", detail="result_shape")
        return False
    if not all(isinstance(p, float) for p in result):
        _warn_once("kernel_invalid", kernel="score_logistic", detail="result_types")
        return False
    inputs_finite = (
        math.isfinite(bias)
        and all(math.isfinite(v) for v in features)
        and all(math.isfinite(v) for v in weights)
    )
    if inputs_finite and not all(math.isfinite(p) and 0.0 <= p <= 1.0 for p in result):
        _warn_once("kernel_invalid", kernel="score_logistic", detail="probability_range")
        return False
    return True


def abstain_reason_masks(
    regimes: Sequence[int],
    allowed_mask: int,
    vix: Sequence[float],
    vix_lo: float,
    vix_hi: float,
    event_flags: Sequence[int],
    block_event_days: bool,
    confidence: Sequence[float],
    tau: float,
) -> list[int]:
    """Abstention reason bitmask per element — the routed entry.

    The same routing law as :func:`score_probabilities` (flag OFF → the
    Python oracle :func:`brutex.meta.abstain.abstain_masks_python`; flag ON
    → the Rust kernel with refuse-poison validation + per-process burn-in
    exact-compare + ship-the-oracle autorevert). Integer masks compare
    EXACTLY (no float packing needed).
    """
    if not meta_gate_enabled() or _STATE["latched"]:
        return abstain_masks_python(
            list(regimes),
            allowed_mask,
            list(vix),
            vix_lo,
            vix_hi,
            list(event_flags),
            block_event_days,
            list(confidence),
            tau,
        )
    kernel = _resolve_kernel()
    if kernel is None:
        return abstain_masks_python(
            list(regimes),
            allowed_mask,
            list(vix),
            vix_lo,
            vix_hi,
            list(event_flags),
            block_event_days,
            list(confidence),
            tau,
        )
    try:
        rust = kernel.abstain_masks(  # type: ignore[attr-defined]
            list(regimes),
            allowed_mask,
            list(vix),
            vix_lo,
            vix_hi,
            list(event_flags),
            block_event_days,
            list(confidence),
            tau,
        )
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except _DEGRADE_ERRORS as exc:
        _warn_once("kernel_error", kernel="abstain_masks", error=repr(str(exc)))
        return abstain_masks_python(
            list(regimes),
            allowed_mask,
            list(vix),
            vix_lo,
            vix_hi,
            list(event_flags),
            block_event_days,
            list(confidence),
            tau,
        )
    if not _valid_masks(rust, len(regimes)):
        return abstain_masks_python(
            list(regimes),
            allowed_mask,
            list(vix),
            vix_lo,
            vix_hi,
            list(event_flags),
            block_event_days,
            list(confidence),
            tau,
        )
    result = list(rust)
    burn_in = int(_STATE["abstain_burn_in"])
    if burn_in < BURN_IN_CALLS:
        oracle = abstain_masks_python(
            list(regimes),
            allowed_mask,
            list(vix),
            vix_lo,
            vix_hi,
            list(event_flags),
            block_event_days,
            list(confidence),
            tau,
        )
        if oracle != result:
            _latch_autorevert("abstain_masks")
            return oracle
        _STATE["abstain_burn_in"] = burn_in + 1
    return result


def _valid_masks(result: object, n: int) -> bool:
    """Refuse-poison gate over an abstain result (bytes of ≤ 5-bit masks)."""
    if not isinstance(result, (bytes, list)) or len(result) != n:
        _warn_once("kernel_invalid", kernel="abstain_masks", detail="result_shape")
        return False
    values = list(result)
    if not all(isinstance(m, int) and 0 <= m <= _MAX_ABSTAIN_MASK for m in values):
        _warn_once("kernel_invalid", kernel="abstain_masks", detail="mask_range")
        return False
    return True
