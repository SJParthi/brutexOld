"""Tests for the ``brutex fetch --feed dhan`` + ``enrich-greeks --feed dhan`` CLI arms.

DEC-DHAN-ROLLING-OPTIONS-001. The Dhan CLI arms are ADDITIVE — ``--feed groww`` (the
default) leaves Groww byte-unchanged. ``fetch --feed dhan`` pulls the read-only Dhan feed
(spot / rolling-ATM options) into ``feeds/dhan/``; the real-run body is the live-credential
boundary, so these pins drive the parser, the dispatch, the ``--dry-run`` (no-vendor-call)
path, date validation, and the fully-offline ``enrich-greeks --feed dhan`` rolling-ATM
Greeks pass (pure lake I/O — no network).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex import cli
from brutex.cli import (
    EXIT_INTERNAL_ERROR,
    EXIT_OK,
    EXIT_USER_ERROR,
    cmd_enrich_greeks_dhan,
    cmd_fetch_dhan,
)
from brutex.data.greeks_enrich_pass import GreeksEnrichOutcome

# ── parser + dispatch ──────────────────────────────────────────────────────────


def test_fetch_parser_defaults_feed_groww_dataset_spot() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["fetch"])
    assert args.feed == "groww"
    assert args.dataset == "spot"


def test_fetch_parser_accepts_feed_dhan_rolling_options() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["fetch", "--feed", "dhan", "--dataset", "rolling-options"])
    assert args.feed == "dhan"
    assert args.dataset == "rolling-options"


def test_enrich_parser_defaults_feed_groww_dataset_rolling_options() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["enrich-greeks"])
    assert args.feed == "groww"
    assert args.dataset == "rolling-options"


def test_fetch_dispatch_routes_feed_dhan_to_cmd_fetch_dhan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def _fake(lake_root: Path | None, **kwargs: object) -> int:
        seen["lake_root"] = lake_root
        seen.update(kwargs)
        return EXIT_OK

    monkeypatch.setattr(cli, "cmd_fetch_dhan", _fake)
    rc = cli.main(
        ["fetch", "--feed", "dhan", "--dataset", "rolling-options", "--lake-root", str(tmp_path)]
    )
    assert rc == EXIT_OK
    assert seen["dataset"] == "rolling-options"
    assert seen["dry_run"] is False


def test_enrich_dispatch_routes_feed_dhan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def _fake(lake_root: Path | None, *, dataset: str) -> int:
        seen["lake_root"] = lake_root
        seen["dataset"] = dataset
        return EXIT_OK

    monkeypatch.setattr(cli, "cmd_enrich_greeks_dhan", _fake)
    rc = cli.main(["enrich-greeks", "--feed", "dhan", "--lake-root", str(tmp_path)])
    assert rc == EXIT_OK
    assert seen["dataset"] == "rolling-options"


# ── cmd_fetch_dhan (dry-run + validation) ──────────────────────────────────────


def test_fetch_dhan_dry_run_no_writes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cmd_fetch_dhan(
        tmp_path, start="2024-06-01", end="2024-06-30", dataset="rolling-options", dry_run=True
    )
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "rolling-ATM" in out
    assert "NOT per-contract" in out
    assert "NO vendor call" in out
    # Dry-run wrote nothing real.
    assert not (tmp_path / "feeds").exists()


def test_fetch_dhan_dry_run_spot_dataset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cmd_fetch_dhan(tmp_path, start=None, end=None, dataset="spot", dry_run=True)
    assert rc == EXIT_OK
    assert "spot indices" in capsys.readouterr().out


def test_fetch_dhan_bad_date_is_user_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cmd_fetch_dhan(tmp_path, start="not-a-date", end=None, dataset="spot", dry_run=True)
    assert rc == EXIT_USER_ERROR


def test_fetch_dhan_reversed_range_is_user_error(tmp_path: Path) -> None:
    rc = cmd_fetch_dhan(
        tmp_path, start="2024-06-30", end="2024-06-01", dataset="spot", dry_run=True
    )
    assert rc == EXIT_USER_ERROR


def test_fetch_dhan_real_run_delegates_to_live_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # dry_run=False dispatches to the pragma'd live-credential boundary — stubbed here
    # so the dispatch line is exercised without real SSM / dhanhq.
    seen: dict[str, object] = {}

    def _fake_run(*, request: object, dataset: str, resolved: Path) -> int:
        seen["dataset"] = dataset
        seen["resolved"] = resolved
        return EXIT_OK

    monkeypatch.setattr(cli, "_run_dhan_fetch", _fake_run)
    rc = cmd_fetch_dhan(
        tmp_path, start="2024-06-01", end="2024-06-30", dataset="rolling-options", dry_run=False
    )
    assert rc == EXIT_OK
    assert seen["dataset"] == "rolling-options"
    assert seen["resolved"] == tmp_path


# ── cmd_enrich_greeks_dhan (offline — pure lake I/O) ───────────────────────────


def test_enrich_dhan_spot_dataset_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cmd_enrich_greeks_dhan(tmp_path, dataset="spot")
    assert rc == EXIT_USER_ERROR
    assert "no Greeks" in capsys.readouterr().err


def test_enrich_dhan_empty_lake_is_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # No rolling-ATM targets under feeds/dhan/rolling_options → GREEKS_NOT_RUN → EXIT_OK.
    rc = cmd_enrich_greeks_dhan(tmp_path, dataset="rolling-options")
    assert rc == EXIT_OK
    assert "no rolling-ATM option targets" in capsys.readouterr().out


def test_enrich_dhan_success_reports_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import brutex.adapters.dhan.rolling_option_greeks as greeks_mod

    monkeypatch.setattr(
        greeks_mod,
        "run_dhan_rolling_greeks_pass",
        lambda *, lake_root, log=None: GreeksEnrichOutcome(ok=True, error=None, errored_targets=0),
    )
    rc = cmd_enrich_greeks_dhan(tmp_path, dataset="rolling-options")
    assert rc == EXIT_OK
    assert "errored_targets=0" in capsys.readouterr().out


def test_enrich_dhan_quarantined_is_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import brutex.adapters.dhan.rolling_option_greeks as greeks_mod

    monkeypatch.setattr(
        greeks_mod,
        "run_dhan_rolling_greeks_pass",
        lambda *, lake_root, log=None: GreeksEnrichOutcome(ok=True, error=None, errored_targets=3),
    )
    assert cmd_enrich_greeks_dhan(tmp_path, dataset="rolling-options") == EXIT_INTERNAL_ERROR


def test_enrich_dhan_failed_is_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import brutex.adapters.dhan.rolling_option_greeks as greeks_mod

    monkeypatch.setattr(
        greeks_mod,
        "run_dhan_rolling_greeks_pass",
        lambda *, lake_root, log=None: GreeksEnrichOutcome(
            ok=False, error="RuntimeError: boom", errored_targets=0
        ),
    )
    assert cmd_enrich_greeks_dhan(tmp_path, dataset="rolling-options") == EXIT_INTERNAL_ERROR


def test_enrich_dhan_halt_reraises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import brutex.adapters.dhan.rolling_option_greeks as greeks_mod
    from brutex.data.errors import LakeWriteHalt

    def _halt(*, lake_root: Path, log: object = None) -> GreeksEnrichOutcome:
        raise LakeWriteHalt("disk_full")

    monkeypatch.setattr(greeks_mod, "run_dhan_rolling_greeks_pass", _halt)
    monkeypatch.setattr(cli, "_emit_pull_halt_alert", lambda _exc: None)
    with pytest.raises(LakeWriteHalt):
        cmd_enrich_greeks_dhan(tmp_path, dataset="rolling-options")


def test_enrich_dhan_missing_lake_is_user_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(cli.CliUserError):
        cmd_enrich_greeks_dhan(missing, dataset="rolling-options")
