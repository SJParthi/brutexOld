"""Shared Groww candle fetch — tile a range, fetch each window, parse, concat.

DEC-GROWW-OPTION-INGEST-001. The single low-level tiling-fetch primitive BOTH
candle paths call (CLAUDE.md §11 COMMON — one source of truth, no copy-paste):

* the **spot** adapter (:meth:`~brutex.adapters.groww.adapter.GrowwMarketAdapter.fetch_ohlcv`)
  — index-RESOLVED: it looks the symbol up in the
  :class:`~brutex.adapters.groww.instruments.InstrumentIndex` first, then hands
  the resolved ``(segment, groww_symbol, drop_volume)`` here;
* the **option** path
  (:func:`~brutex.adapters.groww.option_ingest.fetch_option_candles`)
  — index-FREE: the ~89k FUT/CE/PE legs are DISCARDED from the lean universe
  (DEC-INSTRUMENT-UNIVERSE-FAST-001), so an expired contract is never in the
  index; the option path resolves contracts via ``get_expiries`` /
  ``get_contracts`` and fetches each one HERE by its full ``groww_symbol`` with
  ``segment="FNO"`` (:data:`~brutex.adapters.groww.constants.OPTION_SEGMENT`).

Pure orchestration over injected leaves (the :class:`~brutex.adapters.groww.client.GrowwClient`
+ the pure :func:`~brutex.adapters.groww.parser.parse_candle_rows`), so every
branch is unit-testable with a fake client — no ``growwapi``, no network. O(1)
per emitted window (the loop body is one client call + one parse); the TOTAL is
O(windows) which is network-bound, NOT an O(1)-total claim (Golden Rule #10).

**Ampersand symbol-form fallback (DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001):** an
``&`` in a Groww symbol (``NSE-M&M``, ``NSE-M&M-30Sep25-2000-CE``) is the HTTP
query-string separator; the ``growwapi`` SDK sends it UN-ENCODED, so the vendor
returns a clean-but-EMPTY ``200`` for symbols whose chain RESOLVED fine (the
resolution twin DEC-GROWW-AMPERSAND-FALLBACK-001 fixed ``get_expiries`` /
``get_contracts`` only — the CANDLE fetch still sent the raw ``&``). This module
now applies the SAME try-both-forms law at the candle chokepoint BOTH paths
share: the PLAIN symbol is fetched FIRST; ONLY when that whole tiled fetch
returns clean-EMPTY AND the symbol contains ``&`` is the ``%26``-encoded form
retried ONCE, first non-empty wins. A raised transient error propagates
UNCHANGED (it rides the existing retry ladders — the fallback keys ONLY off a
clean 200-empty), a TERMINAL error raised by the SPECULATIVE encoded attempt is
swallowed to the plain form's honest clean-empty (logged
``groww.candles.symbol_form_fallback_error`` — the fallback may never leave the
window WORSE than not trying it), and a no-``&`` symbol is byte-identical to
before. The exact SDK encoding remains vendor-UNVERIFIED (see
:func:`~brutex.adapters.groww.contracts.underlying_symbol_forms`) — try-both is
no-worse-than-before by construction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, time

import structlog

from brutex.adapters.groww.chunks import plan_candle_windows
from brutex.adapters.groww.client import GrowwClientProtocol
from brutex.adapters.groww.constants import INTERVAL_TO_GROWW
from brutex.adapters.groww.contracts import underlying_symbol_forms
from brutex.adapters.groww.errors import GrowwError, is_retryable
from brutex.adapters.groww.parser import parse_candle_rows
from brutex.adapters.groww.symbol_form_preference import SymbolFormPreference
from brutex.data.models import CandleBar

#: Structured logger — the `%26` symbol-form fallback attempt + outcome
#: (``groww.candles.symbol_form_fallback[_result|_error]``), fired only for an
#: ``&``-symbol whose plain-form fetch came back clean-empty (rare).
_LOG = structlog.get_logger("brutex.adapters.groww.candles")

#: Raw-payload capture hook (DEC-RAW-DATA-INTEGRITY-001). Fired ONCE per fetched
#: window with the RAW vendor payload (the row-of-arrays mapping, BEFORE any
#: parse/transform) plus the exact ``(groww_interval, window_start, window_end,
#: issued_symbol)`` strings the call was issued with — the coordinates a verify
#: re-fetch needs to re-issue the byte-identical call. ``issued_symbol`` is the
#: symbol form THIS physical call was sent with
#: (DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001): on a ``%26`` fallback recovery it
#: differs from the caller's key symbol, and recording it keeps the verify path
#: byte-identical (a latest-wins record captured under the plain key but fetched
#: with the encoded form must be re-verified with the ENCODED form). The hash +
#: sidecar write happen INSIDE the caller's hook (best-effort, guarded), so the
#: integrity layer is purely additive: ``None`` (the default — CLI / tests /
#: non-instrumented paths) makes this a no-op and the fetch byte-identical to the
#: pre-feature behaviour.
RawPayloadHook = Callable[[Mapping[str, object], str, str, str, str], None]


def fetch_candle_windows(
    client: GrowwClientProtocol,
    *,
    exchange: str,
    segment: str,
    groww_symbol: str,
    interval: str,
    start: date,
    end: date,
    drop_volume: bool,
    on_raw_payload: RawPayloadHook | None = None,
    form_preference: SymbolFormPreference | None = None,
    preference_key: str | None = None,
) -> list[CandleBar]:
    """Fetch + parse every Groww candle window for ``[start, end)`` at *interval*.

    Tiles the range into Groww-window-sized chunks for *interval*'s tier
    (:func:`~brutex.adapters.groww.chunks.plan_candle_windows` — 30/90/180-day),
    fetches each via :meth:`GrowwClient.historical_candles`, parses each payload
    with :func:`~brutex.adapters.groww.parser.parse_candle_rows`, and
    concatenates in window order.

    Parameters
    ----------
    client
        The injected :class:`~brutex.adapters.groww.client.GrowwClient` (the only
        network surface; its ``pace`` hook is the rate gate — DEC-DHAN-RATE-SINGLE-BUCKET-001).
    exchange
        The Groww exchange string (``"NSE"`` / ``"BSE"``).
    segment
        The Groww segment string (``"CASH"`` for spot equity, ``"INDEX"`` is mapped
        to the neutral spot row, ``"FNO"`` for derivatives — the caller decides).
    groww_symbol
        The full Groww symbol fed verbatim to the SDK (e.g. ``"NSE-NIFTY"`` for an
        index, ``"NSE-NIFTY-02Jan25-28500-PE"`` for an option contract).
    interval
        A BruteX canonical interval (must be a key of
        :data:`~brutex.adapters.groww.constants.INTERVAL_TO_GROWW`).
    start, end
        Half-open date range. ``start >= end`` yields no windows (empty list).
    drop_volume
        Passed through to :func:`parse_candle_rows`: ``True`` zeroes per-bar
        volume (an INDEX carries no traded volume — DEC-VENDOR-VOLUME-NORMALIZE-001);
        ``False`` keeps it (equities / FUT / CE / PE all trade — 03-backtesting.md:141-143).
    on_raw_payload
        Optional :data:`RawPayloadHook` fired ONCE per fetched window with the
        RAW vendor payload (BEFORE :func:`parse_candle_rows`) + the exact
        ``(groww_interval, window_start, window_end, issued_symbol)`` the call
        was issued with — the raw-data integrity capture seam
        (DEC-RAW-DATA-INTEGRITY-001; ``issued_symbol`` is the PHYSICAL symbol
        form of this call, which on a ``%26`` fallback differs from
        *groww_symbol* so the verify path can re-issue the byte-identical call).
        ``None`` (the default) makes the fetch byte-identical to the pre-feature
        path; the hash + sidecar persist live inside the caller's (best-effort)
        hook, so a hook failure is the CALLER's to absorb — the candle fetch
        itself is never affected by the capture.
    form_preference, preference_key
        Optional learned form ordering (DEC-SYMBOL-FORM-PREFERENCE-001): when a
        preference is injected, the ``&``-symbol form pair is REORDERED so the
        last-winning form (recorded under *preference_key*, default *groww_symbol*;
        the option spine passes the contract's UNDERLYING so one win transfers
        across the whole chain) is fetched FIRST — a pure reorder that never drops
        a form. ``None`` (the default) keeps today's fixed plain-first order
        byte-identical.

    Returns
    -------
    list[CandleBar]
        Parsed bars across all windows, in input order. No-trade bars are dropped
        by the parser, so the result may be shorter than the raw candle count.

    Raises
    ------
    ValueError
        If *interval* is not a supported Groww interval (fail-loud, Golden Rule #1).

    Notes
    -----
    **Ampersand fallback (DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001):** when
    *groww_symbol* contains ``&`` and the WHOLE tiled fetch of the plain form
    returns clean-EMPTY (a 200 with zero parsed bars — the un-encoded-``&``
    silent-miss shape of MIGRATION.md #870/#898), the fetch is retried ONCE with
    the ``%26``-encoded form (:func:`~brutex.adapters.groww.contracts.\
    underlying_symbol_forms` — the ONE shared source of the form order); the
    first non-empty result wins. The fallback fires ONLY on a clean empty: a
    raised transient (429 / timeout / 5xx / empty-body) propagates out of the
    plain fetch BEFORE the emptiness check, riding the existing retry ladders
    untouched — and a transient raised by the ENCODED attempt propagates the
    same way. A TERMINAL error from the encoded attempt, however, is swallowed
    to the plain form's clean-empty result (WARNING
    ``groww.candles.symbol_form_fallback_error``): the encoded fetch is
    SPECULATIVE, so its failure must never convert a pre-fallback honest-empty
    into a perpetual per-contract quarantine (the no-worse-than-before law). A
    no-``&`` symbol takes exactly the single-form path — byte-identical to the
    pre-fallback behaviour.

    **Accounting:** the fallback happens INSIDE this one logical fetch, so a
    caller-level fetch counter (the option spine's ``fetch_count`` /
    ``in_pull_fetches``, the empty-retry + confirmed-empty math) still counts
    ONE logical fetch; only the PHYSICAL SDK call count doubles for the rare
    all-empty ``&`` symbol — each physical call still rides ``client.pace`` +
    the call meter, so rate admission + metering see every real call.

    **Honest envelope (Golden Rule #1/#10):** the exact SDK encoding is
    vendor-UNVERIFIED (a live Groww probe is still owed); try-both is
    no-worse-than-before — if the vendor handles the plain ``&`` we never reach
    the fallback, and if BOTH forms are empty the same empty is recorded as
    today, never a fabricated bar.
    """
    if interval not in INTERVAL_TO_GROWW:
        raise ValueError(f"unsupported Groww interval: {interval!r}")
    groww_interval = INTERVAL_TO_GROWW[interval]
    windows = plan_candle_windows(
        datetime.combine(start, time()), datetime.combine(end, time()), interval=interval
    )

    def _fetch_form(symbol_form: str) -> list[CandleBar]:
        """Fetch + parse every planned window for ONE symbol form, in window order."""
        bars: list[CandleBar] = []
        for window in windows:
            payload = client.historical_candles(
                exchange=exchange,
                segment=segment,
                groww_symbol=symbol_form,
                interval=groww_interval,
                start=window.start,
                end=window.end,
            )
            # Raw-data integrity capture (DEC-RAW-DATA-INTEGRITY-001): hand the RAW
            # payload + the exact issued coordinates — INCLUDING the physical
            # ``symbol_form`` this call was sent with — to the hook BEFORE any parse,
            # so the recorded hash attests EXACTLY what the vendor sent AND the verify
            # path can re-issue the byte-identical call (a `%26` fallback recovery is
            # captured under the plain key but was FETCHED with the encoded form —
            # DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001). Purely additive — a no-op when
            # no hook is wired. Fires for the fallback form too (each physical window
            # fetch is captured with the coordinates it was issued at).
            if on_raw_payload is not None:
                on_raw_payload(payload, groww_interval, window.start, window.end, symbol_form)
            bars.extend(parse_candle_rows(payload, drop_volume=drop_volume))
        return bars

    # DEC-GROWW-AMPERSAND-CANDLE-FALLBACK-001: plain form FIRST (byte-identical for
    # the common no-`&` symbol); the fallback form ONLY on a clean all-empty result.
    # DEC-SYMBOL-FORM-PREFERENCE-001: an injected preference may move a learned winner
    # first (a pure reorder — the loser is still the speculative second attempt, and
    # the no-worse-than-before terminal-swallow law applies to whichever form is that
    # SPECULATIVE second; the FIRST form's errors propagate untouched, as today).
    canonical_forms = underlying_symbol_forms(groww_symbol)
    pref_key = groww_symbol if preference_key is None else preference_key
    forms = (
        canonical_forms
        if form_preference is None
        else form_preference.order(pref_key, canonical_forms)
    )
    first_bars = _fetch_form(forms[0])
    if first_bars or len(forms) == 1 or not windows:
        # Non-empty (the first form works — never fall back), a no-`&` symbol (single
        # form), or an empty window plan (nothing was fetched — no signal to act on).
        if first_bars and form_preference is not None:
            form_preference.record_winner(pref_key, canonical_forms, forms[0])
        return first_bars
    fallback = forms[1]
    _LOG.debug(
        "groww.candles.symbol_form_fallback",
        groww_symbol=groww_symbol,
        fallback_symbol=fallback,
        interval=interval,
        start=start.isoformat(),
        end=end.isoformat(),
    )
    try:
        bars = _fetch_form(fallback)
    except GrowwError as exc:
        if is_retryable(exc):
            # A transient (429/timeout/5xx/empty-body) from the SPECULATIVE second
            # attempt rides the EXISTING retry ladders exactly like the first form's
            # — propagate unchanged (already-reviewed behaviour, never swallowed).
            raise
        # A TERMINAL error from the speculative second attempt must be NO WORSE than
        # never having tried it (the module's no-worse-than-before law): before this
        # fallback existed the window would have recorded the first form's honest
        # clean-empty, so swallow the speculative failure into that SAME empty —
        # logged loudly — instead of quarantining the contract into a perpetual
        # FAILED window over a fetch we volunteered.
        _LOG.warning(
            "groww.candles.symbol_form_fallback_error",
            groww_symbol=groww_symbol,
            fallback_symbol=fallback,
            interval=interval,
            error_type=type(exc).__name__,
        )
        return first_bars
    if bars and form_preference is not None:
        form_preference.record_winner(pref_key, canonical_forms, fallback)
    # Which form won: INFO when the fallback form RECOVERED data (the signal the
    # operator cares about); DEBUG for a genuinely-empty window (both forms empty —
    # the same honest empty as before, just double-checked).
    log = _LOG.info if bars else _LOG.debug
    log(
        "groww.candles.symbol_form_fallback_result",
        groww_symbol=groww_symbol,
        fallback_symbol=fallback,
        interval=interval,
        form_won=("encoded" if fallback != canonical_forms[0] else "plain") if bars else "none",
        bars=len(bars),
    )
    return bars


__all__ = ["RawPayloadHook", "fetch_candle_windows"]
