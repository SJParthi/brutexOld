"""Unit tests for the centralized Groww token-minter Lambda handler.

DEC-TOKEN-MINTER-CENTRAL-001 (docs/MIGRATION.md #1060). These drive the pure
decision logic of :mod:`deploy.aws_token_minter.handler` to 100% line+branch
with a fake SSM client + fake CloudWatch client + fake TOTP/mint callables —
no ``boto3`` / ``growwapi`` / ``pyotp`` / AWS is touched. The real AWS + Groww
shims are ``# pragma: no cover`` (only runnable in AWS against the live vendor).
"""

# Whitebox tests deliberately exercise the handler's private helpers
# (``_read_secure_string`` / ``_sanitize`` / ``_is_rate_limit`` / ``_emit_metric``)
# — the established repo convention for a whitebox test file (DEC-P0-017).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from deploy.aws_token_minter import handler as h

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeSsm:
    """A minimal fake of the boto3 SSM client the handler uses.

    ``params`` seeds ``get_parameter`` responses (``{path: value}``); a path
    absent from it returns an empty ``Parameter`` (the misconfigured-param
    branch). ``put_parameter`` records the last write.
    """

    def __init__(
        self,
        params: Mapping[str, str] | None = None,
        *,
        put_errors: list[Exception] | None = None,
    ) -> None:
        self._params = dict(params or {})
        self.puts: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        # A queue of exceptions to raise on successive put_parameter calls (one
        # per call, popped in order); a shorter queue means later puts succeed.
        self._put_errors = list(put_errors or [])
        # How many times put_parameter was invoked (including the raising ones).
        self.put_attempts = 0

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> dict[str, Any]:
        self.get_calls.append({"Name": Name, "WithDecryption": WithDecryption})
        if Name in self._params:
            return {"Parameter": {"Value": self._params[Name]}}
        return {"Parameter": {}}

    def put_parameter(
        self,
        *,
        Name: str,
        Value: str,
        Type: str,
        Overwrite: bool,
        KeyId: str,
    ) -> dict[str, Any]:
        self.put_attempts += 1
        if self._put_errors:
            raise self._put_errors.pop(0)
        self.puts.append(
            {
                "Name": Name,
                "Value": Value,
                "Type": Type,
                "Overwrite": Overwrite,
                "KeyId": KeyId,
            }
        )
        return {"Version": len(self.puts)}


class FakeCloudWatch:
    """A fake CloudWatch client recording ``put_metric_data`` calls."""

    def __init__(self, *, raise_on_emit: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raise = raise_on_emit

    def put_metric_data(
        self, *, Namespace: str, MetricData: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        if self._raise:
            raise RuntimeError("cloudwatch unavailable")
        self.calls.append({"Namespace": Namespace, "MetricData": list(MetricData)})
        return {}


class _CodedError(Exception):
    """An exception carrying a ``code`` attribute (mimics the Groww SDK)."""

    def __init__(self, message: str, code: object) -> None:
        super().__init__(message)
        self.code = code


def _full_env(**overrides: str) -> dict[str, str]:
    """A fully-populated valid env, with optional per-key overrides."""
    env = {
        h.ENV_API_KEY_PARAM: "/tickvault/prod/groww/api-key",
        h.ENV_TOTP_SECRET_PARAM: "/tickvault/prod/groww/totp-secret",
        h.ENV_TOKEN_PARAM: "/tickvault/prod/groww/access-token",
        h.ENV_KMS_KEY_ID: "alias/tickvault-groww",
    }
    env.update(overrides)
    return env


def _seeded_ssm(env: Mapping[str, str]) -> FakeSsm:
    """A FakeSsm seeded so the two credential reads succeed."""
    return FakeSsm(
        {
            env[h.ENV_API_KEY_PARAM]: "the-api-key",
            env[h.ENV_TOTP_SECRET_PARAM]: "the-totp-secret",
        }
    )


# --------------------------------------------------------------------------- #
# resolve_config
# --------------------------------------------------------------------------- #


class TestResolveConfig:
    def test_defaults_when_optional_unset(self) -> None:
        cfg = h.resolve_config(_full_env())
        assert cfg.api_key_param == "/tickvault/prod/groww/api-key"
        assert cfg.token_param == "/tickvault/prod/groww/access-token"
        assert cfg.kms_key_id == "alias/tickvault-groww"
        assert cfg.metric_namespace == h.DEFAULT_METRIC_NAMESPACE
        assert cfg.max_retries == h.DEFAULT_MAX_RETRIES
        assert cfg.retry_backoff_seconds == h.DEFAULT_RETRY_BACKOFF_SECONDS

    def test_explicit_overrides(self) -> None:
        cfg = h.resolve_config(
            _full_env(
                **{
                    h.ENV_METRIC_NAMESPACE: "Custom/NS",
                    h.ENV_MAX_RETRIES: "5",
                    h.ENV_RETRY_BACKOFF_SECONDS: "0.5",
                }
            )
        )
        assert cfg.metric_namespace == "Custom/NS"
        assert cfg.max_retries == 5
        assert cfg.retry_backoff_seconds == 0.5

    def test_blank_namespace_falls_back_to_default(self) -> None:
        cfg = h.resolve_config(_full_env(**{h.ENV_METRIC_NAMESPACE: "   "}))
        assert cfg.metric_namespace == h.DEFAULT_METRIC_NAMESPACE

    @pytest.mark.parametrize(
        "missing",
        [
            h.ENV_API_KEY_PARAM,
            h.ENV_TOTP_SECRET_PARAM,
            h.ENV_TOKEN_PARAM,
            h.ENV_KMS_KEY_ID,
        ],
    )
    def test_missing_required_raises(self, missing: str) -> None:
        env = _full_env()
        env[missing] = "   "  # blank counts as missing
        with pytest.raises(h.MinterConfigError, match=missing):
            h.resolve_config(env)

    def test_uses_os_environ_when_env_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key, value in _full_env().items():
            monkeypatch.setenv(key, value)
        cfg = h.resolve_config(None)
        assert cfg.token_param == "/tickvault/prod/groww/access-token"

    def test_max_retries_not_integer_raises(self) -> None:
        with pytest.raises(h.MinterConfigError, match="not an integer"):
            h.resolve_config(_full_env(**{h.ENV_MAX_RETRIES: "abc"}))

    def test_max_retries_negative_raises(self) -> None:
        with pytest.raises(h.MinterConfigError, match=">= 0"):
            h.resolve_config(_full_env(**{h.ENV_MAX_RETRIES: "-1"}))

    def test_backoff_not_a_number_raises(self) -> None:
        with pytest.raises(h.MinterConfigError, match="not a number"):
            h.resolve_config(_full_env(**{h.ENV_RETRY_BACKOFF_SECONDS: "x"}))

    def test_backoff_negative_raises(self) -> None:
        with pytest.raises(h.MinterConfigError, match=">= 0"):
            h.resolve_config(_full_env(**{h.ENV_RETRY_BACKOFF_SECONDS: "-0.1"}))

    def test_max_retries_zero_allowed(self) -> None:
        cfg = h.resolve_config(_full_env(**{h.ENV_MAX_RETRIES: "0"}))
        assert cfg.max_retries == 0


# --------------------------------------------------------------------------- #
# _read_secure_string
# --------------------------------------------------------------------------- #


class TestReadSecureString:
    def test_reads_decrypted_value(self) -> None:
        ssm = FakeSsm({"/p": "value"})
        assert h._read_secure_string(ssm, "/p") == "value"
        assert ssm.get_calls == [{"Name": "/p", "WithDecryption": True}]

    def test_missing_value_raises(self) -> None:
        ssm = FakeSsm({})  # /p absent -> empty Parameter
        with pytest.raises(h.MinterConfigError, match="no value"):
            h._read_secure_string(ssm, "/p")

    def test_blank_value_raises(self) -> None:
        ssm = FakeSsm({"/p": "   "})
        with pytest.raises(h.MinterConfigError, match="no value"):
            h._read_secure_string(ssm, "/p")


# --------------------------------------------------------------------------- #
# _sanitize
# --------------------------------------------------------------------------- #


class TestSanitize:
    def test_scrubs_secrets_longest_first(self) -> None:
        # "abcdef" contains "abc"; longest-first must fully mask.
        out = h._sanitize("error abcdef and abc", "abc", "abcdef")
        assert "abcdef" not in out
        assert out.count("***") == 2

    def test_ignores_blank_secret(self) -> None:
        out = h._sanitize("hello", "")
        assert out == "hello"

    def test_no_secrets_is_identity(self) -> None:
        assert h._sanitize("plain message") == "plain message"


# --------------------------------------------------------------------------- #
# _is_rate_limit
# --------------------------------------------------------------------------- #


class TestIsRateLimit:
    def test_int_code_429(self) -> None:
        assert h._is_rate_limit(_CodedError("boom", 429)) is True

    def test_str_code_429(self) -> None:
        assert h._is_rate_limit(_CodedError("boom", "429")) is True

    def test_bool_code_rejected(self) -> None:
        # True is an int subclass == 1, must NOT be read as a 429 status.
        assert h._is_rate_limit(_CodedError("boom", True)) is False

    def test_phrase_rate_limit(self) -> None:
        assert h._is_rate_limit(RuntimeError("Rate limit exceeded")) is True

    def test_phrase_too_many_requests(self) -> None:
        assert h._is_rate_limit(RuntimeError("Too many requests, slow down")) is True

    def test_other_code_not_rate_limit(self) -> None:
        assert h._is_rate_limit(_CodedError("bad totp", 400)) is False

    def test_terminal_message_not_rate_limit(self) -> None:
        assert h._is_rate_limit(RuntimeError("invalid TOTP")) is False


# --------------------------------------------------------------------------- #
# _is_ssm_throttle (MINOR-2 support)
# --------------------------------------------------------------------------- #


class TestIsSsmThrottle:
    def test_throttling_exception_phrase(self) -> None:
        # A botocore ClientError on a throttled PutParameter stringifies like this.
        exc = RuntimeError(
            "An error occurred (ThrottlingException) when calling the "
            "PutParameter operation: Rate exceeded"
        )
        assert h._is_ssm_throttle(exc) is True

    def test_rate_exceeded_phrase(self) -> None:
        assert h._is_ssm_throttle(RuntimeError("Rate exceeded")) is True

    def test_non_throttle_is_false(self) -> None:
        # AccessDenied / KMS errors / genuine bugs are terminal, not retried.
        assert h._is_ssm_throttle(RuntimeError("AccessDeniedException")) is False
        assert h._is_ssm_throttle(RuntimeError("KMSInvalidStateException")) is False


# --------------------------------------------------------------------------- #
# mint_token
# --------------------------------------------------------------------------- #


class TestMintToken:
    def test_happy_path_writes_token(self) -> None:
        env = _full_env()
        cfg = h.resolve_config(env)
        ssm = _seeded_ssm(env)
        attempts = h.mint_token(
            cfg,
            ssm,
            totp_minter=lambda secret: "123456",
            token_factory=lambda api_key, totp: "  fresh-token  ",
        )
        assert attempts == 1
        assert len(ssm.puts) == 1
        put = ssm.puts[0]
        assert put["Name"] == cfg.token_param
        assert put["Value"] == "fresh-token"  # stripped
        assert put["Type"] == "SecureString"
        assert put["Overwrite"] is True
        assert put["KeyId"] == cfg.kms_key_id

    def test_reads_both_credentials(self) -> None:
        env = _full_env()
        cfg = h.resolve_config(env)
        ssm = _seeded_ssm(env)
        h.mint_token(
            cfg,
            ssm,
            totp_minter=lambda secret: "111111",
            token_factory=lambda api_key, totp: "tok",
        )
        read_names = [c["Name"] for c in ssm.get_calls]
        assert cfg.api_key_param in read_names
        assert cfg.totp_secret_param in read_names

    def test_empty_token_raises_and_does_not_write(self) -> None:
        env = _full_env()
        cfg = h.resolve_config(env)
        ssm = _seeded_ssm(env)
        with pytest.raises(h.TokenMintError, match="empty token"):
            h.mint_token(
                cfg,
                ssm,
                totp_minter=lambda secret: "222222",
                token_factory=lambda api_key, totp: "   ",
            )
        assert ssm.puts == []

    def test_terminal_error_raises_sanitized(self) -> None:
        env = _full_env()
        cfg = h.resolve_config(env)
        ssm = _seeded_ssm(env)

        def _bad(api_key: str, totp: str) -> str:
            raise RuntimeError(f"auth failed for key {api_key} totp {totp}")

        with pytest.raises(h.TokenMintError) as excinfo:
            h.mint_token(
                cfg,
                ssm,
                totp_minter=lambda secret: "333333",
                token_factory=_bad,
            )
        message = str(excinfo.value)
        assert "the-api-key" not in message  # api key scrubbed
        assert "333333" not in message  # totp code scrubbed
        assert "***" in message
        assert ssm.puts == []

    def test_retry_then_success(self) -> None:
        env = _full_env(**{h.ENV_MAX_RETRIES: "2", h.ENV_RETRY_BACKOFF_SECONDS: "1"})
        cfg = h.resolve_config(env)
        ssm = _seeded_ssm(env)
        calls = {"n": 0}
        slept: list[float] = []

        def _factory(api_key: str, totp: str) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise _CodedError("Rate limit exceeded", 429)
            return "recovered-token"

        attempts = h.mint_token(
            cfg,
            ssm,
            totp_minter=lambda secret: "444444",
            token_factory=_factory,
            sleep=slept.append,
        )
        assert attempts == 2
        assert slept == [1.0 * (2**0)]  # backoff on first retry
        assert ssm.puts[0]["Value"] == "recovered-token"

    def test_retry_budget_exhausted_raises(self) -> None:
        env = _full_env(**{h.ENV_MAX_RETRIES: "1", h.ENV_RETRY_BACKOFF_SECONDS: "0"})
        cfg = h.resolve_config(env)
        ssm = _seeded_ssm(env)
        slept: list[float] = []

        def _always_throttle(api_key: str, totp: str) -> str:
            raise _CodedError("Too many requests", 429)

        with pytest.raises(h.TokenMintError, match="issuance failed"):
            h.mint_token(
                cfg,
                ssm,
                totp_minter=lambda secret: "555555",
                token_factory=_always_throttle,
                sleep=slept.append,
            )
        assert slept == [0.0]  # one retry backoff (attempt 0), then give up
        assert ssm.puts == []

    def test_zero_retries_single_attempt(self) -> None:
        env = _full_env(**{h.ENV_MAX_RETRIES: "0"})
        cfg = h.resolve_config(env)
        ssm = _seeded_ssm(env)
        slept: list[float] = []

        def _throttle_once(api_key: str, totp: str) -> str:
            raise _CodedError("Rate limit", 429)

        with pytest.raises(h.TokenMintError):
            h.mint_token(
                cfg,
                ssm,
                totp_minter=lambda secret: "666666",
                token_factory=_throttle_once,
                sleep=slept.append,
            )
        assert slept == []  # no retry attempted (budget 0)

    def test_terminal_error_breaks_cause_chain(self) -> None:
        # MAJOR-1 regression: the terminal TokenMintError must NOT chain the raw
        # vendor exception (which may echo the api-key/TOTP) as __cause__ — else the
        # Lambda runtime logs the full chained traceback to CloudWatch, leaking the
        # secret. `raise ... from None` breaks the chain. FAILS-BEFORE the fix (the
        # earlier `from exc` left __cause__ set to the leaking raw exception).
        env = _full_env()
        cfg = h.resolve_config(env)
        ssm = _seeded_ssm(env)

        def _bad(api_key: str, totp: str) -> str:
            # The raw SDK error echoes BOTH secrets — exactly the leak we prevent.
            raise RuntimeError(f"vendor error: key={api_key} totp={totp}")

        with pytest.raises(h.TokenMintError) as excinfo:
            h.mint_token(
                cfg,
                ssm,
                totp_minter=lambda secret: "424242",
                token_factory=_bad,
            )
        # The __cause__ chain is severed — the raw leaking exception is unreachable
        # via ``from None`` (which also sets __suppress_context__).
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True
        # The sanitized message contains neither the api-key nor the TOTP code, and
        # since the chain is severed there is no traceback carrying the raw values.
        message = str(excinfo.value)
        assert "the-api-key" not in message
        assert "424242" not in message
        assert "***" in message

    def test_malformed_totp_secret_error_is_sanitized(self) -> None:
        # MINOR-3 regression: a malformed TOTP secret makes the totp_minter itself
        # raise (from pyotp), and that error can echo the secret. It is computed
        # INSIDE the guarded try now, so it is sanitized like any mint error and
        # never raised raw. FAILS-BEFORE (totp was computed OUTSIDE the try, so the
        # raw secret-echoing error propagated).
        env = _full_env()
        cfg = h.resolve_config(env)
        ssm = _seeded_ssm(env)

        def _bad_totp(secret: str) -> str:
            raise ValueError(f"invalid base32 secret: {secret}")

        with pytest.raises(h.TokenMintError) as excinfo:
            h.mint_token(
                cfg,
                ssm,
                totp_minter=_bad_totp,
                token_factory=lambda api_key, totp: "unused",
            )
        message = str(excinfo.value)
        assert "the-totp-secret" not in message  # totp secret scrubbed
        assert "***" in message
        assert excinfo.value.__cause__ is None  # chain broken (no secret leak)
        assert ssm.puts == []

    def test_put_token_throttle_retries_then_succeeds(self) -> None:
        # MINOR-2: a transient SSM PutParameter throttle is retried on the same
        # budget so a freshly-minted token is not lost. FAILS-BEFORE (the put had no
        # retry: the first throttle propagated and the token was never written).
        env = _full_env(**{h.ENV_MAX_RETRIES: "2", h.ENV_RETRY_BACKOFF_SECONDS: "1"})
        cfg = h.resolve_config(env)
        ssm = FakeSsm(
            {
                env[h.ENV_API_KEY_PARAM]: "the-api-key",
                env[h.ENV_TOTP_SECRET_PARAM]: "the-totp-secret",
            },
            put_errors=[RuntimeError("An error occurred (ThrottlingException): Rate exceeded")],
        )
        slept: list[float] = []
        attempts = h.mint_token(
            cfg,
            ssm,
            totp_minter=lambda secret: "121212",
            token_factory=lambda api_key, totp: "put-token",
            sleep=slept.append,
        )
        assert attempts == 1  # the MINT succeeded first try; only the WRITE retried
        assert ssm.put_attempts == 2  # throttled once, then wrote
        assert slept == [1.0 * (2**0)]  # one put-retry backoff
        assert ssm.puts[0]["Value"] == "put-token"

    def test_put_token_throttle_budget_exhausted_raises(self) -> None:
        # A persistent SSM write throttle exhausts the budget and propagates (caught
        # by run(), which emits the failure metric).
        env = _full_env(**{h.ENV_MAX_RETRIES: "1", h.ENV_RETRY_BACKOFF_SECONDS: "0"})
        cfg = h.resolve_config(env)
        ssm = FakeSsm(
            {
                env[h.ENV_API_KEY_PARAM]: "the-api-key",
                env[h.ENV_TOTP_SECRET_PARAM]: "the-totp-secret",
            },
            put_errors=[
                RuntimeError("ThrottlingException: Rate exceeded"),
                RuntimeError("ThrottlingException: Rate exceeded"),
            ],
        )
        slept: list[float] = []
        with pytest.raises(RuntimeError, match="Rate exceeded"):
            h.mint_token(
                cfg,
                ssm,
                totp_minter=lambda secret: "131313",
                token_factory=lambda api_key, totp: "put-token",
                sleep=slept.append,
            )
        assert ssm.put_attempts == 2  # first try + one retry, then give up
        assert slept == [0.0]  # one put-retry backoff
        assert ssm.puts == []  # never written

    def test_put_token_non_throttle_error_raises_immediately(self) -> None:
        # A non-throttle SSM error (e.g. AccessDenied) is terminal — no retry.
        env = _full_env(**{h.ENV_MAX_RETRIES: "3"})
        cfg = h.resolve_config(env)
        ssm = FakeSsm(
            {
                env[h.ENV_API_KEY_PARAM]: "the-api-key",
                env[h.ENV_TOTP_SECRET_PARAM]: "the-totp-secret",
            },
            put_errors=[RuntimeError("AccessDeniedException: not authorized")],
        )
        slept: list[float] = []
        with pytest.raises(RuntimeError, match="AccessDenied"):
            h.mint_token(
                cfg,
                ssm,
                totp_minter=lambda secret: "141414",
                token_factory=lambda api_key, totp: "put-token",
                sleep=slept.append,
            )
        assert ssm.put_attempts == 1  # NOT retried
        assert slept == []


# --------------------------------------------------------------------------- #
# _emit_metric
# --------------------------------------------------------------------------- #


class TestEmitMetric:
    def test_emits_success(self) -> None:
        cw = FakeCloudWatch()
        h._emit_metric(cw, "NS", success=True)
        assert cw.calls[0]["Namespace"] == "NS"
        datum = cw.calls[0]["MetricData"][0]
        assert datum["MetricName"] == h.METRIC_NAME
        assert datum["Value"] == 1.0

    def test_emits_failure(self) -> None:
        cw = FakeCloudWatch()
        h._emit_metric(cw, "NS", success=False)
        assert cw.calls[0]["MetricData"][0]["Value"] == 0.0

    def test_emit_error_is_swallowed(self) -> None:
        cw = FakeCloudWatch(raise_on_emit=True)
        # Must not raise.
        h._emit_metric(cw, "NS", success=True)
        assert cw.calls == []


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


class TestRun:
    def test_success_emits_success_metric(self) -> None:
        env = _full_env()
        ssm = _seeded_ssm(env)
        cw = FakeCloudWatch()
        outcome = h.run(
            env,
            ssm_factory=lambda: ssm,
            cloudwatch_factory=lambda: cw,
            totp_minter=lambda secret: "777777",
            token_factory=lambda api_key, totp: "run-token",
        )
        assert outcome.ok is True
        assert outcome.attempts == 1
        assert outcome.token_param == env[h.ENV_TOKEN_PARAM]
        assert outcome.as_response() == {
            "ok": True,
            "attempts": 1,
            "token_param": env[h.ENV_TOKEN_PARAM],
        }
        assert cw.calls[0]["MetricData"][0]["Value"] == 1.0
        assert ssm.puts[0]["Value"] == "run-token"

    def test_mint_failure_emits_failure_metric_and_reraises(self) -> None:
        env = _full_env()
        ssm = _seeded_ssm(env)
        cw = FakeCloudWatch()

        def _bad(api_key: str, totp: str) -> str:
            raise RuntimeError("terminal auth failure")

        with pytest.raises(h.TokenMintError):
            h.run(
                env,
                ssm_factory=lambda: ssm,
                cloudwatch_factory=lambda: cw,
                totp_minter=lambda secret: "888888",
                token_factory=_bad,
            )
        assert cw.calls[0]["MetricData"][0]["Value"] == 0.0

    def test_config_error_reraises_without_metric(self) -> None:
        env = _full_env()
        env[h.ENV_KMS_KEY_ID] = ""  # invalid config
        ssm = FakeSsm()
        cw = FakeCloudWatch()
        with pytest.raises(h.MinterConfigError):
            h.run(
                env,
                ssm_factory=lambda: ssm,
                cloudwatch_factory=lambda: cw,
                totp_minter=lambda secret: "999999",
                token_factory=lambda api_key, totp: "unused",
            )
        # resolve_config runs BEFORE the try/except (and before any client is
        # even used meaningfully), so a pure config error emits no metric —
        # the CloudWatch missing-datum alarm still catches a wholly-dead Lambda.
        assert cw.calls == []

    def test_credential_read_failure_emits_failure_metric(self) -> None:
        # Config is valid, but the SSM param has no value -> MinterConfigError
        # raised INSIDE mint_token, so the failure metric IS emitted.
        env = _full_env()
        ssm = FakeSsm()  # no seeded params -> credential read fails
        cw = FakeCloudWatch()
        with pytest.raises(h.MinterConfigError, match="no value"):
            h.run(
                env,
                ssm_factory=lambda: ssm,
                cloudwatch_factory=lambda: cw,
                totp_minter=lambda secret: "101010",
                token_factory=lambda api_key, totp: "unused",
            )
        assert cw.calls[0]["MetricData"][0]["Value"] == 0.0

    def test_non_domain_ssm_error_still_emits_failure_metric(self) -> None:
        # MINOR-1: a NON-domain error raised out of mint_token — e.g. a raw
        # botocore SSM/KMS ClientError on a throttled GetParameter, which is neither
        # MinterConfigError nor TokenMintError — must STILL emit success=0 before
        # propagating, so the stale-token alarm's success=0 signal is reliable.
        # FAILS-BEFORE (the except only caught the two domain errors, so a raw
        # ClientError propagated with NO failure metric).
        class _RaisingSsm(FakeSsm):
            def get_parameter(self, *, Name: str, WithDecryption: bool) -> dict[str, Any]:
                raise RuntimeError("botocore ClientError: ThrottlingException")

        env = _full_env()
        ssm = _RaisingSsm()
        cw = FakeCloudWatch()
        with pytest.raises(RuntimeError, match="ThrottlingException"):
            h.run(
                env,
                ssm_factory=lambda: ssm,
                cloudwatch_factory=lambda: cw,
                totp_minter=lambda secret: "202020",
                token_factory=lambda api_key, totp: "unused",
            )
        assert cw.calls[0]["MetricData"][0]["Value"] == 0.0

    def test_put_throttle_recovery_emits_success_metric(self) -> None:
        # End-to-end via run(): a transient SSM PutParameter throttle that recovers
        # yields a SUCCESS (success=1) — the token was written after the retry.
        env = _full_env(**{h.ENV_MAX_RETRIES: "2", h.ENV_RETRY_BACKOFF_SECONDS: "0"})
        ssm = FakeSsm(
            {
                env[h.ENV_API_KEY_PARAM]: "the-api-key",
                env[h.ENV_TOTP_SECRET_PARAM]: "the-totp-secret",
            },
            put_errors=[RuntimeError("ThrottlingException: Rate exceeded")],
        )
        cw = FakeCloudWatch()
        outcome = h.run(
            env,
            ssm_factory=lambda: ssm,
            cloudwatch_factory=lambda: cw,
            totp_minter=lambda secret: "303030",
            token_factory=lambda api_key, totp: "recovered-put",
            sleep=lambda _s: None,
        )
        assert outcome.ok is True
        assert cw.calls[0]["MetricData"][0]["Value"] == 1.0
        assert ssm.puts[0]["Value"] == "recovered-put"


# --------------------------------------------------------------------------- #
# module surface
# --------------------------------------------------------------------------- #


def test_all_exports_present() -> None:
    for name in h.__all__:
        assert hasattr(h, name), name
