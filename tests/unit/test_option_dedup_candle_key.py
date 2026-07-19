"""Unit tests for :func:`brutex.data.option_dedup.candle_dedup_key` — the L3 key.

DEC-AUDIT-LAKE-REPLAY-ORACLE-001 §3: the L3 replay-oracle producer maps every
lake candle bar to ONE canonical blake3 dedup key over its partition
coordinates + epoch, whose ``.hex()`` is the ``action="add"`` audit subject.
These tests pin the key's determinism, its distinctness across every one of
the five identity coordinates, the concatenation-collision safety of the
unit-separator join, the loud separator guard, and the audit-subject shape
(64 lowercase hex — structurally ``"|"``-free, so it can never trip the
chain's :class:`~brutex.orchestrator.audit.AuditFieldError`).
"""

from __future__ import annotations

import pytest
from brutex.data.candle_fields import CandleParseError
from brutex.data.option_dedup import candle_dedup_key, option_bar_key

#: The canonical fixture coordinates (a real Track-1 spot partition).
_BASE: dict[str, str | int] = {
    "exchange": "NSE",
    "segment": "INDEX",
    "symbol": "NIFTY",
    "interval": "1minute",
    "epoch": 1609731900,
}


def _key(**overrides: str | int) -> bytes:
    kwargs = {**_BASE, **overrides}
    return candle_dedup_key(
        exchange=str(kwargs["exchange"]),
        segment=str(kwargs["segment"]),
        symbol=str(kwargs["symbol"]),
        interval=str(kwargs["interval"]),
        epoch=int(kwargs["epoch"]),
    )


class TestCandleDedupKey:
    """Determinism + distinctness + guard of the L3 producer key."""

    def test_deterministic_raw_32_byte_blake3(self) -> None:
        """Same coordinates → the same raw 32-byte digest, call after call."""
        first = _key()
        second = _key()
        assert first == second
        assert isinstance(first, bytes)
        assert len(first) == 32

    def test_distinct_across_every_coordinate(self) -> None:
        """Changing ANY single one of the five coordinates re-keys the bar."""
        base = _key()
        variants = [
            _key(exchange="BSE"),
            _key(segment="CASH"),
            _key(symbol="BANKNIFTY"),
            _key(interval="1day"),
            _key(epoch=1609731960),
        ]
        assert all(variant != base for variant in variants)
        # And the variants are pairwise distinct too (no accidental collapse).
        assert len({base, *variants}) == 6

    def test_adjacent_field_concatenation_cannot_collide(self) -> None:
        """The unit-separator join keeps shifted field boundaries distinct.

        Without a separator, ``("NSEI", "NDEX")`` and ``("NSE", "INDEX")``
        would concatenate identically — a silent false-dedup that DROPS a
        real bar (Golden Rule #1). The ``\\x1f`` join makes them distinct.
        """
        assert _key(exchange="NSE", segment="INDEX") != _key(exchange="NSEI", segment="NDEX")
        assert _key(symbol="NIFTY", interval="1minute") != _key(symbol="NIFTY1", interval="minute")
        # Epoch is stringified LAST — a symbol/interval shift into the epoch
        # digits must also re-key.
        assert _key(interval="1minute", epoch=1609731900) != _key(
            interval="1minute1", epoch=609731900
        )

    @pytest.mark.parametrize("field", ["exchange", "segment", "symbol", "interval"], ids=str)
    def test_separator_guard_raises_loud(self, field: str) -> None:
        """A field VALUE carrying the reserved ``\\x1f`` separator is refused.

        The collision safety of the join rests on the separator never
        appearing in a value; a separator-bearing value raises the loud,
        quarantinable :class:`CandleParseError` naming the key family.
        """
        with pytest.raises(CandleParseError, match="candle-dedup key field"):
            _key(**{field: "BAD\x1fVALUE"})

    def test_hex_subject_is_audit_safe(self) -> None:
        """``.hex()`` is the audit ``add`` subject: 64 lowercase hex, no ``"|"``."""
        subject = _key().hex()
        assert len(subject) == 64
        assert subject == subject.lower()
        assert "|" not in subject
        assert all(c in "0123456789abcdef" for c in subject)

    def test_shared_core_hashes_identical_payloads_alike(self) -> None:
        """One COMMON ``_blake3_key`` core: identical 5-field payloads hash
        identically across the two key families (an HONEST pin — there is no
        per-family domain separation in the hash, only in the guard label).
        The families stay apart in practice because a candle's first field is
        an exchange (NSE/BSE), never an underlying symbol; if domain
        separation is ever added, this pin forces a conscious re-derivation."""
        candle = candle_dedup_key(
            exchange="NIFTY",
            segment="2021-01-28",
            symbol="14000",
            interval="CE",
            epoch=1609731900,
        )
        option = option_bar_key(
            underlying="NIFTY",
            expiry="2021-01-28",
            strike="14000",
            side="CE",
            epoch=1609731900,
        )
        # Same joined payload → same digest (one COMMON core, no domain tag).
        assert candle == option

    def test_option_bar_key_guard_message_preserved(self) -> None:
        """The shared ``_blake3_key`` core kept ``option_bar_key``'s original
        error message byte-for-byte (the refactor regression pin)."""
        with pytest.raises(CandleParseError, match="option-bar key field"):
            option_bar_key(
                underlying="NIFTY\x1f",
                expiry="2021-01-28",
                strike="14000",
                side="CE",
                epoch=1609731900,
            )
