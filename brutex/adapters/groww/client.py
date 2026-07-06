"""Groww client — the thin SDK wrapper (the only network surface).

DEC-DHAN-GROWW-REMIGRATE-001 (ADR-0009 §5, PR-F). Wraps an **injected**
``growwapi.GrowwAPI`` instance behind the three neutral calls the ingest layer
needs (candles / expiries / contracts), translating every SDK exception through
:func:`brutex.adapters.groww.errors.from_groww_exception` and owning a bounded
in-call fast-retry for transient stalls (mirrors the canonical client pattern).

The SDK is injected (the :class:`GrowwSDK` Protocol), so every branch — happy
path, transient-retry, exhaustion, terminal — is unit-tested with a fake; no
``growwapi`` install and no network.

**Adaptive rate limiting lives HERE, at the one choke point EVERY Groww call
funnels through (DEC-GROWW-RATELIMIT-RETRY-001)** — so spot / expiries / contracts
/ futures / options / historical are uniformly protected, never one bypassed: each
:meth:`_call` (a) ``pace()``-acquires a token (≤ the injected ceiling), (b) on a
RETRYABLE error (rate-limit OR timeout, classified by
:func:`~brutex.adapters.groww.errors.from_groww_exception` — which now recognises a
generic ``GrowwAPIException("Rate limit …")``, the live 429 shape) backs off +
retries within a bounded budget, (c) fires ``on_rate_limit`` to **decrement** the
shared token bucket on every 429 and ``on_success`` to **recover** it on a clean
call (the operator's decremental/incremental contract). The orchestrator's
work-item re-queue (layer 3) still re-pulls any window that exhausts this in-call
budget — belt-and-suspenders. O(1): the retry budget is a fixed constant; the
client holds no per-symbol state.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

import structlog

from brutex.adapters.call_meter import CallMeter
from brutex.adapters.groww.deadline import run_with_deadline
from brutex.adapters.groww.errors import (
    GrowwAuthError,
    GrowwRateLimitError,
    GrowwTokenStillStaleError,
    from_groww_exception,
    is_retryable,
)
from brutex.adapters.groww.sdk_protocol import GrowwSDK

if TYPE_CHECKING:
    from brutex.adapters.groww.reauth import SdkRefresher

#: Per-call observability (DEC-GROWW-CALL-LATENCY-LOG-001): every physical Groww REST
#: call logs its wall-clock latency at INFO (``groww.call``) so the operator can SEE
#: the real per-call response time live in ``/logs`` — instead of back-deriving it.
#: Covers ALL three calls (historical / expiries / contracts), so the historical
#: pull's true latency is finally measurable rather than assumed.
_LOG = structlog.get_logger("brutex.adapters.groww.client")

#: Small pause (seconds) before the post-re-auth retry (DEC-GROWW-AUTH-HALT-001 FP-1
#: hardening). At the 06:00 IST daily-token rotation the freshly minted / freshly
#: re-read token may not be fleet-valid the instant we hold it (a vendor-side
#: propagation lag — UNVERIFIED, treated as possible per Golden Rule #1: the log
#: evidence shows the rotation window is the auth-failure hot spot). An INSTANT
#: retry against a not-yet-valid token burns one bounded ``max_reauths`` slot on a
#: transient 401 — with the old budget of 1 that FALSE-exhausted the budget into a
#: terminal ``GrowwAuthError`` and (post-halt) killed the whole overnight pull.
#: 1.5 s is negligible against a once-daily event yet generous for token
#: propagation; it flows through the INJECTABLE ``self.sleep`` so tests pass a
#: recorder and no real time passes. Deliberately NOT the rate-limit ladder's
#: ``backoff_seconds`` — a re-auth is not a 429 and must not couple to that tuning.
REAUTH_RETRY_BACKOFF_SECONDS: float = 1.5


def _noop_pace() -> None:
    """Default rate-bucket hook — no-op until a controller is injected."""


def _noop_hook() -> None:
    """Default decrement/recover feedback hook — no-op until a controller is injected."""


def _noop_sleep(_seconds: float) -> None:
    """Default backoff sleeper — same ``(seconds)`` shape as :func:`time.sleep`.

    Tests inject a recorder instead so no real time passes; the default-binding
    regression test drives THIS body through the re-auth backoff (arity-pinned).
    """
    time.sleep(_seconds)


@dataclass(frozen=True, slots=True)
class GrowwClient:
    """Neutral wrapper over an injected :class:`GrowwSDK`.

    Attributes
    ----------
    sdk
        The injected SDK (real ``GrowwAPI`` in production, a fake in tests).
    pace
        Rate-bucket acquire hook fired before every physical call (defence in
        depth; the orchestrator owns the real limiter).
    sleep
        Backoff sleeper (injected in tests).
    max_attempts
        Bounded fast-retry budget for a transient timeout (O(1)).
    backoff_seconds
        Base linear backoff between retry attempts.
    """

    sdk: GrowwSDK
    pace: Callable[[], None] = _noop_pace
    #: Bound via ``field(default=...)`` (NOT a bare class-body default, mirroring
    #: :attr:`on_rate_limit` / :attr:`on_success`) so static analysis never models the
    #: plain function as an unbound method whose implicit ``self`` consumes the
    #: ``seconds`` slot — the CodeQL ``py/call/wrong-arity`` false positive on
    #: ``self.sleep(REAUTH_RETRY_BACKOFF_SECONDS)``. Runtime was always correct: a
    #: dataclass default lands as an INSTANCE attribute, a plain function never binds.
    sleep: Callable[[float], None] = field(default=_noop_sleep)
    on_rate_limit: Callable[[], None] = field(default=_noop_hook)
    on_success: Callable[[], None] = field(default=_noop_hook)
    max_attempts: int = 6
    backoff_seconds: float = 0.5
    rate_limit_backoff_seconds: float = 2.0
    #: Thread-safe single-flight SDK refresher (DEC-GROWW-TOKEN-REAUTH-001). When set,
    #: an AUTH-error call auto-RE-MINTS a fresh access token (the 06:00 IST daily-reset
    #: self-heal) and retries against the refreshed SDK; the live SDK is resolved from
    #: it per call (so a re-mint reaches every in-flight call). ``None`` (the default)
    #: keeps the exact pre-DEC behaviour — an auth error raises terminally — for
    #: offline / dry-run / unit tests. Excluded from eq/repr like :attr:`meter` (it is
    #: shared mutable state, not client identity; ``replace`` carries it through).
    refresher: SdkRefresher | None = field(default=None, compare=False, repr=False)
    #: This client's leaf index into the shared :attr:`refresher`'s SDK fleet
    #: (DEC-GROWW-TOKEN-REAUTH-001). ``0`` for a single client; a pool's leaf ``i`` binds
    #: index ``i`` so the K connections stay independent while ONE re-mint refreshes them
    #: all. Ignored when :attr:`refresher` is ``None``. Compared/repr'd like a normal
    #: field (it IS client identity — leaf 0 ≠ leaf 1).
    refresher_index: int = 0
    #: Bounded re-auth budget (DEC-GROWW-TOKEN-REAUTH-001) — a genuinely-bad credential
    #: re-mints AT MOST this many times then raises the :class:`GrowwAuthError`
    #: terminally (never an infinite loop). Counted SEPARATELY from :attr:`max_attempts`
    #: (the rate-limit/timeout budget) so an expiry mid-retry-storm still gets its bound.
    max_reauths: int = 1
    #: TOKEN-STALE fetch-lane pause bound (seconds, DEC-TOKEN-STALE-PAUSE-001). When a
    #: read-only-broker reauth re-read comes back STILL STALE (the 06:00-IST reset
    #: window before the external Lambda's mint lands), ``_call`` PAUSES on the
    #: refresher's :meth:`~brutex.adapters.groww.reauth.SdkRefresher.await_token_recovery`
    #: instead of propagating — so the outage never reaches the window ladder and no
    #: window attempts burn. ``None`` (the default, every build site) resolves the
    #: env-tunable :func:`~brutex.adapters.groww.reauth.resolve_token_stale_wait_seconds`
    #: (``BRUTEX_GROWW_TOKEN_STALE_WAIT_S``, default 420 s); an explicit ``0.0``
    #: disables the pause (pre-gate behaviour — the stale error propagates at once).
    token_stale_wait_seconds: float | None = None
    #: Hard per-call wall-clock deadline (seconds) — the historical-pull FREEZE GUARD
    #: (DEC-GROWW-PULL-CALL-DEADLINE-001). ``None`` (the default) keeps the bare
    #: behaviour for unit tests + dry-runs; the LIVE pull build sites inject
    #: ``GROWW_PULL_CALL_TIMEOUT_S`` so a stalled SDK call can never hang the pull.
    call_timeout_seconds: float | None = None
    #: Wall-clock source for the per-call latency log (DEC-GROWW-CALL-LATENCY-LOG-001);
    #: injected in tests for deterministic ``elapsed_ms`` assertions.
    clock: Callable[[], float] = time.perf_counter
    #: ACHIEVED-throughput meter (DEC-GROWW-RATE-ACHIEVED-METER-001) — records every
    #: physical call's start/stop so the dashboard + ``/logs`` show the REAL calls/s
    #: (not the controller's target). Defaults to a private per-client meter; the live
    #: pull injects ONE shared meter so a multi-connection pool reports an aggregate.
    #: Excluded from eq/repr — it is mutable observability state, not client identity
    #: (two clients differing only by meter instance stay equal; ``replace`` keeps it).
    meter: CallMeter = field(default_factory=CallMeter, compare=False, repr=False)

    @property
    def self_paces(self) -> bool:
        """Whether this client itself admits every physical call through a REAL ``pace`` hook.

        ``True`` once a shared rate bucket has been bound (a live build site's
        ``pace=`` argument or :meth:`with_pace`); ``False`` on the no-op default.
        The ingest spines consult this via :func:`client_self_paces` to pick ONE
        canonical admission point per physical fetch
        (DEC-RATE-SINGLE-ADMISSION-001): when the client self-paces, the
        per-window ``pace()`` is dropped, so a fetch is never admitted twice
        against the shared 500/min governor + AIMD ladder.
        """
        return self.pace is not _noop_pace

    @property
    def self_reports_rate_limits(self) -> bool:
        """Whether this client itself feeds every 429 occurrence to a REAL ``on_rate_limit``.

        ``True`` once the shared controller's decrement hook has been bound (a live
        build site's ``on_rate_limit=`` argument or :meth:`with_rate_hooks`);
        ``False`` on the no-op default. The ingest spines consult this via
        :func:`client_self_reports_rate_limits` to pick ONE canonical feedback
        point per vendor 429 (DEC-SUSPICION-SWEEP-FIXES-001): :meth:`_call` already
        fires :attr:`on_rate_limit` on EVERY 429 occurrence — the FINAL,
        budget-exhausting one included — so a spine that fired its own hook again
        on the propagated error would DOUBLE-COUNT that final occurrence into the
        AIMD ladder (7 hits per exhausted window instead of 6 — two step-downs
        from one window's storm).
        """
        return self.on_rate_limit is not _noop_hook

    @property
    def self_reports_successes(self) -> bool:
        """Whether this client itself feeds every clean call to a REAL ``on_success``.

        The recovery twin of :attr:`self_reports_rate_limits`: :meth:`_call` fires
        :attr:`on_success` on every clean physical call, so a spine firing its own
        per-window recovery hook on top would double-credit the ladder's step-up
        evidence. Consulted via :func:`client_self_reports_successes`
        (DEC-SUSPICION-SWEEP-FIXES-001).
        """
        return self.on_success is not _noop_hook

    def _resolve_sdk(self) -> GrowwSDK:
        """Return the SDK to call against — the refresher's live one, else :attr:`sdk`.

        When a :attr:`refresher` is wired (the live pull) the SDK is resolved PER CALL
        from it, so an auto-RE-MINT (DEC-GROWW-TOKEN-REAUTH-001) reaches every in-flight
        call without rebuilding the client; when ``None`` (offline / dry-run / tests)
        this is byte-identical to using :attr:`sdk` directly.
        """
        return (
            self.refresher.sdk_at(self.refresher_index) if self.refresher is not None else self.sdk
        )

    def _call(
        self, fn: Callable[[GrowwSDK], Mapping[str, object]], *, label: str = ""
    ) -> Mapping[str, object]:
        """Run *fn* through the adaptive limiter: pace → retry retryable → decrement/recover.

        Every physical call acquires a token (:attr:`pace`), resolves the live SDK
        (:meth:`_resolve_sdk` — the :attr:`refresher`'s if wired), then runs
        ``fn(sdk)``. On a RETRYABLE failure (rate-limit OR timeout — :func:`is_retryable`)
        it backs off + retries within :attr:`max_attempts`; a rate-limit additionally
        fires :attr:`on_rate_limit` (decrement the shared bucket) on EVERY occurrence and
        uses the longer :attr:`rate_limit_backoff_seconds`. A clean call fires
        :attr:`on_success` (recover the rate up).

        On an AUTH error (DEC-GROWW-TOKEN-REAUTH-001) — the 06:00 IST daily-token reset —
        if a :attr:`refresher` is wired and the bounded :attr:`max_reauths` budget is not
        exhausted, it RE-MINTS a fresh access token (thread-safe + single-flight via the
        refresher's generation check) and retries against the refreshed SDK WITHOUT
        counting against :attr:`max_attempts` (so an expiry mid-retry-storm still gets its
        bound). A genuinely-bad credential re-mints :attr:`max_reauths` times then raises
        the auth error terminally (never an infinite loop). If the FORCED re-mint is itself
        ISSUANCE-THROTTLED (DEC-GROWW-TOKEN-DISK-CACHE-001), ``refresher.reauth`` raises a
        :class:`~brutex.adapters.groww.errors.GrowwTokenIssuanceThrottled` (a RETRYABLE
        :class:`GrowwAuthError` subclass) that propagates straight OUT of this method —
        it is NOT re-entered into the reauth branch (so no infinite re-mint recursion) and
        the orchestrator's layer-3 re-queue re-pulls the window (``is_retryable`` is True).
        On a READ-ONLY shared-AWS host (DEC-BROKER-READONLY-REAUTH-REREAD-001) the refresher
        instead RE-READS the Lambda-minted token (never mints); a re-read that still holds the
        SAME dead token raises a RETRYABLE
        :class:`~brutex.adapters.groww.errors.GrowwTokenStillStaleError` — which since
        DEC-TOKEN-STALE-PAUSE-001 first PAUSES this call on the refresher's bounded,
        single-flight-aware :meth:`~brutex.adapters.groww.reauth.SdkRefresher.\
        await_token_recovery` (the 06:00-IST fetch-lane pause, gated by
        :attr:`token_stale_wait_seconds`): a fresh token adopted within the shared
        deadline retries this call with NO window attempt spent, while a deadline
        expiry propagates the StillStale OUT of this method exactly like the throttle —
        the window quarantines + re-pulls later (``is_retryable`` is True), never a
        fail-loud restart.

        A terminal error (auth with no refresher / budget exhausted / bad-request /
        not-found / unknown), or a retryable one that exhausts the budget, re-raises the
        neutral :class:`GrowwError` for the caller to handle (the orchestrator's layer-3
        re-queue). Linear backoff (``base × attempt``) keeps it O(1)-state + bounded.
        """
        attempt = 0
        reauths = 0
        while True:
            attempt += 1
            self.pace()
            sdk = self._resolve_sdk()
            # Bracket the physical call for the achieved-throughput meter
            # (DEC-GROWW-RATE-ACHIEVED-METER-001): start AFTER pace() so in-flight
            # measures the call itself, not the token wait; stop on BOTH outcomes so the
            # in-flight slot is always released (a failed attempt counts only its
            # in-flight, never as achieved throughput — that stays SUCCESSFUL calls).
            self.meter.start()
            started = self.clock()
            elapsed_ms = 0.0
            completed = False
            try:
                # Bound EVERY physical call by the wall-clock deadline so a stalled
                # connection can never freeze the pull (DEC-GROWW-PULL-CALL-DEADLINE-001);
                # a timeout raises → classified retryable → handled by this same loop.
                result = (
                    fn(sdk)
                    if self.call_timeout_seconds is None
                    # bind `sdk` via partial so the deadline thread runs THIS attempt's
                    # resolved SDK (and ruff B023 — loop-var closure capture — is moot).
                    else run_with_deadline(partial(fn, sdk), self.call_timeout_seconds)
                )
            except Exception as exc:  # broad: any SDK error → neutral GrowwError
                err = from_groww_exception(exc)
                # Failed-attempt observability (DEC-PULL-TELEMETRY-TRIAD-001): exactly ONE
                # ``groww.call`` outcome=failed line per FAILED attempt — EVERY raise path out
                # of the timed call lands here first (the terminal raise, the 429/timeout
                # retry ``continue``s, AND the reauth branch — including a re-mint that itself
                # raises out of ``refresher.reauth`` below), so a burned-call burst (the 06:00
                # token-reset blip) is READ from /logs, never back-inferred. The elapsed is
                # computed HERE from ``started`` because the shared ``elapsed_ms`` local is
                # deliberately left ``0.0`` on the failure path — the meter's
                # ``stop(elapsed_ms=0.0, completed=False)`` bracket stays byte-identical.
                _LOG.info(
                    "groww.call",
                    method=label,
                    attempt=attempt,
                    elapsed_ms=round((self.clock() - started) * 1000.0, 1),
                    outcome="failed",
                    error_type=type(err).__name__,
                )
                if isinstance(err, GrowwRateLimitError):
                    self.on_rate_limit()  # decrement the shared bucket on EVERY 429
                # AUTH-error auto-RE-MINT (DEC-GROWW-TOKEN-REAUTH-001): on the 06:00 IST
                # daily-token reset, re-mint a fresh token (thread-safe + single-flight)
                # and retry against the refreshed SDK on a SEPARATE `reauths` counter, so a
                # token expiry mid-retry-storm still self-heals without consuming the
                # rate-limit `max_attempts` budget. The `attempt -= 1` buys back exactly ONE
                # attempt per re-auth, so the TOTAL worst-case iteration count is hard-bounded
                # by `max_attempts + max_reauths` however auth/429 errors interleave. Bounded
                # by `max_reauths`: a genuinely-bad credential re-mints once then raises
                # terminally (never an infinite loop).
                if (
                    isinstance(err, GrowwAuthError)
                    and self.refresher is not None
                    and reauths < self.max_reauths
                ):
                    # Capture the generation BEFORE the re-mint so single-flight works:
                    # the refresher only re-mints if no other thread already has.
                    seen_generation = self.refresher.current_generation()
                    try:
                        new_generation = self.refresher.reauth(seen_generation)
                    except GrowwTokenStillStaleError:
                        # TOKEN-STALE GATE (DEC-TOKEN-STALE-PAUSE-001): the read-only
                        # broker holds no fresh token yet (the 06:00-IST reset window
                        # before the external Lambda's mint). PAUSE the fetch lane —
                        # bounded, single-flight-aware, watchdog-beating — instead of
                        # propagating, so the outage never reaches the window ladder
                        # and no window attempts burn (the forensic 111-FAILED-window
                        # shape). On the shared deadline expiring, the refresher
                        # re-raises the StillStale and it propagates OUT of _call
                        # exactly as before the gate — a never-minting broker still
                        # fails loudly and the window defers FAILED. ONLY this error
                        # class engages the gate: a LOCAL-mint refresher re-mints
                        # synchronously (never raises StillStale — no double-wait),
                        # and a genuine terminal auth failure stays terminal.
                        new_generation = self.refresher.await_token_recovery(
                            seen_generation,
                            wait_seconds=self.token_stale_wait_seconds,
                            sleep=self.sleep,
                            clock=self.clock,
                            method=label,
                        )
                    reauths += 1
                    attempt -= 1  # a re-auth retry must not spend the rate-limit budget
                    # OBSERVABILITY (DEC-GROWW-TOKEN-REAUTH-001): make the 06:00 IST daily-token
                    # self-heal VISIBLE in /logs — the operator SEES the re-mint + the pull
                    # continuing, instead of the old SILENT quarantine storm. NEVER logs the
                    # token/secret (only the bumped generation + the call label + the count).
                    _LOG.warning(
                        "groww.token.reauth",
                        method=label,
                        reauth=reauths,
                        generation=new_generation,
                    )
                    # FP-1 hardening (DEC-GROWW-AUTH-HALT-001): give the fresh token a
                    # beat to become fleet-valid before the retry — an INSTANT retry at
                    # the 06:00 IST rotation edge can 401 against a not-yet-propagated
                    # token and burn a bounded ``max_reauths`` slot on a transient
                    # (propagation lag is vendor-UNVERIFIED; treated as possible).
                    # Rides the injectable ``self.sleep`` so tests stay instant.
                    self.sleep(REAUTH_RETRY_BACKOFF_SECONDS)
                    continue
                if is_retryable(err) and attempt < self.max_attempts:
                    base = (
                        self.rate_limit_backoff_seconds
                        if isinstance(err, GrowwRateLimitError)
                        else self.backoff_seconds
                    )
                    self.sleep(base * attempt)
                    continue
                raise err from exc
            else:
                elapsed_ms = round((self.clock() - started) * 1000.0, 1)
                completed = True  # a clean call IS achieved throughput (recorded below)
                self.on_success()  # recover the rate up on a clean call
                # Per-call latency observability (DEC-GROWW-CALL-LATENCY-LOG-001): the
                # REAL response time of THIS Groww REST call, so the operator can SEE
                # it live in /logs instead of it being back-derived/assumed.
                _LOG.info(
                    "groww.call",
                    method=label,
                    attempt=attempt,
                    elapsed_ms=elapsed_ms,
                    outcome="success",
                )
                return result
            finally:
                # ALWAYS release the in-flight slot (success, retry, terminal, OR a
                # BaseException — KeyboardInterrupt / a futures cancellation — that
                # `except Exception` would miss and leak the very gauge this DEC makes
                # trustworthy, DEC-GROWW-RATE-ACHIEVED-METER-001); a clean call also
                # records the completion (completed=True), a failed one does not.
                self.meter.stop(elapsed_ms=elapsed_ms, completed=completed)

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
        """Fetch one historical-candle window (the row-of-arrays payload).

        The ``start`` / ``end`` bounds are normalized to Groww's DOCUMENTED
        ``yyyy-MM-dd HH:mm:ss`` SPACE-separated format (DEC-GROWW-CANDLE-TS-SPACE-001):
        the windows are tiled with :meth:`datetime.isoformat` which emits the ``T``
        separator (``2025-11-30T00:00:00``), but the API reference
        (``docs/groww/api-reference/03-backtesting.md``) specifies a space. Our OWN
        parser already accepts both (:func:`~brutex.adapters.groww.parser.parse_timestamp_ist`
        strips the ``T``), but the request side is aligned to the documented contract
        at this single funnel (spot + options share it — CLAUDE.md §11 COMMON). The
        replace is a no-op for an epoch-seconds bound (no ``T``), so it is always safe.
        """
        return self._call(
            lambda sdk: sdk.get_historical_candles(
                exchange=exchange,
                segment=segment,
                groww_symbol=groww_symbol,
                start_time=start.replace("T", " "),
                end_time=end.replace("T", " "),
                candle_interval=interval,
            ),
            label="historical_candles",
        )

    def expiries(
        self,
        *,
        exchange: str,
        underlying_symbol: str,
        year: int | None = None,
        month: int | None = None,
    ) -> Mapping[str, object]:
        """List FNO expiries for an underlying, optionally scoped to a year/month.

        ``year`` and ``month`` are OPTIONAL per the SDK
        (``docs/groww/api-reference/03-backtesting.md:60-79``): a ``None`` ``year``
        defaults to the current year, a ``None`` ``month`` to the WHOLE year. Each
        is OMITTED from the SDK call when ``None`` (never forwarded as ``None``), so
        the option enumerator can issue ONE call per historical year (month omitted)
        to cover the full FNO depth (DEC-GROWW-OPTION-INGEST-001).
        """
        kwargs: dict[str, object] = {"exchange": exchange, "underlying_symbol": underlying_symbol}
        if year is not None:
            kwargs["year"] = year
        if month is not None:
            kwargs["month"] = month
        return self._call(lambda sdk: sdk.get_expiries(**kwargs), label="get_expiries")

    def contracts(
        self, *, exchange: str, underlying_symbol: str, expiry_date: str
    ) -> Mapping[str, object]:
        """List contract symbols for an underlying + expiry."""
        return self._call(
            lambda sdk: sdk.get_contracts(
                exchange=exchange, underlying_symbol=underlying_symbol, expiry_date=expiry_date
            ),
            label="get_contracts",
        )

    def with_pace(self, pace: Callable[[], None]) -> GrowwClient:
        """Return a copy of this client with its ``pace`` hook rebound (O(1), immutable).

        The :class:`GrowwClientProtocol` seam the ingest spine uses
        (:func:`~brutex.adapters.groww.run_ingest.with_paced_client`) to wire the shared
        rate bucket into the client's internal fast-retry AFTER the adapter is built —
        replacing the prior raw :func:`dataclasses.replace`, so the SAME call also re-fans
        the pace into every leaf of a multi-connection :class:`~brutex.adapters.groww.pool.\
ClientPool` (DEC-GROWW-CLIENT-POOL-001). Frozen dataclass → :func:`replace`, never a
        mutation; the shared :attr:`meter` is carried through unchanged (``compare=False``).
        """
        return replace(self, pace=pace)

    def with_rate_hooks(
        self,
        *,
        pace: Callable[[], None] | None = None,
        on_rate_limit: Callable[[], None] | None = None,
        on_success: Callable[[], None] | None = None,
    ) -> GrowwClient:
        """Return a copy with the shared controller's FULL hook triple rebound (O(1), immutable).

        The DEC-SUSPICION-SWEEP-FIXES-001 widening of :meth:`with_pace`: the spot
        ingest seam (:func:`~brutex.adapters.groww.run_ingest.with_paced_client`)
        historically rebound ONLY ``pace``, so the client's internal bounded
        fast-retry absorbed 429s with a NO-OP :attr:`on_rate_limit` — the AIMD
        ladder saw a 429 only after the whole ``max_attempts`` budget was exhausted
        and the error propagated to the spine (~3× ``max_attempts`` vendor
        rejections per designed step-down, sub-exhaustion bursts invisible). This
        seam binds all three hooks the way the F&O build sites always did
        (``historical_pull._build_live_client`` → :func:`~brutex.adapters.groww.\
live.build_live_client`), so every 429 occurrence decrements and every clean call
        recovers the SHARED bucket. A ``None`` hook keeps the current binding
        (never downgrades a bound hook back to the no-op). Frozen dataclass →
        :func:`replace`; the shared :attr:`meter` is carried through unchanged.
        """
        return replace(
            self,
            pace=pace if pace is not None else self.pace,
            on_rate_limit=on_rate_limit if on_rate_limit is not None else self.on_rate_limit,
            on_success=on_success if on_success is not None else self.on_success,
        )


def client_self_paces(client: object) -> bool:
    """Return whether *client* already acquires a rate-bucket token per physical call.

    The ONE predicate the ingest spines use to pick the canonical admission point
    (DEC-RATE-SINGLE-ADMISSION-001): a :class:`GrowwClient` (or a
    :class:`~brutex.adapters.groww.pool.ClientPool`) whose ``pace`` hook is bound
    to the shared rate bucket admits each physical attempt itself inside
    :meth:`GrowwClient._call` — retry attempts included — so the caller must NOT
    pace again per window: a double admission halves the achieved physical rate
    and double-charges the 500/min governor for one fetch. An object without a
    ``self_paces`` surface (a test fake / a non-Groww adapter's client) reads
    ``False``, keeping the window-level pace as the fallback admission.

    Parameters
    ----------
    client
        Any candidate client (a :class:`GrowwClient`, a pool, a fake, or ``None``).

    Returns
    -------
    bool
        ``True`` iff the client exposes a truthy ``self_paces`` attribute.
    """
    return bool(getattr(client, "self_paces", False))


def client_self_reports_rate_limits(client: object) -> bool:
    """Return whether *client* already feeds every 429 occurrence to the shared ladder.

    The 429-feedback twin of :func:`client_self_paces`
    (DEC-SUSPICION-SWEEP-FIXES-001): a :class:`GrowwClient` (or a
    :class:`~brutex.adapters.groww.pool.ClientPool`) whose ``on_rate_limit`` hook
    is bound to the shared controller fires it inside :meth:`GrowwClient._call`
    on EVERY 429 occurrence — the final, budget-exhausting one included — so the
    ingest spines must NOT fire their own hook again on the propagated error (a
    double count of the final occurrence over-brakes the AIMD ladder). An object
    without the surface (a test fake / an unhooked client) reads ``False``,
    keeping the spine-level hook as the fallback feedback point.
    """
    return bool(getattr(client, "self_reports_rate_limits", False))


def client_self_reports_successes(client: object) -> bool:
    """Return whether *client* already feeds every clean call to the shared ladder.

    The recovery twin of :func:`client_self_reports_rate_limits`: a client whose
    ``on_success`` hook is bound fires it per clean physical call inside
    :meth:`GrowwClient._call`, so the spine's per-window recovery hook must stand
    down or the step-up evidence is double-credited. ``False`` for any object
    without the surface (the spine hook stays the fallback).
    """
    return bool(getattr(client, "self_reports_successes", False))


@runtime_checkable
class GrowwClientProtocol(Protocol):
    """The neutral client surface the spot adapter + option enumerator depend on.

    BOTH the single :class:`GrowwClient` AND the multi-connection
    :class:`~brutex.adapters.groww.pool.ClientPool` satisfy this structurally, so the
    adapter's ``client`` slot (and the shared candle / contract fetchers) accept EITHER
    without change (DEC-GROWW-CLIENT-POOL-001). In both cases ONE shared
    :class:`~brutex.adapters.rate_common.RateController` caps the AGGREGATE issue rate and
    ONE shared :class:`~brutex.adapters.call_meter.CallMeter` reports the achieved rate;
    the pool merely fans physical calls across K independent SDK connections so the worker
    threads stop serialising on a single shared socket (the measured 0.49-in-flight starve).
    """

    @property
    def meter(self) -> CallMeter:
        """The shared achieved-throughput meter the dashboard rate panel reads.

        Read-only (``_observe_rate`` → ``adapter.client.meter.snapshot()``); both a frozen
        :class:`GrowwClient` (a dataclass field) and a :class:`~brutex.adapters.groww.pool.\
ClientPool` (a ``@property`` over its ONE aggregate meter) satisfy it.
        """
        raise NotImplementedError  # structural Protocol stub — never executed

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
        """Fetch one historical-candle window (the row-of-arrays payload)."""
        raise NotImplementedError  # structural Protocol stub — never executed

    def expiries(
        self,
        *,
        exchange: str,
        underlying_symbol: str,
        year: int | None = None,
        month: int | None = None,
    ) -> Mapping[str, object]:
        """List FNO expiries for an underlying."""
        raise NotImplementedError  # structural Protocol stub — never executed

    def contracts(
        self, *, exchange: str, underlying_symbol: str, expiry_date: str
    ) -> Mapping[str, object]:
        """List contract symbols for an underlying + expiry."""
        raise NotImplementedError  # structural Protocol stub — never executed

    def with_pace(self, pace: Callable[[], None]) -> Self:
        """Return a copy with the rate-bucket ``pace`` hook rebound (re-fanned to leaves)."""
        raise NotImplementedError  # structural Protocol stub — never executed

    def with_rate_hooks(
        self,
        *,
        pace: Callable[[], None] | None = None,
        on_rate_limit: Callable[[], None] | None = None,
        on_success: Callable[[], None] | None = None,
    ) -> Self:
        """Return a copy with the full controller hook triple rebound (re-fanned to leaves)."""
        raise NotImplementedError  # structural Protocol stub — never executed


__all__ = [
    "REAUTH_RETRY_BACKOFF_SECONDS",
    "GrowwClient",
    "GrowwClientProtocol",
    "GrowwSDK",
    "client_self_paces",
    "client_self_reports_rate_limits",
    "client_self_reports_successes",
    "run_with_deadline",
]
