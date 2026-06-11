"""AUTOMATIC derived-timeframe materialisation on pull settle (DEC-TF-MATERIALIZE-002).

Supersedes the "manual only" stance of DEC-TF-MATERIALIZE-001: when a spot
pull settles with NEW 1-minute bars for a symbol, the 19 derived intraday
timeframes can be re-materialised for that symbol automatically — zero
clicks (CLAUDE.md §2 rule 11 AUTOMATED), reusing
:func:`~brutex.data.timeframe_materialize.materialize_timeframes` wholesale
(rule 11 COMMON; this module never re-implements derivation or lake I/O).

## OPT-IN, default OFF (operator storage decision 2026-06-11)

Auto-derivation runs ONLY when ``BRUTEX_AUTO_DERIVE_TFS=1`` (the literal
``"1"``, mirroring the ``BRUTEX_M4PRO_STRICT`` strict-decoder pattern). The
operator runs 1m-only on the internal SSD today; the full 19-TF ladder
materialises only once ``$BRUTEX_LAKE_ROOT`` points at the external 8 TB
drive and the operator flips this switch. Storage stays operator-approved,
exactly as DEC-TF-MATERIALIZE-001 demanded — the approval is now a single
explicit env flip instead of a per-symbol click.

## Quarantine

One symbol's failure (corrupt partition, schema drift) NEVER sinks the
rest, and auto-derivation NEVER fails the pull that triggered it — the
caller logs the report and moves on. Mirrors the bulk-pull fan-out
discipline.

## Freshness compare (the doctor probe core)

:func:`compare_derived_freshness` is the pure heart of the ``brutex
doctor`` derived-freshness check: re-derive a stored timeframe from the 1m
base via the canonical session-anchored resampler and compare
frame-for-frame. Any drift (a stale table left behind by a 1m backfill, a
pre-DEC-RESAMPLE-ANCHOR-002 gapped-day grid) returns ``"stale"`` — never
silently absorbed. Both sides hold lake values already snapped to the
2-decimal tick grid at the write boundary (DEC-SPOT-PRICE-2DP-001), so
exact equality is the honest comparison.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    import polars as pl

    from brutex.data.timeframe_materialize import MaterializeResult

#: Env flag enabling auto-derivation (operator-approved storage switch).
ENV_AUTO_DERIVE_TFS: Final[str] = "BRUTEX_AUTO_DERIVE_TFS"


def autoderive_enabled() -> bool:
    """True iff the operator flipped the auto-derive storage switch.

    Strict decoder: only a trimmed literal ``"1"`` activates (mirrors the
    ``BRUTEX_M4PRO_STRICT`` pattern) — a typo falls back to OFF, never to a
    surprise write onto the operator's drive.
    """
    return os.environ.get(ENV_AUTO_DERIVE_TFS, "").strip() == "1"


def plan_autoderive_symbols(results: Iterable[object]) -> tuple[str, ...]:
    """Select the symbols worth re-deriving after a settled pull.

    A symbol qualifies iff its pull result reports ``completed > 0`` —
    i.e. NEW 1-minute bars actually landed (a no-data / failed / untouched
    symbol has nothing new to derive). Duck-typed over the pull
    controller's per-symbol results (``symbol`` + ``completed``
    attributes); order-preserving and de-duplicated.
    """
    seen: set[str] = set()
    planned: list[str] = []
    for result in results:
        symbol = str(getattr(result, "symbol"))  # noqa: B009 — duck-typed contract
        completed = int(getattr(result, "completed"))  # noqa: B009
        if completed > 0 and symbol not in seen:
            seen.add(symbol)
            planned.append(symbol)
    return tuple(planned)


@dataclass(frozen=True, slots=True)
class AutoderiveOutcome:
    """One symbol's auto-derivation outcome."""

    symbol: str
    written_tfs: int
    bars_written: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True iff the symbol derived without an error."""
        return self.error is None


@dataclass(frozen=True, slots=True)
class AutoderiveReport:
    """Aggregated auto-derivation report for one settled pull."""

    outcomes: tuple[AutoderiveOutcome, ...] = field(default=())

    @property
    def symbols_ok(self) -> int:
        """Symbols that derived cleanly."""
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def symbols_failed(self) -> int:
        """Symbols quarantined by an error."""
        return sum(1 for o in self.outcomes if not o.ok)

    @property
    def total_bars(self) -> int:
        """Total derived bars written across all symbols."""
        return sum(o.bars_written for o in self.outcomes)


def run_autoderive(
    *,
    symbols: Iterable[str],
    materialize_one: Callable[[str], MaterializeResult],
) -> AutoderiveReport:
    """Materialise every planned symbol's derived TFs, quarantining failures.

    ``materialize_one`` is the injected per-symbol materialiser (the glue
    binds :func:`~brutex.data.timeframe_materialize.materialize_timeframes`
    with the symbol's lake reader/writer). An exception from one symbol is
    captured into its outcome — the remaining symbols still run, and the
    caller decides how loudly to surface the report (auto-derive must never
    fail the pull that triggered it).
    """
    outcomes: list[AutoderiveOutcome] = []
    for symbol in symbols:
        try:
            result = materialize_one(symbol)
        except Exception as exc:
            outcomes.append(
                AutoderiveOutcome(symbol=symbol, written_tfs=0, bars_written=0, error=str(exc))
            )
            continue
        outcomes.append(
            AutoderiveOutcome(
                symbol=symbol,
                written_tfs=len(result.written),
                bars_written=result.total_bars,
            )
        )
    return AutoderiveReport(outcomes=tuple(outcomes))


def compare_derived_freshness(df_1m: pl.DataFrame, df_stored: pl.DataFrame, tf_minutes: int) -> str:
    """Compare a STORED derived table against a fresh re-derivation.

    Re-derives *tf_minutes* from *df_1m* via the canonical session-anchored
    resampler (DEC-RESAMPLE-ANCHOR-002) and compares against *df_stored*
    frame-for-frame over the canonical bar columns. Returns ``"fresh"`` on
    exact agreement, ``"stale"`` on ANY drift (row count, timestamps, or
    values). Lake prices on both sides are already 2dp-tick-snapped at the
    write boundary (DEC-SPOT-PRICE-2DP-001), so exact equality is the
    honest comparison — no tolerance band that could hide real drift.
    """
    from brutex.data.tf_resampler import REQUIRED_COLUMNS, resample_1m_to_tf

    expected = resample_1m_to_tf(df_1m, tf_minutes).select(*REQUIRED_COLUMNS)
    stored = df_stored.select(*REQUIRED_COLUMNS).sort("ts_utc")
    if expected.height != stored.height:
        return "stale"
    return "fresh" if expected.equals(stored.cast(expected.schema)) else "stale"
