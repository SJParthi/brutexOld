"""Written-vs-settled census — an OBSERVE-ONLY ledger↔lake agreement check.

DEC-WRITTEN-SETTLED-CENSUS-001. The pull records every window's outcome in the
durable window ledger AND writes the fetched bars to the Parquet lake — two
stores that must agree but are never cross-checked post hoc: a ledger row can
say ``SUCCESS`` while the lake holds NOTHING for that series (a manually
deleted / lost partition — the silent shape of the M&M class), or the lake can
hold bars the ledger never settled (an interrupted pull whose ledger save was
lost — those windows re-fetch, which is safe, but the operator never SEES the
disagreement). This module is the bounded first step: a **series-level**
``(symbol, interval)`` census comparing the ledger's settled ``SUCCESS`` rows
against the lake spans the pull ALREADY loaded for its cold-start reconcile —
zero extra I/O, pure fold, DETECT + WARN only (the #919 / #1014 observe-only
pattern; NO auto-remediation, NO ledger/status writes, NO vendor calls).

Honest envelope (Golden Rule #10)
---------------------------------
* **Series-level, NOT per-window**: a per-window enclosure test needs
  trading-calendar-aware tail handling (a tile ending just after a weekend has
  a non-trading last covered day, so a naive ``window_end − 1 ≤ last_date``
  check would false-flag every trailing window of every series) — the
  per-window refinement is a NAMED follow-up.
* **Pre-pull spans**: the seams reuse the spans loaded BEFORE the pull's
  fetching (the sketch's "already-loaded spans"), and the ledger rows are
  snapshotted at the SAME point — both sides are pre-pull-consistent, so THIS
  pull's fresh writes/settles are OUT of the census envelope (they cannot
  false-flag each other); re-loading fresh post-drain spans to also census the
  current pull's writes is a NAMED follow-up.
* Samples are capped at :data:`CENSUS_SAMPLE_CAP` (the
  ``option_ingest._log_underlying_all_empty`` cap pattern), ``repr()``-named.

DEC-RUST-PULL-STATE-001 (flip phase): the fold/sort/cap decision plane IS the
``brutex_pull_state`` Rust kernel
(:func:`brutex.pull_state_rust.rust_census_fold`); the former byte-identical
Python fold body is DELETED. The extraction + scope-filter expressions (the
``str()``/``int()`` conversions over arbitrary payload objects and the
lazy-``int()``-after-the-scope-filter law) and the ``repr()`` sample
rendering STAY Python — input translation and string composition, never the
fold law. A missing/stale/raising wheel — or the removed
``BRUTEX_PULL_STATE_RUST`` knob still set — raises the typed error out of
:func:`build_written_vs_settled_census`; the observe-only wiring
(:func:`emit_written_settled_census`) folds ANY internal error, kernel faults
included, into its guarded ``pull.written_settled_census_error`` WARN — the
census can never break the pull (the pre-existing wiring contract).

ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX); F&O lake stays REFERENCE_ONLY.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from brutex import pull_state_rust as _pull_state_rust

_LOG = structlog.get_logger("brutex.dashboard.pull_census")

#: Max mismatch samples carried on the census / named in the log line — the
#: same bounded-sample discipline as the #919 all-empty detector's cap.
CENSUS_SAMPLE_CAP: int = 20

#: The ledger row statuses that assert "this series has stored data".
_SUCCESS_STATUS: str = "SUCCESS"


@dataclass(frozen=True, slots=True)
class WrittenSettledCensus:
    """One census verdict — immutable, log/report-friendly.

    Attributes
    ----------
    settled_unwritten
        Scoped ``(symbol, interval)`` series with ≥ 1 ledger ``SUCCESS`` row
        but NO lake span holding bars — the ledger claims data the lake does
        not have (the loud one: a lost/deleted partition).
    written_unsettled
        Scoped series whose lake span holds bars but whose ledger carries NO
        ``SUCCESS`` row — the lake holds data the ledger never settled (safe —
        those windows re-fetch idempotently — but worth seeing).
    samples
        Up to :data:`CENSUS_SAMPLE_CAP` ``repr()``-named ``"<kind>: <symbol>
        <interval>"`` mismatch samples, deterministic order.
    scanned
        Ledger rows examined (the scoped subset) — the census's honest work
        figure.
    """

    settled_unwritten: int
    written_unsettled: int
    samples: tuple[str, ...]
    scanned: int

    @property
    def mismatched(self) -> bool:
        """``True`` iff the census found ANY disagreement."""
        return bool(self.settled_unwritten or self.written_unsettled)


def build_written_vs_settled_census(
    ledger_rows: Sequence[tuple[Any, ...]],
    spans: Sequence[Mapping[str, Any]],
    *,
    intervals: Collection[str],
    symbols: Collection[str],
) -> WrittenSettledCensus:
    """Fold ledger rows + lake spans into a series-level agreement census — pure.

    Parameters
    ----------
    ledger_rows
        Serialised ledger rows (:meth:`~brutex.data.window_ledger.WindowLedger.to_rows`
        13-tuples — only ``security_id`` (idx 1), ``interval`` (idx 2) and
        ``status`` (idx 5) are read).
    spans
        The :func:`~brutex.dashboard.coverage_reconcile.load_lake_spans` shape —
        one aggregate dict per ``bars/`` partition set with ``symbol`` /
        ``interval`` / ``bars`` keys.
    intervals
        The FETCHED interval scope (``("1minute",)`` in production). This
        filter is LOAD-BEARING: the span list also carries the DERIVED
        partitions (``1day`` + the 19 intraday TFs, DEC-1MINUTE-ONLY-DERIVE-1DAY-001),
        which legitimately have NO ledger rows — without the scope every
        derived partition would read written-but-unsettled.
    symbols
        The pulled symbol scope (spot ``symbol_name`` / F&O contract
        ``groww_symbol`` — the SAME key the lake partition dir and the ledger
        ``security_id`` both carry). Out-of-scope series are ignored (a prior
        pull's other universe must not flood this pull's census).

    Returns
    -------
    WrittenSettledCensus
        The series-level verdict; O(rows + spans) with O(1) lookups.

    Raises
    ------
    brutex.pull_state_rust.PullStateKernelError
        Wheel absent/stale, kernel raise, or a malformed fold result —
        fail-closed, no oracle exists (DEC-RUST-PULL-STATE-001).
    brutex.pull_state_rust.PullStateFlagRemovedError
        The removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    """
    # --- pull_census (Wave 3G, flip phase — DEC-RUST-PULL-STATE-001): the
    # fold IS the Rust kernel, consulted over the pre-extracted,
    # scope-filtered inputs (the extraction expressions below are the
    # pre-flip oracle's own, run in the oracle's own order — a raising
    # ``str()``/``int()``/short row raises identically); the ``repr()``
    # sample strings are rendered HERE from the returned triples.
    interval_set = set(intervals)
    symbol_set = set(symbols)
    scoped_rows: list[tuple[str, str, bool]] = []
    scanned = 0
    for row in ledger_rows:
        symbol, interval, status = str(row[1]), str(row[2]), str(row[5])
        if symbol not in symbol_set or interval not in interval_set:
            continue
        scanned += 1
        scoped_rows.append((symbol, interval, status == _SUCCESS_STATUS))
    written_pairs: list[tuple[str, str]] = []
    for span in spans:
        symbol, interval = str(span["symbol"]), str(span["interval"])
        if symbol not in symbol_set or interval not in interval_set:
            continue
        if int(span["bars"]) > 0:
            written_pairs.append((symbol, interval))
    settled_unwritten, written_unsettled, triples = _pull_state_rust.rust_census_fold(
        scoped_rows, written_pairs
    )
    samples: list[str] = []
    for kind, symbol, interval in triples:
        prefix = (
            "settled-unwritten"
            if kind == _pull_state_rust.SAMPLE_KIND_SETTLED_UNWRITTEN
            else "written-unsettled"
        )
        samples.append(f"{prefix}: {symbol!r} {interval!r}")
    return WrittenSettledCensus(
        settled_unwritten=settled_unwritten,
        written_unsettled=written_unsettled,
        samples=tuple(samples),
        scanned=scanned,
    )


def emit_written_settled_census(
    ledger_rows: Sequence[tuple[Any, ...]],
    spans: Sequence[Mapping[str, Any]],
    *,
    intervals: Collection[str],
    symbols: Collection[str],
    source: str,
) -> None:
    """Run the census + log the verdict — GUARDED, can NEVER break the pull.

    One WARNING ``pull.written_settled_mismatch`` iff any disagreement (naming
    the capped samples), else one DEBUG ``pull.written_settled_census``. Any
    internal error — a pull-state kernel fault included (the wheel absent /
    stale / raising, DEC-RUST-PULL-STATE-001; the census is observe-only, so
    its fail-closed raise folds into the guard here, never breaks the pull) —
    logs ``pull.written_settled_census_error`` and returns: the observe-only
    wiring contract of ``emit_underlying_all_empty`` (#1014). *source* names
    the emitting spine (``"spot"`` / ``"fno"``) for triage.
    """
    try:
        census = build_written_vs_settled_census(
            ledger_rows, spans, intervals=intervals, symbols=symbols
        )
        if census.mismatched:
            _LOG.warning(
                "pull.written_settled_mismatch",
                source=source,
                settled_unwritten=census.settled_unwritten,
                written_unsettled=census.written_unsettled,
                scanned=census.scanned,
                samples=list(census.samples),
                note=(
                    "ledger settled-SUCCESS vs lake-span disagreement (series-level, "
                    "pre-pull snapshot) — observe-only, nothing was changed"
                ),
            )
        else:
            _LOG.debug(
                "pull.written_settled_census",
                source=source,
                scanned=census.scanned,
            )
    except Exception as exc:  # pylint: disable=broad-exception-caught — observe-only guard
        _LOG.warning("pull.written_settled_census_error", source=source, error=str(exc))


__all__ = [
    "CENSUS_SAMPLE_CAP",
    "WrittenSettledCensus",
    "build_written_vs_settled_census",
    "emit_written_settled_census",
]
