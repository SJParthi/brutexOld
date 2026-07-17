"""Read/producer-side scope predicates — DEC-UNIVERSE-INDEX-ONLY-001 (design §3.5).

The lake-walk / window-ledger halves of the #1934 scope law, kept OUT of
:mod:`brutex.config.feed_scope` so that leaf stays parse-free: this module
imports the scope law PLUS the two parse families it judges through —
:mod:`brutex.data.fno_symbol` (the canonical right-anchored F&O grammar) and
:mod:`brutex.data.lake_keys` (the #911 spot drift-recovery fold family).

Every predicate here is **TOTAL + NEVER-RAISING over hostile coordinates**:
an unknown exchange/segment token, an undecodable FNO symbol (``-G1S``), a
legacy ``sid|segment`` window key with an unmapped sid — each degrades to a
counted exclusion under a BOUNDED scope and a pass-through under an unbounded
one. The predicates run at DISCOVERY — OUTSIDE the per-symbol read isolation
(``movers_read_skip_errors`` covers per-symbol READS, not the walk) — so a
planted junk dir must degrade, never kill the whole compute (the #1594
r12-18 planted-tree DoS class).

**Ordering law (Rev 2):** scope judgement runs AFTER spot drift recovery — a
legacy drifted ``NSE-NIFTY`` / ``"NIFTY "`` INDEX dir is
recovered-then-admitted, never silently dropped (raw-key matching would
regress exactly the lake shape DEC-COMPLETENESS-SPOT-KEY-RECOVER-001 exists
to serve). A CASH key recovers then fails ``admits`` under ``index4`` —
same outcome, honest path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from brutex.config.feed_scope import FeedScope
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.lake_keys import spot_key_candidates
from brutex.models import Exchange

if TYPE_CHECKING:  # pragma: no cover — typing-only imports (no runtime edge)
    from collections.abc import Callable, Mapping

#: Window-row classification tokens (shared by the R11 census + the P9
#: skipped-window audit — design §3.5's window form).
WINDOW_ADMITTED: Final[str] = "admitted"
WINDOW_EXCLUDED: Final[str] = "excluded"
#: A LEGACY ``sid|segment`` option-shape row whose sid the admitted-sid map
#: cannot resolve — skipped-not-reopened + counted DISTINCTLY (never silently
#: dropped, never misattributed purely to the DEC).
WINDOW_LEGACY_UNRESOLVED: Final[str] = "legacy_unresolved"

#: The 5-tuple window-ledger key shape (``market, security_id, interval,
#: start, end`` — ``WindowLedger._by_window``'s sort/rotation key).
type WindowKeyTuple = tuple[str, str, str, str, str]


def _coerce_exchange(token: str) -> Exchange | None:
    """``Exchange`` for *token*, or ``None`` for a junk/unknown coordinate."""
    try:
        return Exchange(token)
    except ValueError:
        return None


def _admitted_symbols(scope: FeedScope) -> frozenset[str]:
    """The bounded *scope*'s admitted SYMBOL names (exchange-folded).

    The window ledger's SPOT keys carry NO exchange coordinate
    (``security_id`` is the bare engine ``symbol_name``), so the window form
    judges by name; the dataset form keeps the exchange-keyed judgement.
    """
    return frozenset(sym for _, sym in scope.admitted_underlyings or frozenset())


def _spot_recovered_forms(symbol: str, exchange: str) -> tuple[str, ...]:
    """Identity + the #911 drift-recovery forms of a spot key, in ladder order."""
    return (symbol, *spot_key_candidates(symbol, exchange))


def scope_admits_dataset(exchange: str, segment: str, symbol: str, scope: FeedScope) -> bool:
    """True iff a lake dataset ``(exchange, segment, symbol)`` is in *scope*.

    The §3.5 dataset predicate — TOTAL + never-raising (module docstring):

    * unbounded scope → ``True`` (pass-through — the legacy walk).
    * ``INDEX`` / ``CASH`` → the raw dir key is folded through the SAME
      read-time recovery ladder completeness/#911 serves drifted keys with
      (exact → whitespace-strip → own-exchange-prefix strip,
      :func:`~brutex.data.lake_keys.spot_key_candidates`), and the RECOVERED
      key is what ``admits_underlying`` judges (scope AFTER recovery — the
      Rev-2 ordering law).
    * ``FNO`` → :func:`~brutex.data.fno_symbol.parse_fno_symbol`; decoded →
      ``admits_fno_underlying`` on the PARSED exchange token (the symbol
      embeds its own ``NSE-``/``BSE-`` prefix); undecodable (``-G1S``) →
      ``False`` (counted by the caller — honest).
    * unknown exchange/segment token → ``False`` under a bounded scope.
    """
    if not scope.bounded():
        return True
    if segment == "FNO":
        parts = parse_fno_symbol(symbol)
        if parts is None:
            return False
        exch = _coerce_exchange(parts.exchange)
        if exch is None:
            return False
        return scope.admits_fno_underlying(exch, parts.underlying)
    if segment in ("INDEX", "CASH"):
        exch = _coerce_exchange(exchange)
        if exch is None:
            return False
        return any(
            scope.admits_underlying(exch, form) for form in _spot_recovered_forms(symbol, exchange)
        )
    # Unknown segment coordinate — excluded under a bounded scope (counted by
    # the caller), never a raise.
    return False


def classify_window_row(
    security_id: str,
    scope: FeedScope,
    *,
    admitted_sids: Mapping[str, str] | None = None,
) -> str:
    """Classify one window-ledger row's ``security_id`` under *scope* (§3.5 window form).

    The SHARED window-row predicate the R11 census fold and the P9
    skipped-window audit bind. Three key shapes, judged in order:

    * **legacy option shape** (``"<sid>|<segment>"`` — the folded key
      ``_decode_option_window`` live-handles): the sid is looked up in
      *admitted_sids* (``{sid: symbol_name}`` — the admitted-index sid map);
      a mapped sid whose symbol is admitted → :data:`WINDOW_ADMITTED`; an
      UNMAPPED sid → :data:`WINDOW_LEGACY_UNRESOLVED` (skipped-not-reopened +
      counted DISTINCTLY, never a silent drop).
    * **modern F&O contract** (``parse_fno_symbol`` decodes) →
      ``admits_fno_underlying`` on the parsed ``(exchange, underlying)``.
    * **spot** (everything else — the bare engine ``symbol_name``, NO
      exchange coordinate in the ledger key): admitted iff any recovered form
      (exact / whitespace-strip / known-exchange-prefix strip) is an admitted
      symbol NAME.

    Unbounded *scope* → always :data:`WINDOW_ADMITTED`.
    """
    if not scope.bounded():
        return WINDOW_ADMITTED
    if "|" in security_id:
        sid = security_id.partition("|")[0]
        mapped = (admitted_sids or {}).get(sid)
        if mapped is None:
            return WINDOW_LEGACY_UNRESOLVED
        return WINDOW_ADMITTED if mapped in _admitted_symbols(scope) else WINDOW_EXCLUDED
    parts = parse_fno_symbol(security_id)
    if parts is not None:
        exch = _coerce_exchange(parts.exchange)
        if exch is not None and scope.admits_fno_underlying(exch, parts.underlying):
            return WINDOW_ADMITTED
        return WINDOW_EXCLUDED
    admitted_names = _admitted_symbols(scope)
    stripped = security_id.strip()
    forms = {security_id, stripped}
    for exchange in Exchange:
        prefix = f"{exchange.value}-"
        if stripped.startswith(prefix):
            candidate = stripped.removeprefix(prefix)
            if candidate:
                forms.add(candidate)
    return WINDOW_ADMITTED if any(form in admitted_names for form in forms) else WINDOW_EXCLUDED


def window_scope_admit(
    scope: FeedScope,
    *,
    admitted_sids: Mapping[str, str] | None = None,
) -> Callable[[WindowKeyTuple], bool] | None:
    """Build the P9 skipped-audit ``admit`` callable for *scope* — or ``None``.

    ``None`` under an UNBOUNDED scope (the legacy unfiltered audit — zero
    overhead, byte-identical); under a bounded scope a pure, fast predicate
    over the 5-tuple window key (safe under the ledger lock): admitted rows
    rotate, everything else — excluded AND legacy-unresolved — is
    skipped-not-reopened (no budget consumed, never demoted; counted into the
    ``pull.skipped_audit`` log line by the caller's counting wrapper).
    """
    if not scope.bounded():
        return None

    def _admit(window_key: WindowKeyTuple) -> bool:
        return (
            classify_window_row(window_key[1], scope, admitted_sids=admitted_sids)
            == WINDOW_ADMITTED
        )

    return _admit


def scope_note_suffix(excluded: int) -> str:
    """Return the honest read-surface note fragment for scope exclusions.

    The ONE wording law of the R1 movers filter (design §3 R1 — CLAUDE.md §11
    COMMON, so the three tabs can never drift): ``""`` when *excluded* is zero
    (EMIT-ON-NONZERO — an index-only lake's note stays byte-identical), else a
    leading-space fragment naming the count + the DEC, e.g.
    ``" 3 stored-only symbols excluded by scope (DEC-UNIVERSE-INDEX-ONLY-001)."``.

    Parameters
    ----------
    excluded
        The count of stored-only dataset symbols the scope filter dropped at
        DISCOVERY (before any per-symbol read).

    Returns
    -------
    str
        The note suffix (empty for zero).
    """
    if excluded <= 0:
        return ""
    return f" {excluded:,} stored-only symbols excluded by scope (DEC-UNIVERSE-INDEX-ONLY-001)."


def scope_excluded_echo(excluded: int) -> dict[str, int]:
    """The EMIT-ON-NONZERO ``scope_excluded`` payload echo (the ``class_echo`` idiom).

    ``{}`` when *excluded* is zero — a defaulted / index-only-lake payload is
    BYTE-IDENTICAL to the pre-#1934 wire (design §3 R1's pinned law); else the
    one additive key ``{"scope_excluded": excluded}``.
    """
    if excluded <= 0:
        return {}
    return {"scope_excluded": excluded}


__all__ = [
    "WINDOW_ADMITTED",
    "WINDOW_EXCLUDED",
    "WINDOW_LEGACY_UNRESOLVED",
    "classify_window_row",
    "scope_admits_dataset",
    "scope_excluded_echo",
    "scope_note_suffix",
    "window_scope_admit",
]
