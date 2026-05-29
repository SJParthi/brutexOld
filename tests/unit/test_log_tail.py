"""Unit tests for :mod:`brutex.observability.log_tail`.

100% line + branch coverage of the safe log-tail reader:

* :func:`clamp_max_lines` — below / within / above the bound.
* :func:`tail_lines` — missing file, empty file, fewer-than-N, exactly-N,
  more-than-N, multi-block backward read, clamp interaction, non-UTF-8
  tolerance, directory (non-regular) path, no-trailing-newline final line.
* :func:`tail_records` — JSON object lines, non-JSON lines, JSON non-object
  (list / scalar) lines.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.observability.log_tail import (
    _BLOCK_SIZE,
    _MAX_LINES,
    _MIN_LINES,
    clamp_max_lines,
    tail_lines,
    tail_records,
)


class TestClampMaxLines:
    """The request-size clamp keeps reads bounded."""

    def test_below_min_clamps_up(self) -> None:
        assert clamp_max_lines(0) == _MIN_LINES
        assert clamp_max_lines(-5) == _MIN_LINES

    def test_within_range_passthrough(self) -> None:
        assert clamp_max_lines(200) == 200

    def test_above_max_clamps_down(self) -> None:
        assert clamp_max_lines(_MAX_LINES + 1) == _MAX_LINES
        assert clamp_max_lines(10_000_000) == _MAX_LINES


class TestTailLinesSafety:
    """Missing / empty / non-regular paths never raise."""

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert tail_lines(tmp_path / "nope.log", max_lines=10) == []

    def test_directory_path_returns_empty(self, tmp_path: Path) -> None:
        # A directory is not a regular file → [].
        assert tail_lines(tmp_path, max_lines=10) == []

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.log"
        f.write_bytes(b"")
        assert tail_lines(f, max_lines=10) == []


class TestTailLinesContent:
    """Line-count semantics for the backward reader."""

    def test_fewer_than_n(self, tmp_path: Path) -> None:
        f = tmp_path / "few.log"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        assert tail_lines(f, max_lines=10) == ["a", "b", "c"]

    def test_exactly_n(self, tmp_path: Path) -> None:
        f = tmp_path / "exact.log"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        assert tail_lines(f, max_lines=3) == ["a", "b", "c"]

    def test_more_than_n(self, tmp_path: Path) -> None:
        f = tmp_path / "more.log"
        f.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
        assert tail_lines(f, max_lines=2) == ["d", "e"]

    def test_no_trailing_newline(self, tmp_path: Path) -> None:
        f = tmp_path / "notrail.log"
        f.write_text("a\nb\nc", encoding="utf-8")
        assert tail_lines(f, max_lines=2) == ["b", "c"]

    def test_clamp_applies_to_content(self, tmp_path: Path) -> None:
        f = tmp_path / "clamp.log"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        # max_lines=0 clamps to 1 → only the last line.
        assert tail_lines(f, max_lines=0) == ["c"]

    def test_multi_block_backward_read(self, tmp_path: Path) -> None:
        # Force the loop to read more than one block backwards: each line is
        # long enough that 5 lines exceed one _BLOCK_SIZE chunk.
        f = tmp_path / "big.log"
        chunk = "x" * (_BLOCK_SIZE // 2)
        content = "".join(f"{i}-{chunk}\n" for i in range(5))
        f.write_text(content, encoding="utf-8")
        out = tail_lines(f, max_lines=2)
        assert len(out) == 2
        assert out[0].startswith("3-")
        assert out[1].startswith("4-")

    def test_non_utf8_tolerated(self, tmp_path: Path) -> None:
        f = tmp_path / "binary.log"
        # An invalid UTF-8 byte (0xFF) on its own line must not raise.
        f.write_bytes(b"good\n\xff\xfe bad\nlast\n")
        out = tail_lines(f, max_lines=10)
        assert out[0] == "good"
        assert out[-1] == "last"
        # The middle line decoded with replacement characters.
        assert "bad" in out[1]


class TestTailRecords:
    """JSON parsing with a graceful raw-line fallback."""

    def test_json_object_line(self, tmp_path: Path) -> None:
        f = tmp_path / "json.log"
        f.write_text('{"event": "hi", "level": "info", "k": 1}\n', encoding="utf-8")
        records = tail_records(f, max_lines=10)
        assert records == [{"event": "hi", "level": "info", "k": 1}]

    def test_non_json_line_wrapped(self, tmp_path: Path) -> None:
        f = tmp_path / "plain.log"
        f.write_text("not json at all\n", encoding="utf-8")
        assert tail_records(f, max_lines=10) == [{"event": "not json at all"}]

    @pytest.mark.parametrize("payload", ["[1, 2, 3]", "42", '"a string"'])
    def test_json_non_object_wrapped(self, tmp_path: Path, payload: str) -> None:
        # Valid JSON that is not an object (list / scalar) is wrapped raw.
        f = tmp_path / "arr.log"
        f.write_text(payload + "\n", encoding="utf-8")
        assert tail_records(f, max_lines=10) == [{"event": payload}]

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert tail_records(tmp_path / "none.log", max_lines=10) == []
