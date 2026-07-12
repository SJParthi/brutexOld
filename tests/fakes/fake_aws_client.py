"""``FakeAwsClient`` — a deterministic, in-memory, network-free AWS SSM surface.

DEC-TOKEN-BROKER-AWS-001. The read-only token broker
(:mod:`brutex.adapters.groww.token_store_aws` + :mod:`brutex.config.credentials_aws`)
makes its ONE side-effecting AWS call — an SSM ``get_parameter`` read — through the
:class:`~brutex.adapters.groww.aws_client.AwsClient` Protocol. This fake satisfies that
Protocol so it flows through the REAL broker code — NOT a bypass — driving those modules to
100% line+branch with NO boto3 install and NO network (mirrors
``tests/fakes/fake_groww_sdk.py``).

It models:

* **Parameter Store** — a ``{name: value}`` map; a name in *param_not_found* raises a fake
  ``ParameterNotFound`` (matched by class name, no botocore import); ``fail_get_parameter``
  raises a generic access-denied error (the unreachable / IAM-denied permutation).

The design is single-writer / read-only for BruteX, so there is NO DynamoDB / KMS-encrypt
surface — the shared access token is minted by a dedicated Lambda and BruteX only READS a
SecureString (KMS-decrypted server-side by SSM; the fake stores plaintext).

Fully deterministic + thread-safe (a lock guards the read counter).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


class FakeParameterNotFound(Exception):
    """A fake SSM ``ParameterNotFound`` — matched by ``type(exc).__name__``.

    The class NAME must equal :data:`~brutex.adapters.groww.aws_client.SSM_PARAMETER_NOT_FOUND`
    so the broker's ``type(exc).__name__`` comparison classifies it as an absent
    parameter (a debug log) rather than a hard read failure (a warning).
    """


# The broker matches on ``type(exc).__name__`` — align the fake's class name exactly.
FakeParameterNotFound.__name__ = "ParameterNotFound"
FakeParameterNotFound.__qualname__ = "ParameterNotFound"


class FakeAwsError(Exception):
    """A generic fake AWS/network error (e.g. an access-denied / region-mismatch raise)."""


@dataclass(slots=True)
class FakeAwsClient:
    """A deterministic in-memory :class:`~brutex.adapters.groww.aws_client.AwsClient`.

    Parameters
    ----------
    parameters
        Seed Parameter-Store values (``{name: value}``).
    fail_get_parameter
        When ``True``, ``get_parameter`` raises :class:`FakeAwsError` — the unreachable /
        IAM-denied permutation.
    param_not_found
        Names for which ``get_parameter`` raises the fake ``ParameterNotFound`` (an absent
        parameter, distinct from a hard failure).
    """

    parameters: dict[str, str] = field(default_factory=dict)
    fail_get_parameter: bool = False
    param_not_found: frozenset[str] = field(default_factory=frozenset)
    #: When ``True``, ``put_parameter`` raises :class:`FakeAwsError` (the write-denied /
    #: unreachable permutation) — the WRITE half of the seam, used ONLY by the self-minting
    #: Dhan token store (DEC-DHAN-AUTH-001). Groww suites never touch this.
    fail_put_parameter: bool = False

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    get_parameter_calls: int = field(default=0, init=False)
    put_parameter_calls: int = field(default=0, init=False)
    #: Records every write (name, value, secure, overwrite) so a test can pin the exact
    #: ``PutParameter`` args (SecureString + Overwrite=True) without a real boto3 call.
    put_parameter_args: list[tuple[str, str, bool, bool]] = field(
        default_factory=list, init=False, repr=False
    )

    def get_parameter(self, *, name: str, with_decryption: bool) -> str | None:
        """Return the seeded value at *name*, raising for a failure / absent name."""
        _ = with_decryption  # the fake stores plaintext (SSM decrypts SecureStrings server-side)
        with self._lock:
            self.get_parameter_calls += 1
        if self.fail_get_parameter:
            raise FakeAwsError(f"access denied reading {name}")
        if name in self.param_not_found:
            raise FakeParameterNotFound(name)
        return self.parameters.get(name)

    def put_parameter(
        self, *, name: str, value: str, secure: bool = True, overwrite: bool = True
    ) -> None:
        """Write *value* at *name* (SecureString), raising on a simulated write failure.

        The WRITE half of the :class:`~brutex.adapters.aws_client.AwsClientWriter` Protocol —
        satisfied here so the Dhan token store flows through the REAL store code (not a
        bypass). Records the call + args and mutates the in-memory map so a subsequent
        ``get_parameter`` reads back what was written.
        """
        with self._lock:
            self.put_parameter_calls += 1
            self.put_parameter_args.append((name, value, secure, overwrite))
        if self.fail_put_parameter:
            raise FakeAwsError(f"access denied writing {name}")
        self.parameters[name] = value


__all__ = ["FakeAwsClient", "FakeAwsError", "FakeParameterNotFound"]
