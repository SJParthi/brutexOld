"""Instrument-master ECONOMICS integrity audit — the HONEST lot/tick invariants.

DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001. The economics sibling of the STRUCTURAL mapping
audit (DEC-INSTRUMENT-MASTER-INTEGRITY-001, #874): a PURE, offline, parse-time audit over the
FUT/CE/PE legs' contract economics (``lot_size`` / ``tick_size`` / ``freeze_quantity``) that
checks ONLY the invariant that is genuinely true of the live vendor master — never crying wolf
on legitimate variation (Golden Rule #1 / #10).

## Why this audit is deliberately NARROW (grounded in the real 164k-row master)

Two "obvious" checks were verified WRONG / redundant against the operator's actual
``instrument.csv`` and are therefore NOT done:

* **lot/tick POSITIVITY is already guaranteed upstream** — :meth:`Instrument.__post_init__`
  fail-CLOSES on a non-positive ``lot_size``/``tick_size`` for any non-index instrument
  (``models.py``), so a leg with ``lot_size <= 0`` can never be constructed; re-checking it
  here would be dead, untestable code.
* **tick_size is NOT uniform within an expiry** — it legitimately varies by strike / premium
  band (e.g. 360ONE 0.05 vs 0.10 — 350 such groups in the real master), so tick uniformity is
  deliberately NOT checked.

## The ONE real invariant this adds (that the per-leg model validation can't see)

* **lot_size is uniform PER (underlying, expiry)** — one expiry's whole chain (every strike,
  CE+PE, + the future) shares ONE lot. 0 violations in the real master; a lot that DIFFERS
  within a single expiry is a genuine data bug the per-leg validator misses → **GATING**.
  lot_size legitimately **REVISES across expiries** (NSE quarterly / post-split revisions —
  e.g. AMBUJACEM 1050→1200, 45 names in the real master), so cross-expiry variation is
  reported as **informational** (``underlyings_with_lot_revisions``), never gated.

``is_green`` ⟺ no within-expiry lot mismatch. Exchange TEST legs (``*NSETEST*``) are excluded
(same DYNAMIC markers as the mapping audit). DETECT-only; one O(legs) pass, O(1) per lookup,
O(expiry-groups) space; samples capped so the persisted summary never bloats.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from brutex.data.detectors_rust import (
    EconomicsIntegrityPayload,
    economics_integrity_rust_enabled,
    routed_audit_economics_integrity,
)
from brutex.data.master_integrity import DEFAULT_TEST_UNDERLYING_MARKERS
from brutex.models import Instrument, InstrumentType

#: Cap on each capped sample (full counts are always exact).
SAMPLE_CAP: Final[int] = 200

#: The ``schema_meta`` key the ingest hook persists the economics audit JSON under.
META_KEY: Final[str] = "economics_integrity"

_LEG_TYPES: Final[frozenset[InstrumentType]] = frozenset(
    {InstrumentType.FUT, InstrumentType.CE, InstrumentType.PE}
)


@dataclass(frozen=True, slots=True)
class LotMismatch:
    """One (underlying, expiry) group whose legs do NOT share a single lot_size (a real bug)."""

    exchange: str
    underlying: str
    expiry: str  # ISO date or ""
    lots: tuple[int, ...]  # the distinct lot sizes found within the one expiry, sorted


@dataclass(frozen=True, slots=True)
class LotRevision:
    """One underlying whose lot_size legitimately CHANGED across expiries (informational)."""

    exchange: str
    underlying: str
    lots: tuple[int, ...]  # the distinct lot sizes across expiries, sorted


@dataclass(frozen=True, slots=True)
class EconomicsIntegrityReport:
    """The economics-integrity verdict over one parsed master + its drill-down."""

    generated_at: str
    run_id: str
    csv_source_sha256: str
    total_legs: int
    expiry_groups: int
    lot_mismatch_expiries: int  # GATING — distinct lot within one (underlying, expiry)
    underlyings_with_lot_revisions: int  # informational (legit cross-expiry revisions)
    mismatches: tuple[LotMismatch, ...]  # capped sample of the gating failures
    lot_revisions: tuple[LotRevision, ...]  # capped sample of the informational revisions

    @property
    def gating_failures(self) -> int:
        """The total gated economics failures (within-expiry lot mismatches)."""
        return self.lot_mismatch_expiries

    @property
    def is_green(self) -> bool:
        """``True`` iff no (underlying, expiry) group has more than one lot_size."""
        return self.gating_failures == 0


def audit_economics_integrity(
    instruments: Sequence[Instrument],
    *,
    test_underlying_markers: Sequence[str] = DEFAULT_TEST_UNDERLYING_MARKERS,
    now: datetime,
    run_id: str = "",
    csv_source_sha256: str = "",
) -> EconomicsIntegrityReport:
    """Audit the parsed *instruments* master's lot economics → a report.

    One linear sweep over the FUT/CE/PE legs (O(legs) time, O(1) per lookup, O(expiry-groups)
    space): collects the distinct lot per (underlying, expiry) and per underlying. Then
    derives the gated within-expiry lot mismatches + the informational cross-expiry revisions.
    Exchange TEST legs are skipped. lot/tick positivity is guaranteed upstream by the model.

    Parameters
    ----------
    instruments
        The FULL parsed master (leg-bearing) — e.g. the Groww ``InstrumentIndex.universe``.
    test_underlying_markers
        Underlying markers identifying exchange TEST legs to exclude (DYNAMIC).
    now, run_id, csv_source_sha256
        Provenance echoed into the report header.

    Returns
    -------
    EconomicsIntegrityReport
        The verdict + counts + capped samples.

    Routing (DEC-DATA-RUST-INTEGRITY-DETECTORS-001, SLICE T24 PR-3a): the
    group folds + verdict derivation are served by
    :func:`_audit_economics_integrity_python` — the verbatim body, the
    LIVE parity oracle — via the default-OFF
    ``BRUTEX_LAKE_RUST_ECONOMICS_INTEGRITY`` facade
    (:mod:`brutex.data.detectors_rust`). NO string semantics enter Rust:
    the routed payload interns every key to a dense id and pre-computes
    the Unicode-sensitive predicates (the ``*NSETEST*`` ``.upper()``
    marker containment, the empty-underlying skip) Python-side, and the
    report-visible capped-sample ORDER (the full-key string sort) stays
    Python in the materializer. Flag OFF (the default) returns the oracle
    directly — the crate is never imported, byte-identical to the
    pre-facade law. Every crate fault under the flag resolves to the
    ORACLE report — never a new exception class out of this audit.

    HONEST (the facade's ceremony disclosure): the routed payload build is
    itself an O(legs) Python pass with dicts — the same complexity class
    as the fold it replaces (payoff ≈ 0, the ADR-0020 banner) — and the
    audit runs ~once per master load, so burn-in effectively means
    compare-always in production (a permanent live differential).
    """

    def _oracle() -> EconomicsIntegrityReport:
        return _audit_economics_integrity_python(
            instruments,
            test_underlying_markers=test_underlying_markers,
            now=now,
            run_id=run_id,
            csv_source_sha256=csv_source_sha256,
        )

    if not economics_integrity_rust_enabled():
        return _oracle()

    # The routed arm (the G6 shape): Python interns the keys ONCE — the
    # payload state is stashed for the materializer (the facade calls the
    # factory before the crate and the materializer only after a clean,
    # gate-passing crate return, so the ordering is structural).
    state: dict[str, Any] = {}

    def _payload() -> EconomicsIntegrityPayload:
        markers = tuple(m.upper() for m in test_underlying_markers)
        group_intern: dict[tuple[str, str, date | None], int] = {}
        under_intern: dict[tuple[str, str], int] = {}
        group_ids: list[int] = []
        under_ids: list[int] = []
        lots: list[int] = []
        total_legs = 0
        # EXACTLY the oracle's leg filter — non-leg rows, empty underlyings
        # and marker-excluded legs never reach the vectors (the Unicode
        # `.upper()` containment stays HERE, never in Rust).
        for inst in instruments:
            if inst.instrument not in _LEG_TYPES:
                continue
            under = inst.underlying_symbol
            if not under or any(m in under.upper() for m in markers):
                continue
            total_legs += 1
            exch = inst.exch_id.value
            group_key = (exch, under, inst.sm_expiry_date)
            group_id = group_intern.setdefault(group_key, len(group_intern))
            under_key = (exch, under)
            under_id = under_intern.setdefault(under_key, len(under_intern))
            group_ids.append(group_id)
            under_ids.append(under_id)
            lots.append(inst.lot_size)
        # Dict insertion order ⇒ list index == interned id.
        state["group_keys"] = list(group_intern)
        state["under_keys"] = list(under_intern)
        state["total_legs"] = total_legs
        return EconomicsIntegrityPayload(
            group_ids=tuple(group_ids),
            under_ids=tuple(under_ids),
            lots=tuple(lots),
            group_count=len(group_intern),
            under_count=len(under_intern),
        )

    def _materialize(
        mismatch_group_ids: Sequence[int],
        mismatch_lots: Sequence[Sequence[int]],
        revision_under_ids: Sequence[int],
        revision_lots: Sequence[Sequence[int]],
    ) -> EconomicsIntegrityReport:
        group_keys: list[tuple[str, str, date | None]] = state["group_keys"]
        under_keys: list[tuple[str, str]] = state["under_keys"]
        # The report-visible SAMPLE ORDER law stays Python (the oracle's
        # full-key sort — input-order-independent): sort the flagged ids on
        # the SAME (exch, under, expiry-iso) key, then cap. The crate's
        # per-group lots are already sorted-distinct ascending.
        mismatch_pairs = sorted(
            zip(mismatch_group_ids, mismatch_lots, strict=True),
            key=lambda pair: _group_sort_key(group_keys[pair[0]]),
        )
        mismatches = tuple(
            _mismatch_entry(group_keys[group_id], lots)
            for group_id, lots in mismatch_pairs[:SAMPLE_CAP]
        )
        revision_pairs = sorted(
            zip(revision_under_ids, revision_lots, strict=True),
            key=lambda pair: under_keys[pair[0]],
        )
        revisions = tuple(
            LotRevision(
                exchange=under_keys[under_id][0],
                underlying=under_keys[under_id][1],
                lots=tuple(lots),
            )
            for under_id, lots in revision_pairs[:SAMPLE_CAP]
        )
        return EconomicsIntegrityReport(
            generated_at=now.isoformat(),
            run_id=run_id,
            csv_source_sha256=csv_source_sha256,
            total_legs=state["total_legs"],
            expiry_groups=len(group_keys),
            lot_mismatch_expiries=len(mismatch_pairs),
            underlyings_with_lot_revisions=len(revision_pairs),
            mismatches=mismatches,
            lot_revisions=revisions,
        )

    return routed_audit_economics_integrity(
        payload_factory=_payload, python_law=_oracle, materialize=_materialize
    )


def _group_sort_key(group_key: tuple[str, str, date | None]) -> tuple[str, str, str]:
    """The routed materializer's group sort key — the oracle's sort law verbatim."""
    exch, under, expiry = group_key
    return (exch, under, expiry.isoformat() if expiry else "")


def _mismatch_entry(group_key: tuple[str, str, date | None], lots: Sequence[int]) -> LotMismatch:
    """One routed-arm mismatch sample entry — the oracle's construction verbatim.

    The ``else ""`` arm is the SAME defensive totality as the oracle's (a
    parsed FUT/CE/PE leg always carries ``sm_expiry_date`` — the model's
    ``__post_init__`` refuses a ``None`` — so the arm is latently
    unreachable but keeps the key total).
    """
    exch, under, expiry = group_key
    return LotMismatch(
        exchange=exch,
        underlying=under,
        expiry=expiry.isoformat() if expiry else "",
        lots=tuple(lots),
    )


def _audit_economics_integrity_python(
    instruments: Sequence[Instrument],
    *,
    test_underlying_markers: Sequence[str] = DEFAULT_TEST_UNDERLYING_MARKERS,
    now: datetime,
    run_id: str = "",
    csv_source_sha256: str = "",
) -> EconomicsIntegrityReport:
    """The verbatim audit body — the LIVE parity oracle (T24 PR-3a).

    The whole pre-routing body of :func:`audit_economics_integrity`, moved
    token-for-token (the Slice-0 extraction discipline); both arms of the
    routed audit serve or burn-in-compare against exactly this body.
    """
    markers = tuple(m.upper() for m in test_underlying_markers)
    lots_by_expiry: dict[tuple[str, str, date | None], set[int]] = {}
    lots_by_under: dict[tuple[str, str], set[int]] = {}
    total_legs = 0

    for inst in instruments:
        if inst.instrument not in _LEG_TYPES:
            continue
        under = inst.underlying_symbol
        if not under or any(m in under.upper() for m in markers):
            continue
        total_legs += 1
        exch = inst.exch_id.value
        lots_by_expiry.setdefault((exch, under, inst.sm_expiry_date), set()).add(inst.lot_size)
        lots_by_under.setdefault((exch, under), set()).add(inst.lot_size)

    mismatches: list[LotMismatch] = []
    lot_mismatch = 0
    # Sort on the FULL key (exch, under, expiry-string) so the capped sample is
    # input-order-independent; a None expiry sorts first as "" (it never occurs for a
    # parsed leg — sm_expiry_date is always present for FUT/CE/PE — but the key stays total).
    for (exch, under, expiry), lots in sorted(
        lots_by_expiry.items(),
        key=lambda kv: (kv[0][0], kv[0][1], kv[0][2].isoformat() if kv[0][2] else ""),
    ):
        if len(lots) > 1:
            lot_mismatch += 1
            if len(mismatches) < SAMPLE_CAP:
                mismatches.append(
                    LotMismatch(
                        exchange=exch,
                        underlying=under,
                        # Defensive ``else ""``: a parsed leg always carries sm_expiry_date,
                        # so the empty arm is latently unreachable but keeps the type total.
                        expiry=expiry.isoformat() if expiry else "",
                        lots=tuple(sorted(lots)),
                    )
                )

    revisions: list[LotRevision] = []
    revision_count = 0
    for (exch, under), lots in sorted(lots_by_under.items()):
        if len(lots) > 1:
            revision_count += 1
            if len(revisions) < SAMPLE_CAP:
                revisions.append(
                    LotRevision(exchange=exch, underlying=under, lots=tuple(sorted(lots)))
                )

    return EconomicsIntegrityReport(
        generated_at=now.isoformat(),
        run_id=run_id,
        csv_source_sha256=csv_source_sha256,
        total_legs=total_legs,
        expiry_groups=len(lots_by_expiry),
        lot_mismatch_expiries=lot_mismatch,
        underlyings_with_lot_revisions=revision_count,
        mismatches=tuple(mismatches),
        lot_revisions=tuple(revisions),
    )


def report_to_dict(report: EconomicsIntegrityReport) -> dict[str, Any]:
    """Project an :class:`EconomicsIntegrityReport` to JSON (route + schema_meta)."""
    return {
        "generated_at": report.generated_at,
        "run_id": report.run_id,
        "csv_source_sha256": report.csv_source_sha256,
        "is_green": report.is_green,
        "total_legs": report.total_legs,
        "expiry_groups": report.expiry_groups,
        "gating_failures": report.gating_failures,
        "lot_mismatch_expiries": report.lot_mismatch_expiries,
        "underlyings_with_lot_revisions": report.underlyings_with_lot_revisions,
        "mismatches": [
            {
                "exchange": m.exchange,
                "underlying": m.underlying,
                "expiry": m.expiry,
                "lots": list(m.lots),
            }
            for m in report.mismatches
        ],
        "lot_revisions": [
            {"exchange": r.exchange, "underlying": r.underlying, "lots": list(r.lots)}
            for r in report.lot_revisions
        ],
    }


def report_from_dict(payload: dict[str, Any]) -> EconomicsIntegrityReport:
    """Reconstruct an :class:`EconomicsIntegrityReport` from :func:`report_to_dict` (CSV route)."""
    return EconomicsIntegrityReport(
        generated_at=payload["generated_at"],
        run_id=payload["run_id"],
        csv_source_sha256=payload["csv_source_sha256"],
        total_legs=payload["total_legs"],
        expiry_groups=payload["expiry_groups"],
        lot_mismatch_expiries=payload["lot_mismatch_expiries"],
        underlyings_with_lot_revisions=payload["underlyings_with_lot_revisions"],
        mismatches=tuple(
            LotMismatch(
                exchange=m["exchange"],
                underlying=m["underlying"],
                expiry=m["expiry"],
                lots=tuple(m["lots"]),
            )
            for m in payload["mismatches"]
        ),
        lot_revisions=tuple(
            LotRevision(exchange=r["exchange"], underlying=r["underlying"], lots=tuple(r["lots"]))
            for r in payload["lot_revisions"]
        ),
    )


__all__ = [
    "META_KEY",
    "SAMPLE_CAP",
    "EconomicsIntegrityReport",
    "LotMismatch",
    "LotRevision",
    "audit_economics_integrity",
    "report_from_dict",
    "report_to_dict",
]
