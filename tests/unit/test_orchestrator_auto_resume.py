"""Unit tests for :mod:`brutex.orchestrator.auto_resume` (DEC-AUTO-RESUME-ON-BOOT-001).

The boot-time auto-resume TRIGGER — a pure detector + a guarded orchestration
helper — that continues an in-progress universe pull automatically at process
start iff (a) the operator opted in via ``BRUTEX_AUTO_RESUME_ON_BOOT`` and
(b) the durable window ledger shows unfinished work (``PLANNED`` / ``FAILED``).

Coverage targets (100% line + 100% branch on the module):

* :func:`resolve_auto_resume_on_boot` — the truthy set, the falsy / unset /
  blank / unrecognised values, and the injected-env path.
* :func:`detect_incomplete_pull` — empty ledger ⇒ None; all-settled ⇒ None;
  PLANNED-only ⇒ verdict; FAILED-only ⇒ verdict; both ⇒ verdict + the
  ``outstanding`` sum.
* :func:`maybe_resume_on_boot` — every :class:`ResumeVerdict`: DISABLED (flag
  off), NOTHING_TO_RESUME (clean ledger), RESUMED (work found → action fires),
  ERROR (a raising load / a raising action is caught, never re-raised).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from brutex.orchestrator.auto_resume import (
    AUTO_RESUME_ON_BOOT_ENV,
    IncompletePull,
    LedgerLike,
    ResumeVerdict,
    detect_incomplete_pull,
    maybe_resume_on_boot,
    resolve_auto_resume_on_boot,
)


@dataclass(frozen=True)
class _Counts:
    """A minimal progress snapshot double (the four folds the detector reads)."""

    total: int
    planned: int
    failed: int
    settled: int


@dataclass
class _FakeLedger:
    """A ledger double exposing only :meth:`progress_counts` (the detector's need)."""

    counts: _Counts

    def progress_counts(self) -> _Counts:
        """Return the injected snapshot."""
        return self.counts


class _RaisingLedger:
    """A ledger double whose :meth:`progress_counts` raises — the error path."""

    def progress_counts(self) -> _Counts:
        """Always raise to prove the guard swallows it."""
        raise RuntimeError("boom-progress")


# ── resolve_auto_resume_on_boot ─────────────────────────────────────────────


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " on ", "ON"])
def test_resolve_flag_truthy_values_enable(value: str) -> None:
    """Every explicit truthy value (case / whitespace-insensitive) enables the flag."""
    assert resolve_auto_resume_on_boot({AUTO_RESUME_ON_BOOT_ENV: value}) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  ", "maybe", "2"])
def test_resolve_flag_falsy_or_unrecognised_disable(value: str) -> None:
    """A falsy / blank / unrecognised value keeps the safe default-OFF."""
    assert resolve_auto_resume_on_boot({AUTO_RESUME_ON_BOOT_ENV: value}) is False


def test_resolve_flag_unset_disables() -> None:
    """An env WITHOUT the key returns False (the default-OFF path)."""
    assert resolve_auto_resume_on_boot({}) is False


def test_resolve_flag_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env injected, the resolver reads the real ``os.environ``."""
    monkeypatch.setenv(AUTO_RESUME_ON_BOOT_ENV, "yes")
    assert resolve_auto_resume_on_boot() is True
    monkeypatch.delenv(AUTO_RESUME_ON_BOOT_ENV, raising=False)
    assert resolve_auto_resume_on_boot() is False


# ── detect_incomplete_pull ──────────────────────────────────────────────────


def test_detect_empty_ledger_is_none() -> None:
    """A ledger with zero recorded windows is NOT an incomplete pull."""
    ledger: LedgerLike = _FakeLedger(_Counts(total=0, planned=0, failed=0, settled=0))
    assert detect_incomplete_pull(ledger) is None


def test_detect_all_settled_is_none() -> None:
    """A ledger whose every window is settled is a COMPLETE pull — None."""
    ledger: LedgerLike = _FakeLedger(_Counts(total=10, planned=0, failed=0, settled=10))
    assert detect_incomplete_pull(ledger) is None


def test_detect_planned_only_is_incomplete() -> None:
    """Any still-PLANNED window makes it an incomplete pull."""
    verdict = detect_incomplete_pull(_FakeLedger(_Counts(total=10, planned=4, failed=0, settled=6)))
    assert verdict == IncompletePull(total=10, settled=6, planned=4, failed=0)
    assert verdict is not None
    assert verdict.outstanding == 4


def test_detect_failed_only_is_incomplete() -> None:
    """Any FAILED (transient) window makes it an incomplete pull."""
    verdict = detect_incomplete_pull(_FakeLedger(_Counts(total=10, planned=0, failed=3, settled=7)))
    assert verdict == IncompletePull(total=10, settled=7, planned=0, failed=3)
    assert verdict is not None
    assert verdict.outstanding == 3


def test_detect_both_outstanding_sums() -> None:
    """The ``outstanding`` property sums PLANNED + FAILED (the re-pull surface)."""
    verdict = detect_incomplete_pull(
        _FakeLedger(_Counts(total=20, planned=5, failed=2, settled=13))
    )
    assert verdict is not None
    assert verdict.outstanding == 7


# ── maybe_resume_on_boot ────────────────────────────────────────────────────


def test_maybe_resume_disabled_does_not_load_or_resume() -> None:
    """Flag OFF ⇒ DISABLED — the ledger is never loaded and no action fires."""
    loaded: list[bool] = []
    resumed: list[IncompletePull] = []

    def _load() -> LedgerLike:
        loaded.append(True)
        return _FakeLedger(_Counts(total=1, planned=1, failed=0, settled=0))

    verdict = maybe_resume_on_boot(
        load_ledger=_load,
        resume=resumed.append,
        enabled=False,
    )
    assert verdict is ResumeVerdict.DISABLED
    assert loaded == []  # short-circuit before any load
    assert resumed == []


def test_maybe_resume_nothing_to_resume_on_clean_ledger() -> None:
    """Flag ON + a clean ledger ⇒ NOTHING_TO_RESUME, no action fired."""
    resumed: list[IncompletePull] = []
    verdict = maybe_resume_on_boot(
        load_ledger=lambda: _FakeLedger(_Counts(total=5, planned=0, failed=0, settled=5)),
        resume=resumed.append,
        enabled=True,
    )
    assert verdict is ResumeVerdict.NOTHING_TO_RESUME
    assert resumed == []


def test_maybe_resume_fires_action_on_incomplete_pull() -> None:
    """Flag ON + outstanding work ⇒ RESUMED, the action fires with the verdict."""
    resumed: list[IncompletePull] = []
    verdict = maybe_resume_on_boot(
        load_ledger=lambda: _FakeLedger(_Counts(total=8, planned=2, failed=1, settled=5)),
        resume=resumed.append,
        enabled=True,
    )
    assert verdict is ResumeVerdict.RESUMED
    assert len(resumed) == 1
    assert resumed[0].outstanding == 3


def test_maybe_resume_swallows_load_error() -> None:
    """A raising load is caught → ERROR (never re-raised, boot unaffected)."""
    resumed: list[IncompletePull] = []
    verdict = maybe_resume_on_boot(
        load_ledger=_RaisingLedger().progress_counts,  # type: ignore[arg-type]
        resume=resumed.append,
        enabled=True,
    )
    assert verdict is ResumeVerdict.ERROR
    assert resumed == []


def test_maybe_resume_swallows_resume_action_error() -> None:
    """A raising resume ACTION is caught → ERROR (never re-raised)."""

    def _boom(_incomplete: IncompletePull) -> None:
        raise ValueError("boom-resume")

    verdict = maybe_resume_on_boot(
        load_ledger=lambda: _FakeLedger(_Counts(total=3, planned=3, failed=0, settled=0)),
        resume=_boom,
        enabled=True,
    )
    assert verdict is ResumeVerdict.ERROR


def test_maybe_resume_enabled_none_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``enabled=None`` (the default) resolves the flag from the environment."""
    resumed: list[IncompletePull] = []
    monkeypatch.setenv(AUTO_RESUME_ON_BOOT_ENV, "1")
    verdict = maybe_resume_on_boot(
        load_ledger=lambda: _FakeLedger(_Counts(total=4, planned=1, failed=0, settled=3)),
        resume=resumed.append,
    )
    assert verdict is ResumeVerdict.RESUMED
    assert len(resumed) == 1

    monkeypatch.delenv(AUTO_RESUME_ON_BOOT_ENV, raising=False)
    verdict_off = maybe_resume_on_boot(
        load_ledger=lambda: _FakeLedger(_Counts(total=4, planned=1, failed=0, settled=3)),
        resume=resumed.append,
    )
    assert verdict_off is ResumeVerdict.DISABLED
    assert len(resumed) == 1  # unchanged — DISABLED never fires the action
