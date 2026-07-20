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
    classify_auto_resume_master,
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
    """A falsy / blank / unrecognised (junk) value keeps the safe default-OFF.

    Junk still DISABLES (fail-safe, never raises — a gate can never be enabled
    by accident) but is no longer silent: the boot gate classifies it via
    :func:`classify_auto_resume_master` and logs a WARNING
    (``auto_resume.junk_value_disabled`` — DEC-AUTO-RESUME-DEFAULT-OFF-001
    review round 1, minor 6). The boolean surface stays disabled either way.
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


# ── classify_auto_resume_master (the honest gate verdict) ───────────────────


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " on ", "ON"])
def test_classify_master_truthy_is_enabled(value: str) -> None:
    """A truthy token classifies ``enabled`` with the raw value echoed."""
    verdict = classify_auto_resume_master({AUTO_RESUME_MASTER_ENV: value})
    assert verdict.enabled is True
    assert verdict.state == "enabled"
    assert verdict.raw == value


def test_classify_master_unset_is_default_off() -> None:
    """An unset key reads ``default_off`` with ``raw=None`` (nothing was chosen)."""
    verdict = classify_auto_resume_master({})
    assert verdict == classify_auto_resume_master({})  # value-equal dataclass
    assert (verdict.enabled, verdict.state, verdict.raw) == (False, "default_off", None)


@pytest.mark.parametrize("value", ["", "  "])
def test_classify_master_blank_is_default_off(value: str) -> None:
    """A blank value classifies like unset (``default_off``) but echoes the raw string."""
    verdict = classify_auto_resume_master({AUTO_RESUME_MASTER_ENV: value})
    assert (verdict.enabled, verdict.state, verdict.raw) == (False, "default_off", value)


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", " No "])
def test_classify_master_explicit_falsy_is_explicit_off(value: str) -> None:
    """An explicit falsy token classifies ``explicit_off`` (a deliberate choice)."""
    verdict = classify_auto_resume_master({AUTO_RESUME_MASTER_ENV: value})
    assert (verdict.enabled, verdict.state, verdict.raw) == (False, "explicit_off", value)


@pytest.mark.parametrize("value", ["maybe", "2", "yess", "enable"])
def test_classify_master_junk_is_junk_off(value: str) -> None:
    """An unrecognised token classifies ``junk_off`` — disabled, but loud at the gate."""
    verdict = classify_auto_resume_master({AUTO_RESUME_MASTER_ENV: value})
    assert (verdict.enabled, verdict.state, verdict.raw) == (False, "junk_off", value)


@pytest.mark.parametrize(
    "value", ["1", "true", "TRUE", "Yes", "on", "0", "false", "no", "off", "", "  ", "maybe", "2"]
)
def test_classify_and_resolve_master_can_never_drift(value: str) -> None:
    """``resolve_auto_resume_master`` IS the classifier's boolean (one reader, no drift)."""
    env = {AUTO_RESUME_MASTER_ENV: value}
    assert resolve_auto_resume_master(env) is classify_auto_resume_master(env).enabled


def test_classify_master_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env injected, the classifier reads the real ``os.environ``."""
    monkeypatch.setenv(AUTO_RESUME_MASTER_ENV, "junk-token")
    assert classify_auto_resume_master().state == "junk_off"
    monkeypatch.delenv(AUTO_RESUME_MASTER_ENV, raising=False)
    assert classify_auto_resume_master().state == "default_off"


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


# ── on_error announce hook (DEC-AUTO-RESUME-ZERO-SURPRISES-001) ──────────────


def test_on_error_fires_on_raising_legacy_resume() -> None:
    """The legacy-arm guard is no longer log-only: the injected hook receives
    the exact caught exception (banner + Telegram live in the binding)."""
    seen: list[Exception] = []

    def _boom(_incomplete: IncompletePull) -> None:
        raise RuntimeError("legacy resume exploded")

    verdict = maybe_resume_on_boot(
        load_ledger=lambda: _FakeLedger(_Counts(total=3, planned=3, failed=0, settled=0)),
        resume=_boom,
        enabled=True,
        on_error=seen.append,
    )
    assert verdict is ResumeVerdict.ERROR
    assert len(seen) == 1
    assert isinstance(seen[0], RuntimeError)
    assert str(seen[0]) == "legacy resume exploded"


def test_on_error_fires_on_raising_intent_resume() -> None:
    """The intent-arm error branch fires the SAME hook (exclusivity holds)."""
    seen: list[Exception] = []

    def _boom() -> None:
        raise RuntimeError("pipeline resume exploded")

    verdict = maybe_resume_on_boot(
        load_ledger=lambda: _FakeLedger(_Counts(total=1, planned=1, failed=0, settled=0)),
        resume=lambda _i: pytest.fail("legacy must not fire — intent exclusivity"),
        enabled=True,
        intent_present=lambda: True,
        resume_intent=_boom,
        on_error=seen.append,
    )
    assert verdict is ResumeVerdict.ERROR
    assert len(seen) == 1


def test_on_error_fires_on_raising_ledger_load() -> None:
    """A load failure (unreadable ledger) also announces — the guard is one."""
    seen: list[Exception] = []
    verdict = maybe_resume_on_boot(
        load_ledger=_RaisingLedger().progress_counts,  # type: ignore[arg-type]
        resume=lambda _i: None,
        enabled=True,
        on_error=seen.append,
    )
    assert verdict is ResumeVerdict.ERROR
    assert len(seen) == 1


def test_on_error_raising_is_swallowed_and_verdict_unchanged() -> None:
    """A raising hook can never crash the boot thread nor mask the verdict."""

    def _boom_resume(_incomplete: IncompletePull) -> None:
        raise RuntimeError("resume exploded")

    def _boom_hook(_exc: Exception) -> None:
        raise OSError("banner volume wedged")

    verdict = maybe_resume_on_boot(
        load_ledger=lambda: _FakeLedger(_Counts(total=1, planned=1, failed=0, settled=0)),
        resume=_boom_resume,
        enabled=True,
        on_error=_boom_hook,
    )
    assert verdict is ResumeVerdict.ERROR


def test_on_error_never_fires_on_success_paths() -> None:
    """The hook is failure-only: DISABLED / NOTHING_TO_RESUME / RESUMED fire nothing."""
    seen: list[Exception] = []
    assert (
        maybe_resume_on_boot(
            load_ledger=lambda: _FakeLedger(_Counts(total=0, planned=0, failed=0, settled=0)),
            resume=lambda _i: None,
            enabled=False,
            on_error=seen.append,
        )
        is ResumeVerdict.DISABLED
    )
    assert (
        maybe_resume_on_boot(
            load_ledger=lambda: _FakeLedger(_Counts(total=2, planned=0, failed=0, settled=2)),
            resume=lambda _i: None,
            enabled=True,
            on_error=seen.append,
        )
        is ResumeVerdict.NOTHING_TO_RESUME
    )
    assert (
        maybe_resume_on_boot(
            load_ledger=lambda: _FakeLedger(_Counts(total=2, planned=1, failed=0, settled=1)),
            resume=lambda _i: None,
            enabled=True,
            on_error=seen.append,
        )
        is ResumeVerdict.RESUMED
    )
    assert seen == []


def test_detect_excludes_fetch_exhausted_settled_windows() -> None:
    """(i) of the durable-verdict design: a window the ledger terminally
    settled SKIPPED via the fetch-exhausted arm no longer counts as
    outstanding work — a ledger holding ONLY exhausted windows is a clean
    boot (None), so the whole-universe re-fire storm converges."""
    from datetime import UTC, date, datetime

    from brutex.data.fetch_coverage import WindowKey
    from brutex.data.window_ledger import (
        FAILED_TERMINAL_THRESHOLD,
        WindowLedger,
    )

    ledger = WindowLedger()
    key = WindowKey(
        market="groww",
        security_id="NIFTY",
        interval="1minute",
        start=date(2021, 1, 1),
        end=date(2021, 4, 1),
    )
    now = datetime(2026, 7, 9, tzinfo=UTC)
    for _ in range(FAILED_TERMINAL_THRESHOLD - 1):
        ledger.record(key, raw_bars=0, error_type="GrowwServerError", bars_appended=0, now=now)
    assert detect_incomplete_pull(ledger) is not None, "below K the window is outstanding"
    ledger.record(key, raw_bars=0, error_type="GrowwServerError", bars_appended=0, now=now)
    assert detect_incomplete_pull(ledger) is None, "the exhausted settle ends the re-fire loop"
