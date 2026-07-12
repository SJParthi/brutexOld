"""Unit tests — :mod:`brutex.crossverify.s3_publish` (PUT-only publisher)."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest
from brutex.crossverify.journal import DayJournal, journal_path, load_journal, staging_path_for_key
from brutex.crossverify.s3_publish import (
    CSV_CONTENT_TYPE,
    FAILED_KEYS_CAP,
    JOURNAL_SAVE_EVERY,
    NONRETRYABLE_S3_ERROR_NAMES,
    S3BackendUnavailable,
    S3PutClient,
    build_real_s3_client,
    is_nonretryable_s3_error,
    publish_staged,
)

from tests.fakes.fake_s3_client import FakeS3AccessDenied, FakeS3Client, FakeS3SlowDown

if TYPE_CHECKING:
    from pathlib import Path

_DAY = date(2026, 7, 10)
_BUCKET = "tv-prod-cold"


def _stage(lake: Path, journal: DayJournal, key: str, body: bytes) -> None:
    dest = staging_path_for_key(lake, journal.day, key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    import hashlib

    journal.mark_staged(key, hashlib.sha256(body).hexdigest(), len(body))


def _journal() -> DayJournal:
    return DayJournal(day=_DAY, run_id="cv-test")


class TestClassification:
    def test_nonretryable_names(self) -> None:
        assert "AccessDenied" in NONRETRYABLE_S3_ERROR_NAMES
        assert "NoSuchBucket" in NONRETRYABLE_S3_ERROR_NAMES
        # Round-4 polish P3: a creds-absent box (botocore NoCredentialsError)
        # is a broken-grant shape — whole-publish abort, never a retry storm.
        assert "NoCredentialsError" in NONRETRYABLE_S3_ERROR_NAMES
        assert is_nonretryable_s3_error(FakeS3AccessDenied("x")) is True
        assert is_nonretryable_s3_error(FakeS3SlowDown("x")) is False
        assert is_nonretryable_s3_error(TimeoutError("x")) is False

    def test_fake_conforms_to_protocol(self) -> None:
        client: S3PutClient = FakeS3Client()
        client.put_object(bucket=_BUCKET, key="k", body=b"x")
        assert client.objects["k"] == b"x"


class TestBuildRealClient:
    def test_missing_boto3_raises_backend_unavailable(self) -> None:
        # boto3 is deliberately NOT installed (the optional `crossverify`
        # extra, never in CI); the factory must name it loudly.
        import importlib.util

        if importlib.util.find_spec("boto3") is not None:  # pragma: no cover
            pytest.skip("boto3 installed — the missing-extra arm is not testable here")
        with pytest.raises(S3BackendUnavailable, match="crossverify"):
            build_real_s3_client("ap-south-1")


class TestPublishStaged:
    def test_happy_path_publishes_and_journals(self, tmp_path: Path) -> None:
        journal = _journal()
        _stage(tmp_path, journal, "p/IDX_I/NIFTY.csv", b"a\n")
        _stage(tmp_path, journal, "p/NSE_EQ/M&M.csv", b"b\n")
        client = FakeS3Client()
        outcome = publish_staged(client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert (outcome.attempted, outcome.published, outcome.failed_count) == (2, 2, 0)
        assert outcome.skipped_already == 0
        assert outcome.aborted_nonretryable is False
        assert client.objects == {"p/IDX_I/NIFTY.csv": b"a\n", "p/NSE_EQ/M&M.csv": b"b\n"}
        # Journal persisted with both entries published (the final dirty save).
        loaded = load_journal(tmp_path, _DAY)
        assert loaded is not None
        assert loaded.pending_keys() == ()

    def test_content_type_pinned(self, tmp_path: Path) -> None:
        journal = _journal()
        _stage(tmp_path, journal, "p/IDX_I/NIFTY.csv", b"a\n")
        client = FakeS3Client()
        publish_staged(client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert client.puts == [(_BUCKET, "p/IDX_I/NIFTY.csv", CSV_CONTENT_TYPE)]
        assert CSV_CONTENT_TYPE == "text/csv; charset=utf-8"

    def test_clean_rerun_puts_nothing(self, tmp_path: Path) -> None:
        journal = _journal()
        _stage(tmp_path, journal, "p/IDX_I/NIFTY.csv", b"a\n")
        client = FakeS3Client()
        publish_staged(client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        rerun = publish_staged(client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert (rerun.attempted, rerun.published) == (0, 0)
        assert rerun.skipped_already == 1
        assert len(client.puts) == 1  # ZERO extra puts

    def test_retry_ladder_with_pinned_backoff(self, tmp_path: Path) -> None:
        journal = _journal()
        _stage(tmp_path, journal, "p/IDX_I/NIFTY.csv", b"a\n")
        client = FakeS3Client(fail_times={"p/IDX_I/NIFTY.csv": 2})
        delays: list[float] = []
        outcome = publish_staged(
            client=client,
            bucket=_BUCKET,
            journal=journal,
            lake_root=tmp_path,
            sleeper=delays.append,
        )
        assert outcome.published == 1
        assert delays == [1.0, 4.0]  # the pinned 1s/4s ladder
        assert len(client.puts) == 3

    def test_exhausted_retries_key_stays_pending_others_publish(self, tmp_path: Path) -> None:
        journal = _journal()
        _stage(tmp_path, journal, "p/IDX_I/BANKNIFTY.csv", b"a\n")
        _stage(tmp_path, journal, "p/IDX_I/NIFTY.csv", b"b\n")
        client = FakeS3Client(fail_times={"p/IDX_I/BANKNIFTY.csv": 99})
        outcome = publish_staged(
            client=client,
            bucket=_BUCKET,
            journal=journal,
            lake_root=tmp_path,
            sleeper=lambda _s: None,
        )
        assert outcome.published == 1
        assert outcome.failed_count == 1
        assert outcome.failed_keys == ("p/IDX_I/BANKNIFTY.csv",)
        assert outcome.aborted_nonretryable is False
        assert journal.pending_keys() == ("p/IDX_I/BANKNIFTY.csv",)
        assert "p/IDX_I/NIFTY.csv" in client.objects

    def test_nonretryable_aborts_whole_publish(self, tmp_path: Path) -> None:
        journal = _journal()
        for name in ("A", "B", "C"):
            _stage(tmp_path, journal, f"p/IDX_I/{name}.csv", b"x\n")
        client = FakeS3Client(nonretryable=frozenset({"p/IDX_I/A.csv"}))
        outcome = publish_staged(client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert outcome.aborted_nonretryable is True
        # The aborting key + every not-yet-attempted pending key count failed.
        assert outcome.failed_count == 3
        assert outcome.published == 0
        assert len(client.puts) == 1  # never a per-key storm against a broken grant

    def test_no_credentials_aborts_whole_publish_zero_retries(self, tmp_path: Path) -> None:
        # Round-4 polish P3 (bring-up hardening): botocore raises
        # NoCredentialsError when NO credential source resolves — a
        # creds-absent box must abort the WHOLE publish on the FIRST
        # attempt (the AccessDenied posture), never retry-storm
        # ~750 keys × 3 attempts against a box that can never succeed.
        class _NoCreds(Exception):
            pass

        # Retryability is classified by class NAME; align exactly.
        _NoCreds.__name__ = "NoCredentialsError"
        _NoCreds.__qualname__ = "NoCredentialsError"
        assert is_nonretryable_s3_error(_NoCreds("Unable to locate credentials")) is True

        class _NoCredsClient:
            def __init__(self) -> None:
                self.attempts = 0

            def put_object(
                self, *, bucket: str, key: str, body: bytes, content_type: str = CSV_CONTENT_TYPE
            ) -> None:
                _ = (bucket, key, body, content_type)
                self.attempts += 1
                raise _NoCreds("Unable to locate credentials")

        journal = _journal()
        for name in ("A", "B", "C"):
            _stage(tmp_path, journal, f"p/IDX_I/{name}.csv", b"x\n")
        sleeps: list[float] = []
        client = _NoCredsClient()
        outcome = publish_staged(
            client=client,
            bucket=_BUCKET,
            journal=journal,
            lake_root=tmp_path,
            sleeper=sleeps.append,
        )
        assert outcome.aborted_nonretryable is True
        assert outcome.published == 0
        assert outcome.failed_count == 3
        assert client.attempts == 1  # ONE attempt total — zero retries, no later keys tried
        assert sleeps == []  # the retry ladder never slept
        assert len(journal.pending_keys()) == 3  # everything stays pending for recovery

    def test_failed_keys_capped(self, tmp_path: Path) -> None:
        journal = _journal()
        keys = [f"p/IDX_I/{i:03d}.csv" for i in range(FAILED_KEYS_CAP + 3)]
        for key in keys:
            _stage(tmp_path, journal, key, b"x\n")
        client = FakeS3Client(nonretryable=frozenset({keys[0]}))
        outcome = publish_staged(client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert outcome.failed_count == len(keys)
        assert len(outcome.failed_keys) == FAILED_KEYS_CAP

    def test_malformed_journal_key_counts_failed_and_continues(self, tmp_path: Path) -> None:
        # F8: a tampered journal key (path traversal) is REJECTED by
        # staging_path_for_key before any join — counted failed + WARNed,
        # the valid sibling still publishes, and NOTHING outside the staging
        # tree is ever read or created.
        journal = _journal()
        hostile = "p/../../ESCAPE.csv"
        journal.mark_staged(hostile, "sha", 2)  # journal junk — never staged
        (tmp_path / "ESCAPE.csv").write_bytes(b"outside\n")  # the traversal target
        _stage(tmp_path, journal, "p/IDX_I/NIFTY.csv", b"a\n")
        client = FakeS3Client()
        outcome = publish_staged(client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert outcome.published == 1
        assert outcome.failed_count == 1
        assert outcome.failed_keys == (hostile,)
        assert hostile not in client.objects  # the outside bytes never left the box
        assert journal.pending_keys() == (hostile,)

    def test_missing_staged_file_counts_failed_and_continues(self, tmp_path: Path) -> None:
        journal = _journal()
        journal.mark_staged("p/IDX_I/GONE.csv", "sha", 2)  # never staged on disk
        _stage(tmp_path, journal, "p/IDX_I/NIFTY.csv", b"a\n")
        client = FakeS3Client()
        outcome = publish_staged(client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert outcome.published == 1
        assert outcome.failed_count == 1
        assert outcome.failed_keys == ("p/IDX_I/GONE.csv",)

    def test_corrupted_staged_bytes_never_published_stay_pending(self, tmp_path: Path) -> None:
        # Round-2 M2 (refuter-confirmed MAJOR): pre-fix the staged bytes were
        # PUT without verifying against the journal entry's recorded
        # sha256/size — on the cross-day recovery path a bit-rotted/tampered
        # staged file was published VERBATIM to production S3 and marked
        # published. Now: WARN + counted failed + stays pending + staging
        # kept (the staged_bytes_missing law — a same-day re-render heals).
        import structlog.testing

        journal = _journal()
        _stage(tmp_path, journal, "p/IDX_I/NIFTY.csv", b"good\n")
        _stage(tmp_path, journal, "p/IDX_I/BANKNIFTY.csv", b"fine\n")
        rotted = staging_path_for_key(tmp_path, _DAY, "p/IDX_I/NIFTY.csv")
        rotted.write_bytes(b"TAMPERED\n")  # bit-rot AFTER journaling
        client = FakeS3Client()
        with structlog.testing.capture_logs() as logs:
            outcome = publish_staged(
                client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path
            )
        assert outcome.published == 1  # the good sibling still publishes
        assert outcome.failed_count == 1
        assert outcome.failed_keys == ("p/IDX_I/NIFTY.csv",)
        assert "p/IDX_I/NIFTY.csv" not in client.objects  # tampered bytes never PUT
        assert client.objects["p/IDX_I/BANKNIFTY.csv"] == b"fine\n"
        assert journal.pending_keys() == ("p/IDX_I/NIFTY.csv",)  # stays pending
        assert rotted.read_bytes() == b"TAMPERED\n"  # staging kept (forensic + heal)
        corrupt = [e for e in logs if e["event"] == "crossverify.staged_bytes_corrupt"]
        assert len(corrupt) == 1
        assert corrupt[0]["key"] == repr("p/IDX_I/NIFTY.csv")

    def test_same_size_different_bytes_tamper_refused(self, tmp_path: Path) -> None:
        # Round-3 X4 (surviving mutant): the sha256 operand of the M2 verify
        # had NO killing test — every prior corrupt fixture ALSO changed the
        # length, so dropping `actual_sha != entry.sha256` survived the whole
        # suite. A SAME-LENGTH different-bytes tamper (b"good\n" → b"badd\n",
        # both 5 bytes) is caught ONLY by the sha operand.
        import structlog.testing

        journal = _journal()
        _stage(tmp_path, journal, "p/IDX_I/NIFTY.csv", b"good\n")
        staging_path_for_key(tmp_path, _DAY, "p/IDX_I/NIFTY.csv").write_bytes(b"badd\n")
        client = FakeS3Client()
        with structlog.testing.capture_logs() as logs:
            outcome = publish_staged(
                client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path
            )
        assert outcome.published == 0
        assert outcome.failed_count == 1
        assert outcome.failed_keys == ("p/IDX_I/NIFTY.csv",)
        assert "p/IDX_I/NIFTY.csv" not in client.objects  # tampered bytes never PUT
        assert journal.pending_keys() == ("p/IDX_I/NIFTY.csv",)  # stays pending
        corrupt = [e for e in logs if e["event"] == "crossverify.staged_bytes_corrupt"]
        assert len(corrupt) == 1
        assert corrupt[0]["expected_size"] == corrupt[0]["actual_size"] == 5

    def test_size_mismatch_alone_counts_failed(self, tmp_path: Path) -> None:
        # The size half of the M2 verify: a journal entry whose recorded
        # size disagrees with the staged bytes (a torn journal write) also
        # refuses the PUT — never a publish on inconsistent evidence.
        import hashlib

        from brutex.crossverify.journal import JournalEntry

        journal = _journal()
        _stage(tmp_path, journal, "p/IDX_I/NIFTY.csv", b"good\n")
        good_sha = hashlib.sha256(b"good\n").hexdigest()
        journal.entries["p/IDX_I/NIFTY.csv"] = JournalEntry(
            key="p/IDX_I/NIFTY.csv", sha256=good_sha, size=999, published=False
        )
        client = FakeS3Client()
        outcome = publish_staged(client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert outcome.published == 0
        assert outcome.failed_count == 1
        assert "p/IDX_I/NIFTY.csv" not in client.objects

    def test_corrupt_then_restage_heals_and_publishes(self, tmp_path: Path) -> None:
        # The heal law: after a corrupt refusal, re-staging the correct bytes
        # (the same-day re-render) publishes them — convergent.
        journal = _journal()
        _stage(tmp_path, journal, "p/IDX_I/NIFTY.csv", b"good\n")
        staging_path_for_key(tmp_path, _DAY, "p/IDX_I/NIFTY.csv").write_bytes(b"rot\n")
        client = FakeS3Client()
        first = publish_staged(client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert first.failed_count == 1
        _stage(tmp_path, journal, "p/IDX_I/NIFTY.csv", b"good\n")  # the re-render
        second = publish_staged(client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert second.published == 1
        assert client.objects["p/IDX_I/NIFTY.csv"] == b"good\n"
        assert journal.pending_keys() == ()

    def test_journal_saved_every_25_published(self, tmp_path: Path) -> None:
        journal = _journal()
        for i in range(JOURNAL_SAVE_EVERY + 1):  # 26 keys → one mid-run save + final
            _stage(tmp_path, journal, f"p/IDX_I/{i:03d}.csv", b"x\n")
        saves: list[int] = []
        original_save = DayJournal.save

        def _spy_save(self: DayJournal, lake_root: Path) -> None:
            saves.append(sum(1 for e in self.entries.values() if e.published))
            original_save(self, lake_root)

        journal.save = _spy_save.__get__(journal)  # type: ignore[method-assign]
        publish_staged(client=FakeS3Client(), bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert saves == [JOURNAL_SAVE_EVERY, JOURNAL_SAVE_EVERY + 1]

    def test_publish_then_journal_ordering_crash_converges(self, tmp_path: Path) -> None:
        # Crash-sim: the process dies BETWEEN a successful PUT and the journal
        # save. The durable journal still says pending; the next run re-puts
        # IDENTICAL bytes (S3 last-writer-wins) and converges.
        journal = _journal()
        _stage(tmp_path, journal, "p/IDX_I/NIFTY.csv", b"a\n")
        journal.save(tmp_path)
        client = FakeS3Client()

        class _Crash(RuntimeError):
            pass

        def _crashing_mark(key: str) -> None:
            raise _Crash(f"crash before journaling {key}")

        journal.mark_published = _crashing_mark  # type: ignore[method-assign]
        with pytest.raises(_Crash):
            publish_staged(client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert client.objects["p/IDX_I/NIFTY.csv"] == b"a\n"  # the PUT landed first
        # Recover from the DURABLE journal (pre-crash state) — still pending.
        recovered = load_journal(tmp_path, _DAY)
        assert recovered is not None
        assert recovered.pending_keys() == ("p/IDX_I/NIFTY.csv",)
        outcome = publish_staged(
            client=client, bucket=_BUCKET, journal=recovered, lake_root=tmp_path
        )
        assert outcome.published == 1
        assert client.objects["p/IDX_I/NIFTY.csv"] == b"a\n"  # identical bytes — convergent

    def test_empty_backoff_sequence_sleeps_zero(self, tmp_path: Path) -> None:
        journal = _journal()
        _stage(tmp_path, journal, "p/IDX_I/NIFTY.csv", b"a\n")
        client = FakeS3Client(fail_times={"p/IDX_I/NIFTY.csv": 1})
        delays: list[float] = []
        outcome = publish_staged(
            client=client,
            bucket=_BUCKET,
            journal=journal,
            lake_root=tmp_path,
            backoff_s=(),
            sleeper=delays.append,
        )
        assert outcome.published == 1
        assert delays == [0.0]

    def test_journal_file_not_rewritten_when_nothing_published(self, tmp_path: Path) -> None:
        journal = _journal()
        journal.mark_staged("p/IDX_I/GONE.csv", "sha", 2)  # missing staged file only
        publish_staged(client=FakeS3Client(), bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert not journal_path(tmp_path, _DAY).exists()  # no dirty save
