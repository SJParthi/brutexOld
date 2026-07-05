"""Unit tests for ``brutex.doctor_live`` — one test per diagnostic line, fakes only.

DEC-DOCTOR-LIVE-PROOF-KIT-001. Every failure mode from the pull-divergence
register that a kit stage diagnoses is SIMULATED here with the existing fake
seams (``FakeAwsClient``, a handler-driven fake SDK, injected clocks/HTTP) and
the exact operator-facing line is asserted. The live path itself remains
UNVERIFIED until the kit runs on the operator's Mac — that is the tool's
purpose, not a gap these tests can close.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from brutex import doctor_live as dl
from brutex.adapters.groww.constants import (
    GROWW_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT,
    INSTRUMENT_CSV_COLUMNS,
)
from brutex.adapters.rate_common import RateController
from brutex.config.credentials_types import CredentialsError, GrowwFileCredentials

from tests.fakes.fake_aws_client import FakeAwsClient

# ── shared fixture data ─────────────────────────────────────────────────

_SECRET = "JBSWY3DPEHPK3PXP"  # valid base32
_TOKEN_PARAM = GROWW_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT
_NOW = datetime(2026, 7, 2, 6, 0, tzinfo=UTC)
_PROBE_DAY = date(2026, 7, 1)

_MASTER_HEADER = ",".join(INSTRUMENT_CSV_COLUMNS)


def _master_row(**over: str) -> str:
    cells = dict.fromkeys(INSTRUMENT_CSV_COLUMNS, "")
    cells.update(over)
    return ",".join(cells[col] for col in INSTRUMENT_CSV_COLUMNS)


def _master_csv(*, drop: frozenset[str] = frozenset()) -> str:
    rows = [
        _master_row(
            exchange="NSE",
            exchange_token="NIFTY",
            trading_symbol="NIFTY",
            groww_symbol="NSE-NIFTY",
            name="NIFTY 50",
            instrument_type="IDX",
            segment="CASH",
        ),
        _master_row(
            exchange="NSE",
            exchange_token="BANKNIFTY",
            trading_symbol="BANKNIFTY",
            groww_symbol="NSE-BANKNIFTY",
            name="NIFTY BANK",
            instrument_type="IDX",
            segment="CASH",
        ),
        _master_row(
            exchange="BSE",
            exchange_token="SENSEX",
            trading_symbol="SENSEX",
            groww_symbol="BSE-SENSEX",
            name="SENSEX",
            instrument_type="IDX",
            segment="CASH",
        ),
        _master_row(
            exchange="NSE",
            exchange_token="INDIAVIX",
            trading_symbol="INDIAVIX",
            groww_symbol="NSE-INDIAVIX",
            name="India Vix",
            instrument_type="IDX",
            segment="CASH",
        ),
        _master_row(
            exchange="NSE",
            exchange_token="3787",
            trading_symbol="WIPRO",
            groww_symbol="NSE-WIPRO",
            name="Wipro",
            instrument_type="EQ",
            segment="CASH",
            series="EQ",
            lot_size="1",
            tick_size="0.05",
        ),
    ]
    kept = [row for row in rows if not any(symbol in row for symbol in drop)]
    return "\n".join([_MASTER_HEADER, *kept]) + "\n"


_BHAVCOPY_HEADER = (
    "Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value"
)
_BHAVCOPY_OK = f"{_BHAVCOPY_HEADER}\nNifty 50,01-07-2026,100.00,101.00,99.00,100.50\n"
_BHAVCOPY_DRIFTED = f"{_BHAVCOPY_HEADER}\nNifty 50,01-07-2026,100.00,101.00,99.00,106.95\n"


def full_day_candles(day: str = "2026-07-01", *, epoch: bool = False) -> list[list[object]]:
    """375 in-session 1-minute rows (09:15 → 15:29 IST), string or epoch stamped."""
    rows: list[list[object]] = []
    base = datetime.fromisoformat(f"{day}T09:15:00")
    for minute in range(375):
        stamp_dt = base.replace(hour=9 + (15 + minute) // 60, minute=(15 + minute) % 60, second=0)
        if epoch:
            # The parser treats int stamps as TRUE-UTC epoch: the instant of an
            # IST wall time is (wall-as-UTC seconds − 19800).
            stamp: object = int(stamp_dt.replace(tzinfo=UTC).timestamp()) - 19800
        else:
            stamp = stamp_dt.strftime("%Y-%m-%dT%H:%M:%S")
        rows.append([stamp, 100.0, 101.0, 99.0, 100.5, 0, None])
    return rows


Handler = Callable[[dict[str, object]], Mapping[str, object]]


def default_handler(kwargs: dict[str, object]) -> Mapping[str, object]:
    """A benign vendor: full trading day for whatever window is asked."""
    day = str(kwargs["start_time"])[:10]
    return {"candles": full_day_candles(day)}


class FakeKitSDK:
    """A handler-driven fake ``GrowwSDK`` with the 1.5.x private-method surface."""

    def __init__(self, box: dict[str, Handler]) -> None:
        self._box = box
        self.calls: list[dict[str, object]] = []

    def get_historical_candles(self, **kwargs: object) -> Mapping[str, object]:
        self.calls.append(dict(kwargs))
        return self._box["handler"](dict(kwargs))

    def get_expiries(self, **_kwargs: object) -> Mapping[str, object]:
        return {"expiries": []}

    def get_contracts(self, **_kwargs: object) -> Mapping[str, object]:
        return {"contracts": []}

    def _request_get(self) -> None:  # pragma: no cover — presence-only shape probe
        return None

    def _request_post(self) -> None:  # pragma: no cover — presence-only shape probe
        return None

    def _request_put(self) -> None:  # pragma: no cover — presence-only shape probe
        return None


class BareGrowwAPIException(Exception):
    """A fake of the SDK's BARE base exception (matched by class name + code)."""

    def __init__(self, message: str, code: object = None) -> None:
        super().__init__(message)
        self.code = code


BareGrowwAPIException.__name__ = "GrowwAPIException"
BareGrowwAPIException.__qualname__ = "GrowwAPIException"


class TypedAuthException(Exception):
    """A fake of the SDK's TYPED auth exception (classified by class name)."""


TypedAuthException.__name__ = "GrowwAPIAuthenticationException"
TypedAuthException.__qualname__ = "GrowwAPIAuthenticationException"


def raising_handler(exc_factory: Callable[[], Exception]) -> Handler:
    def _raise(_kwargs: dict[str, object]) -> Mapping[str, object]:
        raise exc_factory()

    return _raise


def _http_get_ok(url: str) -> str:
    if "ind_close_all" in url:
        return _BHAVCOPY_OK
    return _master_csv()


def make_deps(tmp_path: Path, **overrides: Any) -> tuple[dl.KitDeps, dict[str, Handler]]:
    """Benign fake deps + the mutable SDK-handler box tests re-script per stage."""
    box: dict[str, Handler] = {"handler": default_handler}
    clock = {"mono": 0.0}

    def _monotonic() -> float:
        clock["mono"] += 0.05
        return clock["mono"]

    def _controller() -> RateController:
        return RateController(clock=_monotonic, sleep=lambda _s: None, ceiling_rps=10_000.0)

    aws = FakeAwsClient(parameters={_TOKEN_PARAM: "shared-token"})
    defaults: dict[str, Any] = {
        "now_utc": lambda: _NOW,
        "monotonic": _monotonic,
        "git_sha": lambda: "abc1234",
        "behind_origin": lambda: 0,
        "growwapi_version": lambda: "1.5.2",
        "package_location": lambda: "/Users/sj/bruteX/brutex",
        "python_version": lambda: "3.12.4",
        "getproxies": dict,
        "env": {},
        "home": tmp_path / "home",
        "cwd": tmp_path / "cwd",
        "repo_root": None,
        "ismount": lambda _p: True,
        "resolve_creds": lambda: GrowwFileCredentials(api_key="key", totp_secret=_SECRET),
        "broker_region": lambda: "ap-south-1",
        "totp_minter": lambda _s: "123456",
        "clock_offset_s": lambda: 0.4,
        "aws_client_factory": lambda: aws,
        "sdk_factory": lambda _token: FakeKitSDK(box),
        "real_token_factory": _unexpected_mint,
        "real_totp_minter": _unexpected_mint_totp,
        "controller_factory": _controller,
        "http_get": _http_get_ok,
        "nse_http_get": _http_get_ok,
        "probe_day": lambda: _PROBE_DAY,
    }
    defaults.update(overrides)
    return dl.KitDeps(**defaults), box


def _unexpected_mint(_api_key: str, _totp: str) -> str:
    raise AssertionError("real token mint must NEVER be reached without --mint-anyway")


def _unexpected_mint_totp(_secret: str) -> str:
    raise AssertionError("real TOTP mint must NEVER be reached without --mint-anyway")


def make_ctx(
    tmp_path: Path, deps: dl.KitDeps, **option_overrides: Any
) -> tuple[dl.KitContext, list[str]]:
    options = dl.KitOptions(**option_overrides)
    lake_root = options.lake_root if options.lake_root is not None else tmp_path / "lake"
    events: list[str] = []
    ctx = dl.KitContext(
        options=options,
        lake_root=lake_root,
        run_id="lp-test",
        quarantine_root=lake_root / "_liveproof" / "lp-test",
        budget=dl.CallBudget(dl.compute_call_budget(options)),
        transcript=dl.Transcript(events.append, now_utc=deps.now_utc),
    )
    return ctx, events


def prepare_client(
    tmp_path: Path, **option_overrides: Any
) -> tuple[dl.KitDeps, dl.KitContext, dict[str, Handler]]:
    """Run stages 1+3 with benign deps so later stages have a REAL kit client."""
    deps, box = make_deps(tmp_path)
    ctx, _events = make_ctx(tmp_path, deps, **option_overrides)
    assert dl.stage_creds(deps, ctx).status == dl.STATUS_PASS
    assert dl.stage_token(deps, ctx).status == dl.STATUS_PASS
    return deps, ctx, box


# ── infrastructure ──────────────────────────────────────────────────────


class TestInfrastructure:
    def test_call_budget_spend_and_exhaustion(self) -> None:
        budget = dl.CallBudget(2)
        budget.spend(label="a")
        assert (budget.limit, budget.used, budget.remaining) == (2, 1, 1)
        budget.spend(label="b")
        with pytest.raises(dl.CallBudgetExceeded, match="c: spending 1 would exceed"):
            budget.spend(label="c")

    def test_render_diagnostic_unknown_code_is_a_kit_bug(self) -> None:
        with pytest.raises(KeyError):
            dl.render_diagnostic("no.such.code")

    def test_transcript_event_serialises_sorted_json(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        lines: list[str] = []
        transcript = dl.Transcript(lines.append, now_utc=deps.now_utc)
        transcript.event("probe", zulu=1, alpha=Path("/x"))
        assert lines == [
            '{"alpha": "/x", "event": "probe", "ts_utc": "2026-07-02T06:00:00+00:00", "zulu": 1}'
        ]

    def test_file_transcript_sink_appends_with_parents(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "transcript.jsonl"
        sink = dl.make_file_transcript_sink(path)
        sink("one")
        sink("two")
        assert path.read_text(encoding="utf-8") == "one\ntwo\n"

    def test_compute_call_budget_tiers(self) -> None:
        assert dl.compute_call_budget(dl.KitOptions()) == 40
        assert dl.compute_call_budget(dl.KitOptions(depth_bisect=True)) == 100
        assert dl.compute_call_budget(dl.KitOptions(rate_probe=True)) == 520
        assert dl.compute_call_budget(dl.KitOptions(depth_bisect=True, rate_probe=True)) == 580
        # auto_bisect (the autorun's zero-command mode) grants the SAME bisect
        # extension — and never double-grants it alongside --depth-bisect.
        assert dl.compute_call_budget(dl.KitOptions(auto_bisect=True)) == 100
        assert dl.compute_call_budget(dl.KitOptions(auto_bisect=True, depth_bisect=True)) == 100

    def test_report_format_json_and_exit_codes(self, tmp_path: Path) -> None:
        ok = dl.StageOutcome(stage=0, name="env", status=dl.STATUS_PASS, lines=("PASS env: x",))
        bad = dl.StageOutcome(stage=1, name="creds", status=dl.STATUS_FAIL, lines=("FAIL",))
        green = dl.KitReport(run_id="lp-1", transcript_path=tmp_path / "t.jsonl", outcomes=(ok,))
        red = dl.KitReport(run_id="lp-2", transcript_path=tmp_path / "t.jsonl", outcomes=(ok, bad))
        assert (green.exit_code, green.failed) == (0, False)
        assert (red.exit_code, red.failed) == (2, True)
        text = red.format()
        assert "OVERALL: FAIL" in text
        assert "[stage  1 · creds]" in text
        assert "OVERALL: PASS" in green.format()
        import json

        parsed = json.loads(red.to_json())
        assert parsed["exit_code"] == 2
        assert parsed["stages"][1]["status"] == "FAIL"


# ── stage 0 · env ───────────────────────────────────────────────────────


class TestStageEnv:
    def test_all_benign_passes_with_identity_headline(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, events = make_ctx(tmp_path, deps)
        outcome = dl.stage_env(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert outcome.lines[0].startswith("PASS env: commit abc1234 (0 behind origin/main)")
        assert "editable install" in outcome.lines[0]
        assert any('"event": "env"' in line for line in events)

    def test_unmounted_volume_lake_fails_g1(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path, ismount=lambda _p: False)
        ctx, _ = make_ctx(tmp_path, deps, lake_root=Path("/Volumes/T8/brutex-lake"))
        outcome = dl.stage_env(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic(
            "env.lake_unmounted",
            lake_root=Path("/Volumes/T8/brutex-lake"),
            volume=Path("/Volumes/T8"),
        )

    def test_mounted_volume_lake_passes_with_mount_note(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path, ismount=lambda _p: True)
        ctx, _ = make_ctx(tmp_path, deps, lake_root=Path("/Volumes/T8/brutex-lake"))
        outcome = dl.stage_env(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert "volume MOUNTED" in outcome.lines[0]

    def test_growwapi_missing_fails_g2(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path, growwapi_version=lambda: None)
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_env(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic("env.growwapi_missing")
        assert any(line.startswith("INFO env: ") for line in outcome.lines)

    def test_growwapi_version_drift_warns_g2(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path, growwapi_version=lambda: "1.7.0")
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_env(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert dl.render_diagnostic("env.growwapi_drift", installed="1.7.0") in outcome.lines

    def test_behind_origin_warns_g3(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path, behind_origin=lambda: 14)
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_env(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert dl.render_diagnostic("env.behind_origin", sha="abc1234", behind=14) in outcome.lines

    def test_unknown_behind_count_is_question_mark_not_a_warn(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path, behind_origin=lambda: None)
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_env(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert "(? behind origin/main)" in outcome.lines[0]

    def test_site_packages_install_warns_g3(self, tmp_path: Path) -> None:
        location = "/usr/lib/python3.12/site-packages/brutex"
        deps, _box = make_deps(tmp_path, package_location=lambda: location)
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_env(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert dl.render_diagnostic("env.not_editable", location=location) in outcome.lines

    def test_proxy_warns_r8(self, tmp_path: Path) -> None:
        proxies = {"https": "http://proxy.corp:8080"}
        deps, _box = make_deps(tmp_path, getproxies=lambda: proxies)
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_env(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert dl.render_diagnostic("env.proxy", proxies=proxies) in outcome.lines

    def test_dotenv_split_brain_warns_g1(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        for base, value in ((repo, "/Volumes/T8/lake"), (tmp_path / "home" / ".brutex", "~/lake")):
            base.mkdir(parents=True, exist_ok=True)
            (base / ".env").write_text(f"BRUTEX_LAKE_ROOT={value}\n", encoding="utf-8")
        deps, _box = make_deps(tmp_path, repo_root=repo)
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_env(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert any(
            "WARN env[G1]: dashboard and CLI would resolve" in line for line in outcome.lines
        )

    def test_agreeing_dotenv_files_do_not_warn(self, tmp_path: Path) -> None:
        for base in (tmp_path / "cwd", tmp_path / "home" / ".brutex"):
            base.mkdir(parents=True, exist_ok=True)
            (base / ".env").write_text("BRUTEX_LAKE_ROOT=/same/lake\n", encoding="utf-8")
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        assert dl.stage_env(deps, ctx).status == dl.STATUS_PASS

    def test_dotenv_without_lake_key_ignored(self, tmp_path: Path) -> None:
        cwd = tmp_path / "cwd"
        cwd.mkdir(parents=True, exist_ok=True)
        (cwd / ".env").write_text("OTHER=1\n", encoding="utf-8")
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        assert dl.stage_env(deps, ctx).status == dl.STATUS_PASS


# ── stage 1 · creds ─────────────────────────────────────────────────────


def _creds_error(
    message: str, cause: Exception | None = None
) -> Callable[[], GrowwFileCredentials]:
    def _resolve() -> GrowwFileCredentials:
        if cause is None:
            raise CredentialsError(message)
        raise CredentialsError(message) from cause

    return _resolve


class _AccessDeniedException(Exception):
    pass


_AccessDeniedException.__name__ = "AccessDeniedException"
_AccessDeniedException.__qualname__ = "AccessDeniedException"


class _NoCredentialsError(Exception):
    pass


_NoCredentialsError.__name__ = "NoCredentialsError"
_NoCredentialsError.__qualname__ = "NoCredentialsError"


class TestStageCreds:
    def test_pass_reads_both_params_with_region_and_timing(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_creds(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert outcome.lines[0].startswith(
            "PASS creds: api-key + totp-secret read from SSM "
            "/tickvault/prod/groww/{api-key,totp-secret} (region ap-south-1, "
        )
        assert ctx.creds is not None

    def test_failure_relays_verbatim_with_r1_note(self, tmp_path: Path) -> None:
        message = "could not reach AWS Parameter Store for the Groww credentials (Boom); fix it"
        deps, _box = make_deps(tmp_path, resolve_creds=_creds_error(message))
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_creds(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == f"FAIL creds[R1]: {message}"
        assert outcome.lines[1] == dl.render_diagnostic("creds.synthetic_note")

    @pytest.mark.parametrize(
        ("cause", "expected_code"),
        [
            (ModuleNotFoundError("No module named 'boto3'"), "creds.fix_boto3"),
            (_NoCredentialsError("Unable to locate credentials"), "creds.fix_no_aws_creds"),
            (
                _AccessDeniedException("not authorized to perform ssm:GetParameter"),
                "creds.fix_ssm_denied",
            ),
        ],
    )
    def test_cause_specific_fix_tails(
        self, tmp_path: Path, cause: Exception, expected_code: str
    ) -> None:
        deps, _box = make_deps(tmp_path, resolve_creds=_creds_error("read failed", cause))
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_creds(deps, ctx)
        assert dl.render_diagnostic(expected_code) in outcome.lines

    def test_kms_denied_tail(self, tmp_path: Path) -> None:
        cause = _AccessDeniedException("kms:Decrypt denied on the key")
        deps, _box = make_deps(tmp_path, resolve_creds=_creds_error("read failed", cause))
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_creds(deps, ctx)
        assert dl.render_diagnostic("creds.fix_kms_denied") in outcome.lines

    def test_missing_params_tail_names_region(self, tmp_path: Path) -> None:
        message = "missing Groww credentials in AWS Parameter Store: /x, /y; seed them"
        deps, _box = make_deps(tmp_path, resolve_creds=_creds_error(message))
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_creds(deps, ctx)
        assert dl.render_diagnostic("creds.fix_region", region="ap-south-1") in outcome.lines


# ── stage 2 · totp ──────────────────────────────────────────────────────


class TestStageTotp:
    def test_skips_without_stage1(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_totp(deps, ctx)
        assert outcome.status == dl.STATUS_SKIP
        assert "stage 1 did not run" in outcome.lines[0]

    def test_pass_line(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        ctx.creds = GrowwFileCredentials(api_key="key", totp_secret=_SECRET)
        outcome = dl.stage_totp(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert outcome.lines[0] == dl.render_diagnostic(
            "totp.pass", length=len(_SECRET), offset="+0.4"
        )

    def test_non_base32_char_fails_m3_with_position(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        ctx.creds = GrowwFileCredentials(api_key="key", totp_secret="JBSWY3DPEHPK8PXP")
        outcome = dl.stage_totp(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert "offending char '8' at position 12" in outcome.lines[0]
        assert outcome.lines[0].startswith("FAIL totp[M3]:")

    def test_bad_length_secret_fails_m3(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        ctx.creds = GrowwFileCredentials(api_key="key", totp_secret="ABCDEFGHA")
        outcome = dl.stage_totp(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith("FAIL totp[M3]: TOTP secret in SSM is NOT valid base32")

    def test_pyotp_missing_fails_m3(self, tmp_path: Path) -> None:
        def _no_pyotp(_secret: str) -> str:
            raise ModuleNotFoundError("No module named 'pyotp'")

        deps, _box = make_deps(tmp_path, totp_minter=_no_pyotp)
        ctx, _ = make_ctx(tmp_path, deps)
        ctx.creds = GrowwFileCredentials(api_key="key", totp_secret=_SECRET)
        outcome = dl.stage_totp(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic("totp.pyotp_missing")

    def test_other_mint_crash_fails_m3(self, tmp_path: Path) -> None:
        def _boom(_secret: str) -> str:
            raise ValueError("mangled secret state")

        deps, _box = make_deps(tmp_path, totp_minter=_boom)
        ctx, _ = make_ctx(tmp_path, deps)
        ctx.creds = GrowwFileCredentials(api_key="key", totp_secret=_SECRET)
        outcome = dl.stage_totp(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic(
            "totp.mint_failed", error="ValueError: mangled secret state"
        )

    @pytest.mark.parametrize(("offset", "direction"), [(94.0, "AHEAD"), (-94.0, "BEHIND")])
    def test_clock_skew_fails_n1(self, tmp_path: Path, offset: float, direction: str) -> None:
        deps, _box = make_deps(tmp_path, clock_offset_s=lambda: offset)
        ctx, _ = make_ctx(tmp_path, deps)
        ctx.creds = GrowwFileCredentials(api_key="key", totp_secret=_SECRET)
        outcome = dl.stage_totp(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic(
            "totp.clock_skew", offset="94.0", direction=direction
        )

    def test_unreachable_reference_clock_notes_unverified(self, tmp_path: Path) -> None:
        def _no_ntp() -> float:
            raise OSError("reference host down")

        deps, _box = make_deps(tmp_path, clock_offset_s=_no_ntp)
        ctx, _ = make_ctx(tmp_path, deps)
        ctx.creds = GrowwFileCredentials(api_key="key", totp_secret=_SECRET)
        outcome = dl.stage_totp(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert outcome.lines[1] == dl.render_diagnostic(
            "totp.offset_unknown", error="reference host down"
        )


# ── stage 3 · token ─────────────────────────────────────────────────────


class TestStageToken:
    def test_skips_without_stage1(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        assert dl.stage_token(deps, ctx).status == dl.STATUS_SKIP

    def _ready_ctx(
        self, tmp_path: Path, deps: dl.KitDeps, **option_overrides: Any
    ) -> dl.KitContext:
        ctx, _ = make_ctx(tmp_path, deps, **option_overrides)
        ctx.creds = GrowwFileCredentials(api_key="key", totp_secret=_SECRET)
        return ctx

    def test_pass_smoke_call_and_shape(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx = self._ready_ctx(tmp_path, deps)
        outcome = dl.stage_token(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert outcome.lines[0].startswith(
            f"PASS token: SSM token present (param {_TOKEN_PARAM}); authenticated call OK ("
        )
        assert "375 rows" in outcome.lines[0]
        assert "3 _request_* methods, signature OK" in outcome.lines[0]
        assert ctx.client is not None
        assert ctx.budget.used == 1

    def test_read_error_is_r7_not_token_absent(self, tmp_path: Path) -> None:
        deps, _box = make_deps(
            tmp_path, aws_client_factory=lambda: FakeAwsClient(fail_get_parameter=True)
        )
        ctx = self._ready_ctx(tmp_path, deps)
        outcome = dl.stage_token(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith(
            "FAIL token[R7]: SSM token read FAILED with FakeAwsError — NOT 'token absent'."
        )

    def test_genuinely_absent_token_is_r7_deploy_minter(self, tmp_path: Path) -> None:
        deps, _box = make_deps(
            tmp_path,
            aws_client_factory=lambda: FakeAwsClient(param_not_found=frozenset({_TOKEN_PARAM})),
        )
        ctx = self._ready_ctx(tmp_path, deps)
        outcome = dl.stage_token(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith(
            f"FAIL token[R7]: SSM token genuinely ABSENT ({_TOKEN_PARAM}"
        )
        assert "did NOT mint locally" in outcome.lines[0]

    def test_blank_token_is_absent(self, tmp_path: Path) -> None:
        deps, _box = make_deps(
            tmp_path, aws_client_factory=lambda: FakeAwsClient(parameters={_TOKEN_PARAM: "  "})
        )
        ctx = self._ready_ctx(tmp_path, deps)
        outcome = dl.stage_token(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert "genuinely ABSENT" in outcome.lines[0]

    def test_mint_anyway_mints_and_warns(self, tmp_path: Path) -> None:
        minted: list[str] = []

        def _mint(api_key: str, totp: str) -> str:
            minted.append(f"{api_key}:{totp}")
            return "freshly-minted"

        deps, _box = make_deps(
            tmp_path,
            aws_client_factory=lambda: FakeAwsClient(param_not_found=frozenset({_TOKEN_PARAM})),
            real_token_factory=_mint,
            real_totp_minter=lambda _s: "654321",
        )
        ctx = self._ready_ctx(tmp_path, deps, mint_anyway=True)
        outcome = dl.stage_token(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert outcome.lines[0] == dl.render_diagnostic("token.mint_anyway")
        assert outcome.lines[1].startswith("PASS token:")
        assert minted == ["key:654321"]

    def test_dead_token_rejection_is_m1(self, tmp_path: Path) -> None:
        deps, box = make_deps(tmp_path)
        box["handler"] = raising_handler(lambda: TypedAuthException("401 unauthorised"))
        ctx = self._ready_ctx(tmp_path, deps)
        outcome = dl.stage_token(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith(
            "FAIL token[M1]: token present but the vendor REJECTED it ("
        )
        assert "aws lambda invoke --function-name groww-token-minter" in outcome.lines[0]

    def test_bare_401_adds_m2_evidence(self, tmp_path: Path) -> None:
        deps, box = make_deps(tmp_path)
        box["handler"] = raising_handler(lambda: BareGrowwAPIException("Unauthorized", code=401))
        ctx = self._ready_ctx(tmp_path, deps)
        outcome = dl.stage_token(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith("FAIL token[M1]:")
        assert outcome.lines[1] == dl.render_diagnostic(
            "token.bare_auth_evidence", code=401, text="Unauthorized"
        )

    def test_tls_failure_is_r8_terminal_diagnosis(self, tmp_path: Path) -> None:
        message = (
            "HTTPSConnectionPool(host='api.groww.in', port=443): Max retries exceeded "
            "with url: / (Caused by SSLError(SSLCertVerificationError(1, '[SSL: "
            "CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local "
            "issuer certificate')))"
        )
        deps, box = make_deps(tmp_path)
        box["handler"] = raising_handler(lambda: RuntimeError(message))
        ctx = self._ready_ctx(tmp_path, deps)
        outcome = dl.stage_token(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic("token.tls")

    def test_unclassified_smoke_failure(self, tmp_path: Path) -> None:
        deps, box = make_deps(tmp_path)
        box["handler"] = raising_handler(lambda: RuntimeError("vendor melted"))
        ctx = self._ready_ctx(tmp_path, deps)
        outcome = dl.stage_token(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith("FAIL token: authenticated smoke call failed (")

    def test_sdk_factory_auth_error_maps_to_smoke_failed(self, tmp_path: Path) -> None:
        from brutex.adapters.groww.errors import GrowwAuthError

        def _bad_factory(_token: str) -> FakeKitSDK:
            raise GrowwAuthError("token store said no")

        deps, _box = make_deps(tmp_path, sdk_factory=_bad_factory)
        ctx = self._ready_ctx(tmp_path, deps)
        outcome = dl.stage_token(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert "authenticated smoke call failed" in outcome.lines[0]

    def test_missing_request_get_fails_g2_shape(self, tmp_path: Path) -> None:
        class NoPatchSDK(FakeKitSDK):
            _request_get = None  # type: ignore[assignment]

        deps, _box = make_deps(
            tmp_path, sdk_factory=lambda _t: NoPatchSDK({"handler": default_handler})
        )
        ctx = self._ready_ctx(tmp_path, deps)
        outcome = dl.stage_token(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic(
            "sdk.shape", version="1.5.2", problems="missing _request_get"
        )


class TestProbeSdkShape:
    def test_clean_shape(self) -> None:
        assert dl.probe_sdk_shape(FakeKitSDK({"handler": default_handler})) == []

    def test_missing_get_historical_candles(self) -> None:
        class Sdk:
            _request_get = _request_post = _request_put = staticmethod(lambda: None)

        assert dl.probe_sdk_shape(Sdk()) == ["missing get_historical_candles"]

    def test_signature_drift_detected(self) -> None:
        class Sdk:
            _request_get = _request_post = _request_put = staticmethod(lambda: None)

            def get_historical_candles(self, exchange: str) -> None:  # wrong surface
                return None

        problems = dl.probe_sdk_shape(Sdk())
        assert len(problems) == 1
        assert problems[0].startswith("get_historical_candles signature drifted: (exchange:")

    def test_unknowable_signature_is_not_a_verdict(self) -> None:
        class Sdk:
            _request_get = _request_post = _request_put = staticmethod(lambda: None)
            get_historical_candles = type  # C-level: inspect.signature raises

        assert dl.probe_sdk_shape(Sdk()) == []


# ── stage 4 · master ────────────────────────────────────────────────────


class TestStageMaster:
    def test_pass_with_universe_counts(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_master(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert outcome.lines[0] == dl.render_diagnostic("master.pass", universe=5, eq=1)
        assert ctx.master is not None

    def test_header_drift_fails_m17(self, tmp_path: Path) -> None:
        drifted = _MASTER_HEADER + ",exchange_flag\n"
        deps, _box = make_deps(tmp_path, http_get=lambda _u: drifted)
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_master(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith("FAIL master[M17]: instrument-master header drift — ")

    def test_unreachable_fails_r8(self, tmp_path: Path) -> None:
        def _down(_url: str) -> str:
            raise OSError("Name or service not known")

        deps, _box = make_deps(tmp_path, http_get=_down)
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_master(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic(
            "master.unreachable", error="OSError: Name or service not known"
        )

    def test_missing_universe_symbol_fails(self, tmp_path: Path) -> None:
        deps, _box = make_deps(
            tmp_path, http_get=lambda _u: _master_csv(drop=frozenset({"BSE-SENSEX"}))
        )
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_master(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic(
            "master.universe_gap", universe=4, missing=["BSE-SENSEX"]
        )


# ── stage 5 · spot-1m ───────────────────────────────────────────────────


class TestStageSpot1m:
    def test_skips_without_client(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        assert dl.stage_spot1m(deps, ctx).status == dl.STATUS_SKIP

    def test_pass_full_session_string_encoded(self, tmp_path: Path) -> None:
        deps, ctx, _box = prepare_client(tmp_path)
        outcome = dl.stage_spot1m(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert outcome.lines[0] == dl.render_diagnostic(
            "spot1m.pass",
            raw=375,
            session=375,
            day="2026-07-01",
            first_ist="09:15",
            encoding="string",
            skipped=0,
        )
        assert len(ctx.session_bars) == 375
        assert ctx.record is not None

    def test_pass_epoch_encoded(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path)
        box["handler"] = lambda kwargs: {
            "candles": full_day_candles(str(kwargs["start_time"])[:10], epoch=True)
        }
        outcome = dl.stage_spot1m(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert "(epoch-encoded)" in outcome.lines[0]

    def test_vendor_error(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path)
        box["handler"] = raising_handler(lambda: RuntimeError("vendor melted"))
        outcome = dl.stage_spot1m(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith("FAIL spot-1m: vendor call failed: ")

    def test_non_mapping_payload_is_m10(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path)
        box["handler"] = lambda _kwargs: None  # type: ignore[return-value]
        outcome = dl.stage_spot1m(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic("spot1m.non_mapping", type_name="NoneType")

    def test_missing_candles_key_is_m11_with_present_keys(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path)
        box["handler"] = lambda _kwargs: {"status": "SUCCESS", "payload": {}}
        outcome = dl.stage_spot1m(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic(
            "spot1m.no_candles_key", keys=["payload", "status"]
        )

    def test_offset_suffix_timestamp_is_m13(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path)
        box["handler"] = lambda _kwargs: {
            "candles": [["2026-07-01T09:15:00+05:30", 100.0, 101.0, 99.0, 100.5, 0, None]]
        }
        outcome = dl.stage_spot1m(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith("FAIL spot-1m[M13]: ")
        assert "'2026-07-01T09:15:00+05:30'" in outcome.lines[0]

    def test_malformed_row_is_generic_parse_failure(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path)
        box["handler"] = lambda _kwargs: {"candles": [["2026-07-01T09:15:00", 100.0]]}
        outcome = dl.stage_spot1m(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith(
            "FAIL spot-1m: candle payload failed the production parser"
        )

    def test_clean_empty_200_is_r2_r3(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path)
        box["handler"] = lambda _kwargs: {"candles": []}
        outcome = dl.stage_spot1m(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic("spot1m.clean_empty", day="2026-07-01")

    def test_utc_shifted_stamps_are_m12(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path)

        def _shifted(_kwargs: dict[str, object]) -> Mapping[str, object]:
            rows = full_day_candles("2026-07-01")
            shifted = []
            for row in rows:
                stamp = str(row[0]).replace("2026-07-01T", "")
                hour, rest = stamp.split(":", 1)
                shifted_stamp = f"2026-07-01T{(int(hour) - 6) % 24:02d}:{rest}"
                shifted.append([shifted_stamp, *row[1:]])
            return {"candles": shifted}

        box["handler"] = _shifted
        outcome = dl.stage_spot1m(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith("FAIL spot-1m[M12]: first bar parses to 03:15 IST")

    def test_missing_opening_bars_with_low_drop_is_distinct_from_utc_shift(
        self, tmp_path: Path
    ) -> None:
        """Hostile-review M-1: the OR short-circuit used to collapse this state
        into the '+05:30 UTC-shift' wording — a false trail when the drop ratio
        is LOW and only the opening bars are missing."""
        deps, ctx, box = prepare_client(tmp_path)
        box["handler"] = lambda _kwargs: {"candles": full_day_candles("2026-07-01")[15:]}
        outcome = dl.stage_spot1m(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic(
            "spot1m.first_bar_missing", first_ist="09:30", dropped=0, total=360
        )
        assert "not necessarily a timezone shift" in outcome.lines[0]
        assert "UTC-encoded" not in outcome.lines[0]

    def test_null_ohlc_epidemic_warns_m15(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path)

        def _thinned(_kwargs: dict[str, object]) -> Mapping[str, object]:
            rows = full_day_candles("2026-07-01")
            for index in range(100, 250):  # 40% of 375 nulled mid-session
                rows[index] = [rows[index][0], None, None, None, None, None, None]
            return {"candles": rows}

        box["handler"] = _thinned
        outcome = dl.stage_spot1m(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert outcome.lines[1] == dl.render_diagnostic("spot1m.null_ohlc", percent=40)

    def test_all_rows_skipped_is_a_failure(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path)
        box["handler"] = lambda _kwargs: {
            "candles": [["2026-07-01T09:15:00", None, None, None, None, None, None]]
        }
        outcome = dl.stage_spot1m(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert "all 1 rows were skipped as absent/zero OHLC" in outcome.lines[0]


# ── stage 6 · depth ─────────────────────────────────────────────────────


def _depth_handler(floor: date) -> Handler:
    """A vendor whose 1m history starts at *floor* (empty windows before it)."""

    def _handler(kwargs: dict[str, object]) -> Mapping[str, object]:
        start = date.fromisoformat(str(kwargs["start_time"])[:10])
        end = date.fromisoformat(str(kwargs["end_time"])[:10])
        if end < floor:
            return {"candles": []}
        first_day = max(start, floor)
        return {"candles": full_day_candles(first_day.isoformat())}

    return _handler


class TestStageDepth:
    def test_skips_without_client(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        assert dl.stage_depth(deps, ctx).status == dl.STATUS_SKIP

    def test_all_indexes_confirm_boundary(self, tmp_path: Path) -> None:
        deps, ctx, _box = prepare_client(tmp_path)
        outcome = dl.stage_depth(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert outcome.lines[0].startswith("PASS depth: ")
        assert "GROWW_HISTORY_FLOOR=2020-01-01 CONFIRMED" in outcome.lines[0]
        assert ctx.budget.used == 1 + 3  # smoke + one boundary window per index

    def test_shallow_floor_fails_r6(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path, depth_bisect=True)
        box["handler"] = _depth_handler(date(2021, 6, 14))
        outcome = dl.stage_depth(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert any(
            line.startswith("FAIL depth[R6]: BSE-SENSEX 1m actually starts 2021-06-14")
            for line in outcome.lines
        )
        assert all("configured floor 2020-01-01" in line for line in outcome.lines)

    def test_empty_boundary_without_bisect_is_unknown(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path)
        box["handler"] = _depth_handler(date(2021, 6, 14))
        outcome = dl.stage_depth(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert all("re-run with --depth-bisect" in line for line in outcome.lines)

    def test_no_data_anywhere_with_bisect(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path, depth_bisect=True)
        box["handler"] = lambda _kwargs: {"candles": []}
        outcome = dl.stage_depth(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert all("no probed 1m data at all" in line for line in outcome.lines)

    def test_vendor_error_fails(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path)
        box["handler"] = raising_handler(lambda: RuntimeError("depth melt"))
        outcome = dl.stage_depth(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert all(line.startswith("FAIL depth: ") for line in outcome.lines)

    def test_auto_bisect_reports_found_floor_as_warn_never_a_command(self, tmp_path: Path) -> None:
        """DEC-DOCTOR-KIT-HONESTY-001 fix 1 (zero-command law): an empty boundary
        window under ``auto_bisect`` bisects the true floor ITSELF and reports
        it as an advisory WARN naming the floor — never a FAIL instructing the
        operator to run ``--depth-bisect``. FAILS before the fix (the autorun
        path had ``depth_bisect=False`` → a FAIL prescribing the command)."""
        deps, ctx, box = prepare_client(tmp_path, auto_bisect=True)
        box["handler"] = _depth_handler(date(2021, 6, 14))
        outcome = dl.stage_depth(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert any(
            "1-minute depth floor for BSE-SENSEX: 2021-06-14" in line for line in outcome.lines
        )
        assert all("--depth-bisect" not in line for line in outcome.lines)
        assert all("re-run" not in line for line in outcome.lines)
        assert all(not line.startswith("FAIL") for line in outcome.lines)

    def test_auto_bisect_never_bisects_a_passing_symbol(self, tmp_path: Path) -> None:
        """Only the FAILING symbol pays bisect calls — a boundary hit costs
        exactly ONE vendor call per passing symbol (no wasted vendor spend)."""
        deps, ctx, box = prepare_client(tmp_path, auto_bisect=True)
        counts: dict[str, int] = {}
        shallow = _depth_handler(date(2021, 6, 14))

        def _sensex_only_shallow(kwargs: dict[str, object]) -> Mapping[str, object]:
            symbol = str(kwargs.get("groww_symbol"))
            counts[symbol] = counts.get(symbol, 0) + 1
            if symbol == "BSE-SENSEX":
                return shallow(kwargs)
            return default_handler(kwargs)

        box["handler"] = _sensex_only_shallow
        outcome = dl.stage_depth(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert len(outcome.lines) == 1
        assert "1-minute depth floor for BSE-SENSEX: 2021-06-14" in outcome.lines[0]
        # Passing symbols: ONE boundary call each, never a bisect.
        assert counts["NSE-NIFTY"] == 1
        assert counts["NSE-BANKNIFTY"] == 1
        # The failing symbol's bisect stays inside the per-symbol hard cap.
        assert 2 <= counts["BSE-SENSEX"] <= 20
        # Budget honesty: every call was spent through the (extended) budget.
        assert ctx.budget.limit == 100  # 40 default + 60 auto-bisect extension
        assert ctx.budget.used == 1 + sum(counts.values())  # smoke + depth calls

    def test_auto_bisect_no_data_anywhere_fails_without_prescribing_a_command(
        self, tmp_path: Path
    ) -> None:
        deps, ctx, box = prepare_client(tmp_path, auto_bisect=True)
        box["handler"] = lambda _kwargs: {"candles": []}
        outcome = dl.stage_depth(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert all("no probed 1m data at all" in line for line in outcome.lines)
        assert all("--depth-bisect" not in line for line in outcome.lines)
        assert all("re-run with" not in line for line in outcome.lines)

    def test_manual_depth_bisect_verdict_is_byte_unchanged_even_with_auto(
        self, tmp_path: Path
    ) -> None:
        """An EXPLICIT --depth-bisect keeps the manual FAIL diagnosis (the
        operator asked for the probe — the shallow-floor hazard stays loud)."""
        deps, ctx, box = prepare_client(tmp_path, depth_bisect=True, auto_bisect=True)
        box["handler"] = _depth_handler(date(2021, 6, 14))
        outcome = dl.stage_depth(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert any(
            line.startswith("FAIL depth[R6]: BSE-SENSEX 1m actually starts 2021-06-14")
            for line in outcome.lines
        )

    def test_budget_exhaustion_raises_for_runner(self, tmp_path: Path) -> None:
        deps, ctx, _box = prepare_client(tmp_path)
        ctx.budget.spend(ctx.budget.remaining, label="drain")
        with pytest.raises(dl.CallBudgetExceeded):
            dl.stage_depth(deps, ctx)


# ── stage 7 · vix ───────────────────────────────────────────────────────


class TestStageVix:
    def test_skips_without_client(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        assert dl.stage_vix(deps, ctx).status == dl.STATUS_SKIP

    def test_pass_daily_and_1m(self, tmp_path: Path) -> None:
        deps, ctx, _box = prepare_client(tmp_path)
        outcome = dl.stage_vix(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert outcome.lines[0] == dl.render_diagnostic("vix.pass", daily="375", m1=375)

    def test_absent_fails_r5_hard_gate(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path)
        box["handler"] = lambda _kwargs: {"candles": []}
        outcome = dl.stage_vix(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic(
            "vix.absent",
            shape=("clean-empty 200 on every candidate symbol (1minute); 1day fallback also empty"),
        )
        assert "MIGRATION-RESCINDING" in outcome.lines[0]

    def test_absent_1m_but_daily_present_is_the_distinct_honest_state(self, tmp_path: Path) -> None:
        """Hostile-review m-2: 1m-absent must try DAILY before concluding the gate."""
        deps, ctx, box = prepare_client(tmp_path)

        def _daily_only(kwargs: dict[str, object]) -> Mapping[str, object]:
            if kwargs.get("candle_interval") == "1day":
                return {"candles": full_day_candles("2026-07-01")[:22]}
            return {"candles": []}

        box["handler"] = _daily_only
        outcome = dl.stage_vix(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic("vix.absent_1m", daily=22)
        assert "does NOT fire" in outcome.lines[0]

    def test_absent_1m_with_daily_fallback_error_reports_the_error_shape(
        self, tmp_path: Path
    ) -> None:
        deps, ctx, box = prepare_client(tmp_path)

        def _both_down(kwargs: dict[str, object]) -> Mapping[str, object]:
            if kwargs.get("candle_interval") == "1day":
                raise RuntimeError("daily endpoint melt")
            return {"candles": []}

        box["handler"] = _both_down
        outcome = dl.stage_vix(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert "1day fallback ERROR(GrowwError)" in outcome.lines[0]
        assert "MIGRATION-RESCINDING" in outcome.lines[0]

    def test_typed_error_fails_r5_with_shape(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path)
        box["handler"] = raising_handler(lambda: RuntimeError("vix melt"))
        outcome = dl.stage_vix(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert "shape: typed error — " in outcome.lines[0]

    def test_daily_error_still_passes_the_1m_gate(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path)

        def _daily_down(kwargs: dict[str, object]) -> Mapping[str, object]:
            if kwargs.get("candle_interval") == "1day":
                raise RuntimeError("daily endpoint melt")
            return default_handler(kwargs)

        box["handler"] = _daily_down
        outcome = dl.stage_vix(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert "ERROR(GrowwError)" in outcome.lines[0]


# ── stage 8 · bhavcopy ──────────────────────────────────────────────────


def _spot_pass(
    tmp_path: Path, **option_overrides: Any
) -> tuple[dl.KitDeps, dl.KitContext, dict[str, Handler]]:
    deps, ctx, box = prepare_client(tmp_path, **option_overrides)
    assert dl.stage_spot1m(deps, ctx).status == dl.STATUS_PASS
    return deps, ctx, box


class TestStageBhavcopy:
    def test_skips_without_stage5(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        assert dl.stage_bhavcopy(deps, ctx).status == dl.STATUS_SKIP

    def test_pass_within_one_tick(self, tmp_path: Path) -> None:
        deps, ctx, _box = _spot_pass(tmp_path)
        outcome = dl.stage_bhavcopy(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert outcome.lines[0] == dl.render_diagnostic(
            "bhavcopy.pass",
            index_name="Nifty 50",
            day="2026-07-01",
            delta="0.00",
            tolerance="0.05",
        )

    def test_mismatch_fails_m12_m15(self, tmp_path: Path) -> None:
        def _http(url: str) -> str:
            return _BHAVCOPY_DRIFTED if "ind_close_all" in url else _master_csv()

        deps, ctx, _box = _spot_pass(tmp_path)
        deps.nse_http_get = _http
        outcome = dl.stage_bhavcopy(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith("FAIL bhavcopy[M12/M15]: derived close 100.5 vs 106.95")

    def test_unmapped_symbol_warns(self, tmp_path: Path) -> None:
        deps, ctx, _box = _spot_pass(tmp_path)
        ctx.options.symbol = "BSE-SENSEX"
        outcome = dl.stage_bhavcopy(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert outcome.lines[0] == dl.render_diagnostic("bhavcopy.unmapped", symbol="BSE-SENSEX")

    def test_archive_unreachable_warns_not_passes(self, tmp_path: Path) -> None:
        def _http(url: str) -> str:
            if "ind_close_all" in url:
                raise OSError("nse archive down")
            return _master_csv()

        deps, ctx, _box = _spot_pass(tmp_path)
        deps.nse_http_get = _http
        outcome = dl.stage_bhavcopy(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert outcome.lines[0] == dl.render_diagnostic(
            "bhavcopy.unreachable", error="OSError: nse archive down"
        )

    def test_archive_header_drift_warns(self, tmp_path: Path) -> None:
        def _http(url: str) -> str:
            return "A,B\n1,2\n" if "ind_close_all" in url else _master_csv()

        deps, ctx, _box = _spot_pass(tmp_path)
        deps.nse_http_get = _http
        outcome = dl.stage_bhavcopy(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert "unrecognised NSE indices snapshot header" in outcome.lines[0]

    def test_missing_index_row_warns(self, tmp_path: Path) -> None:
        snapshot = f"{_BHAVCOPY_HEADER}\nNifty Bank,01-07-2026,1,2,0.5,1.5\n"

        def _http(url: str) -> str:
            return snapshot if "ind_close_all" in url else _master_csv()

        deps, ctx, _box = _spot_pass(tmp_path)
        deps.nse_http_get = _http
        outcome = dl.stage_bhavcopy(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert outcome.lines[0] == dl.render_diagnostic(
            "bhavcopy.no_row", day="2026-07-01", index_name="nifty 50"
        )

    def test_no_complete_daily_bar_warns(self, tmp_path: Path) -> None:
        deps, ctx, _box = _spot_pass(tmp_path)
        ctx.probe_day = date(2026, 6, 30)  # bars belong to 07-01 → nothing derives for 06-30
        outcome = dl.stage_bhavcopy(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert outcome.lines[0] == dl.render_diagnostic("bhavcopy.no_daily_bar", day="2026-06-30")

    def test_stage_fetches_via_the_nse_getter_not_the_groww_shim(self, tmp_path: Path) -> None:
        """DEC-DOCTOR-KIT-HONESTY-001 fix 2: stage 8 must route through
        ``KitDeps.nse_http_get`` — the Groww instrument-master getter is never
        called for the NSE archive (its allowlist would block it)."""

        def _groww_only(_url: str) -> str:
            raise AssertionError("stage 8 must not use the Groww http_get shim")

        deps, ctx, _box = _spot_pass(tmp_path)
        deps.http_get = _groww_only
        outcome = dl.stage_bhavcopy(deps, ctx)
        assert outcome.status == dl.STATUS_PASS

    def test_groww_allowlist_refusal_is_a_kit_wiring_fail_not_external(
        self, tmp_path: Path
    ) -> None:
        """The operator's red-banner mislabel (log 23c543ba): the Groww shim's
        SSRF allowlist blocking archives.nseindia.com rendered as 'external
        dependency'. It is OUR wiring — now an internal-kit-bug FAIL. FAILS
        before the fix (the generic except mapped it to bhavcopy.unreachable)."""
        from brutex.adapters.groww.instrument_loader import GrowwInstrumentFetchError

        def _groww_shim(_url: str) -> str:
            raise GrowwInstrumentFetchError(
                "instrument-master host not allowlisted "
                "(growwapi-assets.groww.in): 'archives.nseindia.com'"
            )

        deps, ctx, _box = _spot_pass(tmp_path)
        deps.nse_http_get = _groww_shim
        outcome = dl.stage_bhavcopy(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith(
            "FAIL bhavcopy: kit wiring: URL blocked by local allowlist"
        )
        assert "external dependency" not in outcome.lines[0]

    def test_bhavcopy_own_allowlist_refusal_is_also_a_wiring_fail(self, tmp_path: Path) -> None:
        from brutex.data.bhavcopy import BhavcopyError

        def _refusing(url: str) -> str:
            raise BhavcopyError(
                f"refusing non-allowlisted bhavcopy URL {url!r} — allowed hosts: "
                "['archives.nseindia.com', 'nsearchives.nseindia.com'] over https"
            )

        deps, ctx, _box = _spot_pass(tmp_path)
        deps.nse_http_get = _refusing
        outcome = dl.stage_bhavcopy(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith(
            "FAIL bhavcopy: kit wiring: URL blocked by local allowlist"
        )


# ── stage 9 · resample ──────────────────────────────────────────────────


class TestStageResample:
    def test_skip_on_empty_quarantine(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_resample(deps, ctx)
        assert outcome.status == dl.STATUS_SKIP
        assert "nothing to verify" in outcome.lines[0]

    def test_fail_maps_to_drift_line(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import brutex.doctor as doctor_mod

        monkeypatch.setattr(
            doctor_mod,
            "check_derived_freshness",
            lambda _root: doctor_mod.CheckResult("derived_freshness", "FAIL", "NIFTY drifted"),
        )
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_resample(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic("resample.drift", detail="NIFTY drifted")

    def test_pass_relays_detail_and_against_lake_targets_production(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.doctor as doctor_mod

        seen: list[Path] = []

        def _fake(root: Path) -> doctor_mod.CheckResult:
            seen.append(root)
            return doctor_mod.CheckResult("derived_freshness", "PASS", "all fresh")

        monkeypatch.setattr(doctor_mod, "check_derived_freshness", _fake)
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps, against_lake=True)
        outcome = dl.stage_resample(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert outcome.lines[0] == "PASS resample: all fresh"
        assert seen == [ctx.lake_root]


# ── stage 10 · lake ─────────────────────────────────────────────────────


class TestStageLake:
    def test_skips_without_stage5(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        assert dl.stage_lake(deps, ctx).status == dl.STATUS_SKIP

    def test_pass_write_readback_ledger_audit(self, tmp_path: Path) -> None:
        deps, ctx, _box = _spot_pass(tmp_path)
        outcome = dl.stage_lake(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert outcome.lines[0].startswith("PASS lake: 375 bars → ")
        assert "ledger row SUCCESS" in outcome.lines[0]
        assert (ctx.quarantine_root / "audit" / "liveproof.jsonl").is_file()
        assert list(ctx.quarantine_root.rglob("*.parquet"))
        # SAFE: everything landed under the quarantined subtree, nothing outside it.
        outside = [
            path
            for path in ctx.lake_root.rglob("*")
            if path.is_file() and ctx.quarantine_root not in path.parents
        ]
        assert outside == []

    def test_split_brain_env_file_fails_g1(self, tmp_path: Path) -> None:
        env_dir = tmp_path / "home" / ".brutex"
        env_dir.mkdir(parents=True)
        (env_dir / ".env").write_text("BRUTEX_LAKE_ROOT=/Volumes/T8/other-lake\n", encoding="utf-8")
        deps, ctx, _box = _spot_pass(tmp_path)
        outcome = dl.stage_lake(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith("FAIL lake[G1]: this process resolved lake_root=")
        assert "/Volumes/T8/other-lake" in outcome.lines[0]

    def test_agreeing_env_file_proceeds(self, tmp_path: Path) -> None:
        deps, ctx, _box = _spot_pass(tmp_path)
        env_dir = tmp_path / "home" / ".brutex"
        env_dir.mkdir(parents=True)
        (env_dir / ".env").write_text(f"BRUTEX_LAKE_ROOT={ctx.lake_root}\n", encoding="utf-8")
        assert dl.stage_lake(deps, ctx).status == dl.STATUS_PASS

    def test_write_failure_maps_to_lake_write_failed(self, tmp_path: Path) -> None:
        deps, ctx, _box = _spot_pass(tmp_path)
        ctx.quarantine_root.parent.mkdir(parents=True)
        ctx.quarantine_root.write_text("a file where the quarantine dir should be")
        outcome = dl.stage_lake(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith("FAIL lake: write failed: ")

    def test_readback_drift_detected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import brutex.data.parquet_writer as pw

        real_write = pw.ParquetWriter.write
        calls = {"n": 0}

        def _drifting(self: pw.ParquetWriter, **kwargs: Any) -> pw.WriteResult:
            result = real_write(self, **kwargs)
            calls["n"] += 1
            if calls["n"] == 2:
                from dataclasses import replace

                return replace(result, bars_added=3)
            return result

        monkeypatch.setattr(pw.ParquetWriter, "write", _drifting)
        deps, ctx, _box = _spot_pass(tmp_path)
        outcome = dl.stage_lake(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith("FAIL lake: read-back is NOT bit-exact ")

    def test_ledger_state_failure_detected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import brutex.data.window_ledger_store as wls

        monkeypatch.setattr(wls, "load_window_ledger", lambda _root, **_kw: wls.WindowLedger())
        deps, ctx, _box = _spot_pass(tmp_path)
        outcome = dl.stage_lake(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic("lake.ledger_state", status="MISSING")


# ── stage 11 · integrity ────────────────────────────────────────────────


class TestStageIntegrity:
    def test_skips_without_client(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        assert dl.stage_integrity(deps, ctx).status == dl.STATUS_SKIP

    def test_skips_without_capture(self, tmp_path: Path) -> None:
        deps, ctx, _box = prepare_client(tmp_path)
        outcome = dl.stage_integrity(deps, ctx)
        assert outcome.status == dl.STATUS_SKIP
        assert "no captured window" in outcome.lines[0]

    def test_pass_when_vendor_serves_stable_bytes(self, tmp_path: Path) -> None:
        deps, ctx, _box = _spot_pass(tmp_path)
        outcome = dl.stage_integrity(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert outcome.lines[0] == dl.render_diagnostic("integrity.pass")

    def test_mismatch_fails_r2(self, tmp_path: Path) -> None:
        deps, ctx, box = _spot_pass(tmp_path)
        box["handler"] = lambda _kwargs: {"candles": full_day_candles("2026-07-01")[:100]}
        outcome = dl.stage_integrity(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic("integrity.mismatch")

    def test_refetch_failure_is_inconclusive(self, tmp_path: Path) -> None:
        deps, ctx, box = _spot_pass(tmp_path)
        box["handler"] = raising_handler(lambda: RuntimeError("re-fetch melt"))
        outcome = dl.stage_integrity(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0].startswith("FAIL integrity: re-fetch failed (")

    def _seed_lake_record(self, ctx: dl.KitContext) -> None:
        from brutex.data.raw_integrity import build_record
        from brutex.data.raw_integrity_store import append_record

        record = build_record(
            {"candles": full_day_candles("2026-07-01")},
            exchange="NSE",
            segment="CASH",
            groww_symbol="NSE-NIFTY",
            interval="1minute",
            window_start="2026-07-01 09:15:00",
            window_end="2026-07-01 15:30:00",
            now=_NOW,
        )
        append_record(record, ctx.lake_root)

    def test_against_lake_skips_with_no_records(self, tmp_path: Path) -> None:
        deps, ctx, _box = prepare_client(tmp_path, against_lake=True)
        outcome = dl.stage_integrity(deps, ctx)
        assert outcome.status == dl.STATUS_SKIP
        assert "no integrity sidecar records" in outcome.lines[0]

    def test_against_lake_pass(self, tmp_path: Path) -> None:
        deps, ctx, _box = prepare_client(tmp_path, against_lake=True)
        self._seed_lake_record(ctx)
        outcome = dl.stage_integrity(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert outcome.lines[0] == dl.render_diagnostic("integrity.lake_pass", matched=1, total=1)

    def test_against_lake_mismatch_fails(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path, against_lake=True)
        self._seed_lake_record(ctx)
        box["handler"] = lambda _kwargs: {"candles": []}
        outcome = dl.stage_integrity(deps, ctx)
        assert outcome.status == dl.STATUS_FAIL
        assert outcome.lines[0] == dl.render_diagnostic(
            "integrity.lake_mismatch", mismatched=1, total=1
        )

    def test_against_lake_skips_when_over_budget(self, tmp_path: Path) -> None:
        deps, ctx, _box = prepare_client(tmp_path, against_lake=True)
        self._seed_lake_record(ctx)
        ctx.budget.spend(ctx.budget.remaining, label="drain")
        outcome = dl.stage_integrity(deps, ctx)
        assert outcome.status == dl.STATUS_SKIP
        assert "exceed the remaining call budget" in outcome.lines[0]


# ── stage 12 · resume ───────────────────────────────────────────────────


class TestStageResume:
    def test_pass_with_clean_lake(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        outcome = dl.stage_resume(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert outcome.lines[0] == dl.render_diagnostic("resume.pass", outstanding=0)

    def test_interrupted_pull_warns_g5_with_outstanding_windows(self, tmp_path: Path) -> None:
        # ONE import style for window_ledger_store repo-file-wide (CodeQL
        # py/import-and-import-from — the ledger-state test imports it as wls).
        import brutex.data.window_ledger_store as wls
        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_ledger import WindowLedger
        from brutex.observability.pull_journal import record_pull_start

        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        ledger = WindowLedger()
        ledger.plan(
            WindowKey(
                market="groww",
                security_id="NSE-NIFTY",
                interval="1minute",
                start=date(2026, 6, 1),
                end=date(2026, 6, 2),
            ),
            now=_NOW,
        )
        wls.save_window_ledger(ledger, ctx.lake_root)
        record_pull_start(
            ctx.lake_root, pull_id="p9", dry_run=False, now_utc=_NOW, monotonic_s=10.0
        )
        outcome = dl.stage_resume(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert outcome.lines[0] == dl.render_diagnostic(
            "resume.interrupted", started=_NOW.isoformat(), outstanding=1
        )

    def test_sleep_gap_warns_g6(self, tmp_path: Path) -> None:
        from datetime import timedelta

        from brutex.observability.pull_journal import record_pull_finish, record_pull_start

        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        record_pull_start(ctx.lake_root, pull_id="p", dry_run=False, now_utc=_NOW, monotonic_s=0.0)
        record_pull_finish(
            ctx.lake_root,
            pull_id="p",
            verdict="clean",
            now_utc=_NOW + timedelta(hours=6, minutes=28),
            monotonic_s=10.0,
        )
        outcome = dl.stage_resume(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert outcome.lines[0].startswith(
            "WARN resume[G6]: journal wall-vs-monotonic delta shows the host SLEPT ~388 min"
        )


# ── stage 13 · rate ─────────────────────────────────────────────────────


class TestStageRate:
    def test_skips_when_not_opted_in(self, tmp_path: Path) -> None:
        deps, ctx, _box = prepare_client(tmp_path)
        outcome = dl.stage_rate(deps, ctx)
        assert outcome.status == dl.STATUS_SKIP
        assert "--rate-probe" in outcome.lines[0]

    def test_skips_without_client(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps, rate_probe=True)
        assert dl.stage_rate(deps, ctx).status == dl.STATUS_SKIP

    def test_clean_minute_passes_with_honest_caveats(self, tmp_path: Path) -> None:
        deps, ctx, _box = prepare_client(tmp_path, rate_probe=True)
        ctx.options.rate_probe_seconds = 2.0
        outcome = dl.stage_rate(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert "0 raised throttles" in outcome.lines[0]
        assert "still UNVERIFIED" in outcome.lines[0]
        assert "register M8" in outcome.lines[0]

    def test_throttle_burst_aborts_with_evidence(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path, rate_probe=True)
        ctx.options.rate_probe_seconds = 3600.0
        box["handler"] = raising_handler(
            lambda: RuntimeError("Rate limit has breached for your request. Please retry.")
        )
        outcome = dl.stage_rate(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert outcome.lines[0].startswith("EVIDENCE rate[R4/M6]: throttle observed at ")
        assert outcome.lines[1] == dl.render_diagnostic("rate.aborted", bursts=3)

    def test_hard_call_cap_bounds_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        deps, ctx, _box = prepare_client(tmp_path, rate_probe=True)
        ctx.options.rate_probe_seconds = 10_000.0
        monkeypatch.setattr(dl, "RATE_PROBE_BUDGET", 5)
        outcome = dl.stage_rate(deps, ctx)
        assert outcome.status == dl.STATUS_PASS
        assert "(5 calls, 0 raised throttles)" in outcome.lines[0]


# ── runner ──────────────────────────────────────────────────────────────


class TestRunner:
    def test_all_benign_run_is_green_with_transcript(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        options = dl.KitOptions(lake_root=tmp_path / "lake")
        report = dl.run_live_doctor(deps, options)
        assert report.exit_code == 0
        assert [outcome.stage for outcome in report.outcomes] == list(range(14))
        statuses = {outcome.name: outcome.status for outcome in report.outcomes}
        assert statuses["resample"] == dl.STATUS_SKIP  # no derived TFs in the quarantine
        assert statuses["rate"] == dl.STATUS_SKIP  # opt-in
        assert statuses["lake"] == dl.STATUS_PASS
        assert report.transcript_path.is_file()
        text = report.transcript_path.read_text(encoding="utf-8")
        assert '"event": "run.start"' in text
        assert '"event": "run.done"' in text

    def test_default_lake_root_resolves_from_env(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path, env={"BRUTEX_LAKE_ROOT": str(tmp_path / "envlake")})
        report = dl.run_live_doctor(deps, dl.KitOptions(stages=frozenset({12})))
        assert report.exit_code == 0
        assert report.transcript_path.is_relative_to(tmp_path / "envlake")

    def test_fail_fast_stops_at_first_fail(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path, resolve_creds=_creds_error("no creds"))
        report = dl.run_live_doctor(deps, dl.KitOptions(lake_root=tmp_path / "lake"))
        assert report.exit_code == 2
        assert [outcome.name for outcome in report.outcomes] == ["env", "creds"]

    def test_keep_going_runs_everything(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path, resolve_creds=_creds_error("no creds"))
        report = dl.run_live_doctor(
            deps, dl.KitOptions(lake_root=tmp_path / "lake", keep_going=True)
        )
        assert report.exit_code == 2
        assert len(report.outcomes) == 14
        assert report.outcomes[3].status == dl.STATUS_SKIP  # token skipped without creds

    def test_stage_selection_runs_only_requested(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        report = dl.run_live_doctor(
            deps, dl.KitOptions(lake_root=tmp_path / "lake", stages=frozenset({0, 12}))
        )
        assert [outcome.stage for outcome in report.outcomes] == [0, 12]

    def test_budget_exhaustion_becomes_a_stage_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dl, "DEFAULT_CALL_BUDGET", 0)
        deps, _box = make_deps(tmp_path)
        report = dl.run_live_doctor(deps, dl.KitOptions(lake_root=tmp_path / "lake"))
        assert report.exit_code == 2
        token_outcome = next(o for o in report.outcomes if o.name == "token")
        assert token_outcome.status == dl.STATUS_FAIL
        assert token_outcome.lines[0].startswith("FAIL token: vendor-call budget exhausted (")

    def test_kit_side_oserror_is_internal_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken_stage(_deps: dl.KitDeps, _ctx: dl.KitContext) -> dl.StageOutcome:
            raise OSError("transcript volume vanished")

        monkeypatch.setattr(dl, "STAGES", ((0, "env", _broken_stage),))
        deps, _box = make_deps(tmp_path)
        with pytest.raises(dl.KitInternalError, match="kit-side I/O failure"):
            dl.run_live_doctor(deps, dl.KitOptions(lake_root=tmp_path / "lake"))

    def test_custom_transcript_sink_is_used(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        lines: list[str] = []
        report = dl.run_live_doctor(
            deps,
            dl.KitOptions(lake_root=tmp_path / "lake", stages=frozenset({0})),
            transcript_sink=lines.append,
        )
        assert report.exit_code == 0
        assert not report.transcript_path.exists()
        assert any('"event": "run.start"' in line for line in lines)


# ── small defensive arms ────────────────────────────────────────────────


class TestDefensiveArms:
    def test_rows_in_rejects_non_mapping_and_non_list_candles(self) -> None:
        assert dl._rows_in(None) == 0
        assert dl._rows_in({"candles": "nope"}) == 0
        assert dl._rows_in({"candles": [1, 2]}) == 2

    def test_symbol_is_index_prefers_the_loaded_master(self, tmp_path: Path) -> None:
        from brutex.adapters.groww.instruments import load_index_from_csv

        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        ctx.master = load_index_from_csv(_master_csv())
        assert ctx.symbol_is_index() is True  # NSE-NIFTY is IDX in the master
        ctx.options.symbol = "NSE-WIPRO"
        assert ctx.symbol_is_index() is False  # EQ row
        ctx.options.symbol = "NSE-UNLISTED"
        assert ctx.symbol_is_index() is False  # not in master → fallback set

    def test_build_kit_client_without_creds_is_a_kit_bug(self, tmp_path: Path) -> None:
        deps, _box = make_deps(tmp_path)
        ctx, _ = make_ctx(tmp_path, deps)
        with pytest.raises(dl.KitInternalError, match="without credentials"):
            dl._build_kit_client(deps, ctx)

    def test_capture_failure_is_transcribed_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.data.raw_integrity import RawIntegrityError

        def _boom(*_args: object, **_kwargs: object) -> object:
            raise RawIntegrityError("uncanonicalizable payload")

        monkeypatch.setattr(dl, "build_record", _boom)
        deps, ctx, _box = prepare_client(tmp_path)
        outcome = dl.stage_spot1m(deps, ctx)
        assert outcome.status == dl.STATUS_PASS  # the capture is best-effort
        assert ctx.record is None

    def test_single_throttle_without_burst_is_evidence_only(self, tmp_path: Path) -> None:
        deps, ctx, box = prepare_client(tmp_path, rate_probe=True)
        ctx.options.rate_probe_seconds = 1.0
        calls = {"n": 0}

        def _one_bad(kwargs: dict[str, object]) -> Mapping[str, object]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("vendor melted once")  # terminal -> raises out once
            return default_handler(kwargs)

        box["handler"] = _one_bad
        outcome = dl.stage_rate(deps, ctx)
        assert outcome.status == dl.STATUS_WARN
        assert outcome.lines[0].startswith("EVIDENCE rate[R4/M6]:")
        assert len(outcome.lines) == 1  # no abort line — the burst never sustained


class TestResolveProbeDay:
    """The PURE probe-day rule (hostile-review fold: extracted from the
    ``# pragma: no cover`` ``build_real_deps`` factory so the observed-overlay
    step-over is executable-tested and mutation-protected)."""

    def test_yesterday_trading_returned_directly(self) -> None:
        # Tue 2026-06-30 IST -> yesterday Mon 2026-06-29 is a session.
        now = datetime(2026, 6, 30, 10, 0, tzinfo=UTC)
        assert dl.resolve_probe_day(now) == date(2026, 6, 29)

    def test_weekend_stepped_over(self) -> None:
        # Mon 2026-06-29 IST -> Sun/Sat stepped over -> Fri 2026-06-26.
        now = datetime(2026, 6, 29, 10, 0, tzinfo=UTC)
        assert dl.resolve_probe_day(now) == date(2026, 6, 26)

    def test_observed_overlay_day_never_probed(self) -> None:
        # An observably-untraded Monday (overlay) is NOT a probe day: the walk
        # steps over it AND the weekend to Fri 2026-06-26. Reverting the overlay
        # subtraction to the plain calendar walk fails this test (the mutation
        # the review proved SURVIVED the pre-fold suite).
        now = datetime(2026, 6, 30, 10, 0, tzinfo=UTC)
        observed = frozenset({date(2026, 6, 29)})
        assert dl.resolve_probe_day(now, observed=observed) == date(2026, 6, 26)

    def test_ist_rollover_not_utc(self) -> None:
        # 20:00 UTC on Mon 2026-06-29 is ALREADY Tue 01:30 IST -> the probe day
        # is Mon 2026-06-29, pinning the astimezone(IST) conversion.
        now = datetime(2026, 6, 29, 20, 0, tzinfo=UTC)
        assert dl.resolve_probe_day(now) == date(2026, 6, 29)
