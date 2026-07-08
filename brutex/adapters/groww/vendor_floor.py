"""Lazy in-pull vendor-depth-floor learning (DEC-VENDOR-FLOOR-LAZY-PROBE-001).

The Track-1 pull floor is the configured ``GROWW_HISTORY_FLOOR`` (2020-01-01 —
DEC-PULL-FLOOR-2020-001, MIGRATION.md #722; the vendor's documented depth is
"available from 2020", CLAUDE.md §6 "Historical depth"). For a LATE-LISTED /
shallow-history symbol the vendor genuinely has NO bars for the early part of
that range, so every pull re-spends fetches confirming the same pre-listing
emptiness window by window. This module learns each symbol's TRUE vendor floor
LAZILY, mid-pull, from the one honest trigger available:

* **Trigger** — the symbol's window STARTING AT the configured history floor
  settles CONFIRMED clean-empty (the spot spine's ``confirmed_empty_after_retry``
  seam: ≥ :data:`~brutex.adapters.ingest_common.CONFIRMED_EMPTY_MIN_FETCHES`
  clean 200-zero-candle fetches — never a raised transient). Only then is depth
  worth probing.
* **Probe** — ONE bounded bisection per symbol per pull (single-flight),
  REUSING the existing Phase-0 depth prober
  (:func:`~brutex.adapters.groww.probe.probe_depth`, ``bisect=True``,
  ``max_calls`` = :data:`~brutex.adapters.groww.probe.DEPTH_PROBE_MAX_CALLS`
  = 20) over the PACED replica of the pull's own adapter. PACING IS NOT
  AUTOMATIC: the live pool factory hands every client leaf a no-op pace
  (``build_live_pool`` without a ``pace``), and the session
  :class:`~brutex.adapters.rate_common.RateController` is bound only via
  :func:`~brutex.adapters.groww.run_ingest.with_paced_client` — so the
  production build sites construct the learner over
  ``with_paced_client(adapter, pace)`` (regression-pinned), making the
  client's per-attempt ``pace()`` the one admission point for every probe
  fetch (DEC-RATE-SINGLE-ADMISSION-001) — never a rogue unpaced stream.
* **Gate (the design hazard)** — BOTH the probe AND every cementing action are
  gated on the mass-empty circuit breaker
  (:meth:`~brutex.adapters.empty_circuit_breaker.EmptySettleBreaker.allow_settle`,
  DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): under a suspected vendor soft-throttle
  the boundary "emptiness" is untrustworthy, so NOTHING is probed and NOTHING is
  cemented. The gate is re-checked AFTER the probe too (a throttle can begin
  mid-probe).
* **Outcome (a clean probe)** — the learned floor is persisted to the
  instrument-master ``schema_meta`` sidecar (``groww.vendor_floor.<symbol>``,
  the DEC-INSTRUMENT-MASTER-INTEGRITY-001 key/value pattern), the symbol's
  wholly PRE-floor un-settled ledger windows are mass-settled ``SKIPPED`` with
  reason :data:`PRE_FLOOR_REASON` (REUSING
  :meth:`~brutex.data.window_ledger.WindowLedger.settle_known_gap` — reversible
  via the existing ``brutex reopen-skipped`` path, the same recovery every
  SKIPPED window has), and the NEXT pull's window planning for that symbol
  starts at ``max(GROWW_HISTORY_FLOOR, learned floor)``
  (:func:`floor_symbol_windows`, riding the EXISTING per-symbol
  ``symbol_windows`` seam of ``plan_windows`` + the spot ingest).

**Honest envelope (Golden Rule #10):** lazy + per-symbol + boundary-triggered
only; ≤ 20 probe WINDOW fetches per symbol per pull — a PROBER-window bound,
not a physical-vendor-call bound: each window fetch is one
``adapter.fetch_ohlcv``, and the client layer's own bounded transient
fast-retries (429/timeout/5xx — the same envelope every pull fetch has) may add
physical calls under a flaky vendor, every one individually admitted through
the paced client; a straddling window (one that spans
the learned floor) is NEVER settled; only a clean :data:`VERDICT_OK` probe
cements anything (ABSENT / ERROR / breaker-gated outcomes cement nothing); a
later vendor BACKFILL below the learned floor is NOT auto-detected — the
operator's ``brutex reopen-skipped`` is the recovery, exactly as for every other
SKIPPED window. The probe bisects on DAY offsets with 15-day windows (the
existing prober's granularity — finer than month boundaries at the same ≤ 20
call budget), and the learned floor is the probed first-bar DATE (an upper
bound if the vendor serves old ranges intermittently — the prober's documented
envelope).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Final

import structlog

from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker
from brutex.adapters.groww.probe import (
    VERDICT_OK,
    CandleFetcher,
    DepthProbeResult,
    probe_depth,
)
from brutex.adapters.ingest_common import GROWW_COVERAGE_MARKET
from brutex.adapters.protocol import MarketAdapter
from brutex.data.fetch_coverage import WindowKey
from brutex.data.meta_kv import MetaKV
from brutex.data.window_ledger import SKIPPED, SUCCESS, WindowLedger
from brutex.models import Instrument

_LOG = structlog.get_logger("brutex.adapters.groww.vendor_floor")

#: ``schema_meta`` key prefix for a learned per-symbol vendor floor. The full key
#: is ``groww.vendor_floor.<symbol_name>``; the value is the ISO ``YYYY-MM-DD``
#: learned floor date (the vendor's probed first-data date for that symbol).
VENDOR_FLOOR_META_PREFIX: Final[str] = "groww.vendor_floor."

#: The ledger ``last_error`` reason stamped on a mass-settled pre-floor window —
#: greppable + surfaced on /coverage/windows, and the marker the operator sees
#: before deciding to ``brutex reopen-skipped`` it.
PRE_FLOOR_REASON: Final[str] = "pre-floor"

#: One symbol's depth probe: ``instrument → DepthProbeResult``. Bound at the
#: production build site by :func:`make_adapter_depth_probe` (over the pull's own
#: paced adapter); tests inject a recorder/stub.
DepthProbe = Callable[[Instrument], DepthProbeResult]


#: The ONE persistence surface the learner needs — a back-compat alias of the
#: SHARED :class:`~brutex.data.meta_kv.MetaKV` Protocol (DEC-FORM-WINNER-PERSIST-001
#: consolidated the two structurally identical ``set_meta``/``get_meta`` Protocol
#: copies into one — CLAUDE.md §11 COMMON; Protocols are structural, so this is a
#: behavioral no-op). Structurally satisfied by
#: :class:`~brutex.data.instrument_store.InstrumentStore` (its generic
#: ``schema_meta`` key/value sidecar, the DEC-INSTRUMENT-MASTER-INTEGRITY-001
#: persist pattern).
VendorFloorMetaStore = MetaKV


def _utc_now() -> datetime:
    """The learner's default settle-stamp clock (UTC now) — injectable in tests."""
    return datetime.now(UTC)


def vendor_floor_key(symbol: str) -> str:
    """The ``schema_meta`` key holding *symbol*'s learned vendor floor — O(1)."""
    return f"{VENDOR_FLOOR_META_PREFIX}{symbol}"


def parse_vendor_floor(raw: str | None) -> date | None:
    """Parse a persisted ISO floor value; ``None``/malformed → ``None`` (never raises).

    Defensive: a hand-edited / corrupted ``schema_meta`` value must degrade to
    "no learned floor" (the full-range pull — never a crash, never a fabricated
    clamp; Golden Rule #1).
    """
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def load_vendor_floors(
    store: VendorFloorMetaStore | None, symbols: Iterable[str]
) -> dict[str, date]:
    """Read every learned floor for *symbols* from ``schema_meta`` — best-effort.

    Returns ``{symbol: learned_floor}`` for the symbols that HAVE a parseable
    persisted floor; a ``None`` store, an absent key, or a malformed value simply
    omits the symbol (full-range pull). Any store read error is swallowed +
    logged and yields ``{}`` — a broken sidecar can never break pull planning.
    O(symbols) reads, once at pull start.
    """
    if store is None:
        return {}
    floors: dict[str, date] = {}
    try:
        for symbol in symbols:
            learned = parse_vendor_floor(store.get_meta(vendor_floor_key(symbol)))
            if learned is not None:
                floors[symbol] = learned
    except Exception:  # pylint: disable=broad-exception-caught — planning must never crash on a sidecar read
        _LOG.exception("vendor_floor.load_failed")
        return {}
    return floors


def floor_symbol_windows(
    symbols: Iterable[str],
    *,
    start: date,
    end: date,
    floors: Mapping[str, date],
) -> dict[str, tuple[date, date]] | None:
    """Build the per-symbol ``symbol_windows`` planning clamp from learned floors — O(symbols).

    For each symbol whose learned floor is AFTER *start*, the planned window
    becomes ``(max(start, learned), end)`` — window planning for that symbol
    starts at ``max(GROWW_HISTORY_FLOOR, learned floor)``
    (DEC-VENDOR-FLOOR-LAZY-PROBE-001). Returns ``None`` when NO symbol is
    clamped, so the caller's ``symbol_windows=None`` path stays byte-identical
    (the never-narrow fallback of ``plan_windows`` /
    ``run_ingest._resolve_spot_window``). A learned floor at/before *start*
    clamps nothing; a learned floor at/after *end* still emits the (empty)
    ``(end, end)``-degenerate pair, which the shared tiler already resolves to
    zero windows — never a negative range.
    """
    clamped: dict[str, tuple[date, date]] = {}
    for symbol in symbols:
        learned = floors.get(symbol)
        if learned is None or learned <= start:
            continue
        clamped[symbol] = (min(max(start, learned), end), end)
    return clamped or None


@dataclass(frozen=True, slots=True)
class _AdapterCandleFetcher:
    """A :class:`~brutex.adapters.groww.probe.CandleFetcher` over ONE instrument's
    ``adapter.fetch_ohlcv``.

    The bridge that lets :func:`~brutex.adapters.groww.probe.probe_depth` be
    REUSED verbatim over the pull's own adapter: the adapter resolves the
    instrument MASTER-AUTHORITATIVELY (``by_exchange_symbol`` +
    ``groww_symbol_by_pair`` — DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001, CLAUDE.md §6
    "Symbol/key format"), so no resolution logic is copied and no separate client
    is conjured (CLAUDE.md §11 COMMON). Pacing is the CALLER's obligation: the
    adapter handed in must already be the paced replica
    (:func:`~brutex.adapters.groww.run_ingest.with_paced_client` — the production
    build sites do this; DEC-RATE-SINGLE-ADMISSION-001), because a raw live-pool
    adapter carries a no-op pace. The prober's
    ``exchange`` / ``segment`` / ``groww_symbol`` strings are accepted and
    DISCARDED — the pre-bound instrument is the single source of addressing
    truth. The returned payload carries one ``[timestamp]`` row per bar — the
    prober reads only the row COUNT + the first row's timestamp.
    """

    adapter: MarketAdapter
    instrument: Instrument

    def historical_candles(
        self,
        *,
        exchange: str,
        segment: str,
        groww_symbol: str,
        interval: str,
        start: str,
        end: str,
    ) -> Mapping[str, object]:
        """Fetch one probe window through the adapter; re-wrap as a candles payload."""
        del exchange, segment, groww_symbol  # the pre-bound instrument is authoritative
        bars = self.adapter.fetch_ohlcv(
            symbol=self.instrument.symbol_name,
            exchange=self.instrument.exch_id,
            interval=interval,
            start=date.fromisoformat(start[:10]),
            end=date.fromisoformat(end[:10]),
        )
        return {"candles": [[bar.timestamp.isoformat()] for bar in bars]}


def make_adapter_depth_probe(adapter: MarketAdapter, *, boundary: date, today: date) -> DepthProbe:
    """Bind :func:`~brutex.adapters.groww.probe.probe_depth` to the pull's adapter.

    Returns an ``instrument → DepthProbeResult`` callable running the BOUNDED
    bisection (``bisect=True``, ≤ 20 probe-window fetches; client-level bounded
    transient retries may add physical calls) between *boundary* (the configured
    history floor) and *today*, through :class:`_AdapterCandleFetcher` — the
    existing prober reused verbatim (CLAUDE.md §11 COMMON). *adapter* must be the
    pull's PACED adapter (``with_paced_client(adapter, pace)`` at the production
    build sites) — see :class:`_AdapterCandleFetcher` for why pacing is the
    caller's obligation.
    """

    def _probe(instrument: Instrument) -> DepthProbeResult:
        fetcher: CandleFetcher = _AdapterCandleFetcher(adapter=adapter, instrument=instrument)
        return probe_depth(
            fetcher,
            symbol=instrument.symbol_name,
            boundary=boundary,
            today=today,
            bisect=True,
        )

    return _probe


def settle_pre_floor_windows(
    ledger: WindowLedger,
    *,
    symbol: str,
    learned_floor: date,
    market: str = GROWW_COVERAGE_MARKET,
    now: datetime | None = None,
) -> int:
    """Mass-settle *symbol*'s wholly PRE-floor un-settled windows ``SKIPPED`` — returns the count.

    REUSES :meth:`~brutex.data.window_ledger.WindowLedger.settle_known_gap` (the
    existing SKIPPED settle machinery — reason :data:`PRE_FLOOR_REASON`), so every
    settled window stays REVERSIBLE via the existing ``brutex reopen-skipped``
    path. Settles ONLY a window whose END is STRICTLY BEFORE the learned floor
    (wholly pre-floor — no vendor data possible); a window STRADDLING the floor is
    left untouched (its post-floor tail has real data to fetch). Already-settled
    rows (``SUCCESS`` / ``SKIPPED``) and other symbols/markets are untouched. A
    malformed row date abstains (that row is skipped, never guessed). O(ledger
    rows) — once per learned floor, never on the per-window hot path.
    """
    stamp = now if now is not None else datetime.now(UTC)
    settled = 0
    for row in ledger.records():
        if row.market != market or row.security_id != symbol:
            continue
        if row.status in (SUCCESS, SKIPPED):
            continue
        try:
            row_start = date.fromisoformat(row.window_start)
            row_end = date.fromisoformat(row.window_end)
        except ValueError:
            continue  # a malformed row date abstains — never guessed at
        if row_end >= learned_floor:
            continue  # straddling or post-floor — real data possible, never settled
        ledger.settle_known_gap(
            WindowKey(
                market=row.market,
                security_id=row.security_id,
                interval=row.interval,
                start=row_start,
                end=row_end,
            ),
            now=stamp,
            reason=PRE_FLOOR_REASON,
        )
        settled += 1
    return settled


@dataclass
class VendorFloorLearner:
    """Per-pull, thread-safe lazy vendor-floor learner (DEC-VENDOR-FLOOR-LAZY-PROBE-001).

    ONE instance is threaded through a pull's spot window workers (the
    ``storm_breaker`` arming pattern — production builds one per REAL pull via
    :func:`make_vendor_floor_learner`; a dry-run / synthetic / unit-test pull
    threads ``None`` and nothing is ever probed). The spot spine calls
    :meth:`observe_boundary_empty` from the producer seam when a window settles
    confirmed clean-empty; everything else — the floor-boundary check, the
    breaker gate, the single-flight claim, the probe, the persist and the
    mass-settle — lives here.

    Parameters
    ----------
    probe
        The bound ``instrument → DepthProbeResult`` prober
        (:func:`make_adapter_depth_probe` in production; a stub in tests).
    history_floor
        The configured pull floor (``GROWW_HISTORY_FLOOR``) — the ONLY window
        start that triggers a probe.
    store
        Optional ``schema_meta`` persistence (:class:`VendorFloorMetaStore`);
        ``None`` learns for THIS pull only (mass-settle still runs; the planning
        clamp then relies on the settled rows' skip authority).
    market
        The ledger market namespace the mass-settle filters on (the spot
        Groww namespace by default).
    now
        Injected UTC-now for the settle stamps (deterministic tests).
    """

    probe: DepthProbe
    history_floor: date
    store: VendorFloorMetaStore | None = None
    market: str = GROWW_COVERAGE_MARKET
    now: Callable[[], datetime] = _utc_now
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    #: Symbols whose single probe THIS pull already ran (attempted — success or
    #: not). A breaker-GATED observation deliberately does NOT claim the slot, so
    #: a later clean observation this pull may still probe once.
    _probed: set[str] = field(default_factory=set, repr=False)

    def observe_boundary_empty(
        self,
        *,
        instrument: Instrument,
        window_start: date,
        ledger: WindowLedger | None,
        empty_breaker: EmptySettleBreaker | None,
    ) -> None:
        """Handle one CONFIRMED clean-empty window observation (producer seam).

        No-op unless *window_start* IS the configured history floor (the
        boundary window — the only honest depth-shallower-than-floor signal).
        Gated on the mass-empty breaker BEFORE the probe and AGAIN before any
        cementing (a soft-throttle makes emptiness untrustworthy — probe
        nothing, cement nothing); single-flight per symbol per pull. A clean
        :data:`VERDICT_OK` probe persists the learned floor to ``schema_meta``,
        mass-settles the wholly pre-floor un-settled windows ``SKIPPED``
        (reason :data:`PRE_FLOOR_REASON` — reversible via ``brutex
        reopen-skipped``), and logs one ``vendor_floor.learned``; every other
        outcome logs + cements NOTHING. Probe I/O runs OUTSIDE the learner lock.
        """
        if window_start != self.history_floor:
            return
        symbol = instrument.symbol_name
        if not self._settlement_trustworthy(empty_breaker):
            _LOG.warning(
                "vendor_floor.probe_suppressed",
                symbol=symbol,
                reason="empty_breaker_gated",
            )
            return
        with self._lock:
            if symbol in self._probed:
                return
            self._probed.add(symbol)  # single-flight: ONE probe per symbol per pull
        result = self.probe(instrument)
        if result.verdict != VERDICT_OK or result.probed_floor is None:
            _LOG.warning(
                "vendor_floor.probe_inconclusive",
                symbol=symbol,
                verdict=result.verdict,
                calls_used=result.calls_used,
                detail=result.detail,
            )
            return
        learned = result.probed_floor
        if learned <= self.history_floor:
            # The probe found data at/before the boundary after all (a transient
            # boundary empty) — there is no shallower floor to learn; cement nothing.
            _LOG.info(
                "vendor_floor.boundary_has_data",
                symbol=symbol,
                probed_floor=learned.isoformat(),
                calls_used=result.calls_used,
            )
            return
        # RE-CHECK the gate after the probe (the hazard fold): a soft-throttle that
        # began mid-probe poisons the probe's own empties — cement NOTHING.
        if not self._settlement_trustworthy(empty_breaker):
            _LOG.warning(
                "vendor_floor.cement_suppressed",
                symbol=symbol,
                probed_floor=learned.isoformat(),
                reason="empty_breaker_gated",
            )
            return
        self._persist(symbol, learned)
        settled = 0
        if ledger is not None:
            settled = settle_pre_floor_windows(
                ledger,
                symbol=symbol,
                learned_floor=learned,
                market=self.market,
                now=self.now(),
            )
        _LOG.warning(
            "vendor_floor.learned",
            symbol=symbol,
            learned_floor=learned.isoformat(),
            history_floor=self.history_floor.isoformat(),
            pre_floor_settled=settled,
            calls_used=result.calls_used,
            persisted=self.store is not None,
        )

    @staticmethod
    def _settlement_trustworthy(empty_breaker: EmptySettleBreaker | None) -> bool:
        """``True`` iff emptiness may be trusted — the breaker gate. ``None`` = no signal.

        A ``None`` breaker (unit-test / legacy callers with the learner armed)
        imposes no gate — the production spine always threads the per-pass
        breaker, so the live probe is always gated.
        """
        return empty_breaker is None or empty_breaker.allow_settle()

    def _persist(self, symbol: str, learned: date) -> None:
        """Persist the learned floor to ``schema_meta`` — best-effort, never raises."""
        if self.store is None:
            return
        try:
            self.store.set_meta(vendor_floor_key(symbol), learned.isoformat())
        except Exception:  # pylint: disable=broad-exception-caught — a sidecar write can never break a pull
            _LOG.exception("vendor_floor.persist_failed", symbol=symbol)


def make_vendor_floor_learner(
    adapter: MarketAdapter,
    *,
    history_floor: date,
    today: date,
    store: VendorFloorMetaStore | None = None,
) -> VendorFloorLearner:
    """Build the per-pull learner over the pull's own PACED adapter — the ONE production seam.

    Mirrors :func:`~brutex.adapters.error_storm_breaker.resolve_error_storm_breaker`:
    a FRESH instance per pull (single-flight state never leaks across pulls,
    Golden Rule #7), constructed ONLY at the REAL pull build sites — a dry-run /
    synthetic pull threads ``None`` down the spine instead. O(1). *adapter* MUST
    be the paced replica (``with_paced_client(adapter, pace)``) at a live build
    site: a raw live-pool adapter carries a no-op pace, so a learner built over
    it would probe OUTSIDE the 8 r/s ceiling + 500/min governor (the review
    BLOCKER; regression-pinned at both build sites + end-to-end by the
    admission-counting probe test).
    """
    return VendorFloorLearner(
        probe=make_adapter_depth_probe(adapter, boundary=history_floor, today=today),
        history_floor=history_floor,
        store=store,
    )


def observe_boundary_floor(
    learner: VendorFloorLearner | None,
    *,
    instrument: Instrument,
    window_start: date,
    ledger: WindowLedger | None,
    empty_breaker: EmptySettleBreaker | None,
) -> None:
    """The COMMON None-safe seam the spot spine calls — best-effort, never raises.

    ``None`` learner (a dry-run / synthetic / unit-test pull — the arming gate)
    observes nothing. Any internal learner/probe bug is swallowed + logged so
    depth learning can NEVER crash or halt a pull — it is an optimisation, not a
    correctness surface (the un-clamped pull is always correct, just slower).
    """
    if learner is None:
        return
    try:
        learner.observe_boundary_empty(
            instrument=instrument,
            window_start=window_start,
            ledger=ledger,
            empty_breaker=empty_breaker,
        )
    except Exception:  # pylint: disable=broad-exception-caught — a learner bug never crashes a pull
        _LOG.exception("vendor_floor.observe_failed", symbol=instrument.symbol_name)


__all__ = [
    "PRE_FLOOR_REASON",
    "VENDOR_FLOOR_META_PREFIX",
    "DepthProbe",
    "VendorFloorLearner",
    "VendorFloorMetaStore",
    "floor_symbol_windows",
    "load_vendor_floors",
    "make_adapter_depth_probe",
    "make_vendor_floor_learner",
    "observe_boundary_floor",
    "parse_vendor_floor",
    "settle_pre_floor_windows",
    "vendor_floor_key",
]
