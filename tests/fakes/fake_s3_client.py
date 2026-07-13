"""``FakeS3Client`` — a deterministic, in-memory, network-free S3 PUT surface.

DEC-CROSSVERIFY-S3-WRITE-POSTURE-001. The cross-verify publisher
(:mod:`brutex.crossverify.s3_publish`) makes its ONE side-effecting AWS call —
an S3 ``put_object`` — through the
:class:`~brutex.crossverify.s3_publish.S3PutClient` Protocol. This fake
satisfies that Protocol so it flows through the REAL publisher code — NOT a
bypass — driving the module to 100% line+branch with NO boto3 install and NO
network (mirrors ``tests/fakes/fake_aws_client.py``).

It models:

* **the object store** — ``{key: bytes}`` (last-writer-wins, exactly S3's
  per-key semantics);
* **transient throttling** — ``fail_times[key] = N`` makes the FIRST *N* puts
  of *key* raise a retryable ``SlowDown``-named error, then succeed (the
  bounded-retry ladder permutation);
* **a broken grant** — a key in ``nonretryable`` raises an
  ``AccessDenied``-named error (matched by class NAME — no botocore import),
  the whole-publish-abort permutation.

Fully deterministic + thread-safe (a lock guards the call log).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


class FakeS3SlowDown(Exception):
    """A fake retryable S3 throttling error — matched by ``type(exc).__name__``."""


# Retryability is classified by class NAME; align the fake exactly.
FakeS3SlowDown.__name__ = "SlowDown"
FakeS3SlowDown.__qualname__ = "SlowDown"


class FakeS3AccessDenied(Exception):
    """A fake NONRETRYABLE S3 error (broken/missing IAM grant)."""


FakeS3AccessDenied.__name__ = "AccessDenied"
FakeS3AccessDenied.__qualname__ = "AccessDenied"


@dataclass(slots=True)
class FakeS3Client:
    """A deterministic in-memory :class:`~brutex.crossverify.s3_publish.S3PutClient`.

    Parameters
    ----------
    objects
        The stored bodies (``{key: bytes}``) — mutated by successful puts.
    fail_times
        ``{key: N}`` — the first *N* puts of *key* raise a retryable
        ``SlowDown``; the counter decrements per raise.
    nonretryable
        Keys whose put always raises ``AccessDenied`` (whole-publish abort).
    """

    objects: dict[str, bytes] = field(default_factory=dict)
    fail_times: dict[str, int] = field(default_factory=dict)
    nonretryable: frozenset[str] = field(default_factory=frozenset)

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    #: Thread-safe call log of every put attempt: ``(bucket, key, content_type)``.
    puts: list[tuple[str, str, str]] = field(default_factory=list, init=False)

    def put_object(
        self, *, bucket: str, key: str, body: bytes, content_type: str = "text/csv; charset=utf-8"
    ) -> None:
        """Store *body* at *key*, honouring the configured failure permutations."""
        with self._lock:
            self.puts.append((bucket, key, content_type))
        if key in self.nonretryable:
            raise FakeS3AccessDenied(f"access denied putting {key}")
        remaining = self.fail_times.get(key, 0)
        if remaining > 0:
            self.fail_times[key] = remaining - 1
            raise FakeS3SlowDown(f"slow down putting {key}")
        self.objects[key] = bytes(body)


__all__ = ["FakeS3AccessDenied", "FakeS3Client", "FakeS3SlowDown"]
