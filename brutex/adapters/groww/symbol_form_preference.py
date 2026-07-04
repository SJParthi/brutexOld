"""Learned ordering of the ``&``-symbol FORM fallback (DEC-SYMBOL-FORM-PREFERENCE-001).

DEC-GROWW-AMPERSAND-FALLBACK-001 (#898) / DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001 gave
every ``&``-containing underlying (``M&M``, ``M&MFIN``, ``L&TFH``, …) a TWO-form fetch:
the plain symbol FIRST, then the ``%26``-encoded variant on a clean-empty reply. The
form ORDER was FIXED, so a symbol whose ``%26`` form is the repeat winner still paid
the full wasted plain-form fetch (all tiled windows × the empty-retry budget) on EVERY
subsequent call. This module adds the missing memory: a small, thread-safe,
PROCESS-LOCAL preference that remembers which form ROLE last returned non-empty data
per key and moves that role FIRST on the next call — while NEVER dropping, adding, or
substituting a form (the try-both safety law of #898 is untouched; the loser is still
tried on an empty first result).

Design (COMMON · injected · honest):

* **Role-indexed, not string-keyed.** The winner is remembered as its INDEX in the
  CANONICAL forms tuple (:func:`~brutex.adapters.groww.contracts.underlying_symbol_forms`
  — plain=0, encoded=1), so one underlying's learning transfers across its contracts:
  the encoded CANDLE symbol of ``NSE-M&M-…-CE`` differs per contract, but the "encoded
  form wins" fact is shared under the ``M&M`` key.
* **Injected, never a module global** (CLAUDE.md §4 "no global mutable state"): the F&O
  pull spine constructs ONE instance per pull run and threads it down; every other
  caller defaults to ``None`` and is byte-identical to the fixed order.
* **Never narrows the search.** ``order`` only PERMUTES the canonical tuple; an unknown
  key / stale index returns the input unchanged; all forms are still tried on empties.
  "If uncertain, fetch" is preserved verbatim.
* **O(1)** per lookup/record; a ``threading.Lock`` guards the dict (the F&O fan-out
  calls from parallel pool workers).

**Honest envelope (Golden Rule #1/#10):** the ``%26`` encoding itself remains a
vendor-UNVERIFIED hypothesis (the #898 wording stands — a live Groww probe is the only
closure); this module only reorders which hypothesis is tried first. The memory is
PROCESS-LOCAL and per-pull — cross-pull persistence (a sidecar / ``schema_meta`` row)
is a NAMED follow-up, deliberately not built here.
"""

from __future__ import annotations

import threading

__all__ = ["SymbolFormPreference"]


class SymbolFormPreference:
    """Thread-safe per-key memory of the last WINNING symbol-form role.

    ``order`` returns the given canonical forms with the remembered winner moved
    FIRST (stable order of the rest, NEVER dropping a form); ``record_winner`` stores
    the winning form's index in the canonical tuple. Unknown keys / out-of-range
    indices are no-ops, so the preference can only ever REORDER, never narrow.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: key → the winning form's index in the CANONICAL forms tuple.
        self._winner_index: dict[str, int] = {}

    def order(self, key: str, forms: tuple[str, ...]) -> tuple[str, ...]:
        """Return *forms* with the remembered winner (if any) moved first — O(1).

        The input MUST be the CANONICAL tuple for *key*
        (:func:`~brutex.adapters.groww.contracts.underlying_symbol_forms` order) so the
        remembered index addresses the same ROLE. No memory, a canonical-first winner
        (index 0), or a stale out-of-range index returns *forms* unchanged — the
        default order is always a safe fallback, never an error.
        """
        with self._lock:
            idx = self._winner_index.get(key)
        if idx is None or idx <= 0 or idx >= len(forms):
            return forms
        return (forms[idx], *forms[:idx], *forms[idx + 1 :])

    def record_winner(self, key: str, forms: tuple[str, ...], winning_form: str) -> None:
        """Remember that *winning_form* (∈ the CANONICAL *forms*) returned data — O(forms).

        Called ONLY on a non-empty result. A *winning_form* not present in *forms*
        (defensive — a caller bug) is ignored rather than poisoning the memory.
        ``O(forms)`` is O(2) in practice (plain + encoded).
        """
        try:
            idx = forms.index(winning_form)
        except ValueError:
            return
        with self._lock:
            self._winner_index[key] = idx
