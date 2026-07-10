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
    AUTO_RESUME_MASTER_ENV,
    AUTO_RESUME_ON_BOOT_ENV,
    IncompletePull,
    LedgerLike,
    ResumeVerdict,
    detect_incomplete_pull,
    maybe_resume_on_boot,
    resolve_auto_resume_master,
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


# ── resolve_auto_resume_master (the MASTER kill switch, default OFF) ─────────


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " on ", "ON"])
def test_resolve_master_truthy_values_enable(value: str) -> None:
    """Every explicit truthy value (case / whitespace-insensitive) enables the master gate."""
    assert resolve_auto_resume_master({AUTO_RESUME_MASTER_ENV: value}) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  ", "maybe", "2"])
def test_resolve_master_falsy_or_unrecognised_disable(value: str) -> None:
    """A falsy / blank / unrecognised (junk) value keeps the safe default-OFF SILENTLY.

    Mirrors the sibling :func:`resolve_auto_resume_on_boot` junk handling EXACTLY
    (never raises; unknown token ⇒ the flag's default) — the task's "match how
    the siblings handle junk" pin.
    """
    assert resolve_auto_resume_master({AUTO_RESUME_MASTER_ENV: value}) is False


def test_resolve_master_unset_disables() -> None:
    """An env WITHOUT the key returns False — the default-OFF the whole fix delivers."""
    assert resolve_auto_resume_master({}) is False


def test_resolve_master_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env injected, the master resolver reads the real ``os.environ``."""
    monkeypatch.setenv(AUTO_RESUME_MASTER_ENV, "yes")
    assert resolve_auto_resume_master() is True
    monkeypatch.delenv(AUTO_RESUME_MASTER_ENV, raising=False)
    assert resolve_auto_resume_master() is False


def test_resolve_master_is_independent_of_the_legacy_sub_gate() -> None:
    """The master gate reads ONLY ``BRUTEX_AUTO_RESUME`` — the legacy sub-gate cannot enable it.

    A boot with the legacy ``BRUTEX_AUTO_RESUME_ON_BOOT=1`` set but the master
    gate unset stays DISABLED (the master sits ABOVE the sub-gates).
    """
    assert resolve_auto_resume_master({AUTO_RESUME_ON_BOOT_ENV: "1"}) is False


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


# ── the ONE intent decision point (DEC-PULL-INTENT-RESUME-001) ────────────────


def test_intent_present_runs_pipeline_resume_exclusively() -> None:
    """Intent present ⇒ the pipeline resume fires EXCLUSIVELY: the legacy ledger
    is never even loaded and the legacy CLI-child action never spawns — the
    correction-(a) both-firing flock-race regression."""
    loaded: list[bool] = []
    legacy: list[IncompletePull] = []
    pipeline: list[str] = []

    def _load() -> LedgerLike:
        loaded.append(True)
        return _FakeLedger(_Counts(total=1, planned=1, failed=0, settled=0))

    verdict = maybe_resume_on_boot(
        load_ledger=_load,
        resume=legacy.append,
        enabled=True,  # legacy flag ON — the intent path must STILL win
        intent_present=lambda: True,
        resume_intent=lambda: pipeline.append("fired"),
    )
    assert verdict is ResumeVerdict.INTENT_RESUMED
    assert pipeline == ["fired"]
    assert legacy == [], "the legacy CLI child must NOT be spawned on an intent boot"
    assert loaded == [], "the legacy ledger read is skipped entirely"


def test_intent_path_ignores_legacy_disabled_flag() -> None:
    """The intent resume is independent of the legacy default-OFF env flag —
    the intent file is operator-click evidence (default ON with its own hatch)."""
    pipeline: list[str] = []
    verdict = maybe_resume_on_boot(
        load_ledger=lambda: _FakeLedger(_Counts(total=1, planned=1, failed=0, settled=0)),
        resume=lambda _i: None,
        enabled=False,  # legacy OFF — irrelevant to the intent branch
        intent_present=lambda: True,
        resume_intent=lambda: pipeline.append("fired"),
    )
    assert verdict is ResumeVerdict.INTENT_RESUMED
    assert pipeline == ["fired"]


def test_no_intent_keeps_legacy_path_byte_identical() -> None:
    """intent_present() False ⇒ the legacy decision proceeds exactly as before."""
    legacy: list[IncompletePull] = []
    verdict = maybe_resume_on_boot(
        load_ledger=lambda: _FakeLedger(_Counts(total=8, planned=2, failed=1, settled=5)),
        resume=legacy.append,
        enabled=True,
        intent_present=lambda: False,
        resume_intent=lambda: pytest.fail("must not fire without an intent"),
    )
    assert verdict is ResumeVerdict.RESUMED
    assert len(legacy) == 1


def test_unwired_intent_pair_is_byte_identical() -> None:
    """Either seam missing (every pre-existing caller) ⇒ the legacy path runs."""
    legacy: list[IncompletePull] = []
    verdict = maybe_resume_on_boot(
        load_ledger=lambda: _FakeLedger(_Counts(total=3, planned=3, failed=0, settled=0)),
        resume=legacy.append,
        enabled=True,
        intent_present=lambda: True,  # present, but no resume_intent wired
        resume_intent=None,
    )
    assert verdict is ResumeVerdict.RESUMED
    assert len(legacy) == 1


def test_raising_intent_probe_falls_through_to_legacy() -> None:
    """A raising intent_present probe is logged and the LEGACY resume still runs."""
    legacy: list[IncompletePull] = []

    def _boom_probe() -> bool:
        raise OSError("intent probe exploded")

    verdict = maybe_resume_on_boot(
        load_ledger=lambda: _FakeLedger(_Counts(total=2, planned=1, failed=0, settled=1)),
        resume=legacy.append,
        enabled=True,
        intent_present=_boom_probe,
        resume_intent=lambda: pytest.fail("must not fire when the probe errored"),
    )
    assert verdict is ResumeVerdict.RESUMED
    assert len(legacy) == 1


def test_raising_resume_intent_is_error_and_legacy_still_not_spawned() -> None:
    """Exclusivity holds even on failure: a raising pipeline resume returns ERROR
    and the legacy CLI child is still NOT spawned this boot."""
    legacy: list[IncompletePull] = []

    def _boom() -> None:
        raise RuntimeError("pipeline resume exploded")

    verdict = maybe_resume_on_boot(
        load_ledger=lambda: _FakeLedger(_Counts(total=1, planned=1, failed=0, settled=0)),
        resume=legacy.append,
        enabled=True,
        intent_present=lambda: True,
        resume_intent=_boom,
    )
    assert verdict is ResumeVerdict.ERROR
    assert legacy == []
