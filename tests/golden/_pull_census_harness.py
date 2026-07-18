"""Shared case-enumeration harness for the pull-census goldens (DEC-RUST-PULL-STATE-001, Wave 3G).

Imported by BOTH ``generate_pull_census_golden.py`` (the one-shot freeze) and
``test_pull_census_golden.py`` (the recompute-and-compare gate) — the
``_pull_intent_harness`` shape, never generator-imports-test.

ONE table is computed from the LIVE ``brutex.dashboard.pull_census``: each
case drives the REAL :func:`build_written_vs_settled_census` over hostile row
/ span corpora (agreement, both mismatch classes, scope filters, zero-bar
spans, duplicate series, the 20-sample cap crossing both classes, unicode +
astral symbols pinning the SORT-ORDER law, non-SUCCESS statuses) and records
the full census verdict.  Each case also carries the MECHANICAL kernel-input
projection — the SCOPED ``(symbol, interval, is_success)`` rows + the scoped
bars>0 ``(symbol, interval)`` written pairs the future seam feeds the kernel
(the extraction/filter expressions themselves — ``str()``/``int()`` on
arbitrary payload objects — STAY Python; the kernel folds sets, sorts and
caps only).

The kernel TSV projects the SAME cases onto the dependency-free line format
the crate's own ``cargo test`` replays (strings as UTF-8 hex — the
cross-language pin without a serde/base64 dependency).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from brutex.dashboard.pull_census import CENSUS_SAMPLE_CAP, build_written_vs_settled_census

_NOW = datetime(2026, 7, 13, 18, 0, tzinfo=UTC)

#: Sample-kind codes (the FFI output contract; mirrored by the facade
#: constants + ``rust/brutex-pull-state/src/pull_census.rs``).
SAMPLE_SETTLED_UNWRITTEN = 0
SAMPLE_WRITTEN_UNSETTLED = 1


def _row(symbol: str, interval: str = "1minute", status: str = "SUCCESS") -> tuple[Any, ...]:
    """One serialised 13-tuple ledger row (only idx 1/2/5 are census-read)."""
    return (
        "groww",
        symbol,
        interval,
        "2026-06-01",
        "2026-07-01",
        status,
        5,
        1,
        None,
        _NOW.isoformat(),
        _NOW.isoformat(),
        0,
        1,
    )


def _span(symbol: str, interval: str = "1minute", bars: int = 100) -> Mapping[str, Any]:
    return {
        "exchange": "NSE",
        "segment": "INDEX",
        "symbol": symbol,
        "interval": interval,
        "bars": bars,
        "first_date": "2026-06-01",
        "last_date": "2026-06-30",
    }


def _kernel_inputs(
    ledger_rows: Sequence[tuple[Any, ...]],
    spans: Sequence[Mapping[str, Any]],
    intervals: Sequence[str],
    symbols: Sequence[str],
) -> tuple[list[tuple[str, str, bool]], list[tuple[str, str]]]:
    """The MECHANICAL seam projection: scoped rows + scoped written pairs.

    Mirrors the oracle's own extraction/filter expressions (``str()`` on the
    three row slots for EVERY row; span ``bars`` read only AFTER the scope
    filter — the lazy-`int()` law) — this is the exact prep the seam runs
    before consulting the kernel.
    """
    interval_set = set(intervals)
    symbol_set = set(symbols)
    scoped_rows: list[tuple[str, str, bool]] = []
    for row in ledger_rows:
        symbol, interval, status = str(row[1]), str(row[2]), str(row[5])
        if symbol not in symbol_set or interval not in interval_set:
            continue
        scoped_rows.append((symbol, interval, status == "SUCCESS"))
    written_pairs: list[tuple[str, str]] = []
    for span in spans:
        symbol, interval = str(span["symbol"]), str(span["interval"])
        if symbol not in symbol_set or interval not in interval_set:
            continue
        if int(span["bars"]) > 0:
            written_pairs.append((symbol, interval))
    return scoped_rows, written_pairs


def _cases() -> list[dict[str, Any]]:
    one = ("1minute",)
    cases: list[dict[str, Any]] = []

    def add(
        name: str,
        ledger_rows: Sequence[tuple[Any, ...]],
        spans: Sequence[Mapping[str, Any]],
        intervals: Sequence[str],
        symbols: Sequence[str],
    ) -> None:
        census = build_written_vs_settled_census(
            ledger_rows, spans, intervals=intervals, symbols=symbols
        )
        scoped_rows, written_pairs = _kernel_inputs(ledger_rows, spans, intervals, symbols)
        universe = {(r[0], r[1]) for r in scoped_rows} | set(written_pairs)
        kernel_samples: list[list[Any]] = []
        for rendered in census.samples:
            # MECHANICAL inversion by re-render: find the input pair whose
            # oracle f-string render equals the observed sample (never a
            # re-derivation of the fold law).
            for sym, iv in universe:
                if rendered == f"settled-unwritten: {sym!r} {iv!r}":
                    kernel_samples.append([SAMPLE_SETTLED_UNWRITTEN, sym, iv])
                    break
                if rendered == f"written-unsettled: {sym!r} {iv!r}":
                    kernel_samples.append([SAMPLE_WRITTEN_UNSETTLED, sym, iv])
                    break
            else:  # pragma: no cover - harness invariant
                raise AssertionError(f"unmatched sample {rendered!r}")
        cases.append(
            {
                "name": name,
                "kernel_rows": [list(r) for r in scoped_rows],
                "kernel_written": [list(p) for p in written_pairs],
                "kernel_samples": kernel_samples,
                "out": {
                    "settled_unwritten": census.settled_unwritten,
                    "written_unsettled": census.written_unsettled,
                    "samples": list(census.samples),
                    "scanned": census.scanned,
                    "mismatched": census.mismatched,
                },
            }
        )

    add("empty_inputs_clean", [], [], one, [])
    add(
        "agreeing_series_clean",
        [_row("NSE-NIFTY"), _row("NSE-BANKNIFTY")],
        [_span("NSE-NIFTY"), _span("NSE-BANKNIFTY")],
        one,
        ["NSE-NIFTY", "NSE-BANKNIFTY"],
    )
    add(
        "settled_unwritten_flagged",
        [_row("NSE-NIFTY"), _row("NSE-SENSEX")],
        [_span("NSE-NIFTY")],
        one,
        ["NSE-NIFTY", "NSE-SENSEX"],
    )
    add(
        "zero_bar_span_is_unwritten",
        [_row("NSE-NIFTY")],
        [_span("NSE-NIFTY", bars=0)],
        one,
        ["NSE-NIFTY"],
    )
    add(
        "written_unsettled_flagged",
        [_row("NSE-NIFTY", status="FAILED")],
        [_span("NSE-NIFTY"), _span("NSE-VIX")],
        one,
        ["NSE-NIFTY", "NSE-VIX"],
    )
    add(
        "interval_scope_guards_derived",
        [_row("NSE-NIFTY")],
        [_span("NSE-NIFTY"), _span("NSE-NIFTY", interval="1day"), _span("NSE-NIFTY", "5minute")],
        one,
        ["NSE-NIFTY"],
    )
    add(
        "symbol_scope_ignores_foreign",
        [_row("NSE-NIFTY"), _row("NSE-OTHER")],
        [_span("NSE-THIRD")],
        one,
        ["NSE-NIFTY"],
    )
    add(
        "duplicate_rows_and_spans_dedup",
        [_row("NSE-NIFTY"), _row("NSE-NIFTY"), _row("NSE-NIFTY", status="PLANNED")],
        [_span("NSE-NIFTY"), _span("NSE-NIFTY", bars=7)],
        one,
        ["NSE-NIFTY"],
    )
    add(
        "hyphen_ampersand_and_astral_sort",
        # The sort-order law: Python sorts (symbol, interval) tuples by code
        # point — UTF-8 byte order coincides (the kernel's BTreeSet law).
        [_row(s) for s in ("NSE-M&M", "NSE-BAJAJ-AUTO", "NSE-\U0001f40d", "NSE-É", "NSE-b")],
        [],
        one,
        ["NSE-M&M", "NSE-BAJAJ-AUTO", "NSE-\U0001f40d", "NSE-É", "NSE-b"],
    )
    cap = CENSUS_SAMPLE_CAP
    add(
        "sample_cap_spans_both_classes",
        [_row(f"NSE-SU{n:03d}") for n in range(cap - 5)],
        [_span(f"NSE-WU{n:03d}") for n in range(10)],
        one,
        [f"NSE-SU{n:03d}" for n in range(cap - 5)] + [f"NSE-WU{n:03d}" for n in range(10)],
    )
    add(
        "sample_cap_all_settled_unwritten",
        [_row(f"NSE-SU{n:03d}") for n in range(cap + 5)],
        [],
        one,
        [f"NSE-SU{n:03d}" for n in range(cap + 5)],
    )
    add(
        "multi_interval_series_are_distinct",
        [_row("NSE-NIFTY"), _row("NSE-NIFTY", interval="1day")],
        [_span("NSE-NIFTY", interval="1day")],
        ("1minute", "1day"),
        ["NSE-NIFTY"],
    )
    return cases


def compute_payload() -> dict[str, Any]:
    """The canonical golden payload — recomputed from the LIVE module."""
    return {"sample_cap": CENSUS_SAMPLE_CAP, "cases": _cases()}


def _hex(value: str) -> str:
    return value.encode("utf-8").hex() or "@empty"


def compute_tsv(payload: dict[str, Any]) -> str:
    """The serde-free kernel-case projection for the cargo fixture test.

    Strings travel as UTF-8 hex; list fields join on ``,`` with ``.``-joined
    subfields (hex is ``[0-9a-f]`` only, so both separators are safe);
    ``@none`` is the empty list.  Samples carry kind + symbol + interval —
    the ``repr()``-rendered strings stay a Python-side concern.
    """
    lines = ["# generated by tests/golden/generate_pull_census_golden.py — do not hand-edit"]
    for case in payload["cases"]:
        rows = (
            ",".join(f"{_hex(s)}.{_hex(i)}.{int(ok)}" for s, i, ok in case["kernel_rows"])
            or "@none"
        )
        written = (
            ",".join(f"{_hex(s)}.{_hex(i)}" for s, i in case["kernel_written"]) or "@none"
        )
        out = case["out"]
        samples = (
            ",".join(f"{kind}.{_hex(sym)}.{_hex(iv)}" for kind, sym, iv in case["kernel_samples"])
            or "@none"
        )
        lines.append(
            "\t".join(
                (
                    "census",
                    case["name"],
                    rows,
                    written,
                    str(out["settled_unwritten"]),
                    str(out["written_unsettled"]),
                    samples,
                )
            )
        )
    return "\n".join(lines) + "\n"
