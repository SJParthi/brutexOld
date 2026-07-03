"""SURFACE TRIAL — every number the ``/results`` page displays, independently recomputed.

DEC-SURFACE-TRIAL-001: fixtures are written with the REAL repo writers (the exact
:class:`~brutex.engine.results_store.ResultsStore` the sweep engine persists with),
the REAL ``/api/v1/sweep/leaderboard`` route is driven through the established
``build_app`` + ``TestClient`` pattern, and every displayed number is recomputed
INDEPENDENTLY on the test side — pure integer / hand-rolled arithmetic, no calls
into ``sweep_results_view`` / ``brutex.money`` — then asserted equal. A divergence
here is a production bug by protocol (the independent recompute is never bent to
match production).

Number families trialed (the prioritized list of the PR-2 research):

* **DEC-MONEY-2DP-001 boundaries** — ``net_pnl`` ₹-string for a negative paisa
  (``-1`` → ``"₹-0.01"``), an odd-paisa lakh-scale value, and a value beyond
  ``2**53`` paisa; hand-rolled 3-digit-grouped integer formatting (no Decimal).
* **Aggregates / dedup totals** — ``total_rows`` with a run_id duplicated across
  two overlapping stores (counted ONCE); ``store_count`` / ``skipped`` with a
  deliberately CORRUPT store in the tree.
* **Ranking stats** — descending metric order + run_id-ascending tie-break;
  int64-paisa-beyond-``2**53`` ranking where a float coercion PROVABLY mis-ranks;
  unknown ``rank_by`` → sharpe fallback echo; bit-exact metric passthrough
  (``==``, never approx — the contract IS no-rounding, CLAUDE.md §4).
* **Pagination totals** — ``rows`` length == min(top, total); ``total_rows``
  invariant under ``top``.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")
pytest.importorskip("polars", reason="the leaderboard reads per-tuple Parquet stores")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

# ────────────────────────────────────────────────────────────────────────
# Independent recomputation helpers — NO production imports, by design.
# ────────────────────────────────────────────────────────────────────────


def _independent_inr(paisa: int) -> str:
    """Paisa → the ``₹`` display string via PURE integer arithmetic.

    Hand-rolled western 3-digit grouping + a two-digit paisa tail — deliberately
    NOT ``brutex.money.format_inr`` and NOT :class:`~decimal.Decimal`, so the
    trial's expectation is computed by a genuinely independent code path.
    """
    sign = "-" if paisa < 0 else ""
    rupees, cents = divmod(abs(paisa), 100)
    digits = str(rupees)
    groups: list[str] = []
    while len(digits) > 3:
        groups.append(digits[-3:])
        digits = digits[:-3]
    groups.append(digits)
    return f"₹{sign}{','.join(reversed(groups))}.{cents:02d}"


def _independent_order(specs: list[dict[str, Any]], rank_by: str) -> list[str]:
    """The expected run_id order: *rank_by* DESC, run_id ASC tie-break.

    A single-pass negated-key sort (a different algorithm shape from the
    production two-pass stable sort) over the test's OWN spec dicts. Negation is
    exact for Python ints (arbitrary precision) and IEEE floats alike.
    """
    return [s["run_id"] for s in sorted(specs, key=lambda s: (-s[rank_by], s["run_id"]))]


# ────────────────────────────────────────────────────────────────────────
# Fixture — the smallest sweep tree exercising every displayed number.
# ────────────────────────────────────────────────────────────────────────

#: The test-owned raw specification of every fixture row. The ResultRow objects
#: AND the independent expectations are both derived from these dicts, so the
#: trial compares production against the same raw source it was fed.
_SPECS: list[dict[str, Any]] = [
    {
        # A — odd-paisa lakh-scale money, a 2-bit mask, full-precision win_rate.
        "run_id": f"{1:064x}",
        "instrument": "NIFTY",
        "tf": "5m",
        "direction": "bull",
        "mask_bits": 0b101,
        "net_pnl_paisa": 123_456_789,
        "sharpe_ratio": 2.5,
        "win_rate": 0.6180339887498949,
        "deflated_sharpe": 1.23,
        "total_trades": 34,
        "winning_trades": 21,
        "losing_trades": 13,
    },
    {
        # B — sharpe TIE with A (run_id tie-break), NEGATIVE one-paisa money,
        # the no-condition mask, a null deflated Sharpe.
        "run_id": f"{2:064x}",
        "instrument": "NIFTY",
        "tf": "5m",
        "direction": "bull",
        "mask_bits": 0,
        "net_pnl_paisa": -1,
        "sharpe_ratio": 2.5,
        "win_rate": 0.5,
        "deflated_sharpe": None,
        "total_trades": 10,
        "winning_trades": 5,
        "losing_trades": 5,
    },
    {
        # D — paisa exactly 2**60: float(2**60) == float(2**60 + 1), so an exact
        # int ranking is distinguishable from a float-coerced one (see C).
        "run_id": f"{3:064x}",
        "instrument": "NIFTY",
        "tf": "5m",
        "direction": "bull",
        "mask_bits": 1,
        "net_pnl_paisa": 2**60,
        "sharpe_ratio": 0.5,
        "win_rate": 0.25,
        "deflated_sharpe": 0.1,
        "total_trades": 4,
        "winning_trades": 1,
        "losing_trades": 3,
    },
    {
        # C — paisa 2**60 + 1 with the LARGER run_id: exact ints rank C above D;
        # a lossy float cast would tie them and the run_id tie-break would then
        # WRONGLY put D first. The no-float-coercion teeth of the trial.
        "run_id": f"{4:064x}",
        "instrument": "NIFTY",
        "tf": "5m",
        "direction": "bull",
        "mask_bits": 1,
        "net_pnl_paisa": 2**60 + 1,
        "sharpe_ratio": 1.5,
        "win_rate": 0.75,
        "deflated_sharpe": 0.2,
        "total_trades": 8,
        "winning_trades": 6,
        "losing_trades": 2,
    },
    {
        # E — the second (BANKNIFTY) store's own row; top sharpe overall.
        "run_id": f"{5:064x}",
        "instrument": "BANKNIFTY",
        "tf": "15m",
        "direction": "bear",
        "mask_bits": 0b11,
        "net_pnl_paisa": 55_555,
        "sharpe_ratio": 3.0,
        "win_rate": 0.9,
        "deflated_sharpe": 2.9,
        "total_trades": 20,
        "winning_trades": 18,
        "losing_trades": 2,
    },
]


def _row_from_spec(spec: dict[str, Any]) -> ResultRow:
    """Materialize one fixture spec into the REAL engine ResultRow."""
    return ResultRow(
        run_id=spec["run_id"],
        instrument=spec["instrument"],
        tf=spec["tf"],
        direction=spec["direction"],
        mode="A",
        mask_bits=spec["mask_bits"],
        net_pnl_paisa=spec["net_pnl_paisa"],
        gross_profit_paisa=max(spec["net_pnl_paisa"], 0) + 100,
        gross_loss_paisa=100,
        max_drawdown_paisa=50,
        expectancy_paisa=10,
        total_trades=spec["total_trades"],
        winning_trades=spec["winning_trades"],
        losing_trades=spec["losing_trades"],
        sharpe_ratio=spec["sharpe_ratio"],
        sortino_ratio=1.2,
        calmar_ratio=0.9,
        profit_factor=2.0,
        win_rate=spec["win_rate"],
        max_drawdown_pct=0.05,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
        deflated_sharpe=spec["deflated_sharpe"],
    )


def _nifty() -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY",
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _seed_sweep_tree(lake_root: Path) -> None:
    """Write the fixture stores with the REAL ResultsStore writer.

    * Store 1 (``NIFTY/5m``) — rows A, B, D, C.
    * Store 2 (``BANKNIFTY/15m``) — row E **plus a byte-identical duplicate of
      A** (same run_id): the cross-store dedup must count it ONCE.
    * Store 3 — a deliberately CORRUPT ``.parquet`` (garbage bytes) that must be
      SKIPPED + counted, never a 500.
    """
    by_run_id = {s["run_id"]: s for s in _SPECS}
    store1 = ResultsStore(
        path=lake_root / "sweeps" / "NIFTY" / "5m" / f"bull_k1_{'a' * 16}.parquet"
    )
    store1.path.parent.mkdir(parents=True, exist_ok=True)
    store1.extend([_row_from_spec(by_run_id[f"{seed:064x}"]) for seed in (1, 2, 3, 4)])
    store1.flush()

    store2 = ResultsStore(
        path=lake_root / "sweeps" / "BANKNIFTY" / "15m" / f"bear_k2_{'b' * 16}.parquet"
    )
    store2.path.parent.mkdir(parents=True, exist_ok=True)
    store2.extend(
        [
            _row_from_spec(by_run_id[f"{5:064x}"]),
            _row_from_spec(by_run_id[f"{1:064x}"]),  # the cross-store duplicate of A
        ]
    )
    store2.flush()

    corrupt = lake_root / "sweeps" / "NIFTY" / "5m" / f"bull_k9_{'c' * 16}.parquet"
    corrupt.write_bytes(b"this is not a parquet file")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    _seed_sweep_tree(tmp_path)
    app = build_app(DashboardState(), instrument_store=store)
    with TestClient(app) as c:
        yield c
    store.close()


# ────────────────────────────────────────────────────────────────────────
# The trial.
# ────────────────────────────────────────────────────────────────────────


def test_full_surface_default_sharpe_ranking(client: TestClient) -> None:
    """EVERY displayed number of the default view vs the independent recompute."""
    payload = client.get("/api/v1/sweep/leaderboard").json()

    # Aggregates: 2 readable stores, 1 corrupt skip, 5 DISTINCT run_ids (the
    # duplicated A counts once) — recomputed on the test's own spec list.
    assert payload["available"] is True
    assert payload["store_count"] == 2
    assert payload["skipped"] == 1
    assert payload["total_rows"] == len({s["run_id"] for s in _SPECS})

    # Dropdown option sets: sorted distincts over ALL rows.
    assert payload["instruments"] == sorted({s["instrument"] for s in _SPECS})
    assert payload["timeframes"] == sorted({s["tf"] for s in _SPECS})
    assert payload["directions"] == sorted({s["direction"] for s in _SPECS})
    assert payload["rank_by"] == "sharpe_ratio"

    # Ranking: sharpe DESC, run_id ASC tie-break (A before B at 2.5) —
    # independently recomputed with a single-pass negated-key sort.
    expected_order = _independent_order(_SPECS, "sharpe_ratio")
    assert [r["run_id"] for r in payload["rows"]] == expected_order
    tie_positions = [expected_order.index(f"{1:064x}"), expected_order.index(f"{2:064x}")]
    assert tie_positions[0] + 1 == tie_positions[1]  # the tie broke by run_id ASC

    # Per-row numbers vs the independent recompute (bit-exact, no approx).
    by_run_id = {s["run_id"]: s for s in _SPECS}
    for rank, row in enumerate(payload["rows"], start=1):
        spec = by_run_id[row["run_id"]]
        assert row["rank"] == rank  # 1-based enumerate over the ranked slice
        assert row["instrument"] == spec["instrument"]
        assert row["tf"] == spec["tf"]
        assert row["direction"] == spec["direction"]
        # Money: the ₹ string from pure integer grouping; the client-sort float
        # from the exact rational (paisa / 100) — both fully independent.
        assert row["net_pnl"] == _independent_inr(spec["net_pnl_paisa"])
        assert row["net_pnl_rupees"] == spec["net_pnl_paisa"] / 100
        # Full-precision passthrough — the contract IS no-rounding (CLAUDE.md §4):
        # the JSON float must equal the fixture float EXACTLY.
        assert row["sharpe_ratio"] == spec["sharpe_ratio"]
        assert row["win_rate"] == spec["win_rate"]
        assert row["deflated_sharpe"] == spec["deflated_sharpe"]  # None ⇒ JSON null
        # Counts passthrough.
        assert row["total_trades"] == spec["total_trades"]
        assert row["winning_trades"] == spec["winning_trades"]
        assert row["losing_trades"] == spec["losing_trades"]
        # Conditions decode — structural: one name per set mask bit, "(empty)"
        # for the no-condition mask (the name TABLE itself is production-owned,
        # so the independent check is the bit-count law, not the spellings).
        bits = spec["mask_bits"]
        assert row["mask_bits"] == bits
        if bits == 0:
            assert row["conditions"] == ""
        else:
            assert len(row["conditions"].split(" AND ")) == bin(bits).count("1")


def test_money_boundaries_paisa_to_inr_string(client: TestClient) -> None:
    """DEC-MONEY-2DP-001 boundary values, spelled out against the wire strings."""
    rows = {r["run_id"]: r for r in client.get("/api/v1/sweep/leaderboard").json()["rows"]}
    assert rows[f"{2:064x}"]["net_pnl"] == "₹-0.01"  # −1 paisa: sign + sub-rupee
    assert rows[f"{1:064x}"]["net_pnl"] == "₹1,234,567.89"  # odd paisa, grouped
    assert rows[f"{3:064x}"]["net_pnl"] == "₹11,529,215,046,068,469.76"  # 2**60 paisa
    # And each equals the independent integer formatter (self-consistency of the
    # spelled-out literals above).
    assert rows[f"{2:064x}"]["net_pnl"] == _independent_inr(-1)
    assert rows[f"{1:064x}"]["net_pnl"] == _independent_inr(123_456_789)
    assert rows[f"{3:064x}"]["net_pnl"] == _independent_inr(2**60)


def test_int64_paisa_ranking_is_exact_not_float(client: TestClient) -> None:
    """Ranking by ``net_pnl_paisa`` beyond ``2**53`` must use EXACT ints.

    C (``2**60 + 1``, larger run_id) must rank ABOVE D (``2**60``, smaller
    run_id). ``float(2**60) == float(2**60 + 1)``, so a float-coerced rank would
    tie them and the run_id tie-break would invert the pair — the trial proves
    the exact-int and float-coerced orders genuinely DIFFER on this fixture.
    """
    payload = client.get("/api/v1/sweep/leaderboard?rank_by=net_pnl_paisa").json()
    assert payload["rank_by"] == "net_pnl_paisa"
    got = [r["run_id"] for r in payload["rows"]]
    exact = _independent_order(_SPECS, "net_pnl_paisa")
    float_coerced = [
        s["run_id"] for s in sorted(_SPECS, key=lambda s: (-float(s["net_pnl_paisa"]), s["run_id"]))
    ]
    assert exact != float_coerced  # the fixture has real teeth
    assert got == exact
    assert got[0] == f"{4:064x}"  # C (2**60 + 1) strictly above D (2**60)
    assert got[1] == f"{3:064x}"


def test_filters_casefold_and_total_rows(client: TestClient) -> None:
    """A lower-cased instrument filter matches; ``total_rows`` is post-filter."""
    payload = client.get("/api/v1/sweep/leaderboard?instrument=nifty").json()
    expected = [s for s in _SPECS if s["instrument"] == "NIFTY"]
    assert payload["total_rows"] == len(expected)
    assert {r["instrument"] for r in payload["rows"]} == {"NIFTY"}
    assert [r["run_id"] for r in payload["rows"]] == _independent_order(expected, "sharpe_ratio")
    # The dropdown sets stay filter-independent (built over ALL rows).
    assert payload["instruments"] == sorted({s["instrument"] for s in _SPECS})


def test_top_slices_rows_but_not_total(client: TestClient) -> None:
    """``top`` bounds the row slice; ``total_rows`` is invariant under it."""
    payload = client.get("/api/v1/sweep/leaderboard?top=2").json()
    assert len(payload["rows"]) == 2
    assert payload["total_rows"] == len(_SPECS)  # the "showing N of M" M
    assert [r["rank"] for r in payload["rows"]] == [1, 2]
    assert [r["run_id"] for r in payload["rows"]] == _independent_order(_SPECS, "sharpe_ratio")[:2]


def test_unknown_rank_by_falls_back_to_sharpe(client: TestClient) -> None:
    """An unknown ``rank_by`` degrades honestly to the sharpe default (no 500)."""
    payload = client.get("/api/v1/sweep/leaderboard?rank_by=bogus_column").json()
    assert payload["rank_by"] == "sharpe_ratio"
    assert [r["run_id"] for r in payload["rows"]] == _independent_order(_SPECS, "sharpe_ratio")
