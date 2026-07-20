"""Leaf credential types shared by :mod:`credentials` + :mod:`credentials_aws`.

DEC-AWS-ONLY-CREDS-001. The AWS-only credential resolution needs
:mod:`brutex.config.credentials` (the higher-level resolver) and
:mod:`brutex.config.credentials_aws` (the AWS reader primitive) to share two symbols —
:class:`CredentialsError` + :class:`GrowwFileCredentials`. Defining them HERE, in a
dependency-free LEAF module that imports nothing from either, breaks the module-level import
cycle CodeQL flags: the import DAG is now strictly one-directional —

    credentials  ─┐
                  ├─▶ credentials_aws ─▶ credentials_types
    (top-level)   ┘   (AWS reader)        (leaf — imports neither)

Both consumers import these types from this leaf; :mod:`credentials` also RE-EXPORTS them
(in its ``__all__``) so every existing ``from brutex.config.credentials import
CredentialsError, GrowwFileCredentials`` call site is byte-unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class CredentialsError(Exception):
    """Credentials could not be saved, read, or resolved.

    Raised for blank inputs, insecure file permissions, a corrupt / wrong-shape file, a
    missing field on resolution, or an unreachable / absent AWS Parameter-Store credential.
    The message names WHICH field is involved (and how to set it) but NEVER a secret value.
    """


@dataclass(frozen=True, slots=True)
class GrowwFileCredentials:
    """An immutable pair of Groww TOTP credentials.

    Attributes
    ----------
    api_key
        The Groww TOTP api key (SSM ``/tickvault/prod/groww/api-key`` —
        DEC-AWS-ONLY-CREDS-001).
    totp_secret
        The Groww TOTP shared secret (SSM ``/tickvault/prod/groww/totp-secret`` —
        DEC-AWS-ONLY-CREDS-001), held only to mint the 6-digit code.
    """

    api_key: str
    totp_secret: str


@dataclass(frozen=True, slots=True)
class DhanCredentials:
    """An immutable pair of READ-ONLY Dhan credentials (DEC-DHAN-READONLY-AUTH-001).

    The Dhan read path needs exactly these two values (VERIFIED-live: SDK
    ``dhan_context.py`` — ``DhanContext(client_id, access_token)`` makes NO network
    call at construction). There is NO api-key / secret / PIN / TOTP on the read
    path — the 24-h JWT access token is MINTED elsewhere (a dedicated Lambda /
    TickVault) and BruteX only READS both from AWS SSM. NEVER minted / renewed /
    PutParameter'd by BruteX.

    Attributes
    ----------
    client_id
        The (public) Dhan account client id (SSM ``/tickvault/prod/dhan/client-id``).
    access_token
        The 24-h JWT account bearer token (SSM ``/tickvault/prod/dhan/access-token``,
        a SecureString). Held only to construct ``DhanContext`` + send the
        ``access-token`` header; ``repr``-suppressed so it never leaks into a log /
        traceback.
    """

    client_id: str
    access_token: str = field(repr=False)


__all__ = ["CredentialsError", "DhanCredentials", "GrowwFileCredentials"]
