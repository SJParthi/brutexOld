"""Regression tests — the AIMD ladder sees EVERY client-absorbed 429 (DEC-SUSPICION-SWEEP-FIXES-001).

D1 (the spot blindness, MED): the spot build sites (``dashboard/pull_controller``
+ ``cli``) bind NO ``on_rate_limit`` / ``on_success`` into the Groww client, and
``run_ingest.with_paced_client`` rebound ONLY ``pace`` — so the client's bounded
fast-retry absorbed 429s against a NO-OP decrement hook and the rate ladder saw a
429 only after the whole ``max_attempts`` budget was exhausted (~3× the designed
vendor rejections per step-down; a sub-exhaustion 429 burst was INVISIBLE). The
fix binds the FULL hook triple at the ONE COMMON spine seam every production spot
pull flows through (``run_groww_spot_ingest`` → ``with_paced_client``).

D2 (the F&O double count, LOW): the client fires ``on_rate_limit`` on EVERY 429
occurrence INCLUDING the final one before the exhausted error propagates; the
spine then fired its own hook again on the propagated error — 7 ladder hits per
exhausted window instead of 6. The fix stands the spine's hook down when the
client self-reports (the feedback twin of the ``client_self_paces`` stand-down).

PRODUCTION-WIRING pins: these tests monkeypatch NO builders — they drive the REAL
spines over the REAL ``GrowwClient``/``GrowwMarketAdapter`` (fake SDK only) with
the exact hook threading the production call sites use, so a wiring regression
(e.g. reverting ``with_paced_client`` to pace-only) fails them. The existing
wiring tests monkeypatched the builders away and structurally could not catch D1.

Rate CAPS untouched: everything here wires FEEDBACK hooks — never the ceiling /
governor values (CLAUDE.md §2 Rule 6).
"""

# Shared single-admission fakes imported cross-test-module — sanctioned.
# pyright: reportPrivateUsage=none

from __future__ import annotations

import threading
from datetime import date

from brutex.adapters.groww.adapter import GrowwMarketAdapter
from brutex.adapters.groww.client import (
    GrowwClient,
    client_self_reports_rate_limits,
    client_self_reports_successes,
)
from brutex.adapters.groww.option_ingest import ingest_option_contract
from brutex.adapters.groww.pool import ClientPool
from brutex.adapters.groww.run_ingest import run_groww_spot_ingest, with_paced_client
from brutex.orchestrator.parallel import SerialRunParallel

from tests.unit.test_groww_single_admission import (
    _INDEX,
    _Admissions,
    _ce,
    _FakeSDK,
    _nifty,
    _Sink,
)


class GrowwAPIRateLimitException(Exception):
    """SDK-named exception fake — ``from_groww_exception`` maps it by class name
    to the retryable ``GrowwRateLimitError`` (mirrors tests/unit/test_groww_client.py)."""


class _Hits:
    """Thread-safe counting hooks for ``on_rate_limit`` / ``on_success``."""

    def __init__(self) -> None:
        self.rate_limits = 0
        self.successes = 0
        self._lock = threading.Lock()

    def on_rate_limit(self) -> None:
        with self._lock:
            self.rate_limits += 1

    def on_success(self) -> None:
        with self._lock:
            self.successes += 1


def _spot_adapter(sdk: _FakeSDK, *, max_attempts: int = 6) -> GrowwMarketAdapter:
    """A REAL adapter over a REAL (unhooked) client — exactly what the spot build
    sites produce (they bind NO controller hooks; the spine seam binds them)."""
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=max_attempts)
    return GrowwMarketAdapter(client=client, index=_INDEX)


def _run_spot(adapter: GrowwMarketAdapter, pace: _Admissions, hits: _Hits) -> None:
    """Drive ONE (NIFTY, 1day) pair over one past window through the REAL spot
    spine with the EXACT hook threading the production call sites use
    (``pull_all_spot`` / ``cmd_fetch`` → ``run_groww_spot_ingest_with_retry`` →
    ``run_groww_spot_ingest``)."""
    run_groww_spot_ingest(
        adapter,
        _Sink(),
        instruments=[_nifty()],
        intervals=["1day"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 3),
        pace=pace.pace,
        on_rate_limit=hits.on_rate_limit,
        on_success=hits.on_success,
        run_parallel=SerialRunParallel(),
    )


class TestSpotLadderSeesAbsorbed429s:
    """D1 — the production spot wiring feeds the ladder per 429 OCCURRENCE."""

    def test_sub_exhaustion_429_burst_reaches_the_ladder(self) -> None:
        """FAILS-BEFORE pin: two 429s absorbed by the client's fast-retry (the
        window ultimately succeeds — the error NEVER propagates to the spine)
        must STILL decrement the ladder twice. Pre-fix the client's hooks were
        no-ops on the spot path, so this burst was structurally invisible."""
        sdk = _FakeSDK(
            script=[GrowwAPIRateLimitException("429"), GrowwAPIRateLimitException("429"), None]
        )
        pace, hits = _Admissions(), _Hits()
        _run_spot(_spot_adapter(sdk), pace, hits)
        assert sdk.calls == 3  # 2 rejected attempts + the clean one
        assert hits.rate_limits == 2  # EVERY vendor 429 occurrence fed the ladder
        assert hits.successes >= 1  # the clean call recovered the rate
        assert pace.count == sdk.calls  # single admission preserved (no regression)

    def test_exhausted_window_feeds_exactly_one_hit_per_occurrence(self) -> None:
        """The spot twin of D2: an EXHAUSTED window's propagated error must not
        re-count the final occurrence. With ``max_attempts=2`` and a first-pass
        429+429 exhaust then an in-run-retry success, the ladder sees EXACTLY
        the 2 vendor rejections — not 3 (spine double count) and not 0 (D1)."""
        sdk = _FakeSDK(
            script=[GrowwAPIRateLimitException("429"), GrowwAPIRateLimitException("429"), None]
        )
        pace, hits = _Admissions(), _Hits()
        _run_spot(_spot_adapter(sdk, max_attempts=2), pace, hits)
        assert sdk.calls == 3  # pass 1: 2 rejections (budget exhausted); retry pass: clean
        assert hits.rate_limits == 2  # one hit per vendor 429 — never the +1 double count

    def test_unhooked_fake_adapter_keeps_spine_fallback(self) -> None:
        """A non-Groww adapter (no rebind possible) keeps the spine-level hooks:
        the propagated 429 still feeds the ladder once per exhausted window —
        the pre-existing fallback contract, unchanged."""
        from collections.abc import Sequence
        from datetime import datetime

        from brutex.adapters.groww.errors import GrowwRateLimitError
        from brutex.data.models import CandleBar

        class _Raises429Adapter:
            calls = 0

            def fetch_ohlcv(self, **_kw: object) -> Sequence[CandleBar]:
                type(self).calls += 1
                raise GrowwRateLimitError("429 storm")

            def list_instruments(self, *, cursor: str | None = None) -> object:
                raise NotImplementedError

            def list_derivatives(self, **_kw: object) -> object:
                raise NotImplementedError

            @property
            def capabilities(self) -> object:
                raise NotImplementedError

        del datetime  # imported for symmetry with sibling helpers; unused here
        pace, hits = _Admissions(), _Hits()
        run_groww_spot_ingest(
            _Raises429Adapter(),  # type: ignore[arg-type]  # protocol-shaped fake
            _Sink(),
            instruments=[_nifty()],
            intervals=["1day"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 3),
            pace=pace.pace,
            on_rate_limit=hits.on_rate_limit,
            on_success=hits.on_success,
            run_parallel=SerialRunParallel(),
        )
        # Every spine-level retry pass of the one window fed the ladder once —
        # the fallback path still reports (non-zero); the exact count is the
        # spine's bounded in-run retry ladder, not this test's contract.
        assert hits.rate_limits == _Raises429Adapter.calls
        assert hits.rate_limits > 0


class TestFnoNoDoubleCount:
    """D2 — the F&O spine never re-counts the client-reported final occurrence."""

    def test_exhausted_429_window_counts_client_occurrences_only(self) -> None:
        """FAILS-BEFORE pin (combined client+spine count — none existed): a live
        client (hooks baked in, exactly like ``build_live_client``) exhausting
        ``max_attempts=2`` on 429s propagates the error to the spine; the spine
        must NOT fire the shared hook again — 2 ladder hits, not 3."""
        hits = _Hits()
        sdk = _FakeSDK(
            script=[GrowwAPIRateLimitException("429"), GrowwAPIRateLimitException("429")]
        )
        client = GrowwClient(
            sdk=sdk,
            sleep=lambda _s: None,
            max_attempts=2,
            on_rate_limit=hits.on_rate_limit,
            on_success=hits.on_success,
        )
        report = ingest_option_contract(
            client,
            _Sink(),
            contract=_ce(),
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            # Production threading (run_groww_option_ingest): the SAME controller
            # hooks reach the spine level too — the double-count surface.
            on_rate_limit=hits.on_rate_limit,
            on_success=hits.on_success,
            max_attempts=0,  # no spine-level re-pass: isolate ONE exhausted window
        )
        assert sdk.calls == 2
        assert hits.rate_limits == 2  # pre-fix: 3 (client 2 + spine 1 on the propagated error)
        assert report.transient_failures == 1

    def test_clean_window_recovery_not_double_credited(self) -> None:
        """A clean window on a self-hooked client credits recovery ONCE (per
        physical call, by the client) — the spine's per-window credit stands down."""
        hits = _Hits()
        client = GrowwClient(sdk=_FakeSDK(), sleep=lambda _s: None, on_success=hits.on_success)
        report = ingest_option_contract(
            client,
            _Sink(),
            contract=_ce(),
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            on_success=hits.on_success,
        )
        assert report.bars_appended == 1
        assert hits.successes == 1  # pre-fix: 2 (client per-call + spine per-window)

    def test_unhooked_client_keeps_spine_feedback(self) -> None:
        """An UNhooked client (dry-run / test fakes) keeps the spine-level hooks —
        the propagated exhausted 429 feeds the ladder once (the fallback path)."""
        hits = _Hits()
        sdk = _FakeSDK(script=[GrowwAPIRateLimitException("429")])
        client = GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)
        ingest_option_contract(
            client,
            _Sink(),
            contract=_ce(),
            interval="1day",
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            on_rate_limit=hits.on_rate_limit,
            on_success=hits.on_success,
            max_attempts=0,
        )
        assert hits.rate_limits == 1  # spine fallback still reports


class TestRateHookRebindSeam:
    """The ``with_rate_hooks`` seam + the self-reports predicates (whitebox)."""

    def test_with_paced_client_binds_full_hook_triple(self) -> None:
        adapter = _spot_adapter(_FakeSDK())
        hits = _Hits()
        pace = _Admissions()
        bound = with_paced_client(adapter, pace.pace, hits.on_rate_limit, hits.on_success)
        assert isinstance(bound, GrowwMarketAdapter)
        assert bound.client.self_paces is True  # type: ignore[union-attr]
        assert client_self_reports_rate_limits(bound.client)
        assert client_self_reports_successes(bound.client)

    def test_with_paced_client_pace_only_keeps_pre_d1_shape(self) -> None:
        """A pace-only rebind (no feedback hooks supplied) binds pace and leaves
        the no-op feedback defaults — the pre-D1 caller contract, byte-preserved."""
        adapter = _spot_adapter(_FakeSDK())
        bound = with_paced_client(adapter, _Admissions().pace)
        assert isinstance(bound, GrowwMarketAdapter)
        assert bound.client.self_paces is True  # type: ignore[union-attr]
        assert not client_self_reports_rate_limits(bound.client)
        assert not client_self_reports_successes(bound.client)

    def test_with_paced_client_none_pace_and_foreign_adapter_no_op(self) -> None:
        adapter = _spot_adapter(_FakeSDK())
        hits = _Hits()
        assert with_paced_client(adapter, None, hits.on_rate_limit, hits.on_success) is adapter
        sentinel = object()
        assert with_paced_client(sentinel, _Admissions().pace) is sentinel  # type: ignore[arg-type]

    def test_client_with_rate_hooks_none_keeps_current_binding(self) -> None:
        hits = _Hits()
        rate_hook, success_hook = hits.on_rate_limit, hits.on_success
        client = GrowwClient(sdk=_FakeSDK(), on_rate_limit=rate_hook)
        rebound = client.with_rate_hooks(on_success=success_hook)
        assert rebound.on_rate_limit is rate_hook  # None kept the binding
        assert rebound.on_success is success_hook
        assert rebound.self_paces is False  # pace untouched (still the no-op)

    def test_pool_predicates_and_rebind_fan_to_every_leaf(self) -> None:
        from brutex.adapters.call_meter import CallMeter

        hits = _Hits()
        meter = CallMeter()  # pool leaves must share ONE aggregate meter
        pool = ClientPool(
            [GrowwClient(sdk=_FakeSDK(), meter=meter), GrowwClient(sdk=_FakeSDK(), meter=meter)],
        )
        assert pool.self_reports_rate_limits is False
        assert pool.self_reports_successes is False
        rebound = pool.with_rate_hooks(
            pace=_Admissions().pace,
            on_rate_limit=hits.on_rate_limit,
            on_success=hits.on_success,
        )
        assert rebound.self_paces is True
        assert rebound.self_reports_rate_limits is True
        assert rebound.self_reports_successes is True
        # A pool with ONE unhooked leaf must read False (all-leaves rule).
        mixed = ClientPool(
            [
                GrowwClient(sdk=_FakeSDK(), on_rate_limit=hits.on_rate_limit, meter=meter),
                GrowwClient(sdk=_FakeSDK(), meter=meter),
            ]
        )
        assert mixed.self_reports_rate_limits is False

    def test_predicates_getattr_safe(self) -> None:
        assert client_self_reports_rate_limits(None) is False
        assert client_self_reports_successes(object()) is False
