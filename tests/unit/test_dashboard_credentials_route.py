"""Route test — ``POST /api/v1/credentials`` JWT-shape guard (DEC-CREDS-JWT-SHAPE-001).

A truncated/corrupt access-token paste — the dominant cause of the opaque
``DH-906 Invalid Token`` failures — must be rejected at the web-form save
boundary with a clear "re-paste the FULL token" 400, instead of silently
persisting a doomed token. A well-formed JWT still saves (mode 0600) and the
route echoes back only the masked preview, never a usable secret.

The save is redirected to an isolated tmp file via ``BRUTEX_CREDENTIALS_FILE``
so the real ``~/.brutex/credentials.json`` is never touched. Skipped cleanly
when the ``[ui]`` extra (FastAPI) is not installed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")

from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.dashboard.app import DashboardState, build_app
from fastapi.testclient import TestClient

# FAKE values only — never a real secret. The token is a structurally valid
# (three base64url segments) but otherwise meaningless JWT.
_VALID_JWT = "eyJhbGc.eyJzdWImC6.sIg-na_ture"
_TRUNCATED = "eyJhbGc.eyJzdWImC6"  # lost the signature segment
_CLIENT_ID = "1100123456"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    """A TestClient whose credential saves land in an isolated tmp file."""
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    with TestClient(build_app(DashboardState(), instrument_store=None)) as c:
        yield c


def test_truncated_token_is_rejected_400(client: TestClient, tmp_path: Path) -> None:
    """A 2-segment (truncated) token → 400 're-paste', and nothing is saved."""
    resp = client.post(
        "/api/v1/credentials",
        json={"access_token": _TRUNCATED, "client_id": _CLIENT_ID},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "truncated" in detail
    assert "re-paste the FULL token" in detail
    # The reason must NOT echo the pasted value.
    assert _TRUNCATED not in detail
    # Nothing was persisted — the doomed token never reached the store.
    assert not (tmp_path / "credentials.json").exists()


def test_valid_jwt_saves_and_returns_masked_preview(client: TestClient, tmp_path: Path) -> None:
    """A well-formed JWT saves (mode 0600) and the route returns only a masked preview."""
    resp = client.post(
        "/api/v1/credentials",
        json={"access_token": _VALID_JWT, "client_id": _CLIENT_ID},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["present"] is True
    assert body["access_token_len"] == len(_VALID_JWT)
    # The full secret is never echoed back.
    assert _VALID_JWT not in str(body)
    assert (tmp_path / "credentials.json").exists()
