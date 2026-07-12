"""CLI exit-code contract tests for ``brutex dhan-token status`` (DEC-DHAN-AUTH-001).

Drives :func:`brutex.cli.cmd_dhan_token_status` with an INJECTED fake store (no real AWS, no
network) so the documented exit-code contract — ``0`` if fresh, ``1`` if stale / absent /
corrupt — and the ``envelope is None`` guard are directly verified (they were previously
suppressed behind a ``# pragma: no cover`` on the whole function). Also pins that the token
bytes never reach stdout / stderr (only the ``sha256(token)[:8]`` fingerprint).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from brutex.adapters.dhan.constants import DHAN_TOKEN_REFRESH_AGE_S
from brutex.adapters.dhan.token_store_aws import (
    DhanParameterStoreTokenStore,
    DhanTokenEnvelope,
    encode_envelope,
)
from brutex.cli import EXIT_OK, EXIT_USER_ERROR, cmd_dhan_token_status

from tests.fakes.fake_aws_client import FakeAwsClient

_PARAM = "/tickvault/prod/dhan/access-token"
#: An obviously-fake, non-secret access token (no real JWT / no ``eyJ`` prefix).
_FAKE_TOKEN = "FAKE-DHAN-ACCESS-TOKEN-abc123"


def _store(seed: dict[str, str] | None = None) -> DhanParameterStoreTokenStore:
    return DhanParameterStoreTokenStore(
        client=FakeAwsClient(parameters=dict(seed or {})), param_path=_PARAM
    )


def _env_json(minted: datetime) -> str:
    return encode_envelope(
        DhanTokenEnvelope(token=_FAKE_TOKEN, minted_at_utc=minted, source="mint")
    )


def test_fresh_token_exit_ok(capsys: pytest.CaptureFixture[str]) -> None:
    fresh = datetime.now(tz=UTC) - timedelta(seconds=10)
    rc = cmd_dhan_token_status(_store({_PARAM: _env_json(fresh)}))
    assert rc == EXIT_OK
    captured = capsys.readouterr()
    assert "fresh" in captured.out
    assert _FAKE_TOKEN not in (captured.out + captured.err)


def test_stale_token_exit_user_error(capsys: pytest.CaptureFixture[str]) -> None:
    stale = datetime.now(tz=UTC) - timedelta(seconds=DHAN_TOKEN_REFRESH_AGE_S + 3_600)
    rc = cmd_dhan_token_status(_store({_PARAM: _env_json(stale)}))
    assert rc == EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert "stale" in captured.out
    assert _FAKE_TOKEN not in (captured.out + captured.err)


def test_absent_token_exit_user_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cmd_dhan_token_status(_store())  # nothing seeded → load() returns None
    assert rc == EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert "absent" in captured.err
    assert _FAKE_TOKEN not in (captured.out + captured.err)


def test_corrupt_envelope_exit_user_error(capsys: pytest.CaptureFixture[str]) -> None:
    # A poisoned parameter decodes to None (corrupt) → same absent-guard → EXIT_USER_ERROR.
    rc = cmd_dhan_token_status(_store({_PARAM: "not-a-valid-envelope"}))
    assert rc == EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert _FAKE_TOKEN not in (captured.out + captured.err)
