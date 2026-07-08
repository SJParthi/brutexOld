"""``FakeS3Uploader`` — a deterministic, in-memory, network-free S3 write surface.

DEC-REFERENCE-EXPORT-S3-001. The reference export
(:mod:`brutex.export.reference_export`) makes its ONE side-effecting call — an
S3 ``put_object`` — through the
:class:`~brutex.export.reference_export.S3Uploader` Protocol. This fake satisfies
that Protocol so it flows through the REAL export code — NOT a bypass — driving
the module to 100% line+branch with NO boto3 install and NO network (the proven
``tests/fakes/fake_aws_client.py`` pattern).

It models ONE bucket as an ordered ``{key: (body, content_type)}`` map so a test
can assert exactly which objects were written, in what ORDER (proving the
manifest lands LAST), and read the stored bytes back (parse a Parquet, verify the
manifest JSON + per-file sha256). ``fail_on_key_substring`` makes a chosen
``put_object`` raise, exercising the fenced-failure path (and proving a failed
export leaves NO manifest).

Fully deterministic + thread-safe (a lock guards the writes) — mirrors
``FakeAwsClient``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


class FakeS3Error(Exception):
    """A generic fake S3 transport/permission error (e.g. an access-denied raise)."""


@dataclass(slots=True)
class FakeS3Uploader:
    """A deterministic in-memory :class:`~brutex.export.reference_export.S3Uploader`.

    Parameters
    ----------
    fail_on_key_substring
        When set, any ``put_object`` whose *key* contains this substring raises
        :class:`FakeS3Error` — the transport/permission failure permutation (used
        to prove the seam fences and the manifest is never written on failure).
    """

    fail_on_key_substring: str | None = None
    objects: dict[str, bytes] = field(default_factory=dict)
    content_types: dict[str, str] = field(default_factory=dict)
    put_keys: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def put_object(self, *, key: str, body: bytes, content_type: str) -> None:
        """Store *body* at *key*, recording the write order; may raise if configured."""
        if self.fail_on_key_substring is not None and self.fail_on_key_substring in key:
            raise FakeS3Error(f"fake S3 put_object denied for key={key!r}")
        with self._lock:
            self.objects[key] = body
            self.content_types[key] = content_type
            self.put_keys.append(key)
