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

from brutex.data.detectors_rust import (
    MasterIntegrityPayload,
    master_integrity_rust_enabled,
    routed_audit_master_integrity,
)
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

#: Cap on the special-char-underlying SAMPLE (``special_char_count`` carries the true total).
#: Same anti-bloat rationale as :data:`ORPHAN_SAMPLE_CAP` — a pathological / vendor-drift
#: master where most legs name special-char symbols can never bloat the persisted summary.
SPECIAL_CHAR_SAMPLE_CAP: Final[int] = 200

#: The ``schema_meta`` key the ingest hook persists the audit JSON under (read by the
#: dashboard routes — the legs are discarded post-load, so this snapshot is the source).
META_KEY: Final[str] = "master_integrity"

#: Characters that make an underlying symbol "special" (the cases the resolver must handle).
_SPECIAL_CHARS: Final[frozenset[str]] = frozenset("&-. ")

#: Underlying-symbol markers identifying EXCHANGE TEST instruments (NSE/BSE inject fake
#: ``*NSETEST*`` / ``*BSETEST*`` contracts into every master). A leg whose underlying carries
#: one of these is a known non-tradable test row — counted + reported in its own bucket but
#: EXCLUDED from the gating verdict (it has no real spot, so it would otherwise be a perpetual
#: false-RED orphan). Injected + extensible (DYNAMIC) — NOT silently dropped (Golden Rule #10).
DEFAULT_TEST_UNDERLYING_MARKERS: Final[tuple[str, str]] = ("NSETEST", "BSETEST")

_Identity = tuple[str, int]  # (exchange value, security_id) — the spot identity

# ---------------------------------------------------------------------------
# SLICE T24 PR-3a routing vocabulary (DEC-DATA-RUST-INTEGRITY-DETECTORS-001).
# ---------------------------------------------------------------------------

#: Routed-leg disposition tokens for the payload walk (input-order law —
#: the orphan SAMPLE interleaves empty-underlying failures with routed
#: failures exactly as the oracle records them).
_DISP_EMPTY: Final[str] = "empty"
_DISP_TEST: Final[str] = "test"
_DISP_ROUTED: Final[str] = "routed"

#: The crate's per-leg bucket codes (the ``BUCKET_*`` constants of
#: ``rust/brutex-lake/src/audits.rs``, mirrored — the fake crate and the
#: facade's ``_MASTER_BUCKET_CODE_COUNT`` gate share this order).
_CODE_RESOLVED_BOTH: Final[int] = 0
_CODE_SYMBOL_ONLY: Final[int] = 1
_CODE_TOKEN_ONLY: Final[int] = 2
_CODE_ORPHAN: Final[int] = 3
_CODE_AMBIGUOUS: Final[int] = 4
_CODE_DISAGREEMENT: Final[int] = 5

#: The interned-id sentinel for an ABSENT key (no symbol / no token / no
#: genuine numeric ``feed_token``) — the crate's ``NO_KEY`` twin.
_NO_KEY: Final[int] = -1


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
    test_instrument_count: int  # exchange TEST legs (*NSETEST*/*BSETEST*) — non-gating, reported
    special_char_count: int  # true total of distinct special-char leg underlyings
    special_char_underlyings: tuple[str, ...]  # capped sample (≤ SPECIAL_CHAR_SAMPLE_CAP)
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
    test_underlying_markers: Sequence[str] = DEFAULT_TEST_UNDERLYING_MARKERS,
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

    Routing (DEC-DATA-RUST-INTEGRITY-DETECTORS-001, SLICE T24 PR-3a): the
    two-pass table build + leg classification is served by
    :func:`_audit_master_integrity_python` — the verbatim body, the LIVE
    parity oracle — via the default-OFF
    ``BRUTEX_LAKE_RUST_MASTER_INTEGRITY`` facade
    (:mod:`brutex.data.detectors_rust`). NO string/Decimal semantics enter
    Rust: the routed payload interns every symbol/token/identity key to a
    dense id, applies the alias reversal and pre-computes the
    Unicode-sensitive predicates (``str.isdigit()`` on ``feed_token`` —
    the ``'²²'`` trap class — the ``*NSETEST*`` marker containment, the
    special-char tally) Python-side; the Decimal-bearing dedup, the type
    counts and the input-order orphan-sample walk stay Python (the
    materializer tallies the crate's per-leg bucket CODES). Flag OFF (the
    default) returns the oracle directly — the crate is never imported,
    byte-identical to the pre-facade law. Every crate fault under the flag
    resolves to the ORACLE report — never a new exception class out of
    this audit (the DETECT-only never-crash-the-load posture holds on both
    arms).

    HONEST (the facade's ceremony disclosure): the routed payload build is
    itself an O(rows) two-pass Python walk with dicts — the same
    complexity class as the fold it replaces (payoff ≈ 0, the ADR-0020
    banner) — and the audit runs ~once per master load, so burn-in
    effectively means compare-always in production (a permanent live
    differential).
    """

    def _oracle() -> MasterIntegrityReport:
        return _audit_master_integrity_python(
            instruments,
            index_aliases=index_aliases,
            test_underlying_markers=test_underlying_markers,
            now=now,
            run_id=run_id,
            csv_source_sha256=csv_source_sha256,
        )

    if not master_integrity_rust_enabled():
        return _oracle()

    # The routed arm (the G6 shape): the payload state — dispositions,
    # counts, dedup, special set — is stashed for the materializer (the
    # facade calls the factory before the crate and the materializer only
    # after a clean, gate-passing crate return).
    state: dict[str, Any] = {}

    def _payload() -> MasterIntegrityPayload:
        reverse_aliases = {v: k for k, v in (index_aliases or {}).items()}
        markers = tuple(m.upper() for m in test_underlying_markers)
        sym_intern: dict[tuple[str, str], int] = {}
        tok_intern: dict[tuple[str, int], int] = {}
        identity_intern: dict[_Identity, int] = {}
        spot_sym_key_ids: list[int] = []
        spot_identity_ids: list[int] = []
        spot_token_key_ids: list[int] = []
        seen_dedup: set[tuple[Any, ...]] = set()
        counts = dict.fromkeys(InstrumentType, 0)
        dedup_collisions = 0

        # Pass 1 — EXACTLY the oracle's pass 1, with the spot tables
        # replaced by interned-id VECTORS (the crate replays the dict-op
        # sequence; the Decimal-bearing dedup + type counts stay HERE).
        for inst in instruments:
            counts[inst.instrument] += 1
            key = _dedup_key(inst)
            if key in seen_dedup:
                dedup_collisions += 1
            else:
                seen_dedup.add(key)
            if inst.instrument in (InstrumentType.IDX, InstrumentType.EQ):
                exch = inst.exch_id.value
                identity: _Identity = (exch, inst.security_id)
                identity_id = identity_intern.setdefault(identity, len(identity_intern))
                sym_id = sym_intern.setdefault((exch, inst.symbol_name), len(sym_intern))
                tok_id = _NO_KEY
                if _has_genuine_numeric_token(inst):
                    # The token key IS the identity tuple — interned in its
                    # OWN id space (shared with the LEG token keys below so
                    # the crate's lookups match the oracle's dict hits).
                    tok_id = tok_intern.setdefault(identity, len(tok_intern))
                spot_sym_key_ids.append(sym_id)
                spot_identity_ids.append(identity_id)
                spot_token_key_ids.append(tok_id)

        # Pass 2 — the oracle's leg filter + key derivation (the marker /
        # special-char Unicode predicates + the alias reversal stay HERE);
        # empty/test legs are Python-filtered BEFORE the vectors, recorded
        # as dispositions so the materializer interleaves them in input
        # order exactly like the oracle's sample walk.
        leg_dispositions: list[tuple[Instrument, str]] = []
        leg_sym_key_ids: list[int] = []
        leg_tok_key_ids: list[int] = []
        special: set[str] = set()
        for inst in instruments:
            if inst.instrument not in (InstrumentType.FUT, InstrumentType.CE, InstrumentType.PE):
                continue
            exch = inst.exch_id.value
            under_sym = inst.underlying_symbol
            under_tok = inst.underlying_security_id
            if not under_sym and under_tok is None:
                leg_dispositions.append((inst, _DISP_EMPTY))
                continue
            if under_sym and any(m in under_sym.upper() for m in markers):
                leg_dispositions.append((inst, _DISP_TEST))
                continue
            if under_sym and any(ch in _SPECIAL_CHARS for ch in under_sym):
                special.add(under_sym)
            sym_id = (
                sym_intern.setdefault(
                    (exch, reverse_aliases.get(under_sym, under_sym)), len(sym_intern)
                )
                if under_sym
                else _NO_KEY
            )
            tok_id = (
                tok_intern.setdefault((exch, under_tok), len(tok_intern))
                if under_tok is not None
                else _NO_KEY
            )
            leg_dispositions.append((inst, _DISP_ROUTED))
            leg_sym_key_ids.append(sym_id)
            leg_tok_key_ids.append(tok_id)

        state["counts"] = counts
        state["dedup_collisions"] = dedup_collisions
        state["leg_dispositions"] = leg_dispositions
        state["special"] = special
        return MasterIntegrityPayload(
            spot_sym_key_ids=tuple(spot_sym_key_ids),
            spot_identity_ids=tuple(spot_identity_ids),
            spot_token_key_ids=tuple(spot_token_key_ids),
            leg_sym_key_ids=tuple(leg_sym_key_ids),
            leg_tok_key_ids=tuple(leg_tok_key_ids),
            identity_count=len(identity_intern),
        )

    def _materialize(bucket_codes: Sequence[int], unreferenced: int) -> MasterIntegrityReport:
        resolved_both = resolved_symbol_only = resolved_token_only = 0
        orphan = ambiguous = disagreement = empty_underlying = test_instrument = 0
        orphan_sample: list[OrphanLeg] = []

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

        # The input-order walk: empty/test legs interleave with the routed
        # legs' crate codes exactly as the oracle's single loop records
        # them (one cursor over the code list — one source of ordering).
        cursor = 0
        leg_dispositions: list[tuple[Instrument, str]] = state["leg_dispositions"]
        for inst, disposition in leg_dispositions:
            if disposition is _DISP_EMPTY:
                empty_underlying += 1
                _record_orphan(inst, REASON_EMPTY_UNDERLYING)
            elif disposition is _DISP_TEST:
                test_instrument += 1
            else:
                code = bucket_codes[cursor]
                cursor += 1
                if code == _CODE_RESOLVED_BOTH:
                    resolved_both += 1
                elif code == _CODE_SYMBOL_ONLY:
                    resolved_symbol_only += 1
                elif code == _CODE_TOKEN_ONLY:
                    resolved_token_only += 1
                elif code == _CODE_ORPHAN:
                    orphan += 1
                    _record_orphan(inst, REASON_ORPHAN)
                elif code == _CODE_AMBIGUOUS:
                    ambiguous += 1
                    _record_orphan(inst, REASON_AMBIGUOUS)
                else:  # _CODE_DISAGREEMENT — the gate holds every code < 6
                    disagreement += 1
                    _record_orphan(inst, REASON_DISAGREEMENT)
        counts: dict[InstrumentType, int] = state["counts"]
        special: set[str] = state["special"]
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
            dedup_collisions=state["dedup_collisions"],
            spots_with_no_legs=unreferenced,
            test_instrument_count=test_instrument,
            special_char_count=len(special),
            special_char_underlyings=tuple(sorted(special))[:SPECIAL_CHAR_SAMPLE_CAP],
            orphan_legs=tuple(orphan_sample),
        )

    return routed_audit_master_integrity(
        payload_factory=_payload, python_law=_oracle, materialize=_materialize
    )


def _audit_master_integrity_python(
    instruments: Sequence[Instrument],
    *,
    index_aliases: Mapping[str, str] | None = None,
    test_underlying_markers: Sequence[str] = DEFAULT_TEST_UNDERLYING_MARKERS,
    now: datetime,
    run_id: str = "",
    csv_source_sha256: str = "",
) -> MasterIntegrityReport:
    """The verbatim audit body — the LIVE parity oracle (T24 PR-3a).

    The whole pre-routing body of :func:`audit_master_integrity`, moved
    token-for-token (the Slice-0 extraction discipline); both arms of the
    routed audit serve or burn-in-compare against exactly this body —
    including the pass-1 FIRST-identity/ambiguity precedence (the stored
    ``spot_by_symbol`` mapping stays the first identity while
    ``sym_ambiguous`` wins the classification) the crate transcribes.
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
    markers = tuple(m.upper() for m in test_underlying_markers)
    resolved_both = resolved_symbol_only = resolved_token_only = 0
    orphan = ambiguous = disagreement = empty_underlying = test_instrument = 0
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

        if not under_sym and under_tok is None:
            empty_underlying += 1
            _record_orphan(inst, REASON_EMPTY_UNDERLYING)
            continue

        # Exchange TEST instruments (*NSETEST*/*BSETEST*) have no real spot — count + report
        # them in their own non-gating bucket so they never read as perpetual false orphans
        # (they are NOT silently dropped — Golden Rule #10). Checked BEFORE the special-char
        # tally so a test leg never inflates the special-char count.
        if under_sym and any(m in under_sym.upper() for m in markers):
            test_instrument += 1
            continue

        if under_sym and any(ch in _SPECIAL_CHARS for ch in under_sym):
            special.add(under_sym)

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
        test_instrument_count=test_instrument,
        special_char_count=len(special),
        special_char_underlyings=tuple(sorted(special))[:SPECIAL_CHAR_SAMPLE_CAP],
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
        "test_instrument_count": report.test_instrument_count,
        "special_char_count": report.special_char_count,
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
        test_instrument_count=payload["test_instrument_count"],
        special_char_count=payload["special_char_count"],
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
    "DEFAULT_TEST_UNDERLYING_MARKERS",
    "GATING_REASONS",
    "META_KEY",
    "ORPHAN_SAMPLE_CAP",
    "REASON_AMBIGUOUS",
    "REASON_DISAGREEMENT",
    "REASON_EMPTY_UNDERLYING",
    "REASON_ORPHAN",
    "SPECIAL_CHAR_SAMPLE_CAP",
    "MasterIntegrityReport",
    "OrphanLeg",
    "TypeCounts",
    "audit_master_integrity",
    "report_from_dict",
    "report_to_dict",
]
