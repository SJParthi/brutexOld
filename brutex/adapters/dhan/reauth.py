"""DhanHQ v2 token manager — thread-safe, single-flight, self-minting reauth spine.

DEC-DHAN-AUTH-001. Templated on the LOCAL-mint arm of the Groww
:class:`~brutex.adapters.groww.reauth.SdkRefresher`, inverted for Dhan's SELF-MINT posture
(BruteX mints AND writes the token). :class:`DhanTokenManager` mints the initial token at
construction (cache-first) and refreshes on demand via :meth:`reauth`, with:

* **THREAD-SAFE** — a :class:`threading.Lock` guards every read + the refresh.
* **SINGLE-FLIGHT** — a monotonic :attr:`generation` counter: a caller passes the generation
  it last saw; if another thread already refreshed, the caller is a no-op handoff.
* **FREEZE-GUARDED** — the network mint / renew is wrapped by
  :func:`~brutex.config.deadline.run_with_deadline`, so a hung socket raises a retryable
  timeout instead of holding the lock forever.
* **CROSS-PROCESS SAFE** — the mint decision runs under an advisory ``flock`` sidecar
  (``~/.brutex/locks/dhan-auth.lock``, kernel-released) with a double-checked SSM re-read
  INSIDE the lock — the mandatory cross-process mint-storm brake while
  ``DHAN_MINT_INVALIDATES_PRIOR_TOKEN`` is UNVERIFIED.

THE LADDER (:meth:`reauth`, in order):

1. **SSM re-read** — a fresher USABLE envelope in the SecureString is ADOPTED with ZERO
   vendor calls (a sibling process already minted). The brake.
2. **RENEW** — iff a current token is held AND ``BRUTEX_DHAN_RENEW`` is on: try
   ``RenewToken``; ANY failure falls through (renew is an optimization, never load-bearing).
3. **MINT** — the headless PIN+TOTP mint.

After (2)/(3): **store FIRST, then adopt REGARDLESS** — a store failure WARNs + audits but
NEVER un-adopts (worst case: the next process re-mints).

LOCKDOWN LATCH — ONE :class:`~brutex.adapters.dhan.errors.DhanAuthInvalidCredentials`, or
:data:`~brutex.adapters.dhan.constants.DHAN_MINT_LOCKDOWN_THRESHOLD` consecutive terminal
failures, latches a :class:`~brutex.adapters.dhan.errors.DhanAuthLockdown`: every later call
fails fast with ZERO HTTP and ONE CRITICAL Telegram fires. Recovery is a process restart +
the runbook (no env escape hatch).

NOT copied from the Groww refresher: the StillStale / 06:00-IST pause, the read-only re-read
cold start, the session keying. This module does NOT import ``calendars/market_hours`` — auth
is exempt by rationale (AST-pinned).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import structlog

from brutex.adapters.dhan.auth import (
    HttpTransport,
    mint_access_token,
    renew_access_token,
)
from brutex.adapters.dhan.auth_audit import (
    ACTION_ADOPT,
    ACTION_EXHAUSTED,
    ACTION_LOCKDOWN,
    ACTION_RENEW,
    ACTION_STORE,
    DhanAuthAudit,
)
from brutex.adapters.dhan.constants import (
    DHAN_AUTH_HTTP_TIMEOUT_S,
    DHAN_MINT_LOCKDOWN_THRESHOLD,
    DHAN_TOKEN_ISSUANCE_RETRY_ATTEMPTS,
    DHAN_TOKEN_ISSUANCE_RETRY_CAP_S,
    resolve_dhan_renew_enabled,
)
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanAuthInvalidCredentials,
    DhanAuthLockdown,
    DhanAuthTransportError,
    DhanTokenMintThrottled,
    DhanTokenRenewError,
)
from brutex.adapters.dhan.token_lifecycle import token_usable
from brutex.adapters.dhan.token_store_aws import (
    DhanParameterStoreTokenStore,
    DhanTokenEnvelope,
    token_fingerprint,
)
from brutex.config.credentials_types import DhanCredentials
from brutex.config.deadline import run_with_deadline

_LOG = structlog.get_logger("brutex.adapters.dhan.reauth")


class AuthAlerter(Protocol):
    """The narrow alert surface the manager fires on exhaustion / lockdown (CRITICAL)."""

    def token_auth_failed(self, *, vendor: str, reason: str, detail: str) -> bool:
        """Fire a CRITICAL token-auth-failed alert; return whether it was attempted."""
        raise NotImplementedError  # structural Protocol stub — never executed


def _now_utc() -> datetime:
    """The current UTC instant (injectable in tests)."""
    return datetime.now(tz=UTC)


def _default_lock_path() -> Path:
    """The default advisory-flock sidecar path (``~/.brutex/locks/dhan-auth.lock``)."""
    return Path.home() / ".brutex" / "locks" / "dhan-auth.lock"


class DhanTokenManager:
    """Thread-safe, single-flight, cross-process-safe minter of the daily Dhan token.

    Parameters
    ----------
    store
        The write-capable :class:`~brutex.adapters.dhan.token_store_aws.\
        DhanParameterStoreTokenStore` (read + WRITE the SecureString).
    load_credentials
        Zero-arg loader of the :class:`~brutex.config.credentials_types.DhanCredentials`
        triple (production: ``load_dhan_aws_credentials``).
    transport
        Injected :class:`~brutex.adapters.dhan.auth.HttpTransport` (``None`` ⇒ real urllib).
    alerter
        Optional CRITICAL alert sink (:class:`AuthAlerter`); ``None`` ⇒ no alerts.
    audit
        Optional :class:`~brutex.adapters.dhan.auth_audit.DhanAuthAudit`; ``None`` ⇒ no audit.
    clock
        UTC-instant source (default :func:`_now_utc`).
    max_reauths
        The :meth:`ensure_token` refresh budget (clamped ``>= 1``).
    lock_path
        The advisory-flock sidecar path (``None`` ⇒ ``~/.brutex/locks/dhan-auth.lock``);
        injected to a tmp path in tests.

    Raises
    ------
    DhanAuthLockdown
        If the cold-start mint hits an invalid credential (latched immediately).
    DhanTokenMintThrottled | DhanAuthTransportError
        If the cold-start mint is throttled / transport-failed past its budget (retryable —
        the caller re-pulls later).
    """

    __slots__ = (
        "_alerter",
        "_audit",
        "_clock",
        "_consecutive_terminal",
        "_current",
        "_generation",
        "_load_credentials",
        "_lock",
        "_lock_path",
        "_lockdown",
        "_max_reauths",
        "_mint_deadline_s",
        "_store",
        "_transport",
    )

    def __init__(
        self,
        store: DhanParameterStoreTokenStore,
        load_credentials: Callable[[], DhanCredentials],
        *,
        transport: HttpTransport | None = None,
        alerter: AuthAlerter | None = None,
        audit: DhanAuthAudit | None = None,
        clock: Callable[[], datetime] = _now_utc,
        max_reauths: int = 1,
        lock_path: Path | None = None,
    ) -> None:
        self._store = store
        self._load_credentials = load_credentials
        self._transport = transport
        self._alerter = alerter
        self._audit = audit
        self._clock = clock
        self._max_reauths = max(1, max_reauths)
        self._lock_path = lock_path if lock_path is not None else _default_lock_path()
        self._lock = threading.Lock()
        self._current: DhanTokenEnvelope | None = None
        self._generation = 0
        self._lockdown: DhanAuthLockdown | None = None
        self._consecutive_terminal = 0
        #: Overall freeze-guard deadline for the mint (its internal retry budget × the call
        #: timeout + the back-off caps), so a legitimate throttle-retry sequence is not cut
        #: while a genuine hang still aborts.
        attempts = max(1, DHAN_TOKEN_ISSUANCE_RETRY_ATTEMPTS)
        self._mint_deadline_s = (
            DHAN_AUTH_HTTP_TIMEOUT_S * attempts + DHAN_TOKEN_ISSUANCE_RETRY_CAP_S * attempts
        )
        # Cold start (construction thread; no lock contention): reread-or-mint, no renew.
        self._guarded_acquire(allow_renew=False)

    # ── Public accessors ────────────────────────────────────────────

    def current_token(self) -> str:
        """Return the live access token (lock-guarded, O(1)); raises if none / locked down."""
        with self._lock:
            self._raise_if_lockdown()
            if self._current is None:
                raise DhanAuthError("no Dhan access token has been established")
            return self._current.token

    def current_generation(self) -> int:
        """Return the current refresh generation (lock-guarded, O(1))."""
        with self._lock:
            return self._generation

    def reauth(self, observed_generation: int) -> None:
        """Refresh the token SINGLE-FLIGHT against *observed_generation* (the ladder).

        If the generation already moved past *observed_generation*, this is a no-op handoff.
        Otherwise it runs the reread → renew → mint ladder under the lock + advisory flock,
        storing-first-then-adopting the fresh token. A lockdown raises fast with zero HTTP.
        """
        with self._lock:
            self._raise_if_lockdown()
            if self._generation != observed_generation:
                return  # single-flight: another thread already refreshed
            self._guarded_acquire(allow_renew=True)

    def ensure_token(self) -> str:
        """Return a USABLE token, refreshing (bounded by *max_reauths*) if stale/absent.

        A fresh, usable in-memory token short-circuits with no refresh. Otherwise it drives
        :meth:`reauth` up to *max_reauths* times (single-flight-safe) until a usable token is
        adopted. A lockdown / a retryable-exhausted / a terminal error propagates.
        """
        with self._lock:
            self._raise_if_lockdown()
            if self._current is not None and token_usable(self._current, self._clock()):
                return self._current.token
        for _ in range(self._max_reauths):
            with self._lock:
                generation = self._generation
            self.reauth(generation)
            with self._lock:
                self._raise_if_lockdown()
                if self._current is not None and token_usable(self._current, self._clock()):
                    return self._current.token
        with self._lock:
            if self._current is not None:
                return self._current.token
            raise DhanAuthError("Dhan token unavailable after reauth budget exhausted")

    # ── The ladder (all under the caller's lock) ────────────────────

    def _guarded_acquire(self, *, allow_renew: bool) -> None:
        """Run the acquire ladder, mapping terminal failures to the lockdown latch.

        Retryable failures reset the consecutive-terminal streak (and audit EXHAUSTED); a
        single invalid-credential latches lockdown immediately; a repeated OTHER terminal
        (malformed response) latches once the threshold is reached.
        """
        try:
            self._acquire_and_adopt(allow_renew=allow_renew)
        except DhanAuthInvalidCredentials as exc:
            self._latch_lockdown("invalid_credentials", exc.safe_message)
            raise self._require_lockdown() from None
        except (DhanTokenMintThrottled, DhanAuthTransportError) as exc:
            self._consecutive_terminal = 0
            self._audit_safe(
                ACTION_EXHAUSTED,
                subject="dhan-auth",
                outcome="retryable",
                reason_class=type(exc).__name__,
            )
            raise
        except DhanAuthError as exc:
            self._consecutive_terminal += 1
            if self._consecutive_terminal >= DHAN_MINT_LOCKDOWN_THRESHOLD:
                self._latch_lockdown("repeated_terminal", exc.safe_message)
                raise self._require_lockdown() from None
            raise

    def _acquire_and_adopt(self, *, allow_renew: bool) -> None:
        """Reread → renew → mint under the advisory flock, then store-first-then-adopt."""
        now = self._clock()
        with self._advisory_lock():
            existing = self._load_guarded()
            if (
                existing is not None
                and token_usable(existing, now)
                and self._is_new_token(existing)
            ):
                self._adopt_only(existing, outcome="reread")
                return
            creds = self._load_credentials()
            new_env: DhanTokenEnvelope | None = None
            if allow_renew and self._current is not None and resolve_dhan_renew_enabled():
                new_env = self._try_renew(creds)
            if new_env is None:
                new_env = self._mint(creds)
            self._store_then_adopt(new_env)

    def _load_guarded(self) -> DhanTokenEnvelope | None:
        """Read the stored envelope under the freeze guard; a hung SSM read degrades to ``None``.

        :meth:`~brutex.adapters.dhan.token_store_aws.DhanParameterStoreTokenStore.load` never
        raises (it degrades every miss to ``None``), but the underlying boto3 ``GetParameter``
        carries only botocore's default multi-minute connect/read timeout × retries — and this
        runs while the process-wide :attr:`_lock` AND the cross-process advisory ``flock`` are
        held. The freeze guard bounds it by :data:`DHAN_AUTH_HTTP_TIMEOUT_S` (like the mint /
        renew network calls) so a stalled SSM endpoint can never hold the auth lock — and block
        every other thread's :meth:`current_token` / :meth:`reauth` — for minutes; a timeout
        degrades to ``None`` (fall through to renew / mint), never a raise. O(1).
        """
        try:
            return run_with_deadline(
                self._store.load,
                DHAN_AUTH_HTTP_TIMEOUT_S,
                what="Dhan SSM read",
                thread_name="dhan-ssm-read",
            )
        except TimeoutError as exc:
            _LOG.warning("dhan.token.load_timed_out", error_type=type(exc).__name__)
            return None

    def _is_new_token(self, envelope: DhanTokenEnvelope) -> bool:
        """True iff *envelope* holds a DIFFERENT token than the one currently in memory."""
        return self._current is None or envelope.token != self._current.token

    def _try_renew(self, creds: DhanCredentials) -> DhanTokenEnvelope | None:
        """Try RenewToken (freeze-guarded); return the fresh envelope or ``None`` on ANY failure.

        Renew is an OPTIMIZATION — a timeout / transport / renew error all return ``None`` so
        the caller falls through to a fresh mint. The current token (asserted present by the
        caller) is the one renewed.
        """
        current = self._current
        assert current is not None  # noqa: S101  # guaranteed by the caller's `self._current is not None`
        try:
            minted = run_with_deadline(
                lambda: renew_access_token(
                    current.token,
                    creds.client_id,
                    transport=self._transport,
                    clock=self._clock,
                ),
                DHAN_AUTH_HTTP_TIMEOUT_S,
                what="Dhan renew",
                thread_name="dhan-renew",
            )
        except (TimeoutError, DhanTokenRenewError) as exc:
            _LOG.warning("dhan.token.renew_failed", error_type=type(exc).__name__)
            return None
        self._audit_safe(
            ACTION_RENEW,
            subject=token_fingerprint(minted.token),
            outcome="ok",
            fingerprint=token_fingerprint(minted.token),
        )
        return DhanTokenEnvelope(
            token=minted.token, minted_at_utc=minted.minted_at_utc, source=minted.source
        )

    def _mint(self, creds: DhanCredentials) -> DhanTokenEnvelope:
        """Mint a fresh token (freeze-guarded); a hang maps to a retryable transport error."""
        try:
            minted = run_with_deadline(
                lambda: mint_access_token(creds, transport=self._transport, clock=self._clock),
                self._mint_deadline_s,
                what="Dhan mint",
                thread_name="dhan-mint",
            )
        except TimeoutError:
            raise DhanAuthTransportError("Dhan mint timed out (freeze guard)") from None
        return DhanTokenEnvelope(
            token=minted.token, minted_at_utc=minted.minted_at_utc, source=minted.source
        )

    def _store_then_adopt(self, envelope: DhanTokenEnvelope) -> None:
        """Store the fresh token FIRST, then adopt REGARDLESS (a store failure never un-adopts)."""
        fingerprint = token_fingerprint(envelope.token)
        try:
            # Freeze-guard the boto3 ``PutParameter`` (like the mint / renew / read network
            # calls): a stalled SSM write can never hold the auth lock + advisory flock for
            # botocore's multi-minute default. A timeout maps to the SAME graceful
            # store-failed path as a raised store error (never un-adopts).
            run_with_deadline(
                lambda: self._store.store(envelope),
                DHAN_AUTH_HTTP_TIMEOUT_S,
                what="Dhan SSM write",
                thread_name="dhan-ssm-write",
            )
            self._audit_safe(
                ACTION_STORE,
                subject=fingerprint,
                outcome="ok",
                fingerprint=fingerprint,
                source=envelope.source,
            )
        except (DhanAuthError, TimeoutError) as exc:
            _LOG.warning(
                "dhan.token.store_failed", error_type=type(exc).__name__, fingerprint=fingerprint
            )
            self._audit_safe(
                ACTION_STORE,
                subject=fingerprint,
                outcome="failed",
                reason_class=type(exc).__name__,
            )
        self._adopt_only(envelope, outcome=envelope.source)

    def _adopt_only(self, envelope: DhanTokenEnvelope, *, outcome: str) -> None:
        """Adopt *envelope* in memory, bump the generation, reset the terminal streak."""
        self._current = envelope
        self._generation += 1
        self._consecutive_terminal = 0
        fingerprint = token_fingerprint(envelope.token)
        self._audit_safe(
            ACTION_ADOPT,
            subject=fingerprint,
            outcome=outcome,
            fingerprint=fingerprint,
            source=envelope.source,
        )
        _LOG.info("dhan.token.adopt", outcome=outcome, fingerprint=fingerprint)

    # ── Lockdown + audit + flock helpers ────────────────────────────

    def _raise_if_lockdown(self) -> None:
        """Raise the latched :class:`DhanAuthLockdown` (zero HTTP) if auth is locked down."""
        if self._lockdown is not None:
            raise self._lockdown

    def _require_lockdown(self) -> DhanAuthLockdown:
        """Return the latched lockdown (set by :meth:`_latch_lockdown`); asserts it is present."""
        assert self._lockdown is not None  # noqa: S101  # set immediately before this call
        return self._lockdown

    def _latch_lockdown(self, reason: str, detail: str) -> None:
        """Latch the terminal lockdown: record it, audit it, fire ONE CRITICAL alert."""
        self._lockdown = DhanAuthLockdown(f"Dhan auth locked down ({reason}): {detail}")
        _LOG.error("dhan.token.lockdown", reason=reason)
        self._audit_safe(
            ACTION_LOCKDOWN, subject="dhan-auth", outcome="latched", reason_class=reason
        )
        if self._alerter is not None:
            self._alerter.token_auth_failed(
                vendor="dhan", reason=reason, detail=self._lockdown.safe_message
            )

    def _audit_safe(self, action: str, *, subject: str, **payload: object) -> None:
        """Record one audit entry when an audit sink is wired (secret-free payload)."""
        if self._audit is not None:
            self._audit.record(action, subject=subject, **payload)

    @contextlib.contextmanager
    def _advisory_lock(self) -> Iterator[None]:
        """Hold the cross-process advisory ``flock`` around the mint decision (kernel-released).

        A blocking exclusive ``flock`` on the sidecar so two BruteX processes sharing the lake
        never mint concurrently — combined with the double-checked SSM re-read inside, the
        loser adopts the winner's token. The fd is closed in ``finally`` (the kernel releases
        the lock on close / process death). O(1).
        """
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


__all__ = ["AuthAlerter", "DhanTokenManager"]
