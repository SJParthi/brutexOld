"""Centralized Groww daily-access-token minter — AWS Lambda handler.

DEC-TOKEN-MINTER-CENTRAL-001 (docs/adr/0014-centralized-token-minter.md,
docs/MIGRATION.md #1060). ONE Lambda is the SOLE writer of the shared Groww
daily access token. On an EventBridge schedule (~06:05 IST, shortly after the
06:00 IST daily token reset) and on an on-demand manual invoke, it:

1. reads the Groww ``api-key`` + ``totp-secret`` from AWS Systems Manager
   Parameter Store (SecureString, KMS-decrypted);
2. computes the current TOTP code from the secret;
3. calls ``GrowwAPI.get_access_token(api_key, totp)`` to mint a fresh token;
4. ``PutParameter``s the token to the shared token param
   (``/tickvault/prod/groww/access-token``, SecureString, KMS-encrypted,
   ``Overwrite=True``).

Both TickVault (live) and BruteX (backtest) then read that ONE token
READ-ONLY, so no application ever mints — this permanently eliminates the
multi-app "minting storm" that made the 06:00 IST reset flaky.

Single-writer, so **no DynamoDB / no compare-and-swap is needed**: only this
Lambda ever writes the token param, and Parameter Store ``PutParameter`` with
``Overwrite=True`` is a last-writer-wins overwrite that this design never races
against itself for (EventBridge does not overlap a still-running invocation of
the same rule by default, and the manual invoke is operator-driven).

Design constraints honoured here (Golden Rule #11 COMMON · Golden Rule #1
no-hallucination):

* **Self-contained** — the deployed Lambda bundle is ONLY
  ``deploy/aws_token_minter/`` plus its vendored runtime deps (``boto3`` is on
  the AWS-provided Lambda runtime; ``growwapi`` + ``pyotp`` are bundled by the
  operator's build). It does NOT import the ``brutex`` package, so it can be
  packaged and deployed independently of the backtest engine. The TOTP + mint
  logic mirrors ``brutex/adapters/groww/auth.py`` CONCEPTUALLY, re-implemented
  here rather than imported.
* **Injectable side effects** — every real AWS / Groww call is an injected
  callable with a real default shim marked ``# pragma: no cover`` (the real
  ``boto3``/``growwapi``/``pyotp`` calls can only run in AWS against the live
  vendor). The PURE decision logic — parse config, decide mint-or-skip, retry
  a transient issuance throttle, emit a CloudWatch metric — is driven to 100%
  line+branch by a fake SSM client + fake mint in ``tests/deploy/``.

AWS behavioural claims cited in the docstrings below are sourced to the AWS
documentation in ``docs/adr/0014-centralized-token-minter.md`` §"AWS API
references"; none is invented.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

# The Lambda-native logger. A self-contained infra handler uses the stdlib
# ``logging`` module (AWS Lambda wires this to CloudWatch Logs automatically)
# rather than ``structlog`` so the deploy bundle carries no extra dependency.
_LOG: Final = logging.getLogger("brutex.token_minter")
if not _LOG.handlers:  # pragma: no cover - Lambda runtime pre-installs a handler
    logging.basicConfig(level=logging.INFO)


# --------------------------------------------------------------------------- #
# Environment variable names (set by Terraform on the Lambda).
# --------------------------------------------------------------------------- #

#: SSM path of the SecureString Groww API key (created OUTSIDE this stack).
ENV_API_KEY_PARAM: Final[str] = "GROWW_API_KEY_PARAM"
#: SSM path of the SecureString Groww TOTP secret (created OUTSIDE this stack).
ENV_TOTP_SECRET_PARAM: Final[str] = "GROWW_TOTP_SECRET_PARAM"
#: SSM path of the SecureString shared access token (written by THIS Lambda).
ENV_TOKEN_PARAM: Final[str] = "GROWW_TOKEN_PARAM"
#: KMS key id/ARN/alias used to encrypt the token SecureString on write.
ENV_KMS_KEY_ID: Final[str] = "GROWW_KMS_KEY_ID"
#: CloudWatch namespace the mint-success / mint-failure metric is emitted to.
ENV_METRIC_NAMESPACE: Final[str] = "GROWW_METRIC_NAMESPACE"
#: Max in-handler retries for a transient Groww issuance throttle (429).
ENV_MAX_RETRIES: Final[str] = "GROWW_MINT_MAX_RETRIES"
#: Base seconds for the exponential issuance-throttle backoff.
ENV_RETRY_BACKOFF_SECONDS: Final[str] = "GROWW_MINT_RETRY_BACKOFF_SECONDS"

#: Default CloudWatch namespace when the env var is unset/blank.
DEFAULT_METRIC_NAMESPACE: Final[str] = "BruteX/GrowwTokenMinter"
#: Default number of retries for a transient issuance throttle.
DEFAULT_MAX_RETRIES: Final[int] = 3
#: Default base backoff (seconds) for the issuance-throttle retry.
DEFAULT_RETRY_BACKOFF_SECONDS: Final[float] = 2.0
#: The single CloudWatch metric name (Value=1 on success, 0 on failure).
METRIC_NAME: Final[str] = "TokenMintSuccess"

#: Phrases (lower-cased) that mark a Groww issuance error as a transient
#: RATE-LIMIT throttle (retryable), mirroring the ``_RATE_LIMIT_HINTS`` set in
#: ``brutex/adapters/groww/errors.py`` — phrase-only, never a bare ``"429"``
#: substring (which would false-match an unrelated message).
_RATE_LIMIT_HINTS: Final[tuple[str, ...]] = ("rate limit", "too many request")

#: Phrases (lower-cased) that mark an AWS SSM/KMS ``ClientError`` as a transient
#: request THROTTLE (retryable on the token PutParameter). A botocore throttle
#: stringifies as "(ThrottlingException) ... Rate exceeded"; matched phrase-only so
#: the self-contained handler needs no botocore import.
_SSM_THROTTLE_HINTS: Final[tuple[str, ...]] = ("throttl", "rate exceeded")


class MinterConfigError(RuntimeError):
    """A required environment variable is missing or blank.

    Raised by :func:`resolve_config` naming the offending var(s) — never their
    values — so the config-validation branch is testable without any AWS call.
    """


class TokenMintError(RuntimeError):
    """The token could not be minted after exhausting the retry budget.

    Carries the SANITIZED reason (secrets scrubbed) as ``str(exc)`` so a
    CloudWatch log line never leaks the api-key or TOTP secret.
    """


@dataclass(frozen=True, slots=True)
class MinterConfig:
    """Resolved, validated configuration for one minter invocation.

    Attributes
    ----------
    api_key_param, totp_secret_param, token_param
        SSM parameter paths. The first two are read; the third is written.
    kms_key_id
        KMS key id/ARN/alias used to encrypt the token SecureString on write.
    metric_namespace
        CloudWatch namespace for the mint success/failure metric.
    max_retries
        Retry budget for a transient Groww issuance throttle (``>= 0``).
    retry_backoff_seconds
        Base seconds for the exponential issuance-throttle backoff (``>= 0``).
    """

    api_key_param: str
    totp_secret_param: str
    token_param: str
    kms_key_id: str
    metric_namespace: str
    max_retries: int
    retry_backoff_seconds: float


@dataclass(frozen=True, slots=True)
class MintOutcome:
    """The result of one handler invocation (also the Lambda's JSON response).

    Attributes
    ----------
    ok
        ``True`` iff a fresh token was minted AND written to Parameter Store.
    attempts
        How many mint attempts were made (``1`` on first-try success).
    token_param
        The SSM path the token was written to (for the operator's log).
    """

    ok: bool
    attempts: int
    token_param: str

    def as_response(self) -> dict[str, Any]:
        """Return the JSON-serialisable Lambda response for this outcome."""
        return {
            "ok": self.ok,
            "attempts": self.attempts,
            "token_param": self.token_param,
        }


class SsmClient(Protocol):
    """The narrow subset of the boto3 SSM client this handler uses.

    Only ``get_parameter`` (with ``WithDecryption``) and ``put_parameter`` (with
    ``Overwrite`` + ``KeyId`` + ``Type='SecureString'``) are needed, so the
    Protocol keeps the injected fake minimal and the real dependency surface
    explicit. Signatures follow the boto3 SSM client (see ADR-0014
    §"AWS API references").
    """

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> Mapping[str, Any]:
        """Return the SSM ``GetParameter`` response mapping."""
        raise NotImplementedError  # pragma: no cover - Protocol stub, never executed

    def put_parameter(
        self,
        *,
        Name: str,
        Value: str,
        Type: str,
        Overwrite: bool,
        KeyId: str,
    ) -> Mapping[str, Any]:
        """Write an SSM parameter and return the ``PutParameter`` response."""
        raise NotImplementedError  # pragma: no cover - Protocol stub, never executed


class CloudWatchClient(Protocol):
    """The narrow subset of the boto3 CloudWatch client this handler uses."""

    def put_metric_data(
        self,
        *,
        Namespace: str,
        MetricData: list[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Emit one or more CloudWatch metric datums."""
        raise NotImplementedError  # pragma: no cover - Protocol stub, never executed


# --------------------------------------------------------------------------- #
# Real default shims — only runnable in AWS against the live vendor, so each is
# ``# pragma: no cover``. The pure logic below is driven by injected fakes.
# --------------------------------------------------------------------------- #


def _boto3_client(service: str) -> Any:  # pragma: no cover - needs boto3 + AWS
    """Build a boto3 client for *service* (region from the Lambda env)."""
    # The multi-code ignore + ``unused-ignore`` is portable across environments:
    # boto3 may be absent (import-not-found), present-but-untyped (import-untyped),
    # or fully resolvable (the ignore is then unused) — mirrors the established
    # ``brutex/adapters/groww/reauth.py`` lazy-import pattern.
    import boto3  # type: ignore[import-not-found, import-untyped, unused-ignore]

    return boto3.client(service)


def _real_ssm_client() -> SsmClient:  # pragma: no cover - needs boto3 + AWS
    """Build the default boto3 SSM client (region from the Lambda env)."""
    return cast("SsmClient", _boto3_client("ssm"))


def _real_cloudwatch_client() -> CloudWatchClient:  # pragma: no cover - needs boto3
    """Build the default boto3 CloudWatch client (region from the Lambda env)."""
    return cast("CloudWatchClient", _boto3_client("cloudwatch"))


def _real_totp(secret: str) -> str:  # pragma: no cover - needs pyotp + real secret
    """Default TOTP minter: ``pyotp.TOTP(secret).now()`` (lazy import)."""
    import pyotp  # type: ignore[import-not-found, import-untyped, unused-ignore]

    return str(pyotp.TOTP(secret).now())


def _real_token_factory(api_key: str, totp: str) -> str:  # pragma: no cover - needs growwapi
    """Default token factory: ``GrowwAPI.get_access_token(...)`` (lazy import)."""
    from growwapi import GrowwAPI  # type: ignore[import-not-found, import-untyped, unused-ignore]

    return str(GrowwAPI.get_access_token(api_key=api_key, totp=totp))


# --------------------------------------------------------------------------- #
# Pure logic (100% line+branch via fakes).
# --------------------------------------------------------------------------- #


def _require(env: Mapping[str, str], name: str) -> str:
    """Return the non-blank env var *name* or raise :class:`MinterConfigError`."""
    value = (env.get(name) or "").strip()
    if not value:
        raise MinterConfigError(f"missing required environment variable: {name}")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    """Parse a ``>= 0`` int env var, falling back to *default* if unset/blank.

    A present-but-unparseable or negative value raises
    :class:`MinterConfigError` — an invalid retry budget must fail loudly, not
    silently coerce to a default (Golden Rule #1: no invented value).
    """
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise MinterConfigError(f"{name} is not an integer: {raw!r}") from exc
    if parsed < 0:
        raise MinterConfigError(f"{name} must be >= 0, got {parsed}")
    return parsed


def _non_negative_float(env: Mapping[str, str], name: str, default: float) -> float:
    """Parse a ``>= 0`` float env var, falling back to *default* if unset/blank."""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError as exc:
        raise MinterConfigError(f"{name} is not a number: {raw!r}") from exc
    if parsed < 0:
        raise MinterConfigError(f"{name} must be >= 0, got {parsed}")
    return parsed


def resolve_config(env: Mapping[str, str] | None = None) -> MinterConfig:
    """Read + validate the minter configuration from the environment.

    Parameters
    ----------
    env
        The environment mapping (defaults to :data:`os.environ`).

    Returns
    -------
    MinterConfig
        The validated configuration.

    Raises
    ------
    MinterConfigError
        If any required param path is missing/blank, or a numeric override is
        present but invalid.
    """
    source = os.environ if env is None else env
    namespace = (source.get(ENV_METRIC_NAMESPACE) or "").strip() or DEFAULT_METRIC_NAMESPACE
    return MinterConfig(
        api_key_param=_require(source, ENV_API_KEY_PARAM),
        totp_secret_param=_require(source, ENV_TOTP_SECRET_PARAM),
        token_param=_require(source, ENV_TOKEN_PARAM),
        kms_key_id=_require(source, ENV_KMS_KEY_ID),
        metric_namespace=namespace,
        max_retries=_positive_int(source, ENV_MAX_RETRIES, DEFAULT_MAX_RETRIES),
        retry_backoff_seconds=_non_negative_float(
            source, ENV_RETRY_BACKOFF_SECONDS, DEFAULT_RETRY_BACKOFF_SECONDS
        ),
    )


def _read_secure_string(ssm: SsmClient, name: str) -> str:
    """Read a SecureString SSM parameter, decrypted, and return its value.

    Uses ``get_parameter(Name=..., WithDecryption=True)`` — the ``WithDecryption``
    flag makes SSM KMS-decrypt a SecureString before returning it (AWS SSM
    ``GetParameter``; see ADR-0014 §"AWS API references"). Raises
    :class:`MinterConfigError` if the response has no decrypted value (an
    empty/misconfigured param), so a blank credential never reaches the mint.
    """
    response = ssm.get_parameter(Name=name, WithDecryption=True)
    parameter = response.get("Parameter") or {}
    value = str(parameter.get("Value") or "").strip()
    if not value:
        raise MinterConfigError(f"SSM parameter has no value: {name}")
    return value


def _sanitize(text: str, *secrets: str) -> str:
    """Scrub every non-blank *secrets* occurrence in *text* to ``***``.

    Mirrors ``brutex.adapters.groww.auth._sanitize_reason`` — longest secret
    first, blank secrets skipped (``str.replace("", ...)`` would corrupt the
    string) — so a vendor error message can be logged WITHOUT leaking the
    api-key, the TOTP secret, or the minted 6-digit code.
    """
    sanitized = text
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        sanitized = sanitized.replace(secret, "***")
    return sanitized


def _is_rate_limit(exc: Exception) -> bool:
    """True iff *exc* looks like a transient Groww issuance RATE-LIMIT (429).

    Structural first: a ``code`` attribute equal to ``429`` (int or the
    digit-string ``"429"``) — mirroring ``brutex.adapters.groww.errors``. Then a
    phrase match on the (lower-cased) message against :data:`_RATE_LIMIT_HINTS`.
    A ``bool`` ``code`` is rejected (``True`` is an ``int`` subclass but never a
    status). Anything else is a terminal failure (wrong TOTP / expired key).
    """
    code = getattr(exc, "code", None)
    if isinstance(code, bool):
        code = None
    if isinstance(code, int) and code == 429:  # noqa: PLR2004 - HTTP 429 is a named status
        return True
    if isinstance(code, str) and code.strip() == "429":
        return True
    message = str(exc).lower()
    return any(hint in message for hint in _RATE_LIMIT_HINTS)


def _is_ssm_throttle(exc: Exception) -> bool:
    """True iff *exc* looks like a transient AWS SSM/KMS request THROTTLE.

    A ``botocore`` ``ClientError`` on a throttled ``PutParameter`` stringifies as
    e.g. ``An error occurred (ThrottlingException) when calling the PutParameter
    operation: Rate exceeded``. The handler is self-contained (no ``botocore``
    import), so it classifies phrase-only on the (lower-cased) message against
    :data:`_SSM_THROTTLE_HINTS` — a transient AWS throttle is retryable; any other
    error (``AccessDenied``, ``KMSInvalidState``, a genuine bug) is terminal.
    """
    message = str(exc).lower()
    return any(hint in message for hint in _SSM_THROTTLE_HINTS)


def _put_token_with_retry(
    ssm: SsmClient,
    config: MinterConfig,
    token: str,
    *,
    sleep: Callable[[float], None],
) -> None:
    """Write the token to Parameter Store, retrying a TRANSIENT SSM throttle.

    ``PutParameter(Overwrite=True, Type='SecureString', KeyId=...)`` (AWS SSM
    ``PutParameter``; ADR-0014 §"AWS API references"). A transient throttle
    (:func:`_is_ssm_throttle`) is retried up to ``config.max_retries`` times with
    the SAME exponential backoff (``retry_backoff_seconds * 2**attempt``) the mint
    uses, so a freshly-minted token is not lost to a momentary write throttle. Any
    non-throttle error — or exhausting the budget — propagates (caught by
    :func:`run`, which emits the failure metric).
    """
    for attempt in range(config.max_retries + 1):
        try:
            ssm.put_parameter(
                Name=config.token_param,
                Value=token,
                Type="SecureString",
                Overwrite=True,
                KeyId=config.kms_key_id,
            )
            return
        except Exception as exc:
            if _is_ssm_throttle(exc) and attempt < config.max_retries:
                backoff = config.retry_backoff_seconds * (2**attempt)
                _LOG.warning(
                    "groww.token.put_throttled attempt=%d/%d backoff=%.2fs",
                    attempt + 1,
                    config.max_retries + 1,
                    backoff,
                )
                sleep(backoff)
                continue
            raise
    # Unreachable: the final attempt (attempt == max_retries) always returns on a
    # clean write or re-raises (the throttle-retry guard is False when
    # attempt == max_retries), so the loop never falls through. This defensive
    # raise keeps mypy's return-path analysis honest.
    raise RuntimeError(  # pragma: no cover - defensive; loop always returns/raises
        "unreachable: put-token retry loop exhausted without return/raise"
    )


def mint_token(
    config: MinterConfig,
    ssm: SsmClient,
    *,
    totp_minter: Callable[[str], str] = _real_totp,
    token_factory: Callable[[str, str], str] = _real_token_factory,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Read creds, mint a fresh Groww access token, write it to Parameter Store.

    Reads the api-key + totp-secret SecureStrings, computes a TOTP code, and
    calls ``token_factory`` to mint. A transient issuance throttle
    (:func:`_is_rate_limit`) is retried up to ``config.max_retries`` times with
    exponential backoff (``retry_backoff_seconds * 2**attempt``); any other
    failure — or exhausting the budget — raises :class:`TokenMintError` with a
    SANITIZED reason. On success the token is written via
    :func:`_put_token_with_retry` (``put_parameter(Overwrite=True,
    Type='SecureString', KeyId=...)``; AWS SSM ``PutParameter``, ADR-0014 §"AWS
    API references"), which retries a transient SSM WRITE throttle on the same
    budget so a freshly-minted token is not lost to a momentary throttle.

    Returns
    -------
    int
        The number of mint attempts made (``1`` on first-try success).

    Raises
    ------
    TokenMintError
        If the token could not be minted after the retry budget is exhausted.
    """
    api_key = _read_secure_string(ssm, config.api_key_param)
    totp_secret = _read_secure_string(ssm, config.totp_secret_param)

    attempts = 0
    last_reason = "no attempt made"
    # max_retries RETRIES on top of the first try => (max_retries + 1) attempts.
    for attempt in range(config.max_retries + 1):
        attempts = attempt + 1
        # The TOTP computation (``pyotp.TOTP(secret).now()``) is INSIDE the guarded
        # try: a malformed TOTP secret makes ``pyotp`` raise, and that error can echo
        # the secret — it MUST be sanitized like any mint error, never allowed to
        # propagate raw. ``totp`` is seeded blank so the ``except`` sanitizer has a
        # value to scrub even when ``totp_minter`` is what raised (its own error may
        # carry the secret; the not-yet-computed code is not in it).
        totp = ""
        try:
            totp = totp_minter(totp_secret)
            token = token_factory(api_key, totp)
        except Exception as exc:
            last_reason = _sanitize(str(exc), api_key, totp_secret, totp)
            if _is_rate_limit(exc) and attempt < config.max_retries:
                backoff = config.retry_backoff_seconds * (2**attempt)
                _LOG.warning(
                    "groww.token.issuance_throttled attempt=%d/%d backoff=%.2fs reason=%s",
                    attempts,
                    config.max_retries + 1,
                    backoff,
                    last_reason,
                )
                sleep(backoff)
                continue
            _LOG.error("groww.token.issuance_failed attempt=%d reason=%s", attempts, last_reason)
            # ``from None`` (NOT ``from exc``): breaking the ``__cause__`` chain is
            # security-critical. The raw ``exc`` is the vendor SDK error, which may
            # ECHO the api-key / TOTP in its message; if it stayed chained, the AWS
            # Lambda runtime would log the FULL chained traceback (raw exc included)
            # to CloudWatch on an uncaught propagation out of ``lambda_handler`` —
            # leaking the secret. The SANITIZED ``last_reason`` is already logged
            # above and carried in ``str(TokenMintError)``, so no diagnostic signal
            # is lost (Golden Rule #1 / #10 — no secret ever reaches CloudWatch).
            raise TokenMintError(f"Groww access-token issuance failed: {last_reason}") from None
        token = token.strip()
        if not token:
            # A clean but EMPTY token is a failure, not a silent success —
            # treat it like a terminal issuance error (never write a blank).
            _LOG.error("groww.token.issuance_empty attempt=%d", attempts)
            raise TokenMintError("Groww access-token issuance returned an empty token")
        _put_token_with_retry(ssm, config, token, sleep=sleep)
        _LOG.info("groww.token.minted attempts=%d token_param=%s", attempts, config.token_param)
        return attempts

    # Unreachable in practice (the loop returns on success or raises on failure),
    # but a defensive terminal raise keeps mypy's return-path analysis honest.
    raise TokenMintError(  # pragma: no cover - defensive; loop always returns/raises
        f"Groww access-token issuance failed after {attempts} attempts: {last_reason}"
    )


def _emit_metric(cloudwatch: CloudWatchClient, namespace: str, *, success: bool) -> None:
    """Emit the ``TokenMintSuccess`` CloudWatch metric (``1`` ok / ``0`` fail).

    Best-effort: a metric-emit failure is logged and swallowed so it can never
    mask the real mint outcome (the CloudWatch alarm on a MISSING datum still
    fires on a total-metric outage). Uses ``put_metric_data`` (AWS CloudWatch
    ``PutMetricData``; ADR-0014 §"AWS API references").
    """
    try:
        cloudwatch.put_metric_data(
            Namespace=namespace,
            MetricData=[
                {
                    "MetricName": METRIC_NAME,
                    "Value": 1.0 if success else 0.0,
                    "Unit": "Count",
                }
            ],
        )
    except Exception as exc:
        _LOG.warning("groww.token.metric_emit_failed success=%s reason=%s", success, exc)


def run(
    env: Mapping[str, str] | None = None,
    *,
    ssm_factory: Callable[[], SsmClient] = _real_ssm_client,
    cloudwatch_factory: Callable[[], CloudWatchClient] = _real_cloudwatch_client,
    totp_minter: Callable[[str], str] = _real_totp,
    token_factory: Callable[[str, str], str] = _real_token_factory,
    sleep: Callable[[float], None] = time.sleep,
) -> MintOutcome:
    """Run one mint cycle: resolve config, mint + write token, emit the metric.

    This is the pure, fully-injectable core the AWS entry point
    (:func:`lambda_handler`) delegates to. Every side effect is an injected
    factory/callable so a fake SSM + fake mint drive 100% line+branch.

    Returns
    -------
    MintOutcome
        ``ok=True`` (with the attempt count) on a successful mint+write.

    Raises
    ------
    MinterConfigError
        On invalid configuration (surfaced to the caller/alarm).
    TokenMintError
        If the token could not be minted after the retry budget is exhausted.
    """
    config = resolve_config(env)
    ssm = ssm_factory()
    cloudwatch = cloudwatch_factory()
    try:
        attempts = mint_token(
            config,
            ssm,
            totp_minter=totp_minter,
            token_factory=token_factory,
            sleep=sleep,
        )
    except Exception:
        # Emit the failure metric for ANY exception raised out of ``mint_token`` —
        # not only the domain ``MinterConfigError`` / ``TokenMintError``, but also a
        # raw ``botocore`` SSM/KMS ``ClientError`` (a throttled ``GetParameter`` /
        # ``PutParameter`` or a KMS-denied decrypt raises straight out, bypassing the
        # domain wrappers). The stale-token alarm relies on a ``success=0`` datum, so
        # a failure must ALWAYS emit it before the error propagates and marks the
        # invocation errored — never leave the alarm to infer failure only from a
        # MISSING datum (Golden Rule #1: observability must be reliable, not lucky).
        # The metric emit is itself best-effort (``_emit_metric`` swallows), so this
        # can never mask or replace the original error, which is re-raised intact.
        _emit_metric(cloudwatch, config.metric_namespace, success=False)
        raise
    _emit_metric(cloudwatch, config.metric_namespace, success=True)
    return MintOutcome(ok=True, attempts=attempts, token_param=config.token_param)


def lambda_handler(
    event: Mapping[str, Any] | None = None,
    context: Any = None,
) -> dict[str, Any]:
    """AWS Lambda entry point.

    Invoked by EventBridge on the daily schedule and by an on-demand manual
    invoke. Delegates to :func:`run` with the real AWS/Groww shims. The event +
    context are unused (the schedule carries no payload; the manual invoke is a
    bare trigger). Returns the :class:`MintOutcome` JSON on success; a failure
    propagates so the Lambda invocation is marked ``Errors=1`` and the
    CloudWatch alarm fires.
    """
    return run().as_response()  # pragma: no cover - thin AWS entry point (real shims)


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_METRIC_NAMESPACE",
    "DEFAULT_RETRY_BACKOFF_SECONDS",
    "METRIC_NAME",
    "CloudWatchClient",
    "MintOutcome",
    "MinterConfig",
    "MinterConfigError",
    "SsmClient",
    "TokenMintError",
    "lambda_handler",
    "mint_token",
    "resolve_config",
    "run",
]
