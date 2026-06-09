"""Tests for `brutex.adapters.dhan.instrument_refresh` — download → persist.

100% line+branch of `refresh_instruments` via a fake http_get + a fake
InstrumentSink (no network, no DuckDB).
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Sequence
from datetime import UTC, date, datetime

import pytest
from brutex import __version__
from brutex.adapters.dhan.instrument_refresh import (
    RefreshResult,
    _code_commit_sha,
    refresh_instruments,
)
from brutex.data.instrument_store import SCHEMA_VERSION, InstrumentRun
from brutex.models import Instrument

_HEADER = [
    "EXCH_ID",
    "SECURITY_ID",
    "INSTRUMENT",
    "INSTRUMENT_TYPE",
    "SYMBOL_NAME",
    "DISPLAY_NAME",
    "LOT_SIZE",
    "TICK_SIZE",
    "SM_FREEZE_QTY",
    "UNDERLYING_SYMBOL",
    "OPTION_TYPE",
    "STRIKE_PRICE",
    "SM_EXPIRY_DATE",
]


def _row(**over: str) -> dict[str, str]:
    base = dict.fromkeys(_HEADER, "")
    base.update(over)
    return base


def _csv(rows: list[dict[str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_HEADER)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _rows() -> list[dict[str, str]]:
    return [
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="13",
            INSTRUMENT="INDEX",
            SYMBOL_NAME="NIFTY",
            DISPLAY_NAME="Nifty 50",
        ),
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="2885",
            INSTRUMENT="EQUITY",
            SYMBOL_NAME="RELIANCE INDUSTRIES LTD",  # long name
            UNDERLYING_SYMBOL="RELIANCE",  # ticker → F&O join key + trading_symbol
            DISPLAY_NAME="Reliance",
            LOT_SIZE="1",
            TICK_SIZE="0.05",
        ),
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="3001",
            INSTRUMENT="OPTSTK",
            SYMBOL_NAME="RELIANCE-OPT",
            DISPLAY_NAME="Rel CE",
            LOT_SIZE="250",
            TICK_SIZE="0.05",
            OPTION_TYPE="CE",
            UNDERLYING_SYMBOL="RELIANCE",
            STRIKE_PRICE="2600",
            SM_EXPIRY_DATE="2025-01-30",
        ),
        # NIFTY has a listed INDEX option (OPTIDX) → option-pullable. Without this
        # leg the option-pull guard (DEC-OPTIONS-ELIGIBLE-GUARD-001) would tag NIFTY
        # spot-only — these fixtures mirror reality where NIFTY DOES have a chain.
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="3002",
            INSTRUMENT="OPTIDX",
            SYMBOL_NAME="NIFTY-OPT",
            DISPLAY_NAME="Nifty CE",
            LOT_SIZE="75",
            TICK_SIZE="0.05",
            OPTION_TYPE="CE",
            UNDERLYING_SYMBOL="NIFTY",
            STRIKE_PRICE="23000",
            SM_EXPIRY_DATE="2025-01-30",
        ),
    ]


class _Store:
    def __init__(self, pruned: int = 5) -> None:
        self.upserts: list[dict[str, object]] = []
        self.prune_calls = 0
        self._pruned = pruned
        self.runs: list[InstrumentRun] = []

    def upsert_many(
        self,
        instruments: Sequence[Instrument],
        *,
        snapshot_date: date,
        run_id: str,
        csv_source_sha256: str,
        reference_only_of: object = None,
        replace_snapshot: bool,
    ) -> object:
        insts = list(instruments)
        # Capture which rows the caller's reference_only_of would tag (Stage 2b
        # PR-4c) so tests can assert the tag without a real DuckDB.
        ref_ids = (
            frozenset(i.security_id for i in insts if reference_only_of(i))  # type: ignore[operator]
            if callable(reference_only_of)
            else frozenset()
        )
        self.upserts.append(
            {
                "instruments": insts,
                "snapshot_date": snapshot_date,
                "run_id": run_id,
                "csv_source_sha256": csv_source_sha256,
                "reference_only_of": reference_only_of,
                "reference_only_ids": ref_ids,
                "replace_snapshot": replace_snapshot,
            }
        )
        return object()

    def prune_to_latest_snapshot(self) -> int:
        self.prune_calls += 1
        return self._pruned

    def record_run(self, run: InstrumentRun) -> None:
        self.runs.append(run)


def test_refresh_persists_universe_full_snapshot() -> None:
    store = _Store(pruned=7)
    text = _csv(_rows())
    result = refresh_instruments(
        store,
        lambda _url: text,
        snapshot_date=date(2026, 5, 31),
        run_id="run-1",
    )
    assert len(store.upserts) == 1
    up = store.upserts[0]
    assert up["replace_snapshot"] is True
    assert up["snapshot_date"] == date(2026, 5, 31)
    assert up["run_id"] == "run-1"
    assert up["csv_source_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    persisted = up["instruments"]
    assert isinstance(persisted, list)
    assert sorted(i.symbol_name for i in persisted) == [
        "NIFTY",
        "RELIANCE",
    ]  # option leg dropped
    assert store.prune_calls == 1
    assert result == RefreshResult(
        parsed=4,  # NSE/BSE rows seen: NIFTY + RELIANCE + RELIANCE-OPT + NIFTY-OPT
        persisted=2,  # universe upserted: NIFTY + RELIANCE
        legs_dropped=2,  # both option legs, not persisted in the lean universe
        filtered=0,  # no out-of-scope (MCX) rows here
        snapshot_date=date(2026, 5, 31),
        csv_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        pruned=7,
    )


def test_refresh_uses_custom_url() -> None:
    store = _Store()
    seen: list[str] = []

    def _get(url: str) -> str:
        seen.append(url)
        return _csv(_rows())

    refresh_instruments(
        store,
        _get,
        snapshot_date=date(2026, 5, 31),
        run_id="r",
        url="https://images.dhan.co/x.csv",
    )
    assert seen == ["https://images.dhan.co/x.csv"]


def test_refresh_records_instrument_run() -> None:
    """The load is recorded in instrument_runs with the parse census (DEC-INSTRUMENT-RUN-AUDIT-001)."""
    store = _Store()
    text = _csv(_rows())
    refresh_instruments(store, lambda _url: text, snapshot_date=date(2026, 5, 31), run_id="run-1")
    assert len(store.runs) == 1
    run = store.runs[0]
    assert run.run_id == "run-1"
    assert run.snapshot_date == date(2026, 5, 31)
    assert run.csv_source_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert run.total_raw_rows == 4
    assert run.integrity_failures == 0
    assert run.in_scope_count == 4
    assert run.out_of_scope_count == 0
    assert run.index_count == 1  # NIFTY
    assert run.futures_count == 0  # no future leg in this fixture
    assert run.options_count == 2  # RELIANCE-OPT + NIFTY-OPT
    assert run.nse_count == 4
    assert run.bse_count == 0
    assert run.schema_version == SCHEMA_VERSION
    assert run.fetched_at.tzinfo is not None  # default clock is UTC-aware


def test_refresh_run_uses_injected_clock() -> None:
    """A fixed clock makes ``fetched_at`` deterministic (Rule #4)."""
    store = _Store()
    fixed = datetime(2026, 5, 31, 9, 15, tzinfo=UTC)
    refresh_instruments(
        store,
        lambda _url: _csv(_rows()),
        snapshot_date=date(2026, 5, 31),
        run_id="run-1",
        clock=lambda: fixed,
    )
    assert store.runs[0].fetched_at == fixed


def test_code_commit_sha_env_then_version_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """``BRUTEX_COMMIT_SHA`` wins; empty/absent falls back to ``__version__``."""
    monkeypatch.setenv("BRUTEX_COMMIT_SHA", "deadbeefcafe")
    assert _code_commit_sha() == "deadbeefcafe"
    monkeypatch.delenv("BRUTEX_COMMIT_SHA", raising=False)
    assert _code_commit_sha() == __version__
    monkeypatch.setenv("BRUTEX_COMMIT_SHA", "")  # empty → falsy → fallback
    assert _code_commit_sha() == __version__


def _rows_with_reference() -> list[dict[str, str]]:
    rows = _rows()
    rows.append(
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="9999",
            INSTRUMENT="EQUITY",
            SYMBOL_NAME="TINY CO LTD",
            DISPLAY_NAME="Tiny Co",
            LOT_SIZE="1",
            TICK_SIZE="0.05",
        )  # non-F&O (no UNDERLYING_SYMBOL)
    )
    return rows


def test_refresh_admits_and_tags_reference_constituents() -> None:
    # DEC-NIFTY-TOTAL-MARKET-PERSIST-001: a non-F&O equity is admitted by its id
    # AND tagged reference_only=TRUE at upsert.
    store = _Store()
    text = _csv(_rows_with_reference())
    refresh_instruments(
        store,
        lambda _url: text,
        snapshot_date=date(2026, 6, 6),
        run_id="run-ref",
        reference_security_ids=frozenset({9999}),
    )
    up = store.upserts[0]
    persisted = up["instruments"]
    assert isinstance(persisted, list)
    assert "TINY CO LTD" in [i.symbol_name for i in persisted]
    # The constituent is tagged reference_only (VIX absent from these rows).
    assert up["reference_only_ids"] == frozenset({9999})


def test_refresh_default_does_not_admit_or_tag_reference() -> None:
    store = _Store()
    text = _csv(_rows_with_reference())
    refresh_instruments(
        store, lambda _url: text, snapshot_date=date(2026, 6, 6), run_id="run-noref"
    )
    up = store.upserts[0]
    persisted = up["instruments"]
    assert isinstance(persisted, list)
    assert "TINY CO LTD" not in [i.symbol_name for i in persisted]
    # The tagger is ALWAYS supplied now (the option-pull guard must run on every
    # refresh, DEC-OPTIONS-ELIGIBLE-GUARD-001) — it still applies the VIX default
    # internally. No reference set + every admitted index here (NIFTY) has options
    # → nothing tagged reference_only.
    assert callable(up["reference_only_of"])
    assert up["reference_only_ids"] == frozenset()


def _rows_with_ntm_index() -> list[dict[str, str]]:
    rows = _rows()
    rows.append(
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="46",  # verified Dhan id (web.dhan.co/...secid=46)
            INSTRUMENT="INDEX",
            SYMBOL_NAME="NIFTY TOTAL MKT",
            DISPLAY_NAME="Nifty Total Market",
        )
    )
    return rows


def test_refresh_admits_and_tags_ntm_index_reference_only() -> None:
    # DEC-NIFTY-TOTAL-MARKET-DISCOVER-001: the NTM index is admitted via
    # extra_index_symbols AND tagged reference_only=TRUE (its id is in the
    # reference set). The brute-force engine is unaffected — it stays an IDX
    # outside the hardcoded 3-index brute set.
    store = _Store()
    text = _csv(_rows_with_ntm_index())
    refresh_instruments(
        store,
        lambda _url: text,
        snapshot_date=date(2026, 6, 6),
        run_id="run-ntm",
        reference_security_ids=frozenset({46}),
        extra_index_symbols=frozenset({"NIFTY TOTAL MKT"}),
    )
    up = store.upserts[0]
    persisted = up["instruments"]
    assert isinstance(persisted, list)
    assert "NIFTY TOTAL MKT" in [i.symbol_name for i in persisted]
    assert up["reference_only_ids"] == frozenset({46})


def test_refresh_default_does_not_admit_ntm_index() -> None:
    store = _Store()
    text = _csv(_rows_with_ntm_index())
    refresh_instruments(
        store, lambda _url: text, snapshot_date=date(2026, 6, 6), run_id="run-nontm"
    )
    persisted = store.upserts[0]["instruments"]
    assert isinstance(persisted, list)
    assert "NIFTY TOTAL MKT" not in [i.symbol_name for i in persisted]


def test_refresh_does_not_tag_fno_eligible_constituent_reference_only() -> None:
    # DEC-OPTIONS-PULL-GUARD-001: RELIANCE is F&O-eligible (it has an OPTSTK leg in
    # _rows) AND we pass it in reference_security_ids (as if it were an NTM
    # constituent). It must NOT be tagged reference_only — a first-class F&O stock
    # keeps its option chain. So reference_only_ids comes back EMPTY here.
    store = _Store()
    text = _csv(_rows())  # RELIANCE (2885) + its OPTSTK leg → F&O-eligible
    refresh_instruments(
        store,
        lambda _url: text,
        snapshot_date=date(2026, 6, 6),
        run_id="run-fno-ref",
        reference_security_ids=frozenset({2885}),  # RELIANCE's id
    )
    up = store.upserts[0]
    persisted = up["instruments"]
    assert isinstance(persisted, list)
    assert "RELIANCE" in [i.symbol_name for i in persisted]
    # F&O-eligible → excluded from reference_only despite being in the reference set.
    assert up["reference_only_ids"] == frozenset()


def _rows_with_spot_only_index() -> list[dict[str, str]]:
    # An admitted index with NO option leg — e.g. NIFTY 100 in reality. It is
    # spot-tradable but has no chain, so option-pull must be blocked.
    rows = _rows()
    rows.append(
        _row(
            EXCH_ID="NSE",
            SECURITY_ID="55",
            INSTRUMENT="INDEX",
            SYMBOL_NAME="NIFTY 100",
            DISPLAY_NAME="Nifty 100",
        )
    )
    return rows


def test_refresh_tags_spot_only_index_reference_only() -> None:
    # DEC-OPTIONS-ELIGIBLE-GUARD-001: an admitted index with NO listed OPTIDX leg
    # (NIFTY 100) is tagged reference_only so a mis-click can't waste Dhan quota on
    # an empty option pull — while NIFTY (which HAS an OPTIDX leg) stays pullable.
    store = _Store()
    text = _csv(_rows_with_spot_only_index())
    refresh_instruments(
        store, lambda _url: text, snapshot_date=date(2026, 6, 9), run_id="run-spot-idx"
    )
    up = store.upserts[0]
    persisted = up["instruments"]
    assert isinstance(persisted, list)
    names = [i.symbol_name for i in persisted]
    assert "NIFTY 100" in names  # admitted (spot is fine)
    assert "NIFTY" in names
    by_name = {i.symbol_name: i for i in persisted}
    # NIFTY 100 (no chain) is tagged reference_only; NIFTY (has chain) is not.
    ref_of = up["reference_only_of"]
    assert callable(ref_of)
    assert ref_of(by_name["NIFTY 100"]) is True
    assert ref_of(by_name["NIFTY"]) is False
    assert by_name["NIFTY 100"].security_id in up["reference_only_ids"]
    assert by_name["NIFTY"].security_id not in up["reference_only_ids"]
