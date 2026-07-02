"""Unit tests for the kit's production seams (DEC-DOCTOR-LIVE-PROOF-KIT-001).

Covers the small reason-carrying seams the kit added to EXISTING modules:
``paths.lake_root_identity`` (G1), ``ParameterStoreTokenStore.load_with_reason``
(R7), the live-module mint blockers + growwapi version stamp (G2), the
``http_timeout`` shape-probe alias, and the resurrected ``probe_depth`` (R6).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from brutex.adapters.groww.aws_client import SSM_PARAMETER_NOT_FOUND
from brutex.adapters.groww.http_timeout import _REQUEST_METHOD_NAMES, REQUEST_METHOD_NAMES
from brutex.adapters.groww.live import (
    KitMintBlockedError,
    installed_growwapi_version,
    mint_blocked_token_factory,
    mint_blocked_totp_minter,
)
from brutex.adapters.groww.probe import (
    DEPTH_PROBE_MAX_CALLS,
    VERDICT_ABSENT,
    VERDICT_ERROR,
    VERDICT_OK,
    probe_depth,
)
from brutex.adapters.groww.token_store_aws import (
    ParameterStoreTokenStore,
    build_readonly_token_store_with,
)
from brutex.config.paths import lake_root_identity

from tests.fakes.fake_aws_client import FakeAwsClient

_NOW = datetime(2026, 7, 2, 6, 0, tzinfo=UTC)


# ── paths.lake_root_identity (G1) ───────────────────────────────────────


class TestLakeRootIdentity:
    def test_volumes_path_mounted(self) -> None:
        identity = lake_root_identity(
            Path("/Volumes/T8/brutex-lake"),
            ismount=lambda p: p == "/Volumes/T8",
            stat_dev=lambda _p: 42,
        )
        assert identity.volume_parent == Path("/Volumes/T8")
        assert identity.volume_mounted is True
        assert identity.device == 42
        assert identity.exists is False

    def test_volumes_path_unmounted(self) -> None:
        identity = lake_root_identity(
            Path("/Volumes/T8/brutex-lake"), ismount=lambda _p: False, stat_dev=lambda _p: 1
        )
        assert identity.volume_mounted is False

    def test_non_volumes_path_has_no_volume_verdict(self, tmp_path: Path) -> None:
        identity = lake_root_identity(tmp_path, stat_dev=lambda _p: 7)
        assert identity.volume_parent is None
        assert identity.volume_mounted is None
        assert identity.exists is True

    def test_real_stat_dev_default(self, tmp_path: Path) -> None:
        identity = lake_root_identity(tmp_path)
        assert identity.device is not None

    def test_device_walks_to_nearest_existing_ancestor(self, tmp_path: Path) -> None:
        seen: list[str] = []

        def _stat(path: str) -> int:
            seen.append(path)
            if path == str(tmp_path):
                return 99
            raise OSError("does not exist")

        identity = lake_root_identity(tmp_path / "missing" / "lake", stat_dev=_stat)
        assert identity.device == 99
        assert seen[0] == str(tmp_path / "missing" / "lake")

    def test_device_none_when_every_stat_fails(self) -> None:
        def _stat(_path: str) -> int:
            raise OSError("no")

        identity = lake_root_identity(Path("/nowhere/lake"), stat_dev=_stat)
        assert identity.device is None

    def test_expanduser_applied(self) -> None:
        identity = lake_root_identity(Path("~/lake"), stat_dev=lambda _p: 1)
        assert "~" not in str(identity.path)


# ── ParameterStoreTokenStore.load_with_reason (R7) ──────────────────────


def _store(client: FakeAwsClient) -> ParameterStoreTokenStore:
    return ParameterStoreTokenStore(client=client, token_param="/t/token")


class TestLoadWithReason:
    def test_hit_returns_token_and_no_miss(self) -> None:
        token, miss = _store(FakeAwsClient(parameters={"/t/token": "tok"})).load_with_reason(
            now_utc=_NOW
        )
        assert miss is None
        assert token is not None
        assert token.access_token == "tok"

    def test_read_error_carries_error_kind(self) -> None:
        token, miss = _store(FakeAwsClient(fail_get_parameter=True)).load_with_reason(now_utc=_NOW)
        assert token is None
        assert miss is not None
        assert (miss.kind, miss.error_type) == ("error", "FakeAwsError")
        assert "access denied" in miss.detail

    def test_parameter_not_found_is_genuinely_absent(self) -> None:
        client = FakeAwsClient(param_not_found=frozenset({"/t/token"}))
        token, miss = _store(client).load_with_reason(now_utc=_NOW)
        assert token is None
        assert miss is not None
        assert miss.kind == "absent"
        assert miss.error_type == SSM_PARAMETER_NOT_FOUND

    def test_blank_value_is_absent(self) -> None:
        token, miss = _store(FakeAwsClient(parameters={"/t/token": "  "})).load_with_reason(
            now_utc=_NOW
        )
        assert token is None
        assert miss is not None
        assert (miss.kind, miss.error_type) == ("absent", None)

    def test_token_param_property(self) -> None:
        assert _store(FakeAwsClient()).token_param == "/t/token"

    def test_load_delegates_and_keeps_never_raises_contract(self) -> None:
        from brutex.adapters.groww.auth import GrowwCredentials

        creds = GrowwCredentials(api_key="k", totp_secret="s")
        assert _store(FakeAwsClient(fail_get_parameter=True)).load(creds, now_utc=_NOW) is None
        assert (
            _store(FakeAwsClient(param_not_found=frozenset({"/t/token"}))).load(creds, now_utc=_NOW)
            is None
        )
        hit = _store(FakeAwsClient(parameters={"/t/token": "tok"})).load(creds, now_utc=_NOW)
        assert hit is not None
        assert hit.access_token == "tok"

    def test_build_readonly_store_with_resolves_default_param(self) -> None:
        store = build_readonly_token_store_with(FakeAwsClient())
        assert store.token_param == "/tickvault/prod/groww/access-token"


# ── live-module kit helpers (G2 / no-mint guard) ────────────────────────


class TestLiveKitHelpers:
    def test_mint_blocked_token_factory_refuses(self) -> None:
        with pytest.raises(KitMintBlockedError, match="never mints a Groww token"):
            mint_blocked_token_factory("key", "123456")

    def test_mint_blocked_totp_minter_refuses(self) -> None:
        with pytest.raises(KitMintBlockedError, match="never mints a TOTP code"):
            mint_blocked_totp_minter("secret")

    def test_installed_growwapi_version_absent_is_none(self) -> None:
        # The sandbox venv has no growwapi (the honest premise of the kit).
        assert installed_growwapi_version() is None

    def test_installed_growwapi_version_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.metadata

        monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.5.2")
        assert installed_growwapi_version() == "1.5.2"

    def test_request_method_alias_matches_private_source(self) -> None:
        assert REQUEST_METHOD_NAMES == _REQUEST_METHOD_NAMES
        assert REQUEST_METHOD_NAMES == ("_request_get", "_request_post", "_request_put")


# ── probe_depth (R6) ────────────────────────────────────────────────────


class _DepthClient:
    """A minimal fake vendor whose 1m history starts at *floor* (or never)."""

    def __init__(self, floor: date | None, *, epoch: bool = False, error: bool = False) -> None:
        self.floor = floor
        self.epoch = epoch
        self.error = error
        self.calls = 0

    def historical_candles(self, **kwargs: object) -> Mapping[str, object]:
        self.calls += 1
        if self.error:
            from brutex.adapters.groww.errors import GrowwError

            raise GrowwError("probe transport down")
        start = date.fromisoformat(str(kwargs["start"])[:10])
        end = date.fromisoformat(str(kwargs["end"])[:10])
        if self.floor is None or end < self.floor:
            return {"candles": []}
        first_day = max(start, self.floor)
        stamp: object = f"{first_day.isoformat()}T09:15:00"
        if self.epoch:
            stamp = int(datetime(first_day.year, first_day.month, first_day.day, 9, 15).timestamp())
        return {"candles": [[stamp, 100.0, 101.0, 99.0, 100.5, 0, None]]}


class TestProbeDepth:
    def test_boundary_data_confirms_floor_in_one_call(self) -> None:
        client = _DepthClient(date(2020, 1, 1))
        result = probe_depth(client, today=date(2026, 7, 2))
        assert result.verdict == VERDICT_OK
        assert result.boundary_has_data is True
        assert result.probed_floor == date(2020, 1, 1)
        assert result.calls_used == 1

    def test_empty_boundary_without_bisect_reports_unknown(self) -> None:
        client = _DepthClient(date(2021, 6, 14))
        result = probe_depth(client, today=date(2026, 7, 2))
        assert result.verdict == VERDICT_ABSENT
        assert "re-run with --depth-bisect" in result.detail
        assert result.calls_used == 1

    def test_bisect_locates_exact_floor(self) -> None:
        client = _DepthClient(date(2021, 6, 14))
        result = probe_depth(client, today=date(2026, 7, 2), bisect=True)
        assert result.verdict == VERDICT_OK
        assert result.probed_floor == date(2021, 6, 14)
        assert result.first_ts == "2021-06-14T09:15:00"
        assert result.calls_used <= DEPTH_PROBE_MAX_CALLS

    def test_bisect_epoch_stamps_fall_back_to_window_floor(self) -> None:
        true_floor = date(2021, 6, 14)
        client = _DepthClient(true_floor, epoch=True)
        result = probe_depth(client, today=date(2026, 7, 2), bisect=True)
        assert result.verdict == VERDICT_OK
        assert result.probed_floor is not None
        # Window-granularity floor: within one window of the true first bar.
        assert abs((result.probed_floor - true_floor).days) < 15

    def test_bisect_with_no_data_at_all(self) -> None:
        client = _DepthClient(None)
        result = probe_depth(client, today=date(2026, 7, 2), bisect=True)
        assert result.verdict == VERDICT_ABSENT
        assert "no probed 1m data at all" in result.detail
        assert result.calls_used == 2

    def test_transport_error_is_error_verdict(self) -> None:
        client = _DepthClient(date(2020, 1, 1), error=True)
        result = probe_depth(client, today=date(2026, 7, 2))
        assert result.verdict == VERDICT_ERROR
        assert "probe transport down" in result.detail

    def test_call_budget_cap_is_honest_about_non_convergence(self) -> None:
        client = _DepthClient(date(2024, 3, 8))
        result = probe_depth(client, today=date(2026, 7, 2), bisect=True, max_calls=4)
        assert result.verdict == VERDICT_OK
        assert result.calls_used == 4
        assert "call budget hit — floor is an UPPER bound" in result.detail

    def test_first_row_ts_defensive_arms(self) -> None:
        from brutex.adapters.groww.probe import _date_of_first_ts, _first_row_ts

        assert _first_row_ts(None) is None
        assert _first_row_ts({"candles": "nope"}) is None
        assert _first_row_ts({"candles": ["2021-06-14T09:15:00"]}) == "2021-06-14T09:15:00"
        assert _date_of_first_ts(None) is None
        assert _date_of_first_ts("1623630300") is None
        assert _date_of_first_ts("2021-06-14T09:15:00") == date(2021, 6, 14)
