"""``/db`` discovery + column metadata for the Dhan **rolling-ATM** options lake.

DEC-DHAN-ROLLING-OPTIONS-001 (§5 /db surface) · DEC-DHAN-ROLLING-DB-VIEW-001
(the deferred interactive-cascade follow-up this module is the MINIMAL first
slice of).

**HONESTY LAW (binding — see the writer module header).** Dhan serves NO
per-expired-contract candles — ONLY an ATM-relative ROLLING series. The
resolved expiry DATE and the resolved absolute STRIKE drift per bar-date under
a FIXED selector, so they are per-ROW columns (``resolved_expiry_date`` /
``resolved_strike``), NEVER partition keys. Every label this module emits says
**"rolling-ATM" / "ATM±N"**, NEVER "per-contract" / "per-expiry" /
"per-strike". A fixed ``expiry_code`` partition legitimately holds MANY distinct
``resolved_expiry_date`` values across a real roll — that is the whole point.

This is the SELF-CONTAINED first slice of the ``/db`` surface: a dataset
DISCOVERY (:func:`list_dhan_rolling_option_datasets`) + the honest COLUMN
metadata (:func:`dhan_rolling_options_schema`). It touches NONE of the shared
``db_view.py`` (the Groww spot / F&O candle paths stay byte-identical) and adds
NO query surface. The full interactive selector cascade + the live candle query
route are the DEFERRED follow-up **DEC-DHAN-ROLLING-DB-VIEW-001** (a separate
change against the shared browser).

The physical partition scheme this walks (fixed selectors only)::

    <feed_root>/rolling_options/{exchange}/{underlying}/{expiry_flag}/EC{code}/
        {strike_offset_safe}/{option_type}/{interval}/{yyyy}/{mm}.parquet

where ``feed_root`` is the Dhan feed root (``<top>/feeds/dhan``) — the same root
:mod:`brutex.adapters.dhan.rolling_option_writer` writes under.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from brutex.adapters.dhan.rolling_option_constants import DHAN_ROLLING_SUBDIR
from brutex.data.fno_candle_lake import FnoCandleColumn

__all__ = [
    "DHAN_ROLLING_OPTIONS_COLUMNS",
    "dhan_rolling_options_schema",
    "list_dhan_rolling_option_datasets",
    "strike_offset_from_safe",
]

#: The honest rolling-ATM options logical-table schema — the coordinate-echo
#: selectors (promoted from the partition path), the per-bar OHLCV + served
#: reference fields, the per-ROW resolved metadata (the rolling-ATM crux), and
#: the nullable Greeks overlay the served-vs-solved enrich pass fills. Every
#: ``doc`` string is labelled rolling-ATM — NEVER "per-contract".
DHAN_ROLLING_OPTIONS_COLUMNS: Final[tuple[FnoCandleColumn, ...]] = (
    # ── coordinate echo (the FIXED selectors, promoted from the path) ──
    FnoCandleColumn(
        "exch_id", "string", "partition", False, "Exchange id (e.g. ``NSE`` / ``BSE``)."
    ),
    FnoCandleColumn(
        "exchange_segment", "string", "bar", False, "Vendor exchangeSegment (e.g. ``NSE_FNO``)."
    ),
    FnoCandleColumn("security_id", "int", "bar", False, "Underlying vendor SECURITY_ID (numeric)."),
    FnoCandleColumn(
        "underlying_symbol", "string", "partition", False, "Underlying engine symbol (``NIFTY``)."
    ),
    FnoCandleColumn(
        "expiry_flag",
        "string",
        "partition",
        False,
        "Rolling tenor selector (``WEEK`` / ``MONTH``).",
    ),
    FnoCandleColumn(
        "expiry_code", "int", "partition", False, "Rolling expiry selector (Near/Next/Far — FIXED)."
    ),
    FnoCandleColumn(
        "strike_offset",
        "string",
        "partition",
        False,
        "Rolling-ATM relative strike LABEL (``ATM`` / ``ATM±N``) — the FIXED selector, "
        "NOT an absolute contract strike.",
    ),
    FnoCandleColumn("option_type", "string", "partition", False, "``CALL`` / ``PUT``."),
    FnoCandleColumn("interval", "string", "partition", False, "Candle interval (``1minute``)."),
    # ── per-bar value fields ──
    FnoCandleColumn("timestamp", "timestamp", "bar", False, "Bar open time (UTC; displayed IST)."),
    FnoCandleColumn("open", "decimal", "bar", False, "Open premium (₹, 2-dp tick grid)."),
    FnoCandleColumn("high", "decimal", "bar", False, "High premium (₹, 2-dp tick grid)."),
    FnoCandleColumn("low", "decimal", "bar", False, "Low premium (₹, 2-dp tick grid)."),
    FnoCandleColumn("close", "decimal", "bar", False, "Close premium (₹, 2-dp tick grid)."),
    FnoCandleColumn("volume", "int", "bar", False, "Traded contracts in the bar."),
    FnoCandleColumn("open_interest", "int", "bar", False, "Open interest for the rolling leg."),
    FnoCandleColumn(
        "iv", "float", "bar", True, "Vendor-served implied volatility (annualized); NULL if absent."
    ),
    FnoCandleColumn(
        "spot", "decimal", "bar", True, "Underlying spot at the bar (₹, 2-dp); NULL if absent."
    ),
    # ── per-ROW resolved metadata (the rolling-ATM crux — drifts per bar) ──
    FnoCandleColumn(
        "resolved_strike",
        "decimal",
        "bar",
        True,
        "The ABSOLUTE strike the rolling-ATM selector resolved to for THIS bar (₹, 2-dp) — "
        "a per-ROW column, NOT a partition key; drifts as spot moves.",
    ),
    FnoCandleColumn(
        "resolved_expiry_date",
        "date",
        "bar",
        True,
        "The expiry the rolling selector resolved to for THIS bar — a per-ROW column, "
        "NOT a partition key; steps forward across a real roll.",
    ),
    FnoCandleColumn(
        "resolved_expiry_source",
        "string",
        "bar",
        True,
        "``echoed`` (vendor supplied the expiry) / ``derived`` (NSE-calendar) / ``unresolved`` "
        "(honestly unknown — never fabricated).",
    ),
    # ── nullable Greeks overlay (served-vs-solved; DEC-DHAN-ROLLING-OPTIONS-001 §3) ──
    FnoCandleColumn(
        "iv_used", "float", "bar", True, "IV used to price the Greeks (served or solved)."
    ),
    FnoCandleColumn("delta", "float", "bar", True, "∂price/∂spot (signed; CALL + / PUT −)."),
    FnoCandleColumn("gamma", "float", "bar", True, "∂delta/∂spot (>= 0)."),
    FnoCandleColumn("theta", "float", "bar", True, "∂price/∂time per Act/365 calendar day."),
    FnoCandleColumn("vega", "float", "bar", True, "∂price/∂σ per 1 vol-point (>= 0)."),
    FnoCandleColumn("rho", "float", "bar", True, "∂price/∂r per 1% rate (signed)."),
    FnoCandleColumn(
        "spot_at_bar", "decimal", "bar", True, "Underlying spot frozen into the Greeks (₹, 2-dp)."
    ),
    FnoCandleColumn(
        "t_years_used",
        "float",
        "bar",
        True,
        "Time-to-expiry (years) bound to THIS bar's ``resolved_expiry_date``.",
    ),
    FnoCandleColumn("rate_used", "float", "bar", True, "Risk-free r frozen into the Greeks."),
    FnoCandleColumn(
        "greeks_provenance_id",
        "int",
        "bar",
        True,
        "Packed provenance stamp (formula/inputs/status/iv-source) — NULL until enriched.",
    ),
    FnoCandleColumn(
        "iv_source",
        "string",
        "bar",
        True,
        "``served`` (priced directly from the vendor IV, no root-solve) / ``solved`` "
        "(root-solved from the premium exactly as the Groww path does).",
    ),
)


def dhan_rolling_options_schema() -> list[dict[str, Any]]:
    """Return the rolling-ATM options logical-table schema (documented columns).

    A JSON-friendly projection of :data:`DHAN_ROLLING_OPTIONS_COLUMNS` — the twin
    of :func:`brutex.dashboard.db_view.fno_candle_lake_schema`, served for API
    clients / the ``schema_meta`` browser row. Pure projection — no disk access.
    """
    return [
        {
            "name": c.name,
            "dtype": c.dtype,
            "source": c.source,
            "nullable": c.nullable,
            "doc": c.doc,
        }
        for c in DHAN_ROLLING_OPTIONS_COLUMNS
    ]


def strike_offset_from_safe(safe: str) -> str:
    """Reconstruct the honest ``ATM±N`` label from a path-safe partition segment.

    The inverse of
    :func:`brutex.adapters.dhan.rolling_option_writer.strike_offset_safe`
    (``+`` → ``p``, ``-`` → ``m``): ``ATM`` → ``ATM``, ``ATMp1`` → ``ATM+1``,
    ``ATMm10`` → ``ATM-10``. An unexpected / legacy segment (neither the bare
    ``ATM`` nor an ``ATMp…`` / ``ATMm…`` form) is returned VERBATIM — the honest
    passthrough, never a fabricated label (Golden Rule #1).
    """
    if safe == "ATM":
        return "ATM"
    if safe.startswith("ATMp"):
        return "ATM+" + safe[len("ATMp") :]
    if safe.startswith("ATMm"):
        return "ATM-" + safe[len("ATMm") :]
    return safe


def _parse_expiry_code(segment: str) -> int | None:
    """Parse ``EC<n>`` → ``n`` (``EC0`` → ``0``); a non-conforming name → ``None``.

    Honest surfacing (Golden Rule #1): a present-but-undecodable ``EC…`` dir is
    still listed (with ``expiry_code=None``), never dropped.
    """
    if segment.startswith("EC"):
        rest = segment[len("EC") :]
        if rest.isdigit():
            return int(rest)
    return None


def list_dhan_rolling_option_datasets(feed_root: Path) -> list[dict[str, Any]]:
    """Discover the Dhan rolling-ATM option datasets under a feed root.

    Walks ``<feed_root>/rolling_options/{exchange}/{underlying}/{expiry_flag}/
    EC{code}/{strike_offset_safe}/{option_type}/{interval}/`` and returns one
    entry per FIXED-selector set that holds at least one ``.parquet`` file. A
    missing ``rolling_options/`` directory yields ``[]``. Directory-only at each
    level (a stray file / partially-written tree never raises). Sorted by the
    on-disk selector path segments for a stable picker order.

    Each entry is shaped::

        {"kind": "dhan_rolling_options", "exchange", "underlying", "expiry_flag",
         "expiry_code": int|None, "strike_offset": "ATM±N", "strike_offset_safe",
         "option_type", "interval"}

    ``strike_offset`` is the honest ``ATM±N`` LABEL (reconstructed from the
    path-safe segment); ``expiry_code`` is the FIXED Near/Next/Far selector. The
    per-ROW ``resolved_expiry_date`` / ``resolved_strike`` are DELIBERATELY absent
    from this selector-level descriptor — they are bar COLUMNS, not partition
    keys (the rolling-ATM honesty law).

    Parameters
    ----------
    feed_root
        The Dhan feed root (``<top>/feeds/dhan``) — the parent of
        ``rolling_options/``.
    """
    root = feed_root / DHAN_ROLLING_SUBDIR
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for exch_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for under_dir in sorted(p for p in exch_dir.iterdir() if p.is_dir()):
            for flag_dir in sorted(p for p in under_dir.iterdir() if p.is_dir()):
                for ec_dir in sorted(p for p in flag_dir.iterdir() if p.is_dir()):
                    for off_dir in sorted(p for p in ec_dir.iterdir() if p.is_dir()):
                        for otype_dir in sorted(p for p in off_dir.iterdir() if p.is_dir()):
                            for iv_dir in sorted(p for p in otype_dir.iterdir() if p.is_dir()):
                                if not any(iv_dir.rglob("*.parquet")):
                                    continue
                                out.append(
                                    {
                                        "kind": "dhan_rolling_options",
                                        "exchange": exch_dir.name,
                                        "underlying": under_dir.name,
                                        "expiry_flag": flag_dir.name,
                                        "expiry_code": _parse_expiry_code(ec_dir.name),
                                        "strike_offset": strike_offset_from_safe(off_dir.name),
                                        "strike_offset_safe": off_dir.name,
                                        "option_type": otype_dir.name,
                                        "interval": iv_dir.name,
                                    }
                                )
    return out
