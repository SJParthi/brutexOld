"""Unit tests for the dashboard per-connection subscription-proof parser.

The proof is the exact Markdown the capture's
:meth:`brutex.live.multiconn.SubscriptionPlanReport.render_markdown` writes — a
real instance renders it here so the parser is tested against the live grammar,
not a hand-rolled fixture that could drift. The single ``brutex.live`` import is
in the TEST only (the parser source never imports it — ``DEC-LIVE-DECOUPLE-001``).
Covers :mod:`brutex.dashboard.live_connections`.
"""

from __future__ import annotations

from brutex.dashboard.live_connections import (
    EMPTY_NOTE,
    parse_connections_markdown,
    read_connections,
)


def _real_report_markdown() -> str:
    """Render a real 2-connection proof via the capture's own renderer."""
    from brutex.live.multiconn import (
        ConnectionRow,
        SubscriptionPlanReport,
    )

    report = SubscriptionPlanReport(
        connections=(
            ConnectionRow(0, "both", 775),
            ConnectionRow(1, "ltp", 1000),
        ),
        skipped_shards=0,
        total_instruments=1775,
    )
    return report.render_markdown()


def test_parse_real_capture_markdown() -> None:
    payload = parse_connections_markdown(_real_report_markdown())
    assert payload["note"] is None
    assert payload["connections"] == [
        {"index": 0, "feed": "both", "subscribed": 775},
        {"index": 1, "feed": "ltp", "subscribed": 1000},
    ]
    # The summary is the bold one-liner from the renderer.
    assert "2 connections" in payload["summary"]


def test_parse_carries_skipped_shards_summary() -> None:
    from brutex.live.multiconn import ConnectionRow, SubscriptionPlanReport

    md = SubscriptionPlanReport(
        connections=(ConnectionRow(0, "both", 5),),
        skipped_shards=3,
        total_instruments=5,
    ).render_markdown()
    payload = parse_connections_markdown(md)
    assert payload["connections"] == [{"index": 0, "feed": "both", "subscribed": 5}]
    assert "shard(s) over the ceiling" in payload["summary"]


def test_parse_empty_text_yields_empty_note() -> None:
    payload = parse_connections_markdown("")
    assert payload["connections"] == [] and payload["note"] == EMPTY_NOTE


def test_parse_garbage_lines_skipped() -> None:
    """Header / separator / non-matching lines never become connection rows."""
    text = "# title\n\nnot a row\n| Connection | Feed | Instruments |\n|---|---|---|\n| #2 | ltp | 9 |\n"
    payload = parse_connections_markdown(text)
    assert payload["connections"] == [{"index": 2, "feed": "ltp", "subscribed": 9}]
    assert payload["note"] is None
    assert payload["summary"] == ""  # no bold summary line present


def test_read_missing_report(tmp_path) -> None:
    payload = read_connections(tmp_path / "nope.md")
    assert payload == {"connections": [], "summary": "", "note": EMPTY_NOTE}


def test_read_unreadable_report(tmp_path, monkeypatch) -> None:
    """A real file whose read raises OSError → empty payload (never raises)."""
    path = tmp_path / "report.md"
    path.write_text("# present\n", encoding="utf-8")  # is_file() is True

    def _boom(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("pathlib.Path.read_text", _boom)
    assert read_connections(path) == {"connections": [], "summary": "", "note": EMPTY_NOTE}


def test_read_real_report_file(tmp_path) -> None:
    path = tmp_path / "live-connections.md"
    path.write_text(_real_report_markdown(), encoding="utf-8")
    payload = read_connections(path)
    assert len(payload["connections"]) == 2 and payload["note"] is None
