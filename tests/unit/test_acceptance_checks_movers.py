"""Movers-check pins: real-wire vs fenced-leg equality + the tamper tooth.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1797). The tamper tooth is
the proof this check CATCHES rather than mirrors: a perturbed payload cell
must FAIL with both legs' values.
"""

from __future__ import annotations

import json
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="movers leg A drives the real app")

from brutex.acceptance import independent as ind
from brutex.acceptance.checks import movers
from brutex.acceptance.model import AbstainReason, Verdict

from tests.fakes.acceptance_twin import DAY1, DAY2, make_ctx, make_twin


def _sub(check, sub_id: str):
    return next(s for s in check.sub_checks if s.id == sub_id)


def _real_client(lake: Path):
    from brutex.dashboard.app import DashboardState, build_app
    from brutex.data.instrument_store import InstrumentStore
    from fastapi.testclient import TestClient

    store = InstrumentStore.open(lake / "instruments.duckdb")
    return TestClient(
        build_app(DashboardState(), instrument_store=store), base_url="http://localhost"
    )


class _MutatingClient:
    """Proxy over the real client that perturbs served payload cells."""

    def __init__(self, real: Any, mutate) -> None:
        self._real = real
        self._mutate = mutate

    def get(self, path: str, params: dict | None = None) -> Any:
        response = self._real.get(path, params=params)
        if path != "/api/v1/movers" or (params or {}).get("asset") != "spot":
            return response
        payload = response.json()
        self._mutate(payload, params or {})
        return _CannedResponse(payload)


class _CannedResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = json.dumps(payload, sort_keys=True).encode()

    def json(self) -> dict:
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload, sort_keys=True)

    def raise_for_status(self) -> None:  # 200 by construction
        return None


class TestMoversRealWire:
    def test_clean_twin_equality_pass(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        ctx = make_ctx(lake, client_factory=lambda: _real_client(lake))
        check = movers.run(ctx)
        spot = _sub(check, "movers.spot")
        assert spot.verdict is Verdict.PASS, spot.evidence["mismatches"]
        assert spot.evidence["cells_compared"] > 0
        assert _sub(check, "movers.tf_invariance").verdict is Verdict.PASS
        assert _sub(check, "movers.cache_consistency").verdict is Verdict.PASS
        assert _sub(check, "movers.futures").abstain_reason is AbstainReason.NO_FNO_DATA
        assert _sub(check, "movers.options").abstain_reason is AbstainReason.NO_FNO_DATA

    def test_tamper_tooth_volume_cell(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        real = _real_client(lake)

        def mutate(payload: dict, params: dict) -> None:
            if params.get("interval") == "1minute":
                for row in payload.get("volume", []):
                    if row["symbol"] == "NIFTY":
                        row["volume"] = row["volume"] + 1

        ctx = make_ctx(lake, client_factory=lambda: _MutatingClient(real, mutate))
        check = movers.run(ctx)
        spot = _sub(check, "movers.spot")
        assert spot.verdict is Verdict.FAIL
        hit = next(m for m in spot.evidence["mismatches"] if "volume" in m)
        assert "page=" in hit and "independent=" in hit  # both legs' values shown

    def test_pct_divergence_bucketed_stored_tf(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        real = _real_client(lake)

        def mutate(payload: dict, params: dict) -> None:
            for rows in payload.values():
                if isinstance(rows, list):
                    for row in rows:
                        if row.get("pct_change") is not None:
                            row["pct_change"] = str(Fraction(row["pct_change"]) + 1)

        ctx = make_ctx(lake, client_factory=lambda: _MutatingClient(real, mutate))
        check = movers.run(ctx)
        spot = _sub(check, "movers.spot")
        assert spot.verdict is Verdict.FAIL
        assert spot.evidence["stored_tf_prevclose_divergences"] > 0
        assert any("[stored_tf_prevclose_divergence]" in m for m in spot.evidence["mismatches"])

    def test_rank_order_perturbation_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        real = _real_client(lake)

        def mutate(payload: dict, params: dict) -> None:
            if params.get("interval") == "5minute":
                payload["volume"] = list(reversed(payload.get("volume", [])))

        ctx = make_ctx(lake, client_factory=lambda: _MutatingClient(real, mutate))
        check = movers.run(ctx)
        spot = _sub(check, "movers.spot")
        assert spot.verdict is Verdict.FAIL
        assert any("rank order" in m for m in spot.evidence["mismatches"])

    def test_phantom_row_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        real = _real_client(lake)

        def mutate(payload: dict, params: dict) -> None:
            if params.get("interval") == "1day":
                rows = payload.get("volume", [])
                if rows:
                    ghost = dict(rows[0])
                    ghost["symbol"] = "GHOSTSYM"
                    rows.append(ghost)

        ctx = make_ctx(lake, client_factory=lambda: _MutatingClient(real, mutate))
        check = movers.run(ctx)
        spot = _sub(check, "movers.spot")
        assert spot.verdict is Verdict.FAIL
        assert any("no bar" in m for m in spot.evidence["mismatches"])

    def test_gated_payload_abstains(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)

        class _Gated:
            def get(self, path: str, params: dict | None = None) -> Any:
                return _CannedResponse({"gated": True})

        ctx = make_ctx(lake, client_factory=_Gated)
        check = movers.run(ctx)
        assert check.sub_checks[0].abstain_reason is AbstainReason.PREBUILD_GATED

    def test_empty_lake_abstains_no_dataset(self, tmp_path: Path) -> None:
        check = movers.run(make_ctx(tmp_path))
        assert check.sub_checks[0].abstain_reason is AbstainReason.NO_DATASET

    def test_explicit_date_flag_used(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        ctx = make_ctx(lake, date=DAY2.isoformat(), client_factory=lambda: _real_client(lake))
        assert movers.resolve_day(ctx) == DAY2.isoformat()


class TestTfInvarianceAndCache:
    def test_tf_invariance_divergence_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        real = _real_client(lake)

        def mutate(payload: dict, params: dict) -> None:
            if params.get("interval") == "5minute":
                for row in payload.get("volume", []):
                    row["ltp"] = "₹1.00"
                    row["value_cr"] = "9999.99"

        ctx = make_ctx(lake, client_factory=lambda: _MutatingClient(real, mutate))
        check = movers.run(ctx)
        tf_sub = _sub(check, "movers.tf_invariance")
        assert tf_sub.verdict is Verdict.FAIL
        assert any("diverges across TFs" in m for m in tf_sub.evidence["mismatches"])

    def test_cache_divergence_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        real = _real_client(lake)
        state = {"n": 0}

        def mutate(payload: dict, params: dict) -> None:
            if params.get("interval") == "1day":
                state["n"] += 1
                payload["note"] = f"changed-{state['n']}"

        ctx = make_ctx(lake, client_factory=lambda: _MutatingClient(real, mutate))
        check = movers.run(ctx)
        cache = _sub(check, "movers.cache_consistency")
        assert cache.verdict is Verdict.FAIL
        assert cache.evidence["failure_class"] == "cache_divergence"


class TestContiguityStamp:
    def _row(self, prev_date: date | None) -> ind.IndependentSpotRow:
        return ind.IndependentSpotRow(
            symbol="X",
            close=Fraction(100),
            prev_close=Fraction(90) if prev_date else None,
            prev_close_date=prev_date,
            chng=None,
            pct=None,
            volume=1,
            value_paisa=1,
        )

    def test_hole_without_stamp_flagged(self) -> None:
        # day 2026-07-09 (Thu): prior session is 07-08; anchoring on 07-01 is a hole.
        got = movers.contiguity_stamp_mismatch({}, self._row(date(2026, 7, 1)), day=DAY2)
        assert got is not None and "without stamp" in got

    def test_contiguous_anchor_clean(self) -> None:
        assert movers.contiguity_stamp_mismatch({}, self._row(DAY1), day=DAY2) is None

    def test_stamped_hole_clean(self) -> None:
        page = {"prev_close_contiguous": False}
        assert movers.contiguity_stamp_mismatch(page, self._row(date(2026, 7, 1)), day=DAY2) is None

    def test_muhurat_anchor_with_stamp_clean(self) -> None:
        muhurat = sorted(ind.MUHURAT_SESSIONS)[-1]
        page = {"prev_close_muhurat": True}
        assert movers.contiguity_stamp_mismatch(page, self._row(muhurat), day=DAY2) is None

    def test_no_baseline_abstains(self) -> None:
        assert movers.contiguity_stamp_mismatch({}, self._row(None), day=DAY2) is None


def _write_fno_contract(lake: Path, contract: str, *, day: date) -> None:
    from brutex.acceptance.selftest import _twin_bars
    from brutex.data.parquet_writer import ParquetWriter

    ParquetWriter(lake_root=lake).write(
        exchange="NSE",
        segment="FNO",
        symbol=contract,
        interval="1minute",
        bars=_twin_bars(day, base=200.0),
    )


class _FnoClient:
    """Serves crafted futures/options payloads; spot rides the real client."""

    def __init__(self, real: Any, futures_rows: list[dict], options_rows: list[dict]) -> None:
        self._real = real
        self._futures = futures_rows
        self._options = options_rows

    def get(self, path: str, params: dict | None = None) -> Any:
        asset = (params or {}).get("asset")
        if path == "/api/v1/movers" and asset == "futures":
            return _CannedResponse({"volume": self._futures, "note": "x"})
        if path == "/api/v1/movers" and asset == "options":
            return _CannedResponse({"volume": self._options, "note": "x"})
        return self._real.get(path, params=params)


class TestFuturesLeg:
    CONTRACT = "NSE-NIFTY-30Jul26-FUT"

    def _rows_for(self, lake: Path) -> list[dict]:
        bars = ind.read_1m_bars(lake / "bars" / "NSE" / "FNO" / self.CONTRACT)
        row = ind.independent_spot_row(self.CONTRACT, bars, day=DAY2, cutoff_minute=None)
        assert row is not None
        return [
            {"symbol": self.CONTRACT, "ltp": row.ltp_display, "volume": row.volume},
            {"symbol": self.CONTRACT, "ltp": row.ltp_display, "volume": row.volume},  # dup skipped
            {"symbol": "NSE-JUNKNAME", "ltp": "₹1.00", "volume": 0},  # unparsable skipped
        ]

    def test_futures_reduced_compare_pass(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _write_fno_contract(lake, self.CONTRACT, day=DAY2)
        real = _real_client(lake)
        rows = self._rows_for(lake)
        ctx = make_ctx(lake, client_factory=lambda: _FnoClient(real, rows, []))
        check = movers.run(ctx)
        futures = _sub(check, "movers.futures")
        assert futures.verdict is Verdict.PASS
        assert futures.evidence["contracts_compared"] == 1

    def test_futures_mismatch_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _write_fno_contract(lake, self.CONTRACT, day=DAY2)
        real = _real_client(lake)
        rows = self._rows_for(lake)
        rows[0]["volume"] = rows[0]["volume"] + 5
        ctx = make_ctx(lake, client_factory=lambda: _FnoClient(real, rows, []))
        futures = _sub(movers.run(ctx), "movers.futures")
        assert futures.verdict is Verdict.FAIL
        assert any("volume" in m for m in futures.evidence["mismatches"])

    def test_futures_ltp_mismatch_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        _write_fno_contract(lake, self.CONTRACT, day=DAY2)
        real = _real_client(lake)
        rows = self._rows_for(lake)
        rows[0]["ltp"] = "₹1.23"
        futures = _sub(
            movers.run(make_ctx(lake, client_factory=lambda: _FnoClient(real, rows, []))),
            "movers.futures",
        )
        assert futures.verdict is Verdict.FAIL

    def test_futures_row_without_raw_bars_fails(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        real = _real_client(lake)
        ghost = [{"symbol": "NSE-NIFTY-27Aug26-FUT", "ltp": "₹1.00", "volume": 1}]
        futures = _sub(
            movers.run(make_ctx(lake, client_factory=lambda: _FnoClient(real, ghost, []))),
            "movers.futures",
        )
        assert futures.verdict is Verdict.FAIL
        assert any("no raw bar" in m for m in futures.evidence["mismatches"])


class TestOptionsLeg:
    def test_grammar_and_presence_pass(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        real = _real_client(lake)
        rows = [
            {"symbol": "NSE-NIFTY-30Jul26-24000-CE", "moneyness": "ATM", "iv": None, "oi": 5},
            {"symbol": "NSE-NIFTY-30Jul26-25000-CE", "moneyness": "OTM +10", "iv": 0.2, "oi": 9},
        ]
        options = _sub(
            movers.run(make_ctx(lake, client_factory=lambda: _FnoClient(real, [], rows))),
            "movers.options",
        )
        assert options.verdict is Verdict.PASS

    def test_bad_label_and_missing_overlay_fail(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        real = _real_client(lake)
        rows = [
            {"symbol": "A", "moneyness": "DEEP ITM", "iv": None, "oi": 1},
            {"symbol": "B", "moneyness": "ATM"},  # iv/oi keys absent
        ]
        options = _sub(
            movers.run(make_ctx(lake, client_factory=lambda: _FnoClient(real, [], rows))),
            "movers.options",
        )
        assert options.verdict is Verdict.FAIL
        assert options.evidence["bad_labels"]
        assert options.evidence["missing_overlay_keys"]

    def test_honest_sentinels_are_in_grammar(self, tmp_path: Path) -> None:
        # Round 1 MAJOR-3 regression: the FULL production label vocabulary
        # (moneyness._format_label) is in-grammar — a correct sparse lake
        # serving "?" (spot unavailable) / bare ITM/OTM (no grid) / "—"
        # (non-option) must never redden the nightly.
        lake = make_twin(tmp_path)
        real = _real_client(lake)
        rows = [
            {"symbol": "Q", "moneyness": "?", "iv": None, "oi": 1},
            {"symbol": "G1", "moneyness": "ITM", "iv": None, "oi": 1},
            {"symbol": "G2", "moneyness": "OTM", "iv": None, "oi": 1},
            {"symbol": "NA", "moneyness": "—", "iv": None, "oi": 1},
            {"symbol": "D1", "moneyness": "ITM -2", "iv": None, "oi": 1},
            {"symbol": "D2", "moneyness": "OTM +10", "iv": None, "oi": 1},
        ]
        options = _sub(
            movers.run(make_ctx(lake, client_factory=lambda: _FnoClient(real, [], rows))),
            "movers.options",
        )
        assert options.verdict is Verdict.PASS
        assert options.evidence["bad_labels"] == []

    def test_wrong_sign_stays_off_grammar(self, tmp_path: Path) -> None:
        # The sign tracks the BUCKET (ITM always -, OTM always +): a flipped
        # sign is NOT production vocabulary and must still FAIL.
        lake = make_twin(tmp_path)
        real = _real_client(lake)
        rows = [{"symbol": "X", "moneyness": "ITM +2", "iv": None, "oi": 1}]
        options = _sub(
            movers.run(make_ctx(lake, client_factory=lambda: _FnoClient(real, [], rows))),
            "movers.options",
        )
        assert options.verdict is Verdict.FAIL

    def test_zero_and_zero_padded_depths_stay_off_grammar(self, tmp_path: Path) -> None:
        # Round-2 NIT: production _format_label can never emit "ITM -0" /
        # "OTM +0" (the ATM band IS offset 0) nor zero-padded depths — the
        # tightened grammar refuses the production-unreachable slack.
        lake = make_twin(tmp_path)
        real = _real_client(lake)
        rows = [
            {"symbol": "Z0", "moneyness": "ITM -0", "iv": None, "oi": 1},
            {"symbol": "ZP", "moneyness": "ITM -007", "iv": None, "oi": 1},
            {"symbol": "O0", "moneyness": "OTM +0", "iv": None, "oi": 1},
        ]
        options = _sub(
            movers.run(make_ctx(lake, client_factory=lambda: _FnoClient(real, [], rows))),
            "movers.options",
        )
        assert options.verdict is Verdict.FAIL
        assert len(options.evidence["bad_labels"]) == 3


class _OptionsRaisingClient:
    """Proxy that HARD-FAILS the test if the options route is ever hit."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def get(self, path: str, params: dict | None = None) -> Any:
        if path == "/api/v1/movers" and (params or {}).get("asset") == "options":
            raise AssertionError("options route must NOT run under a drifted registry")
        return self._real.get(path, params=params)


class TestRegistrySchemaGuard:
    """Round-2 NEW-1 (security refuter): the registry RW-open heal class."""

    @staticmethod
    def _make_registry(lake: Path, *, version: str | None = None) -> Path:
        from brutex.data.registry_store import RegistryStore

        path = lake / "registry.duckdb"
        store = RegistryStore.open(path)
        if version is not None:
            store._conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                [version],
            )
        store.close()
        return path

    def test_version_bumped_registry_abstains_file_byte_identical(self, tmp_path: Path) -> None:
        # The latent DROP+RECREATE: a registry stamped at an OLDER schema
        # version would be dropped by the route's RegistryStore.open — the
        # verifier must ABSTAIN before the route runs, bytes untouched.
        import hashlib

        lake = make_twin(tmp_path)
        path = self._make_registry(lake, version="0")
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        real = _real_client(lake)
        check = movers.run(make_ctx(lake, client_factory=lambda: _OptionsRaisingClient(real)))
        options = _sub(check, "movers.options")
        assert options.verdict is Verdict.ABSTAIN
        assert options.abstain_reason is AbstainReason.REGISTRY_SCHEMA_DRIFT
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before

    def test_matching_registry_proceeds_with_disclosure(self, tmp_path: Path) -> None:
        from brutex.data.registry_store import REGISTRY_SCHEMA_VERSION

        lake = make_twin(tmp_path)
        self._make_registry(lake)
        real = _real_client(lake)
        rows = [{"symbol": "M", "moneyness": "ATM", "iv": None, "oi": 1}]
        options = _sub(
            movers.run(make_ctx(lake, client_factory=lambda: _FnoClient(real, [], rows))),
            "movers.options",
        )
        assert options.verdict is Verdict.PASS
        assert options.evidence["registry_schema_version"] == str(REGISTRY_SCHEMA_VERSION)

    def test_absent_registry_real_route_creates_nothing(self, tmp_path: Path) -> None:
        # Empirical proof the route tolerates absence WITHOUT creating the
        # file (load_registry_expiries returns None before any open) — the
        # licence for proceeding on "absent" instead of abstaining.
        lake = make_twin(tmp_path)
        _write_fno_contract(lake, "NSE-NIFTY-30Jul26-24000-CE", day=DAY2)
        ctx = make_ctx(lake, client_factory=lambda: _real_client(lake))
        check = movers.run(ctx)
        assert not (lake / "registry.duckdb").exists()
        options = _sub(check, "movers.options")
        assert options.verdict is not Verdict.ERROR

    def test_probe_states(self, tmp_path: Path) -> None:
        import duckdb

        lake = make_twin(tmp_path)
        # absent
        state, _ = movers.registry_schema_probe(lake / "registry.duckdb")
        assert state == "absent"
        # match
        path = self._make_registry(lake)
        state, evidence = movers.registry_schema_probe(path)
        assert state == "match" and evidence["registry_schema_version"] is not None
        # drift (older stamp)
        conn = duckdb.connect(str(path))
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '0')"
        )
        conn.close()
        state, _ = movers.registry_schema_probe(path)
        assert state == "drift"
        # drift (no schema_meta table at all — an open would restamp = write)
        bare = lake / "bare.duckdb"
        conn = duckdb.connect(str(bare))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.close()
        state, _ = movers.registry_schema_probe(bare)
        assert state == "drift"
        # drift (schema_meta exists but no schema_version row)
        rowless = lake / "rowless.duckdb"
        conn = duckdb.connect(str(rowless))
        conn.execute("CREATE TABLE schema_meta (key VARCHAR, value VARCHAR)")
        conn.close()
        state, evidence = movers.registry_schema_probe(rowless)
        assert state == "drift" and evidence["registry_schema_version"] is None
        # unreadable (a concurrent RW holder in-process)
        holder = duckdb.connect(str(path))
        try:
            state, _ = movers.registry_schema_probe(path)
            assert state == "unreadable"
        finally:
            holder.close()


class TestStoreUnavailableAbstain:
    def test_non_200_spot_route_abstains_named(self, tmp_path: Path) -> None:
        # Round 1 M1 knock-on: a locked/absent instrument master 503s the
        # store-backed movers route — a named ABSTAIN, never a crash/ERROR.
        lake = make_twin(tmp_path)

        class _Unavailable:
            def get(self, path: str, params: dict | None = None) -> Any:
                return _CannedResponse({"detail": "no store"}, status_code=503)

        check = movers.run(make_ctx(lake, client_factory=_Unavailable))
        spot = _sub(check, "movers.spot")
        assert spot.verdict is Verdict.ABSTAIN
        assert spot.abstain_reason is AbstainReason.NO_INSTRUMENT_STORE

    def test_400_route_error_not_mislabeled_master_absent(self, tmp_path: Path) -> None:
        # Round-6 F2 (Golden Rule #10): a 400 from the route (e.g. a corrupt bar
        # `CandleBar: close must be in [low, high]`) is a genuine route/data
        # error on a lake whose master is PRESENT + healthy — it must NOT be
        # relabelled "instrument master absent". It ABSTAINs ROUTE_ERROR with the
        # honest status + brief detail (no verdict slip — the corrupt bar is also
        # caught as sanity.price FAIL).
        lake = make_twin(tmp_path)

        class _BadRequest:
            def get(self, path: str, params: dict | None = None) -> Any:
                return _CannedResponse(
                    {"detail": "CandleBar: close (244.2) must be in [low=120.6, high=120.8]"},
                    status_code=400,
                )

        check = movers.run(make_ctx(lake, client_factory=_BadRequest))
        spot = _sub(check, "movers.spot")
        assert spot.verdict is Verdict.ABSTAIN
        assert spot.abstain_reason is AbstainReason.ROUTE_ERROR
        assert spot.abstain_reason is not AbstainReason.NO_INSTRUMENT_STORE
        assert spot.evidence["status"] == 400
        assert "master" not in str(spot.evidence["reason"]).lower()
        assert "HTTP 400" in str(spot.evidence["reason"])


class TestCompareCellsUnits:
    def _row(self) -> ind.IndependentSpotRow:
        return ind.IndependentSpotRow(
            symbol="X",
            close=Fraction("100.00"),
            prev_close=Fraction("90.00"),
            prev_close_date=DAY1,
            chng=Fraction("10.00"),
            pct=Fraction(100, 9),
            volume=5,
            value_paisa=100,
        )

    def _page(self, row: ind.IndependentSpotRow) -> dict:
        return {
            "symbol": "X",
            "ltp": row.ltp_display,
            "volume": row.volume,
            "value_cr": row.value_cr_display,
            "pct_change": str(float(row.pct)) if row.pct is not None else None,
            "change": ind.format_inr_2dp(row.chng) if row.chng is not None else None,
        }

    def test_equal_row_no_mismatch(self) -> None:
        row = self._row()
        assert movers.compare_spot_cells(self._page(row), row) == []

    def test_pct_nullability_mismatch(self) -> None:
        row = self._row()
        page = self._page(row)
        page["pct_change"] = None
        assert any("nullability" in m for m in movers.compare_spot_cells(page, row))

    def test_change_cell_mismatch(self) -> None:
        row = self._row()
        page = self._page(row)
        page["change"] = "₹9.99"
        assert any(m.startswith("change:") for m in movers.compare_spot_cells(page, row))


class TestStampInLoop:
    def test_stripped_hole_stamp_fails_on_the_wire(self, tmp_path: Path) -> None:
        from brutex.acceptance.selftest import build_twin_lake

        # A lake with a REAL prev-close hole (2026-06-25 → 2026-07-09): the
        # production page stamps it noncontiguous; a proxy that STRIPS the
        # stamp must be caught by the independent contiguity law.
        lake = tmp_path / "lake"
        build_twin_lake(lake, days=(date(2026, 6, 25), DAY2), symbols=("NIFTY", "INDIAVIX"))
        real = _real_client(lake)

        def strip(payload: dict, params: dict) -> None:
            for rows in payload.values():
                if isinstance(rows, list):
                    for row in rows:
                        row.pop("prev_close_contiguous", None)
                        row.pop("prev_close_session", None)

        ctx = make_ctx(lake, client_factory=lambda: _MutatingClient(real, strip))
        check = movers.run(ctx)
        spot = _sub(check, "movers.spot")
        assert spot.verdict is Verdict.FAIL
        assert any("without stamp" in m for m in spot.evidence["mismatches"])

    def test_stamped_hole_passes_on_the_wire(self, tmp_path: Path) -> None:
        from brutex.acceptance.selftest import build_twin_lake

        lake = tmp_path / "lake"
        build_twin_lake(lake, days=(date(2026, 6, 25), DAY2), symbols=("NIFTY", "INDIAVIX"))
        ctx = make_ctx(lake, client_factory=lambda: _real_client(lake))
        check = movers.run(ctx)
        spot = _sub(check, "movers.spot")
        assert spot.verdict is Verdict.PASS, spot.evidence["mismatches"]


class TestCompareCellsNoBaseline:
    def test_no_baseline_row_skips_change_compare(self) -> None:
        row = ind.IndependentSpotRow(
            symbol="X",
            close=Fraction("100.00"),
            prev_close=None,
            prev_close_date=None,
            chng=None,
            pct=None,
            volume=1,
            value_paisa=100,
        )
        page = {
            "symbol": "X",
            "ltp": row.ltp_display,
            "volume": 1,
            "value_cr": row.value_cr_display,
            "pct_change": None,
            "change": None,
        }
        assert movers.compare_spot_cells(page, row) == []
