"""Groww historical-candle fetcher — the real :class:`CandleFetcher`.

This is the "phone line to Groww": the thin client the resilient
:func:`~brutex.adapters.groww.fetch_loop.run_fetch` loop drives. It wraps
the ``growwapi`` SDK's ``get_historical_candles`` call and turns one
:class:`~brutex.data.watermark.ChunkKey` window into a list of raw candle
rows (``[ts, o, h, l, c, vol, oi]`` — see ``docs/groww/api-reference/
03-backtesting.md``), ready for :func:`~brutex.adapters.groww.parser.
parse_candle_row`.

Two operator guarantees are enforced HERE, at the request boundary:

* **Rate-limit respect (CLAUDE.md §2 rule 6).** Every SDK call is paced
  through an injected token-bucket :class:`Pacer`
  (:class:`~brutex.orchestrator.rate_limiter.AdaptiveTokenBucket` in
  production): ``acquire()`` blocks until a token is free, ``record_success``
  / ``record_rate_limit_hit`` feed the bucket's adaptive up/down-shift. The
  Groww backtesting bucket's per-second cap is UNVERIFIED (only Orders /
  Live-Data / Non-Trading are documented — see ``docs/groww/api-reference/
  01-introduction.md``), so the bucket defaults conservatively and the
  Phase 0b probe is the hard gate before any raise.
* **Date-chunk respect.** The 30/90/180-day per-call window limits live in
  :data:`~brutex.adapters.groww.chunks.WINDOW_DAYS`; this client receives
  one already-windowed :class:`ChunkKey` per call and never widens it.

Lazy SDK import: ``growwapi`` is imported only inside :meth:`GrowwCandleFetcher.
from_env` (production wiring), so this module imports — and unit-tests run —
with no SDK installed. Tests inject a fake SDK + fake exception base.

Broker-neutral error boundary: ``growwapi`` raises ``Groww*Exception`` types,
but :func:`~brutex.orchestrator.retry.classify_exception` keys off BruteX's
neutral ``Vendor*`` vocabulary (the same anonymisation as ``vendor_symbol`` /
``MarketAdapter``). This client translates each caught SDK exception to its
neutral twin via a ``Groww`` → ``Vendor`` prefix swap, so the loop classifies
it without importing the SDK. See ``docs/TRACK1_GROWW_ADAPTER.md §6``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol, cast
from zoneinfo import ZoneInfo

from brutex.data.watermark import ChunkKey
from brutex.models import BruteXError

#: India Standard Time — Groww's historical-candle ``start_time`` / ``end_time``
#: are IST market-time strings (symmetric with the response side, which
#: ``brutex.adapters.groww.parser`` reads back as Asia/Kolkata — CLAUDE.md §6).
_IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")


class GrowwFetchError(BruteXError):
    """A usage/wiring error in the Groww client (not a transient API error).

    Raised by :meth:`GrowwCandleFetcher.reauth` when no re-auth callback is
    configured. Deliberately NOT one of the neutral ``Vendor*`` types — it
    signals a programmer/config bug that should fail loudly, never be
    quarantined as a per-chunk outcome.
    """


class VendorSdkError(BruteXError):
    """Neutral base for a translated ``growwapi`` SDK exception.

    The named subclasses in :data:`_NEUTRAL_BY_GROWW_NAME` carry the
    ``Vendor*`` names that :func:`~brutex.orchestrator.retry.classify_exception`
    recognises — so a re-raised instance routes to the correct
    :class:`~brutex.orchestrator.state.Outcome` (retry / quarantine / re-auth).
    """


#: The full ``growwapi`` exception hierarchy, HARD-VERIFIED against the
#: installed ``growwapi==1.5.0`` wheel (``growwapi.groww.exceptions``). Each
#: maps to its neutral twin via :func:`_neutral_name`, lining up 1:1 with the
#: keys of :data:`~brutex.orchestrator.retry.BROKER_EXCEPTION_CLASSIFICATION`.
#:
#: NOTE: the SDK names its root ``BaseGrowwException`` (NOT ``GrowwBaseException``
#: — CLAUDE.md §6's facts table is stale on this one point). The names below
#: therefore use the verified SDK spelling so the MRO walk in
#: :func:`to_neutral_exc` matches a real caught exception.
_GROWW_EXCEPTION_NAMES: Final[tuple[str, ...]] = (
    "BaseGrowwException",
    "GrowwAPIException",
    "GrowwAPIAuthenticationException",
    "GrowwAPIAuthorisationException",
    "GrowwAPIBadRequestException",
    "GrowwAPINotFoundException",
    "GrowwAPIRateLimitException",
    "GrowwAPITimeoutException",
    "GrowwFeedException",
    "GrowwFeedConnectionException",
    "GrowwFeedNotSubscribedException",
)


def _neutral_name(groww_name: str) -> str:
    """Map a ``growwapi`` SDK exception name to its neutral ``Vendor*`` twin.

    Every concrete SDK exception is ``Groww*``-prefixed, so a one-shot
    ``Groww`` → ``Vendor`` swap yields the neutral name. The single
    exception is the SDK root ``BaseGrowwException`` (irregularly named —
    ``Base`` leads, not ``Groww``); it maps explicitly to
    ``VendorBaseException`` so it still lines up with
    :data:`~brutex.orchestrator.retry.BROKER_EXCEPTION_CLASSIFICATION`.
    """
    if groww_name == "BaseGrowwException":
        return "VendorBaseException"
    return groww_name.replace("Groww", "Vendor", 1)


#: ``Groww*`` name → freshly-minted neutral exception class. Built once at
#: import; the classes are stable for the process lifetime.
_NEUTRAL_BY_GROWW_NAME: Final[dict[str, type[VendorSdkError]]] = {
    name: type(_neutral_name(name), (VendorSdkError,), {}) for name in _GROWW_EXCEPTION_NAMES
}

#: Neutral name that classifies as a rate-limit (drives bucket down-shift).
_RATE_LIMIT_NEUTRAL_NAME: Final[str] = _neutral_name("GrowwAPIRateLimitException")


def bad_request(message: str) -> VendorSdkError:
    """Neutral ``BAD_REQUEST`` (terminal — quarantine the chunk, no retry).

    Public so the persistence layer can raise the same terminal-quarantine
    twin for a corrupt row (CLAUDE.md §11 COMMON — one constructor).
    """
    return _NEUTRAL_BY_GROWW_NAME["GrowwAPIBadRequestException"](message)


def _api_error(message: str) -> VendorSdkError:
    """Neutral generic API error (retry once, then quarantine)."""
    return _NEUTRAL_BY_GROWW_NAME["GrowwAPIException"](message)


def to_neutral_exc(exc: BaseException) -> VendorSdkError:
    """Translate a caught growwapi SDK exception to its neutral ``Vendor*`` twin.

    Walks the MRO (like :func:`~brutex.orchestrator.retry.classify_exception`)
    so a subclass Groww exception resolves to the nearest known twin. Shared by
    every surface that calls the SDK (candles / expiries / contracts) so the
    translation lives in ONE place (CLAUDE.md §11 COMMON).
    """
    for cls in type(exc).__mro__:
        neutral_cls = _NEUTRAL_BY_GROWW_NAME.get(cls.__name__)
        if neutral_cls is not None:
            return neutral_cls(str(exc))
    # Unreachable: callers only pass ``base_exception`` subclasses, whose MRO
    # always contains "BaseGrowwException".
    return VendorSdkError(str(exc))  # pragma: no cover


def is_rate_limit(neutral: VendorSdkError) -> bool:
    """True if *neutral* is the rate-limit twin (→ feed the bucket's down-shift)."""
    return type(neutral).__name__ == _RATE_LIMIT_NEUTRAL_NAME


#: BruteX interval id → ``growwapi`` ``CANDLE_INTERVAL_*`` attribute name.
#: Keys mirror :data:`~brutex.adapters.groww.chunks.WINDOW_DAYS` exactly.
#: The value is resolved at call time via ``getattr(sdk, attr)`` rather than
#: hard-coding the SDK's interval strings (Golden Rule #1 — don't re-derive a
#: vendor constant; read it from the SDK).
_INTERVAL_ATTR: Final[dict[str, str]] = {
    "1m": "CANDLE_INTERVAL_MIN_1",
    "2m": "CANDLE_INTERVAL_MIN_2",
    "3m": "CANDLE_INTERVAL_MIN_3",
    "5m": "CANDLE_INTERVAL_MIN_5",
    "10m": "CANDLE_INTERVAL_MIN_10",
    "15m": "CANDLE_INTERVAL_MIN_15",
    "30m": "CANDLE_INTERVAL_MIN_30",
    "1h": "CANDLE_INTERVAL_HOUR_1",
    "4h": "CANDLE_INTERVAL_HOUR_4",
    "1d": "CANDLE_INTERVAL_DAY",
    "1w": "CANDLE_INTERVAL_WEEK",
    "1mo": "CANDLE_INTERVAL_MONTH",
}

#: A ``groww_symbol`` ending in one of these is an FNO contract (segment=FNO);
#: anything else (index / equity) is segment=CASH. See backtesting doc §"Groww
#: Symbol": ``EXCHANGE-SYMBOL[-EXPIRY[-STRIKE]-TYPE]``.
_FNO_SUFFIXES: Final[tuple[str, ...]] = ("-FUT", "-CE", "-PE")


def symbol_to_exchange_segment(groww_symbol: str) -> tuple[str, str]:
    """Derive ``(exchange, segment)`` from a ``groww_symbol``'s structure.

    The symbol encodes everything (backtesting doc §"Groww Symbol"):
    ``EXCHANGE-SYMBOL[-EXPIRY[-STRIKE]-TYPE]``. Exchange is the leading
    token; segment is FNO for a futures/option suffix, else CASH. Shared by
    the request builder and the persistence layer so the rule lives in ONE
    place (CLAUDE.md §11 COMMON).
    """
    exchange = groww_symbol.split("-", 1)[0]
    segment = "FNO" if groww_symbol.endswith(_FNO_SUFFIXES) else "CASH"
    return exchange, segment


def _to_groww_time(iso_ts: str) -> str:
    """Format a watermark ISO timestamp as Groww's ``"yyyy-MM-dd HH:mm:ss"``.

    The growwapi SDK's ``get_historical_candles`` documents ``start_time`` /
    ``end_time`` as **``"yyyy-MM-dd HH:mm:ss"`` strings** (HARD-VERIFIED from
    the wheel docstring), and Groww candle times are IST market times — the
    response parser localises them as ``Asia/Kolkata`` (``parser.py`` /
    CLAUDE.md §6). So the chunk boundary (tz-aware UTC from the planner) is
    converted to IST wall-clock and formatted — symmetric with the parse path.

    The prior implementation sent an **integer epoch** ("epoch is also
    accepted" was an UNVERIFIED claim); against live Groww that was rejected
    for every chunk (``completed=0 failed=N``), so this restores the documented
    string contract (DEC-PULL-CANDLE-TIME-FORMAT-001).
    """
    parsed = datetime.fromisoformat(iso_ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(_IST).strftime("%Y-%m-%d %H:%M:%S")


class Pacer(Protocol):
    """The slice of :class:`~brutex.orchestrator.rate_limiter.AdaptiveTokenBucket`
    this client drives. A local Protocol (not a hard import) keeps the module
    free of the orchestrator package's eager LMDB/Bloom import chain — so it
    imports without native deps and unit-tests with a plain mock.
    """

    def acquire(self) -> float:
        """Block until a request token is free; return seconds waited."""
        ...

    def record_success(self) -> None:
        """Mark one clean (non-rate-limited) call — feeds adaptive up-shift."""
        ...

    def record_rate_limit_hit(self) -> object:
        """Mark one rate-limit hit — feeds adaptive down-shift."""
        ...


@dataclass(slots=True)
class GrowwCandleFetcher:
    """Real :class:`~brutex.adapters.groww.fetch_loop.CandleFetcher` over ``growwapi``.

    Parameters
    ----------
    sdk
        An authenticated ``growwapi.GrowwAPI`` instance (or a fake exposing
        ``get_historical_candles`` + the ``EXCHANGE_* / SEGMENT_* /
        CANDLE_INTERVAL_*`` constants).
    bucket
        Token-bucket :class:`Pacer` for rate-limit respect.
    base_exception
        The SDK's exception root (``growwapi.groww.exceptions.BaseGrowwException``,
        or a fake). Only subclasses of this are translated; anything else
        propagates untouched (it would be a non-SDK bug).
    reauth_fn
        Optional callback returning a freshly-authenticated SDK. Wired in
        production so the loop's daily/6 AM token-rollover hook can self-heal
        (:meth:`reauth`). ``None`` in tests that don't exercise re-auth.
    """

    sdk: Any
    bucket: Pacer
    base_exception: type[BaseException]
    reauth_fn: Callable[[], Any] | None = None

    def fetch_chunk(self, key: ChunkKey) -> Sequence[object]:
        """Fetch one window's raw candle rows for *key*.

        Returns the raw 7-field rows (possibly empty). Never raises a bare
        Python error to the loop: every failure is re-raised as a neutral
        ``Vendor*`` exception so :func:`~brutex.orchestrator.retry.
        classify_exception` can route it (retry / re-auth / quarantine).
        """
        try:
            request = self._build_request(key)
        except (KeyError, AttributeError, ValueError) as exc:
            # Unmappable interval / symbol / timestamp — a malformed request
            # for THIS chunk. Quarantine it (BAD_REQUEST), don't kill the run.
            raise bad_request(f"unmappable chunk {key!r}: {exc}") from exc

        self.bucket.acquire()
        try:
            response = self.sdk.get_historical_candles(**request)
            candles = response["candles"]
        except self.base_exception as exc:
            neutral = to_neutral_exc(exc)
            if is_rate_limit(neutral):
                self.bucket.record_rate_limit_hit()
            raise neutral from exc
        except (KeyError, TypeError) as exc:
            # 200 OK but the body wasn't the documented ``{"candles": [...]}``.
            raise _api_error(f"malformed candle response for {key.groww_symbol!r}") from exc

        self.bucket.record_success()
        return list(candles)

    def reauth(self) -> None:
        """Replace the SDK with a freshly-authenticated one (token rollover)."""
        if self.reauth_fn is None:
            raise GrowwFetchError("reauth requested but no reauth_fn configured")
        self.sdk = self.reauth_fn()

    def _build_request(self, key: ChunkKey) -> dict[str, object]:
        """Translate a :class:`ChunkKey` into ``get_historical_candles`` kwargs."""
        symbol = key.groww_symbol
        exchange_code, segment = symbol_to_exchange_segment(symbol)
        return {
            "exchange": getattr(self.sdk, f"EXCHANGE_{exchange_code}"),
            "segment": getattr(self.sdk, f"SEGMENT_{segment}"),
            "groww_symbol": symbol,
            "start_time": _to_groww_time(key.start),
            "end_time": _to_groww_time(key.end),
            "candle_interval": getattr(self.sdk, _INTERVAL_ATTR[key.interval]),
        }

    @classmethod
    def from_env(cls, bucket: Pacer) -> GrowwCandleFetcher:  # pragma: no cover
        """Build a production fetcher from ``BRUTEX_GROWW_*`` env vars.

        TOTP-only auth (``docs/TRACK1_GROWW_ADAPTER.md §2``): generate a TOTP
        from ``BRUTEX_GROWW_TOTP_SECRET``, exchange the ``BRUTEX_GROWW_API_KEY``
        token for an access token, and build the SDK. The ``reauth_fn`` repeats
        the flow so the loop can refresh on token expiry.

        Marked ``no cover`` — it imports ``growwapi`` + ``pyotp`` and performs
        live network auth; exercised only by the ``BRUTEX_GROWW_SMOKE`` smoke
        test, never in the offline unit suite.
        """
        import os

        from growwapi import GrowwAPI
        from growwapi.groww.exceptions import BaseGrowwException

        api_key = os.environ["BRUTEX_GROWW_API_KEY"]
        totp_secret = os.environ["BRUTEX_GROWW_TOTP_SECRET"]

        def _auth() -> Any:
            import pyotp

            totp = pyotp.TOTP(totp_secret).now()
            # ``get_access_token`` is annotated ``-> dict`` in growwapi 1.5.0,
            # but its body returns ``response.json()["token"]`` — i.e. the
            # access-token STRING (HARD-VERIFIED from the wheel source). The
            # stale annotation is the SDK's bug; cast to ``str`` so the
            # ``GrowwAPI(token: str)`` constructor type-checks.
            access_token = cast(str, GrowwAPI.get_access_token(api_key=api_key, totp=totp))
            return GrowwAPI(access_token)

        return cls(
            sdk=_auth(),
            bucket=bucket,
            base_exception=BaseGrowwException,
            reauth_fn=_auth,
        )


__all__ = [
    "GrowwCandleFetcher",
    "GrowwFetchError",
    "Pacer",
    "VendorSdkError",
    "bad_request",
    "is_rate_limit",
    "symbol_to_exchange_segment",
    "to_neutral_exc",
]
