"""Lake coordinate → canonical cross-verify identity mapper (deny-by-default).

DEC-CROSSVERIFY-PRODUCER-001 §1.3, amended SPOT-ONLY + WHOLE-SPOT-UNIVERSE by
DEC-CROSSVERIFY-SPOT-ONLY-001 (MIGRATION.md #1788). Pure: stdlib + the ONE
F&O grammar (:func:`brutex.data.fno_symbol.parse_fno_symbol` — never
re-minted, the #1646 truncation class). Every mapping is O(1); junk /
out-of-contract coordinates map to ``None`` (skip + count at the caller),
never a raise.

Pinned laws:

* indices — the WHOLE admitted spot-index universe: every key-safe NSE
  index (NIFTY / BANKNIFTY / INDIAVIX / the sectorals …), from BSE only
  SENSEX — the exact mirror of the universe IDX fetch rule
  (:func:`brutex.universe.is_universe_underlying`; mirror-pinned), so the
  export enumerates precisely what the spot pull covers (#1788 supersedes
  the 3-coord engine-trio allowlist ``EXPORT_INDEX_COORDS``); segment
  token ``IDX_I`` is CROSS-exchange (the prior-vendor era's single Index
  segment, MIGRATION.md #57/#80);
* stocks — the lake ``CASH`` dir name verbatim (``BAJAJ-AUTO``, ``M&M``) —
  never split a groww_symbol on ``-``;
* futures — STRUCTURAL ONLY since #1788 (the producer neither pulls nor
  enumerates futures; the mapping is retained, direct-unit-covered, for the
  contract's possible re-expansion): ``parse_fno_symbol`` ONLY; canonical
  token ``<underlying>-<YYYY-MM-DD>-FUT`` (date-BEFORE-FUT — deliberately
  distinct from the RETIRED ``NIFTY-FUT-YYYY-MM-DD`` series key); identity
  is the PARSED ``(underlying, expiry)``, never the token string;
* the S3 key embeds the RAW canonical symbol (``M&M.csv`` — no ``%26``
  minting, the ``lake_keys`` pledge; boto3 percent-encodes at transport only).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

from brutex.crossverify import rust_seam
from brutex.data.fno_symbol import FUTURE_TYPE, parse_fno_symbol

SEGMENT_IDX: Final[str] = "IDX_I"
SEGMENT_NSE_EQ: Final[str] = "NSE_EQ"
SEGMENT_BSE_EQ: Final[str] = "BSE_EQ"
SEGMENT_NSE_FNO: Final[str] = "NSE_FNO"
SEGMENT_BSE_FNO: Final[str] = "BSE_FNO"

CLASS_INDEX_SPOT: Final[str] = "index_spot"
CLASS_CASH_SPOT: Final[str] = "cash_spot"
CLASS_FUT: Final[str] = "fut"

#: The BSE indices in the export contract — from BSE only SENSEX, the exact
#: mirror of the universe IDX fetch rule (``is_universe_underlying`` /
#: ``is_in_fetch_scope``: every NSE index; from BSE only SENSEX) so the
#: export can never widen past what the spot pull covers. Mirror-pinned in
#: ``tests/unit/test_crossverify_canonical.py`` against the universe rule.
_BSE_INDEX_EXPORT_SYMBOLS: Final[frozenset[str]] = frozenset({"SENSEX"})

#: Futures underlyings the STRUCTURAL FUT mapping accepts. The producer is
#: SPOT-ONLY since DEC-CROSSVERIFY-SPOT-ONLY-001 (#1788) — this arm is
#: unreachable from the producer's enumeration and retained (direct-unit-
#: covered) only for the contract's possible re-expansion; the SENSEX-FUT
#: ingest follow-up DEC-CROSSVERIFY-SENSEX-FUT-INGEST-001 is CLOSED MOOT.
EXPORT_FUTURES_UNDERLYINGS: Final[frozenset[str]] = frozenset({"NIFTY", "BANKNIFTY", "SENSEX"})

#: The POSITIVE symbol charset (round-1 hostile review — a deny-list let
#: control chars like ESC/TAB/NUL ride into S3 keys, staged paths and log
#: lines): uppercase letters, digits, and the three legal-NSE-name specials
#: ``&`` (M&M) · ``-`` (BAJAJ-AUTO) · ``.`` (legacy series dots). Everything
#: else — lowercase, whitespace, ``/``, CSV metacharacters, control chars —
#: is refused; the renderer re-refuses metachars at render time (the
#: byte-contract tripwire — this is the key-side belt).
_SYMBOL_ALLOWED_CHARS: Final[frozenset[str]] = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789&-.")

_EQ_TOKEN_BY_EXCHANGE: Final[dict[str, str]] = {"NSE": SEGMENT_NSE_EQ, "BSE": SEGMENT_BSE_EQ}
_FNO_TOKEN_BY_EXCHANGE: Final[dict[str, str]] = {"NSE": SEGMENT_NSE_FNO, "BSE": SEGMENT_BSE_FNO}


@dataclass(frozen=True, slots=True)
class CanonicalIdentity:
    """One exportable symbol's lake coordinate + canonical contract identity."""

    exchange: str  # lake exchange dir: "NSE" | "BSE"
    lake_segment: str  # "INDEX" | "CASH" | "FNO"
    lake_symbol: str  # lake dir name verbatim
    canonical_symbol: str  # "NIFTY" | "RELIANCE" | "NIFTY-2026-07-30-FUT"
    segment_token: str  # IDX_I | NSE_EQ | BSE_EQ | NSE_FNO | BSE_FNO
    instrument_class: str  # "index_spot" | "cash_spot" | "fut"


def symbol_key_safe(symbol: str) -> bool:
    """``True`` iff *symbol* is allowlist-safe for S3 keys / staged paths / cells.

    Positive allowlist, deny-by-default (:data:`_SYMBOL_ALLOWED_CHARS`):
    every legal NSE form passes (``M&M``, ``BAJAJ-AUTO``, ``L&TFH``,
    ``3MINDIA``, ``NAM-INDIA``); anything else — empty, lowercase junk,
    whitespace/padding, ``/``, CSV metacharacters, control chars (ESC/TAB/
    NUL) — is refused. The pure-dot path tokens ``.`` / ``..`` are refused
    explicitly (a dot is otherwise a legal char). Shared with the journal's
    staged-key validation (:func:`brutex.crossverify.journal.staging_path_for_key`).
    O(len); never raises.
    """
    if not symbol or symbol in (".", ".."):
        return False
    return all(ch in _SYMBOL_ALLOWED_CHARS for ch in symbol)


def _canonical_identity_python(
    *, exchange: str, segment: str, lake_symbol: str
) -> CanonicalIdentity | None:
    """The verbatim Python mapping — the LIVE parity oracle (flag-OFF path).

    The original ``canonical_identity`` body, extracted unchanged
    (DEC-CROSSVERIFY-RUST-CORE, the Slice-0 pattern).
    """
    if segment == "INDEX":
        # The WHOLE spot-index universe (#1788): every key-safe NSE index;
        # from BSE only SENSEX — mirroring is_universe_underlying's IDX rule.
        if not symbol_key_safe(lake_symbol):
            return None
        if exchange != "NSE" and not (
            exchange == "BSE" and lake_symbol in _BSE_INDEX_EXPORT_SYMBOLS
        ):
            return None
        return CanonicalIdentity(
            exchange=exchange,
            lake_segment="INDEX",
            lake_symbol=lake_symbol,
            canonical_symbol=lake_symbol,
            segment_token=SEGMENT_IDX,
            instrument_class=CLASS_INDEX_SPOT,
        )
    if segment == "CASH":
        token = _EQ_TOKEN_BY_EXCHANGE.get(exchange)
        if token is None or not symbol_key_safe(lake_symbol):
            return None
        return CanonicalIdentity(
            exchange=exchange,
            lake_segment="CASH",
            lake_symbol=lake_symbol,
            canonical_symbol=lake_symbol,
            segment_token=token,
            instrument_class=CLASS_CASH_SPOT,
        )
    if segment == "FNO":
        parts = parse_fno_symbol(lake_symbol)
        if parts is None or parts.instrument_type != FUTURE_TYPE:
            return None  # options / junk dirs are out of the contract
        if parts.underlying not in EXPORT_FUTURES_UNDERLYINGS:
            return None
        if parts.exchange != exchange:
            return None  # a dir filed under the wrong exchange is junk
        fno_token = _FNO_TOKEN_BY_EXCHANGE.get(exchange)
        if fno_token is None:
            return None
        return CanonicalIdentity(
            exchange=exchange,
            lake_segment="FNO",
            lake_symbol=lake_symbol,
            canonical_symbol=f"{parts.underlying}-{parts.expiry.isoformat()}-FUT",
            segment_token=fno_token,
            instrument_class=CLASS_FUT,
        )
    return None


def _identity_fields(
    identity: CanonicalIdentity | None,
) -> tuple[str, str, str, str, str, str] | None:
    """Project an identity onto its six-field tuple (the seam compare shape)."""
    if identity is None:
        return None
    return (
        identity.exchange,
        identity.lake_segment,
        identity.lake_symbol,
        identity.canonical_symbol,
        identity.segment_token,
        identity.instrument_class,
    )


def canonical_identity(
    *, exchange: str, segment: str, lake_symbol: str
) -> CanonicalIdentity | None:
    """Map one lake coordinate to its canonical export identity, or ``None``.

    ``None`` = excluded (skip + count at the caller): a BSE index other than
    SENSEX (the universe IDX fetch rule's mirror), any CE/PE leg, an unparsed
    FNO dir, a non-allowlisted FUT underlying (the STRUCTURAL arm), an
    exchange/segment outside the contract, or a key-unsafe symbol.
    Case-exact; never raises. O(1) — O(len(symbol)) for the charset check.

    DEC-CROSSVERIFY-RUST-CORE: behind the default-OFF
    ``BRUTEX_CROSSVERIFY_RUST`` flag the pure mapping law is routed through
    :mod:`brutex.crossverify.rust_seam` — the F&O grammar stays PYTHON
    (:func:`parse_fno_symbol` is never re-implemented; its parsed fields
    cross the FFI as plain strings), deny-by-default in BOTH arms; flag OFF
    runs the verbatim Python body in place, byte-identical.
    """
    if not (rust_seam.crossverify_rust_enabled() and not rust_seam.rust_seam_disabled()):
        return _canonical_identity_python(
            exchange=exchange, segment=segment, lake_symbol=lake_symbol
        )
    parts = parse_fno_symbol(lake_symbol) if segment == "FNO" else None
    fields = rust_seam.routed_canonical_identity(
        exchange=exchange,
        segment=segment,
        lake_symbol=lake_symbol,
        fno_underlying=None if parts is None else parts.underlying,
        fno_expiry_iso=None if parts is None else parts.expiry.isoformat(),
        fno_is_future=parts is not None and parts.instrument_type == FUTURE_TYPE,
        fno_exchange=None if parts is None else parts.exchange,
        python_oracle=lambda: _identity_fields(
            _canonical_identity_python(exchange=exchange, segment=segment, lake_symbol=lake_symbol)
        ),
    )
    if fields is None:
        return None
    return CanonicalIdentity(
        exchange=fields[0],
        lake_segment=fields[1],
        lake_symbol=fields[2],
        canonical_symbol=fields[3],
        segment_token=fields[4],
        instrument_class=fields[5],
    )


def _s3_key_python(*, prefix: str, day: date, identity: CanonicalIdentity) -> str:
    """The verbatim Python key law — the LIVE parity oracle (flag-OFF path)."""
    return f"{prefix}/{day.isoformat()}/{identity.segment_token}/{identity.canonical_symbol}.csv"


def s3_key(*, prefix: str, day: date, identity: CanonicalIdentity) -> str:
    """The ONE S3 key law: ``<prefix>/<YYYY-MM-DD>/<segment_token>/<symbol>.csv``.

    RAW canonical symbol in the key (``M&M.csv`` — ``&`` is a legal S3 key
    char; boto3 percent-encodes at transport only, never in the stored key).
    Deterministic per ``(day, symbol)`` — no timestamps, no run ids.

    DEC-CROSSVERIFY-RUST-CORE: flag-ON routes the assembly through
    :mod:`brutex.crossverify.rust_seam` with the DAY pre-formatted by the
    caller-side ``day.isoformat()`` (never re-derived crate-side — a
    ``datetime`` subclass day renders identically in both arms by
    construction); flag OFF runs the verbatim body in place.
    """
    if not (rust_seam.crossverify_rust_enabled() and not rust_seam.rust_seam_disabled()):
        return _s3_key_python(prefix=prefix, day=day, identity=identity)
    return rust_seam.routed_s3_key(
        prefix=prefix,
        day_iso=day.isoformat(),
        segment_token=identity.segment_token,
        canonical_symbol=identity.canonical_symbol,
        python_oracle=lambda: _s3_key_python(prefix=prefix, day=day, identity=identity),
    )


__all__ = [
    "CLASS_CASH_SPOT",
    "CLASS_FUT",
    "CLASS_INDEX_SPOT",
    "EXPORT_FUTURES_UNDERLYINGS",
    "SEGMENT_BSE_EQ",
    "SEGMENT_BSE_FNO",
    "SEGMENT_IDX",
    "SEGMENT_NSE_EQ",
    "SEGMENT_NSE_FNO",
    "CanonicalIdentity",
    "canonical_identity",
    "s3_key",
    "symbol_key_safe",
]
