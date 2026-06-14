"""Parse the N-connection subscription proof the capture writes (``--report``).

The capture subprocess (``python -m brutex.live capture-all --report <md>``) writes
a per-connection subscription proof as a Markdown table — ``# connection | feed |
#subscribed`` — so the operator can SEE "how many connections opened, what each
subscribed". THIS module is the dashboard half that READS that Markdown back into a
JSON payload the ``/live`` page renders, WITHOUT importing :mod:`brutex.live`
(``DEC-LIVE-DECOUPLE-001``): it parses a plain file the subprocess left behind.

The proof's grammar (mirrors
:meth:`brutex.live.multiconn.SubscriptionPlanReport.render_markdown`)::

    # Live-feed connection subscription plan

    **<summary line>**

    | Connection | Feed | Instruments |
    |---|---|---|
    | #0 | both | 775 |
    | #1 | ltp | 1000 |

A missing / not-yet-written report degrades to an empty payload (the toggle has
not been turned ON yet) — never raises, never invents a connection (Golden Rule
#1). Read-only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

#: The honest empty-state note when no proof has been written yet.
EMPTY_NOTE: Final[str] = "no connection proof yet — turn the Live feed ON to capture"

#: A connection data row: ``| #<idx> | <feed> | <subscribed> |``. The summary /
#: header / separator rows do NOT match (only a ``#<int>`` first cell does), so a
#: single pass over the lines extracts exactly the connection rows.
_ROW_RE: Final = re.compile(
    r"^\|\s*#(?P<index>\d+)\s*\|\s*(?P<feed>[^|]+?)\s*\|\s*(?P<subscribed>\d+)\s*\|\s*$"
)

#: The bold summary line (``**N connections (…)**``) — the human one-liner.
_SUMMARY_RE: Final = re.compile(r"^\*\*(?P<summary>.+)\*\*\s*$")


def parse_connections_markdown(text: str) -> dict[str, Any]:
    """Parse a connection-proof Markdown *text* into the ``/live`` payload dict.

    Walks the lines once: the first ``**…**`` line is the summary; every
    ``| #<idx> | <feed> | <subscribed> |`` line is one connection row. Lines that
    do not match (the title, the column header, the ``|---|`` separator, blanks)
    are skipped, so a malformed / partial report yields only the rows that DID
    parse — never raises.

    Returns
    -------
    dict[str, Any]
        ``{"connections": [{"index", "feed", "subscribed"}], "summary": <str>,
        "note": <str|None>}`` — ``note`` is :data:`EMPTY_NOTE` when zero
        connections parsed (e.g. an empty/garbage file), else ``None``.
    """
    summary = ""
    connections: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not summary:
            summary_match = _SUMMARY_RE.match(line.strip())
            if summary_match:
                summary = summary_match.group("summary")
                continue
        row_match = _ROW_RE.match(line)
        if row_match:
            connections.append(
                {
                    "index": int(row_match.group("index")),
                    "feed": row_match.group("feed").strip(),
                    "subscribed": int(row_match.group("subscribed")),
                }
            )
    note = EMPTY_NOTE if not connections else None
    return {"connections": connections, "summary": summary, "note": note}


def read_connections(report_path: str | Path) -> dict[str, Any]:
    """Read the connection-proof at *report_path* into the ``/live`` payload dict.

    A MISSING / unreadable report degrades to an empty payload + :data:`EMPTY_NOTE`
    (the Live feed has not been turned ON yet, or the proof is gone) — never raises,
    never invents a connection (Golden Rule #1). Read-only.

    Parameters
    ----------
    report_path
        The capture's ``--report`` Markdown path.

    Returns
    -------
    dict[str, Any]
        The :func:`parse_connections_markdown` payload, or the empty payload when
        the file is absent / unreadable.
    """
    path = Path(report_path)
    if not path.is_file():
        return {"connections": [], "summary": "", "note": EMPTY_NOTE}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"connections": [], "summary": "", "note": EMPTY_NOTE}
    return parse_connections_markdown(text)


__all__ = [
    "EMPTY_NOTE",
    "parse_connections_markdown",
    "read_connections",
]
