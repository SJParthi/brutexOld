"""AWS Parameter-Store credential reader — the SOLE source of the Groww credentials.

DEC-AWS-ONLY-CREDS-001 (operator's FINAL design, 2026-07-01, stacked on
DEC-TOKEN-BROKER-AWS-001). The Groww ``api_key`` + TOTP secret live in AWS Systems
Manager Parameter Store as SecureStrings — ``/tickvault/prod/groww/api-key`` +
``/tickvault/prod/groww/totp-secret`` (region ``ap-south-1``) — so a key rotation
propagates to BOTH BruteX and TickVault at once (not just one machine's
``~/.brutex/credentials.json``). This module is the single reader; the in-app credentials
webpage AND the local env-var / ``credentials.json`` file paths were REMOVED, so AWS is now
the ONLY credential source (no env fallback, no file fallback, no webpage).

Design (fail-fast — the opposite of the opt-in graceful-fallback design of #1015):

* **NOT gated on any flag.** AWS is the default AND only source. Reading is mandatory.
* **Fail-fast, no silent fallback.** A missing boto3 extra, an absent/blank parameter, or
  ANY SSM/network/IAM/KMS error raises a CLEAR :class:`~brutex.config.credentials_types.\
  CredentialsError` telling the operator exactly what to fix — it NEVER returns ``None`` and
  NEVER falls back to env vars, a local file, or the removed webpage (Golden Rule #1: no
  silent fabrication of a credential source).
* **Leaf-level shared types.** :class:`CredentialsError` + :class:`GrowwFileCredentials` are
  imported from the dependency-free leaf :mod:`brutex.config.credentials_types` (which imports
  neither this module nor :mod:`credentials`), so this reader is a lower-level PRIMITIVE that
  :mod:`credentials` imports one-directionally — NO module-level import cycle
  (DEC-AWS-ONLY-CREDS-001; the earlier bidirectional import was flagged by CodeQL).
* **Both fields or a loud error.** A partial fetch (api_key present, totp_secret absent)
  raises — never a mixed / half-resolved credential.
* **Injectable client for tests.** The real boto3 client build is confined to a
  ``# pragma: no cover`` factory; a deterministic in-memory fake
  (``tests/fakes/fake_aws_client.py``) drives every branch to 100% line+branch with NO
  boto3 install and NO network.

A read is a plain ``GetParameter WithDecryption=True`` — AWS KMS-decrypts the SecureString
server-side (AWS SSM docs "Working with SecureString parameters"; ``GetParameter`` API
reference). BruteX only READS these parameters (never writes them — a dedicated Lambda /
the operator owns writing them).
"""

from __future__ import annotations

import structlog

from brutex.adapters.aws_client import (
    SSM_PARAMETER_NOT_FOUND,
    AwsClient,
    build_real_aws_client,
    resolve_token_broker_region,
    resolve_token_param_path,
)
from brutex.adapters.groww.constants import (
    GROWW_TOKEN_PARAM_API_KEY_DEFAULT,
    GROWW_TOKEN_PARAM_API_KEY_ENV,
    GROWW_TOKEN_PARAM_TOTP_SECRET_DEFAULT,
    GROWW_TOKEN_PARAM_TOTP_SECRET_ENV,
)
from brutex.config.credentials_types import (
    CredentialsError,
    DhanCredentials,
    GrowwFileCredentials,
)

_LOG = structlog.get_logger("brutex.config.credentials_aws")

# ── Dhan SSM parameter paths (DEC-DHAN-AUTH-001, MIGRATION.md #1755) ─────────
# The Dhan headless-mint credential triple lives in AWS Parameter Store SecureStrings under
# ``/tickvault/prod/dhan/*`` (region resolved by the ONE shared resolver). Each honours a
# ``BRUTEX_TOKEN_PARAM_DHAN_*`` path override (config-only relocation). The access-token path
# is here too (the ONE SecureString BruteX WRITES — the Dhan token store owns writing it).
DHAN_TOKEN_PARAM_CLIENT_ID_ENV: str = "BRUTEX_TOKEN_PARAM_DHAN_CLIENT_ID"  # noqa: S105  # env var name, not a secret
DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT: str = "/tickvault/prod/dhan/client-id"  # noqa: S105  # parameter path, not a secret
DHAN_TOKEN_PARAM_PIN_ENV: str = "BRUTEX_TOKEN_PARAM_DHAN_PIN"  # noqa: S105  # env var name, not a secret
DHAN_TOKEN_PARAM_PIN_DEFAULT: str = "/tickvault/prod/dhan/pin"  # noqa: S105  # parameter path, not a secret
DHAN_TOKEN_PARAM_TOTP_SECRET_ENV: str = "BRUTEX_TOKEN_PARAM_DHAN_TOTP_SECRET"  # noqa: S105  # env var name, not a secret
DHAN_TOKEN_PARAM_TOTP_SECRET_DEFAULT: str = "/tickvault/prod/dhan/totp-secret"  # noqa: S105  # parameter path, not a secret
DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV: str = "BRUTEX_TOKEN_PARAM_DHAN_ACCESS_TOKEN"  # noqa: S105  # env var name, not a secret
DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT: str = "/tickvault/prod/dhan/access-token"  # noqa: S105  # parameter path, not a secret

#: How to fix an AWS credential-resolution failure, appended to the raised error. Names
#: the fixed default parameter paths + the deploy pointer; contains NO secret values.
AWS_CREDS_HOW_TO_FIX: str = (
    "seed the Groww credentials in AWS Parameter Store "
    "(/tickvault/prod/groww/api-key + /tickvault/prod/groww/totp-secret, SecureStrings "
    "in ap-south-1) — deploy the minter (deploy/aws_token_minter/deploy.sh) first; "
    "BruteX reads credentials ONLY from AWS (no env-var / local-file / webpage fallback)"
)

#: How to fix a Dhan AWS credential-resolution failure (DEC-DHAN-AUTH-001). Names the fixed
#: default parameter paths + the runbook; contains NO secret values.
DHAN_CREDS_HOW_TO_FIX: str = (
    "seed the Dhan credentials in AWS Parameter Store "
    "(/tickvault/prod/dhan/client-id + /tickvault/prod/dhan/pin + "
    "/tickvault/prod/dhan/totp-secret, SecureStrings in ap-south-1) — see "
    "docs/runbooks/dhan-auth.md; BruteX reads Dhan credentials ONLY from AWS "
    "(no env-var / local-file / webpage fallback)"
)


def load_groww_aws_credentials(*, client: AwsClient | None = None) -> GrowwFileCredentials:
    """Load the Groww credentials from AWS Parameter Store — the SOLE source.

    Reads ``/tickvault/prod/groww/api-key`` + ``/tickvault/prod/groww/totp-secret`` (both
    env-overridable path names) as SecureStrings (``WithDecryption=True``). This is
    MANDATORY and FAIL-FAST: any failure — boto3 missing, either parameter absent/blank, or
    ANY SSM/network/IAM/KMS error — raises :class:`~brutex.config.credentials_types.CredentialsError`
    with a clear, secret-free fix message. It NEVER returns ``None`` and NEVER falls back to
    an env var, a local file, or the removed webpage (DEC-AWS-ONLY-CREDS-001).

    Parameters
    ----------
    client
        Injected :class:`~brutex.adapters.groww.aws_client.AwsClient` for tests (a fake).
        ``None`` builds the real boto3 client lazily (``# pragma: no cover``).

    Returns
    -------
    GrowwFileCredentials
        The resolved credentials (both fields non-blank).

    Raises
    ------
    CredentialsError
        When AWS is unreachable / not configured, boto3 is missing, or either parameter is
        absent or blank. The message names the parameter paths + the deploy pointer, never a
        secret value.
    """
    resolved_client = (
        client
        if client is not None
        else _build_client_or_raise(vendor_label="Groww", how_to_fix=AWS_CREDS_HOW_TO_FIX)
    )
    params = _load_vendor_params(
        resolved_client,
        (
            ("api_key", GROWW_TOKEN_PARAM_API_KEY_ENV, GROWW_TOKEN_PARAM_API_KEY_DEFAULT),
            (
                "totp_secret",
                GROWW_TOKEN_PARAM_TOTP_SECRET_ENV,
                GROWW_TOKEN_PARAM_TOTP_SECRET_DEFAULT,
            ),
        ),
        vendor_label="Groww",
        how_to_fix=AWS_CREDS_HOW_TO_FIX,
    )
    return GrowwFileCredentials(api_key=params["api_key"], totp_secret=params["totp_secret"])


def load_dhan_aws_credentials(*, client: AwsClient | None = None) -> DhanCredentials:
    """Load the Dhan credentials from AWS Parameter Store — the SOLE source (DEC-DHAN-AUTH-001).

    Reads ``/tickvault/prod/dhan/client-id`` + ``/tickvault/prod/dhan/pin`` +
    ``/tickvault/prod/dhan/totp-secret`` (all ``BRUTEX_TOKEN_PARAM_DHAN_*``-overridable) as
    SecureStrings (``WithDecryption=True``). MANDATORY and FAIL-FAST exactly like the Groww
    reader: any failure — boto3 missing, ANY parameter absent/blank, or ANY SSM/network/IAM/
    KMS error — raises :class:`~brutex.config.credentials_types.CredentialsError` naming
    EVERY missing path + the runbook, never a secret value. It NEVER returns ``None`` and
    NEVER falls back to env/file/webpage.

    Parameters
    ----------
    client
        Injected :class:`~brutex.adapters.aws_client.AwsClient` for tests (a fake). ``None``
        builds the real boto3 client lazily (``# pragma: no cover``).

    Returns
    -------
    DhanCredentials
        The resolved credential triple (all three fields non-blank).

    Raises
    ------
    CredentialsError
        When AWS is unreachable / not configured, boto3 is missing, or any of the three
        parameters is absent or blank. The message names every missing parameter path + the
        runbook, never a secret value.
    """
    resolved_client = (
        client
        if client is not None
        else _build_client_or_raise(vendor_label="Dhan", how_to_fix=DHAN_CREDS_HOW_TO_FIX)
    )
    params = _load_vendor_params(
        resolved_client,
        (
            ("client_id", DHAN_TOKEN_PARAM_CLIENT_ID_ENV, DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT),
            ("pin", DHAN_TOKEN_PARAM_PIN_ENV, DHAN_TOKEN_PARAM_PIN_DEFAULT),
            (
                "totp_secret",
                DHAN_TOKEN_PARAM_TOTP_SECRET_ENV,
                DHAN_TOKEN_PARAM_TOTP_SECRET_DEFAULT,
            ),
        ),
        vendor_label="Dhan",
        how_to_fix=DHAN_CREDS_HOW_TO_FIX,
    )
    return DhanCredentials(
        client_id=params["client_id"], pin=params["pin"], totp_secret=params["totp_secret"]
    )


def _load_vendor_params(
    client: AwsClient,
    specs: tuple[tuple[str, str, str], ...],
    *,
    vendor_label: str,
    how_to_fix: str,
) -> dict[str, str]:
    """Read every ``(field, env_name, default)`` spec, raising an all-or-loud error on a miss.

    The ONE generic vendor-credential reader (CLAUDE.md §11 COMMON) shared by
    :func:`load_groww_aws_credentials` + :func:`load_dhan_aws_credentials`. Each spec resolves
    its Parameter-Store path (honouring the env override) and reads it via :func:`_read_param`
    (a hard SSM/IAM/network error is FAIL-FAST there, never mistaken for "absent"). An
    absent/blank value collects the path into *missing*; if ANY field is missing the whole
    read raises :class:`~brutex.config.credentials_types.CredentialsError` naming EVERY missing
    path + *how_to_fix* — never a partial/half-resolved credential. Returns a
    ``{field: value}`` map with every field non-blank on success. O(len(specs)).
    """
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for field_name, env_name, default in specs:
        path = resolve_token_param_path(env_name, default)
        value = _read_param(client, path, vendor_label=vendor_label, how_to_fix=how_to_fix)
        if value is None:
            missing.append(path)
        else:
            resolved[field_name] = value
    if missing:
        raise CredentialsError(
            f"missing {vendor_label} credentials in AWS Parameter Store: "
            f"{', '.join(missing)}; {how_to_fix}"
        )
    return resolved


def _build_client_or_raise(
    *, vendor_label: str, how_to_fix: str
) -> AwsClient:  # pragma: no cover — real boto3 client build
    """Build the real boto3 :class:`AwsClient`, raising a clear error when it cannot be built.

    A missing boto3 (:class:`~brutex.adapters.aws_client.AwsBackendUnavailable`, an
    ``ImportError``) OR any boto3-client construction error — a bad region, an absent
    profile (``ProfileNotFound``), a credential-chain failure — is FAIL-FAST: it raises
    :class:`~brutex.config.credentials_types.CredentialsError` naming the *vendor_label* + the
    fix, never a silent fallback to env/file (DEC-AWS-ONLY-CREDS-001).
    """
    try:
        return build_real_aws_client(resolve_token_broker_region())
    except Exception as exc:  # pylint: disable=broad-exception-caught — any build failure is fail-fast
        # Vendor-NAMESPACED event name (Groww byte-identity: ``groww.creds.aws_unavailable``
        # matches origin/main; a Dhan failure emits ``dhan.creds.aws_unavailable``) so
        # log-keyed alerting never mismatches after this move-with-shim (finding fix).
        _LOG.error(f"{vendor_label.lower()}.creds.aws_unavailable", error_type=type(exc).__name__)
        raise CredentialsError(
            f"could not reach AWS Parameter Store for the {vendor_label} credentials "
            f"({type(exc).__name__}); {how_to_fix}"
        ) from exc


def _read_param(client: AwsClient, name: str, *, vendor_label: str, how_to_fix: str) -> str | None:
    """Read one SecureString parameter, returning its non-blank value or ``None``.

    A ``ParameterNotFound`` (matched by exception class name — no botocore import) or an
    absent/blank value returns ``None`` (the caller raises a coherent all-fields error). ANY
    OTHER SSM/network/IAM/KMS error is FAIL-FAST — it raises
    :class:`~brutex.config.credentials_types.CredentialsError` (a redacting log, never the
    value), since a transient/denied AWS read must not be mistaken for "credential absent".

    The *vendor_label* + *how_to_fix* (resolved once by the caller) are threaded through so a
    Dhan hard-read failure names Dhan + the Dhan runbook, not Groww (finding fix); the log
    events are vendor-NAMESPACED (``groww.creds.*`` stays byte-identical to origin/main;
    ``dhan.creds.*`` for the Dhan reader). The parameter value is NEVER captured — only the
    path *name* + the exception class name are surfaced.
    """
    try:
        value = client.get_parameter(name=name, with_decryption=True)
    except Exception as exc:  # pylint: disable=broad-exception-caught — classify absent vs hard failure
        reason = type(exc).__name__
        event_vendor = vendor_label.lower()
        if reason == SSM_PARAMETER_NOT_FOUND:
            _LOG.debug(f"{event_vendor}.creds.aws_param_absent", name=name)
            return None
        _LOG.error(f"{event_vendor}.creds.aws_read_failed", error_type=reason)
        raise CredentialsError(
            f"failed to read the {vendor_label} credential parameter {name} from AWS "
            f"({reason}); {how_to_fix}"
        ) from exc
    if value is None or not value.strip():
        return None
    return value


__all__ = [
    "AWS_CREDS_HOW_TO_FIX",
    "DHAN_CREDS_HOW_TO_FIX",
    "DHAN_TOKEN_PARAM_ACCESS_TOKEN_DEFAULT",
    "DHAN_TOKEN_PARAM_ACCESS_TOKEN_ENV",
    "DHAN_TOKEN_PARAM_CLIENT_ID_DEFAULT",
    "DHAN_TOKEN_PARAM_CLIENT_ID_ENV",
    "DHAN_TOKEN_PARAM_PIN_DEFAULT",
    "DHAN_TOKEN_PARAM_PIN_ENV",
    "DHAN_TOKEN_PARAM_TOTP_SECRET_DEFAULT",
    "DHAN_TOKEN_PARAM_TOTP_SECRET_ENV",
    "load_dhan_aws_credentials",
    "load_groww_aws_credentials",
]
