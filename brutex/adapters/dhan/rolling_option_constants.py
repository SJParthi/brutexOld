"""Rolling-ATM option adapter constants — the pure, honest leaf.

DEC-DHAN-ROLLING-OPTIONS-001 (umbrella DEC-DHAN-ROLLING-OPTIONS-001, ADR-0022 §
"Rolling-ATM expired-options"). The Dhan **rolling-ATM** options track: Dhan serves
NO per-expired-contract candles — ONLY an ATM-relative ROLLING series via
``POST /v2/charts/rollingoption`` (SDK ``expired_options_data``). The resolved expiry
DATE and the absolute STRIKE **change per bar-date** under a fixed selector, so they
are per-ROW metadata, NEVER partition keys (see :mod:`.rolling_option_writer`).

Every value here is traceable to the DhanHQ-v2 research (the official ``dhanhq``
SDK read verbatim + official-doc snippets) OR carries the ``_UNVERIFIED`` honesty
suffix (the Groww / Dhan-spot ``*_UNVERIFIED`` precedent, CLAUDE.md §6 / Golden
Rule #1). An ``_UNVERIFIED`` value is NEVER read by a decision branch — an AST pin
proves it — so the pull can never gate on a fabricated "verified" flag. All
``*.dhan.co`` hosts are egress-blocked in-sandbox, so servability ships UNVERIFIED
until a credentialed ``brutex doctor --live --dhan-rolling-probe`` transcript lands.

Scope: the three ENGINE index underlyings (NIFTY / BANKNIFTY / SENSEX) — the SAME
brute-force surface as spot; the rolling-ATM option lake is REFERENCE_ONLY (never in
the 52→60-condition vocabulary). NO F&O stocks, NO India-VIX options.
"""

from __future__ import annotations

from datetime import date
from typing import Final

# ─────────────────────────────────────────────────────────────────────────────
# Endpoint + request shape (SDK ``expired_options_data`` → POST /v2/charts/rollingoption)
# ─────────────────────────────────────────────────────────────────────────────

#: The DhanHQ v2 rolling-option REST path — recorded for provenance (the vendor
#: ``dhanhq`` SDK owns the transport; not used to build requests here).
DHAN_ROLLING_ENDPOINT: Final[str] = "POST /v2/charts/rollingoption"

#: The SDK method the adapter calls (``expired_options_data``) — provenance only.
DHAN_ROLLING_SDK_METHOD: Final[str] = "expired_options_data"

#: The instrument-type token for the rolling-ATM INDEX-option series (the ONLY
#: in-source verbatim value). SDK-verified for index options.
DHAN_ROLLING_INSTRUMENT: Final[str] = "INDEX"

#: The ALTERNATE instrument token some venues use for index options (``OPTIDX``).
#: UNVERIFIED — behind a credentialed disambiguation probe (§7 gate). NEVER read by
#: a decision branch (AST-pinned in the test module): the adapter always requests
#: :data:`DHAN_ROLLING_INSTRUMENT` and LOUD-skips on a reject — never a silent switch.
DHAN_ROLLING_INSTRUMENT_OPTIDX_UNVERIFIED: Final[str] = "OPTIDX"

#: The SDK interval code for 1-minute rolling-option candles (SDK ``interval=1``).
#: The rolling-ATM track is ALWAYS 1-minute — the 19 derived intraday TFs + the
#: daily anchor are DERIVED from the stored 1-minute by the prebuild stage, never
#: separately fetched (CLAUDE.md §3).
DHAN_ROLLING_INTERVAL: Final[int] = 1

#: The single canonical interval this rolling-ATM track fetches (1-minute only).
DHAN_ROLLING_FETCH_INTERVAL: Final[str] = "1minute"

#: The intended transport read timeout for a rolling-option call (seconds). The
#: ``dhanhq`` SDK constructs its OWN http client / timeout, so whether a per-call
#: override is honoured is UNVERIFIED (host-blocked); recorded as the intended
#: bound + surfaced in the adapter docstring, NOT passed as a fabricated SDK kwarg
#: (Golden Rule #1 — never a silently-ignored knob presented as verified).
DHAN_ROLLING_TIMEOUT_S: Final[float] = 60.0

#: The ``requiredData`` payload list — the exact per-bar columns requested (VERBATIM,
#: order-preserving). ``oi`` / ``iv`` / ``spot`` may be absent for a thin / far-OTM
#: key (§7 gate) — the parser tolerates an absent OPTIONAL column as a null column.
DHAN_ROLLING_REQUIRED_DATA: Final[tuple[str, ...]] = (
    "open",
    "high",
    "low",
    "close",
    "iv",
    "volume",
    "strike",
    "oi",
    "spot",
)

# ─────────────────────────────────────────────────────────────────────────────
# Request enums (SDK-verified selectors — each key IS the partition identity)
# ─────────────────────────────────────────────────────────────────────────────

#: ``expiryFlag`` selectors — weekly / monthly expiry tenor.
EXPIRY_FLAG_WEEK: Final[str] = "WEEK"
EXPIRY_FLAG_MONTH: Final[str] = "MONTH"

#: The valid ``expiryFlag`` set (fail-loud on anything else — Golden Rule #1).
DHAN_ROLLING_EXPIRY_FLAGS: Final[frozenset[str]] = frozenset({EXPIRY_FLAG_WEEK, EXPIRY_FLAG_MONTH})

#: ``drvOptionType`` selectors — CALL / PUT leg.
OPTION_TYPE_CALL: Final[str] = "CALL"
OPTION_TYPE_PUT: Final[str] = "PUT"

#: The valid ``drvOptionType`` set (fail-loud on anything else).
DHAN_ROLLING_OPTION_TYPES: Final[frozenset[str]] = frozenset({OPTION_TYPE_CALL, OPTION_TYPE_PUT})

#: ``expiryCode`` selectors — Near(0) / Next(1) / Far(2) / Farther(3). The valid
#: closed set (fail-loud outside it).
DHAN_ROLLING_EXPIRY_CODES: Final[tuple[int, ...]] = (0, 1, 2, 3)

#: The ATM-relative strike band half-width: strikes are requested from ATM-N to
#: ATM+N inclusive (``N=10`` → 21 offsets per key). Wing offsets beyond the served
#: band degrade to an honest empty (§7 gate).
DHAN_ROLLING_STRIKE_BAND: Final[int] = 10

# ─────────────────────────────────────────────────────────────────────────────
# Underlyings + wire addressing (the 3 ENGINE indices; reference-only lake)
# ─────────────────────────────────────────────────────────────────────────────

#: The three ENGINE index underlyings the rolling-ATM track pulls. The SAME
#: brute-force surface as spot; the option lake is REFERENCE_ONLY (CLAUDE.md §3).
DHAN_ROLLING_UNDERLYINGS: Final[tuple[str, ...]] = ("NIFTY", "BANKNIFTY", "SENSEX")

#: ``exchangeSegment`` per underlying — NSE F&O for NIFTY / BANKNIFTY, BSE F&O for
#: SENSEX. UNVERIFIED wire tokens (conventional; SENSEX-WEEK servability is an
#: honest per-key skip, §7 gate).
DHAN_ROLLING_EXCHANGE_SEGMENTS: Final[dict[str, str]] = {
    "NIFTY": "NSE_FNO",
    "BANKNIFTY": "NSE_FNO",
    "SENSEX": "BSE_FNO",
}

#: Partition-path exchange dir per underlying (NIFTY / BANKNIFTY → NSE, SENSEX →
#: BSE). Mirrors the spot ``dhan_index_exchange`` split.
DHAN_ROLLING_EXCHANGES: Final[dict[str, str]] = {
    "NIFTY": "NSE",
    "BANKNIFTY": "NSE",
    "SENSEX": "BSE",
}

# ─────────────────────────────────────────────────────────────────────────────
# Windowing + lake shape
# ─────────────────────────────────────────────────────────────────────────────

#: Per-request window cap for the rolling-option endpoint — 30 days per call
#: (expired-options cap, DISTINCT from the spot 90-day intraday cap; UNVERIFIED →
#: the CONSERVATIVE 30 over-chunks safely, §7 gate).
DHAN_ROLLING_WINDOW_DAYS: Final[int] = 30

#: The lake subtree the rolling-ATM option lake nests under (NEVER ``options`` /
#: ``fno_candles`` / ``bars`` — a distinct root so ``parse_fno_symbol`` and every
#: fixed-contract reader/heal path are STRUCTURALLY unable to touch it).
DHAN_ROLLING_SUBDIR: Final[str] = "rolling_options"

#: The rolling-ATM option row schema version (see :mod:`.rolling_option_writer`).
ROLLING_OPTIONS_SCHEMA_VERSION: Final[int] = 1

# ─────────────────────────────────────────────────────────────────────────────
# Ledger namespace (own namespace → no Groww / Dhan-spot collision)
# ─────────────────────────────────────────────────────────────────────────────

#: The per-key outcome/attribution namespace for Dhan rolling-ATM option pulls —
#: the ``dhan-option`` prefix the feed binding already reserves
#: (``ledger_market_option``). Own namespace so a rolling-option outcome can never
#: collide with a Groww window or a Dhan-spot / Dhan-future window. NOT a durable
#: ledger namespace this pass (the lean spine uses in-memory outcomes — mirrors the
#: spot ingest's honest framing; the durable ledger is deferred).
DHAN_ROLLING_COVERAGE_MARKET: Final[str] = "dhan-option"

# ─────────────────────────────────────────────────────────────────────────────
# History floor + servability — UNVERIFIED until a probe transcript lands
# ─────────────────────────────────────────────────────────────────────────────

#: The earliest 1-minute date the rolling endpoint is CONFIRMED to serve. ``None``
#: = UNVERIFIED (Golden Rule #1): June-2022 is the operator's REQUESTED reachable
#: start but the true floor + the rolling edge are UNVERIFIED (host-blocked). The
#: credentialed probe (§7 gate) flips this to a real ``date`` in a ONE-line data
#: change; until then the pull NEVER fabricates a floor.
DHAN_ROLLING_HISTORY_FLOOR: Final[date | None] = None

#: The operator's REQUESTED reachable-today start (research-supported PARTIAL). A
#: recorded marker ONLY — deliberately NOT the honest floor authority
#: (:data:`DHAN_ROLLING_HISTORY_FLOOR` is ``None`` and drives planning). Surfaced as
#: the adapter's ``earliest_history`` CAPABILITY (a display/metadata field), so it is
#: NOT one of the AST-pinned decision-branch sentinels; no PLANNING branch reads it —
#: planning gates on the ``None`` floor above.
DHAN_ROLLING_HISTORY_FLOOR_REQUESTED: Final[date] = date(2022, 6, 1)

#: Whole-track rolling servability (the DH-905 gate) — UNVERIFIED until a
#: credentialed probe transcript lands. An HONESTY MARKER ONLY: no decision branch
#: reads it (AST-pinned), so the pull can never gate on a fabricated "verified"
#: flag. ``None`` = "no probe transcript yet".
DHAN_ROLLING_SERVABILITY_UNVERIFIED: Final[None] = None


__all__ = [
    "DHAN_ROLLING_COVERAGE_MARKET",
    "DHAN_ROLLING_ENDPOINT",
    "DHAN_ROLLING_EXCHANGES",
    "DHAN_ROLLING_EXCHANGE_SEGMENTS",
    "DHAN_ROLLING_EXPIRY_CODES",
    "DHAN_ROLLING_EXPIRY_FLAGS",
    "DHAN_ROLLING_FETCH_INTERVAL",
    "DHAN_ROLLING_HISTORY_FLOOR",
    "DHAN_ROLLING_HISTORY_FLOOR_REQUESTED",
    "DHAN_ROLLING_INSTRUMENT",
    "DHAN_ROLLING_INSTRUMENT_OPTIDX_UNVERIFIED",
    "DHAN_ROLLING_INTERVAL",
    "DHAN_ROLLING_OPTION_TYPES",
    "DHAN_ROLLING_REQUIRED_DATA",
    "DHAN_ROLLING_SDK_METHOD",
    "DHAN_ROLLING_SERVABILITY_UNVERIFIED",
    "DHAN_ROLLING_STRIKE_BAND",
    "DHAN_ROLLING_SUBDIR",
    "DHAN_ROLLING_TIMEOUT_S",
    "DHAN_ROLLING_UNDERLYINGS",
    "DHAN_ROLLING_WINDOW_DAYS",
    "EXPIRY_FLAG_MONTH",
    "EXPIRY_FLAG_WEEK",
    "OPTION_TYPE_CALL",
    "OPTION_TYPE_PUT",
    "ROLLING_OPTIONS_SCHEMA_VERSION",
]
