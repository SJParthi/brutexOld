"""Python ``StrategySpec`` + the spec run-identity wiring — DEC-SPEC-IDENTITY-WIRING-001.

PR-4b sub-PR: the Python mirror of the FROZEN shared-Rust
``trading_core.StrategySpec`` (DEC-STRATEGYSPEC-CORE-001, MIGRATION.md #1027)
plus the wiring that feeds its ``canonical_bytes`` fingerprint into the
EXISTING 9-field BLAKE3 :func:`brutex.strategies.combo.combo_hash` — so a
strategy-as-DATA declaration gains a reproducible run-identity (Golden Rule
#4) without touching the wire-locked ``combo_hash`` formula or the shipped
``mask_run_id`` (``"mask:%032x"``) namespace.

Design rules (hostile-review-hardened; see the PR-4b build spec):

* **ONE encoder, in Rust.** :meth:`StrategySpec.canonical_bytes` calls the
  PyO3 ``trading_core.spec_canonical_bytes`` binding — the byte layout is
  NEVER re-implemented in Python, so the fingerprint is bit-identical to
  TickVault's native ``canonical_bytes`` by construction (CLAUDE.md §11
  COMMON). One FFI call per combo at IDENTITY time, O(1) per combo.
* **Constructed, not policed params.** :func:`spec_run_id` takes the
  sim knobs as NAMED arguments and builds its params dict internally via
  :func:`canonical_spec_params` — there is no free-form mapping through
  which a caller could double-carry a spec-carried axis (mask / direction /
  proximity / prior_n / symbol) or omit an eval-affecting one. Every
  eval-affecting axis NOT carried by ``canonical_bytes`` or a fixed
  ``combo_hash`` field MUST ride params, in one of TWO emit classes
  (mirroring ``canonical_trade_params``):

  1. **Unconditionally emitted** — ``{fib_anchor}`` (the anchor changes the
     Group-4 Fib levels a spec evaluates against — two anchors are two
     computations and MUST get two run-identities).
  2. **Optionally emitted when non-default** — ``{vwap_source, ema_source}``
     (DEC-NIFTY-PRICE-SOURCE-AXIS-001, Override 1). The swept VWAP / EMA
     price-source axis is eval-affecting (bits 52-53 / 0-5) but is OMITTED at
     its default ``(HLC3, CLOSE)`` and emitted only when non-default, so
     existing run_ids stay byte-identical (a hard back-compat invariant for
     the sibling Rust-core owner's + product-audit's twin-baseline capture).
     Absent ALWAYS means the default — a clean bijection, no ambiguity.
* **Shared rules vs BruteX policy.** ``__post_init__`` mirrors the Rust
  binding's guard set (mask ⊆ ``IMPLEMENTED_MASK``, direction ∈ {Long,
  Short}, each proximity ∈ ``[0, MAX_PROXIMITY_BPS]``, ``prior_n`` a u64)
  PLUS two explicitly BruteX-side POLICIES the shared crate deliberately
  does not enforce: ``symbol_id`` 0 is RESERVED (the crate accepts 0) and
  every ``symbol_id`` must be registered here. A rejection-parity test pins
  each shared row to raise on BOTH sides.
* **Opaque, append-only symbol ids.** BruteX is the SOLE assigner of
  ``symbol_id`` values; TickVault consumes only already-encoded bytes/ids.
  The table below is data (config-loadable later per gap D1), never
  hash-derived, never renumbered — a golden pins both directions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

import trading_core

from brutex.strategies.combo import combo_hash
from brutex.strategies.errors import StrategyError
from brutex.strategies.fib_definitions import Direction
from brutex.strategies.types import (
    FibAnchorKind,
    PriceSource,
    price_source_params_fragment,
)
from brutex.vocabulary.bits import IMPLEMENTED_MASK

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from brutex.strategies.fib_definitions import StrategyDefinition

__all__ = [
    "MAX_PRIOR_N",
    "MAX_PROXIMITY_BPS",
    "SPEC_STRATEGY_ID_PREFIX",
    "SYMBOL_IDS",
    "StrategySpec",
    "canonical_spec_params",
    "spec_run_id",
    "symbol_id_for",
    "symbol_label_for",
]

#: Ceiling for a proximity ``bps`` value — MIRRORS the Rust boundary constant
#: ``python.rs::MAX_BPS`` (1,000,000 bps = 10,000%, astronomically above any
#: real tolerance; the golden max is 50). A test pins the two constants equal
#: via a boundary probe so they cannot drift apart silently.
MAX_PROXIMITY_BPS: Final[int] = 1_000_000

#: ``canonical_bytes`` encodes ``prior_n`` as a fixed u64 — the Python mirror
#: enforces the same width so a valid :class:`StrategySpec` is ALWAYS
#: encodable (a raw binding call beyond this raises PyO3 ``OverflowError``,
#: the documented backstop).
MAX_PRIOR_N: Final[int] = 2**64 - 1

#: ``symbol_id`` is a u32 on the wire.
_MAX_SYMBOL_ID: Final[int] = 2**32 - 1

#: The spec-axis ``strategy_id`` namespace — disjoint from the shipped
#: ``"mask:"`` namespace (DEC-MASK-IDENTITY-001) by literal prefix.
SPEC_STRATEGY_ID_PREFIX: Final[str] = "spec:"

#: The APPEND-ONLY symbol-id assignment table (BruteX is the sole assigner).
#: id 0 is RESERVED (the unassigned sentinel — construction rejects it).
#: NEVER renumber an existing pair and NEVER rename a label in place (a
#: rename-keeping-id would mis-decode every historical strategy_id); a new
#: instrument gets the next free id in a reviewed PR (the NIFTY-futures id
#: landed as row 4 with the futures-sweep launch PR —
#: DEC-FUTURES-ADDRESSING-PIN-001). Golden-pinned in both directions.
_SYMBOL_ID_TABLE: Final[tuple[tuple[str, int], ...]] = (
    ("NSE-NIFTY", 1),
    ("NSE-BANKNIFTY", 2),
    ("BSE-SENSEX", 3),
    # ADR-0012 NIFTY-futures dynamic-strategy track — the SERIES-level label
    # (== brutex.universe.NIFTY_FUTURES_SERIES_SYMBOL, import-identity-pinned),
    # appended by DEC-FUTURES-ADDRESSING-PIN-001 per the #1138 [C16] reservation.
    # ONE row for the track — per-contract labels are deliberately NOT registered
    # (an append-only-forever registry cannot absorb ~12 immortal rows/year;
    # per-contract grain rides mask_run_id's verbatim instrument + the
    # full-content data_snapshot_hash, which are per-launch by construction).
    ("NSE-NIFTY-FUT", 4),
)


def _build_registry(
    table: tuple[tuple[str, int], ...],
) -> tuple[Mapping[str, int], Mapping[int, str]]:
    """Build the forward + reverse symbol maps, RAISING on any invariant break.

    "Injective by construction" is enforced code, not a comment: a duplicate
    label, duplicate id, blank label, reserved id 0, or an id outside the u32
    range fails at import time (:class:`StrategyError`), so a bad table can
    never silently mint ambiguous identities. The reverse map is precomputed
    (O(1) lookup, no per-call scan).

    Parameters
    ----------
    table
        ``(label, symbol_id)`` pairs, append-only.

    Returns
    -------
    tuple[Mapping[str, int], Mapping[int, str]]
        Read-only ``label -> id`` and ``id -> label`` views.

    Raises
    ------
    StrategyError
        On any duplicate / blank / out-of-range entry.
    """
    forward: dict[str, int] = {}
    reverse: dict[int, str] = {}
    for label, symbol_id in table:
        if not label:
            raise StrategyError("symbol registry: label must be non-empty")
        if not 0 < symbol_id <= _MAX_SYMBOL_ID:
            raise StrategyError(
                f"symbol registry: id {symbol_id} for {label!r} outside [1, {_MAX_SYMBOL_ID}] "
                "(0 is the RESERVED unassigned sentinel)"
            )
        if label in forward:
            raise StrategyError(f"symbol registry: duplicate label {label!r}")
        if symbol_id in reverse:
            raise StrategyError(
                f"symbol registry: id {symbol_id} assigned to both "
                f"{reverse[symbol_id]!r} and {label!r}"
            )
        forward[label] = symbol_id
        reverse[symbol_id] = label
    return MappingProxyType(forward), MappingProxyType(reverse)


_registry_maps: Final = _build_registry(_SYMBOL_ID_TABLE)

#: Read-only ``label -> symbol_id`` view (the public registry surface).
SYMBOL_IDS: Final[Mapping[str, int]] = _registry_maps[0]

_LABEL_BY_ID: Final[Mapping[int, str]] = _registry_maps[1]


def symbol_id_for(label: str) -> int:
    """Return the registered opaque ``symbol_id`` for *label*.

    Parameters
    ----------
    label
        A registered instrument label (e.g. ``"NSE-NIFTY"``).

    Returns
    -------
    int
        The frozen u32 symbol id.

    Raises
    ------
    StrategyError
        If *label* is not registered (loud — never a guessed/hashed id).
    """
    try:
        return SYMBOL_IDS[label]
    except KeyError:
        raise StrategyError(
            f"symbol_id_for: unregistered instrument label {label!r} "
            f"(registered: {sorted(SYMBOL_IDS)})"
        ) from None


def symbol_label_for(symbol_id: int) -> str:
    """Return the instrument label a registered ``symbol_id`` decodes to.

    Parameters
    ----------
    symbol_id
        A registered opaque id.

    Returns
    -------
    str
        The frozen instrument label.

    Raises
    ------
    StrategyError
        If *symbol_id* is not registered.
    """
    try:
        return _LABEL_BY_ID[symbol_id]
    except KeyError:
        raise StrategyError(
            f"symbol_label_for: unregistered symbol_id {symbol_id} "
            f"(registered: {sorted(_LABEL_BY_ID)})"
        ) from None


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """Python mirror of the FROZEN Rust ``trading_core.StrategySpec`` (#1027).

    A DECLARATION, not behaviour: the required-condition u128 mask, the
    direction, the proximity tolerances, the prior-bar look-back, and the
    opaque instrument token. Field order and widths deliberately mirror
    ``spec.rs``; the canonical encode is delegated to the Rust binding
    (:meth:`canonical_bytes`) — never re-implemented here.

    Attributes
    ----------
    condition_mask
        Required-bits u128 mask; must be a subset of
        :data:`brutex.vocabulary.bits.IMPLEMENTED_MASK` (a spec requiring a
        reserved bit could only ever be a silent dead strategy — refused).
    direction
        Swept trade direction (:class:`~brutex.strategies.fib_definitions.Direction`).
    pivot_proximity_bps, pdh_pdl_proximity_bps, fib_proximity_bps
        The ``near_*`` tolerances (bps), each in ``[0, MAX_PROXIMITY_BPS]`` —
        mirrors the Rust ``ProximityParams`` fields in encode order.
    prior_n
        Prior-bar look-back (encoded as u64).
    symbol_id
        Opaque registered instrument token; ``0`` is RESERVED (BruteX POLICY
        — the shared crate itself accepts 0) and every id must resolve via
        :func:`symbol_label_for`.
    """

    condition_mask: int
    direction: Direction
    pivot_proximity_bps: int
    pdh_pdl_proximity_bps: int
    fib_proximity_bps: int
    prior_n: int
    symbol_id: int

    def __post_init__(self) -> None:
        if self.condition_mask < 0:
            raise StrategyError(
                f"StrategySpec.condition_mask must be non-negative, got {self.condition_mask}"
            )
        unimplemented = self.condition_mask & ~IMPLEMENTED_MASK
        if unimplemented:
            raise StrategyError(
                "StrategySpec.condition_mask requires unimplemented/reserved bits "
                f"({unimplemented:#034x}); only bits in IMPLEMENTED_MASK "
                f"({IMPLEMENTED_MASK:#034x}) may be required"
            )
        if not isinstance(self.direction, Direction):
            raise StrategyError(
                f"StrategySpec.direction must be a Direction, got {type(self.direction).__name__}"
            )
        for name, bps in (
            ("pivot_proximity_bps", self.pivot_proximity_bps),
            ("pdh_pdl_proximity_bps", self.pdh_pdl_proximity_bps),
            ("fib_proximity_bps", self.fib_proximity_bps),
        ):
            if not 0 <= bps <= MAX_PROXIMITY_BPS:
                raise StrategyError(f"StrategySpec.{name} ({bps}) outside [0, {MAX_PROXIMITY_BPS}]")
        if not 0 <= self.prior_n <= MAX_PRIOR_N:
            raise StrategyError(
                f"StrategySpec.prior_n ({self.prior_n}) outside [0, {MAX_PRIOR_N}] "
                "(encoded as a fixed u64)"
            )
        # BruteX POLICY rows (not shared-crate rules): id 0 reserved + must be
        # registered, so every spec run-identity decodes to a real instrument.
        if not 0 < self.symbol_id <= _MAX_SYMBOL_ID:
            raise StrategyError(
                f"StrategySpec.symbol_id ({self.symbol_id}) outside [1, {_MAX_SYMBOL_ID}] "
                "(0 is the RESERVED unassigned sentinel — BruteX policy)"
            )
        symbol_label_for(self.symbol_id)

    def canonical_bytes(self) -> bytes:
        """Return the FROZEN 57-byte v1 canonical encode of this spec.

        Delegates to the Rust ``trading_core.spec_canonical_bytes`` binding —
        the ONE encoder both repos share — so the bytes are bit-identical to
        TickVault's native ``StrategySpec::canonical_bytes`` by construction.
        O(1) per call (one FFI crossing at identity time, not per bar).

        Returns
        -------
        bytes
            ``version(u32 LE) ‖ mask(u128 LE) ‖ direction(u8) ‖ 3×bps(i64 LE)
            ‖ prior_n(u64 LE) ‖ symbol_id(u32 LE)`` — 57 bytes.
        """
        # bytes(...) keeps this typed `bytes` where the trading_core wheel
        # degrades to Any (the isolated pre-commit mypy env — the same
        # hook-portability pattern as combo.py's `str(...)`).
        return bytes(
            trading_core.spec_canonical_bytes(
                f"{self.condition_mask:032x}",
                self.direction.as_u8(),
                self.pivot_proximity_bps,
                self.pdh_pdl_proximity_bps,
                self.fib_proximity_bps,
                self.prior_n,
                self.symbol_id,
            )
        )

    @classmethod
    def from_definition(
        cls,
        definition: StrategyDefinition,
        *,
        condition_mask: int,
        direction: Direction,
        pivot_proximity_bps: int,
        pdh_pdl_proximity_bps: int,
        fib_proximity_bps: int,
        prior_n: int,
        symbol_label: str,
    ) -> StrategySpec:
        """Project one concrete sweep point of a #1030 definition onto a spec.

        Reuses the definition's own :meth:`~brutex.strategies.fib_definitions.
        StrategyDefinition.spec_args` validation (mask within the declared
        condition scope AND *direction* one the strategy actually sweeps), then
        resolves *symbol_label* through the registry — so an out-of-scope mask,
        an unswept direction, or an unregistered symbol all fail loudly before
        any identity is minted.

        Parameters
        ----------
        definition
            The strategy-as-DATA declaration being swept.
        condition_mask, direction
            The concrete sweep point (one mask × one swept direction).
        pivot_proximity_bps, pdh_pdl_proximity_bps, fib_proximity_bps, prior_n
            The evaluator tolerances for this run (spec-carried axes).
        symbol_label
            A registered instrument label (e.g. ``"NSE-NIFTY"``).

        Returns
        -------
        StrategySpec
            The validated concrete spec.
        """
        definition.spec_args(condition_mask, direction)
        return cls(
            condition_mask=condition_mask,
            direction=direction,
            pivot_proximity_bps=pivot_proximity_bps,
            pdh_pdl_proximity_bps=pdh_pdl_proximity_bps,
            fib_proximity_bps=fib_proximity_bps,
            prior_n=prior_n,
            symbol_id=symbol_id_for(symbol_label),
        )


def canonical_spec_params(
    *,
    sl_paisa: int | None,
    tp_paisa: int | None,
    start_equity_paisa: int,
    fib_anchor: FibAnchorKind,
    vwap_source: PriceSource = PriceSource.HLC3,
    ema_source: PriceSource = PriceSource.CLOSE,
) -> dict[str, Any]:
    """Return the ONE params layout inside every spec run-identity (COMMON).

    Invariant: every eval-affecting axis NOT carried by ``canonical_bytes`` or a
    fixed ``combo_hash`` field MUST ride params, in one of TWO emit classes
    (mirroring :func:`brutex.engine.sweep_run.canonical_trade_params`):

    * **Unconditionally emitted** — ``{fib_anchor}``. The anchor genuinely
      changes the Group-4 Fib levels a spec evaluates against, so two anchors are
      two computations and MUST get distinct run-identities. Emitted for EVERY
      family (including prev-day).
    * **Optionally emitted when non-default** — ``{vwap_source, ema_source}``
      (DEC-NIFTY-PRICE-SOURCE-AXIS-001, Override 1). The swept VWAP / EMA
      price-source axis is eval-affecting (bits 52-53 / 0-5) but the keys are
      OMITTED when they equal the default ``(HLC3, CLOSE)`` and emitted only when
      a non-default source is swept. WHY (run-id back-compat): unconditional emit
      would re-key EVERY existing run's identity — a hard break for the sibling
      Rust-core owner's + product-audit's twin-baseline capture, which depend on
      existing run_ids staying byte-identical. Default-omit has no
      canonicalisation ambiguity (absent ALWAYS means the default — a clean
      bijection) and follows the repo's optional-param convention.

    The spec-carried axes (mask / direction / proximity / prior_n / symbol) are
    STRUCTURALLY absent — this builder has no parameter through which to
    double-carry them, so one logical run can never mint two identities (or two
    runs one).

    The swept ``swing_proximity_bps`` band (DEC-FIB-TEMPLATE-EXPRESS-001) is
    deliberately NOT a spec-axis knob: :class:`StrategySpec` carries only the
    frozen bps triple and its 57-byte ``canonical_bytes`` encode never
    serializes a swing band, so the spec path always evaluates at the
    production default (10) and this builder emits NO swing key (absent ⟺ the
    default — trivially, since no spec caller can pass a non-default). A
    spec-axis swing sweep would need its own DEC + a ``SPEC_SCHEMA_VERSION``
    bump.

    Parameters
    ----------
    sl_paisa, tp_paisa
        Stop-loss / take-profit in paisa (``None`` = Mode B signal-exit).
    start_equity_paisa
        Sim starting equity in paisa.
    fib_anchor
        The swept Fib anchor — REQUIRED, emitted UNCONDITIONALLY (never
        omitted for the prev-day default).
    vwap_source, ema_source
        The swept VWAP / EMA price-source axis. DEFAULT-OMITTED (Override 1):
        emitted only when non-default, so a default cell's spec run-identity is
        byte-identical to the pre-axis identity.

    Returns
    -------
    dict[str, Any]
        JSON-canonicalisable params (ints / strings / ``None`` only).
    """
    params: dict[str, Any] = {
        "sl_paisa": sl_paisa,
        "tp_paisa": tp_paisa,
        "start_equity_paisa": start_equity_paisa,
        "fib_anchor": fib_anchor.value,
    }
    # DEFAULT-OMIT the price-source axis (Override 1) via the ONE COMMON fragment
    # (CLAUDE.md §11): absent ⟺ the (HLC3, CLOSE) default, byte-identical identity.
    params.update(price_source_params_fragment(vwap_source, ema_source))
    return params


def spec_run_id(
    spec: StrategySpec,
    *,
    tf: str,
    mode: str,
    sl_paisa: int | None,
    tp_paisa: int | None,
    start_equity_paisa: int,
    fib_anchor: FibAnchorKind,
    vwap_source: PriceSource = PriceSource.HLC3,
    ema_source: PriceSource = PriceSource.CLOSE,
    data_snapshot_hash: str,
    indicator_version: str,
    code_commit_sha: str,
) -> str:
    """Return the 9-field BLAKE3 run-identity of one :class:`StrategySpec`.

    The spec axis enters the EXISTING wire-locked
    :func:`~brutex.strategies.combo.combo_hash` through ONE deterministic
    projection — no new hasher, no ``combo_hash`` signature change:

    * ``strategy_id`` = ``"spec:" + canonical_bytes().hex()`` (114 hex chars)
      — hex-embedded, not pre-hashed: injective, and decodable back to the
      spec GIVEN the frozen v1 layout + the symbol registry. The version
      prefix inside the bytes makes any future encode change a new identity
      namespace. Disjoint from the shipped ``"mask:"`` namespace by literal
      prefix (and both prefixes are REFUSED for free-form
      ``StrategyMeta.strategy_id`` registrations).
    * ``direction`` / ``instrument`` are DERIVED from the spec (its
      ``Direction.value`` token and the registry label), so a spec-vs-field
      inconsistency is impossible by construction.
    * ``params`` comes from :func:`canonical_spec_params` — named knobs only.

    Parameters
    ----------
    spec
        The concrete validated spec.
    tf
        Timeframe label (one of the 21 Track-1 TFs).
    mode
        Exit-mode label (``"A"`` SL/TP, ``"B"`` signal-exit).
    sl_paisa, tp_paisa, start_equity_paisa, fib_anchor
        The sim knobs — see :func:`canonical_spec_params`.
    data_snapshot_hash, indicator_version, code_commit_sha
        Real provenance values (never placeholders — Golden Rule #1/#4).

    Returns
    -------
    str
        The 64-char lowercase BLAKE3 hex run-identity.

    Raises
    ------
    StrategyError
        If any required string field is empty (delegated to ``combo_hash``).
    """
    strategy_id = SPEC_STRATEGY_ID_PREFIX + spec.canonical_bytes().hex()
    return combo_hash(
        strategy_id=strategy_id,
        direction=spec.direction.value,
        instrument=symbol_label_for(spec.symbol_id),
        tf=tf,
        mode=mode,
        params=canonical_spec_params(
            sl_paisa=sl_paisa,
            tp_paisa=tp_paisa,
            start_equity_paisa=start_equity_paisa,
            fib_anchor=fib_anchor,
            vwap_source=vwap_source,
            ema_source=ema_source,
        ),
        data_snapshot_hash=data_snapshot_hash,
        indicator_version=indicator_version,
        code_commit_sha=code_commit_sha,
    )
