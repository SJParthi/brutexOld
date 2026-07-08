"""O(1) uniqueness + dedup for expired-option bars — the lake-write guard.

The operator decree (2026-05-31; MIGRATION.md #342) demands **O(1) uniqueness
deduplication** on every write path. This module is that guard for the
expired-options surface: it turns one :class:`~brutex.data.models.OptionBar`
(plus its series coordinates) into a canonical content key and answers
"have I already written this bar?" in **O(1)** — so a duplicate re-pull
(idempotence, CLAUDE.md §2 Rule 7) never double-writes.

Design (the 2 fast tiers of the §12 4-tier dedup; LMDB spill is the next
tier in the concrete sink):

1. **blake3 content key** — :func:`option_bar_key` hashes
   ``underlying ‖ expiry ‖ strike ‖ side ‖ epoch`` via the
   ``fno_math.blake3_hash`` Rust kernel (the same primitive
   ``strategies.combo`` uses; DEC-P0-044 blake3). O(1) per bar.
2. **Bloom filter** (:class:`~brutex.orchestrator.bloom.BloomFilter`) — O(1)
   probabilistic membership. A Bloom **negative** means *definitely new* (no
   exact lookup needed); a Bloom **positive** falls through to…
3. **exact membership** — an injected ``set``-like backing confirms the
   positive, so a Bloom false-positive can **never** cause a false dedup
   (no data loss). Defaults to an in-memory ``set``; an LMDB-backed set
   drops in for the persistent tier.

Worst cases covered: Bloom false-positive (exact tier disambiguates),
duplicate re-pull (dropped, idempotent), Bloom capacity exhaustion
(``BloomFilter`` fails closed and the error propagates — operator bumps
capacity), ``None`` strike/spot (stringified into the key deterministically).

Honest envelope (Golden Rule #10): every *operation* is O(1) time. The
total dedup state is O(n) — inherent to exact dedup — bounded in RAM by the
fixed-width Bloom plus the injected backing (LMDB spill keeps it off-heap in
the concrete sink). "O(1) space" is **per-insert amortized**, not total.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Protocol

import fno_math

from brutex.data.candle_fields import CandleParseError
from brutex.data.models import OptionBar
from brutex.orchestrator.bloom import DEFAULT_CAPACITY, DEFAULT_FPR, BloomFilter

#: ASCII unit separator joining key fields (collision-safe — never in values).
_FIELD_SEP: str = "\x1f"


class ExactMembership(Protocol):
    """The ``set``-like surface the exact-confirm tier needs (inject LMDB later).

    Params are positional-only (``/``) so a plain ``set[bytes]`` — whose
    ``add``/``__contains__`` take positional args — satisfies it structurally.
    """

    def __contains__(self, key: bytes, /) -> bool:
        """True iff *key* was previously added."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def add(self, key: bytes, /) -> None:
        """Record *key* as seen."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class BloomMembership(Protocol):
    """The probabilistic-membership surface the index needs from a Bloom filter."""

    def add(self, content_hash: bytes) -> None:
        """Insert *content_hash* into the filter."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def might_contain(self, content_hash: bytes) -> bool:
        """True if *content_hash* may be present (false positives, no false negatives)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def option_bar_key(
    *,
    underlying: str,
    expiry: str,
    strike: str,
    side: str,
    epoch: int,
) -> bytes:
    """Return the canonical blake3 content key for one option bar (O(1)).

    The 5-tuple ``(underlying, expiry, strike, side, epoch)`` is the unique
    identity of an expired-option 1-minute bar in the lake; two bars sharing
    it are duplicates. Fields are joined with the ASCII unit separator (never
    present in a value) so distinct tuples cannot collide on concatenation.

    Raises
    ------
    CandleParseError
        If any field VALUE itself contains the unit separator. The collision-
        safety of the concatenation rests on the separator never appearing in a
        value (the docstring's "never present in a value" claim); without this
        guard a separator-bearing value would let two DISTINCT tuples hash to the
        SAME key — a silent false-dedup that DROPS a real bar (Golden Rule #1 /
        §2 Rule 7 idempotence: dedup must never lose data). A vendor symbol /
        expiry / strike is plain ASCII, so this never fires in practice; it is a
        cheap guard turning an impossible-but-catastrophic collision into a loud,
        quarantinable :class:`~brutex.data.candle_fields.CandleParseError` (a
        :class:`~brutex.models.BruteXError`).

        This guard is DELIBERATELY NOT migrated onto the run-identity helper
        :func:`brutex.strategies.combo.reject_unit_separator`
        (DEC-OPTION-DEDUP-BLOOM-VERIFY-PIN-001): the digests here are
        content-addressed keys persisted by the LMDB exact tier, and the
        pinned error contract is this quarantinable ``CandleParseError``
        ("reserved unit separator" message — the per-window quarantine path),
        not the run-identity guard's per-caller error + message format.
        Reject-only semantics are identical; only the contracts differ.

    Returns
    -------
    bytes
        The raw 32-byte blake3 digest (not hex — the Bloom + exact tiers
        consume raw bytes; call ``.hex()`` at a display/audit boundary).
    """
    fields = [underlying, expiry, strike, side, str(epoch)]
    if any(_FIELD_SEP in field for field in fields):
        raise CandleParseError(
            f"option-bar key field contains the reserved unit separator: {fields!r}"
        )
    payload = _FIELD_SEP.join(fields)
    return fno_math.blake3_hash(payload.encode("utf-8"))


def key_for_option_bar(bar: OptionBar, *, underlying: str, expiry: str) -> bytes:
    """Derive :func:`option_bar_key` from an :class:`OptionBar` + its series coords.

    ``strike``/``side`` come off the bar; ``epoch`` is the bar's UTC second.
    A ``None`` strike stringifies deterministically (``"None"``) so a side
    that omits the resolved strike still dedups stably.
    """
    return option_bar_key(
        underlying=underlying,
        expiry=expiry,
        strike=str(bar.strike),
        side=bar.side,
        epoch=int(bar.timestamp.timestamp()),
    )


class OptionDedupIndex:
    """O(1) Bloom + exact-confirm dedup index for option-bar content keys.

    Parameters
    ----------
    capacity, fpr_target
        Bloom sizing — used only when *bloom* is not injected.
    bloom
        An optional pre-built :class:`BloomFilter` (inject for sharing /
        sizing); when ``None`` one is built from *capacity* / *fpr_target*.
    exact
        The exact-confirm backing — defaults to an in-memory ``set``; inject
        an LMDB-backed set for the persistent tier.

    Thread-safety (DEC-DHAN-PARALLEL-INGEST-001). One index is shared by the
    parallel option-ingest fan-out (each worker dedups its underlying's bars
    against the same Bloom + exact backing), so the **check-then-act** in
    :meth:`add_if_new` MUST be atomic — otherwise two workers could both observe
    a key as unseen and both write it, defeating the idempotence guard (Rule
    #7). A :class:`threading.Lock` makes ``seen`` / ``add`` / ``add_if_new`` each
    one atomic O(1) critical section, so a given content key is admitted exactly
    once across all workers. No I/O is done under the lock (the in-memory Bloom +
    ``set`` tiers; an injected LMDB exact backing has its own internal locking).

    Probe ordering (VERIFIED + PINNED — DEC-OPTION-DEDUP-BLOOM-VERIFY-PIN-001).
    The membership probe is **Bloom FIRST, exact-confirm second**: a Bloom
    NEGATIVE answers "definitely new" from the fixed-width bit array without
    ever touching the exact backing — the only ordering that keeps the fresh-bar
    common case free of an exact-tier read (an LMDB exact backing would
    otherwise pay one storage lookup per new bar; §2 Rule 11 SCALABLE) — while a
    Bloom POSITIVE (real or false) is ALWAYS confirmed against the exact tier,
    so a false positive can never phantom-drop a genuinely-new bar (Golden Rule
    #1 / §2 Rule 7). Regression-pinned: a spy exact tier proves the negative
    short-circuit; an always-true Bloom proves the no-phantom fall-through at
    every public probe entry (``add_if_new`` / :func:`unique_option_bars`).
    The production consumer of this verified ordering is the engine sweep's
    ``ComboDedup`` dedup tier — ``engine/sweep_launch.py::plan_launch_dedup``
    builds ``ComboDedup(OptionDedupIndex(...))`` on the legacy no-digest arm
    (the default digest path substitutes the no-alloc ``DigestPathNoDedup``
    and provably never consults dedup — R2-SL-F1); the prior-vendor-era
    ``OptionBarSink`` orchestration that also rode it was deleted as dead code
    by DEC-INGEST-AUDIT-WIRING-001 (MIGRATION.md #1500).
    """

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_CAPACITY,
        fpr_target: float = DEFAULT_FPR,
        bloom: BloomMembership | None = None,
        exact: ExactMembership | None = None,
    ) -> None:
        self._bloom: BloomMembership = (
            bloom if bloom is not None else BloomFilter(capacity=capacity, fpr_target=fpr_target)
        )
        self._exact: ExactMembership = exact if exact is not None else set()
        #: Serialises the check-then-act so a key is admitted exactly once under
        #: concurrent workers (DEC-DHAN-PARALLEL-INGEST-001). O(1) hold time.
        self._lock = threading.Lock()

    def _seen_locked(self, key: bytes) -> bool:
        """Membership probe, Bloom FIRST then exact-confirm — caller MUST hold ``self._lock``.

        The pinned bloom-before-exact ordering (see the class docstring):
        a negative short-circuits without an exact probe; a positive is
        always exact-confirmed.
        """
        if not self._bloom.might_contain(key):
            return False
        return key in self._exact

    def _add_locked(self, key: bytes) -> None:
        """Record *key* in both tiers — caller MUST hold ``self._lock``."""
        self._bloom.add(key)
        self._exact.add(key)

    def seen(self, key: bytes) -> bool:
        """True iff *key* was already added — O(1), atomic.

        Bloom-negative short-circuits to ``False`` (definitely new) without
        touching the exact tier; a Bloom-positive is confirmed against the
        exact backing so a false-positive never causes a false dedup.
        """
        with self._lock:
            return self._seen_locked(key)

    def add(self, key: bytes) -> None:
        """Record *key* in both tiers — O(1), atomic."""
        with self._lock:
            self._add_locked(key)

    def add_if_new(self, key: bytes) -> bool:
        """Add *key* iff unseen; return ``True`` if newly added, else ``False``. O(1), atomic.

        The membership probe and the insert are ONE critical section so two
        concurrent workers can never both see *key* as new and both add it
        (DEC-DHAN-PARALLEL-INGEST-001) — the key is admitted exactly once.
        """
        with self._lock:
            if self._seen_locked(key):
                return False
            self._add_locked(key)
            return True


def unique_option_bars(
    bars: Iterable[OptionBar],
    index: OptionDedupIndex,
    *,
    underlying: str,
    expiry: str,
) -> list[OptionBar]:
    """Return only the bars not already in *index*, marking them seen — O(1)/bar.

    The dedup guard the expired-options sink applies before a lake write: a
    duplicate re-pull collapses to nothing (idempotent), and the input order
    is preserved for the survivors.
    """
    return [
        bar
        for bar in bars
        if index.add_if_new(key_for_option_bar(bar, underlying=underlying, expiry=expiry))
    ]


__all__ = [
    "BloomMembership",
    "ExactMembership",
    "OptionDedupIndex",
    "key_for_option_bar",
    "option_bar_key",
    "unique_option_bars",
]
