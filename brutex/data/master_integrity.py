"""Instrument-master structural integrity audit — every leg maps to exactly one spot.

DEC-INSTRUMENT-MASTER-INTEGRITY-001. A PURE, offline, parse-time audit over the FULL
Groww ``instrument.csv`` universe (all five types — IDX / EQ / FUT / CE / PE) that proves
the core mapping is precise: **every futures / option leg resolves to exactly one spot
underlying (an IDX or EQ), cross-checked by BOTH the `underlying_symbol` string AND the
numeric `underlying_security_id` token-join** — flagging orphans, ambiguity, symbol↔token
disagreement, empty-underlying legs, and duplicate rows.

It is the STRUCTURAL sibling of :mod:`brutex.data.mapping_integrity` (#870, which is the
COVERAGE audit — *did* each eligible spot pull its expiries). This one needs only the
parsed master (no ledger, no lake, no network) and runs at INGEST time, BEFORE the lean
store discards the ~89k legs (DEC-INSTRUMENT-UNIVERSE-FAST-001) — so it is the only place
the leg→spot mapping can be verified. It SUMMARISES (full counts + a capped orphan sample);
it never re-stores the legs (the lean-store decree stands).

## The resolution (O(rows) total, O(1) per lookup, O(spots) space)

Pass 1 builds the spot lookup tables in ONE sweep; pass 2 resolves each leg against them —
two linear sweeps over the SAME already-materialised sequence (no nested scan):

* ``spot_by_symbol[(exchange, symbol)]`` — every IDX/EQ row's ``(exch_id, symbol_name)``.
* ``spot_by_token[(exchange, security_id)]`` — every IDX/EQ row whose ``feed_token`` is a
  GENUINE numeric vendor token (a non-numeric NSE-index token hashes to a blake2b surrogate
  `security_id` that can never joinably equal a leg's numeric `underlying_security_id`, so
  it is excluded — DEC-FNO-INDEX-TOKEN-JOIN-001).
* the index-alias map (injected ``{spot_symbol: fno_underlying_symbol}``,
  DEC-FNO-INDEX-ALIAS-001) is reversed so a leg whose ``underlying_symbol`` is the F&O form
  (e.g. ``NIFTYNXT50``) resolves to its spot symbol (e.g. ``NIFTYJR``).

Each leg lands in EXACTLY one bucket: ``resolved_both`` (symbol + token agree),
``resolved_symbol_only`` (token surrogate/absent — expected for NSE indices),
``resolved_token_only`` (the special-char case: ``M&M`` / ``BAJAJ-AUTO`` whose spot
``symbol_name`` differs but the numeric token agrees), or a GATING failure — ``orphan``
(no spot), ``ambiguous`` (resolves to >1 distinct spot), ``disagreement`` (symbol→A,
token→B), ``empty_underlying`` (a leg naming no underlying at all).

``is_green`` ⟺ zero gating failures. DETECT-only (mirrors #870) — it never crashes the
keyless load on a vendor master drift (Golden Rule #10).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from brutex.models import Instrument, InstrumentType

#: Leg buckets that turn the verdict RED (the structural failures).
REASON_ORPHAN: Final[str] = "orphan"
REASON_AMBIGUOUS: Final[str] = "ambiguous"
REASON_DISAGREEMENT: Final[str] = "disagreement"
REASON_EMPTY_UNDERLYING: Final[str] = "empty_underlying"

#: Gating reasons, for drift tests / consumers.
GATING_REASONS: Final[tuple[str, str, str, str]] = (
    REASON_ORPHAN,
    REASON_AMBIGUOUS,
    REASON_DISAGREEMENT,
    REASON_EMPTY_UNDERLYING,
)

#: Cap on the per-report orphan-leg SAMPLE (full counts are always exact; the sample is
#: bounded so the persisted summary / JSONL line can never bloat on a pathological master).
ORPHAN_SAMPLE_CAP: Final[int] = 200

#: The ``schema_meta`` key the ingest hook persists the audit JSON under (read by the
#: dashboard routes — the legs are discarded post-load, so this snapshot is the source).
META_KEY: Final[str] = "master_integrity"

#: Characters that make an underlying symbol "special" (the cases the resolver must handle).
_SPECIAL_CHARS: Final[frozenset[str]] = frozenset("&-. ")

_Identity = tuple[str, int]  # (exchange value, security_id) — the spot identity


@dataclass(frozen=True, slots=True)
class OrphanLeg:
    """One FUT/CE/PE leg that did NOT cleanly map to exactly one spot underlying."""

    exchange: str
    symbol: str  # the leg's own trading symbol
    instrument_type: str  # FUT / CE / PE
    underlying_symbol: str | None
    underlying_security_id: int | None
    reason: str  # one of GATING_REASONS


@dataclass(frozen=True, slots=True)
class TypeCounts:
    """Per-instrument-type row counts across the whole master."""

    idx: int
    eq: int
    fut: int
    ce: int
    pe: int

    @property
    def spots(self) -> int:
        """IDX + EQ — the spot underlyings."""
        return self.idx + self.eq

    @property
    def legs(self) -> int:
        """FUT + CE + PE — the derivative legs."""
        return self.fut + self.ce + self.pe


@dataclass(frozen=True, slots=True)
class MasterIntegrityReport:
    """The structural-integrity verdict over one parsed instrument master + its drill-down."""

    generated_at: str
    run_id: str
    csv_source_sha256: str
    total_rows: int
    type_counts: TypeCounts
    resolved_both: int
    resolved_symbol_only: int
    resolved_token_only: int
    orphan_count: int
    ambiguous_count: int
    disagreement_count: int
    empty_underlying_count: int
    dedup_collisions: int
    spots_with_no_legs: int
    special_char_underlyings: tuple[str, ...]
    orphan_legs: tuple[OrphanLeg, ...]  # capped sample (≤ ORPHAN_SAMPLE_CAP)

    @property
    def gating_failures(self) -> int:
        """The total structural failures that turn the verdict red."""
        return (
            self.orphan_count
            + self.ambiguous_count
            + self.disagreement_count
            + self.empty_underlying_count
        )

    @property
    def is_green(self) -> bool:
        """``True`` iff every leg mapped cleanly to exactly one spot (no gating failure)."""
        return self.gating_failures == 0


def _has_genuine_numeric_token(inst: Instrument) -> bool:
    """``True`` iff *inst*'s ``security_id`` is a GENUINE numeric vendor token.

    Self-contained replica of the loader's same-named predicate
    (DEC-FNO-INDEX-TOKEN-JOIN-001): a numeric ``feed_token`` ⇒ ``security_id`` is the real
    vendor token (token-join valid); a non-numeric / absent ``feed_token`` ⇒ a blake2b
    surrogate, so the token-join MUST be skipped for that row. Kept here (not imported from
    the dashboard layer) so this ``data/`` module stays free of any ``dashboard/`` import.
    """
    token = inst.feed_token
    return token is not None and token.isdigit()


def _dedup_key(inst: Instrument) -> tuple[Any, ...]:
    """The row-identity key for duplicate detection (same instrument listed twice)."""
    return (
        inst.exch_id.value,
        inst.security_id,
        inst.instrument.value,
        inst.sm_expiry_date,
        inst.strike_price,
    )


def audit_master_integrity(
    instruments: Sequence[Instrument],
    *,
    index_aliases: Mapping[str, str] | None = None,
    now: datetime,
    run_id: str = "",
    csv_source_sha256: str = "",
) -> MasterIntegrityReport:
    """Audit the parsed *instruments* master → a structural-integrity report.

    Two linear sweeps over *instruments* (O(rows) total, O(1) per lookup, O(spots) space):
    pass 1 builds the spot symbol/token tables + per-type counts + dedup detection; pass 2
    resolves every FUT/CE/PE leg against them, classifying each into exactly one bucket.

    Parameters
    ----------
    instruments
        The FULL parsed master (leg-bearing) — e.g. the Groww ``InstrumentIndex.universe``.
    index_aliases
        The ``{spot_symbol: fno_underlying_symbol}`` index-alias map (DEC-FNO-INDEX-ALIAS-001);
        reversed internally so a leg's F&O-form underlying resolves to its spot symbol. ``None``
        ⇒ no aliases.
    now
        Report timestamp (injected for determinism).
    run_id, csv_source_sha256
        Provenance echoed into the report header (the loader passes the real values).

    Returns
    -------
    MasterIntegrityReport
        The verdict + counts + a capped orphan sample.
    """
    reverse_aliases = {v: k for k, v in (index_aliases or {}).items()}

    spot_by_symbol: dict[tuple[str, str], _Identity] = {}
    spot_by_token: dict[tuple[str, int], _Identity] = {}
    ambiguous_symbol_keys: set[tuple[str, str]] = set()
    spot_identities: set[_Identity] = set()
    seen_dedup: set[tuple[Any, ...]] = set()
    counts = dict.fromkeys(InstrumentType, 0)
    dedup_collisions = 0

    # Pass 1 — partition spots + build O(1) lookup tables.
    for inst in instruments:
        counts[inst.instrument] += 1
        key = _dedup_key(inst)
        if key in seen_dedup:
            dedup_collisions += 1
        else:
            seen_dedup.add(key)
        if inst.instrument in (InstrumentType.IDX, InstrumentType.EQ):
            identity: _Identity = (inst.exch_id.value, inst.security_id)
            spot_identities.add(identity)
            sym_key = (inst.exch_id.value, inst.symbol_name)
            existing = spot_by_symbol.get(sym_key)
            if existing is not None and existing != identity:
                ambiguous_symbol_keys.add(sym_key)
            else:
                spot_by_symbol[sym_key] = identity
            # The token key IS the identity ``(exch, security_id)``, so it can never map to
            # two DISTINCT identities — token ambiguity is structurally impossible (a repeat
            # is the same spot). Only the symbol map can be ambiguous (two security_ids, one
            # symbol). A cross-symbol security_id collision is a rare master corruption out of
            # this audit's scope (Golden Rule #10 — stated, not silently mishandled).
            if _has_genuine_numeric_token(inst):
                spot_by_token[(inst.exch_id.value, inst.security_id)] = identity

    # Pass 2 — resolve every leg against the spot tables.
    resolved_both = resolved_symbol_only = resolved_token_only = 0
    orphan = ambiguous = disagreement = empty_underlying = 0
    orphan_sample: list[OrphanLeg] = []
    referenced: set[_Identity] = set()
    special: set[str] = set()

    def _record_orphan(inst: Instrument, reason: str) -> None:
        if len(orphan_sample) < ORPHAN_SAMPLE_CAP:
            orphan_sample.append(
                OrphanLeg(
                    exchange=inst.exch_id.value,
                    symbol=inst.symbol_name,
                    instrument_type=inst.instrument.value,
                    underlying_symbol=inst.underlying_symbol,
                    underlying_security_id=inst.underlying_security_id,
                    reason=reason,
                )
            )

    for inst in instruments:
        if inst.instrument not in (InstrumentType.FUT, InstrumentType.CE, InstrumentType.PE):
            continue
        exch = inst.exch_id.value
        under_sym = inst.underlying_symbol
        under_tok = inst.underlying_security_id
        if under_sym and any(ch in _SPECIAL_CHARS for ch in under_sym):
            special.add(under_sym)

        if not under_sym and under_tok is None:
            empty_underlying += 1
            _record_orphan(inst, REASON_EMPTY_UNDERLYING)
            continue

        leg_sym_key: tuple[str, str] | None = (
            (exch, reverse_aliases.get(under_sym, under_sym)) if under_sym else None
        )
        leg_tok_key: tuple[str, int] | None = (exch, under_tok) if under_tok is not None else None
        sym_ambiguous = leg_sym_key is not None and leg_sym_key in ambiguous_symbol_keys
        sym_hit = spot_by_symbol.get(leg_sym_key) if leg_sym_key is not None else None
        tok_hit = spot_by_token.get(leg_tok_key) if leg_tok_key is not None else None

        if sym_ambiguous:
            ambiguous += 1
            _record_orphan(inst, REASON_AMBIGUOUS)
        elif sym_hit is not None and tok_hit is not None and sym_hit != tok_hit:
            disagreement += 1
            _record_orphan(inst, REASON_DISAGREEMENT)
        elif sym_hit is not None and tok_hit is not None:
            resolved_both += 1
            referenced.add(sym_hit)
        elif sym_hit is not None:
            resolved_symbol_only += 1
            referenced.add(sym_hit)
        elif tok_hit is not None:
            resolved_token_only += 1
            referenced.add(tok_hit)
        else:
            orphan += 1
            _record_orphan(inst, REASON_ORPHAN)

    return MasterIntegrityReport(
        generated_at=now.isoformat(),
        run_id=run_id,
        csv_source_sha256=csv_source_sha256,
        total_rows=len(instruments),
        type_counts=TypeCounts(
            idx=counts[InstrumentType.IDX],
            eq=counts[InstrumentType.EQ],
            fut=counts[InstrumentType.FUT],
            ce=counts[InstrumentType.CE],
            pe=counts[InstrumentType.PE],
        ),
        resolved_both=resolved_both,
        resolved_symbol_only=resolved_symbol_only,
        resolved_token_only=resolved_token_only,
        orphan_count=orphan,
        ambiguous_count=ambiguous,
        disagreement_count=disagreement,
        empty_underlying_count=empty_underlying,
        dedup_collisions=dedup_collisions,
        spots_with_no_legs=len(spot_identities - referenced),
        special_char_underlyings=tuple(sorted(special)),
        orphan_legs=tuple(orphan_sample),
    )


def report_to_dict(report: MasterIntegrityReport) -> dict[str, Any]:
    """Project a :class:`MasterIntegrityReport` to JSON (dashboard route + schema_meta)."""
    return {
        "generated_at": report.generated_at,
        "run_id": report.run_id,
        "csv_source_sha256": report.csv_source_sha256,
        "is_green": report.is_green,
        "total_rows": report.total_rows,
        "type_counts": {
            "idx": report.type_counts.idx,
            "eq": report.type_counts.eq,
            "fut": report.type_counts.fut,
            "ce": report.type_counts.ce,
            "pe": report.type_counts.pe,
            "spots": report.type_counts.spots,
            "legs": report.type_counts.legs,
        },
        "resolved_both": report.resolved_both,
        "resolved_symbol_only": report.resolved_symbol_only,
        "resolved_token_only": report.resolved_token_only,
        "gating_failures": report.gating_failures,
        "orphan_count": report.orphan_count,
        "ambiguous_count": report.ambiguous_count,
        "disagreement_count": report.disagreement_count,
        "empty_underlying_count": report.empty_underlying_count,
        "dedup_collisions": report.dedup_collisions,
        "spots_with_no_legs": report.spots_with_no_legs,
        "special_char_underlyings": list(report.special_char_underlyings),
        "orphan_legs": [
            {
                "exchange": leg.exchange,
                "symbol": leg.symbol,
                "instrument_type": leg.instrument_type,
                "underlying_symbol": leg.underlying_symbol,
                "underlying_security_id": leg.underlying_security_id,
                "reason": leg.reason,
            }
            for leg in report.orphan_legs
        ],
    }


def report_from_dict(payload: Mapping[str, Any]) -> MasterIntegrityReport:
    """Reconstruct a :class:`MasterIntegrityReport` from a :func:`report_to_dict` projection.

    The inverse of :func:`report_to_dict` — used by the CSV download route to re-render the
    snapshot persisted to ``schema_meta`` at ingest (the legs are discarded by then, so the
    audit cannot be re-run; the persisted dict is the source). Round-trip stable.
    """
    tc = payload["type_counts"]
    return MasterIntegrityReport(
        generated_at=payload["generated_at"],
        run_id=payload["run_id"],
        csv_source_sha256=payload["csv_source_sha256"],
        total_rows=payload["total_rows"],
        type_counts=TypeCounts(idx=tc["idx"], eq=tc["eq"], fut=tc["fut"], ce=tc["ce"], pe=tc["pe"]),
        resolved_both=payload["resolved_both"],
        resolved_symbol_only=payload["resolved_symbol_only"],
        resolved_token_only=payload["resolved_token_only"],
        orphan_count=payload["orphan_count"],
        ambiguous_count=payload["ambiguous_count"],
        disagreement_count=payload["disagreement_count"],
        empty_underlying_count=payload["empty_underlying_count"],
        dedup_collisions=payload["dedup_collisions"],
        spots_with_no_legs=payload["spots_with_no_legs"],
        special_char_underlyings=tuple(payload["special_char_underlyings"]),
        orphan_legs=tuple(
            OrphanLeg(
                exchange=leg["exchange"],
                symbol=leg["symbol"],
                instrument_type=leg["instrument_type"],
                underlying_symbol=leg["underlying_symbol"],
                underlying_security_id=leg["underlying_security_id"],
                reason=leg["reason"],
            )
            for leg in payload["orphan_legs"]
        ),
    )


__all__ = [
    "GATING_REASONS",
    "META_KEY",
    "ORPHAN_SAMPLE_CAP",
    "REASON_AMBIGUOUS",
    "REASON_DISAGREEMENT",
    "REASON_EMPTY_UNDERLYING",
    "REASON_ORPHAN",
    "MasterIntegrityReport",
    "OrphanLeg",
    "TypeCounts",
    "audit_master_integrity",
    "report_from_dict",
    "report_to_dict",
]
