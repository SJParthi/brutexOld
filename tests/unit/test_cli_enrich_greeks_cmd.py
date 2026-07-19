# pyright: reportPrivateUsage=none
"""Tests for the ``brutex enrich-greeks`` subcommand (DEC-CLI-GREEKS-ENRICH-001).

The #1188 bulk re-enrich lever: parser + ``_dispatch`` wiring, whole-lake
default discovery, the repeatable ``--underlying`` filter, the INCLUSIVE
``--start``/``--end`` semantics (``+1 day`` internally — the boundary pin: a
contract whose only bars sit ON the ``--end`` date IS included), the
wheel-free ``--dry-run`` (counts only — zero writes, zero locks, zero
``fno.greeks_enrich.*`` events), the live path's single-flight lock +
``keep_awake`` envelope (deterministic refusal under a peer pull — the
``test_cli_retry_storm.py`` single-flight idiom), the halt
alert-then-re-raise contract, and the cron-visible exit codes
(``EXIT_OK`` iff ``ok is True and errored_targets == 0``;
``EXIT_INTERNAL_ERROR`` on quarantined targets AND on a whole-pass failure —
the ``lake audit-unique`` precedent, a deliberate divergence from the
never-fail ``fetch`` path).

Everything here is offline + wheel-free: discovery fixtures are 0-byte
``touch`` stubs (discovery reads directory metadata only), the common pass is
stubbed at the :mod:`brutex.data.greeks_enrich_pass` seam (the subcommand's
call-time ``from … import`` reads the CURRENT module attribute — the
``test_pull_historical.py`` monkeypatch idiom), and the account-keyed lock
leg is skipped via a ``CredentialsError`` resolver (the
``test_cli_retry_storm.py`` idiom) so no test ever touches AWS or ``$HOME``.
"""

from __future__ import annotations

import contextlib
import importlib
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from brutex import cli
from brutex.config.credentials import CredentialsError
from brutex.config.feed_scope import FEED_SCOPE_ENV as _FEED_SCOPE_ENV_1934
from brutex.config.feed_scope import resolve_read_scope
from brutex.data import greeks_enrich_pass as gep
from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt
from brutex.data.greeks_enrich_pass import GreeksEnrichOutcome
from brutex.observability import power_keepawake
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

# ── Contract-symbol fixtures (decoded by the COMMON parse_fno_symbol) ────

#: One NIFTY Jan-2024 CE leg (expiry 2024-01-25) — the workhorse contract.
_CE_JAN = "NSE-NIFTY-25Jan24-21000-CE"
#: The sibling PE of the SAME (exchange, underlying, expiry) chain.
_PE_JAN = "NSE-NIFTY-25Jan24-21500-PE"
#: A second expiry of the same underlying (2024-02-29, a real leap day).
_CE_FEB = "NSE-NIFTY-29Feb24-21000-CE"
#: A different underlying on the Jan expiry — a third chain.
_BANK_CE = "NSE-BANKNIFTY-25Jan24-45000-CE"
#: A hyphenated underlying — must be decoded + filter-matched intact.
_BAJAJ_CE = "NSE-BAJAJ-AUTO-25Jan24-9000-CE"
#: The chain's future — NEVER enumerated by discovery (CE/PE only).
_FUT_JAN = "NSE-NIFTY-25Jan24-FUT"


def _seed_contract(lake_root: Path, symbol: str, months: Sequence[tuple[int, int]]) -> None:
    """Create 0-byte ``bars/NSE/FNO/<symbol>/1minute/<yyyy>/<mm>.parquet`` stubs.

    Discovery reads directory METADATA only — ``touch`` files suffice (the
    ``test_greeks_enrich_pass.py`` fixture convention).
    """
    for year, month in months:
        year_dir = lake_root / "bars" / "NSE" / "FNO" / symbol / "1minute" / f"{year:04d}"
        year_dir.mkdir(parents=True, exist_ok=True)
        (year_dir / f"{month:02d}.parquet").touch()


def _seed_default_lake(lake_root: Path) -> None:
    """The shared 4-contract / 3-chain tree (+ a FUT and a junk dir, both skipped)."""
    _seed_contract(lake_root, _CE_JAN, [(2024, 1)])
    _seed_contract(lake_root, _PE_JAN, [(2024, 1)])
    _seed_contract(lake_root, _CE_FEB, [(2024, 2)])
    _seed_contract(lake_root, _BANK_CE, [(2024, 1)])
    _seed_contract(lake_root, _FUT_JAN, [(2024, 1)])
    _seed_contract(lake_root, "not-a-contract", [(2024, 1)])


def _tree(lake_root: Path) -> list[str]:
    """A stable snapshot of every path under *lake_root* (mutation oracle)."""
    return sorted(str(entry.relative_to(lake_root)) for entry in lake_root.rglob("*"))


def _skip_account_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the account-keyed lock leg skip deterministically (no AWS, no $HOME).

    ``_enter_pull_singleflight``'s default resolver reads the AWS-only
    credentials; a ``CredentialsError`` gracefully skips the ACCOUNT lock while
    the LAKE lock still guards — the ``test_cli_retry_storm.py`` idiom.
    """
    credentials = importlib.import_module("brutex.config.credentials")

    def _no_aws() -> Any:
        raise CredentialsError("aws unavailable in tests")

    monkeypatch.setattr(credentials, "resolve_groww_aws_credentials", _no_aws)


def _stub_pass(
    monkeypatch: pytest.MonkeyPatch, outcome: GreeksEnrichOutcome
) -> list[dict[str, Any]]:
    """Stub the COMMON pass at the module seam; return the recorded call list."""
    calls: list[dict[str, Any]] = []

    def _fake(*, build_targets: Any, lake_root: Path) -> GreeksEnrichOutcome:
        calls.append({"build_targets": build_targets, "lake_root": lake_root})
        return outcome

    monkeypatch.setattr(gep, "run_greeks_enrich_pass", _fake)
    return calls


def _forbid_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any invocation of the COMMON pass an immediate test failure."""

    def _never(*_a: Any, **_k: Any) -> GreeksEnrichOutcome:
        raise AssertionError("run_greeks_enrich_pass must not run on this path")

    monkeypatch.setattr(gep, "run_greeks_enrich_pass", _never)


def _record_keep_awake(monkeypatch: pytest.MonkeyPatch, events: list[tuple[str, Any]]) -> None:
    """Replace ``keep_awake`` with a recording no-op context manager."""

    @contextlib.contextmanager
    def _fake_keep_awake(reason: str) -> Generator[None]:
        events.append(("keep_awake", reason))
        yield

    monkeypatch.setattr(power_keepawake, "keep_awake", _fake_keep_awake)


_OK_CLEAN = GreeksEnrichOutcome(ok=True, error=None, errored_targets=0)


# ────────────────────────────────────────────────────────────────────────
# Parser + dispatch wiring
# ────────────────────────────────────────────────────────────────────────


class TestEnrichGreeksParser:
    def test_top_level_help_lists_enrich_greeks(self) -> None:
        """The subcommand is registered and advertised in the top-level help."""
        help_text = cli.build_parser().format_help()
        assert "enrich-greeks" in help_text

    def test_subcommand_help_renders_and_names_every_flag(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``enrich-greeks --help`` renders (exit 0) and documents all five flags."""
        parser = cli.build_parser()
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["enrich-greeks", "--help"])
        assert excinfo.value.code == 0
        help_text = capsys.readouterr().out
        assert "--lake-root" in help_text
        assert "--underlying" in help_text
        assert "--start" in help_text
        assert "--end" in help_text
        assert "--dry-run" in help_text
        # The operator-facing semantics are stated in the help itself
        # (whitespace-normalised — argparse re-wraps long help strings).
        flat = " ".join(help_text.split())
        assert "INCLUSIVE" in flat  # --end is inclusive (+1 day internally)
        assert "repeatable" in flat  # --underlying repeats
        assert "case-sensitive" in flat  # --underlying matching is exact (round-4)
        assert "take no lock" in flat  # --dry-run takes no lock

    def test_parser_defaults(self) -> None:
        """Bare ``enrich-greeks`` = whole lake, no window, live (not dry-run)."""
        args = cli.build_parser().parse_args(["enrich-greeks"])
        assert args.command == "enrich-greeks"
        assert args.lake_root is None
        assert args.underlying is None
        assert args.start is None
        assert args.end is None
        assert args.dry_run is False

    def test_parser_underlying_repeatable_and_lake_root_path(self) -> None:
        """``--underlying`` appends (repeatable) and ``--lake-root`` is a Path."""
        args = cli.build_parser().parse_args(
            [
                "enrich-greeks",
                "--lake-root",
                "/tmp/lake",
                "--underlying",
                "NIFTY",
                "--underlying",
                "BAJAJ-AUTO",
            ]
        )
        assert args.lake_root == Path("/tmp/lake")
        assert args.underlying == ["NIFTY", "BAJAJ-AUTO"]

    def test_dispatch_wires_enrich_greeks(self, tmp_path: Path) -> None:
        """``main`` threads every parsed flag into ``cmd_enrich_greeks`` verbatim."""
        with mock.patch.object(cli, "cmd_enrich_greeks", return_value=0) as handler:
            rc = cli.main(
                [
                    "enrich-greeks",
                    "--lake-root",
                    str(tmp_path),
                    "--underlying",
                    "NIFTY",
                    "--underlying",
                    "BANKNIFTY",
                    "--start",
                    "2024-01-01",
                    "--end",
                    "2024-01-31",
                    "--dry-run",
                ]
            )
        assert rc == cli.EXIT_OK
        args, kwargs = handler.call_args
        assert args == (tmp_path,)
        assert kwargs == {
            "underlying": ["NIFTY", "BANKNIFTY"],
            "start": "2024-01-01",
            "end": "2024-01-31",
            "dry_run": True,
        }

    def test_dispatch_falls_through_to_later_commands(self) -> None:
        """A later command (``doctor``) passes the enrich-greeks arm untouched."""
        with (
            mock.patch.object(cli, "cmd_doctor", return_value=0) as doctor,
            mock.patch.object(cli, "cmd_enrich_greeks") as enrich,
        ):
            rc = cli.main(["doctor"])
        assert rc == cli.EXIT_OK
        doctor.assert_called_once()
        enrich.assert_not_called()


# ────────────────────────────────────────────────────────────────────────
# Date validation — user errors before any work
# ────────────────────────────────────────────────────────────────────────


class TestEnrichGreeksDates:
    def test_bad_start_date_is_user_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-ISO ``--start`` exits EXIT_USER_ERROR before any discovery."""

        def _never(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("discovery must not run on a bad date")

        monkeypatch.setattr(gep, "discover_option_windows", _never)
        _forbid_pass(monkeypatch)
        rc = cli.cmd_enrich_greeks(
            tmp_path, underlying=None, start="not-a-date", end=None, dry_run=True
        )
        assert rc == cli.EXIT_USER_ERROR
        assert "[brutex]" in capsys.readouterr().err

    def test_bad_end_date_is_user_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-ISO ``--end`` (start absent) exits EXIT_USER_ERROR too."""
        _forbid_pass(monkeypatch)
        rc = cli.cmd_enrich_greeks(
            tmp_path, underlying=None, start=None, end="2024-13-01", dry_run=True
        )
        assert rc == cli.EXIT_USER_ERROR
        assert "[brutex]" in capsys.readouterr().err

    def test_start_after_end_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--start`` strictly after ``--end`` is a deterministic user error."""
        _forbid_pass(monkeypatch)
        rc = cli.cmd_enrich_greeks(
            tmp_path, underlying=None, start="2024-02-01", end="2024-01-01", dry_run=True
        )
        assert rc == cli.EXIT_USER_ERROR
        assert "--start 2024-02-01 is after --end 2024-01-01" in capsys.readouterr().err

    def test_start_equal_end_accepted_as_single_day(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--start == --end`` is a valid one-day INCLUSIVE window (half-open +1)."""
        rc = cli.cmd_enrich_greeks(
            tmp_path, underlying=None, start="2024-01-15", end="2024-01-15", dry_run=True
        )
        assert rc == cli.EXIT_OK
        # The printed window shows the internal half-open end: 15th + 1 day.
        assert "window=[2024-01-15 → 2024-01-16)" in capsys.readouterr().out

    def test_end_date_max_is_clean_user_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--end 9999-12-31`` (``date.max``) is refused cleanly, never a traceback.

        Review round-1 regression (findings 1/4): the +1-day widening raised an
        uncaught ``OverflowError`` for the ONE valid ISO date it cannot widen —
        it must get the same one-line ``[brutex]`` + ``EXIT_USER_ERROR`` every
        other malformed date gets (DEC-CLI-USER-ERROR-HYGIENE-001).
        """

        def _never(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("discovery must not run on a refused --end")

        monkeypatch.setattr(gep, "discover_option_windows", _never)
        _forbid_pass(monkeypatch)
        rc = cli.cmd_enrich_greeks(
            tmp_path, underlying=None, start=None, end="9999-12-31", dry_run=True
        )
        assert rc == cli.EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert "[brutex] --end 9999-12-31 is out of range" in err
        assert "widened +1 day" in err

    def test_end_date_max_via_main_returns_user_error_not_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The real-CLI shape of the same pin: ``main`` exits USER_ERROR, no raise."""
        rc = cli.main(
            ["enrich-greeks", "--lake-root", str(tmp_path), "--end", "9999-12-31", "--dry-run"]
        )
        assert rc == cli.EXIT_USER_ERROR
        assert "--end 9999-12-31 is out of range" in capsys.readouterr().err


# ────────────────────────────────────────────────────────────────────────
# Lake-root existence — the read-command guard (review round-1, finding 5)
# ────────────────────────────────────────────────────────────────────────


class TestEnrichGreeksLakeRoot:
    """A typo'd ``--lake-root`` must fail LOUDLY, never exit green.

    Review round-1 regression (finding 5): ``enrich-greeks`` on a nonexistent
    lake previously discovered ``()``, logged an all-zeros ``done``, printed
    ``errored_targets=0`` with ``EXIT_OK`` — and the live path even
    materialised ``<typo>/.locks/`` — the exact silent-illusion class
    ``_require_lake_dir`` (DEC-CLI-USER-ERROR-HYGIENE-001) prevents on every
    other lake-READING command.
    """

    def test_missing_lake_root_refused_on_dry_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--dry-run`` over a phantom lake refuses BEFORE any discovery."""

        def _never(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("discovery must not run on a missing lake root")

        monkeypatch.setattr(gep, "discover_option_windows", _never)
        _forbid_pass(monkeypatch)
        missing = tmp_path / "tpyo"
        rc = cli.main(["enrich-greeks", "--lake-root", str(missing), "--dry-run"])
        assert rc == cli.EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert f"lake root not found: {missing}" in err
        assert "brutex enrich-greeks` reads an existing lake" in err
        assert not missing.exists()  # nothing materialised

    def test_missing_lake_root_refused_live_and_no_locks_materialised(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The live path refuses too — and never creates the phantom ``.locks/``."""
        _skip_account_lock(monkeypatch)
        _forbid_pass(monkeypatch)
        locks: list[Any] = []

        def _never_lock(*_a: Any, **_k: Any) -> bool:
            locks.append(_a)
            raise AssertionError("the single-flight gate must not run on a missing lake root")

        monkeypatch.setattr(cli, "_enter_pull_singleflight", _never_lock)
        missing = tmp_path / "tpyo"
        rc = cli.main(["enrich-greeks", "--lake-root", str(missing)])
        assert rc == cli.EXIT_USER_ERROR
        assert f"lake root not found: {missing}" in capsys.readouterr().err
        assert locks == []  # refused BEFORE the lock leg
        assert not missing.exists()  # no .locks/ dir conjured
        assert not (missing / ".locks").exists()


# ────────────────────────────────────────────────────────────────────────
# --dry-run — counts only; zero writes, zero locks, zero events
# ────────────────────────────────────────────────────────────────────────


class TestEnrichGreeksDryRun:
    def test_dry_run_counts_whole_lake_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No filters = the WHOLE lake: CE/PE counted, FUT + junk skipped; no
        lock consulted, no pass invoked, no events, lake bytes untouched."""
        _seed_default_lake(tmp_path)
        before = _tree(tmp_path)
        _forbid_pass(monkeypatch)

        def _never_lock(*_a: Any, **_k: Any) -> bool:
            raise AssertionError("--dry-run must never consult the single-flight gate")

        monkeypatch.setattr(cli, "_enter_pull_singleflight", _never_lock)
        with capture_logs() as logs:
            rc = cli.cmd_enrich_greeks(
                tmp_path, underlying=None, start=None, end=None, dry_run=True
            )
        assert rc == cli.EXIT_OK
        out = capsys.readouterr().out
        assert (
            "[brutex] enrich-greeks --dry-run — contracts=4 chains=3 "
            "window=[lake-start → lake-end) (nothing enriched)" in out
        )
        assert _tree(tmp_path) == before  # zero writes
        assert [e for e in logs if str(e["event"]).startswith("fno.greeks_enrich")] == []

    def test_dry_run_is_wheel_free_and_lockless_under_peer_pull(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--dry-run`` succeeds with the wheel POISONED and a peer pull holding
        the lake lock — proving zero ``trading_core`` imports and zero locks
        (mirrors ``test_cmd_fetch_dry_run_does_not_take_singleflight_lock``)."""
        from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock

        _seed_contract(tmp_path, _CE_JAN, [(2024, 1)])
        _forbid_pass(monkeypatch)
        monkeypatch.setitem(sys.modules, "trading_core", None)
        monkeypatch.setitem(sys.modules, "brutex.data.greeks_enrich", None)
        with pull_singleflight_lock(tmp_path):  # a peer pull is mid-flight
            rc = cli.cmd_enrich_greeks(
                tmp_path, underlying=None, start=None, end=None, dry_run=True
            )
        assert rc == cli.EXIT_OK
        assert "contracts=1 chains=1" in capsys.readouterr().out

    def test_dry_run_underlying_filter_repeatable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--underlying`` filters discovery; repeating it unions the tokens;
        a hyphenated underlying (BAJAJ-AUTO) matches intact."""
        _seed_default_lake(tmp_path)
        _seed_contract(tmp_path, _BAJAJ_CE, [(2024, 1)])

        rc = cli.cmd_enrich_greeks(
            tmp_path, underlying=["NIFTY"], start=None, end=None, dry_run=True
        )
        assert rc == cli.EXIT_OK
        assert "contracts=3 chains=2" in capsys.readouterr().out  # CE+PE Jan, CE Feb

        rc = cli.cmd_enrich_greeks(
            tmp_path, underlying=["NIFTY", "BANKNIFTY"], start=None, end=None, dry_run=True
        )
        assert rc == cli.EXIT_OK
        assert "contracts=4 chains=3" in capsys.readouterr().out

        # DEC-UNIVERSE-INDEX-ONLY-001 §6 Law 2 — the BAJAJ-AUTO leg flipped to
        # pin HEAD behavior: under the shipped index4 default the stored stock
        # contract is scope-gated OUT of targeting (zero contracts, the honest
        # counted stderr line); the merge-base behavior lives on in the
        # full-arm twin below.
        rc = cli.cmd_enrich_greeks(
            tmp_path, underlying=["BAJAJ-AUTO"], start=None, end=None, dry_run=True
        )
        assert rc == cli.EXIT_OK
        captured = capsys.readouterr()
        assert "contracts=0 chains=0" in captured.out
        assert (
            "1 stored contract(s) excluded by scope (DEC-UNIVERSE-INDEX-ONLY-001)" in captured.err
        )

    def test_dry_run_hyphenated_underlying_matches_under_full_scope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The full-arm twin (§6 Law 2): a hyphenated underlying (BAJAJ-AUTO)
        still matches intact when the scope admits stocks — the retained
        recovery/filter machinery keeps its net verbatim."""
        monkeypatch.setenv(_FEED_SCOPE_ENV_1934, "full")
        _seed_default_lake(tmp_path)
        _seed_contract(tmp_path, _BAJAJ_CE, [(2024, 1)])
        rc = cli.cmd_enrich_greeks(
            tmp_path, underlying=["BAJAJ-AUTO"], start=None, end=None, dry_run=True
        )
        assert rc == cli.EXIT_OK
        captured = capsys.readouterr()
        assert "contracts=1 chains=1" in captured.out
        assert "excluded by scope" not in captured.err

    def test_dry_run_end_boundary_inclusive(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """THE +1-day boundary pin: a contract whose only bars sit ON the
        ``--end`` date is INCLUDED (inclusive CLI end → half-open +1 internal);
        an ``--end`` the day before excludes it."""
        _seed_contract(tmp_path, _CE_FEB, [(2024, 2)])  # bars only in Feb 2024

        rc = cli.cmd_enrich_greeks(
            tmp_path, underlying=None, start=None, end="2024-01-31", dry_run=True
        )
        assert rc == cli.EXIT_OK
        assert "contracts=0 chains=0" in capsys.readouterr().out  # Jan end excludes Feb

        rc = cli.cmd_enrich_greeks(
            tmp_path, underlying=None, start=None, end="2024-02-01", dry_run=True
        )
        assert rc == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "contracts=1 chains=1" in out  # bars ON --end are in scope
        assert "window=[lake-start → 2024-02-02)" in out  # the internal half-open end

    def test_dry_run_start_only_window_text(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A ``--start``-only run clamps from the start and prints an open end."""
        _seed_contract(tmp_path, _CE_JAN, [(2024, 1)])
        rc = cli.cmd_enrich_greeks(
            tmp_path, underlying=None, start="2024-01-01", end=None, dry_run=True
        )
        assert rc == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "contracts=1 chains=1" in out
        assert "window=[2024-01-01 → lake-end)" in out

    def test_dry_run_discovery_failure_exits_internal_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A discovery blow-up is caught: one stderr line + EXIT_INTERNAL_ERROR."""

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("walk exploded")

        # DEC-UNIVERSE-INDEX-ONLY-001: the dry-run arm now walks the SCOPED
        # twin (discover_option_windows delegates to it).
        monkeypatch.setattr(gep, "scoped_option_windows", _boom)
        rc = cli.cmd_enrich_greeks(tmp_path, underlying=None, start=None, end=None, dry_run=True)
        assert rc == cli.EXIT_INTERNAL_ERROR
        assert "enrich-greeks --dry-run discovery FAILED: walk exploded" in capsys.readouterr().err


# ────────────────────────────────────────────────────────────────────────
# Live path — single-flight + keep_awake + the COMMON pass
# ────────────────────────────────────────────────────────────────────────


class TestEnrichGreeksLive:
    def test_live_takes_singleflight_keep_awake_then_runs_pass_whole_lake(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ordering law: single-flight lock → keep_awake → the COMMON pass; the
        whole-lake default threads ``underlyings=None`` + no clamps; there is NO
        ``has_option_candles`` pre-gate (an empty lake still runs the pass and
        exits 0 — the operator explicitly asked); the CLI's counting wrapper
        delegates to the builder's closure exactly once and the clean summary
        echoes ``contracts=0`` — with NO zero-match WARNING, because no
        explicit ``--underlying`` filter was given (review round-4)."""
        _skip_account_lock(monkeypatch)
        events: list[tuple[str, Any]] = []
        real_enter = cli._enter_pull_singleflight

        def _spy_enter(stack: Any, lake_root: Path) -> bool:
            events.append(("singleflight", lake_root))
            return real_enter(stack, lake_root)

        monkeypatch.setattr(cli, "_enter_pull_singleflight", _spy_enter)
        _record_keep_awake(monkeypatch, events)

        builder_calls: list[dict[str, Any]] = []
        sentinel_invocations: list[None] = []

        def _sentinel_builder() -> list[Any]:
            sentinel_invocations.append(None)
            return []

        def _fake_builder(lake_root: Path, **kwargs: Any) -> Any:
            builder_calls.append({"lake_root": lake_root, **kwargs})
            return _sentinel_builder

        monkeypatch.setattr(gep, "lake_target_builder", _fake_builder)

        pass_calls: list[dict[str, Any]] = []

        def _fake_pass(*, build_targets: Any, lake_root: Path) -> GreeksEnrichOutcome:
            events.append(("pass", lake_root))
            # Mirror the real pass contract: build_targets is ALWAYS invoked
            # before an ok=True return — this drives the counting wrapper.
            pass_calls.append({"targets": list(build_targets()), "lake_root": lake_root})
            return _OK_CLEAN

        monkeypatch.setattr(gep, "run_greeks_enrich_pass", _fake_pass)

        rc = cli.cmd_enrich_greeks(tmp_path, underlying=None, start=None, end=None, dry_run=False)
        assert rc == cli.EXIT_OK
        assert events == [
            ("singleflight", tmp_path),
            ("keep_awake", "brutex enrich-greeks"),
            ("pass", tmp_path),
        ]
        # DEC-UNIVERSE-INDEX-ONLY-001 (design §3 R4): the CLI threads the
        # read-union scope + the counting callback into the builder.
        (call,) = builder_calls
        assert call.pop("scope") == resolve_read_scope()
        assert callable(call.pop("on_scope_excluded"))
        assert [call] == [{"lake_root": tmp_path, "underlyings": None, "start": None, "end": None}]
        assert pass_calls == [{"targets": [], "lake_root": tmp_path}]
        assert sentinel_invocations == [None]  # the counting wrapper delegates ONCE
        captured = capsys.readouterr()
        assert (
            f"[brutex] enrich-greeks — window=[lake-start → lake-end) lake: {tmp_path}"
            in captured.err
        )
        assert "[brutex] enrich-greeks done — contracts=0 errored_targets=0" in captured.out
        # Whole-lake (tokens=None) zero stays QUIET — the empty-lake exit-0
        # contract is unchanged; only an EXPLICIT filter zero-match warns.
        assert "WARNING" not in captured.err

    def test_live_filters_thread_inclusive_end_to_builder(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeatable ``--underlying`` tokens become ONE frozenset and the
        INCLUSIVE ``--end`` reaches the builder widened ``+1 day``; a stubbed
        pass that never invokes the builder exercises the DEFENSIVE ``?`` leg
        (production-unreachable with the real pass, whose ``ok=True`` implies
        the build ran — a pre-build failure takes the FAILED branch instead):
        the count stays honestly UNKNOWN (``contracts=?`` — never a fabricated
        0, and never a spurious zero-match WARNING; review round-4/round-5)."""
        _skip_account_lock(monkeypatch)
        builder_calls: list[dict[str, Any]] = []

        def _fake_builder(lake_root: Path, **kwargs: Any) -> Any:
            builder_calls.append({"lake_root": lake_root, **kwargs})
            return list

        monkeypatch.setattr(gep, "lake_target_builder", _fake_builder)
        _stub_pass(monkeypatch, _OK_CLEAN)

        rc = cli.cmd_enrich_greeks(
            tmp_path,
            underlying=["NIFTY", "BANKNIFTY"],
            start="2024-01-01",
            end="2024-01-31",
            dry_run=False,
        )
        assert rc == cli.EXIT_OK
        # DEC-UNIVERSE-INDEX-ONLY-001: the scope thread rides beside the filters.
        (call,) = builder_calls
        assert call.pop("scope") == resolve_read_scope()
        assert callable(call.pop("on_scope_excluded"))
        assert [call] == [
            {
                "lake_root": tmp_path,
                "underlyings": frozenset({"NIFTY", "BANKNIFTY"}),
                "start": date(2024, 1, 1),
                "end": date(2024, 2, 1),  # inclusive --end 2024-01-31 → half-open +1
            }
        ]
        captured = capsys.readouterr()
        assert "window=[2024-01-01 → 2024-02-01)" in captured.err
        # The stub pass never invoked the builder, so the count is honestly
        # UNKNOWN — rendered "?" (never a fabricated 0, Golden Rule #10) and
        # never treated as an explicit-filter zero-match (no WARNING). This
        # ok=True-without-build shape is impossible with the REAL pass (it
        # returns ok=True only after list(build_targets())) — the stub pins
        # the DEFENSIVE belt against a future pass-contract change.
        assert "[brutex] enrich-greeks done — contracts=? errored_targets=0" in captured.out
        assert "WARNING" not in captured.err

    def test_live_scope_excluded_census_is_printed_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEC-UNIVERSE-INDEX-ONLY-001 (design §3 R4): the LIVE arm's counted
        census reaches stderr — the ``_capture_scope_excluded`` value flow +
        the ``_print_greeks_scope_excluded("enrich-greeks", …)`` call at the
        clean-done render (the greeks-backfill CLI + the ``--dry-run`` arm's
        live sibling). The builder fake is FAITHFUL to the real
        ``lake_target_builder``: ``on_scope_excluded`` fires INSIDE the
        returned closure (discovery time, inside the fence) with the NONZERO
        count of scope-gated stored contracts. Dropping the live print — or
        zeroing the captured count — silences a stock-bearing lake's bulk
        pass about the contracts the scope gated out; both mutants fail
        HERE."""
        _skip_account_lock(monkeypatch)
        builder_calls: list[dict[str, Any]] = []

        def _fake_builder(lake_root: Path, **kwargs: Any) -> Any:
            builder_calls.append({"lake_root": lake_root, **kwargs})
            on_scope_excluded = kwargs["on_scope_excluded"]

            def _build() -> list[Any]:
                on_scope_excluded(3)  # 3 stored stock contracts gated out
                return []

            return _build

        monkeypatch.setattr(gep, "lake_target_builder", _fake_builder)

        def _fake_pass(*, build_targets: Any, lake_root: Path) -> GreeksEnrichOutcome:
            # Mirror the real pass contract: build_targets runs before ok=True.
            list(build_targets())
            return _OK_CLEAN

        monkeypatch.setattr(gep, "run_greeks_enrich_pass", _fake_pass)

        rc = cli.cmd_enrich_greeks(tmp_path, underlying=None, start=None, end=None, dry_run=False)
        assert rc == cli.EXIT_OK
        (call,) = builder_calls
        assert callable(call["on_scope_excluded"])
        captured = capsys.readouterr()
        assert "[brutex] enrich-greeks done — contracts=0 errored_targets=0" in captured.out
        assert (
            "[brutex] enrich-greeks — 3 stored contract(s) excluded by scope "
            "(DEC-UNIVERSE-INDEX-ONLY-001); their Greeks stamps stay inert" in captured.err
        )

    def test_live_refused_under_peer_pull(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A peer pull holding the lake lock refuses the live run deterministically:
        EXIT_USER_ERROR, no keep_awake, no pass (never a second concurrent pull)."""
        from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock

        _skip_account_lock(monkeypatch)
        _forbid_pass(monkeypatch)
        awake_events: list[tuple[str, Any]] = []
        _record_keep_awake(monkeypatch, awake_events)

        with pull_singleflight_lock(tmp_path):  # a peer pull is mid-flight on this lake
            rc = cli.cmd_enrich_greeks(
                tmp_path, underlying=None, start=None, end=None, dry_run=False
            )
        assert rc == cli.EXIT_USER_ERROR
        err = capsys.readouterr().err
        assert "a pull is already running for this lake" in err
        assert "refusing to start a second concurrent pull" in err
        assert awake_events == []  # refused BEFORE keep_awake engaged

    def test_exit_internal_error_on_errored_targets(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ok=True`` with quarantined targets reads RED for cron (exit 2) —
        the deliberate divergence from the never-fail fetch path."""
        _skip_account_lock(monkeypatch)
        _stub_pass(monkeypatch, GreeksEnrichOutcome(ok=True, error=None, errored_targets=2))
        rc = cli.cmd_enrich_greeks(tmp_path, underlying=None, start=None, end=None, dry_run=False)
        assert rc == cli.EXIT_INTERNAL_ERROR
        err = capsys.readouterr().err
        assert "completed with 2 quarantined target(s)" in err
        assert "idempotent, re-run after fixing" in err

    def test_exit_internal_error_on_whole_pass_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ok=False`` (e.g. a wheel-absent host) exits 2 naming the error."""
        _skip_account_lock(monkeypatch)
        _stub_pass(
            monkeypatch,
            GreeksEnrichOutcome(
                ok=False, error="ImportError: No module named 'trading_core'", errored_targets=0
            ),
        )
        rc = cli.cmd_enrich_greeks(tmp_path, underlying=None, start=None, end=None, dry_run=False)
        assert rc == cli.EXIT_INTERNAL_ERROR
        assert (
            "enrich-greeks FAILED (ImportError: No module named 'trading_core')"
            in capsys.readouterr().err
        )

    @pytest.mark.parametrize(
        "halt",
        [LakeWriteHalt("disk_full"), GreeksEnrichPoolBroken("worker died mid-chain")],
        ids=["lake_write_halt", "pool_broken"],
    )
    def test_halt_alerts_once_then_reraises_and_releases_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, halt: Exception
    ) -> None:
        """A pull-wide HOST halt fires ONE CRITICAL alert then re-raises (the
        ``_one_fetch_pass`` convention); the single-flight lock is released."""
        from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock

        _skip_account_lock(monkeypatch)

        def _raising_pass(*, build_targets: Any, lake_root: Path) -> GreeksEnrichOutcome:
            raise halt

        monkeypatch.setattr(gep, "run_greeks_enrich_pass", _raising_pass)
        halts: list[Exception] = []
        monkeypatch.setattr(cli, "_emit_pull_halt_alert", halts.append)

        with pytest.raises(type(halt)):
            cli.cmd_enrich_greeks(tmp_path, underlying=None, start=None, end=None, dry_run=False)
        assert halts == [halt]  # exactly one alert, the propagating exception itself
        # The ExitStack released the lock on the raise — a fresh acquire succeeds.
        with pull_singleflight_lock(tmp_path):
            pass


# ────────────────────────────────────────────────────────────────────────
# Zero-match --underlying visibility (review round-4)
# ────────────────────────────────────────────────────────────────────────


class TestEnrichGreeksZeroMatchVisibility:
    """An explicit ``--underlying`` matching NOTHING must never read green-silent.

    Review round-4 regression: ``brutex enrich-greeks --underlying nifty``
    (lowercase, or any typo) over a lake that stores NIFTY previously ran the
    full live pass over ZERO targets and exited 0 printing only
    ``errored_targets=0`` — the same silent-green typo class the round-1
    ``_require_lake_dir`` fix targeted, one level down (the token filter is
    exact + case-sensitive frozenset membership against the canonical-
    uppercase ``parse_fno_symbol`` underlyings). The live summary now echoes
    ``contracts=N`` and an explicit filter with zero matches prints one loud
    stderr WARNING naming the tokens; the exit code is deliberately unchanged
    (the pass genuinely completed — warn, not fail).
    """

    @staticmethod
    def _wire_filter_faithful_seams(
        monkeypatch: pytest.MonkeyPatch, *, stored: str = "NIFTY"
    ) -> None:
        """Stub builder + pass at the seam, faithful to the real contracts.

        The builder honors the exact/case-sensitive ``underlyings`` filter the
        way the real discovery does (one dummy target iff *stored* is in the
        token set), and the pass invokes ``build_targets`` before returning
        ``ok=True`` — exactly like the real ``run_greeks_enrich_pass`` — so the
        CLI's counting wrapper is exercised end-to-end while the file stays
        wheel-free (the true full-stack repro lives in
        ``tests/integration/test_cli_greeks_enrich_e2e.py``).
        """

        def _fake_builder(
            lake_root: Path,
            *,
            underlyings: frozenset[str] | None,
            start: Any,
            end: Any,
            scope: Any = None,
            on_scope_excluded: Any = None,
        ) -> Any:
            def _build() -> list[Any]:
                if on_scope_excluded is not None:
                    on_scope_excluded(0)  # faithful: the real builder fires the seam
                if underlyings is None or stored in underlyings:
                    return [object()]
                return []

            return _build

        monkeypatch.setattr(gep, "lake_target_builder", _fake_builder)

        def _invoking_pass(*, build_targets: Any, lake_root: Path) -> GreeksEnrichOutcome:
            list(build_targets())
            return _OK_CLEAN

        monkeypatch.setattr(gep, "run_greeks_enrich_pass", _invoking_pass)

    def test_lowercase_token_zero_match_warns_and_echoes_contracts_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--underlying nifty`` over a NIFTY lake warns LOUDLY, still exit 0."""
        _skip_account_lock(monkeypatch)
        self._wire_filter_faithful_seams(monkeypatch)
        rc = cli.cmd_enrich_greeks(
            tmp_path, underlying=["nifty"], start=None, end=None, dry_run=False
        )
        assert rc == cli.EXIT_OK  # the pass genuinely completed — warn, not fail
        captured = capsys.readouterr()
        assert "WARNING: --underlying matched ZERO stored option contracts" in captured.err
        assert "(tokens: nifty)" in captured.err
        assert "exact and case-sensitive" in captured.err
        assert "--dry-run" in captured.err  # the operator is pointed at the scope probe
        assert "[brutex] enrich-greeks done — contracts=0 errored_targets=0" in captured.out

    def test_exact_token_match_echoes_count_and_stays_quiet(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact token enriches: ``contracts=1`` echoed, no WARNING."""
        _skip_account_lock(monkeypatch)
        self._wire_filter_faithful_seams(monkeypatch)
        rc = cli.cmd_enrich_greeks(
            tmp_path, underlying=["NIFTY"], start=None, end=None, dry_run=False
        )
        assert rc == cli.EXIT_OK
        captured = capsys.readouterr()
        assert "WARNING" not in captured.err
        assert "[brutex] enrich-greeks done — contracts=1 errored_targets=0" in captured.out

    def test_multi_token_zero_match_warning_names_tokens_sorted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The warning names EVERY token, sorted — a deterministic operator hint."""
        _skip_account_lock(monkeypatch)
        self._wire_filter_faithful_seams(monkeypatch, stored="BANKNIFTY")
        rc = cli.cmd_enrich_greeks(
            tmp_path, underlying=["nifty", "banknifty"], start=None, end=None, dry_run=False
        )
        assert rc == cli.EXIT_OK
        assert "(tokens: banknifty, nifty)" in capsys.readouterr().err
