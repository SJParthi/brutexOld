"""Batched resampled-frame → ``CandleBar`` conversion — a TRUE import-graph leaf.

The ONE (CLAUDE.md §11 COMMON) home of :func:`frame_to_candle_bars`.

Why a dedicated LEAF module (DEC-PREBUILD-FRAME-WRITE-001 review fold): the
converter started life in :mod:`brutex.data.timeframe_materialize`, moved to
:mod:`brutex.data.spot_daily_resample` to break the ``spot_daily_resample ↔
timeframe_materialize`` cycle (DEC-PREBUILD-GIL-VECTORIZE-001
follow-through), and the frame-direct write lane
(DEC-PREBUILD-FRAME-WRITE-001) then added degrade-path consumers in
:mod:`brutex.data.sink` + :mod:`brutex.data.parquet_writer` — whose imports
re-formed a cycle (CodeQL ``py/cyclic-import``: ``sink →
spot_daily_resample → sink``, the daily-derive writer's own lazy
sink/writer imports being the pre-existing reverse edge). This module is the
terminal fix: a genuine leaf (module level: stdlib + typing only; polars +
the bar model imported function-locally, preserving the light-graph
discipline of the previous homes) that every consumer — ``sink`` /
``parquet_writer`` / ``timeframe_materialize`` / ``spot_daily_resample`` —
imports ONE-directionally. The two previous homes keep byte-compatible
re-exports for their original call sites/tests (the house alias pattern);
the cycle's absence is drift-pinned by
``tests/unit/test_data_sink_frame.py::TestImportCycleDriftPin``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    import polars as pl

    from brutex.data.models import CandleBar

__all__ = ["frame_to_candle_bars"]


def frame_to_candle_bars(
    frame: pl.DataFrame,
    *,
    absent_open_interest: int | None = 0,
) -> list[CandleBar]:
    """Batched resampled-frame → :class:`~brutex.data.models.CandleBar` conversion.

    The COMMON (CLAUDE.md §11) replacement for the per-row
    ``frame.iter_rows(named=True)`` → ``CandleBar(Decimal(str(...)) × 4)`` loops
    the derived-TF writer and the spot daily resampler each carried
    (DEC-PREBUILD-GIL-VECTORIZE-001). Why: the pipeline prebuild runs INSIDE
    the dashboard server process, and that per-row conversion was the largest
    GIL-held pure-Python section of the prebuild (stack-sampled during the
    blank-``GET /`` starvation diagnosis at the two former loop sites —
    ``timeframe_materialize.py:516-527`` and ``spot_daily_resample.py:359-370``
    pre-fix), stealing event-loop time from uvicorn while polars saturated the
    cores. This batched form extracts each column ONCE (columnar, C-level)
    instead of building a Python dict per row, renders the four price columns
    to their shortest-round-trip decimal strings INSIDE polars
    (``cast(String)`` — GIL released; numerically ``Decimal``-equal to Python's
    ``str(float)`` per the DEC-LAKE-WRITE-VECTORIZE-001 byte-contract analysis
    in :mod:`brutex.data.parquet_writer`, which pinned the same seed for the
    write-side snap), and memoises the ``str → Decimal`` parse (2dp-snapped
    lake prices repeat heavily across resample buckets; ``Decimal`` is
    immutable, so sharing instances is value-safe).

    Lives HERE — :mod:`brutex.data.frame_bars`, the TRUE import-graph LEAF
    (see the module docstring for the two-cycle relocation history:
    ``timeframe_materialize`` → ``spot_daily_resample`` → this leaf) — and is
    re-exported byte-compatibly from :mod:`brutex.data.spot_daily_resample`
    AND :mod:`brutex.data.timeframe_materialize` for the original call
    sites/tests (the house alias pattern; CodeQL ``py/cyclic-import``,
    DEC-PREBUILD-FRAME-WRITE-001 review fold).

    BYTE-IDENTICAL lake output (Golden Rule #7): every constructed bar compares
    equal to the legacy per-row form — same instants, ``Decimal`` values
    numerically equal to ``Decimal(str(x))`` (polars may emit a different digit
    STRING for the same value, e.g. ``0.00001`` vs ``1e-05`` — numerically
    equal, and the sink's ``round_money`` 2dp snap keys off the numeric value
    only) — pinned by the golden byte-equality regression over seeded lakes.
    HONEST envelope (Golden Rule #10): this is a BATCHED construction, not a
    bar-object elimination — the irreducible per-bar ``CandleBar`` (validated,
    frozen) + Python ``datetime`` objects remain, so the measured win is the
    dict/str/Decimal overhead (~25-30 % of the conversion), NOT an
    order-of-magnitude cut; the sink write path is byte-unchanged. This
    decision ships ONLY the vectorization — the diagnosis's other lever, a
    ``POLARS_MAX_THREADS`` cap at the dashboard/pull entrypoints (set before
    the first polars import, never on the engine/sweep path, which pins its
    OWN per-worker cap in :mod:`brutex.engine.sweep_parallel`), is NOT
    implemented anywhere: ``brutex/cli.py`` is fenced by open PRs, so the cap
    is a REPORTED follow-up and polars core saturation during a prebuild
    remains (starvation REDUCED, not eliminated).

    Parameters
    ----------
    frame
        A resampled frame with columns ``ts_utc, open, high, low, close,
        volume`` and optionally ``open_interest``.
    absent_open_interest
        The ``open_interest`` used when the frame has NO OI column, and for a
        present-but-null OI value. ``0`` is the derived-intraday-TF/spot lake
        convention (the legacy ``_oi`` helper's contract); the spot daily
        resampler passes ``None`` (spot/index daily bars carry no OI).
    """
    # Local imports keep the module import graph free of polars + the bar model
    # for callers that only need the dataclasses (module discipline, §11).
    from decimal import Decimal

    import polars as pl

    from brutex.data.models import CandleBar

    if frame.height == 0:
        return []

    timestamps = frame.get_column("ts_utc").to_list()
    opens, highs, lows, closes = (
        frame.get_column(column).cast(pl.String).to_list()
        for column in ("open", "high", "low", "close")
    )
    volumes = frame.get_column("volume").to_list()
    open_interests: list[int | None]
    if "open_interest" in frame.columns:
        # Present-but-null OI maps to *absent_open_interest* (the legacy `_oi`
        # contract: the lake stored 0 for spot, so 0 is the consistent absent
        # value on the intraday path).
        open_interests = [
            absent_open_interest if value is None else int(value)
            for value in frame.get_column("open_interest").to_list()
        ]
    else:
        open_interests = [absent_open_interest] * frame.height

    decimal_memo: dict[str, Decimal] = {}

    def to_decimal(text: str) -> Decimal:
        value = decimal_memo.get(text)
        if value is None:
            value = decimal_memo[text] = Decimal(text)
        return value

    return [
        CandleBar(
            timestamp=ts,
            open=to_decimal(open_text),
            high=to_decimal(high_text),
            low=to_decimal(low_text),
            close=to_decimal(close_text),
            volume=int(volume),
            open_interest=open_interest,
        )
        for ts, open_text, high_text, low_text, close_text, volume, open_interest in zip(
            timestamps, opens, highs, lows, closes, volumes, open_interests, strict=True
        )
    ]
