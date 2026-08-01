"""The BRUTEX_BACKUP_RUST seam legs for :mod:`brutex.backup.schedule_render`.

DEC-BACKUP-RUST-OPS-001. The golden plists under
``docs/runbooks/backup-templates/`` stay the byte pin (the UNMODIFIED
``test_backup_schedule_render.py`` proves Python == golden; the cargo suite
proves Rust == golden); these legs cover the routed wrappers — fake-crate
match/poison arms and, when the wheel is installed, the REAL crate's
Rust == Python byte parity through the seam.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable, Iterator

import pytest
from brutex.backup import backup_rust, schedule_render
from brutex.backup.schedule_render import (
    render_backup_plist,
    render_cron_lines,
    render_deadman_plist,
    render_drill_plist,
)
from brutex.config.rust_strict import RUST_STRICT_ENV, RustFallbackRefused

RENDERERS: dict[str, tuple[Callable[[], str], Callable[[], str]]] = {
    "render_backup_plist": (render_backup_plist, schedule_render._render_backup_plist_python),
    "render_deadman_plist": (render_deadman_plist, schedule_render._render_deadman_plist_python),
    "render_drill_plist": (render_drill_plist, schedule_render._render_drill_plist_python),
    "render_cron_lines": (render_cron_lines, schedule_render._render_cron_lines_python),
}


@pytest.fixture(autouse=True)
def _fresh_seam() -> Iterator[None]:
    backup_rust.reset_backup_rust_for_tests()
    yield
    backup_rust.reset_backup_rust_for_tests()


@pytest.fixture
def strict_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the degrade-to-oracle contract to the mode that still HAS one.

    DEC-RUST-STRICT-NO-FALLBACK-001 ships ``BRUTEX_RUST_STRICT`` default-ON,
    where a Class-A (crate absent/stale/errored) or Class-B (crate answered
    but the RESULT is structurally refused — here a drifted render or a
    non-``str`` render) fall-back RAISES :class:`RustFallbackRefused` at the
    seam's choke point instead of serving the Python oracle.  The degrade
    machinery is not deleted — it stays reachable through the documented
    ``BRUTEX_RUST_STRICT=0`` kill switch, and THAT is the mode the tests
    requesting this fixture describe.  The strict-ON refusal for the SAME
    induced faults is pinned positively by
    :class:`TestStrictModeRefusesTheFallback`, so no coverage is traded away.

    Uses the SHARED ``monkeypatch`` (never a module-scoped
    ``MonkeyPatch.context()``) so set/unset ordering with the sibling
    ``BACKUP_RUST_ENV`` pins in this file stays deterministic.
    """
    monkeypatch.setenv(RUST_STRICT_ENV, "0")


def _install_fake(monkeypatch: pytest.MonkeyPatch, render: Callable[[], object]) -> None:
    module = types.ModuleType("brutex_ops")
    for name in backup_rust._CRATE_API:
        setattr(module, name, render)
    monkeypatch.setitem(sys.modules, "brutex_ops", module)


class TestOffArm:
    @pytest.mark.parametrize("law", sorted(RENDERERS))
    def test_off_is_the_python_oracle_crate_untouched(
        self, monkeypatch: pytest.MonkeyPatch, law: str
    ) -> None:
        # The explicit "0" kill switch (retargeted from the pre-flip unset
        # arm — DEC-RUST-DEFAULT-ON-WAVE-001); assertions unchanged.
        monkeypatch.setenv(backup_rust.BACKUP_RUST_ENV, "0")
        monkeypatch.setitem(sys.modules, "brutex_ops", None)  # the bomb
        public, oracle = RENDERERS[law]
        assert public() == oracle()
        assert backup_rust.disabled_reason() is None


class TestFakeCrate:
    @pytest.mark.parametrize("law", sorted(RENDERERS))
    def test_matching_fake_render_serves_rust(
        self, monkeypatch: pytest.MonkeyPatch, law: str
    ) -> None:
        monkeypatch.setenv(backup_rust.BACKUP_RUST_ENV, "1")
        public, oracle = RENDERERS[law]
        expected = oracle()
        _install_fake(monkeypatch, lambda: expected)
        assert public() == expected
        assert backup_rust.disabled_reason() is None

    def test_drifted_fake_render_autoreverts_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, strict_off: None
    ) -> None:
        monkeypatch.setenv(backup_rust.BACKUP_RUST_ENV, "1")
        _install_fake(monkeypatch, lambda: "drifted bytes")
        assert render_cron_lines() == schedule_render._render_cron_lines_python()
        assert backup_rust.disabled_reason() == "burn_in_mismatch"

    def test_non_str_fake_render_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, strict_off: None
    ) -> None:
        monkeypatch.setenv(backup_rust.BACKUP_RUST_ENV, "1")
        _install_fake(monkeypatch, lambda: b"bytes, not str")
        assert render_backup_plist() == schedule_render._render_backup_plist_python()
        assert backup_rust.disabled_reason() is None, "refused, not latched"

    def test_str_thunk_poison_message(self) -> None:
        with pytest.raises(TypeError, match="expected str"):
            schedule_render._str_thunk(42)()

    def test_str_thunk_passthrough(self) -> None:
        assert schedule_render._str_thunk("ok")() == "ok"


class TestRealWheelParity:
    @pytest.fixture(autouse=True)
    def _require_wheel(self) -> None:
        pytest.importorskip("brutex_ops")

    @pytest.mark.parametrize("law", sorted(RENDERERS))
    def test_rust_render_is_byte_identical(self, monkeypatch: pytest.MonkeyPatch, law: str) -> None:
        monkeypatch.setenv(backup_rust.BACKUP_RUST_ENV, "1")
        public, oracle = RENDERERS[law]
        assert public() == oracle()
        assert backup_rust.disabled_reason() is None

    @pytest.mark.parametrize("law", sorted(RENDERERS))
    def test_steady_state_rust_bytes_still_identical(
        self, monkeypatch: pytest.MonkeyPatch, law: str
    ) -> None:
        """remaining=0 serves the CRATE bytes with no oracle in the path."""
        monkeypatch.setenv(backup_rust.BACKUP_RUST_ENV, "1")
        backup_rust.reset_backup_rust_for_tests(remaining=0)
        public, oracle = RENDERERS[law]
        assert public() == oracle()


class TestStrictModeRefusesTheFallback:
    """The no-fallback guarantee, pinned POSITIVELY on this facade.

    DEC-RUST-STRICT-NO-FALLBACK-001.  Each test induces the SAME fault its
    ``strict_off`` twin in :class:`TestFakeCrate` induces, but leaves
    ``BRUTEX_RUST_STRICT`` at its shipped default (ON) and asserts the seam
    RAISES rather than quietly serving the Python oracle.  Without these the
    ``strict_off`` pins above would be a coverage trade, not a relocation.
    """

    def test_drifted_render_raises_instead_of_serving_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)  # shipped default = ON
        monkeypatch.setenv(backup_rust.BACKUP_RUST_ENV, "1")
        _install_fake(monkeypatch, lambda: "drifted bytes")
        with pytest.raises(RustFallbackRefused, match="burn_in_mismatch"):
            render_cron_lines()

    def test_non_str_render_raises_instead_of_serving_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)  # shipped default = ON
        monkeypatch.setenv(backup_rust.BACKUP_RUST_ENV, "1")
        _install_fake(monkeypatch, lambda: b"bytes, not str")
        with pytest.raises(RustFallbackRefused, match="result_refused"):
            render_backup_plist()
