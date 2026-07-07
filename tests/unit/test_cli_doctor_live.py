"""CLI wiring for ``brutex doctor --live`` (DEC-DOCTOR-LIVE-PROOF-KIT-001).

Pins the ``--stage`` parser, the ``doctor --live`` dispatch line, and the red
``--mint-anyway`` warning. Stem-matched HERE (``brutex/cli.py``'s own per-file
coverage gate): ``scripts/ci/coverage_gate_discovery.py`` maps ``brutex/cli.py``
to ``tests/unit/test_cli_*.py``, so the kit's CLI lines must be covered by a
``test_cli_*`` file, not the doctor-live stage suite. ``cmd_doctor_live`` itself
is the real-deps composition wrapper (``# pragma: no cover``); every kit
behaviour behind it is unit-tested in ``tests/unit/test_doctor_live.py``.
"""

from __future__ import annotations

import pytest
from brutex import cli
from brutex.cli import MINT_ANYWAY_WARNING, parse_stage_selection


class TestCliWiring:
    def test_parse_stage_selection(self) -> None:
        assert parse_stage_selection(None) is None
        assert parse_stage_selection("  ") is None
        assert parse_stage_selection("0,5, 13") == frozenset({0, 5, 13})
        # Stage 14 (ampersand probe, DEC-FORM-WINNER-PERSIST-001) is selectable.
        assert parse_stage_selection("14") == frozenset({14})
        with pytest.raises(ValueError, match="got 'x'"):
            parse_stage_selection("0,x")
        with pytest.raises(ValueError, match="got '15'"):
            parse_stage_selection("15")

    def test_doctor_live_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[object] = []

        def _fake(args: object) -> int:
            seen.append(args)
            return 0

        monkeypatch.setattr(cli, "cmd_doctor_live", _fake)
        assert cli.main(["doctor", "--live", "--symbol", "NSE-BANKNIFTY"]) == 0
        assert len(seen) == 1

    def test_ampersand_probe_flag_parses_into_namespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--ampersand-probe`` reaches the dispatch namespace (default False)."""
        seen: list[object] = []

        def _fake(args: object) -> int:
            seen.append(args)
            return 0

        monkeypatch.setattr(cli, "cmd_doctor_live", _fake)
        assert cli.main(["doctor", "--live", "--ampersand-probe"]) == 0
        assert getattr(seen[0], "ampersand_probe") is True  # noqa: B009 — namespace probe
        seen.clear()
        assert cli.main(["doctor", "--live"]) == 0
        assert getattr(seen[0], "ampersand_probe") is False  # noqa: B009 — namespace probe

    def test_mint_anyway_warning_is_red_and_multi_line(self) -> None:
        assert MINT_ANYWAY_WARNING.startswith("\x1b[31m")
        assert MINT_ANYWAY_WARNING.count("\n") >= 4
        assert "REVOKE the shared token" in MINT_ANYWAY_WARNING
