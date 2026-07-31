# pyright: reportPrivateUsage=none
"""The report.py ``summary_lines`` seam (DEC-NIGHTLY-RUST-CORE-001).

Flag-OFF bomb-spy byte-identity + flag-ON routed parity against the
retained ``_summary_lines_python`` oracle over the faithful wheel-free
fake crate + the refuse/degrade arms. Stem-matched so the per-module
coverage gate folds this suite into ``brutex/nightly/report.py``'s field.
"""

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING

import pytest
from brutex.config.rust_strict import RUST_STRICT_ENV, RustFallbackRefused
from brutex.nightly import nightly_rust
from brutex.nightly.chain import (
    LinkOutcome,
    LinkStatus,
    NightlyLink,
    NightlyReport,
    Tripwire,
)
from brutex.nightly.report import _summary_lines_python, summary_lines
from structlog.testing import capture_logs

from tests.fakes.fake_brutex_ops import build_fake_crate

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


class _BombModule(types.ModuleType):
    """Records + refuses ANY attribute access (the CODEQL bomb shape).

    ``__getattr__`` honours the special-method contract (AttributeError,
    never AssertionError — the ``test_fno_symbol_rust.py`` precedent); the
    TEETH are the ``touched`` ledger asserted EMPTY after the OFF-arm walk.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.touched.append(name)
        raise AttributeError(f"flag-OFF path touched the crate: {name}")


def _outcome(
    link: NightlyLink,
    status: LinkStatus,
    *,
    blocked_by: NightlyLink | None = None,
) -> LinkOutcome:
    return LinkOutcome(
        link=link,
        status=status,
        started_utc=None if status is LinkStatus.BLOCKED else 10.0,
        ended_utc=None if status is LinkStatus.BLOCKED else 20.0,
        detail="",
        counts={},
        error_type=None,
        blocked_by=blocked_by,
        exit_hint=None,
    )


def _report(
    *,
    links: tuple[LinkOutcome, ...],
    tripwires: tuple[Tripwire, ...] = (),
    counts: Mapping[str, int | None] | None = None,
    honesty_notes: tuple[str, ...] = (),
    dry_run: bool = False,
    ended_utc: float = 7_723.0,
) -> NightlyReport:
    return NightlyReport(
        schema_version=1,
        nightly_id="nightly-20260719T013000Z-abc123",
        lake_root="/lake",
        results_root="/results",
        backup_root="/backups",
        started_utc=4_000.0,
        ended_utc=ended_utc,
        links=links,
        tripwires=tripwires,
        counts={} if counts is None else counts,
        snapshot_id=None,
        absent_artifacts=(),
        honesty_notes=honesty_notes,
        dry_run=dry_run,
    )


_FULL_OK = _report(
    links=tuple(
        _outcome(link, LinkStatus.OK)
        for link in (
            NightlyLink.COLLISION,
            NightlyLink.DISK,
            NightlyLink.BACKUP,
            NightlyLink.PULL,
            NightlyLink.PREBUILD,
            NightlyLink.SWEEP,
        )
    ),
    counts={
        "backup_files": 12,
        "pull_bars": 375,
        "prebuild_built": 19,
        "sweep_run": 4,
        "sweep_skipped_cache": 2,
        "sweep_failed": 0,
    },
)
_REFUSED_BLOCKED = _report(
    links=(
        _outcome(NightlyLink.COLLISION, LinkStatus.OK),
        _outcome(NightlyLink.DISK, LinkStatus.REFUSED),
        _outcome(NightlyLink.BACKUP, LinkStatus.BLOCKED, blocked_by=NightlyLink.DISK),
        _outcome(NightlyLink.SWEEP, LinkStatus.BLOCKED, blocked_by=NightlyLink.DISK),
    ),
    tripwires=(Tripwire(name="audit_reconcile", link=None, detail="stream torn"),),
    honesty_notes=("note one",),
)
_SKIPPED_BACKUP_WET = _report(
    links=(
        _outcome(NightlyLink.BACKUP, LinkStatus.SKIPPED),
        _outcome(NightlyLink.SWEEP, LinkStatus.FAILED),
    ),
    counts={"sweep_run": None, "sweep_skipped_cache": 3, "sweep_failed": None},
)
_SKIPPED_BACKUP_DRY = _report(
    links=(_outcome(NightlyLink.BACKUP, LinkStatus.SKIPPED),),
    dry_run=True,
)


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(nightly_rust.NIGHTLY_RUST_ENV, raising=False)
    nightly_rust.reset_nightly_rust_for_tests()
    yield
    nightly_rust.reset_nightly_rust_for_tests()


def _flag_on(monkeypatch: pytest.MonkeyPatch, crate: types.ModuleType) -> None:
    monkeypatch.setenv(nightly_rust.NIGHTLY_RUST_ENV, "1")
    monkeypatch.setitem(sys.modules, "brutex_ops", crate)


class TestFlagOffBombSpy:
    def test_off_arm_matches_oracle_and_never_touches_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The explicit "0" kill switch (retargeted from the pre-flip unset
        # arm — DEC-RUST-DEFAULT-ON-WAVE-001); assertions unchanged.
        monkeypatch.setenv(nightly_rust.NIGHTLY_RUST_ENV, "0")
        bomb = _BombModule("brutex_ops")
        monkeypatch.setitem(sys.modules, "brutex_ops", bomb)
        for report in (_FULL_OK, _REFUSED_BLOCKED, _SKIPPED_BACKUP_WET, _SKIPPED_BACKUP_DRY):
            assert summary_lines(report) == _summary_lines_python(report)
        assert bomb.touched == []


class TestFlagOnParity:
    @pytest.mark.parametrize(
        "report",
        [_FULL_OK, _REFUSED_BLOCKED, _SKIPPED_BACKUP_WET, _SKIPPED_BACKUP_DRY],
        ids=["full-ok", "refused-blocked", "skipped-wet", "skipped-dry"],
    )
    def test_summary_parity(self, monkeypatch: pytest.MonkeyPatch, report: NightlyReport) -> None:
        _flag_on(monkeypatch, build_fake_crate())
        with capture_logs():
            routed = summary_lines(report)
        assert routed == _summary_lines_python(report)

    def test_full_ok_body_golden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One absolute golden so the parity net cannot go vacuously green."""
        _flag_on(monkeypatch, build_fake_crate())
        with capture_logs():
            lines, verdict = summary_lines(_FULL_OK)
        assert verdict == "OK"
        # ONE deliberate two-part literal (hoisted out of the list so the
        # concatenation is unambiguous — CodeQL implicit-concat-in-list).
        expected_detail = (
            "collision ok · disk ok · backup ok (12 files) · pull ok (375 bars) · "
            "prebuild ok (19 built) · sweep ok (4 run · 2 skipped-cache · 0 failed)"
        )
        assert lines == [
            "🌙 Nightly nightly-20260719T013000Z-abc123 — ✅ OK in 1:02.",
            expected_detail,
        ]

    def test_negative_duration_clamps_to_zero_both_arms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = _report(links=(_outcome(NightlyLink.PULL, LinkStatus.OK),), ended_utc=0.0)
        _flag_on(monkeypatch, build_fake_crate())
        with capture_logs():
            routed = summary_lines(report)
        assert routed == _summary_lines_python(report)
        assert routed[0][0].endswith("in 0:00.")


class TestFlagOnRefuseAndDegrade:
    """The legacy warn-and-degrade arms, pinned under the kill switch.

    DEC-RUST-STRICT-NO-FALLBACK-001 ships strict mode ON, so every arm in
    this class would now raise instead of degrading. The degrade law is
    still the behaviour ``BRUTEX_RUST_STRICT=0`` restores, so it is pinned
    here with the switch explicitly off; :class:`TestStrictRefusesTheSameArms`
    pins that the *same* three arms refuse when it is not.
    """

    @pytest.fixture(autouse=True)
    def _strict_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RUST_STRICT_ENV, "0")

    def test_poisoned_summary_shape_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(
            monkeypatch,
            build_fake_crate(nightly_summary_lines=lambda *args: ["lines", "OK"]),
        )
        with capture_logs() as logs:
            assert summary_lines(_FULL_OK) == _summary_lines_python(_FULL_OK)
        warns = [entry for entry in logs if entry["event"] == "nightly_rust.fallback"]
        assert warns and warns[0]["reason"] == "summary_lines_result_refused"

    def test_crate_fault_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args: object) -> tuple[list[str], str]:
            raise TypeError("junk count value")

        _flag_on(monkeypatch, build_fake_crate(nightly_summary_lines=_boom))
        with capture_logs() as logs:
            assert summary_lines(_FULL_OK) == _summary_lines_python(_FULL_OK)
        warns = [entry for entry in logs if entry["event"] == "nightly_rust.fallback"]
        assert warns and warns[0]["reason"] == "summary_lines_error"

    def test_wrong_lines_autorevert_via_burn_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(
            monkeypatch,
            build_fake_crate(nightly_summary_lines=lambda *args: (["wrong"], "OK")),
        )
        with capture_logs() as logs:
            assert summary_lines(_FULL_OK) == _summary_lines_python(_FULL_OK)
        assert nightly_rust.nightly_rust_disabled() is True
        assert any(entry["event"] == "nightly_rust.kernel_autoreverted" for entry in logs)


class TestStrictRefusesTheSameArms:
    """Strict mode (the shipped default) turns each degrade above into a raise.

    Same three fake-crate poisonings, no kill switch — each must raise
    :class:`RustFallbackRefused` naming the facade and the one way back.
    """

    @pytest.fixture(autouse=True)
    def _strict_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)

    @staticmethod
    def _assert_names_facade_and_switch(excinfo: pytest.ExceptionInfo[RustFallbackRefused]) -> None:
        text = str(excinfo.value)
        assert "brutex.nightly.nightly_rust" in text
        assert f"{RUST_STRICT_ENV}=0" in text

    def test_poisoned_summary_shape_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(
            monkeypatch,
            build_fake_crate(nightly_summary_lines=lambda *args: ["lines", "OK"]),
        )
        with pytest.raises(RustFallbackRefused, match="summary_lines_result_refused") as excinfo:
            summary_lines(_FULL_OK)
        self._assert_names_facade_and_switch(excinfo)

    def test_crate_fault_still_degrades_because_the_token_is_held(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``summary_lines_error`` is one of the six HELD-UNRESOLVED tokens.

        It is *not* classified Class C (a legitimate Python-owned input) — it
        is held out of the refusal set because the crate lumps a genuine
        crate fault together with an out-of-envelope argument under one
        token, so refusing it would crash on inputs Python was always meant
        to serve. Splitting it crate-side is the named follow-up. Until then
        this arm degrades even under strict, and that is pinned here so the
        hold is visible rather than assumed.
        """

        def _boom(*args: object) -> tuple[list[str], str]:
            raise TypeError("junk count value")

        _flag_on(monkeypatch, build_fake_crate(nightly_summary_lines=_boom))
        with capture_logs() as logs:
            assert summary_lines(_FULL_OK) == _summary_lines_python(_FULL_OK)
        warns = [entry for entry in logs if entry["event"] == "nightly_rust.fallback"]
        assert warns and warns[0]["reason"] == "summary_lines_error"

    def test_wrong_lines_refuse_at_the_burn_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(
            monkeypatch,
            build_fake_crate(nightly_summary_lines=lambda *args: (["wrong"], "OK")),
        )
        with pytest.raises(RustFallbackRefused, match="burn_in_mismatch") as excinfo:
            summary_lines(_FULL_OK)
        self._assert_names_facade_and_switch(excinfo)
