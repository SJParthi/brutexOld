"""The overfit/DSR gate CLI default flip (DEC-RESULTS-TRUST-HONESTY-001 · B2b).

SANCTIONED default-behavior change: every DEFAULT ``sweep-all`` /
``sweep-daemon`` / ``nightly run`` sweep now runs the DEC-RANKING-OVERFIT-001
Deflated-Sharpe gate; ``--no-overfit-gate`` is the paired explicit escape.
GUARDRAILS pinned here: the ENGINE library default ``SweepRunConfig.overfit``
stays ``None`` and the frozen engine sweep goldens
(``_GOLDEN_CONTENT_HASH``/``_GOLDEN_RUN_ID``) pass UNMODIFIED (they construct
engine configs directly; verified by the golden suite itself). The dashboard
one-click surface rides the landed DEC-SWEEP-ONECLICK-PREFLIGHT-ESTIMATOR-001
(MIGRATION.md #1957) INSTITUTIONAL default — ``overfit_gate=True`` — per the
operator ruling of 2026-07-19 ("everything needs to be ON", relayed via the
steward), which SUPERSEDES this member's original dashboard gate-OFF
carve-out (fb06ca8a); see the renamed dashboard test below for the record.

Dispatch tests mirror ``test_cli_sweep_launch_wiring.py`` (the pragma-covered
``cmd_sweep_all`` body is mocked; the parser + ``main()`` threading is what is
measured).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest import mock

import pytest
from brutex import cli
from brutex.dashboard.sweep_runner import SweepRunRequest, build_sweep_kwargs
from brutex.engine.sweep_run import SweepRunConfig

pytestmark = pytest.mark.usefixtures("_inert_sweep_logging_guard")  # see tests/conftest.py

_SWEEP_ALL_MIN = ["sweep-all", "--start", "2024-01-01", "--end", "2024-01-02"]


def test_default_dispatch_threads_overfit_gate_true() -> None:
    """The SANCTIONED flip: a bare sweep-all now runs the DSR gate."""
    with mock.patch.object(cli, "cmd_sweep_all", return_value=0) as sweep_all:
        rc = cli.main(list(_SWEEP_ALL_MIN))
    assert rc == 0
    _, kwargs = sweep_all.call_args
    assert kwargs["overfit_gate"] is True


def test_no_overfit_gate_escape_threads_false() -> None:
    """--no-overfit-gate restores the pre-flip ungated ranking."""
    with mock.patch.object(cli, "cmd_sweep_all", return_value=0) as sweep_all:
        rc = cli.main([*_SWEEP_ALL_MIN, "--no-overfit-gate"])
    assert rc == 0
    _, kwargs = sweep_all.call_args
    assert kwargs["overfit_gate"] is False


def test_explicit_overfit_gate_flag_stays_a_true_twin() -> None:
    """The legacy opt-in flag is kept as the explicit no-op twin."""
    with mock.patch.object(cli, "cmd_sweep_all", return_value=0) as sweep_all:
        rc = cli.main([*_SWEEP_ALL_MIN, "--overfit-gate"])
    assert rc == 0
    _, kwargs = sweep_all.call_args
    assert kwargs["overfit_gate"] is True


def test_escape_wins_regardless_of_flag_order() -> None:
    """argparse last-wins on the shared dest — the explicit escape is honored."""
    with mock.patch.object(cli, "cmd_sweep_all", return_value=0) as sweep_all:
        rc = cli.main([*_SWEEP_ALL_MIN, "--overfit-gate", "--no-overfit-gate"])
    assert rc == 0
    _, kwargs = sweep_all.call_args
    assert kwargs["overfit_gate"] is False


@pytest.mark.parametrize("subcommand", ["sweep-all", "sweep-daemon"])
def test_shared_grid_parser_defaults_on_for_both_surfaces(subcommand: str) -> None:
    """The ONE _add_sweep_grid_args declaration flips every sweep surface."""
    parser = cli.build_parser()
    args = parser.parse_args([subcommand, "--start", "2024-01-01", "--end", "2024-01-02"])
    assert args.overfit_gate is True
    escaped = parser.parse_args(
        [subcommand, "--start", "2024-01-01", "--end", "2024-01-02", "--no-overfit-gate"]
    )
    assert escaped.overfit_gate is False


def test_cmd_sweep_all_signature_default_is_true() -> None:
    """A kwargs caller omitting overfit_gate (the nightly binding shape before
    parse — and any scripted caller) rides the same default-on."""
    assert inspect.signature(cli.cmd_sweep_all).parameters["overfit_gate"].default is True


def test_library_default_stays_none_guardrail() -> None:
    """GUARDRAIL: the ENGINE contract is untouched — SweepRunConfig.overfit
    defaults None (the frozen sweep goldens construct configs directly)."""
    field = SweepRunConfig.__dataclass_fields__["overfit"]
    assert field.default is None


def test_dashboard_runner_rides_the_ruled_institutional_default_on() -> None:
    """RULED SUPERSESSION (2026-07-19, not a make-CI-pass weakening): the
    dashboard one-click sweep pins ``overfit_gate=True`` — the landed
    DEC-SWEEP-ONECLICK-PREFLIGHT-ESTIMATOR-001 (MIGRATION.md #1957)
    institutional default ("the one-click button launches the institutional
    sweep, never the retired light research one"), confirmed by the operator
    ruling of 2026-07-19 ("everything needs to be ON", relayed via the
    steward). This SUPERSEDES the original club-member guardrail (fb06ca8a,
    2026-07-09: "the dashboard one-click runner keeps its own explicit
    overfit_gate=False … the flip cannot leak onto that surface"), which went
    red when main's #1957 flip landed via club O (184a48a4) at the S2
    merge-forward d85c266f. NOTE: PR #1674's body/tests still record the
    gate-OFF carve-out on this surface — that lane's debt to reconcile at
    ITS merge-forward, not the club's."""
    request = SweepRunRequest(
        exchange="NSE",
        segment="INDEX",
        symbols="NIFTY",
        intervals="5minute",
        start="2024-01-01",
        end="2024-01-02",
        directions="BUY",
        k="1",
        sl="none",
        tp="none",
        top=5,
        mode="A",
    )
    kwargs = build_sweep_kwargs(request, lake_root=Path("/tmp/lake"))
    assert kwargs["overfit_gate"] is True
