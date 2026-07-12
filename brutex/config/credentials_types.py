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
    """An immutable triple of DhanHQ v2 headless-mint credentials (DEC-DHAN-AUTH-001).

    All three are AWS-SSM-only (``/tickvault/prod/dhan/*``, DEC-DHAN-AUTH-001) — never an env
    var, a file, or a webpage. Held only to mint the daily access token via the headless
    PIN+TOTP flow (``POST https://auth.dhan.co/app/generateAccessToken``). The *pin* and
    *totp_secret* carry ``field(repr=False)`` so ``repr(creds)`` (which the audit log /
    dashboard could persist) NEVER leaks a secret — only the non-secret *client_id* shows.

    Attributes
    ----------
    client_id
        The numeric Dhan client id (SSM ``/tickvault/prod/dhan/client-id``). NOT a secret in
        the sense the PIN/TOTP are — it is a public account identifier — but read from SSM
        alongside them so a single IAM grant covers the whole triple.
    pin
        The 6-digit login PIN (SSM ``/tickvault/prod/dhan/pin``); ``repr``-suppressed.
    totp_secret
        The Base32 TOTP shared secret (SSM ``/tickvault/prod/dhan/totp-secret``), held only
        to mint the 6-digit TOTP code; ``repr``-suppressed.
    """

    client_id: str
    pin: str = field(repr=False)
    totp_secret: str = field(repr=False)


__all__ = ["CredentialsError", "DhanCredentials", "GrowwFileCredentials"]
