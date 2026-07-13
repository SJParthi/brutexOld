"""Lake coordinate → canonical cross-verify identity mapper (deny-by-default).

DEC-CROSSVERIFY-PRODUCER-001 §1.3. Pure: stdlib + the ONE F&O grammar
(:func:`brutex.data.fno_symbol.parse_fno_symbol` — never re-minted, the #1646
truncation class) + :data:`brutex.universe.V1_INDICES` for the index
allowlist cross-check. Every mapping is O(1); junk / out-of-contract
coordinates map to ``None`` (skip + count at the caller), never a raise.

Pinned laws:

* indices — exactly ``{("NSE","NIFTY"), ("NSE","BANKNIFTY"), ("BSE","SENSEX")}``
  (derived from ``V1_INDICES`` minus reference-only, so INDIAVIX can never
  silently join); segment token ``IDX_I`` is CROSS-exchange (the prior-vendor
  era's single Index segment, MIGRATION.md #57/#80);
* stocks — the lake ``CASH`` dir name verbatim (``BAJAJ-AUTO``, ``M&M``) —
  never split a groww_symbol on ``-``;
* futures — ``parse_fno_symbol`` ONLY; canonical token
  ``<underlying>-<YYYY-MM-DD>-FUT`` (date-BEFORE-FUT — deliberately distinct
  from the RETIRED ``NIFTY-FUT-YYYY-MM-DD`` series key); identity is the
  PARSED ``(underlying, expiry)``, never the token string;
* the S3 key embeds the RAW canonical symbol (``M&M.csv`` — no ``%26``
  minting, the ``lake_keys`` pledge; boto3 percent-encodes at transport only).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

from brutex.data.fno_symbol import FUTURE_TYPE, parse_fno_symbol
from brutex.universe import V1_INDICES

SEGMENT_IDX: Final[str] = "IDX_I"
SEGMENT_NSE_EQ: Final[str] = "NSE_EQ"
SEGMENT_BSE_EQ: Final[str] = "BSE_EQ"
SEGMENT_NSE_FNO: Final[str] = "NSE_FNO"
SEGMENT_BSE_FNO: Final[str] = "BSE_FNO"

CLASS_INDEX_SPOT: Final[str] = "index_spot"
CLASS_CASH_SPOT: Final[str] = "cash_spot"
CLASS_FUT: Final[str] = "fut"

#: The exported index coordinates — derived from ``V1_INDICES`` minus the
#: reference-only rows (India VIX), so the allowlist can never drift from the
#: universe's own index registry. Test-pinned to the literal 3-tuple set.
EXPORT_INDEX_COORDS: Final[frozenset[tuple[str, str]]] = frozenset(
    (spec.exchange.value, spec.underlying_symbol) for spec in V1_INDICES if not spec.reference_only
)

#: Futures underlyings in the export contract. SENSEX is STRUCTURAL only —
#: the v1 pull leg pulls NIFTY + BANKNIFTY (BSE derivatives are out of the
#: fetch universe; DEC-CROSSVERIFY-SENSEX-FUT-INGEST-001 is the follow-up).
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


def canonical_identity(
    *, exchange: str, segment: str, lake_symbol: str
) -> CanonicalIdentity | None:
    """Map one lake coordinate to its canonical export identity, or ``None``.

    ``None`` = excluded (skip + count at the caller): a non-allowlisted index
    (INDIAVIX / GIFTNIFTY), any CE/PE leg, an unparsed FNO dir, a
    non-allowlisted FUT underlying, an exchange/segment outside the contract,
    or a key-unsafe symbol. Case-exact; never raises. O(1).
    """
    if segment == "INDEX":
        if (exchange, lake_symbol) not in EXPORT_INDEX_COORDS:
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


def s3_key(*, prefix: str, day: date, identity: CanonicalIdentity) -> str:
    """The ONE S3 key law: ``<prefix>/<YYYY-MM-DD>/<segment_token>/<symbol>.csv``.

    RAW canonical symbol in the key (``M&M.csv`` — ``&`` is a legal S3 key
    char; boto3 percent-encodes at transport only, never in the stored key).
    Deterministic per ``(day, symbol)`` — no timestamps, no run ids.
    """
    return f"{prefix}/{day.isoformat()}/{identity.segment_token}/{identity.canonical_symbol}.csv"


__all__ = [
    "CLASS_CASH_SPOT",
    "CLASS_FUT",
    "CLASS_INDEX_SPOT",
    "EXPORT_FUTURES_UNDERLYINGS",
    "EXPORT_INDEX_COORDS",
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
