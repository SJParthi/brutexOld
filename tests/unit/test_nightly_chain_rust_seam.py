# pyright: reportPrivateUsage=none
"""The chain.py ``brutex_ops`` seam (DEC-NIGHTLY-RUST-CORE-001).

Flag-OFF bomb-spy byte-identity (the default arm never touches the crate)
+ flag-ON routed parity against the retained ``*_python`` oracles over a
FAITHFUL wheel-free fake crate (``tests/fakes/fake_brutex_ops.py``) + the
seam-local refuse/degrade arms (junk index, junk detail shape, poisoned
exit code). Stem-matched so the per-module coverage gate folds this suite
into ``brutex/nightly/chain.py``'s own field.
"""

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING

import pytest
from brutex.nightly import nightly_rust
from brutex.nightly.chain import (
    LinkOutcome,
    LinkStatus,
    NightlyLink,
    Tripwire,
    audit_reconcile_tripwire,
    audit_reconcile_tripwire_python,
    exit_code_for,
    exit_code_for_python,
    first_breaker,
    first_breaker_python,
)
from structlog.testing import capture_logs

from tests.fakes.fake_brutex_ops import build_fake_crate

if TYPE_CHECKING:
    from collections.abc import Iterator


class _BombModule(types.ModuleType):
    """A ``brutex_ops`` stand-in that records + refuses ANY attribute access.

    CODEQL shape (the ``test_fno_symbol_rust.py`` precedent): ``__getattr__``
    honours the special-method contract (AttributeError, never
    AssertionError). The TEETH are the ``touched`` ledger asserted EMPTY at
    the end of the OFF-arm test — a wrongful touch fails even if a facade
    probe swallows the raise via ``getattr(crate, name, None)``.
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
    attempted: bool = True,
    exit_hint: int | None = None,
) -> LinkOutcome:
    return LinkOutcome(
        link=link,
        status=status,
        started_utc=100.0 if attempted else None,
        ended_utc=101.0 if attempted else None,
        detail="",
        counts={},
        error_type=None,
        blocked_by=None,
        exit_hint=exit_hint,
    )


_GREEN_RUN = (
    _outcome(NightlyLink.COLLISION, LinkStatus.OK),
    _outcome(NightlyLink.DISK, LinkStatus.SKIPPED),
)
_FAILED_RUN = (
    _outcome(NightlyLink.COLLISION, LinkStatus.OK),
    _outcome(NightlyLink.DISK, LinkStatus.REFUSED),
    _outcome(NightlyLink.SWEEP, LinkStatus.FAILED, exit_hint=6),
)
_SWEEP_HINT_RUN = (
    _outcome(NightlyLink.PULL, LinkStatus.OK),
    _outcome(NightlyLink.SWEEP, LinkStatus.FAILED, exit_hint=6),
)
_TRIPWIRE = Tripwire(name="link_transition", link=None, detail="torn")

_CLEAN_EVENTS = (
    ("NIGHTLY_START", None),
    ("LINK_START", "pull"),
    ("LINK_DONE", "pull"),
    ("LINK_DONE", "prebuild"),
)
_CLEAN_OUTCOMES = (
    _outcome(NightlyLink.PULL, LinkStatus.OK),
    _outcome(NightlyLink.PREBUILD, LinkStatus.OK, attempted=False),
)
_TORN_EVENTS = (
    ("NIGHTLY_START", None),
    ("NIGHTLY_START", None),
    ("LINK_START", "sweep"),
    ("LINK_DONE", "pull"),
    ("LINK_DONE", "pull"),
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
    """Flag OFF — the default — is byte-identical AND never imports the crate."""

    def test_off_arm_matches_oracles_and_never_touches_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bomb = _BombModule("brutex_ops")
        monkeypatch.setitem(sys.modules, "brutex_ops", bomb)
        assert first_breaker(_GREEN_RUN) is first_breaker_python(_GREEN_RUN) is None
        assert first_breaker(_FAILED_RUN) is first_breaker_python(_FAILED_RUN)
        assert exit_code_for(_FAILED_RUN, ()) == exit_code_for_python(_FAILED_RUN, ()) == 1
        assert exit_code_for(_GREEN_RUN, (_TRIPWIRE,)) == 2
        seamed = audit_reconcile_tripwire(
            nightly_id="n-1", events=_CLEAN_EVENTS, outcomes=_CLEAN_OUTCOMES
        )
        oracle = audit_reconcile_tripwire_python(
            nightly_id="n-1", events=_CLEAN_EVENTS, outcomes=_CLEAN_OUTCOMES
        )
        assert seamed is None and oracle is None
        assert bomb.touched == []

    def test_off_arm_torn_stream_matches_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bomb = _BombModule("brutex_ops")
        monkeypatch.setitem(sys.modules, "brutex_ops", bomb)
        seamed = audit_reconcile_tripwire(
            nightly_id="n-2", events=_TORN_EVENTS, outcomes=_CLEAN_OUTCOMES
        )
        oracle = audit_reconcile_tripwire_python(
            nightly_id="n-2", events=_TORN_EVENTS, outcomes=_CLEAN_OUTCOMES
        )
        assert seamed == oracle
        assert seamed is not None and seamed.name == "audit_reconcile"
        assert bomb.touched == []


class TestFlagOnParity:
    """Flag ON over the faithful fake — routed values equal the oracles."""

    @pytest.mark.parametrize("outcomes", [(), _GREEN_RUN, _FAILED_RUN, _SWEEP_HINT_RUN], ids=str)
    def test_first_breaker_parity_and_identity(
        self, monkeypatch: pytest.MonkeyPatch, outcomes: tuple[LinkOutcome, ...]
    ) -> None:
        _flag_on(monkeypatch, build_fake_crate())
        with capture_logs():
            routed = first_breaker(outcomes)
        oracle = first_breaker_python(outcomes)
        assert routed is oracle  # the SAME object out of the sequence, or None

    @pytest.mark.parametrize(
        ("outcomes", "tripwires"),
        [
            (_GREEN_RUN, ()),
            (_GREEN_RUN, (_TRIPWIRE,)),
            (_FAILED_RUN, ()),
            (_SWEEP_HINT_RUN, ()),
            (_SWEEP_HINT_RUN, (_TRIPWIRE,)),
            ((_outcome(NightlyLink.PULL, LinkStatus.FAILED),), ()),
        ],
        ids=str,
    )
    def test_exit_code_parity(
        self,
        monkeypatch: pytest.MonkeyPatch,
        outcomes: tuple[LinkOutcome, ...],
        tripwires: tuple[Tripwire, ...],
    ) -> None:
        _flag_on(monkeypatch, build_fake_crate())
        with capture_logs():
            assert exit_code_for(outcomes, tripwires) == exit_code_for_python(outcomes, tripwires)

    @pytest.mark.parametrize(
        "events",
        [
            _CLEAN_EVENTS,
            _TORN_EVENTS,
            (("NIGHTLY_START", None), ("LINK_START", None), ("LINK_DONE", "pull")),
            (),
        ],
        ids=str,
    )
    def test_audit_reconcile_parity(
        self,
        monkeypatch: pytest.MonkeyPatch,
        events: tuple[tuple[str, str | None], ...],
    ) -> None:
        _flag_on(monkeypatch, build_fake_crate())
        with capture_logs():
            routed = audit_reconcile_tripwire(
                nightly_id="n-3", events=events, outcomes=_CLEAN_OUTCOMES
            )
        oracle = audit_reconcile_tripwire_python(
            nightly_id="n-3", events=events, outcomes=_CLEAN_OUTCOMES
        )
        assert routed == oracle


class TestFlagOnRefuseAndDegrade:
    def test_junk_breaker_index_degrades_to_oracle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(
            monkeypatch,
            build_fake_crate(nightly_first_breaker_index=lambda statuses: 99),
        )
        with capture_logs() as logs:
            assert first_breaker(_FAILED_RUN) is first_breaker_python(_FAILED_RUN)
        warns = [entry for entry in logs if entry["event"] == "nightly_rust.fallback"]
        assert warns and warns[0]["reason"] == "first_breaker_error"

    def test_non_int_breaker_index_degrades_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(
            monkeypatch,
            build_fake_crate(nightly_first_breaker_index=lambda statuses: "1"),
        )
        with capture_logs():
            assert first_breaker(_FAILED_RUN) is first_breaker_python(_FAILED_RUN)

    def test_poisoned_exit_code_shape_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch, build_fake_crate(nightly_exit_code=lambda *args: "6"))
        with capture_logs() as logs:
            assert exit_code_for(_SWEEP_HINT_RUN, ()) == 6
        warns = [entry for entry in logs if entry["event"] == "nightly_rust.fallback"]
        assert warns and warns[0]["reason"] == "exit_code_for_result_refused"

    def test_junk_audit_detail_shape_degrades_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _flag_on(
            monkeypatch,
            build_fake_crate(nightly_audit_reconcile_detail=lambda *args: 7),
        )
        with capture_logs() as logs:
            routed = audit_reconcile_tripwire(
                nightly_id="n-4", events=_TORN_EVENTS, outcomes=_CLEAN_OUTCOMES
            )
        assert routed == audit_reconcile_tripwire_python(
            nightly_id="n-4", events=_TORN_EVENTS, outcomes=_CLEAN_OUTCOMES
        )
        warns = [entry for entry in logs if entry["event"] == "nightly_rust.fallback"]
        assert warns and warns[0]["reason"] == "audit_reconcile_tripwire_error"

    def test_wrong_value_autoreverts_via_burn_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _flag_on(monkeypatch, build_fake_crate(nightly_exit_code=lambda *args: 5))
        with capture_logs() as logs:
            # Burn-in bit-compare catches the divergence and ships the ORACLE.
            assert exit_code_for(_GREEN_RUN, ()) == 0
        assert nightly_rust.nightly_rust_disabled() is True
        assert any(entry["event"] == "nightly_rust.kernel_autoreverted" for entry in logs)
