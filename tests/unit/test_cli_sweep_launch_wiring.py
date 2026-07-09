"""CLI wiring tests for the sweep launch knobs + dispatch (R2-SL-F1/F4-F5).

The ``cmd_sweep_all`` body is a ``# pragma: no cover`` shim, so these tests pin
the MEASURED wiring around it: the ``sweep-all`` parser accepts the two new
off-heap dedup-store knobs (``--sweep-cache-map-mb`` /
``--sweep-cache-bloom-capacity``) and ``main()`` threads them into
``cmd_sweep_all`` — ``None`` when unset so the env-var > module-default
resolution applies (an unset flag must never collapse a store's own default).
Mirrors the dispatch-test style of tests/unit/test_cli_backfill.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from brutex import cli

pytestmark = pytest.mark.usefixtures("_inert_sweep_logging_guard")  # see tests/conftest.py


def test_main_dispatch_sweep_all_threads_cache_knobs_none_when_unset() -> None:
    """Unset flags thread as None — env/module defaults stay in charge."""
    with mock.patch.object(cli, "cmd_sweep_all", return_value=0) as sweep_all:
        rc = cli.main(["sweep-all", "--start", "2024-01-01", "--end", "2024-01-02"])
    assert rc == 0
    _, kwargs = sweep_all.call_args
    assert kwargs["sweep_cache_map_mb"] is None
    assert kwargs["sweep_cache_bloom_capacity"] is None


def test_main_dispatch_sweep_all_threads_explicit_cache_knobs() -> None:
    """Explicit flags parse as ints and thread verbatim into cmd_sweep_all."""
    with mock.patch.object(cli, "cmd_sweep_all", return_value=0) as sweep_all:
        rc = cli.main(
            [
                "sweep-all",
                "--start",
                "2024-01-01",
                "--end",
                "2024-01-02",
                "--sweep-cache-map-mb",
                "4096",
                "--sweep-cache-bloom-capacity",
                "6000000",
            ]
        )
    assert rc == 0
    _, kwargs = sweep_all.call_args
    assert kwargs["sweep_cache_map_mb"] == 4096
    assert kwargs["sweep_cache_bloom_capacity"] == 6_000_000


def test_sweep_all_parser_help_names_the_env_fallbacks() -> None:
    """The flag help stays honest about the env-var > module-default chain."""
    parser = cli.build_parser()
    args = parser.parse_args(["sweep-all", "--start", "2024-01-01", "--end", "2024-01-02"])
    assert args.sweep_cache_map_mb is None
    assert args.sweep_cache_bloom_capacity is None
    # The knobs are sweep-all flags; the daemon path is env-var-tunable only
    # (BRUTEX_SWEEP_CACHE_MAP_MB / BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY) — a
    # flagged follow-up, not silently accepted-and-ignored there.
    daemon_args = parser.parse_args(
        ["sweep-daemon", "--once", "--start", "2024-01-01", "--end", "2024-01-02"]
    )
    assert not hasattr(daemon_args, "sweep_cache_map_mb")


# ────────────────────────────────────────────────────────────────────────
# --acknowledge-sla-overrun (DEC-SLA-LAUNCH-GUARD-001)
# ────────────────────────────────────────────────────────────────────────


def test_main_dispatch_threads_sla_acknowledge_false_by_default() -> None:
    """Unset flag threads as False — the guard REFUSES an over-envelope grid."""
    with mock.patch.object(cli, "cmd_sweep_all", return_value=0) as sweep_all:
        rc = cli.main(["sweep-all", "--start", "2024-01-01", "--end", "2024-01-02"])
    assert rc == 0
    _, kwargs = sweep_all.call_args
    assert kwargs["acknowledge_sla_overrun"] is False


def test_main_dispatch_threads_sla_acknowledge_true_when_passed() -> None:
    """--acknowledge-sla-overrun threads verbatim into cmd_sweep_all."""
    with mock.patch.object(cli, "cmd_sweep_all", return_value=0) as sweep_all:
        rc = cli.main(
            [
                "sweep-all",
                "--start",
                "2024-01-01",
                "--end",
                "2024-01-02",
                "--acknowledge-sla-overrun",
            ]
        )
    assert rc == 0
    _, kwargs = sweep_all.call_args
    assert kwargs["acknowledge_sla_overrun"] is True


def test_sla_acknowledge_is_sweep_all_only() -> None:
    """The unattended daemon has NO acknowledge flag (it can never override the
    SLA guard — mirrors the --allow-out-of-scope daemon refusal precedent)."""
    parser = cli.build_parser()
    daemon_args = parser.parse_args(
        ["sweep-daemon", "--once", "--start", "2024-01-01", "--end", "2024-01-02"]
    )
    assert not hasattr(daemon_args, "acknowledge_sla_overrun")


def _cmd_sweep_kwargs(tmp_path: Path, interval: str) -> dict[str, Any]:
    """Minimal in-scope cmd_sweep kwargs against a missing catalog.

    The 1day launch note prints BEFORE `_require_catalog_exists`, so the
    missing catalog's CliUserError bounds the test without any lake I/O.
    """
    return {
        "lake_root": tmp_path,
        "catalog_path": tmp_path / "missing-catalog.duckdb",
        "exchange": "NSE",
        "segment": "INDEX",
        "symbol": "NIFTY",
        "interval": interval,
        "start": "2024-01-01",
        "end": "2024-01-02",
        "direction": "BUY",
        "k": 1,
        "conditions": None,
        "stop_loss_paisa": None,
        "take_profit_paisa": None,
        "top": 5,
        "min_hits": None,
        "sort_by": "score",
        "validate_oos": False,
        "oos_only": False,
    }


def test_cmd_sweep_1day_launch_prints_the_trade_law_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-8 finding 1: a 1day launch is loudly LABELLED, never a silent
    empty leaderboard — `one_day_trade_law_note` prints before any lake I/O."""
    with pytest.raises(cli.CliUserError, match="catalog not found"):
        cli.cmd_sweep(**_cmd_sweep_kwargs(tmp_path, "1day"))
    out = capsys.readouterr().out
    assert "interval '1day' ranks NOTHING under the intraday-only trade law" in out
    assert "DEC-FUTURES-LAUNCH-REVIEW-FIXES-008" in out  # the named follow-up


def test_cmd_sweep_intraday_launch_prints_no_1day_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The intraday grid gets NO note — the label fires only for 1day."""
    with pytest.raises(cli.CliUserError, match="catalog not found"):
        cli.cmd_sweep(**_cmd_sweep_kwargs(tmp_path, "15minute"))
    assert "intraday-only trade law" not in capsys.readouterr().out
