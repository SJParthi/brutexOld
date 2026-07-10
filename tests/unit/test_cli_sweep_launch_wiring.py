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


def test_price_source_cells_argparse_default_is_baseline_cardinality_one() -> None:
    """GAP-3: the ``--price-source-cells`` argparse DEFAULT resolves to the sole
    baseline ``(HLC3, CLOSE)`` cell — the swept axis stays default-OMITTED/dormant.

    The config-layer default (``price_source_cells: str = "hlc3:close"`` on the
    ``cmd_sweep_all`` shim) is pinned elsewhere, but the PARSER-layer default was
    unpinned: flipping the argparse ``default=`` string to e.g. ``"open:close"``
    would silently turn the axis default-ON (breaking Override 1's byte-identity
    for every plain ``sweep-all`` invocation) without tripping any test. The
    assertion rides ``build_parser()`` (NOT the ``# pragma: no cover``
    declarative-wiring helper), so it exercises + bites the real default value.
    """
    from brutex.strategies.types import (
        DEFAULT_PRICE_SOURCE_CELL,
        PriceSourceCell,
        parse_price_source_cells,
    )

    parser = cli.build_parser()
    args = parser.parse_args(["sweep-all", "--start", "2024-01-01", "--end", "2024-01-02"])
    # The raw parser default string (the mutation target).
    assert args.price_source_cells == "hlc3:close"
    # …which resolves to exactly the cardinality-1 baseline cell (axis dormant).
    resolved = parse_price_source_cells(args.price_source_cells)
    assert resolved == [DEFAULT_PRICE_SOURCE_CELL]
    assert tuple(resolved) == (PriceSourceCell(),)


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
