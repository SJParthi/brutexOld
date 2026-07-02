"""Unit tests for the ``brutex doctor --live`` CLI wiring (DEC-DOCTOR-LIVE-PROOF-KIT-001).

Stem-matched to ``brutex/cli.py`` so the strict per-module coverage discovery
gate scopes these tests to the module they cover — the ``--stage`` selection
parser, the ``doctor --live`` dispatch branch, and the red ``--mint-anyway``
warning constant (moved out of ``test_doctor_live.py``, whose stem matches the
kit module, not ``cli.py``).
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
        with pytest.raises(ValueError, match="got 'x'"):
            parse_stage_selection("0,x")
        with pytest.raises(ValueError, match="got '14'"):
            parse_stage_selection("14")

    def test_doctor_live_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[object] = []

        def _fake(args: object) -> int:
            seen.append(args)
            return 0

        monkeypatch.setattr(cli, "cmd_doctor_live", _fake)
        assert cli.main(["doctor", "--live", "--symbol", "NSE-BANKNIFTY"]) == 0
        assert len(seen) == 1

    def test_mint_anyway_warning_is_red_and_multi_line(self) -> None:
        assert MINT_ANYWAY_WARNING.startswith("\x1b[31m")
        assert MINT_ANYWAY_WARNING.count("\n") >= 4
        assert "REVOKE the shared token" in MINT_ANYWAY_WARNING
