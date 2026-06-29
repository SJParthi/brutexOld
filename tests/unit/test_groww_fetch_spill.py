"""Unit tests for ``brutex.adapters.groww.fetch_spill`` — the bounded on-disk spill overflow.

DEC-FETCH-NONBLOCKING-HANDOFF-001. 100% line+branch on:

* :func:`resolve_fetch_spill_max_bytes` — env / raw / unset / blank / non-int / non-positive;
* :class:`FetchSpillArea` — round-trip spill→load→discard, the byte-budget reserve/release, the
  CAP rejection (``try_spill`` → ``None``), the disk-full LakeWriteHalt envelope (budget released),
  idempotent discard, the ``cleanup`` straggler sweep (incl. the unlink-failure log branch);
* :class:`SpilledWindow` + :meth:`FetchSpillArea.under_lake_root` / properties.
"""

# Whitebox test of the spill internals (reserved-byte counter, dir) — sanctioned.
# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.fetch_spill import (
    FETCH_SPILL_MAX_BYTES_ENV,
    FetchSpillArea,
    SpilledWindow,
    resolve_fetch_spill_max_bytes,
)
from brutex.data.errors import LakeWriteHalt
from brutex.data.models import CandleBar

_DEFAULT_MAX = 256 * 1024 * 1024


def _bar(close: str = "105.00") -> CandleBar:
    return CandleBar(
        timestamp=datetime(2021, 1, 4, 4, 5, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal(close),
        volume=7,
        open_interest=None,
    )


class TestResolveFetchSpillMaxBytes:
    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(FETCH_SPILL_MAX_BYTES_ENV, raising=False)
        assert resolve_fetch_spill_max_bytes() == _DEFAULT_MAX

    def test_reads_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FETCH_SPILL_MAX_BYTES_ENV, "4096")
        assert resolve_fetch_spill_max_bytes() == 4096

    def test_explicit_raw_wins(self) -> None:
        assert resolve_fetch_spill_max_bytes("2048") == 2048

    @pytest.mark.parametrize("raw", ["", "   ", None])
    def test_blank_or_none_falls_back(
        self, raw: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(FETCH_SPILL_MAX_BYTES_ENV, raising=False)
        assert resolve_fetch_spill_max_bytes(raw) == _DEFAULT_MAX

    @pytest.mark.parametrize("raw", ["abc", "1.5", "0", "-10"])
    def test_non_int_or_non_positive_falls_back(self, raw: str) -> None:
        assert resolve_fetch_spill_max_bytes(raw) == _DEFAULT_MAX


class TestFetchSpillArea:
    def test_rejects_zero_max(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="max_bytes must be >= 1"):
            FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=0)

    def test_round_trip_spill_load_discard(self, tmp_path: Path) -> None:
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=_DEFAULT_MAX)
        bars = [_bar("101.00"), _bar("102.00")]
        handle = area.try_spill(bars)
        assert isinstance(handle, SpilledWindow)
        assert handle.path.is_file()
        assert area.reserved_bytes == handle.size_bytes > 0
        # Load restores byte-equal bars (exact Decimal round-trip via the JSON spill format).
        loaded = area.load(handle)
        assert loaded == bars
        # Discard deletes the file + releases the budget.
        area.discard(handle)
        assert not handle.path.exists()
        assert area.reserved_bytes == 0

    def test_round_trip_preserves_open_interest_int(self, tmp_path: Path) -> None:
        # An F&O bar carries an INTEGER open_interest — the JSON decode's ``oi is not None`` arm
        # must round-trip it to an int (the OI=None arm is covered by the other round-trip tests).
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=_DEFAULT_MAX)
        fno_bar = CandleBar(
            timestamp=datetime(2021, 1, 4, 4, 5, tzinfo=UTC),
            open=Decimal("100.05"),
            high=Decimal("110.10"),
            low=Decimal("90.95"),
            close=Decimal("105.55"),
            volume=42,
            open_interest=1234,
        )
        handle = area.try_spill([fno_bar])
        assert handle is not None
        loaded = area.load(handle)
        assert loaded == [fno_bar]
        assert loaded[0].open_interest == 1234

    def test_round_trip_empty_window(self, tmp_path: Path) -> None:
        # A zero-bar window spills + reloads to an empty list (the JSON encode/decode loops handle
        # the empty case — no special path needed).
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=_DEFAULT_MAX)
        handle = area.try_spill([])
        assert handle is not None
        assert area.load(handle) == []

    def test_discard_is_idempotent(self, tmp_path: Path) -> None:
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=_DEFAULT_MAX)
        handle = area.try_spill([_bar()])
        assert handle is not None
        area.discard(handle)
        # Second discard: file already gone (FileNotFoundError suppressed) + budget clamped >= 0.
        area.discard(handle)
        assert area.reserved_bytes == 0

    def test_cap_rejects_when_exceeded(self, tmp_path: Path) -> None:
        # A cap of 1 byte is below any real JSON spill payload → the first spill is rejected.
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=1)
        assert area.try_spill([_bar()]) is None
        assert area.reserved_bytes == 0  # nothing reserved on a rejection

    def test_cap_rejects_second_when_full(self, tmp_path: Path) -> None:
        # Size the cap so EXACTLY one window fits; the second is rejected.
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=_DEFAULT_MAX)
        first = area.try_spill([_bar()])
        assert first is not None
        tight = FetchSpillArea(spill_dir=tmp_path / "t", max_bytes=first.size_bytes)
        h1 = tight.try_spill([_bar()])
        assert h1 is not None
        assert tight.try_spill([_bar()]) is None  # cap full → second rejected
        # Discarding the first frees the budget so a third spill now fits.
        tight.discard(h1)
        assert tight.try_spill([_bar()]) is not None

    def test_disk_full_at_spill_raises_lake_write_halt_and_releases_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=_DEFAULT_MAX)

        def _enospc(*_a: object, **_k: object) -> None:
            raise OSError(28, "No space left on device")  # ENOSPC

        # atomic_write_via maps ENOSPC → LakeWriteHalt; patch the rename to force it.
        monkeypatch.setattr("brutex.data.atomic_io.os.replace", _enospc)
        with pytest.raises(LakeWriteHalt):
            area.try_spill([_bar()])
        # The reservation is RELEASED on the failed spill (budget never leaks).
        assert area.reserved_bytes == 0

    def test_cleanup_removes_stragglers(self, tmp_path: Path) -> None:
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=_DEFAULT_MAX)
        handle = area.try_spill([_bar()])
        assert handle is not None and handle.path.is_file()
        # cleanup() sweeps any straggler (an un-discarded spill) + resets the counter.
        area.cleanup()
        assert not handle.path.exists()
        assert area.reserved_bytes == 0

    def test_cleanup_missing_dir_is_noop(self, tmp_path: Path) -> None:
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=_DEFAULT_MAX)
        # Remove the dir AFTER construction → iterdir raises OSError → entries=[] (no crash).
        area._dir.rmdir()
        area.cleanup()  # no raise
        assert area.reserved_bytes == 0

    def test_cleanup_unlink_failure_is_logged_and_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=_DEFAULT_MAX)
        handle = area.try_spill([_bar()])
        assert handle is not None

        def _boom(self: Path) -> None:
            raise OSError("unlink boom")

        monkeypatch.setattr(Path, "unlink", _boom)
        area.cleanup()  # the OSError is logged + swallowed, never re-raised
        assert area.reserved_bytes == 0

    def test_under_lake_root_builds_with_default_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(FETCH_SPILL_MAX_BYTES_ENV, raising=False)
        area = FetchSpillArea.under_lake_root(tmp_path)
        assert area.spill_dir == tmp_path / ".fetch-spill"
        assert area._max_bytes == _DEFAULT_MAX

    def test_under_lake_root_honours_explicit_cap(self, tmp_path: Path) -> None:
        area = FetchSpillArea.under_lake_root(tmp_path, max_bytes=12345)
        assert area._max_bytes == 12345
