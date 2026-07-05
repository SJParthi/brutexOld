"""Token-stale fetch-lane PAUSE tests (DEC-TOKEN-STALE-PAUSE-001).

Forensically proven problem (operator's real overnight run, 2026-07-04): at the
06:00-IST Groww token reset the read-only AWS token broker held no fresh token until
the external Lambda's 06:05:31 mint (a 331 s outage). Every fetch raised the
retryable ``GrowwTokenStillStaleError``; the F&O window ladder burned all 3 attempts
INSIDE the outage for 111 windows (339 failed calls across 115 windows), settling
them ledger-FAILED — rescued only by the outer until-clean rounds. The fix:
``SdkRefresher.await_token_recovery`` PAUSES the fetch lane (bounded, single-flight
aware, watchdog-beating) until the fresh token is adopted, so the outage never
reaches the window ladder and no window attempts burn.

Everything here runs against the REAL :class:`SdkRefresher` + REAL
:class:`GrowwClient` + REAL :func:`ingest_option_contract` spine with injected
fakes (clock / sleep / SSM re-read hook / SDK factory) — no ``growwapi``, no
network, no real time (except the deliberately-real-threaded stampede test).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.client import REAUTH_RETRY_BACKOFF_SECONDS, GrowwClient
from brutex.adapters.groww.constants import (
    GROWW_TOKEN_CACHE_PATH_ENV,
    GROWW_TOKEN_STALE_WAIT_ENV,
    GROWW_TOKEN_STALE_WAIT_S,
)
from brutex.adapters.groww.errors import (
    GrowwAuthError,
    GrowwTokenStillStaleError,
    is_retryable,
)
from brutex.adapters.groww.option_ingest import ingest_option_contract
from brutex.adapters.groww.reauth import (
    SdkRefresher,
    resolve_token_stale_wait_seconds,
)
from brutex.adapters.groww.token_cache import CachedToken
from brutex.adapters.protocol import DerivativeContract
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_status import FAILED, SUCCESS
from brutex.observability.progress_beat import (
    register_progress_beat,
    unregister_progress_beat,
)
from structlog.testing import capture_logs

_CREDS = GrowwCredentials(api_key="fake_key", totp_secret="fake_secret")
_SESSION = date(2026, 7, 4)
_STALE = "STALE-TOK"
_FRESH = "FRESH-TOK"
#: The forensically-measured 06:00:00 → 06:05:31 IST mint lag (operator log,
#: 2026-07-04): the Lambda's fresh token landed 331 s after the reset.
_OUTAGE_SECONDS = 331.0


@pytest.fixture(autouse=True)
def _isolated_token_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the on-disk token cache at a clean tmp file for every test here.

    The refresher builds below inject their own cache hooks, but the LOCAL-mint
    scenarios ride the default ``store_token`` — this keeps any write off the real
    ``~/.brutex/cache`` (mirrors ``tests/unit/test_groww_reauth.py``).
    """
    monkeypatch.setenv(GROWW_TOKEN_CACHE_PATH_ENV, str(tmp_path / "tok.json"))


# CodeQL-friendly fake time: plain ``def`` closures over a mutable holder (a
# callable CLASS fixture defeats CodeQL's points-to analysis and false-flags the
# production ``clock()`` call sites as py/call-to-non-callable).
def _fake_time() -> tuple[Callable[[], float], Callable[[float], None]]:
    """Return ``(now, advance)`` — a monotonic fake clock + its sleep-shaped mover."""
    state = {"now": 0.0}

    def now() -> float:
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] += seconds

    return now, advance


# Named like the SDK exception so from_groww_exception maps it to GrowwAuthError.
class GrowwAPIAuthenticationException(Exception):
    """Fake SDK auth-rejection (the shape a dead daily token produces)."""


class _TokenSDK:
    """A ``GrowwSDK``-shaped fake bound to the token it was built from.

    A stale-token SDK rejects EVERY call with the auth shape (exactly what the
    vendor does after the 06:00-IST reset); a fresh-token SDK serves candles.
    """

    def __init__(self, token: str) -> None:
        self.token = token
        self.calls = 0

    def _serve(self) -> Mapping[str, object]:
        self.calls += 1
        if self.token == _STALE:
            raise GrowwAPIAuthenticationException("token expired or invalid")
        return {"candles": [["2024-01-02T09:30:00", 100.0, 110.0, 90.0, 105.0, 50, 7]]}

    def get_historical_candles(self, **_kw: object) -> Mapping[str, object]:
        return self._serve()

    def get_expiries(self, **_kw: object) -> Mapping[str, object]:
        self._serve()
        return {"expiries": ["2026-07-30"]}

    def get_contracts(self, **_kw: object) -> Mapping[str, object]:  # pragma: no cover
        return {"contracts": []}


def _readonly_refresher(
    reread: Callable[..., CachedToken | None],
) -> SdkRefresher:
    """A READ-ONLY-broker refresher: cold-starts on the (stale) cached token, never mints."""
    return SdkRefresher(
        _CREDS,
        sdk_factory=_TokenSDK,
        token_cache_load=lambda _creds, now_utc: CachedToken(
            access_token=_STALE, issued_for_session=_SESSION
        ),
        token_cache_store=lambda *_a, **_k: True,
        readonly_reread=reread,
        call_timeout_seconds=5.0,
    )


def _lagging_reread(
    now: Callable[[], float],
) -> tuple[Callable[..., CachedToken | None], list[str]]:
    """A broker re-read serving the STALE token until the fake clock passes the outage."""
    served: list[str] = []

    def reread(_creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken:
        token = _FRESH if now() >= _OUTAGE_SECONDS else _STALE
        served.append(token)
        return CachedToken(access_token=token, issued_for_session=_SESSION)

    return reread, served


# ─────────────────────────────────────────────────────────────────────────
# resolve_token_stale_wait_seconds — the env-tunable pause bound
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, GROWW_TOKEN_STALE_WAIT_S),  # unset env → the audited 420 s default
        ("", GROWW_TOKEN_STALE_WAIT_S),  # blank → default
        ("  ", GROWW_TOKEN_STALE_WAIT_S),  # whitespace → default
        ("abc", GROWW_TOKEN_STALE_WAIT_S),  # non-numeric → default
        ("nan", GROWW_TOKEN_STALE_WAIT_S),  # non-finite would break the bound → default
        ("inf", GROWW_TOKEN_STALE_WAIT_S),  # unbounded pause forbidden → default
        ("-5", GROWW_TOKEN_STALE_WAIT_S),  # negative → default
        ("0", 0.0),  # explicit 0 = the operator's DISABLE lever
        ("300", 300.0),  # a valid override is honoured
    ],
)
def test_resolve_token_stale_wait_seconds(raw: str | None, expected: float) -> None:
    """Bad values can never disable/unbound the pause; 0 is the explicit revert lever."""
    assert resolve_token_stale_wait_seconds(raw) == expected


def test_resolver_reads_env_when_raw_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None`` reads BRUTEX_GROWW_TOKEN_STALE_WAIT_S (the operator escape hatch)."""
    monkeypatch.setenv(GROWW_TOKEN_STALE_WAIT_ENV, "123.5")
    assert resolve_token_stale_wait_seconds() == 123.5


# ─────────────────────────────────────────────────────────────────────────
# await_token_recovery — the bounded, single-flight-aware pause primitive
# ─────────────────────────────────────────────────────────────────────────
def test_pause_adopts_fresh_token_after_outage() -> None:
    """The 06:00 shape: the pause rides out the 331 s mint lag and adopts EXACTLY once.

    A stale re-read starts the wait; the fake Lambda mints at t=331; the waiter's
    leader re-reads adopt the fresh token on the first post-mint tick — one adoption,
    one ``groww.token.reread``, bounded-cadence ``groww.token.stale_wait`` events.
    """
    now, advance = _fake_time()
    reread, served = _lagging_reread(now)
    refresher = _readonly_refresher(reread)
    with pytest.raises(GrowwTokenStillStaleError):
        refresher.reauth(0)  # the initiating still-stale re-read (as the client sees it)
    with capture_logs() as logs:
        generation = refresher.await_token_recovery(
            0,
            wait_seconds=420.0,
            poll_seconds=5.0,
            sleep=advance,
            clock=now,
            method="historical_candles",
        )
    assert generation == 1
    fresh_sdk = refresher.current_sdk()
    assert isinstance(fresh_sdk, _TokenSDK) and fresh_sdk.token == _FRESH
    assert served.count(_FRESH) == 1  # exactly ONE adoption re-read
    assert now() < 420.0  # recovered at the mint (+ one tick), never the full bound
    events = [entry["event"] for entry in logs]
    stale_waits = [entry for entry in logs if entry["event"] == "groww.token.stale_wait"]
    # Bounded cadence: ~67 ticks over 331 s but at most one log per 30 s interval.
    assert 1 <= len(stale_waits) <= 15
    assert stale_waits[0]["deadline_s"] == 420.0
    assert stale_waits[0]["method"] == "historical_candles"
    assert events.count("groww.token.reread") == 1


def test_pause_deadline_expiry_then_fail_fast_reentry() -> None:
    """A never-minting broker: ONE bounded pause, then every re-entry fails FAST.

    The deadline anchor is SHARED per generation, so after the first wait expires a
    ladder re-entry (or a sibling thread) raises immediately with ZERO extra wait and
    ZERO extra broker re-reads — a dead broker degrades to the pre-gate fail shape.
    """
    now, advance = _fake_time()
    reads: list[float] = []

    def always_stale(_creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken:
        reads.append(now())
        return CachedToken(access_token=_STALE, issued_for_session=_SESSION)

    refresher = _readonly_refresher(always_stale)
    with pytest.raises(GrowwTokenStillStaleError) as exc:
        refresher.await_token_recovery(
            0, wait_seconds=60.0, poll_seconds=5.0, sleep=advance, clock=now
        )
    assert is_retryable(exc.value)  # the propagated shape stays the retryable defer
    assert now() == 60.0  # the full bound was waited exactly once
    reads_after_first = len(reads)
    with pytest.raises(GrowwTokenStillStaleError):
        refresher.await_token_recovery(
            0, wait_seconds=60.0, poll_seconds=5.0, sleep=advance, clock=now
        )
    assert now() == 60.0  # ZERO additional wait — the shared anchor fail-fasts
    assert len(reads) == reads_after_first  # …and ZERO additional broker re-reads


def test_zero_wait_disables_pause_pre_gate_behaviour() -> None:
    """``wait_seconds=0`` raises immediately: the explicit operator revert lever."""
    now, advance = _fake_time()
    reads: list[float] = []

    def always_stale(_creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken:
        reads.append(now())
        return CachedToken(access_token=_STALE, issued_for_session=_SESSION)

    refresher = _readonly_refresher(always_stale)
    with pytest.raises(GrowwTokenStillStaleError):
        refresher.await_token_recovery(0, wait_seconds=0.0, sleep=advance, clock=now)
    assert now() == 0.0  # no wait at all
    assert reads == []  # no extra broker read either


def test_default_wait_resolves_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``wait_seconds=None`` (the production client default) resolves the env bound."""
    monkeypatch.setenv(GROWW_TOKEN_STALE_WAIT_ENV, "0")
    now, advance = _fake_time()
    refresher = _readonly_refresher(
        lambda _creds, *, now_utc: CachedToken(access_token=_STALE, issued_for_session=_SESSION)
    )
    with pytest.raises(GrowwTokenStillStaleError):
        refresher.await_token_recovery(0, sleep=advance, clock=now)
    assert now() == 0.0  # env "0" disabled the pause


def test_already_adopted_generation_returns_immediately() -> None:
    """A waiter whose generation already moved rides the sibling's adoption instantly."""
    now, advance = _fake_time()
    reread, served = _lagging_reread(now)
    refresher = _readonly_refresher(reread)
    advance(_OUTAGE_SECONDS)  # the Lambda has minted
    assert refresher.reauth(0) == 1  # a "sibling" adopts the fresh token
    before = now()
    served_before = len(served)
    assert refresher.await_token_recovery(0, wait_seconds=60.0, sleep=advance, clock=now) == 1
    assert now() == before  # zero wait
    assert len(served) == served_before  # zero re-reads — the generation check answered


def test_watchdog_beats_flow_during_pause() -> None:
    """(e) The pause BEATS the progress-beat seam every tick — the stall watchdog stays fed."""
    now, advance = _fake_time()
    reread, _served = _lagging_reread(now)
    refresher = _readonly_refresher(reread)
    advance(_OUTAGE_SECONDS - 25.0)  # 5 ticks from the mint
    beats: list[float] = []
    register_progress_beat(lambda: beats.append(now()))
    try:
        assert (
            refresher.await_token_recovery(
                0, wait_seconds=420.0, poll_seconds=5.0, sleep=advance, clock=now
            )
            == 1
        )
    finally:
        unregister_progress_beat()
    assert len(beats) >= 5  # one liveness beat per tick throughout the pause


def test_single_flight_no_ssm_stampede_across_pool_threads() -> None:
    """(d) N concurrent waiters issue ONE broker re-read per tick — never N.

    8 real threads pause together; the hook serves STALE for the first five reads and
    FRESH on the sixth. The leader lock + the ``reauth`` generation single-flight make
    the total re-read count EXACTLY 6 (5 stale + 1 adopting) — a per-thread poll would
    have issued ~8× that. Every thread adopts the same bumped generation.
    """
    lock = threading.Lock()
    count = {"reads": 0}

    def reread(_creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken:
        with lock:
            count["reads"] += 1
            token = _FRESH if count["reads"] >= 6 else _STALE
        return CachedToken(access_token=token, issued_for_session=_SESSION)

    refresher = _readonly_refresher(reread)
    results: list[int] = []
    results_lock = threading.Lock()

    def worker() -> None:
        generation = refresher.await_token_recovery(
            0, wait_seconds=30.0, poll_seconds=0.01, method="historical_candles"
        )
        with results_lock:
            results.append(generation)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20.0)
    assert not any(thread.is_alive() for thread in threads)
    assert results == [1] * 8  # every waiter adopted the ONE fresh generation
    assert count["reads"] == 6  # 5 stale + 1 fresh — no stampede, no post-adoption reads


# ─────────────────────────────────────────────────────────────────────────
# End-to-end through the REAL client + F&O ingest spine
# ─────────────────────────────────────────────────────────────────────────
class _RecordingSink:
    """Minimal recording BarSink (thread-free — the replay drives contracts serially)."""

    def __init__(self) -> None:
        self.appends: list[tuple[str, int]] = []

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        self.appends.append((symbol, len(bars)))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


def _contract(symbol: str, strike: float) -> DerivativeContract:
    return DerivativeContract(
        underlying_symbol="NIFTY",
        expiry=date(2025, 1, 2),
        symbol=symbol,
        contract_type="CE",
        strike=strike,
    )


def _gated_client(
    refresher: SdkRefresher,
    *,
    now: Callable[[], float],
    advance: Callable[[float], None],
    wait_seconds: float = 420.0,
) -> GrowwClient:
    return GrowwClient(
        sdk=refresher.current_sdk(),
        refresher=refresher,
        sleep=advance,  # the injected sleeper ADVANCES the fake clock — zero real time
        clock=now,
        max_attempts=1,  # the client never fast-retries: only the gate buy-back retries
        token_stale_wait_seconds=wait_seconds,
    )


def test_forensic_outage_replay_zero_failed_windows() -> None:
    """(a)+(f) The 2026-07-04 replay: a 5.5-min token outage burns ZERO window attempts.

    Fail-before shape (same run, pre-gate): 339 failed calls across 115 windows, 111
    settled ledger-FAILED, and every failure fed ``on_timeout`` → the rate ladder's
    ``rate.timeout`` storm drove the deep 1 r/s floor. With the gate: the FIRST fetch
    pauses through the outage, the fresh token is adopted EXACTLY once, every window
    stores its bars, the ledger holds ONLY SUCCESS rows, and the 429/timeout ladder is
    NEVER fed.
    """
    now, advance = _fake_time()
    reread, served = _lagging_reread(now)
    refresher = _readonly_refresher(reread)
    client = _gated_client(refresher, now=now, advance=advance)
    sink = _RecordingSink()
    ledger = WindowLedger()
    on_timeout_calls: list[int] = []
    on_rate_limit_calls: list[int] = []
    contracts = [
        _contract(f"NSE-NIFTY-02Jan25-{strike}-CE", float(strike))
        for strike in (24000, 24100, 24200, 24300, 24400)
    ]
    with capture_logs() as logs:
        reports = [
            ingest_option_contract(
                client,
                sink,
                contract=contract,
                interval="1day",
                start=date(2024, 1, 1),
                end=date(2024, 1, 2),
                ledger=ledger,
                on_timeout=lambda: on_timeout_calls.append(1),
                on_rate_limit=lambda: on_rate_limit_calls.append(1),
                now_ist=lambda: datetime(2024, 1, 10, 8, 0, tzinfo=UTC),
            )
            for contract in contracts
        ]
    # Windows never burned an attempt: every report is a clean single-window success.
    assert all(report.windows == 1 and report.bars_appended == 1 for report in reports)
    assert all(report.transient_failures == 0 for report in reports)
    assert all(report.still_failed_windows == 0 for report in reports)
    # ZERO ledger-FAILED rows (vs 111 in the pre-gate forensic run) — all SUCCESS.
    records = ledger.records()
    assert len(records) == 5
    assert {record.status for record in records} == {SUCCESS}
    assert [n for _sym, n in sink.appends] == [1] * 5  # all bars stored
    # The 429/timeout ladder was NEVER fed (the 339 rate.timeout events are gone).
    assert on_timeout_calls == []
    assert on_rate_limit_calls == []
    # Exactly ONE adoption for the whole pool/outage.
    assert refresher.current_generation() == 1
    assert served.count(_FRESH) == 1
    events = [entry["event"] for entry in logs]
    assert "groww.token.stale_wait" in events  # the pause is operator-visible
    assert events.count("groww.token.reread") == 1  # the one adoption, logged
    assert events.count("option.ingest.window.transient") == 0  # nothing hit the ladder
    assert _OUTAGE_SECONDS <= now() < 420.0  # one bounded pause, then straight through


def test_never_minting_broker_window_defers_failed_as_today() -> None:
    """(b) The deadline-expiry fallback: the window defers FAILED exactly as pre-gate.

    A broker that NEVER mints: the first attempt pays the ONE bounded pause; the
    ladder's remaining attempts fail fast (shared anchor); the window records FAILED
    (re-pullable — the until-clean loop stays bounded). No infinite hang: total fake
    wall-clock = ONE pause bound. ``on_timeout`` is NEVER fed — the propagating
    ``GrowwTokenStillStaleError`` is a retryable AUTH-family failure, which
    ``steps_timeout_ladder`` excludes from the AIMD timeout ladder
    (DEC-AUTH-LADDER-EXCLUDE-001 supersedes the pre-gate "fed exactly as today"
    expectation this test originally pinned: credential state is no evidence of
    candle-endpoint stress, so it must not ease the pull rate).
    """
    now, advance = _fake_time()

    def always_stale(_creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken:
        return CachedToken(access_token=_STALE, issued_for_session=_SESSION)

    refresher = _readonly_refresher(always_stale)
    client = _gated_client(refresher, now=now, advance=advance, wait_seconds=30.0)
    sink = _RecordingSink()
    ledger = WindowLedger()
    on_timeout_calls: list[int] = []
    report = ingest_option_contract(
        client,
        sink,
        contract=_contract("NSE-NIFTY-02Jan25-24000-CE", 24000.0),
        interval="1day",
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
        ledger=ledger,
        on_timeout=lambda: on_timeout_calls.append(1),
        backoff=lambda _attempt: None,
        max_attempts=2,  # the production _DEFAULT_MAX_ATTEMPTS ladder (3 attempts)
        now_ist=lambda: datetime(2024, 1, 10, 8, 0, tzinfo=UTC),
    )
    assert report.windows == 1 and report.bars_appended == 0
    assert report.transient_failures == 1 and report.still_failed_windows == 1
    records = ledger.records()
    assert len(records) == 1 and records[0].status == FAILED  # deferred, re-pullable
    assert records[0].last_error == "GrowwTokenStillStaleError"
    # Auth-family transients feed NEITHER controller hook (DEC-AUTH-LADDER-EXCLUDE-001):
    # the window still burns its ladder attempts + defers FAILED (asserted above), but
    # the AIMD timeout ladder is never mis-trained by pure credential state.
    assert on_timeout_calls == []
    assert sink.appends == []  # nothing fabricated
    assert now() == pytest.approx(30.0)  # ONE bounded pause total — re-entries fail fast


def test_terminal_auth_stays_terminal_no_pause() -> None:
    """(c) A genuinely revoked credential is untouched: terminal, and the gate never engages."""
    mints = {"n": 0}

    def token_factory(_api_key: str, _totp: str) -> str:
        mints["n"] += 1
        if mints["n"] == 1:
            return _STALE  # the cold-start mint (the SDK will reject every call)
        raise RuntimeError("credential revoked by vendor")

    refresher = SdkRefresher(
        _CREDS,
        token_factory=token_factory,
        totp_minter=lambda _secret: "123456",
        sdk_factory=_TokenSDK,
        token_cache_load=lambda *_a, **_k: None,
        token_cache_store=lambda *_a, **_k: True,
        call_timeout_seconds=5.0,
    )
    now, advance = _fake_time()
    client = _gated_client(refresher, now=now, advance=advance)
    with capture_logs() as logs, pytest.raises(GrowwAuthError) as exc:
        client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert not is_retryable(exc.value)  # TERMINAL — is_terminal_auth_failure semantics kept
    assert not isinstance(exc.value, GrowwTokenStillStaleError)
    assert now() == 0.0  # the pause never engaged — no wait loop for a dead credential
    assert all(entry["event"] != "groww.token.stale_wait" for entry in logs)


def test_local_mint_mode_never_double_waits() -> None:
    """(5) LOCAL-mint mode: the synchronous re-mint IS the recovery — the gate never engages.

    A non-read-only refresher (``readonly_reread=None``) re-mints inside ``reauth`` and
    never raises the StillStale shape, so the client pays only the fixed re-auth
    propagation beat — zero ``groww.token.stale_wait`` events, zero pause ticks.
    """
    minted: list[str] = []

    def token_factory(_api_key: str, _totp: str) -> str:
        token = _STALE if not minted else _FRESH  # cold-start dead token, re-mint fresh
        minted.append(token)
        return token

    refresher = SdkRefresher(
        _CREDS,
        token_factory=token_factory,
        totp_minter=lambda _secret: "123456",
        sdk_factory=_TokenSDK,
        token_cache_load=lambda *_a, **_k: None,
        token_cache_store=lambda *_a, **_k: True,
        call_timeout_seconds=5.0,
    )
    now, _advance = _fake_time()
    slept: list[float] = []
    client = GrowwClient(
        sdk=refresher.current_sdk(),
        refresher=refresher,
        sleep=slept.append,
        clock=now,
        max_attempts=1,
        token_stale_wait_seconds=420.0,
    )
    with capture_logs() as logs:
        out = client.expiries(exchange="NSE", underlying_symbol="NIFTY")
    assert out == {"expiries": ["2026-07-30"]}
    assert refresher.current_generation() == 1  # ONE synchronous re-mint healed it
    assert minted == [_STALE, _FRESH]
    assert slept == [REAUTH_RETRY_BACKOFF_SECONDS]  # only the fixed re-auth beat — no pause
    assert all(entry["event"] != "groww.token.stale_wait" for entry in logs)
