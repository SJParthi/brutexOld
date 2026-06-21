"""Shared CSV-safety primitives — the ONE source of truth for downloadable reports.

DEC-CSV-FORMULA-INJECTION-001 (extracted COMMON per DEC-MAPPING-INTEGRITY-EXPORT-001,
CLAUDE.md §11 COMMON). Every CSV the dashboard streams to the operator — the
completeness scoreboard, the mapping-integrity audit, any future export — runs each
cell through :func:`csv_safe` (via :class:`FormulaSafeWriter`) so a value that starts
with a spreadsheet formula lead-in (``= + - @`` or a leading TAB / CR) can NEVER execute
when the file is opened in Excel / Sheets / LibreOffice (OWASP "CSV injection").

Keeping this in ONE module means a new report can never silently bypass the guard by
re-implementing its own writer (the copy-paste trap this avoids). The CSV-quoting of
commas / quotes stays the underlying :mod:`csv` writer's job; this only adds the
formula-lead-in defusal on top. O(1) per cell.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

#: Leading characters a spreadsheet app (Excel / Sheets / LibreOffice) treats as the start
#: of a FORMULA when it opens a CSV cell — the CSV/formula-injection vector (OWASP "CSV
#: injection"). ``= + - @`` plus a leading TAB / CR. A cell whose text starts with one of
#: these is prefixed with a single quote so the app renders it as literal text.
CSV_INJECTION_LEADERS: Final[frozenset[str]] = frozenset("=+-@\t\r")

#: The formula lead-in characters that stay dangerous even behind LEADING WHITESPACE —
#: some spreadsheet apps strip a leading space then evaluate ``" =cmd"`` as a formula. So a
#: cell whose FIRST NON-WHITESPACE char is one of these is also neutralised (the
#: belt-and-suspenders "out-of-box" edge).
CSV_FORMULA_FIRST_CHARS: Final[frozenset[str]] = frozenset("=+-@")


def csv_safe(cell: object) -> object:
    """Neutralise CSV/formula injection on ONE cell.

    A STRING cell is prefixed with a single quote (``'``) — so a spreadsheet app shows the
    literal text instead of evaluating it as a formula — when EITHER its first character is
    a formula lead-in (``= + - @`` or a leading TAB / CR) OR its first NON-whitespace
    character is one of ``= + - @`` (the leading-whitespace bypass). Non-string cells (ints,
    ``None``) and empty / genuinely-plain strings pass through UNCHANGED, so numeric columns
    and ISO dates stay numeric/plain. O(1) — a first-char test plus one ``lstrip`` probe.
    """
    if (
        isinstance(cell, str)
        and cell
        and (cell[0] in CSV_INJECTION_LEADERS or cell.lstrip()[:1] in CSV_FORMULA_FIRST_CHARS)
    ):
        return "'" + cell
    return cell


class FormulaSafeWriter:
    """A :func:`csv.writer` wrapper that runs every cell through :func:`csv_safe`.

    Wrapping the writer ONCE neutralises formula-injection on EVERY ``writerow`` call
    without sprinkling the guard across each call site — one source of truth, so a future
    row can never bypass it (CLAUDE.md §11 COMMON). The CSV-quoting of commas / quotes is
    still the underlying :mod:`csv` writer's job; this only adds the formula-lead-in
    defusal on top.
    """

    __slots__ = ("_writer",)

    def __init__(self, writer: Any) -> None:
        self._writer = writer

    def writerow(self, row: Iterable[object]) -> None:
        """Write *row* with every cell formula-injection-sanitised via :func:`csv_safe`."""
        self._writer.writerow([csv_safe(cell) for cell in row])


__all__ = [
    "CSV_FORMULA_FIRST_CHARS",
    "CSV_INJECTION_LEADERS",
    "FormulaSafeWriter",
    "csv_safe",
]
