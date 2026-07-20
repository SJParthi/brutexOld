"""Deep-verification hardening pins for ``/movers`` (the 2026-07-13 audit upstream).

Curated regression pins from the independent-oracle ``/movers`` audit run against
main tip ``8105234`` (workers A rankings · B as-of · C tiers · D contiguity ·
E filters + the F tip re-verify). The audit found ZERO new correctness defects —
these tests upstream the audit's independent-oracle checks that were NOT already
pinned by the shipped suites, so a future regression fails a named test instead
of a re-audit. House rules throughout: real lakes via the production
:class:`~brutex.data.parquet_writer.ParquetWriter` ONLY, routes (where a test
drives a route — three test functions deliberately drive
``run_movers_snapshot`` / ``compute_spot_candidates`` directly) via
:func:`~brutex.dashboard.app.build_app` + ``TestClient``, and every expected
number recomputed by a hand-rolled INTEGER-paisa / :class:`fractions.Fraction`
oracle inside the test (no production helper computes an expected value).

Pinned here (each verified ABSENT from the existing movers suites first):

* the exact ``x.xx5``-crore ROUND_HALF_UP boundary of the wire ``value_cr``
  string (banker's rounding would serve ``"1.50"`` for 1.505 crore);
* integer-exact "Top Value" RANKING beyond ``2**53`` paisa (two symbols whose
  turnovers float64-COLLAPSE to the same double must still order by the exact
  int — the ``test_surface_trial_results`` teeth applied to ``/movers``);
* the exact ``±0.005`` %CHNG half-boundaries (positive AND negative twins)
  surviving to the wire full-precision (a premature server-side 2dp rounding
  would serve ``0.01`` / ``0.00`` / ``-0.01``);
* as-of SUFFIX-INDEPENDENCE — mutating every bar AFTER the resolved cutoff
  leaves the served rows byte-identical (no look-ahead through the cut), on the
  1-minute identity cut, a non-anchor coarse-TF cut AND a #1601
  anchor-resolved-forward cut;
* the futures Near/Next/Far tier cache-INDEPENDENCE size law over an injected
  cache (one NEW entry per tier — ``[1, 2, 3]`` — and a repeat-Near hit that
  neither grows the cache nor serves a stale wrong-tier row);
* the multi-row gainers/losers ORDER law on ALL THREE tabs (spot / futures /
  options): ≥3 candidates per side with distinct %CHNG magnitudes serve
  ``price_gainers`` DESCENDING (largest gain first) and ``price_losers``
  ASCENDING (most negative first). HONEST teeth provenance (refuter round 2
  — corrects the round-1 over-generalised "any tab's flip survived the
  battery" claim): the genuinely UNPINNED repo gaps were EXACTLY spot
  gainers, spot losers and futures LOSERS — the SPOT flips empirically
  survived R1's 859-passed ``-k movers`` battery snapshot
  (R1-teeth-refuter F1, a spot-scoped measurement; the count is that run's
  passed-count, not a stable selection size) and the futures-losers flip
  survived every pre-existing test on round-2 re-measurement; futures
  GAINERS and options BOTH directions were ALREADY killed by shipped
  multi-row UNIT order pins (``tests/unit/test_movers_futures.py::
  TestRanking::test_top_n_caps_and_orders_descending``;
  ``tests/unit/test_movers_options.py::TestRankers::
  test_price_gainers_positive_desc`` / ``test_price_losers_negative_asc``),
  so for those three list-directions the pins here are ROUTE-level
  complements — real lake → candidate compute → payload → wire, plus exact
  wire-pct :class:`fractions.Fraction` oracles — to the ``_mover()``-stub
  unit pins (the same unit-vs-route differentiation the min-volume equality
  trio uses);
* the noncontiguous-WARN cap END-TO-END through the real
  ``compute_spot_candidates`` loop (25 holed symbols → exactly 20 WARNs + ONE
  suppressed summary, every row still stamped — the shipped cap test drives
  ``_NoncontiguousWarnState`` directly, never the loop);
* the ``min_volume == volume`` EQUALITY boundary (``>=`` — the floor keeps the
  exactly-at-floor row) at ROUTE level on all three tabs (the shipped
  ROUTE-level floor pins use strictly-above/below values; only the isolated
  ``value_meets_volume_floor`` unit predicate pins equality, never the route
  wiring);
* default-``date`` and explicit-same-date polls share ONE candidate-cache
  entry (the date is canonicalised BEFORE keying — a dual-entry regression
  would recompute and see un-signalled writes);
* a genuine CODE bug (``KeyError``) inside the per-instrument loop PROPAGATES
  to an HTTP 500 — never swallowed into the narrow corrupt-file ``skipped``
  arm (DEC-MOVERS-CORRUPT-SKIP-001's honest catch set);
* a CORRUPT (garbage-bytes) ``registry.duckdb`` through the LIVE options route:
  HTTP 200 + lake-derived tier rows + the ``movers.options.registry_unreadable``
  WARN (the shipped unit test mocks ``RegistryError`` — this drives the real
  DuckDB open over real garbage bytes end-to-end).

CURATION CORRECTION (refuter round 1): an earlier revision of this file
carried a ``TestOiOnlyContiguitySplit`` pin whose docstring — and this module
docstring — claimed "the shipped futures golden covers only the both-False
arm". That claim was FALSE: the OI-only contiguity split (close contiguous +
OI baseline non-contiguous, exactly one ``kind="oi"`` WARN, served values
untouched) was ALREADY pinned on the identical fixture dates and law by
``tests/unit/test_movers_futures.py:1414``
(``TestResolvePrevOi::test_null_oi_1day_divergence_close_true_oi_false``,
which drives the production ``_futures_row`` projector). The duplicate class
was DELETED under the zero-duplication rule; that unit test is the covering
pin.

DELIBERATELY NOT PINNED (owned by in-flight work — nothing here asserts the
current stale-stored serve): the stored-partition freshness class of
DEC-MOVERS-STORED-FRESHNESS-001 / DEC-MOVERS-DAILY-FRESHNESS-001 (PRs #1511 +
#1358). No test below touches ``_read_intraday_bars`` / ``_read_cut_1m_frame``
freshness territory or ``templates/movers.html``.

MERGED-SCREEN CONSTRAINT (PR #1462, MERGED to main — squash ``70d44a3``,
DEC-DASHBOARD-READ-SCREEN-001, MIGRATION.md #1759 — the dashboard bad-tick
READ screen, ``brutex/data/bar_screen.py`` + ``movers_snapshot.screen_frame``):
that lane SKIPS at read time any bar whose close is ``>= 4x`` or ``<= 1/4x``
the previous in-session KEPT close (first bar of a session exempt). It is a
MERGED production constraint this file's fixtures respect BY DESIGN: every
fixture keeps ALL adjacent in-session closes within the (1/4x, 4x) band — or
uses single-bar sessions (first-bar exempt) — so all 19 tests pass
identically with and without the bad-tick arm (round-2 merge-verified against
the #1462 head; round 3 rebased this branch directly onto the merged main).
The DURABLE design rules — not any PR inventory — are the contract this
file relies on: (a) every fixture keeps all adjacent in-session closes
inside the (1/4x, 4x) merged-screen band or uses single-bar sessions, and
(b) no test pins stored-partition freshness territory (the DELIBERATELY
NOT PINNED class above) — so a semantics-preserving change on either
front has no fixture-level reason to break these 19 tests, while a merge
that genuinely breaks the movers read path fails them LOUDLY (the fence
working as designed). Any in-flight-PR compatibility assessment is a
dated SNAPSHOT of the then-open PR set: in-flight sets drift, and each
such assessment must be re-verified at each merge, never read as a
standing guarantee. Snapshot 2026-07-13 (R4c, on the post-#1462 rebase):
the then-open relevant PRs were #1511 + #1358 (the not-pinned freshness
owners) and #1399 — which carries a ``bad_tick``/``BadTickScreen`` API
rework (DEC-BADTICK-HEAD-SEED-001) whose merge will need a main-side
``bar_screen`` consumer reconcile; until then a naive merge breaks
main's own ``bar_screen`` consumer call site and these route-level pins
fail loudly alongside main's own suites — intended fence behavior, not
a defect of this file.

Skipped when ``fastapi`` / ``duckdb`` / ``polars`` / ``pyarrow`` are absent.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("polars")
pytest.importorskip("pyarrow")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.dashboard.movers_snapshot import compute_spot_candidates, run_movers_snapshot
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

_EXCHANGE = "NSE"
_DAY = date(2024, 6, 13)


# ── seeding helpers (the sibling movers files' idiom, self-contained) ──────


def _min_bar(
    *,
    hour: int,
    minute: int,
    close: str,
    volume: int,
    oi: int | None = None,
    day: date = _DAY,
) -> CandleBar:
    """A flat 1-minute bar at ``HH:MM`` **IST** on *day* (IST = UTC + 05:30)."""
    c = Decimal(close)
    total = hour * 60 + minute - (5 * 60 + 30)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, total // 60, total % 60, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=oi,
    )


def _day_bar(*, day: date, close: str, volume: int = 1, oi: int | None = None) -> CandleBar:
    """A flat 1-day OHLCV bar labelled at 09:15 IST (03:45 UTC) on *day*."""
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        open_interest=oi,
    )


def _write(
    lake: Path, *, symbol: str, interval: str, bars: list[CandleBar], segment: str = "INDEX"
) -> None:
    ParquetWriter(lake_root=lake).write(
        exchange=_EXCHANGE, segment=segment, symbol=symbol, interval=interval, bars=bars
    )


def _idx(symbol: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        # Deterministic id — zlib.crc32 is PYTHONHASHSEED-independent (hash() is not).
        security_id=zlib.crc32(symbol.encode()) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    # store.path is tmp_path/instruments.duckdb → the route's lake root is tmp_path.
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many([_idx("NIFTY")], snapshot_date=_DAY, run_id="r1", csv_source_sha256="0" * 64)
    yield s
    s.close()


# ── independent oracles (hand-rolled; NEVER a production helper) ───────────


def _crore_str_half_up(paisa: int) -> str:
    """Integer ROUND_HALF_UP paisa → 2dp ₹-crore string (1 crore = 1e9 paisa).

    ``paisa / 1e7`` gives crore-hundredths; a remainder of half-or-more rounds
    the hundredth UP (never banker's) — the independent twin of the production
    ``value_crore_str`` law, rebuilt from pure int arithmetic.
    """
    hundredths, rem = divmod(paisa, 10_000_000)
    if 2 * rem >= 10_000_000:
        hundredths += 1
    return f"{hundredths // 100}.{hundredths % 100:02d}"


# ── wire-cell exactness boundaries (audit worker A) ────────────────────────


class TestValueWireExactness:
    def test_crore_half_up_display_boundary(self, store: InstrumentStore, tmp_path: Path) -> None:
        # An EXACT x.xx5-crore turnover: one flat 1-minute bar close 15050.00 ×
        # volume 1000 → HLC3 == close → 1_505_000 paisa × 1000 = 1_505_000_000
        # paisa = 1.505 crore. ROUND_HALF_UP (DEC-MONEY-2DP-001) must serve
        # "1.51"; banker's rounding (ROUND_HALF_EVEN) would serve "1.50".
        _write(
            tmp_path,
            symbol="HALFCR",
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="15050.00", volume=1000)],
        )
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "spot", "interval": "1minute", "date": _DAY.isoformat()},
        ).json()
        (row,) = [r for r in body["value"] if r["symbol"] == "HALFCR"]
        expected_paisa = 1_505_000 * 1000  # the hand-derived exact half-boundary
        assert expected_paisa == 1_505_000_000
        assert row["value_cr"] == _crore_str_half_up(expected_paisa) == "1.51"

    def test_big_value_ranking_is_integer_exact_beyond_float(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # Two symbols whose turnovers differ by ONE paisa in the float64
        # spacing-4 region (~1.98e16 paisa, well past 2**53): a float-based
        # comparator COLLAPSES them to the same double, ties, and the
        # (symbol, exchange, segment) tie-break would rank BIGA first — the
        # exact-int ranking must put BIGB (v + 1) first.
        #
        # Fixture design (round 2 — the PR #1462 read-screen rule in the module
        # docstring): the paired second bars keep every adjacent-close ratio
        # INSIDE the (1/4x, 4x) bad-tick band (99000.03 → 90000.00 / 90000.01
        # ≈ 0.909x), so the +1 paisa differentiator never rides a screenable
        # spike. big_v = 9_900_003 paisa × 2e9 + 9_000_000 paisa × 1 (both
        # snapped-HLC3 × volume integer products, per bar).
        big_v = 9_900_003 * 2_000_000_000 + 9_000_000 * 1
        assert big_v > 2**53
        assert float(big_v) == float(big_v + 1)  # the float64 collapse this pins
        _write(
            tmp_path,
            symbol="BIGA",
            interval="1minute",
            bars=[
                _min_bar(hour=9, minute=15, close="99000.03", volume=2_000_000_000),
                # 9_000_000 paisa of turnover: close 90000.00 × volume 1.
                _min_bar(hour=9, minute=16, close="90000.00", volume=1),
            ],
        )
        _write(
            tmp_path,
            symbol="BIGB",
            interval="1minute",
            bars=[
                _min_bar(hour=9, minute=15, close="99000.03", volume=2_000_000_000),
                # +1 paisa vs BIGA: close 90000.01 × volume 1 (HLC3 == close).
                _min_bar(hour=9, minute=16, close="90000.01", volume=1),
            ],
        )
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "spot", "interval": "1minute", "date": _DAY.isoformat()},
        ).json()
        # Integer-exact descending value order: v+1 strictly ABOVE v.
        assert [r["symbol"] for r in body["value"]] == ["BIGB", "BIGA"]
        # Both crore strings equal the independent integer half-up render.
        assert [r["value_cr"] for r in body["value"]] == [
            _crore_str_half_up(big_v + 1),
            _crore_str_half_up(big_v),
        ]

    def test_pct_change_exact_half_boundary_survives_to_wire(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # prev close 20000 → ltp 20001 is EXACTLY +0.005% = Fraction(1, 200).
        # The wire pct_change is full-precision (str(Decimal)) — a premature
        # server-side 2dp money-rounding would serve "0.01" or "0.00" and a
        # float pathway would serve 0.005000000000000000104…; 2024-06-12 (Wed)
        # → 2024-06-13 (Thu) is calendar-contiguous, so no stamp keys appear.
        _write(
            tmp_path,
            symbol="PCTB",
            interval="1day",
            bars=[
                _day_bar(day=date(2024, 6, 12), close="20000", volume=5),
                _day_bar(day=_DAY, close="20001", volume=5),
            ],
        )
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "spot", "interval": "1day", "date": _DAY.isoformat()},
        ).json()
        (row,) = [r for r in body["price_gainers"] if r["symbol"] == "PCTB"]
        # The independent oracle: (20001 − 20000) / 20000 × 100 as an exact rational.
        assert Fraction(row["pct_change"]) == Fraction(20001 - 20000, 20000) * 100
        assert Fraction(row["pct_change"]) == Fraction(1, 200)
        assert Decimal(row["pct_change"]) == Decimal("0.005")

    def test_pct_change_exact_negative_half_boundary_survives_to_wire(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # The NEGATIVE twin of the +0.005 pin (symmetry coverage): prev close
        # 20000 → ltp 19999 is EXACTLY −0.005% = Fraction(-1, 200). The wire
        # pct_change stays full-precision AND negative — a premature 2dp
        # money-rounding would serve "-0.01" / "0.00", and a sign-dropping
        # abs() pathway would serve the positive twin's value.
        _write(
            tmp_path,
            symbol="PCTN",
            interval="1day",
            bars=[
                _day_bar(day=date(2024, 6, 12), close="20000", volume=5),
                _day_bar(day=_DAY, close="19999", volume=5),
            ],
        )
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "spot", "interval": "1day", "date": _DAY.isoformat()},
        ).json()
        (row,) = [r for r in body["price_losers"] if r["symbol"] == "PCTN"]
        # The independent oracle: (19999 − 20000) / 20000 × 100 as an exact rational.
        assert Fraction(row["pct_change"]) == Fraction(19999 - 20000, 20000) * 100
        assert Fraction(row["pct_change"]) == Fraction(-1, 200)
        assert Decimal(row["pct_change"]) == Decimal("-0.005")
        assert row["pct_change"].startswith("-")  # the sign survives on the wire


# ── as-of suffix independence (audit worker B, LAW 3) ──────────────────────


def _suffix_session(*, mutate_after: int | None) -> list[CandleBar]:
    """Ten 1-minute NIFTY bars 09:15–09:24 (close 100+i, volume 10+i).

    With *mutate_after* = 4, every bar STRICTLY AFTER 09:19 (indices 5..9) is
    perturbed (+150 close, +7 volume) — the divergent suffix. The +150 step
    (104 → 255 at the boundary, ≈ 2.45x) stays INSIDE the PR #1462 (1/4x, 4x)
    bad-tick band (the module-docstring fixture rule), so the divergence
    survives a read screen. ``None`` keeps the reference session.
    """
    bars: list[CandleBar] = []
    for i in range(10):
        close = 100 + i
        volume = 10 + i
        if mutate_after is not None and i > mutate_after:
            close += 150
            volume += 7
        bars.append(_min_bar(hour=9, minute=15 + i, close=str(close), volume=volume))
    return bars


def _seed_suffix_lake(lake: Path, *, mutate_after: int | None) -> None:
    _write(
        lake, symbol="NIFTY", interval="1minute", bars=_suffix_session(mutate_after=mutate_after)
    )
    # An identical prior-day 1-day close in both lakes (the %CHNG baseline).
    _write(
        lake,
        symbol="NIFTY",
        interval="1day",
        bars=[_day_bar(day=date(2024, 6, 12), close="100", volume=3)],
    )


class TestAsOfSuffixIndependence:
    """Bars AFTER the resolved as-of cutoff must be INVISIBLE (no look-ahead).

    Two real lakes identical through 09:19 and divergent after it: every served
    row at a cutoff resolving to 09:19 must be byte-identical across the lakes,
    while the whole-day rows genuinely differ (the anti-vacuity control). The
    three cut shapes: TF=1-minute identity (``time=09:19``), a NON-anchor
    coarse-TF pick (5-minute @ 09:19 — as-of-instant, pre-#1601 semantics
    preserved), and a #1601 bucket-ANCHOR pick (5-minute @ 09:15 — resolved
    FORWARD to the bucket-final 09:19; suffix independence must hold at the
    RESOLVED cutoff).
    """

    @pytest.mark.parametrize(
        ("interval", "time"),
        [("1minute", "09:19"), ("5minute", "09:19"), ("5minute", "09:15")],
    )
    def test_cutoff_rows_identical_under_divergent_suffix(
        self, tmp_path: Path, interval: str, time: str
    ) -> None:
        lake_a = tmp_path / "a"
        lake_b = tmp_path / "b"
        _seed_suffix_lake(lake_a, mutate_after=None)
        _seed_suffix_lake(lake_b, mutate_after=4)
        payload_a = run_movers_snapshot(
            lake_root=lake_a, interval=interval, date=_DAY.isoformat(), time=time, top_n=5
        )
        payload_b = run_movers_snapshot(
            lake_root=lake_b, interval=interval, date=_DAY.isoformat(), time=time, top_n=5
        )
        assert payload_a == payload_b  # the whole wire payload, byte-for-byte
        (row,) = payload_a["price_gainers"]
        # Independent hand values at the 09:19 cut: close@09:19 = 104,
        # cumulative session volume Σ(10..14) = 60.
        assert row["ltp"] == "₹104.00"
        assert row["volume"] == 60

    def test_whole_day_rows_genuinely_diverge(self, tmp_path: Path) -> None:
        # The anti-vacuity control: WITHOUT a cutoff the two lakes serve
        # different closes (109 vs 259) — the suffix mutation is real.
        lake_a = tmp_path / "a"
        lake_b = tmp_path / "b"
        _seed_suffix_lake(lake_a, mutate_after=None)
        _seed_suffix_lake(lake_b, mutate_after=4)
        whole_a = run_movers_snapshot(
            lake_root=lake_a, interval="1minute", date=_DAY.isoformat(), time=None, top_n=5
        )
        whole_b = run_movers_snapshot(
            lake_root=lake_b, interval="1minute", date=_DAY.isoformat(), time=None, top_n=5
        )
        assert whole_a["price_gainers"][0]["ltp"] == "₹109.00"
        assert whole_b["price_gainers"][0]["ltp"] == "₹259.00"


# ── futures Near/Next/Far cache-independence SIZE law (audit worker C) ─────

_SERIAL_FUTS = (
    "NSE-NIFTY-27Jun24-FUT",  # near  (rank 0)
    "NSE-NIFTY-25Jul24-FUT",  # next  (rank 1)
    "NSE-NIFTY-29Aug24-FUT",  # far   (rank 2)
)


def _seed_three_serial_futures(lake: Path) -> None:
    for fut in _SERIAL_FUTS:
        _write(
            lake,
            segment="FNO",
            symbol=fut,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="20100", volume=100, oi=1100)],
        )
        _write(
            lake,
            segment="FNO",
            symbol=fut,
            interval="1day",
            bars=[_day_bar(day=date(2024, 6, 12), close="20000", oi=1000)],
        )


class TestFuturesTierCacheIndependenceSizes:
    def test_tier_grid_one_new_cache_entry_per_tier_and_repeat_hit(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """DEC-MOVERS-FUTURES-TIER-001 strengthened: the injected-cache SIZE law.

        The shipped ``test_tiers_cache_independently`` pins DIFFERENT rows for
        Near vs Next; this pins the cache-key STRUCTURE — one NEW entry per
        tier (sizes ``[1, 2, 3]``) so a future key regression that COLLAPSES
        the tiers into one entry (rows accidentally similar) also fails, plus
        the repeat-Near hit (size stays 3 AND the Near contract is served — no
        stale cross-tier clobber).
        """
        _seed_three_serial_futures(tmp_path)
        movers_cache = MoversSnapshotCache()
        client = TestClient(
            build_app(
                DashboardState(),
                instrument_store=store,
                summary_cache=SummaryCache(ttl_floor_s=0.0),
                movers_cache=movers_cache,
            )
        )

        def _top_volume_symbol(tier: str) -> str:
            body = client.get(
                "/api/v1/movers",
                params={
                    "asset": "futures",
                    "interval": "1day",
                    "date": _DAY.isoformat(),
                    "expiry": tier,
                },
            ).json()
            assert body["expiry_tier"] == tier.lower()
            symbol: str = body["volume"][0]["symbol"]
            return symbol

        sizes: list[int] = []
        for tier, expected in zip(("Near", "Next", "Far"), _SERIAL_FUTS, strict=True):
            assert _top_volume_symbol(tier) == expected
            sizes.append(movers_cache.size())
        assert sizes == [1, 2, 3]  # one INDEPENDENT candidate entry per tier
        # Repeat Near: a cache HIT — no new entry, and still the Near contract
        # (a stale Far entry serving a Near poll would fail both asserts).
        assert _top_volume_symbol("Near") == _SERIAL_FUTS[0]
        assert movers_cache.size() == 3


# ── multi-row gainers/losers ORDER teeth (refuter rounds 1 + 2) ────────────
#
# The production laws under pin (read from source, then pinned):
#   * ``movers_snapshot._gainers`` / ``movers_futures.rank_price_gainers`` /
#     ``movers_options.rank_price_gainers`` — pct_change > 0, DESCENDING
#     (``-(pct)`` primary key; largest gain first);
#   * the ``_losers`` / ``rank_price_losers`` twins — pct_change < 0,
#     ASCENDING (raw ``pct`` primary key; most negative first).
# HONEST teeth provenance (round 2 — corrects the round-1 fix's
# over-generalised "a flip in any tab survived the battery" claim): the
# genuinely UNPINNED repo gaps were exactly SPOT gainers, SPOT losers and
# FUTURES losers. The spot flips survived R1's 859-passed ``-k movers``
# battery snapshot (R1-teeth-refuter.md, a SPOT-scoped measurement —
# captured wire repro: a 2-gainer lake served ['SMALLGAIN', 'BIGGAIN'] under
# the mutant); the futures-losers flip survived every pre-existing test on
# round-2 re-measurement. Futures GAINERS and options BOTH directions were
# ALREADY killed by shipped multi-row UNIT order pins
# (``test_movers_futures.py::TestRanking::test_top_n_caps_and_orders_descending``;
# ``test_movers_options.py::TestRankers::test_price_gainers_positive_desc``
# + ``test_price_losers_negative_asc``) — for those three list-directions
# the tests below are ROUTE-level complements (real lake → candidate compute
# → payload → wire, plus exact wire-pct ``Fraction`` oracles) to those
# ``_mover()``-stub unit pins: the same unit-vs-route differentiation the
# min-volume equality trio applies. These pins drive ≥3 candidates per side,
# with distinct %CHNG magnitudes, through the LIVE routes.

# symbol → (prior-session close, snapshot close) per side; %CHNG magnitudes
# all distinct so the exact list order is fully pct-determined (the
# deterministic symbol tie-break is a separate, untested-here law). Names
# chosen so neither alphabetical NOR reversed-pct order coincides with the
# expected order on the gainers side.
_SPOT_GAINER_CLOSES = {"GMID": ("100", "102"), "GTOP": ("100", "103"), "GLOW": ("100", "101")}
_SPOT_LOSER_CLOSES = {"LSML": ("100", "97"), "LMID": ("100", "98"), "LBIG": ("100", "99")}


def _pct_oracle(closes: dict[str, tuple[str, str]]) -> dict[str, Fraction]:
    """Independent per-symbol %CHNG oracle: exact rationals, never Decimal."""
    return {
        sym: Fraction(int(cur) - int(prev), int(prev)) * 100 for sym, (prev, cur) in closes.items()
    }


def _order_oracle(pcts: dict[str, Fraction], *, descending: bool) -> list[str]:
    """Independent rank oracle: sort by exact-Fraction pct (stable by symbol)."""
    return sorted(pcts, key=lambda sym: (-pcts[sym] if descending else pcts[sym], sym))


class TestGainersLosersMultiRowOrder:
    """The gainers/losers ORDER law with ≥3 rows per side, on all three tabs.

    ``price_gainers`` must serve descending pct (largest gain first) and
    ``price_losers`` ascending pct (most negative first) — pinned against an
    independent :class:`fractions.Fraction` oracle for both the per-row pct
    values AND the exact list order, at ROUTE level so the pins exercise each
    tab's real ranker (``_gainers``/``_losers`` · ``rank_price_gainers``/
    ``rank_price_losers`` in ``movers_futures`` and ``movers_options``).

    Teeth provenance (round 2, honest): NOVEL order coverage exists here for
    spot (both sides) and futures LOSERS only; the futures-gainers and
    options assertions are ROUTE-level + exact-wire-pct complements to the
    shipped unit order pins (see the section comment above).
    """

    def _assert_orders(
        self,
        body: dict[str, object],
        *,
        gainer_pcts: dict[str, Fraction],
        loser_pcts: dict[str, Fraction],
        symbol_of: dict[str, str],
    ) -> None:
        """Pin both lists' exact order + every wire pct vs the oracle.

        *symbol_of* maps each oracle key to its WIRE symbol (identity for
        spot; the full contract symbol for futures/options).
        """
        pct_of_wire = {symbol_of[key]: pct for key, pct in {**gainer_pcts, **loser_pcts}.items()}
        gainers = body["price_gainers"]
        losers = body["price_losers"]
        assert isinstance(gainers, list) and isinstance(losers, list)
        expected_gainers = [symbol_of[k] for k in _order_oracle(gainer_pcts, descending=True)]
        expected_losers = [symbol_of[k] for k in _order_oracle(loser_pcts, descending=False)]
        assert [r["symbol"] for r in gainers] == expected_gainers
        assert [r["symbol"] for r in losers] == expected_losers
        for row in [*gainers, *losers]:
            assert Fraction(row["pct_change"]) == pct_of_wire[row["symbol"]]

    def test_spot_gainers_descending_losers_most_negative_first(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        for sym, (prev, cur) in {**_SPOT_GAINER_CLOSES, **_SPOT_LOSER_CLOSES}.items():
            _write(
                tmp_path,
                symbol=sym,
                interval="1day",
                bars=[
                    _day_bar(day=date(2024, 6, 12), close=prev, volume=5),
                    _day_bar(day=_DAY, close=cur, volume=5),
                ],
            )
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "spot", "interval": "1day", "date": _DAY.isoformat()},
        ).json()
        self._assert_orders(
            body,
            gainer_pcts=_pct_oracle(_SPOT_GAINER_CLOSES),
            loser_pcts=_pct_oracle(_SPOT_LOSER_CLOSES),
            symbol_of={s: s for s in {**_SPOT_GAINER_CLOSES, **_SPOT_LOSER_CLOSES}},
        )

    def test_futures_gainers_descending_losers_most_negative_first(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # One near-month contract per DISTINCT underlying (the futures tab
        # serves one row per underlying's near contract), prior 1-day close
        # 20000 each, snapshot 1-minute closes giving ±3/±2/±1 %.
        gainer_closes = {
            "GMID": ("20000", "20400"),
            "GTOP": ("20000", "20600"),
            "GLOW": ("20000", "20200"),
        }
        loser_closes = {
            "LSML": ("20000", "19400"),
            "LMID": ("20000", "19600"),
            "LBIG": ("20000", "19800"),
        }
        symbol_of = {u: f"NSE-{u}-27Jun24-FUT" for u in {**gainer_closes, **loser_closes}}
        for underlying, (prev, cur) in {**gainer_closes, **loser_closes}.items():
            fut = symbol_of[underlying]
            _write(
                tmp_path,
                segment="FNO",
                symbol=fut,
                interval="1day",
                bars=[_day_bar(day=date(2024, 6, 12), close=prev, oi=1000)],
            )
            _write(
                tmp_path,
                segment="FNO",
                symbol=fut,
                interval="1minute",
                bars=[_min_bar(hour=9, minute=15, close=cur, volume=100, oi=1100)],
            )
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "futures", "interval": "1day", "date": _DAY.isoformat()},
        ).json()
        self._assert_orders(
            body,
            gainer_pcts=_pct_oracle(gainer_closes),
            loser_pcts=_pct_oracle(loser_closes),
            symbol_of=symbol_of,
        )

    def test_options_gainers_descending_losers_most_negative_first(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        # Six CE contracts on ONE underlying + expiry (all land in the near
        # tier), prior-session close 100 each, snapshot closes giving
        # ±3/±2/±1 %. Post-#1831 (DEC-OPTIONS-PREBUILD-RETIRE-001) a CE/PE
        # prev-close baseline SKIPS the frozen 1day read and walks back the
        # prior session's 1-MINUTE bars, so each CE seeds a prior-session
        # (2024-06-12) 1-minute bar carrying the prev close; the 1day prev
        # bar is kept — inert for CE/PE now — as the retirement-eve shape.
        # Strike→pct assignment PERMUTED (round-2 F2) so the expected pct
        # order is NEITHER the lexicographic contract-symbol order NOR its
        # reverse on EITHER side: gainers expect [20200, 20000, 20100] and
        # losers expect [20400, 20500, 20300] — a pct-key-drop mutant
        # (``pool.sort(key=_sort_key)``) now fails IN-FILE instead of
        # coinciding with the symbol tie-break (mutant-verified round 2).
        gainer_closes = {"20000": ("100", "102"), "20100": ("100", "101"), "20200": ("100", "103")}
        loser_closes = {"20300": ("100", "99"), "20400": ("100", "97"), "20500": ("100", "98")}
        symbol_of = {
            strike: f"NSE-NIFTY-27Jun24-{strike}-CE" for strike in {**gainer_closes, **loser_closes}
        }
        for strike, (prev, cur) in {**gainer_closes, **loser_closes}.items():
            ce = symbol_of[strike]
            _write(
                tmp_path,
                segment="FNO",
                symbol=ce,
                interval="1day",
                bars=[_day_bar(day=date(2024, 6, 12), close=prev, oi=1000)],
            )
            _write(
                tmp_path,
                segment="FNO",
                symbol=ce,
                interval="1minute",
                bars=[
                    # The prior-session 1-minute bar the #1831 walk-back reads.
                    _min_bar(
                        hour=9, minute=15, close=prev, volume=100, oi=1000, day=date(2024, 6, 12)
                    ),
                    _min_bar(hour=9, minute=15, close=cur, volume=100, oi=1100),
                ],
            )
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get(
            "/api/v1/movers",
            params={"asset": "options", "interval": "1day", "date": _DAY.isoformat()},
        ).json()
        self._assert_orders(
            body,
            gainer_pcts=_pct_oracle(gainer_closes),
            loser_pcts=_pct_oracle(loser_closes),
            symbol_of=symbol_of,
        )


# ── noncontiguous WARN cap end-to-end (audit worker D, P7) ─────────────────


class TestNoncontiguousWarnCapEndToEnd:
    def test_25_holed_symbols_emit_20_warns_one_summary_all_rows_stamped(
        self, tmp_path: Path
    ) -> None:
        """The cap bounds LOGS, never the wire honesty — through the REAL loop.

        The shipped cap test drives ``_NoncontiguousWarnState`` directly; this
        drives 25 lake-holed symbols through the real
        ``compute_spot_candidates`` candidate loop and pins the end-to-end
        contract: exactly 20 ``movers.prev_baseline_noncontiguous`` WARNs +
        exactly ONE ``…_suppressed`` summary (cap=20), while ALL 25 snapshots
        carry the ``prev_close_contiguous is False`` stamp.
        """
        # Each symbol: prior 1-day on Thu 2025-09-25 + snapshot Mon 2025-09-29
        # with the true prior session Fri 2025-09-26 a HOLE (the #1539 repro).
        for i in range(25):
            _write(
                tmp_path,
                symbol=f"SYM{i:02d}",
                interval="1day",
                bars=[
                    _day_bar(day=date(2025, 9, 25), close="100", volume=10),
                    _day_bar(day=date(2025, 9, 29), close="110", volume=10),
                ],
            )
        with capture_logs() as logs:
            result = compute_spot_candidates(
                lake_root=tmp_path, interval="1day", date="2025-09-29", time=None
            )
        assert len(result.snapshots) == 25
        assert all(s.prev_close_contiguous is False for s in result.snapshots)
        warns = [e for e in logs if e["event"] == "movers.prev_baseline_noncontiguous"]
        assert len(warns) == 20  # the hard per-build cap
        summaries = [
            e for e in logs if e["event"] == "movers.prev_baseline_noncontiguous_suppressed"
        ]
        assert len(summaries) == 1  # exactly ONE past-cap summary
        assert summaries[0]["cap"] == 20


# ── min_volume == volume equality boundary, route level (audit worker E) ───


class TestMinVolumeEqualBoundary:
    """DEC-MOVERS-MIN-VOLUME-001 — the ``>=`` law AT the boundary, on the wire.

    The shipped ROUTE-level floor pins use strictly-above/below values; only
    the isolated ``value_meets_volume_floor`` unit predicate
    (``tests/unit/test_movers_snapshot.py``) pins equality, never the route
    wiring — these pin the EQUALITY case (``volume == min_volume`` stays IN;
    ``+1`` drops it) at route level on all three tabs, so a route-side
    off-by-one (``>``) fails.
    """

    def test_spot_floor_equal_keeps_row(self, store: InstrumentStore, tmp_path: Path) -> None:
        _write(
            tmp_path,
            symbol="NIFTY",
            interval="1day",
            bars=[
                _day_bar(day=date(2024, 6, 12), close="20000", volume=700),
                _day_bar(day=_DAY, close="22000", volume=700),
            ],
        )
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        params = {"asset": "spot", "interval": "1day", "date": _DAY.isoformat()}
        at_floor = client.get("/api/v1/movers", params={**params, "min_volume": 700}).json()
        assert [r["symbol"] for r in at_floor["volume"]] == ["NIFTY"]  # == is IN
        above = client.get("/api/v1/movers", params={**params, "min_volume": 701}).json()
        assert above["volume"] == []  # one past the boundary drops it

    def test_futures_floor_equal_keeps_row(self, store: InstrumentStore, tmp_path: Path) -> None:
        fut = "NSE-NIFTY-27Jun24-FUT"
        _write(
            tmp_path,
            segment="FNO",
            symbol=fut,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="20100", volume=500, oi=1100)],
        )
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        params = {"asset": "futures", "interval": "1day", "date": _DAY.isoformat()}
        at_floor = client.get("/api/v1/movers", params={**params, "min_volume": 500}).json()
        assert [r["symbol"] for r in at_floor["volume"]] == [fut]
        above = client.get("/api/v1/movers", params={**params, "min_volume": 501}).json()
        assert above["volume"] == []

    def test_options_floor_equal_keeps_row(self, store: InstrumentStore, tmp_path: Path) -> None:
        ce = "NSE-NIFTY-27Jun24-24000-CE"
        pe = "NSE-NIFTY-27Jun24-24000-PE"
        _write(
            tmp_path,
            segment="FNO",
            symbol=ce,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="150", volume=500, oi=1000)],
        )
        _write(
            tmp_path,
            segment="FNO",
            symbol=pe,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="90", volume=200, oi=800)],
        )
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        params = {"asset": "options", "interval": "1day", "date": _DAY.isoformat()}
        at_floor = client.get("/api/v1/movers", params={**params, "min_volume": 200}).json()
        assert {r["symbol"] for r in at_floor["volume"]} == {ce, pe}  # PE at floor IN
        above = client.get("/api/v1/movers", params={**params, "min_volume": 201}).json()
        assert [r["symbol"] for r in above["volume"]] == [ce]


# ── default-date canonicalised BEFORE the cache key (audit worker E, S4f) ──


class TestDateCanonicalisedBeforeCacheKey:
    def test_default_and_explicit_same_date_share_one_entry(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """A ``date=None`` poll and an explicit-same-date poll are ONE coordinate.

        The default date is resolved to the latest available day BEFORE the
        candidate cache is keyed — a regression that keyed the raw ``None``
        would mint a SECOND entry, and the explicit poll would recompute and
        SEE the un-signalled second symbol (the exchange-cache staleness trick
        proves the hit without any monkeypatch spy).
        """
        _write(
            tmp_path,
            symbol="NIFTY",
            interval="1day",
            bars=[
                _day_bar(day=date(2024, 6, 12), close="20000", volume=300),
                _day_bar(day=_DAY, close="22000", volume=700),
            ],
        )
        movers_cache = MoversSnapshotCache()
        client = TestClient(
            build_app(
                DashboardState(),
                instrument_store=store,
                summary_cache=SummaryCache(ttl_floor_s=0.0),
                movers_cache=movers_cache,
            )
        )
        default = client.get("/api/v1/movers", params={"asset": "spot", "interval": "1day"}).json()
        assert default["date"] == _DAY.isoformat()  # resolved, never today
        assert movers_cache.size() == 1
        # Write a second symbol with NO write signal: a cache HIT must not see it.
        _write(
            tmp_path,
            symbol="BANKNIFTY",
            interval="1day",
            bars=[_day_bar(day=_DAY, close="48000", volume=900)],
        )
        explicit = client.get(
            "/api/v1/movers",
            params={"asset": "spot", "interval": "1day", "date": _DAY.isoformat()},
        ).json()
        assert movers_cache.size() == 1  # the SAME canonical coordinate — no 2nd key
        assert explicit == default  # served from the shared entry, byte-equal
        assert {r["symbol"] for r in explicit["volume"]} == {"NIFTY"}  # BANKNIFTY unseen


# ── a genuine code bug PROPAGATES — never a silent skip (worker E, S3e) ────


class TestCodeBugPropagates500:
    def test_keyerror_in_candidate_loop_is_500_not_skipped(
        self, store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEC-MOVERS-CORRUPT-SKIP-001's catch set is NARROW by design.

        The per-instrument skip arm covers only unreadable-file faults
        (Polars/OS/DuckDB/resample); a genuine CODE bug (here a ``KeyError``
        injected into the daily read) must PROPAGATE to an HTTP 500 — never be
        folded into ``skipped_count`` where it would silently blank rows.
        """
        _write(
            tmp_path,
            symbol="NIFTY",
            interval="1day",
            bars=[_day_bar(day=_DAY, close="22000", volume=700)],
        )

        def _boom(*args: object, **kwargs: object) -> object:
            raise KeyError("injected code bug — must NOT be swallowed as a skip")

        monkeypatch.setattr("brutex.dashboard.movers_snapshot._read_daily_bars", _boom)
        client = TestClient(
            build_app(DashboardState(), instrument_store=store),
            raise_server_exceptions=False,
        )
        resp = client.get(
            "/api/v1/movers",
            params={"asset": "spot", "interval": "1day", "date": _DAY.isoformat()},
        )
        assert resp.status_code == 500


# ── corrupt registry.duckdb through the LIVE options route (worker C, L3c) ─


class TestCorruptRegistryRouteFallback:
    def test_garbage_registry_file_serves_lake_rows_with_warn_never_500(
        self, store: InstrumentStore, tmp_path: Path
    ) -> None:
        """DEC-MOVERS-CALENDAR-TIERS-001 graceful fallback over REAL garbage.

        The shipped unit test mocks ``RegistryError``; this plants literal
        garbage bytes at ``registry.duckdb`` and drives the LIVE route: the
        real DuckDB open fails, ``load_registry_expiries`` degrades to
        ``None`` with ONE ``movers.options.registry_unreadable`` WARN, and the
        options tab still serves the lake-derived near tier — HTTP 200, never
        a 500 or a blank screen.
        """
        ce = "NSE-NIFTY-27Jun24-24000-CE"
        _write(
            tmp_path,
            segment="FNO",
            symbol=ce,
            interval="1minute",
            bars=[_min_bar(hour=9, minute=15, close="150", volume=100, oi=1000)],
        )
        (tmp_path / "registry.duckdb").write_bytes(b"not a duckdb database \x00 garbage")
        client = TestClient(build_app(DashboardState(), instrument_store=store))
        with capture_logs() as logs:
            resp = client.get(
                "/api/v1/movers",
                params={"asset": "options", "interval": "1day", "date": _DAY.isoformat()},
            )
        assert resp.status_code == 200
        body = resp.json()
        # The lake-partition fallback still resolves the near tier and ranks.
        assert body["instrument_count"] == 1
        assert [r["symbol"] for r in body["volume"]] == [ce]
        warns = [e for e in logs if e["event"] == "movers.options.registry_unreadable"]
        assert len(warns) == 1
