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
        for name in ("a", "b", "c"):
            _stage(tmp_path, journal, f"p/IDX_I/{name}.csv", b"x\n")
        client = FakeS3Client(nonretryable=frozenset({"p/IDX_I/a.csv"}))
        outcome = publish_staged(client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert outcome.aborted_nonretryable is True
        # The aborting key + every not-yet-attempted pending key count failed.
        assert outcome.failed_count == 3
        assert outcome.published == 0
        assert len(client.puts) == 1  # never a per-key storm against a broken grant

    def test_failed_keys_capped(self, tmp_path: Path) -> None:
        journal = _journal()
        keys = [f"p/IDX_I/{i:03d}.csv" for i in range(FAILED_KEYS_CAP + 3)]
        for key in keys:
            _stage(tmp_path, journal, key, b"x\n")
        client = FakeS3Client(nonretryable=frozenset({keys[0]}))
        outcome = publish_staged(client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert outcome.failed_count == len(keys)
        assert len(outcome.failed_keys) == FAILED_KEYS_CAP

    def test_missing_staged_file_counts_failed_and_continues(self, tmp_path: Path) -> None:
        journal = _journal()
        journal.mark_staged("p/IDX_I/GONE.csv", "sha", 2)  # never staged on disk
        _stage(tmp_path, journal, "p/IDX_I/NIFTY.csv", b"a\n")
        client = FakeS3Client()
        outcome = publish_staged(client=client, bucket=_BUCKET, journal=journal, lake_root=tmp_path)
        assert outcome.published == 1
        assert outcome.failed_count == 1
        assert outcome.failed_keys == ("p/IDX_I/GONE.csv",)

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
