"""Loud credential refusal — DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001 (audit finding R1).

Before this decision the dashboard computed ``dry_run = not
groww_credentials_present()`` at every pull route, so ANY Groww/AWS credential
failure SILENTLY swapped the real pull for the synthetic
``(SyntheticSpotAdapter, NullBarSink)`` pair and reported the pull GREEN with
counts while ZERO real bars landed — and the ``CredentialsError``'s actionable
fix text was discarded at the gate. This suite pins the new contract on every
layer:

* :func:`brutex.config.credentials.groww_credentials_error` — preserves the
  CredentialsError's own fix text instead of swallowing it to a bare ``False``,
  driven end-to-end through the REAL ``load_groww_aws_credentials``
  classification (fake SSM clients).
* The ``credentials_missing`` refusal verdict rendered red through the REAL
  ``banner_for`` machinery.
* The dashboard routes — a REAL pull with broken creds is a **503** whose
  detail carries the relayed fix text, logged at ERROR and stamped into the
  ``/pull-verdict`` banner as the red ``needs_operator`` refusal; an EXPLICIT
  ``?dry_run=true`` still works and is LOUDLY labeled synthetic — including
  the CredentialsError-raised-mid-build race (route 400 + background job
  settling ``error``).

Extreme permutations covered: creds absent · AWS reachable but a parameter
missing · parameter present-but-blank (invalid shape) · AWS hard read failure
(IAM/KMS) · CredentialsError raised mid-build (route + background job) ·
explicit dry-run (works, loudly labeled) · real pull with good creds
(behaviour unchanged).

The PURE unit tests on the shared gate + labels
(:func:`~brutex.adapters.ingest_common.gate_pull_credentials` /
``credentials_refusal_verdict`` / ``verdict_message`` / ``label_synthetic`` /
``mark_synthetic``) live in ``tests/unit/test_ingest_common_pull_verdict.py``,
and the CLI creds-vanished-mid-build race lives in
``tests/unit/test_cli_retry_storm.py`` (next to the up-front CLI refusal) —
placed so the strict per-module 100% coverage gates (the static file lists in
``ci.yml`` + the stem-matched discovery gate) scope them to their modules.
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.adapters.ingest_common import (
    CREDENTIALS_REFUSAL_HEADLINE,
    SYNTHETIC_DRY_RUN_LABEL,
    classify_pull,
    credentials_refusal_verdict,
)
from brutex.config.credentials import (
    CredentialsError,
    GrowwFileCredentials,
    groww_credentials_error,
    groww_credentials_present,
)
from brutex.config.credentials_aws import load_groww_aws_credentials
from brutex.dashboard.verdict_banner import banner_for

_FIX_TEXT = "seed /tickvault/prod/groww/api-key + totp-secret in ap-south-1 (test fix text)"


def _good_loader() -> GrowwFileCredentials:
    """Fake AWS loader — credentials resolve (values are FAKE, never real)."""
    return GrowwFileCredentials(api_key="gw_key_123456", totp_secret="JBSWY3DPEHPK3PXP")


def _bad_loader() -> GrowwFileCredentials:
    """Fake AWS loader — fail-fast with an actionable fix message."""
    raise CredentialsError(_FIX_TEXT)


# ────────────────────────────────────────────────────────────────────────
# config.credentials — the fix text is PRESERVED, never swallowed
# ────────────────────────────────────────────────────────────────────────


class TestGrowwCredentialsError:
    def test_returns_none_when_credentials_resolve(self) -> None:
        assert groww_credentials_error(aws_loader=_good_loader) is None

    def test_relays_the_credentials_error_text_verbatim(self) -> None:
        # The exact fix message — which parameter is wrong + what to do — is
        # returned, NOT discarded to a bare False (the R1 swallow).
        assert groww_credentials_error(aws_loader=_bad_loader) == _FIX_TEXT

    def test_present_gate_delegates_true(self) -> None:
        assert groww_credentials_present(aws_loader=_good_loader) is True

    def test_present_gate_delegates_false(self) -> None:
        assert groww_credentials_present(aws_loader=_bad_loader) is False


class _ParamMissingClient:
    """Fake SSM client: AWS reachable, the parameter itself absent."""

    def get_parameter(self, *, name: str, with_decryption: bool) -> str | None:
        del name, with_decryption

        class ParameterNotFound(Exception):
            """Named exactly like botocore's class (matched by name, not import)."""

        raise ParameterNotFound("parameter absent")


class _BlankParamClient:
    """Fake SSM client: parameter present but blank — the invalid-shape seed."""

    def get_parameter(self, *, name: str, with_decryption: bool) -> str | None:
        del name, with_decryption
        return "   "


class _AccessDeniedClient:
    """Fake SSM client: AWS reachable but the read is denied (IAM/KMS)."""

    def get_parameter(self, *, name: str, with_decryption: bool) -> str | None:
        del name, with_decryption

        class AccessDeniedException(Exception):
            """Named like the AWS SDK's access-denied class."""

        raise AccessDeniedException("denied")


class TestRealChainPermutations:
    """Drive the REAL load_groww_aws_credentials classification end-to-end."""

    def test_aws_reachable_but_parameter_missing_names_the_paths(self) -> None:
        reason = groww_credentials_error(
            aws_loader=lambda: load_groww_aws_credentials(client=_ParamMissingClient())
        )
        assert reason is not None
        assert "missing Groww credentials in AWS Parameter Store" in reason
        assert "/tickvault/prod/groww/api-key" in reason  # the actionable path

    def test_parameter_present_but_blank_is_invalid_shape(self) -> None:
        reason = groww_credentials_error(
            aws_loader=lambda: load_groww_aws_credentials(client=_BlankParamClient())
        )
        assert reason is not None
        assert "missing Groww credentials in AWS Parameter Store" in reason

    def test_aws_hard_read_failure_names_the_error_class(self) -> None:
        reason = groww_credentials_error(
            aws_loader=lambda: load_groww_aws_credentials(client=_AccessDeniedClient())
        )
        assert reason is not None
        assert "failed to read the Groww credential parameter" in reason
        assert "AccessDeniedException" in reason


# ────────────────────────────────────────────────────────────────────────
# verdict_banner — the refusal verdict renders RED with the fix text
# (the pure gate/label units live in test_ingest_common_pull_verdict.py)
# ────────────────────────────────────────────────────────────────────────


class TestRefusalBanner:
    def test_banner_for_credentials_missing_is_red_with_retry(self) -> None:
        banner = banner_for(credentials_refusal_verdict(_FIX_TEXT).to_dict())
        assert banner.kind == "credentials_missing"
        assert banner.css_class == "verdict--red"
        assert banner.colour == "red"
        assert banner.show_retry_button is True  # needs_operator pass-through
        assert _FIX_TEXT in banner.action


# ────────────────────────────────────────────────────────────────────────
# pull_controller — an explicit dry-run's finished verdict SAYS synthetic
# ────────────────────────────────────────────────────────────────────────


class TestBulkReportSyntheticVerdict:
    def _report(self, *, dry_run: bool) -> Any:
        pytest.importorskip("duckdb")
        from brutex.dashboard.pull_controller import BulkSpotReport, SymbolPullResult

        return BulkSpotReport(
            dry_run=dry_run,
            start_date="2020-01-01",
            end_date="2026-07-01",
            symbols=[SymbolPullResult(symbol="NIFTY", completed=1, failed=0, no_data=0)],
        )

    def test_dry_run_verdict_is_loudly_synthetic(self) -> None:
        verdict = self._report(dry_run=True).verdict
        assert SYNTHETIC_DRY_RUN_LABEL in verdict.headline
        assert verdict.kind == "clean"  # classification preserved, labeling loud

    def test_real_pull_verdict_is_unchanged(self) -> None:
        verdict = self._report(dry_run=False).verdict
        assert verdict == classify_pull(
            auth_aborted=False,
            still_failed_windows=0,
            failed_pairs=0,
            no_data=0,
            completed=1,
        )
        assert SYNTHETIC_DRY_RUN_LABEL not in verdict.headline


# ────────────────────────────────────────────────────────────────────────
# Dashboard routes — 503 refusal / explicit dry-run / good-creds unchanged
# ────────────────────────────────────────────────────────────────────────

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store pull routes need duckdb")

import time  # noqa: E402 — after the importorskip guards, matching sibling suites

from brutex.dashboard.app import DashboardState, build_app  # noqa: E402
from brutex.data.instrument_store import InstrumentStore  # noqa: E402
from brutex.models import Exchange, Instrument, InstrumentType, Segment  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app_module = importlib.import_module("brutex.dashboard.app")

#: Every dashboard pull route the gate must protect, with the minimal params
#: that reach the gate (it runs FIRST, before selection/validation).
_PULL_ROUTES: list[tuple[str, dict[str, str]]] = [
    ("/api/v1/universe/pull", {"symbol": "NIFTY"}),
    ("/api/v1/universe/pull-all", {"kind": "index"}),
    ("/api/v1/universe/pull-expiries-contracts", {"symbol": "NIFTY"}),
    ("/api/v1/universe/pull-historical", {"underlying": "NIFTY"}),
    ("/api/v1/universe/pull-pipeline", {}),
    ("/api/v1/universe/retry-failed", {}),
]


def _nifty() -> Instrument:
    """The admitted NIFTY index instrument fixture."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY",
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    """A real DuckDB-backed store with one admitted index (NIFTY)."""
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def client(store: InstrumentStore) -> Iterator[TestClient]:
    """A wired TestClient over the real app factory."""
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


def _drain_job(client: TestClient, *, timeout: float = 10.0) -> dict[str, Any]:
    """Poll /pull-progress until the single-flight background job settles."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error", "idle"}:
            return snap
        time.sleep(0.01)
    raise AssertionError("background pull job did not settle within the timeout")


class TestDashboardRefusal:
    @pytest.mark.parametrize(("path", "params"), _PULL_ROUTES)
    def test_every_pull_route_refuses_loudly_on_broken_creds(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        path: str,
        params: dict[str, str],
    ) -> None:
        """A REAL pull with broken creds → 503 + the relayed fix text + red verdict."""
        monkeypatch.setattr(app_module, "groww_credentials_error", lambda: _FIX_TEXT)
        resp = client.post(path, params=params)
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert CREDENTIALS_REFUSAL_HEADLINE in detail
        assert _FIX_TEXT in detail  # the DYNAMIC relay — whatever the error names
        # The /universe banner slot carries the RED needs-operator refusal.
        verdict = client.get("/api/v1/universe/pull-verdict").json()
        assert verdict["present"] is True
        assert verdict["kind"] == "credentials_missing"
        assert verdict["colour"] == "red"
        assert verdict["show_retry_button"] is True
        assert _FIX_TEXT in verdict["action"]
        # Nothing started — the refusal happens BEFORE any pull/job.
        assert client.get("/api/v1/universe/pull-progress").json()["state"] == "idle"

    @pytest.mark.parametrize(("path", "params"), _PULL_ROUTES)
    def test_explicit_dry_run_never_resolves_credentials(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        path: str,
        params: dict[str, str],
    ) -> None:
        """?dry_run=true short-circuits the gate — the resolver is NEVER called."""
        calls: list[bool] = []

        def _spy() -> str | None:
            calls.append(True)
            return _FIX_TEXT

        monkeypatch.setattr(app_module, "groww_credentials_error", _spy)
        # Offline fakes for every spine a started job could touch.
        monkeypatch.setattr(app_module, "pull_candles", _fake_pull_candles)
        monkeypatch.setattr(app_module, "pull_all_spot", _fake_pull_all_spot)
        monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
        monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_composite)
        resp = client.post(path, params={**params, "dry_run": "true"})
        assert resp.status_code != 503  # the gate NEVER refuses an explicit dry-run
        assert calls == []  # and NEVER touches the credential chain
        _drain_job(client)

    def test_real_pull_with_good_creds_is_unchanged(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Good creds → live pull proceeds exactly as before (no label, no refusal)."""
        monkeypatch.setattr(app_module, "groww_credentials_error", lambda: None)
        monkeypatch.setattr(app_module, "pull_all_spot", _fake_pull_all_spot)
        resp = client.post("/api/v1/universe/pull-all", params={"kind": "index"})
        assert resp.status_code == 200
        assert resp.json()["dry_run"] is False
        snap = _drain_job(client)
        assert snap["state"] == "done"
        # The finished (live) verdict carries NO synthetic label.
        verdict = client.get("/api/v1/universe/pull-verdict").json()
        assert verdict["present"] is True
        assert SYNTHETIC_DRY_RUN_LABEL not in verdict["headline"]

    def test_mid_build_credentials_error_surfaces_on_the_single_pull(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Creds vanish AFTER the gate: the single-symbol route surfaces the real error."""
        monkeypatch.setattr(app_module, "groww_credentials_error", lambda: None)

        def _mid_build_boom(*_a: Any, **_k: Any) -> Any:
            raise CredentialsError(_FIX_TEXT)

        monkeypatch.setattr(app_module, "pull_candles", _mid_build_boom)
        resp = client.post("/api/v1/universe/pull", params={"symbol": "NIFTY"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == f"CredentialsError: {_FIX_TEXT}"

    def test_mid_build_credentials_error_surfaces_on_the_background_job(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Creds vanish on the job thread: the job settles ERROR carrying the fix text."""
        monkeypatch.setattr(app_module, "groww_credentials_error", lambda: None)

        def _mid_build_boom(*_a: Any, **_k: Any) -> Any:
            raise CredentialsError(_FIX_TEXT)

        monkeypatch.setattr(app_module, "pull_all_spot", _mid_build_boom)
        resp = client.post("/api/v1/universe/pull-all", params={"kind": "index"})
        assert resp.status_code == 200  # the job starts; the failure is reported live
        snap = _drain_job(client)
        assert snap["state"] == "error"
        assert f"CredentialsError: {_FIX_TEXT}" in snap["detail"]


class TestDashboardSyntheticLabels:
    """The explicit dry-run's progress title says SYNTHETIC on every heavy route."""

    def _seed_registry(self, store: InstrumentStore) -> None:
        """Seed one cached NIFTY contract so /pull-historical resolves a selection."""
        from datetime import UTC, datetime

        from brutex.data.registry_store import RegistryStore

        path = store.path
        assert isinstance(path, Path)
        with RegistryStore.open(path.parent / "registry.duckdb") as registry:
            registry.upsert_contract(
                "NSE",
                "NIFTY",
                date(2024, 1, 25),
                "NSE-NIFTY-25Jan24-21000-CE",
                side="CE",
                strike=21000.0,
                run_id="r",
                fetched_at=datetime(2024, 1, 2, tzinfo=UTC),
            )

    def _assert_running_detail_labeled(
        self, client: TestClient, path: str, params: dict[str, str]
    ) -> None:
        """POST an explicit dry-run and assert the RUNNING title carries the label."""
        resp = client.post(path, params={**params, "dry_run": "true"})
        assert resp.status_code == 200
        assert resp.json()["dry_run"] is True
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert snap["state"] == "running"
        assert SYNTHETIC_DRY_RUN_LABEL in snap["detail"]
        _RELEASE_GATE.set()
        assert _drain_job(client)["state"] == "done"

    def test_pull_historical_dry_run_detail_is_loudly_synthetic(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_registry(store)
        _RELEASE_GATE.clear()
        monkeypatch.setattr(app_module, "pull_contract_candles", _gated(_fake_historical_candles))
        self._assert_running_detail_labeled(
            client, "/api/v1/universe/pull-historical", {"underlying": "NIFTY"}
        )

    def test_pull_historical_composite_dry_run_detail_is_loudly_synthetic(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _RELEASE_GATE.clear()
        monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _gated(_fake_composite))
        self._assert_running_detail_labeled(
            client, "/api/v1/universe/pull-historical", {"symbols": "NIFTY"}
        )


# Offline route fakes (module-level so both parametrized tests share them).


#: Gate the fakes so the RUNNING progress snapshot (whose ``detail`` carries the
#: synthetic label until the job finishes) can be sampled deterministically.
_RELEASE_GATE = threading.Event()


def _gated(fake: Any) -> Any:
    """Wrap *fake* to wait on the module release gate before returning."""

    def _waiting(*args: Any, **kwargs: Any) -> Any:
        assert _RELEASE_GATE.wait(timeout=10.0), "test never released the gated fake pull"
        return fake(*args, **kwargs)

    return _waiting


def _fake_historical_candles(contracts: Any, **kwargs: Any) -> Any:
    """Offline stand-in for the contract-candle pull."""
    from brutex.dashboard.historical_pull import HistoricalPullReport

    return HistoricalPullReport(
        dry_run=kwargs["dry_run"],
        intervals=tuple(kwargs["intervals"]),
        contracts_selected=len(list(contracts)),
        contracts_completed=0,
        bars_appended=0,
        windows=0,
        failed_pairs=0,
        still_failed_windows=0,
        auth_aborted=False,
        note="offline fake",
        phase1_clean=True,
        phase1_complete=(),
        start_dates={},
        minute_requested=True,
        minute_skipped=False,
    )


def _fake_pull_candles(instrument: Instrument, **kwargs: Any) -> Any:
    """Offline stand-in for the single-symbol pull."""
    from brutex.dashboard.pull_controller import PullCandlesReport

    return PullCandlesReport(
        symbol=instrument.symbol_name,
        year=0,
        dry_run=kwargs["dry_run"],
        start_date="2020-01-01",
        end_date="2026-07-01",
    )


def _fake_pull_all_spot(instruments: list[Instrument], **kwargs: Any) -> Any:
    """Offline stand-in for the bulk spot pull."""
    from brutex.dashboard.pull_controller import BulkSpotReport, SymbolPullResult

    return BulkSpotReport(
        dry_run=kwargs["dry_run"],
        start_date="2020-01-01",
        end_date="2026-07-01",
        symbols=[
            SymbolPullResult(symbol=inst.symbol_name, completed=1, failed=0, no_data=0)
            for inst in instruments
        ],
    )


def _fake_metadata(*args: Any, **kwargs: Any) -> Any:
    """Offline stand-in for the metadata chain pull (mirrors the pipeline suite's fake)."""
    from brutex.dashboard.metadata_pull import MetadataPullReport

    del args
    return MetadataPullReport(
        dry_run=kwargs["dry_run"],
        years=tuple(kwargs["years"]),
        underlyings=("NIFTY",),
        expiry_cells=0,
        cells_skipped=0,
        expiry_failures=0,
        expiries_persisted=0,
        contract_jobs=0,
        contract_failures=0,
        contracts_persisted=0,
        note="offline fake",
    )


def _fake_composite(*args: Any, **kwargs: Any) -> Any:
    """Offline stand-in for the composite metadata→candle pull."""
    from brutex.dashboard.composite_pull import CompositePullReport
    from brutex.dashboard.historical_pull import HistoricalPullReport

    del args
    return CompositePullReport(
        dry_run=kwargs["dry_run"],
        requested=("NIFTY",),
        tradable=("NIFTY",),
        skipped_reference_only=(),
        metadata=_fake_metadata(**kwargs),
        historical=HistoricalPullReport(
            dry_run=kwargs["dry_run"],
            intervals=("1minute",),
            contracts_selected=0,
            contracts_completed=0,
            bars_appended=0,
            windows=0,
            failed_pairs=0,
            still_failed_windows=0,
            auth_aborted=False,
            note="offline fake",
            phase1_clean=True,
            phase1_complete=(),
            start_dates={},
            minute_requested=True,
            minute_skipped=False,
        ),
        note="offline fake",
    )
