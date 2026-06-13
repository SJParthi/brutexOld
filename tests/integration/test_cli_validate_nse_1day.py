"""CLI wiring for ``brutex validate-nse-1day`` (DEC-NSE-1DAY-VALIDATION-001).

Covers the argparse declarations + the ``main`` dispatch branch + parameter
threading WITHOUT touching the live network: the ``cmd_validate_nse_1day``
handler body is ``# pragma: no cover`` (real Groww + NSE/BSE), so the dispatch
branch is exercised by monkeypatching the handler (the house pattern from
``tests/unit/test_cli_backfill.py`` / ``test_cli_run.py``).
"""

from __future__ import annotations

from unittest import mock

from brutex import cli


def test_parser_validate_nse_1day_flags() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["validate-nse-1day", "--date", "2025-09-24"])
    assert args.command == "validate-nse-1day"
    assert args.date == "2025-09-24"
    assert args.symbol is None  # default
    assert args.tolerance is None  # default

    full = parser.parse_args(
        ["validate-nse-1day", "--date", "2025-09-24", "--symbol", "NIFTY", "--tolerance", "0.05"]
    )
    assert full.symbol == "NIFTY"
    assert full.tolerance == "0.05"


def test_main_dispatch_threads_args_to_handler() -> None:
    with mock.patch.object(cli, "cmd_validate_nse_1day", return_value=0) as handler:
        rc = cli.main(
            [
                "validate-nse-1day",
                "--date",
                "2025-09-24",
                "--symbol",
                "BANKNIFTY",
                "--tolerance",
                "0.10",
            ]
        )
    assert rc == 0
    _, kwargs = handler.call_args
    assert kwargs == {"date_str": "2025-09-24", "symbol": "BANKNIFTY", "tolerance": "0.10"}


def test_main_dispatch_defaults_symbol_and_tolerance_none() -> None:
    with mock.patch.object(cli, "cmd_validate_nse_1day", return_value=0) as handler:
        rc = cli.main(["validate-nse-1day", "--date", "2025-09-24"])
    assert rc == 0
    _, kwargs = handler.call_args
    assert kwargs == {"date_str": "2025-09-24", "symbol": None, "tolerance": None}
