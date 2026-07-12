"""Integration: the Dhan auth Golden-Rule-#8 hash chain over a full token lifecycle.

DEC-DHAN-AUTH-001 / PR-1. Drives the REAL :class:`DhanTokenManager` end-to-end so every
material auth action (adopt/store/renew/lockdown/exhausted) appends ONE line to a real
on-disk SHA-256 hash chain, then proves:

* the chain VERIFIES (:func:`~brutex.orchestrator.audit.verify_chain`),
* a single-byte on-disk TAMPER is DETECTED (``AuditChainCorruption``),
* the audit is FAIL-SOFT — a writer that always raises latches the audit off with a WARN yet
  auth STILL succeeds (the flight-recorder law: observability failure never changes behaviour).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from brutex.adapters.dhan import auth as auth_mod
from brutex.adapters.dhan import reauth as reauth_mod
from brutex.adapters.dhan.auth_audit import (
    ACTION_ADOPT,
    ACTION_LOCKDOWN,
    ACTION_RENEW,
    ACTION_STORE,
    DhanAuthAudit,
)
from brutex.adapters.dhan.errors import DhanAuthLockdown
from brutex.adapters.dhan.reauth import DhanTokenManager
from brutex.adapters.dhan.token_store_aws import DhanParameterStoreTokenStore
from brutex.config.credentials_types import DhanCredentials
from brutex.orchestrator.audit import AuditChainCorruption, AuditWriter, verify_chain

from tests.fakes.fake_aws_client import FakeAwsClient
from tests.fakes.fake_dhan_auth_http import FakeDhanAuthHttp, FakeHttpError

_CREDS = DhanCredentials(client_id="FAKE-CID", pin="FAKE-PIN", totp_secret="FAKE-TOTP")
_ANCHOR = datetime(2026, 7, 12, 6, 0, 0, tzinfo=UTC)
_PARAM = "/tickvault/prod/dhan/access-token"


def _clock() -> datetime:
    return _ANCHOR


@pytest.fixture(autouse=True)
def _inject_totp(monkeypatch: pytest.MonkeyPatch) -> None:
    real_mint = auth_mod.mint_access_token

    def _mint(creds: DhanCredentials, *, transport: object = None, clock: object = None) -> object:
        return real_mint(
            creds,
            transport=cast("Any", transport),
            totp_minter=lambda _s: "FAKE-CODE",
            clock=cast("Any", clock),
            sleeper=lambda _s: None,
        )

    monkeypatch.setattr(reauth_mod, "mint_access_token", _mint)


def _manager(
    tmp_path: Path,
    transport: FakeDhanAuthHttp,
    *,
    audit: DhanAuthAudit,
    alerter: object | None = None,
) -> DhanTokenManager:
    store = DhanParameterStoreTokenStore(client=FakeAwsClient(), param_path=_PARAM)
    return DhanTokenManager(
        store,
        lambda: _CREDS,
        transport=cast("Any", transport),
        alerter=cast("Any", alerter),
        audit=audit,
        clock=_clock,
        lock_path=tmp_path / "dhan-auth.lock",
    )


def test_chain_records_every_material_action_and_verifies(tmp_path: Path) -> None:
    audit_path = tmp_path / "dhan-auth.jsonl"
    audit = DhanAuthAudit(audit_path)
    transport = FakeDhanAuthHttp(
        [
            {"accessToken": "TOK-A"},  # cold-start mint
            {"accessToken": "TOK-B"},  # reauth#1 renew ok
            FakeHttpError(500, "renew down"),  # reauth#2 renew fail (falls through)
            FakeHttpError(401, "bad creds"),  # reauth#2 mint → invalid cred → lockdown
        ]
    )
    manager = _manager(tmp_path, transport, audit=audit)
    manager.reauth(manager.current_generation())  # renew
    with pytest.raises(DhanAuthLockdown):
        manager.reauth(manager.current_generation())  # renew-fail → mint-fail → lockdown

    n = verify_chain(audit_path)
    assert n >= 4  # at least: cold-start STORE+ADOPT, renew RENEW+STORE+ADOPT, LOCKDOWN
    actions = [
        line.split('"action":"', 1)[1].split('"', 1)[0]
        for line in audit_path.read_text(encoding="utf-8").strip().splitlines()
    ]
    # Every kind of material auth action reached the ledger.
    assert ACTION_ADOPT in actions
    assert ACTION_STORE in actions
    assert ACTION_RENEW in actions
    assert ACTION_LOCKDOWN in actions
    # The lockdown is the LAST material action recorded.
    assert actions[-1] == ACTION_LOCKDOWN
    assert not audit.degraded


def test_single_byte_tamper_is_detected(tmp_path: Path) -> None:
    audit_path = tmp_path / "dhan-auth.jsonl"
    audit = DhanAuthAudit(audit_path)
    transport = FakeDhanAuthHttp([{"accessToken": "TOK-A"}, {"accessToken": "TOK-B"}])
    manager = _manager(tmp_path, transport, audit=audit)
    manager.reauth(manager.current_generation())  # renew → more chain lines
    verify_chain(audit_path)  # clean before tamper

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2
    # Flip ONE hex char of the first entry's stored ``entry_hash`` — the recomputed hash then
    # mismatches the tampered stored value AND the next entry's prev_hash linkage breaks.
    good = json.loads(lines[0])["entry_hash"]
    bad = ("0" if good[0] != "0" else "f") + good[1:]
    lines[0] = lines[0].replace(good, bad, 1)
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(AuditChainCorruption):
        verify_chain(audit_path)


def test_fail_soft_audit_never_blocks_auth(tmp_path: Path) -> None:
    calls: list[int] = []

    def _boom(_path: Path) -> AuditWriter:
        calls.append(1)
        raise OSError("disk full")

    audit = DhanAuthAudit(tmp_path / "audit.jsonl", writer_factory=_boom)
    transport = FakeDhanAuthHttp([{"accessToken": "TOK-A"}])
    # Cold-start mint happens under the failing audit — auth MUST still succeed.
    manager = _manager(tmp_path, transport, audit=audit)
    assert manager.current_token() == "TOK-A"  # auth unaffected by audit failure
    assert audit.degraded  # audit latched off after the first append attempt
    assert calls == [1]  # latched — no further factory calls
