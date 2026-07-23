"""Abstention-first gate vocabulary + the pure reason function
(DEC-ABSTAIN-GATE-STREAM-001 — ANNOTATION-ONLY today).

The exact structural template of ``brutex/ranking/__init__.py``'s
``GATE_REASON_ORDER`` + ``gate_reasons`` (the stable-wire-vocabulary law): a
pinned ordered token tuple, per-door token constants, and a pure
reason-returning function co-located with its predicate — both reading the
same constants, with the property ``passes ⇔ reasons == ()`` holding for
EVERY input (property-pinned).

ANNOTATION-FIRST law (the operator's 2026-07-18 winning-completeness
ruling): every judgment this module produces lands in ``.meta.json``
sidecars — NOTHING is ever blocked, skipped, or re-ranked anywhere in the
product today. The reasons are OBSERVATIONS ("this trade WOULD have been
abstained under this illustrative config"), and a gated stream must be
re-validated as its OWN strategy (fewer trades ⇒ the MIN_TRADES=30 floor +
the DSR effective-N interaction — disclosed in the DEC) before any gating
flip, which needs its own DEC + operator ruling.

The dormant IN-SIM arm: ``BRUTEX_ABSTAIN_GATE_INSIM`` is a DESIGNED but
REFUSED knob (the ``BRUTEX_FILL_MODE`` set-knob-refuses precedent,
:mod:`brutex.execution.fill_mode`): a launch that finds it set — to ANY
non-blank value — raises :class:`AbstainGateInSimRefusedError` naming the
DEC, because the walker composition (the ``abstain_gate=None`` ctor hook of
``docs/design/meta/ABSTAIN_INSIM_DESIGN.md``) lands only AFTER W4's PR-B
dual-feed walker and an explicit operator gate-flip ruling.

STDLIB-ONLY LEAF (the ``fill_mode.py`` discipline): imported by the CLI's
eager sweep-launch resolution, so it must never pull numpy/polars/structlog
into that path.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

__all__ = [
    "ABSTAIN_EVENT_DAY",
    "ABSTAIN_GATE_INSIM_ENV",
    "ABSTAIN_LOW_CONFIDENCE",
    "ABSTAIN_REASON_ORDER",
    "ABSTAIN_REGIME_MISMATCH",
    "ABSTAIN_VIX_MISSING",
    "ABSTAIN_VIX_OUT_OF_BAND",
    "REGIME_CODES",
    "AbstainGateConfig",
    "AbstainGateInSimRefusedError",
    "abstain_masks_python",
    "abstain_passes",
    "abstain_reasons",
    "default_abstain_config",
    "reasons_from_mask",
    "require_abstain_gate_insim_unset",
]

# ── The stable reason vocabulary (mirrors GATE_REASON_ORDER's law) ──────────

#: Door 1 — the trade's PRIOR-session trend regime is not in ``allowed_regimes``.
ABSTAIN_REGIME_MISMATCH: Final[str] = "regime_mismatch"
#: Door 2 — VIX stamped and outside the configured INCLUSIVE ``[lo, hi]``.
ABSTAIN_VIX_OUT_OF_BAND: Final[str] = "vix_out_of_band"
#: Door 3 — a VIX gate is configured but the entry stamp is missing. In the
#: annotation-only stream this is a COUNTED observation (nothing is blocked
#: anywhere); a future in-sim flip must decide the missing-stamp policy
#: under its own DEC.
ABSTAIN_VIX_MISSING: Final[str] = "vix_missing"
#: Door 4 — a verified event day (any :data:`brutex.engine.session_lens.EVENT_FLAGS`
#: class) under ``block_event_days``.
ABSTAIN_EVENT_DAY: Final[str] = "event_day"
#: Door 5 — a τ gate is configured and ``p_win >= tau`` is not certified
#: (missing p_win included — the model cannot certify the bound; fail-closed,
#: the abstention-first posture).
ABSTAIN_LOW_CONFIDENCE: Final[str] = "low_confidence"

#: The COMPLETE reason vocabulary in EVALUATION order — the stable wire
#: tokens (the ``GATE_REASON_ORDER`` law: consumers may rely on this order).
#: Index in this tuple == the Rust kernel's bitmask BIT position
#: (``rust/brutex-meta/src/abstain.rs`` — mirror-pinned by tests).
ABSTAIN_REASON_ORDER: Final[tuple[str, ...]] = (
    ABSTAIN_REGIME_MISMATCH,
    ABSTAIN_VIX_OUT_OF_BAND,
    ABSTAIN_VIX_MISSING,
    ABSTAIN_EVENT_DAY,
    ABSTAIN_LOW_CONFIDENCE,
)

#: The documented regime → FFI code map (the ONE map both sides pin;
#: ``rust/brutex-meta/src/abstain.rs`` documents the same values). Keys are
#: the :class:`brutex.engine.session_lens.TrendRegime` values plus the
#: lens-miss token ``"unknown"``.
REGIME_CODES: Final[Mapping[str, int]] = {
    "up": 0,
    "down": 1,
    "sideways": 2,
    "unknown": 3,
}

#: The FFI regime-code width (the Rust kernel's u64 allowed-mask bit count).
_REGIME_CODE_BITS: Final[int] = 64

#: All regimes allowed — the DEFAULT (no regime restriction).
_ALL_REGIMES: Final[frozenset[str]] = frozenset(REGIME_CODES)

#: The default illustrative confidence threshold (τ) — the mid-grid value
#: the annotation echoes; NOT a validated trading parameter (the sidecar's
#: ``revalidation_required`` flag is unconditional).
DEFAULT_TAU: Final[float] = 0.7


@dataclass(frozen=True, slots=True)
class AbstainGateConfig:
    """One abstention-gate configuration (frozen, validated at construction).

    ``None`` disables a door: ``vix_lo``/``vix_hi`` ``None`` = that band side
    unbounded (both ``None`` = no VIX gate at all); ``tau`` ``None`` = no
    confidence gate. ``allowed_regimes`` must be a non-empty subset of
    :data:`REGIME_CODES`' keys (fail-loud on junk tokens — a typo'd regime
    would silently abstain everything).
    """

    allowed_regimes: frozenset[str]
    vix_lo: float | None
    vix_hi: float | None
    block_event_days: bool
    tau: float | None

    def __post_init__(self) -> None:
        unknown = sorted(self.allowed_regimes - set(REGIME_CODES))
        if unknown:
            raise ValueError(
                f"AbstainGateConfig: unknown regime token(s) {unknown} — "
                f"the vocabulary is {sorted(REGIME_CODES)}"
            )
        if not self.allowed_regimes:
            raise ValueError(
                "AbstainGateConfig: allowed_regimes must be non-empty "
                "(an empty set would abstain every trade — configure the "
                "regime door explicitly, never by vacuum)"
            )
        for name, value in (("vix_lo", self.vix_lo), ("vix_hi", self.vix_hi)):
            if value is not None and not math.isfinite(value):
                raise ValueError(f"AbstainGateConfig: {name} must be finite or None, got {value!r}")
        if self.vix_lo is not None and self.vix_hi is not None and self.vix_lo > self.vix_hi:
            raise ValueError(f"AbstainGateConfig: vix_lo {self.vix_lo!r} > vix_hi {self.vix_hi!r}")
        if self.tau is not None and not (math.isfinite(self.tau) and 0.0 <= self.tau <= 1.0):
            raise ValueError(
                f"AbstainGateConfig: tau must be a finite probability in [0, 1] "
                f"or None, got {self.tau!r}"
            )


def default_abstain_config() -> AbstainGateConfig:
    """The DEFAULT ILLUSTRATIVE annotation config (echoed in every sidecar).

    * NO regime restriction (all four tokens allowed — deriving a
      restriction from the run's own ``pnl_by_trend_json`` was REJECTED as
      overfit-prone: gating on in-sample per-regime P&L is exactly the
      double-dip the annotation-first law exists to prevent).
    * NO VIX gate (``vix_lo = vix_hi = None`` — the anti-overfit posture,
      the hostile-review round-1 MAJOR-2 fix): the only stamped band
      available at annotation time is the aggs ``vix_band_lo``/``hi``,
      which ``combo_aggregates`` computes as the p10–p90 of THIS combo's
      WINNING trades' entry VIX — a LABEL-CONDITIONAL, IN-SAMPLE statistic
      (fit-on-self). Adopting it as a default gate would be exactly the
      double-dip above. The annotator still RECORDS that band as the
      informational ``vix_band_winners_p10_p90`` sidecar field (descriptive
      only, never a held-out estimate), and an operator may set a band
      DELIBERATELY via an explicit :class:`AbstainGateConfig` — which then
      needs its own validation before any gating use.
    * ``block_event_days=True`` (the four verified event classes).
    * ``tau`` = :data:`DEFAULT_TAU` (0.7 — mid-grid, illustrative).

    Any gating use of this config requires re-validation of the gated stream
    as its OWN strategy (the DEC's MIN_TRADES/DSR disclosure); alternate
    configs are a re-run of the annotator, never a hand-edit of a sidecar.
    """
    return AbstainGateConfig(
        allowed_regimes=_ALL_REGIMES,
        vix_lo=None,
        vix_hi=None,
        block_event_days=True,
        tau=DEFAULT_TAU,
    )


def abstain_reasons(
    *,
    regime: str,
    vix: float | None,
    event_day: bool,
    confidence: float | None,
    config: AbstainGateConfig,
) -> tuple[str, ...]:
    """Every failed gate door, named — pure, total, all doors independent.

    The ``gate_reasons`` law: on ANY input every configured door is judged
    INDEPENDENTLY (no short-circuit), reasons emit in
    :data:`ABSTAIN_REASON_ORDER` order, and ``abstain_passes(...) ⇔
    reasons == ()`` holds for every input (property-pinned). A NaN ``vix``
    or ``confidence`` float is normalised to missing (``None`` semantics) —
    the stored stamps are nullable floats, and a NaN must never silently
    pass a ``<`` comparison (Golden Rule #1).

    O(1) per call (a handful of comparisons + one small tuple), stateless.
    """
    if vix is not None and math.isnan(vix):
        vix = None
    if confidence is not None and math.isnan(confidence):
        confidence = None
    reasons: list[str] = []
    if regime not in config.allowed_regimes:
        reasons.append(ABSTAIN_REGIME_MISMATCH)
    vix_gate_configured = config.vix_lo is not None or config.vix_hi is not None
    if vix_gate_configured:
        if vix is None:
            reasons.append(ABSTAIN_VIX_MISSING)
        elif (config.vix_lo is not None and vix < config.vix_lo) or (
            config.vix_hi is not None and vix > config.vix_hi
        ):
            # The band is INCLUSIVE both ends: vix == lo and vix == hi PASS.
            reasons.append(ABSTAIN_VIX_OUT_OF_BAND)
    if config.block_event_days and event_day:
        reasons.append(ABSTAIN_EVENT_DAY)
    # EXACT ``>=`` boundary (pinned): confidence == tau PASSES. A missing
    # confidence under a configured tau reads low_confidence — the model
    # cannot certify the bound (fail-closed, abstention-first).
    if config.tau is not None and not (confidence is not None and confidence >= config.tau):
        reasons.append(ABSTAIN_LOW_CONFIDENCE)
    return tuple(reasons)


def abstain_passes(
    *,
    regime: str,
    vix: float | None,
    event_day: bool,
    confidence: float | None,
    config: AbstainGateConfig,
) -> bool:
    """The predicate twin: ``True`` ⇔ :func:`abstain_reasons` is empty."""
    return (
        abstain_reasons(
            regime=regime, vix=vix, event_day=event_day, confidence=confidence, config=config
        )
        == ()
    )


def reasons_from_mask(mask: int) -> tuple[str, ...]:
    """Decode a Rust kernel reason BITMASK to ordered reason tokens.

    Bit ``i`` of *mask* ⇔ ``ABSTAIN_REASON_ORDER[i]`` (the documented FFI
    bit order — mirror-pinned against ``rust/brutex-meta/src/abstain.rs``).
    Bits beyond the vocabulary raise ``ValueError`` (a poisoned wheel must
    refuse loudly at the facade, never decode silently).
    """
    if mask < 0 or mask >= (1 << len(ABSTAIN_REASON_ORDER)):
        raise ValueError(
            f"reasons_from_mask: mask {mask!r} outside the "
            f"{len(ABSTAIN_REASON_ORDER)}-bit reason vocabulary"
        )
    return tuple(reason for i, reason in enumerate(ABSTAIN_REASON_ORDER) if (mask >> i) & 1)


def abstain_masks_python(
    regimes: list[int],
    allowed_mask: int,
    vix: list[float],
    vix_lo: float,
    vix_hi: float,
    event_flags: list[int],
    block_event_days: bool,
    confidence: list[float],
    tau: float,
) -> list[int]:
    """The retained Python ORACLE of ``brutex_meta.abstain_masks`` (bit-level).

    Token-for-token the Rust kernel's semantics over the SAME NaN-encoded
    scalar shape (NaN vix = missing, NaN band side = unbounded, NaN tau = no
    confidence gate; regime codes per :data:`REGIME_CODES`; a code outside
    ``[0, 64)`` can never be allowed). The facade's burn-in bit-compares the
    two arms; the hypothesis differential in ``tests/property/`` sweeps
    hostile inputs. Raises ``ValueError`` on mismatched column lengths
    (mirroring the kernel's structural refusal).
    """
    n = len(regimes)
    if len(vix) != n or len(event_flags) != n or len(confidence) != n:
        raise ValueError(
            f"abstain_masks_python: column lengths disagree (regimes={n}, "
            f"vix={len(vix)}, event_flags={len(event_flags)}, confidence={len(confidence)})"
        )
    vix_gate_configured = not math.isnan(vix_lo) or not math.isnan(vix_hi)
    out: list[int] = []
    for i in range(n):
        mask = 0
        r = regimes[i]
        if not (0 <= r < _REGIME_CODE_BITS and (allowed_mask >> r) & 1):
            mask |= 1 << 0
        if vix_gate_configured:
            v = vix[i]
            if math.isnan(v):
                mask |= 1 << 2
            elif (not math.isnan(vix_lo) and v < vix_lo) or (not math.isnan(vix_hi) and v > vix_hi):
                mask |= 1 << 1
        if block_event_days and event_flags[i] != 0:
            mask |= 1 << 3
        # EXACT >= boundary; NaN confidence fails the certification.
        if not math.isnan(tau) and not (confidence[i] >= tau):
            mask |= 1 << 4
        out.append(mask)
    return out


# ── The dormant IN-SIM arm's refusal knob (the fill_mode precedent) ─────────

#: The DESIGNED-but-DORMANT in-sim gate env knob. NOTHING resolves a gate
#: from it; a launch that finds it set refuses loudly (below).
ABSTAIN_GATE_INSIM_ENV: Final[str] = "BRUTEX_ABSTAIN_GATE_INSIM"


class AbstainGateInSimRefusedError(RuntimeError):
    """A launch found ``$BRUTEX_ABSTAIN_GATE_INSIM`` set — the arm is dormant.

    DEC-ABSTAIN-GATE-STREAM-001: the abstention gate is ANNOTATION-ONLY; the
    in-sim arm is designed (``docs/design/meta/ABSTAIN_INSIM_DESIGN.md``)
    but awaits the operator gate-flip ruling AND W4's PR-B dual-feed walker
    landing first (club order). Raised — never warned — so a premature
    deployment config fails LOUD at launch instead of silently pretending
    the gate is live (the ``BRUTEX_FILL_MODE`` fail-loud precedent).
    """


def require_abstain_gate_insim_unset(env: Mapping[str, str] | None = None) -> None:
    """Refuse LOUDLY when the dormant ``$BRUTEX_ABSTAIN_GATE_INSIM`` knob is set.

    Wired at the sweep LAUNCH resolution (``cmd_sweep_all``'s eager
    pre-compute config block, beside ``require_fill_mode_env_unset``) so the
    refusal fires before any compute. EVERY non-blank value refuses —
    including truthy-looking spellings: no in-sim gate exists to enable, and
    silently tolerating the knob would let an operator believe a gated sweep
    ran when it did not (the anti-illusion law, Golden Rule #1).

    Parameters
    ----------
    env
        The environment mapping to read (defaults to ``os.environ`` — the
        injection seam keeps the guard pure for tests).

    Raises
    ------
    AbstainGateInSimRefusedError
        When the variable is present with ANY non-blank value. An UNSET or
        empty-string variable passes (empty = the common ``VAR= cmd`` unset
        idiom; nothing was configured).
    """
    source = os.environ if env is None else env
    raw = source.get(ABSTAIN_GATE_INSIM_ENV, "")
    if raw.strip():
        raise AbstainGateInSimRefusedError(
            f"${ABSTAIN_GATE_INSIM_ENV}={raw!r} is set, but the in-sim abstention "
            f"gate is DORMANT — the stream is annotation-only "
            f"(DEC-ABSTAIN-GATE-STREAM-001), awaiting the operator gate-flip "
            f"ruling. Unset the variable; annotate via `brutex meta-annotate` "
            f"instead."
        )
