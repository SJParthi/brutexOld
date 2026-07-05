"""Unit tests for ``brutex.adapters.groww.fetch_spill`` — the bounded on-disk spill overflow.

DEC-FETCH-NONBLOCKING-HANDOFF-001. 100% line+branch on:

* :func:`resolve_fetch_spill_max_bytes` — env / raw / unset / blank / non-int / non-positive;
* :class:`FetchSpillArea` — round-trip spill→load→discard, the byte-budget reserve/release, the
  CAP rejection (``try_spill`` → ``None``), the disk-full LakeWriteHalt envelope (budget released),
  idempotent discard, the ``cleanup`` straggler sweep (incl. the unlink-failure log branch);
* :class:`SpilledWindow` + :meth:`FetchSpillArea.under_lake_root` / properties;
* DEC-FETCH-SPILL-ISOLATION-001 — the per-pull ``pull-<pid>-<uuid>`` spill namespacing, the
  own-dir-only ``cleanup`` (a concurrent pull's LIVE spill is NEVER touched), and the bounded
  stale-straggler sweep (a hard-killed pull's leftover dir / a legacy flat file);
* DEC-FETCH-SPILL-COUNT-CAP-001 — the spilled-window COUNT cap (``max_windows`` /
  :func:`resolve_fetch_spill_max_windows`): a tiny/empty-window flood is rejected at the count
  cap even with byte headroom (the fail-before OOM pathology), both budgets reserve/release
  symmetrically, and the byte cap still binds independently.
"""

# Whitebox test of the spill internals (reserved-byte counter, dir) — sanctioned.
# pyright: reportPrivateUsage=none

from __future__ import annotations

import os
import shutil
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.fetch_spill import (
    FETCH_SPILL_MAX_BYTES_ENV,
    FETCH_SPILL_MAX_WINDOWS_ENV,
    FetchSpillArea,
    SpilledWindow,
    resolve_fetch_spill_max_bytes,
    resolve_fetch_spill_max_windows,
)
from brutex.data.errors import LakeWriteHalt
from brutex.data.models import CandleBar

_DEFAULT_MAX = 256 * 1024 * 1024
_DEFAULT_MAX_WINDOWS = 8192


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


class TestResolveFetchSpillMaxWindows:
    """DEC-FETCH-SPILL-COUNT-CAP-001 — the structural twin of the byte-cap resolver."""

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(FETCH_SPILL_MAX_WINDOWS_ENV, raising=False)
        assert resolve_fetch_spill_max_windows() == _DEFAULT_MAX_WINDOWS

    def test_reads_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FETCH_SPILL_MAX_WINDOWS_ENV, "128")
        assert resolve_fetch_spill_max_windows() == 128

    def test_explicit_raw_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FETCH_SPILL_MAX_WINDOWS_ENV, "128")
        assert resolve_fetch_spill_max_windows("64") == 64

    @pytest.mark.parametrize("raw", ["", "   ", None])
    def test_blank_or_none_falls_back(
        self, raw: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(FETCH_SPILL_MAX_WINDOWS_ENV, raising=False)
        assert resolve_fetch_spill_max_windows(raw) == _DEFAULT_MAX_WINDOWS

    @pytest.mark.parametrize("raw", ["abc", "1.5", "0", "-5"])
    def test_non_int_or_non_positive_falls_back(self, raw: str) -> None:
        # A bad value can never DISABLE the bound — fail-safe to the audited default.
        assert resolve_fetch_spill_max_windows(raw) == _DEFAULT_MAX_WINDOWS


class TestFetchSpillArea:
    def test_rejects_zero_max(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="max_bytes must be >= 1"):
            FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=0)

    def test_rejects_zero_max_windows(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="max_windows must be >= 1"):
            FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=_DEFAULT_MAX, max_windows=0)

    def test_default_max_windows_is_the_audited_default(self, tmp_path: Path) -> None:
        # Back-compat pin: every existing construction site omits ``max_windows`` and inherits
        # the generous default (normal windows keep tripping the BYTE cap first).
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=_DEFAULT_MAX)
        assert area._max_windows == _DEFAULT_MAX_WINDOWS

    def test_round_trip_spill_load_discard(self, tmp_path: Path) -> None:
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=_DEFAULT_MAX)
        bars = [_bar("101.00"), _bar("102.00")]
        handle = area.try_spill(bars)
        assert isinstance(handle, SpilledWindow)
        assert handle.path.is_file()
        assert area.reserved_bytes == handle.size_bytes > 0
        assert area.reserved_windows == 1  # the count budget reserved symmetrically
        # Load restores byte-equal bars (exact Decimal round-trip via the JSON spill format).
        loaded = area.load(handle)
        assert loaded == bars
        # Discard deletes the file + releases BOTH budgets.
        area.discard(handle)
        assert not handle.path.exists()
        assert area.reserved_bytes == 0
        assert area.reserved_windows == 0

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
        # Second discard: file already gone (FileNotFoundError suppressed) + budgets clamped >= 0.
        area.discard(handle)
        assert area.reserved_bytes == 0
        assert area.reserved_windows == 0  # the count budget can never go negative either

    def test_cap_rejects_when_exceeded(self, tmp_path: Path) -> None:
        # A cap of 1 byte is below any real JSON spill payload → the first spill is rejected.
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=1)
        assert area.try_spill([_bar()]) is None
        assert area.reserved_bytes == 0  # nothing reserved on a rejection
        assert area.reserved_windows == 0  # a byte-cap rejection reserves NO window slot either

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

    def test_count_cap_rejects_spill_despite_byte_headroom(self, tmp_path: Path) -> None:
        """THE COUNT-CAP REGRESSION (DEC-FETCH-SPILL-COUNT-CAP-001, fail-before/pass-after).

        An EMPTY window's payload is 2 bytes (``"[]"``), so the byte cap alone admits ~134M
        resident overflow handles at the 256 MiB default — the OOM pathology. Before the fix
        the second spill here RETURNED A HANDLE (the count was unbounded); now the count cap
        rejects it even with near-infinite byte headroom, and ``discard`` frees the slot.
        """
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=_DEFAULT_MAX, max_windows=1)
        first = area.try_spill([])  # the tiny/empty pathology — 2 bytes on disk
        assert first is not None
        assert area.reserved_windows == 1
        assert area.reserved_bytes == first.size_bytes == 2  # byte headroom is ENORMOUS
        # FAIL-BEFORE: this returned a SpilledWindow (unbounded handle count) pre-fix.
        assert area.try_spill([]) is None  # count cap hit — byte cap nowhere near
        assert area.reserved_windows == 1  # a count-cap rejection reserves nothing
        assert area.reserved_bytes == 2
        # Discarding the live window frees the slot so a later spill fits again.
        area.discard(first)
        assert area.reserved_windows == 0
        assert area.try_spill([]) is not None

    def test_byte_cap_still_binds_with_huge_window_cap(self, tmp_path: Path) -> None:
        # Independence regression: a huge count cap must NOT loosen the byte cap.
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=1, max_windows=1_000_000)
        assert area.try_spill([_bar()]) is None  # the byte cap rejects as before
        assert area.reserved_bytes == 0
        assert area.reserved_windows == 0

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
        # BOTH reservations are RELEASED on the failed spill (neither budget ever leaks).
        assert area.reserved_bytes == 0
        assert area.reserved_windows == 0

    def test_cleanup_removes_stragglers(self, tmp_path: Path) -> None:
        area = FetchSpillArea(spill_dir=tmp_path / "s", max_bytes=_DEFAULT_MAX)
        handle = area.try_spill([_bar()])
        assert handle is not None and handle.path.is_file()
        # cleanup() sweeps any straggler (an un-discarded spill) + resets BOTH counters.
        area.cleanup()
        assert not handle.path.exists()
        assert area.reserved_bytes == 0
        assert area.reserved_windows == 0

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
        monkeypatch.delenv(FETCH_SPILL_MAX_WINDOWS_ENV, raising=False)
        area = FetchSpillArea.under_lake_root(tmp_path)
        # Per-pull namespacing (DEC-FETCH-SPILL-ISOLATION-001): a unique subdir of .fetch-spill.
        assert area.spill_dir.parent == tmp_path / ".fetch-spill"
        assert area.spill_dir.name.startswith("pull-")
        assert area._max_bytes == _DEFAULT_MAX
        assert area._max_windows == _DEFAULT_MAX_WINDOWS  # the live site inherits BOTH caps

    def test_under_lake_root_honours_explicit_cap(self, tmp_path: Path) -> None:
        area = FetchSpillArea.under_lake_root(tmp_path, max_bytes=12345)
        assert area._max_bytes == 12345

    def test_under_lake_root_honours_explicit_window_cap(self, tmp_path: Path) -> None:
        area = FetchSpillArea.under_lake_root(tmp_path, max_windows=7)
        assert area._max_windows == 7

    def test_under_lake_root_reads_window_cap_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The operator escape hatch: the env-tuned count cap reaches the live build path.
        monkeypatch.setenv(FETCH_SPILL_MAX_WINDOWS_ENV, "77")
        area = FetchSpillArea.under_lake_root(tmp_path)
        assert area._max_windows == 77


def _age(path: Path, *, hours: float) -> None:
    """Backdate *path*'s mtime by *hours* (drives the stale-straggler sweep in tests)."""
    old = time.time() - hours * 3600.0
    os.utime(path, (old, old))


class TestPerPullIsolation:
    """DEC-FETCH-SPILL-ISOLATION-001 — per-pull namespacing + own-dir-only cleanup + stale sweep.

    THE AUDIT SCENARIO (C-3 trigger): two concurrent pulls (CLI + dashboard) shared ONE flat
    ``<lake>/.fetch-spill`` dir, and either pull's drain ``cleanup()`` unlinked EVERY file in
    it — deleting the sibling pull's LIVE spill files out from under its consumer. With the
    per-pull namespace a cleanup can only ever touch its OWN dir (plus provably-stale
    hard-kill stragglers).
    """

    def test_under_lake_root_namespaces_per_pull(self, tmp_path: Path) -> None:
        a = FetchSpillArea.under_lake_root(tmp_path, max_bytes=_DEFAULT_MAX)
        b = FetchSpillArea.under_lake_root(tmp_path, max_bytes=_DEFAULT_MAX)
        assert a.spill_dir != b.spill_dir  # two pulls can never collide
        assert a.spill_dir.parent == b.spill_dir.parent == tmp_path / ".fetch-spill"

    def test_cleanup_never_touches_a_concurrent_pulls_live_spill(self, tmp_path: Path) -> None:
        """THE REGRESSION PIN: one pull's cleanup leaves the sibling pull's LIVE spill intact."""
        ours = FetchSpillArea.under_lake_root(tmp_path, max_bytes=_DEFAULT_MAX)
        sibling = FetchSpillArea.under_lake_root(tmp_path, max_bytes=_DEFAULT_MAX)
        live = sibling.try_spill([_bar("108.00")])
        assert live is not None
        ours_handle = ours.try_spill([_bar()])
        assert ours_handle is not None
        ours.cleanup()
        # Our own namespace dir + files are gone; the sibling's LIVE spill file survives and
        # is still loadable (pre-fix, the flat shared dir meant it was DELETED here).
        assert not ours.spill_dir.exists()
        assert live.path.is_file()
        assert sibling.load(live) == [_bar("108.00")]

    def test_cleanup_sweeps_stale_stragglers_keeps_fresh_sibling(self, tmp_path: Path) -> None:
        parent = tmp_path / ".fetch-spill"
        parent.mkdir()
        stale_dir = parent / "pull-999-deadbeef0000"
        stale_dir.mkdir()
        (stale_dir / "w1.spill").write_bytes(b"[]")
        empty_stale_dir = parent / "pull-998-deadbeef0001"
        empty_stale_dir.mkdir()
        legacy_file = parent / "old-flat-spill.pkl"
        legacy_file.write_bytes(b"[]")
        for path in (stale_dir, empty_stale_dir, legacy_file):
            _age(path, hours=25.0)
        fresh_dir = parent / "pull-111-cafecafe0000"
        fresh_dir.mkdir()
        (fresh_dir / "w2.spill").write_bytes(b"[]")
        FetchSpillArea.under_lake_root(tmp_path, max_bytes=_DEFAULT_MAX).cleanup()
        assert not stale_dir.exists()  # a hard-killed pull's straggler dir is swept
        assert not empty_stale_dir.exists()  # an empty straggler dir is swept too
        assert not legacy_file.exists()  # a pre-namespacing flat spill file is swept
        assert (fresh_dir / "w2.spill").is_file()  # a plausibly-LIVE sibling is untouched

    def test_stale_sweep_failure_is_logged_and_swallowed(self, tmp_path: Path) -> None:
        # A stale dir CONTAINING A SUBDIR: child.unlink() raises an OSError → logged, no raise.
        parent = tmp_path / ".fetch-spill"
        parent.mkdir()
        stubborn = parent / "pull-777-feedface0000"
        stubborn.mkdir()
        (stubborn / "nested").mkdir()
        _age(stubborn, hours=25.0)
        FetchSpillArea.under_lake_root(tmp_path, max_bytes=_DEFAULT_MAX).cleanup()  # no raise
        assert stubborn.exists()  # left in place (best-effort), never crashes the drain

    def test_stale_sweep_skips_unstatable_entry(self, tmp_path: Path) -> None:
        # A dangling symlink: stat() raises FileNotFoundError → the entry is skipped, no raise.
        parent = tmp_path / ".fetch-spill"
        parent.mkdir()
        dangling = parent / "gone"
        dangling.symlink_to(parent / "nonexistent-target")
        FetchSpillArea.under_lake_root(tmp_path, max_bytes=_DEFAULT_MAX).cleanup()  # no raise
        assert dangling.is_symlink()

    def test_no_stale_sweep_outside_fetch_spill_parent(self, tmp_path: Path) -> None:
        # A directly-constructed area (arbitrary parent): the sweep must NEVER touch siblings.
        area = FetchSpillArea(spill_dir=tmp_path / "custom", max_bytes=_DEFAULT_MAX)
        outsider = tmp_path / "unrelated.txt"
        outsider.write_text("keep")
        _age(outsider, hours=48.0)
        area.cleanup()
        assert outsider.read_text() == "keep"

    def test_cleanup_survives_missing_parent(self, tmp_path: Path) -> None:
        area = FetchSpillArea.under_lake_root(tmp_path, max_bytes=_DEFAULT_MAX)
        shutil.rmtree(tmp_path / ".fetch-spill")
        area.cleanup()  # own iterdir + rmdir + the sibling sweep all degrade, never raise
        assert area.reserved_bytes == 0

    def test_stale_sweep_never_removes_own_dir_even_if_stale(self, tmp_path: Path) -> None:
        # Whitebox: even a >threshold-old OWN dir is skipped by the sweep (the identity guard).
        area = FetchSpillArea.under_lake_root(tmp_path, max_bytes=_DEFAULT_MAX)
        handle = area.try_spill([_bar()])
        assert handle is not None
        _age(area.spill_dir, hours=48.0)
        area._sweep_stale_siblings()
        assert area.spill_dir.exists()
        assert handle.path.is_file()
