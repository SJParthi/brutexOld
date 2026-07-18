"""Generate the FROZEN pre-conversion oracle fixture for DEC-RUST-RUNID-STORES-001.

Captures the PRE-Rust-conversion Python outputs of the four L14 lane surfaces —
``canonicalise_params`` / ``combo_hash`` (``brutex/strategies/combo.py``),
``data_snapshot_hash`` (``brutex/engine/snapshot_hash.py``), the results-store
append/merge law (``brutex/engine/results_store.py``) + ``top_k`` ordering, and
the window-ledger merge law + legacy-column derivations
(``brutex/data/window_ledger_store.py``) — into
``tests/golden/fixtures/runid_stores_oracle_v1.json``.

The committed fixture is the FROZEN correctness oracle the Rust conversion
(``rust/brutex-identity``) must reproduce: it was generated from the last
all-Python implementation, so ``tests/golden/test_runid_stores_oracle_golden.py``
asserting the CURRENT implementation against it proves the conversion changed
no observable value on these laws (the greenfield golden-regeneration
sanction of the lane covers only the surfaces where parity is *unreachable* —
the window-ledger parquet BYTES, re-baselined separately; every law captured
HERE is byte/value-parity, proven not regenerated).

Refuses to overwrite the committed fixture unless ``BRUTEX_REGEN_RUNID_ORACLE=1``
(the ``generate_preopen``/``BRUTEX_REGEN_PREOPEN_GOLDEN`` precedent), so the
frozen pre-conversion capture can never be silently clobbered by a
post-conversion re-run.

Run: ``python tests/golden/generate_runid_stores_fixtures.py``
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, TypedDict

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "runid_stores_oracle_v1.json"

_IST = timezone(timedelta(hours=5, minutes=30))
_ODD = timezone(timedelta(hours=1, minutes=2, seconds=3))  # sub-minute offset


def canonical_inputs() -> list[tuple[str, dict[str, Any]]]:
    """The hostile params corpus INPUTS — ONE source shared with the golden test
    (``tests/golden/test_runid_stores_oracle_golden.py``), so the asserted cases
    can never drift from the captured ones (CLAUDE.md §11 COMMON)."""
    return [
        ("empty", {}),
        (
            "flat_scalars",
            {"i": 42, "b": True, "n": None, "s": "plain", "f": 1.5, "d": Decimal("1.5")},
        ),
        (
            "float_boundaries",
            {
                "zero": 0.0,
                "neg_zero": -0.0,
                "one_e16": 1e16,
                "one_e15": 1e15,
                "one_e_neg4": 1e-4,
                "one_e_neg5": 1e-5,
                "denormal_min": 5e-324,
                "max": 1.7976931348623157e308,
                "point_one": 0.1,
                "long": 123456789012345.6,
                "sci_neg": 2.5e-10,
                "one_e100": 1e100,
                "half": 0.5,
                "seventeen_sig": 1.2345678901234567,
                "big_odd": 9007199254740993.0,
                "tiny_norm": 2.2250738585072014e-308,
            },
        ),
        (
            "decimal_scales",
            {
                "plain": Decimal("1.5"),
                "scaled": Decimal("1.00"),
                "zero": Decimal("0"),
                "neg_zero": Decimal("-0"),
                "sci_up": Decimal("1E+5"),
                "small": Decimal("0.000001"),
                "sci_dn": Decimal("1.5e-7"),
                "neg": Decimal("-12.345"),
            },
        ),
        (
            "int_extremes",
            {
                "zero": 0,
                "i64_max": 2**63 - 1,
                "i64_min": -(2**63),
                "u64_edge": 2**64,
                "big": 2**200,
                "neg_big": -(3**150),
            },
        ),
        (
            "strings_hostile",
            {
                "unicode": "µ-सूचकांक-指数",
                "astral": "mask😀tail",
                "controls": "line\nfeed\ttab\rret",
                "nul": "nul\x00byte",
                "quotes": 'quo"te\\back',
                "del_7f": "del\x7fchar",
                "solidus": "a/b",
                "bell": "\x07",
                "high_latin": "caf\xe9",
            },
        ),
        (
            "nested",
            {
                "outer": {"z": [1, 2.5, "three", None], "a": {"deep": (True, False)}},
                "tuple": (1, "two"),
                "range": range(3),
                "list_of_dict": [{"k2": 2, "k1": 1}],
                "empty_list": [],
                "empty_dict": {},
            },
        ),
        (
            "key_sort_unicode",
            {"b": 1, "a": 2, "B": 3, "\xe9": 4, "z": 5, "0": 6},
        ),
        (
            "sweep_realistic",
            {
                "sl_paisa": 2000,
                "tp_paisa": 4000,
                "prior_n": 20,
                "start_equity_paisa": 10_000_000,
                "fill_mode": "worst_case",
                "proximity": {"pivot_bps": 25, "pdh_pdl_bps": 15, "fib_bps": 10},
                "roll_schedule": [738521, 738549, 738577],
            },
        ),
    ]


def _canonical_corpus() -> list[dict[str, Any]]:
    """The hostile params corpus for ``canonicalise_params`` (JSON-string oracle)."""
    from brutex.strategies.combo import canonicalise_params

    return [
        {"name": name, "expected_json": canonicalise_params(params)}
        for name, params in canonical_inputs()
    ]


def combo_inputs() -> list[tuple[str, dict[str, Any]]]:
    """The 9-field ``combo_hash`` corpus INPUTS (each a full kwargs dict) —
    ONE source shared with the golden test (CLAUDE.md §11 COMMON)."""
    base: dict[str, Any] = {
        "strategy_id": "mask:%032x" % ((1 << 7) | (1 << 42)),
        "direction": "bull",
        "instrument": "NSE-NIFTY",
        "tf": "5minute",
        "mode": "A",
        "params": {"sl_paisa": 2000, "tp_paisa": 4000, "fill_mode": "worst_case"},
        "data_snapshot_hash": "ab" * 32,
        "indicator_version": "trading-core-0.3.2",
        "code_commit_sha": "deadbeef" * 5,
    }
    variants: list[tuple[str, dict[str, Any]]] = [
        ("base", {}),
        ("direction", {"direction": "bear"}),
        ("instrument", {"instrument": "BSE-SENSEX"}),
        ("unicode_field", {"instrument": "NSE-NIFTY-µ"}),
        (
            "params_rich",
            {
                "params": {
                    "f": 1e16,
                    "d": Decimal("22.50"),
                    "nested": {"b": [1, 2], "a": None},
                    "neg_zero": -0.0,
                }
            },
        ),
        ("empty_params", {"params": {}}),
        ("mode", {"mode": "B"}),
    ]
    return [(name, {**base, **override}) for name, override in variants]


def _combo_corpus() -> list[dict[str, Any]]:
    """9-field ``combo_hash`` digests over the hostile corpus."""
    from brutex.strategies.combo import combo_hash

    return [{"name": name, "expected_hex": combo_hash(**kwargs)} for name, kwargs in combo_inputs()]


def snapshot_inputs() -> list[tuple[str, dict[str, Any]]]:
    """The ``data_snapshot_hash`` corpus INPUTS (each a full kwargs dict of real
    :class:`~brutex.data.models.CandleBar` slices) — ONE source shared with the
    golden test (CLAUDE.md §11 COMMON)."""
    from brutex.data.models import CandleBar

    def bar(
        ts: datetime,
        o: str,
        h: str,
        lo: str,
        c: str,
        v: int,
        oi: int | None,
    ) -> CandleBar:
        return CandleBar(
            timestamp=ts,
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(lo),
            close=Decimal(c),
            volume=v,
            open_interest=oi,
        )

    utc1 = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)
    cases: list[tuple[str, dict[str, Any]]] = [
        (
            "empty_slices",
            {
                "exchange": "NSE",
                "segment": "INDEX",
                "start": "2024-01-01",
                "end": "2024-02-01",
                "slices": {},
            },
        ),
        (
            "one_empty_slice",
            {
                "exchange": "NSE",
                "segment": "INDEX",
                "start": "2024-01-01",
                "end": "2024-02-01",
                "slices": {("NSE-NIFTY", "1minute"): []},
            },
        ),
        (
            "utc_bars",
            {
                "exchange": "NSE",
                "segment": "INDEX",
                "start": "2024-01-01",
                "end": "2024-02-01",
                "slices": {
                    ("NSE-NIFTY", "1minute"): [
                        bar(utc1, "21500.10", "21510.55", "21490.00", "21505.25", 0, None),
                        bar(
                            utc1 + timedelta(minutes=1),
                            "21505.25",
                            "21520.00",
                            "21500.10",
                            "21519.95",
                            12345,
                            None,
                        ),
                    ]
                },
            },
        ),
        (
            "ist_micros_oi",
            {
                "exchange": "NSE",
                "segment": "FNO",
                "start": "2024-03-01",
                "end": "2024-03-31",
                "slices": {
                    ("NSE-NIFTY-28Mar24-FUT", "1minute"): [
                        bar(
                            datetime(2024, 3, 5, 9, 15, 0, 123456, tzinfo=_IST),
                            "22000.00",
                            "22001.00",
                            "21999.00",
                            "22000.50",
                            750,
                            123456,
                        ),
                        bar(
                            datetime(2024, 3, 5, 9, 16, tzinfo=_IST),
                            "22000.50",
                            "22002.00",
                            "22000.00",
                            "22001.10",
                            0,
                            0,
                        ),
                    ]
                },
            },
        ),
        (
            "multi_slice_sort",
            {
                "exchange": "BSE",
                "segment": "INDEX",
                "start": "2020-01-01",
                "end": "2026-01-01",
                "slices": {
                    ("b-lower", "1minute"): [bar(utc1, "1.00", "1.00", "1.00", "1.00", 1, None)],
                    ("B-upper", "1minute"): [bar(utc1, "2.00", "2.00", "2.00", "2.00", 2, None)],
                    ("\xc9-latin", "1day"): [bar(utc1, "3.00", "3.00", "3.00", "3.00", 3, None)],
                    ("B-upper", "1day"): [],
                },
            },
        ),
        (
            "odd_offset",
            {
                "exchange": "NSE",
                "segment": "CASH",
                "start": "2024-01-01",
                "end": "2024-01-02",
                "slices": {
                    ("NSE-ODD", "1minute"): [
                        bar(
                            datetime(2024, 1, 2, 10, 0, tzinfo=_ODD),
                            "10.05",
                            "10.10",
                            "10.00",
                            "10.05",
                            5,
                            None,
                        )
                    ]
                },
            },
        ),
    ]
    return cases


def _snapshot_corpus() -> list[dict[str, Any]]:
    """``data_snapshot_hash`` (bxsnap2) digests over the synthetic bar corpora."""
    from brutex.engine.snapshot_hash import data_snapshot_hash

    return [
        {"name": name, "expected_hex": data_snapshot_hash(**kwargs)}
        for name, kwargs in snapshot_inputs()
    ]


def _result_row_kwargs(run_id: str, **override: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": run_id,
        "instrument": "NSE-NIFTY",
        "tf": "5minute",
        "direction": "bull",
        "mode": "A",
        "mask_bits": (1 << 7) | (1 << 42),
        "net_pnl_paisa": 150_000,
        "gross_profit_paisa": 400_000,
        "gross_loss_paisa": 250_000,
        "max_drawdown_paisa": 90_000,
        "expectancy_paisa": 1_500,
        "total_trades": 100,
        "winning_trades": 60,
        "losing_trades": 40,
        "sharpe_ratio": 1.25,
        "sortino_ratio": 1.5,
        "calmar_ratio": 0.8,
        "profit_factor": 1.6,
        "win_rate": 0.6,
        "max_drawdown_pct": 0.09,
        "avg_entry_price": Decimal("21500.10"),
        "avg_exit_price": Decimal("21515.35"),
        "deflated_sharpe": None,
        "vix_mean": None,
        "vix_min": None,
        "vix_max": None,
    }
    base.update(override)
    return base


def _row_to_jsonable(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in kwargs.items()}


def append_inputs() -> list[tuple[str, dict[str, Any] | None, dict[str, Any]]]:
    """The append/merge decision corpus INPUTS (existing kwargs | None, incoming
    kwargs) — ONE source shared with the golden test (CLAUDE.md §11 COMMON)."""
    rid = "a1" * 32
    return [
        ("fresh_insert", None, _result_row_kwargs(rid)),
        ("identical_noop", _result_row_kwargs(rid), _result_row_kwargs(rid)),
        (
            "vix_backfill",
            _result_row_kwargs(rid),
            _result_row_kwargs(rid, vix_mean=14.5, vix_min=12.0, vix_max=17.25),
        ),
        (
            "vix_partial_backfill",
            _result_row_kwargs(rid, vix_min=12.0),
            _result_row_kwargs(rid, vix_mean=14.5),
        ),
        (
            "vix_reverse_noop",
            _result_row_kwargs(rid, vix_mean=14.5, vix_min=12.0, vix_max=17.25),
            _result_row_kwargs(rid),
        ),
        (
            "vix_conflict",
            _result_row_kwargs(rid, vix_mean=14.5),
            _result_row_kwargs(rid, vix_mean=15.0),
        ),
        (
            "plain_conflict",
            _result_row_kwargs(rid),
            _result_row_kwargs(rid, net_pnl_paisa=150_001),
        ),
        (
            "gate_conflict_none_vs_float",
            _result_row_kwargs(rid),
            _result_row_kwargs(rid, deflated_sharpe=0.42),
        ),
        (
            "gate_conflict_float_vs_float",
            _result_row_kwargs(rid, deflated_sharpe=0.41),
            _result_row_kwargs(rid, deflated_sharpe=0.42),
        ),
        (
            "mixed_vix_and_metric_conflict",
            _result_row_kwargs(rid, vix_mean=14.5),
            _result_row_kwargs(rid, vix_mean=15.0, sharpe_ratio=9.9),
        ),
        (
            "price_diff_conflict",
            _result_row_kwargs(rid),
            _result_row_kwargs(rid, avg_entry_price=Decimal("21500.15")),
        ),
    ]


def _append_law_corpus() -> list[dict[str, Any]]:
    """The results-store append/merge decision law over row pairs."""
    from brutex.engine.results_store import (
        ResultRow,
        ResultsStore,
        ResultsStoreError,
        ResultsStoreGateConflictError,
        VixTrioConflictError,
    )

    out = []
    for name, existing_kw, incoming_kw in append_inputs():
        store = ResultsStore(path=Path("/dev/null"))
        if existing_kw is not None:
            store.append(ResultRow(**existing_kw))
        outcome: dict[str, Any] = {}
        try:
            changed = store.append(ResultRow(**incoming_kw))
        except VixTrioConflictError:
            outcome = {"kind": "vix_conflict"}
        except ResultsStoreGateConflictError:
            outcome = {"kind": "gate_conflict"}
        except ResultsStoreError:
            outcome = {"kind": "plain_conflict"}
        else:
            stored = store.get(str(incoming_kw["run_id"]))
            assert stored is not None
            outcome = {
                "kind": "ok",
                "changed": changed,
                "stored_vix": [stored.vix_mean, stored.vix_min, stored.vix_max],
            }
        out.append(
            {
                "name": name,
                "existing": None if existing_kw is None else _row_to_jsonable(existing_kw),
                "incoming": _row_to_jsonable(incoming_kw),
                "outcome": outcome,
            }
        )
    return out


def topk_inputs() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The ``top_k`` corpus INPUTS — (row kwargs, queries); ONE source shared
    with the golden test (CLAUDE.md §11 COMMON)."""
    rows = [
        _result_row_kwargs("aa" * 32, net_pnl_paisa=2**60, sharpe_ratio=1.0),
        _result_row_kwargs("bb" * 32, net_pnl_paisa=2**60 + 1, sharpe_ratio=1.0),
        _result_row_kwargs("cc" * 32, net_pnl_paisa=-5, sharpe_ratio=2.5),
        _result_row_kwargs("dd" * 32, net_pnl_paisa=0, sharpe_ratio=2.5),
        _result_row_kwargs("ee" * 32, net_pnl_paisa=2**60, sharpe_ratio=-0.5),
    ]
    queries = [
        {"k": 5, "by": "sharpe_ratio", "descending": True},
        {"k": 3, "by": "sharpe_ratio", "descending": False},
        {"k": 5, "by": "net_pnl_paisa", "descending": True},
        {"k": 2, "by": "total_trades", "descending": True},
    ]
    return rows, queries


def _topk_corpus() -> list[dict[str, Any]]:
    """``top_k`` ordering law incl. exact big-int ranking + run_id tie-breaks."""
    from brutex.engine.results_store import ResultRow, ResultsStore

    store = ResultsStore(path=Path("/dev/null"))
    rows, queries = topk_inputs()
    for kw in rows:
        store.append(ResultRow(**kw))
    out = []
    for q in queries:
        ranked = store.top_k(int(q["k"]), by=str(q["by"]), descending=bool(q["descending"]))
        out.append({**q, "expected_run_ids": [r.run_id for r in ranked]})
    return out


def _ledger_row(
    market: str = "groww",
    security_id: str = "NSE-NIFTY",
    interval: str = "1minute",
    window_start: str = "2024-01-01",
    window_end: str = "2024-01-31",
    status: str = "SUCCESS",
    bars: int = 375,
    attempts: int = 1,
    last_error: str | None = None,
    first_seen_at: str = "2024-01-31T16:00:00+00:00",
    last_attempt_at: str | None = "2024-01-31T16:00:00+00:00",
    empty_streak: int = 0,
    in_pull_fetches: int = 1,
    bars_net_new: int | None = 375,
    empty_retries: int | None = 0,
    transient_retries: int | None = 0,
) -> tuple[Any, ...]:
    return (
        market,
        security_id,
        interval,
        window_start,
        window_end,
        status,
        bars,
        attempts,
        last_error,
        first_seen_at,
        last_attempt_at,
        empty_streak,
        in_pull_fetches,
        bars_net_new,
        empty_retries,
        transient_retries,
    )


class _SecurityKw(TypedDict):
    """The one splatted `_ledger_row` kwarg — typed so `**k1` splats stay
    precisely checked (a bare `dict[str, str]` splat would type every int
    parameter as str under pyright strict)."""

    security_id: str


def ledger_merge_inputs() -> list[tuple[str, dict[str, Any]]]:
    """The merge-law corpus INPUTS — ONE source shared with the golden test
    (CLAUDE.md §11 COMMON)."""
    k1: _SecurityKw = {"security_id": "NSE-A"}
    k2: _SecurityKw = {"security_id": "NSE-B"}
    k3: _SecurityKw = {"security_id": "NSE-C"}
    cases: list[tuple[str, dict[str, Any]]] = [
        ("both_empty", {"memory_rows": [], "disk_rows": []}),
        (
            "memory_only_plus_disk_only",
            {
                "memory_rows": [_ledger_row(**k1)],
                "disk_rows": [_ledger_row(**k2, status="FAILED", last_error="boom")],
            },
        ),
        (
            "settled_beats_planned",
            {
                "memory_rows": [_ledger_row(**k1, status="PLANNED", bars=0, attempts=0)],
                "disk_rows": [_ledger_row(**k1, status="SUCCESS")],
            },
        ),
        (
            "newer_attempt_wins",
            {
                "memory_rows": [
                    _ledger_row(**k1, last_attempt_at="2024-02-01T10:00:00+00:00", bars=10)
                ],
                "disk_rows": [
                    _ledger_row(**k1, last_attempt_at="2024-01-31T10:00:00+00:00", bars=20)
                ],
            },
        ),
        (
            "attempts_then_full_row_tiebreak",
            {
                "memory_rows": [_ledger_row(**k1, attempts=2, bars=1)],
                "disk_rows": [_ledger_row(**k1, attempts=2, bars=2)],
            },
        ),
        (
            "none_last_attempt_sorts_oldest",
            {
                "memory_rows": [_ledger_row(**k1, last_attempt_at=None, status="EMPTY", bars=0)],
                "disk_rows": [_ledger_row(**k1, status="EMPTY", bars=0)],
            },
        ),
        (
            "unknown_status_ranks_below",
            {
                "memory_rows": [_ledger_row(**k1, status="GARBAGE")],
                "disk_rows": [_ledger_row(**k1, status="PLANNED", bars=0)],
            },
        ),
        (
            "pruned_disk_only_dropped",
            {
                "memory_rows": [_ledger_row(**k2)],
                "disk_rows": [_ledger_row(**k1), _ledger_row(**k3)],
                "pruned_keys": [["groww", "NSE-A", "1minute", "2024-01-01", "2024-01-31"]],
            },
        ),
        (
            "reopened_memory_wins",
            {
                "memory_rows": [_ledger_row(**k1, status="FAILED", last_error="reopened")],
                "disk_rows": [_ledger_row(**k1, status="SKIPPED", attempts=5)],
                "reopened_keys": [["groww", "NSE-A", "1minute", "2024-01-01", "2024-01-31"]],
            },
        ),
        (
            "degraded_memory_wins",
            {
                "memory_rows": [_ledger_row(**k1, status="FAILED", last_error="INTERIOR_GAP")],
                "disk_rows": [_ledger_row(**k1, status="SUCCESS", attempts=9)],
                "degraded_keys": [["groww", "NSE-A", "1minute", "2024-01-01", "2024-01-31"]],
            },
        ),
        (
            "none_optional_tail",
            {
                "memory_rows": [
                    _ledger_row(
                        **k1,
                        bars_net_new=None,
                        empty_retries=None,
                        transient_retries=None,
                        attempts=3,
                    )
                ],
                "disk_rows": [_ledger_row(**k1, attempts=3)],
            },
        ),
        (
            "sort_order_multi_key",
            {
                "memory_rows": [_ledger_row(**k3), _ledger_row(**k1, interval="1day")],
                "disk_rows": [_ledger_row(**k2), _ledger_row(**k1)],
            },
        ),
    ]
    return cases


def _ledger_merge_corpus() -> list[dict[str, Any]]:
    """``merge_ledger_rows`` outputs over conflict/tombstone corpora."""
    from brutex.data.window_ledger_store import merge_ledger_rows

    out = []
    for name, kwargs in ledger_merge_inputs():
        merged = merge_ledger_rows(
            [tuple(r) for r in kwargs["memory_rows"]],
            [tuple(r) for r in kwargs["disk_rows"]],
            pruned_keys=frozenset(tuple(k) for k in kwargs.get("pruned_keys", [])),
            reopened_keys=frozenset(tuple(k) for k in kwargs.get("reopened_keys", [])),
            degraded_keys=frozenset(tuple(k) for k in kwargs.get("degraded_keys", [])),
        )
        out.append(
            {
                "name": name,
                **{
                    k: [list(r) for r in v] if k.endswith("_rows") else v for k, v in kwargs.items()
                },
                "expected": [list(r) for r in merged],
            }
        )
    return out


def _legacy_derivation_corpus() -> list[dict[str, Any]]:
    """The legacy-column derivation laws (`_derive_*`) over the status vocabulary."""
    from brutex.data.window_ledger_store import (
        _derive_empty_streak,
        _derive_in_pull_fetches,
    )

    out = []
    for status in ("SUCCESS", "EMPTY", "FAILED", "PLANNED", "SKIPPED", "GARBAGE"):
        for attempts in (0, 1, 2, 3, 7):
            out.append(
                {
                    "status": status,
                    "attempts": attempts,
                    "empty_streak": _derive_empty_streak(status, attempts),
                    "in_pull_fetches": _derive_in_pull_fetches(status),
                    "bars_net_new": None,
                    "retry_split": None,
                }
            )
    return out


def _ledger_roundtrip_corpus() -> list[dict[str, Any]]:
    """Row fixed points through the CURRENT ledger codec — the #1917 law input.

    The captured `rows` are the fixed-point contract: whatever encoder writes the
    ledger, reading it back must yield EXACTLY these row tuples (the option-(iii)
    correctness law of the G10 re-baseline). The frozen fixture was captured
    through the PRE-conversion pyarrow codec; a sanctioned REGEN runs the
    canonical kernel codec — the captured `rows` are encoder-independent by the
    fixed-point law, so the two captures are identical.
    """
    import tempfile

    from brutex.data.window_ledger_store import _read_ledger_file_rows, render_ledger_parquet

    corpora = ledger_roundtrip_inputs()
    out = []
    for name, rows in corpora:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.parquet"
            path.write_bytes(render_ledger_parquet(rows))
            read_back = _read_ledger_file_rows(path)
        assert read_back == rows, f"ledger codec round-trip not a fixed point for {name}"
        out.append({"name": name, "rows": [list(r) for r in rows]})
    return out


def ledger_roundtrip_inputs() -> list[tuple[str, list[tuple[Any, ...]]]]:
    """The row-fixed-point corpora INPUTS — ONE source shared with the golden
    test (CLAUDE.md §11 COMMON)."""
    return [
        ("empty", []),
        (
            "mixed",
            [
                _ledger_row(),
                _ledger_row(
                    security_id="NSE-M&M",
                    status="FAILED",
                    last_error="429 rate limited\nline2\ttab",
                    bars=0,
                    attempts=3,
                    last_attempt_at=None,
                    bars_net_new=None,
                    empty_retries=None,
                    transient_retries=None,
                ),
                _ledger_row(
                    security_id="NSE-\xc9-unicode",
                    interval="1day",
                    status="EMPTY",
                    bars=0,
                    empty_streak=2,
                    bars_net_new=0,
                    empty_retries=2,
                    transient_retries=1,
                ),
            ],
        ),
    ]


def main() -> int:
    if FIXTURE_PATH.exists() and os.environ.get("BRUTEX_REGEN_RUNID_ORACLE") != "1":
        sys.stderr.write(
            f"REFUSED: {FIXTURE_PATH} exists — the frozen pre-conversion oracle is "
            "never silently overwritten. Set BRUTEX_REGEN_RUNID_ORACLE=1 to regenerate.\n"
        )
        return 2
    payload = {
        "schema_version": 1,
        "provenance": (
            "FROZEN pre-Rust-conversion Python outputs (DEC-RUST-RUNID-STORES-001); "
            "generated by tests/golden/generate_runid_stores_fixtures.py"
        ),
        "canonical_params": _canonical_corpus(),
        "combo_hash": _combo_corpus(),
        "snapshot_hash": _snapshot_corpus(),
        "append_law": _append_law_corpus(),
        "top_k": _topk_corpus(),
        "ledger_merge": _ledger_merge_corpus(),
        "ledger_legacy_derivations": _legacy_derivation_corpus(),
        "ledger_row_fixed_points": _ledger_roundtrip_corpus(),
    }
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(
        json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(f"wrote {FIXTURE_PATH}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
