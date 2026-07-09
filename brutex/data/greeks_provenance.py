"""The packed Greeks provenance stamp — constants, pack/unpack, display labels.

The ONE COMMON home (CLAUDE.md §11 — Golden Rule #11) of the
``greeks_provenance_id`` bit-format written by the generation-time Greeks
enrichment (:mod:`brutex.data.greeks_enrich`, DEC-GREEKS-ENGINE-PR2B-001 /
plan §15.3) and DECODED by the read-only ``/db`` viewer
(:mod:`brutex.dashboard.db_view`, DEC-DB-FNO-DISPLAY-TRUTH-001 item 3).

MOVED VERBATIM from ``greeks_enrich.py`` (which re-exports every name
byte-compatibly — the ``symbol_form_preference.py``/``contracts.py`` #1581 and
``meta_kv.py`` precedents) so the dashboard read path can decode a stored
stamp WITHOUT importing the enrichment module — ``greeks_enrich`` imports
:mod:`brutex.options.greeks`, which imports the compiled ``trading_core``
wheel at module scope, and the viewer read path is deliberately wheel-free
(the DEC-CLI-GREEKS-ENRICH-001 ``greeks_enrich_pass.py`` precedent).

**Stdlib-only at import** (honest envelope, ONE documented exception):
importing this module pulls nothing beyond the stdlib. The single deviation
from the verbatim move is :func:`pack_greeks_provenance`'s ``rate_version``
default — previously the eager module-level import of
:data:`brutex.options.risk_free.RISK_FREE_SOURCE_VERSION`, which cannot be
imported here at module scope (``brutex.options.__init__`` imports the
``trading_core`` wheel). The default is now a ``None`` sentinel resolved
LAZILY from the SAME constant (one source of truth, never a copied value —
Golden Rule #11) the first time a caller relies on it; every packed integer
is byte-identical. The wheel-free viewer only ever calls
:func:`unpack_greeks_provenance` / :func:`greeks_status_label`, which stay
pure stdlib end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "GREEKS_FORMULA_VERSION",
    "INPUTS_MASK",
    "Q_BIAS_ZERO",
    "RATE_SOURCE_ID_CSV",
    "RATE_SOURCE_ID_FIXED",
    "RATE_SOURCE_ID_NONE",
    "STATUS_AT_EXPIRY",
    "STATUS_BAD_INPUT",
    "STATUS_BELOW_INTRINSIC",
    "STATUS_DISPLAY_LABELS",
    "STATUS_NAMES",
    "STATUS_NO_RATE",
    "STATUS_NO_SOLUTION",
    "STATUS_NO_SPOT",
    "STATUS_OK",
    "STYLE_AMERICAN_APPROX",
    "STYLE_EUROPEAN_EXACT",
    "STYLE_UNKNOWN",
    "TTE_ACT365_EXPIRY_1530_IST",
    "GreeksProvenance",
    "greeks_status_label",
    "pack_greeks_provenance",
    "unpack_greeks_provenance",
]

#: Formula version frozen into every stored tuple. Bump when the kernel maths /
#: overlay semantics change — the staleness key that turns a formula revision
#: into a positive re-enrich signal (plan §15.3 "why the stamp is MANDATORY").
#: v2 = the DEC-GREEKS-CORRECTNESS-AUDIT-001 round (degenerate time-value
#: pre-screen + value-hardened reuse + 2dp spot snap): every v1 stored bar —
#: incl. the IVX-1 at-floor artifacts — recomputes + restamps on the next
#: enrich pass over its window. HONEST envelope: that pass reaches only
#: contracts still inside a live pull window; expired contracts keep v1
#: stamps until a bulk re-enrich surface exists (named follow-up).
GREEKS_FORMULA_VERSION: Final[int] = 2

#: Rate-source ids packed into the provenance stamp (bits 8-11).
RATE_SOURCE_ID_NONE: Final[int] = 0
RATE_SOURCE_ID_FIXED: Final[int] = 1
RATE_SOURCE_ID_CSV: Final[int] = 2

#: Dividend-bias id (bits 16-17): ``q = 0`` with the documented bias direction
#: (over-prices calls / under-prices puts — ADR-0015 §4), the §3 #3 default.
Q_BIAS_ZERO: Final[int] = 1

#: Time-to-expiry convention id (bits 18-19): calendar Act/365 seconds to
#: 15:30 IST on the expiry date (see the ``greeks_enrich`` docstring for the
#: lock note).
TTE_ACT365_EXPIRY_1530_IST: Final[int] = 1

#: Exercise-style flags (bits 20-22). ``STYLE_AMERICAN_APPROX`` marks the
#: ADR-0015 §3 #1 FLAGGED APPROXIMATION for the ~212 American single stocks —
#: computed, never silently presented as exact (Golden Rule #10).
STYLE_UNKNOWN: Final[int] = 0
STYLE_EUROPEAN_EXACT: Final[int] = 1
STYLE_AMERICAN_APPROX: Final[int] = 2

#: Per-bar solve statuses (bits 23-26) — the stamped NULL reasons of plan §15.8.
STATUS_OK: Final[int] = 0
STATUS_NO_SPOT: Final[int] = 1
STATUS_NO_SOLUTION: Final[int] = 2
STATUS_AT_EXPIRY: Final[int] = 3
STATUS_NO_RATE: Final[int] = 4
STATUS_BAD_INPUT: Final[int] = 5
STATUS_BELOW_INTRINSIC: Final[int] = 6

#: Machine-readable name per packed status — the ``null_reasons`` telemetry
#: labels (DEC-GREEKS-CORRECTNESS-AUDIT-001 CTR-6). Public here (was the
#: module-private ``_STATUS_NAMES`` of ``greeks_enrich``, which now aliases it).
STATUS_NAMES: Final[dict[int, str]] = {
    STATUS_OK: "ok",
    STATUS_NO_SPOT: "no_spot",
    STATUS_NO_SOLUTION: "no_solution",
    STATUS_AT_EXPIRY: "at_expiry",
    STATUS_NO_RATE: "no_rate",
    STATUS_BAD_INPUT: "bad_input",
    STATUS_BELOW_INTRINSIC: "below_intrinsic",
}

#: Human-readable status wording for the ``/db`` viewer's derived
#: ``greeks_status`` column (DEC-DB-FNO-DISPLAY-TRUTH-001 item 3) — the words
#: an operator reads instead of a bare "null" beside a packed int.
STATUS_DISPLAY_LABELS: Final[dict[int, str]] = {
    STATUS_OK: "ok",
    STATUS_NO_SPOT: "no spot — underlying bar missing",
    STATUS_NO_SOLUTION: "no IV solution",
    STATUS_AT_EXPIRY: "at/after expiry",
    STATUS_NO_RATE: "no risk-free rate",
    STATUS_BAD_INPUT: "bad input bar",
    STATUS_BELOW_INTRINSIC: "below intrinsic — no unique IV",
}

# Bit layout of the packed Int32 provenance stamp. The INPUT-defining fields
# (formula/rate/q/T — bits 0-19) decide staleness; style + status (bits 20-26)
# describe the individual bar. Total 27 bits < 2^31 (Int32-safe, validated by
# FnoGreeksBar).
_FORMULA_SHIFT: Final[int] = 0
_RATE_SOURCE_SHIFT: Final[int] = 8
_RATE_VERSION_SHIFT: Final[int] = 12
_Q_BIAS_SHIFT: Final[int] = 16
_TTE_SHIFT: Final[int] = 18
_STYLE_SHIFT: Final[int] = 20
_STATUS_SHIFT: Final[int] = 23
_FORMULA_MASK: Final[int] = 0xFF
_RATE_SOURCE_MASK: Final[int] = 0xF
_RATE_VERSION_MASK: Final[int] = 0xF
_Q_BIAS_MASK: Final[int] = 0x3
_TTE_MASK: Final[int] = 0x3
_STYLE_MASK: Final[int] = 0x7
_STATUS_MASK: Final[int] = 0xF

#: The provenance bits that define the MODEL INPUTS (formula + rate source +
#: rate version + q bias + T convention) — the staleness-compare subset; style
#: and status are per-bar descriptions, not inputs. Public here (was the
#: module-private ``_INPUTS_MASK`` of ``greeks_enrich``, which now aliases it).
INPUTS_MASK: Final[int] = (1 << _STYLE_SHIFT) - 1


@dataclass(frozen=True, slots=True)
class GreeksProvenance:
    """The unpacked fields of one ``greeks_provenance_id`` stamp.

    Attributes
    ----------
    formula_version
        :data:`GREEKS_FORMULA_VERSION` at write time.
    rate_source_id, rate_version
        The risk-free source kind + its ``RISK_FREE_SOURCE_VERSION``.
    q_bias_id
        The dividend assumption (:data:`Q_BIAS_ZERO`).
    tte_convention_id
        The time-to-expiry convention (:data:`TTE_ACT365_EXPIRY_1530_IST`).
    style_flag
        The exercise-style flag (:data:`STYLE_EUROPEAN_EXACT` /
        :data:`STYLE_AMERICAN_APPROX` / :data:`STYLE_UNKNOWN`).
    status
        The per-bar solve status (:data:`STATUS_OK` or a stamped NULL reason).
    """

    formula_version: int
    rate_source_id: int
    rate_version: int
    q_bias_id: int
    tte_convention_id: int
    style_flag: int
    status: int


def pack_greeks_provenance(
    *,
    rate_source_id: int,
    rate_version: int | None = None,
    style_flag: int,
    status: int,
    formula_version: int = GREEKS_FORMULA_VERSION,
    q_bias_id: int = Q_BIAS_ZERO,
    tte_convention_id: int = TTE_ACT365_EXPIRY_1530_IST,
) -> int:
    """Pack the provenance fields into the stored Int32 stamp.

    Every field is validated LOUDLY against its documented bit band BEFORE
    packing (DEC-GREEKS-CORRECTNESS-AUDIT-001 PROV-2): the old silent
    ``&``-mask made an out-of-band value (e.g. a future
    ``RISK_FREE_SOURCE_VERSION = 17``) collide bit-identically with an
    in-band stamp — the wrong failure mode for a STALENESS key, whose one job
    is to never alias two different input sets. An in-band call packs the
    byte-identical integer it always did (27 used bits < 2^31, so the
    :class:`~brutex.data.models.FnoGreeksBar` range validation can never fire
    for a stamp built here).

    ``rate_version`` defaults to ``None`` = "the current
    :data:`brutex.options.risk_free.RISK_FREE_SOURCE_VERSION`", resolved
    lazily (see the module docstring — the ONE documented deviation from the
    verbatim #1581-style move, keeping this module importable without the
    ``trading_core`` wheel while the default value keeps its single source of
    truth). Every default-relying call packs the byte-identical integer the
    pre-move eager default packed.

    Raises
    ------
    ValueError
        When any field lies outside its bit band (negative, or above the
        band's mask) — naming the field, the value, and the band width.
    """
    if rate_version is None:
        # Lazy import: `brutex.options.__init__` pulls the compiled kernel wheel,
        # which this stdlib-only module must not require at import time. Only a
        # caller relying on the default (the enrichment side, which already has
        # the wheel) pays this import; the viewer decode path never reaches it.
        from brutex.options.risk_free import RISK_FREE_SOURCE_VERSION

        rate_version = RISK_FREE_SOURCE_VERSION
    for name, value, mask in (
        ("formula_version", formula_version, _FORMULA_MASK),
        ("rate_source_id", rate_source_id, _RATE_SOURCE_MASK),
        ("rate_version", rate_version, _RATE_VERSION_MASK),
        ("q_bias_id", q_bias_id, _Q_BIAS_MASK),
        ("tte_convention_id", tte_convention_id, _TTE_MASK),
        ("style_flag", style_flag, _STYLE_MASK),
        ("status", status, _STATUS_MASK),
    ):
        if not 0 <= value <= mask:
            msg = (
                f"greeks provenance {name}={value} outside its "
                f"{mask.bit_length()}-bit band [0, {mask}]"
            )
            raise ValueError(msg)
    return (
        (formula_version << _FORMULA_SHIFT)
        | (rate_source_id << _RATE_SOURCE_SHIFT)
        | (rate_version << _RATE_VERSION_SHIFT)
        | (q_bias_id << _Q_BIAS_SHIFT)
        | (tte_convention_id << _TTE_SHIFT)
        | (style_flag << _STYLE_SHIFT)
        | (status << _STATUS_SHIFT)
    )


def unpack_greeks_provenance(packed: int) -> GreeksProvenance:
    """Unpack a stored ``greeks_provenance_id`` into its fields (pure inverse)."""
    return GreeksProvenance(
        formula_version=(packed >> _FORMULA_SHIFT) & _FORMULA_MASK,
        rate_source_id=(packed >> _RATE_SOURCE_SHIFT) & _RATE_SOURCE_MASK,
        rate_version=(packed >> _RATE_VERSION_SHIFT) & _RATE_VERSION_MASK,
        q_bias_id=(packed >> _Q_BIAS_SHIFT) & _Q_BIAS_MASK,
        tte_convention_id=(packed >> _TTE_SHIFT) & _TTE_MASK,
        style_flag=(packed >> _STYLE_SHIFT) & _STYLE_MASK,
        status=(packed >> _STATUS_SHIFT) & _STATUS_MASK,
    )


def greeks_status_label(packed: int | None, *, is_option: bool) -> str:
    """Human-readable Greeks status for one displayed bar (never raises).

    The read-time decoder behind the ``/db`` viewer's derived ``greeks_status``
    column (DEC-DB-FNO-DISPLAY-TRUTH-001 item 3):

    * a non-option (a future / an undecodable symbol) reads ``"—"`` — a future
      is never enriched BY DESIGN ("never a bogus Greek for a future");
    * a ``None`` stamp reads ``"not enriched"`` — a legacy 7-column partition,
      a derived-TF partition (the overlay is stored on ``1minute`` only), or a
      bar the enrichment never reached;
    * a stamped bar reads its :data:`STATUS_DISPLAY_LABELS` wording; an
      unknown future status id reads the honest ``"status <n>"`` (never a
      wrong label — Golden Rule #1).
    """
    if not is_option:
        return "—"
    if packed is None:
        return "not enriched"
    status = unpack_greeks_provenance(packed).status
    return STATUS_DISPLAY_LABELS.get(status, f"status {status}")
