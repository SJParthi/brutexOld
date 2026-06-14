"""Render the live-vs-backtest parity verdict as a human-readable table.

The operator wants the answer "are the live and backtest 1-minute candles the
same?" as a **dead-simple table even a non-engineer can read** — not a JSON
blob. :func:`render_parity_markdown` turns a list of
:class:`~brutex.live.parity.ParityReport` into a Markdown report: one verdict row
per instrument, then a divergence table showing the first N disagreeing minutes in
IST wall-clock with the live vs backtest values side by side.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from brutex.calendars.nse import IST
from brutex.live.parity import MinuteDiff, ParityReport

#: How many divergence rows to show before truncating (keeps the table readable).
DEFAULT_MAX_DIFF_ROWS = 25


def _ist(ts: datetime) -> str:
    """Format a UTC timestamp as ``HH:MM`` IST wall-clock (the trader's clock)."""
    return ts.astimezone(IST).strftime("%Y-%m-%d %H:%M")


def _diff_value(bar_field: object) -> str:
    """Render one candle field (or ``—`` when the bar is missing)."""
    return "—" if bar_field is None else str(bar_field)


def _diff_rows(diff: MinuteDiff) -> list[tuple[str, str, str, str]]:
    """Expand one diverging minute into ``(minute, field, live, backtest)`` rows."""
    minute = _ist(diff.timestamp)
    if diff.reason == "live_only":
        return [(minute, "(present)", "live bar", "MISSING")]
    if diff.reason == "backtest_only":
        return [(minute, "(present)", "MISSING", "backtest bar")]
    rows: list[tuple[str, str, str, str]] = []
    for field in ("open", "high", "low", "close"):
        live_val = getattr(diff.live, field, None)
        bt_val = getattr(diff.backtest, field, None)
        if live_val != bt_val:
            rows.append((minute, field, _diff_value(live_val), _diff_value(bt_val)))
    return rows


def render_parity_markdown(
    reports: Sequence[ParityReport], *, max_diff_rows: int = DEFAULT_MAX_DIFF_ROWS
) -> str:
    """Render *reports* as a Markdown parity report.

    Parameters
    ----------
    reports
        One :class:`ParityReport` per instrument.
    max_diff_rows
        Cap on divergence rows shown across all instruments.

    Returns
    -------
    str
        A self-contained Markdown document (verdict table + divergence table).
    """
    lines: list[str] = [
        "# Live vs Backtest — 1-minute candle parity",
        "",
        "| Instrument | Verdict | Matched | Mismatched | Live-only | Backtest-only | Total |",
        "|---|---|---|---|---|---|---|",
    ]
    all_identical = True
    for report in reports:
        verdict = "✅ IDENTICAL" if report.is_identical else "❌ DIVERGES"
        all_identical = all_identical and report.is_identical
        lines.append(
            f"| {report.instrument_key} | {verdict} | {report.matched} | "
            f"{report.mismatched} | {report.live_only} | {report.backtest_only} | "
            f"{report.total_minutes} |"
        )

    headline = (
        "✅ ALL IDENTICAL"
        if all_identical and reports
        else ("❌ DIVERGENCES FOUND" if reports else "⚠️ NO INSTRUMENTS")
    )
    lines.extend(["", f"**Overall: {headline}**", ""])

    diff_rows: list[tuple[str, str, str, str, str]] = []
    for report in reports:
        for diff in report.diffs:
            for row in _diff_rows(diff):
                diff_rows.append((report.instrument_key, *row))

    if diff_rows:
        lines.extend(
            [
                f"## Divergences (showing up to {max_diff_rows})",
                "",
                "| Instrument | Minute (IST) | Field | Live | Backtest |",
                "|---|---|---|---|---|",
            ]
        )
        for instrument, minute, field, live_val, bt_val in diff_rows[:max_diff_rows]:
            lines.append(f"| {instrument} | {minute} | {field} | {live_val} | {bt_val} |")
        if len(diff_rows) > max_diff_rows:
            lines.append("")
            lines.append(f"_…and {len(diff_rows) - max_diff_rows} more diverging field(s)._")

    return "\n".join(lines) + "\n"


__all__ = ["DEFAULT_MAX_DIFF_ROWS", "render_parity_markdown"]
