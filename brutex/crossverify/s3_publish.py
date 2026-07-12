"""S3 publisher — PUT-only Protocol seam + retrying idempotent publish.

DEC-CROSSVERIFY-S3-WRITE-POSTURE-001: BruteX's FIRST sanctioned AWS WRITE
surface — ``s3:PutObject`` on ``arn:aws:s3:::tv-prod-cold/crossverify/*``
ONLY (operator-provisioned IAM; NO Get/Head/List/Delete, so the publisher
NEVER reads remote state — idempotency is journal-side, see
:mod:`brutex.crossverify.journal`). The seam mirrors
:mod:`brutex.adapters.groww.aws_client` exactly: a dependency-free
:class:`S3PutClient` Protocol, a lazy ``# pragma: no cover`` boto3 factory
(boto3 is the optional ``crossverify`` extra — CI never installs it), and
error classification by exception CLASS-NAME string — never a botocore
import, so the whole package stays importable with boto3 absent.

Retry law: per key ``max_attempts`` with the injected backoff/sleeper; a
NONRETRYABLE error (``AccessDenied`` / ``NoSuchBucket`` class) aborts the
WHOLE publish immediately — never a per-key retry storm against a broken
grant. Crash-resume granularity: the journal is saved every
:data:`JOURNAL_SAVE_EVERY` published keys. O(pending keys) PUTs per run.

S3 PUTs are atomic per key with last-writer-wins semantics (AWS S3 docs,
"Amazon S3 data consistency model") — a crash between put and journal-save
re-puts identical bytes on the next run, which converges.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

import structlog

from brutex.crossverify.journal import staging_path_for_key
from brutex.models import BruteXError

if TYPE_CHECKING:  # pragma: no cover — typing-only imports
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from brutex.crossverify.journal import DayJournal

_LOG = structlog.get_logger("brutex.crossverify.s3_publish")

#: The published object content type (pinned — TickVault reads text/csv).
CSV_CONTENT_TYPE: Final[str] = "text/csv; charset=utf-8"

#: Journal save cadence during publish (crash-resume granularity).
JOURNAL_SAVE_EVERY: Final[int] = 25

#: Failed-key report cap (the true count always rides ``failed_count``).
FAILED_KEYS_CAP: Final[int] = 10

#: Exception CLASS NAMES that abort the WHOLE publish (a broken grant /
#: wrong bucket is never a per-key retry storm). Matched by
#: ``type(exc).__name__`` — never a botocore import (the ``aws_client.py``
#: / ``credentials_aws.py`` house pattern). Everything else is retried up
#: to the bounded per-key attempt budget (safe: transient throttle /
#: timeout / 5xx shapes — Throttling / SlowDown / RequestTimeout /
#: InternalError / connection errors — all retry).
NONRETRYABLE_S3_ERROR_NAMES: Final[frozenset[str]] = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "NoSuchBucket",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "ExpiredToken",
    }
)


class S3BackendUnavailable(BruteXError):
    """boto3 is not installed — the S3 publisher cannot be built."""


class S3PutClient(Protocol):
    """The ONLY S3 surface — PUT-only (matches the PutObject-only IAM grant).

    NO get/head/list/delete — idempotency is journal-side. ``put_object``
    raises on any error; retryability is classified by the exception's class
    name (:data:`NONRETRYABLE_S3_ERROR_NAMES`).
    """

    def put_object(
        self, *, bucket: str, key: str, body: bytes, content_type: str = CSV_CONTENT_TYPE
    ) -> None:
        """PUT *body* at ``s3://<bucket>/<key>``; raise on any error."""
        ...


def is_nonretryable_s3_error(exc: BaseException) -> bool:
    """``True`` iff *exc*'s class name is a whole-publish-abort error."""
    return type(exc).__name__ in NONRETRYABLE_S3_ERROR_NAMES


def build_real_s3_client(region: str) -> S3PutClient:  # pragma: no cover — needs boto3 + AWS
    """Build the production :class:`S3PutClient` over boto3 (lazy import).

    boto3 rides its default credential chain (env / shared config / instance
    profile — AWS boto3 docs "Credentials"); only *region* is pinned. A
    missing boto3 raises :class:`S3BackendUnavailable` naming the extra.
    """
    try:
        import boto3  # type: ignore[import-not-found, unused-ignore]
    except ImportError as exc:
        raise S3BackendUnavailable(
            "boto3 is required to publish cross-verify CSVs to S3; install the "
            'extra: pip install -e ".[crossverify]"'
        ) from exc

    s3 = boto3.client("s3", region_name=region)
    return _Boto3S3Client(s3=s3)


class _Boto3S3Client:  # pragma: no cover — needs boto3 + AWS
    """Adapts the boto3 low-level S3 client to :class:`S3PutClient`.

    A thin translation layer only (the ``_Boto3AwsClient`` shape): maps the
    Protocol kwargs to boto3's ``PutObject`` request. No caching, no retry —
    the bounded retry ladder lives in :func:`publish_staged`.
    """

    def __init__(self, *, s3: object) -> None:
        self._s3 = s3

    def put_object(
        self, *, bucket: str, key: str, body: bytes, content_type: str = CSV_CONTENT_TYPE
    ) -> None:
        """PUT one object via boto3 (raw ``&`` in *key* — boto3 transport-encodes)."""
        self._s3.put_object(  # type: ignore[attr-defined]
            Bucket=bucket, Key=key, Body=body, ContentType=content_type
        )


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """The outcome of one :func:`publish_staged` pass."""

    attempted: int
    published: int
    skipped_already: int
    failed_count: int
    failed_keys: tuple[str, ...]  # capped at FAILED_KEYS_CAP; failed_count is the truth
    aborted_nonretryable: bool


def _put_with_retry(
    *,
    client: S3PutClient,
    bucket: str,
    key: str,
    body: bytes,
    max_attempts: int,
    backoff_s: Sequence[float],
    sleeper: Callable[[float], None],
) -> BaseException | None:
    """PUT one key with bounded retry; return the final error (``None`` = success)."""
    last_error: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            client.put_object(bucket=bucket, key=key, body=body)
        except Exception as exc:  # classified below — never a bare except
            last_error = exc
            if is_nonretryable_s3_error(exc):
                return exc
            if attempt + 1 < max_attempts:
                delay = backoff_s[min(attempt, len(backoff_s) - 1)] if backoff_s else 0.0
                _LOG.warning(
                    "crossverify.s3_put_retry",
                    key=key,
                    attempt=attempt + 1,
                    error_type=type(exc).__name__,
                    delay_s=delay,
                )
                sleeper(delay)
        else:
            return None
    return last_error


def publish_staged(
    *,
    client: S3PutClient,
    bucket: str,
    journal: DayJournal,
    lake_root: Path,
    max_attempts: int = 3,
    backoff_s: Sequence[float] = (1.0, 4.0),
    sleeper: Callable[[float], None] = time.sleep,
) -> PublishOutcome:
    """PUT every journal-pending staged object; journal-save every 25 successes.

    Publish-then-journal ordering: ``mark_published`` lands strictly AFTER a
    successful PUT (a crash in between re-puts identical bytes next run —
    convergent, per-key S3 PUT atomicity). A missing staged file counts
    failed (+ WARN, stays pending). A nonretryable error aborts the WHOLE
    publish immediately with the remaining pending keys counted failed.
    O(pending keys).
    """
    pending = journal.pending_keys()
    skipped_already = len(journal.entries) - len(pending)
    published = 0
    attempted = 0
    failed: list[str] = []
    aborted = False
    dirty = False
    for index, key in enumerate(pending):
        staged = staging_path_for_key(lake_root, journal.day, key)
        try:
            body = staged.read_bytes()
        except OSError as exc:
            _LOG.warning(
                "crossverify.staged_bytes_missing",
                key=key,
                path=str(staged),
                error_type=type(exc).__name__,
            )
            failed.append(key)
            continue
        attempted += 1
        error = _put_with_retry(
            client=client,
            bucket=bucket,
            key=key,
            body=body,
            max_attempts=max_attempts,
            backoff_s=backoff_s,
            sleeper=sleeper,
        )
        if error is None:
            journal.mark_published(key)
            published += 1
            dirty = True
            if published % JOURNAL_SAVE_EVERY == 0:
                journal.save(lake_root)
                dirty = False
            continue
        failed.append(key)
        if is_nonretryable_s3_error(error):
            aborted = True
            failed.extend(pending[index + 1 :])
            _LOG.error(
                "crossverify.s3_publish_aborted",
                key=key,
                error_type=type(error).__name__,
                error=str(error),
                remediation=(
                    "provision the IAM grant s3:PutObject on "
                    "arn:aws:s3:::<bucket>/crossverify/* (runbook: crossverify-producer)"
                ),
            )
            break
        _LOG.error(
            "crossverify.s3_put_failed",
            key=key,
            error_type=type(error).__name__,
            error=str(error),
        )
    if dirty:
        journal.save(lake_root)
    return PublishOutcome(
        attempted=attempted,
        published=published,
        skipped_already=skipped_already,
        failed_count=len(failed),
        failed_keys=tuple(failed[:FAILED_KEYS_CAP]),
        aborted_nonretryable=aborted,
    )


__all__ = [
    "CSV_CONTENT_TYPE",
    "FAILED_KEYS_CAP",
    "JOURNAL_SAVE_EVERY",
    "NONRETRYABLE_S3_ERROR_NAMES",
    "PublishOutcome",
    "S3BackendUnavailable",
    "S3PutClient",
    "build_real_s3_client",
    "is_nonretryable_s3_error",
    "publish_staged",
]
