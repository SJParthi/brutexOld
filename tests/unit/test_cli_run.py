"""Tests for the one-click ``brutex run`` command (DEC-ONECLICK-RUN-001).

``brutex/cli.py`` is coverage-omitted (CLI snapshot harness is a separate
PR), but ``cmd_run`` carries real logic — lake-root resolution, the
browser-open decision, and delegation to ``cmd_dashboard`` — so it is
unit-tested here for correctness.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from brutex import cli


def test_run_resolves_explicit_lake_root_and_opens_browser() -> None:
    """--lake-root is honoured; browser opens to /universe; dashboard gets the lake."""
    with (
        mock.patch.object(cli, "cmd_dashboard", return_value=0) as dash,
        mock.patch.object(cli, "_open_browser") as opener,
    ):
        rc = cli.cmd_run(Path("/tmp/lake"), "127.0.0.1", 9080, open_browser=True)

    assert rc == 0
    # DEC-DASHBOARD-AUTOOPEN-001: cmd_run now opens the Universe Explorer
    # surface directly, not the bare dashboard root.
    opener.assert_called_once_with("http://127.0.0.1:9080/universe")
    dash.assert_called_once_with(lake_root=Path("/tmp/lake"), host="127.0.0.1", port=9080)


def test_run_no_browser_skips_opener() -> None:
    """--no-browser (open_browser=False) must not open a browser."""
    with (
        mock.patch.object(cli, "cmd_dashboard", return_value=0),
        mock.patch.object(cli, "_open_browser") as opener,
    ):
        cli.cmd_run(Path("/tmp/lake"), "0.0.0.0", 9090, open_browser=False)

    opener.assert_not_called()


def test_run_falls_back_to_env_lake_root() -> None:
    """When --lake-root is omitted, $BRUTEX_LAKE_ROOT is used."""
    with (
        mock.patch.object(cli, "cmd_dashboard", return_value=0) as dash,
        mock.patch.object(cli, "_open_browser"),
        mock.patch.dict("os.environ", {"BRUTEX_LAKE_ROOT": "/data/lake"}),
    ):
        cli.cmd_run(None, "127.0.0.1", 9080, open_browser=True)

    dash.assert_called_once_with(lake_root=Path("/data/lake"), host="127.0.0.1", port=9080)


def test_run_falls_back_to_home_default_when_no_env() -> None:
    """No --lake-root and no env → ~/.brutex/lake."""
    with (
        mock.patch.object(cli, "cmd_dashboard", return_value=0) as dash,
        mock.patch.object(cli, "_open_browser"),
        mock.patch.dict("os.environ", {}, clear=True),
        mock.patch.object(cli.Path, "home", return_value=Path("/home/op")),
    ):
        cli.cmd_run(None, "127.0.0.1", 9080, open_browser=False)

    dash.assert_called_once_with(
        lake_root=Path("/home/op/.brutex/lake"), host="127.0.0.1", port=9080
    )


def test_fetch_universe_flag_defaults_false() -> None:
    """``brutex fetch`` parses --universe (default False, store_true when present)."""
    parser = cli.build_parser()
    without = parser.parse_args(["fetch", "--dry-run"])
    with_flag = parser.parse_args(["fetch", "--dry-run", "--universe"])

    assert without.universe is False
    assert with_flag.universe is True


def test_main_dispatch_threads_universe_into_cmd_fetch() -> None:
    """``main`` forwards ``args.universe`` to ``cmd_fetch`` as the ``universe`` kwarg."""
    with mock.patch.object(cli, "cmd_fetch", return_value=0) as fetch:
        rc = cli.main(["fetch", "--dry-run", "--universe"])

    assert rc == 0
    _, kwargs = fetch.call_args
    assert kwargs["universe"] is True
    assert kwargs["dry_run"] is True


def test_main_dispatch_universe_defaults_false_without_flag() -> None:
    """Without --universe, ``main`` passes ``universe=False`` to ``cmd_fetch``."""
    with mock.patch.object(cli, "cmd_fetch", return_value=0) as fetch:
        cli.main(["fetch", "--dry-run"])

    _, kwargs = fetch.call_args
    assert kwargs["universe"] is False
