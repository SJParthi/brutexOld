"""Unit tests — :mod:`brutex.crossverify.journal` (durable idempotency record)."""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING

import pytest
from brutex.crossverify.canonical import canonical_identity
from brutex.crossverify.journal import (
    CROSSVERIFY_DIRNAME,
    JOURNAL_VERSION,
    MAX_JOURNAL_BYTES,
    DayJournal,
    JournalEntry,
    journal_path,
    last_run_path,
    load_journal,
    load_last_run,
    scan_pending_journals,
    scan_withheld_journals,
    staging_dir,
    staging_path,
    staging_path_for_key,
    write_last_run,
)

if TYPE_CHECKING:
    from pathlib import Path

_DAY = date(2026, 7, 10)


def _journal(**entries: JournalEntry) -> DayJournal:
    return DayJournal(day=_DAY, run_id="cv-test", entries=dict(entries))


class TestPaths:
    def test_journal_path(self, tmp_path: Path) -> None:
        assert journal_path(tmp_path, _DAY) == tmp_path / ".crossverify" / "2026-07-10.json"
        assert CROSSVERIFY_DIRNAME == ".crossverify"

    def test_staging_dir(self, tmp_path: Path) -> None:
        assert staging_dir(tmp_path, _DAY) == tmp_path / ".crossverify" / "staging" / "2026-07-10"

    def test_staging_path_mirrors_s3_layout(self, tmp_path: Path) -> None:
        identity = canonical_identity(exchange="NSE", segment="CASH", lake_symbol="M&M")
        assert identity is not None
        assert staging_path(tmp_path, _DAY, identity) == (
            tmp_path / ".crossverify" / "staging" / "2026-07-10" / "NSE_EQ" / "M&M.csv"
        )

    def test_staging_path_for_key_takes_last_two_components(self, tmp_path: Path) -> None:
        key = "crossverify/groww/2026-07-10/IDX_I/NIFTY.csv"
        assert staging_path_for_key(tmp_path, _DAY, key) == (
            staging_dir(tmp_path, _DAY) / "IDX_I" / "NIFTY.csv"
        )

    def test_staging_path_for_key_accepts_legal_nse_symbols(self, tmp_path: Path) -> None:
        for symbol in ("M&M", "BAJAJ-AUTO", "L&TFH", "3MINDIA", "NIFTY-2026-07-30-FUT"):
            key = f"crossverify/groww/2026-07-10/NSE_EQ/{symbol}.csv"
            assert staging_path_for_key(tmp_path, _DAY, key) == (
                staging_dir(tmp_path, _DAY) / "NSE_EQ" / f"{symbol}.csv"
            )

    @pytest.mark.parametrize(
        "key",
        [
            "solo",  # fewer than 2 components
            "",  # empty key
            "p/../ESCAPE.csv",  # traversal segment
            "p/nse_eq/NIFTY.csv",  # lowercase segment token
            "p/IDX I/NIFTY.csv",  # whitespace in segment
            "p/IDX_I/..",  # non-.csv filename
            "p/IDX_I/NIFTY.txt",  # wrong suffix
            "p/IDX_I/.csv",  # empty symbol stem
            "p/IDX_I/nifty.csv",  # lowercase symbol (F5 allowlist)
            "p/IDX_I/NIF\x1bTY.csv",  # control char (ESC) in symbol
            "p/IDX_I/NIF\tTY.csv",  # TAB in symbol
            "p/IDX_I/NIF\x00TY.csv",  # NUL in symbol
            "p/IDX_I/...csv",  # pure-dot symbol stem ".."
            "p/IDX_I/..csv",  # pure-dot symbol stem "."
        ],
    )
    def test_staging_path_for_key_rejects_malformed_key(self, tmp_path: Path, key: str) -> None:
        # A tampered/corrupt journal key must never traverse outside the
        # staging tree — validated BEFORE any path join (deny-by-default).
        with pytest.raises(ValueError, match="journal key"):
            staging_path_for_key(tmp_path, _DAY, key)


class TestMarkAndPending:
    def test_mark_staged_fresh(self) -> None:
        journal = _journal()
        journal.mark_staged("k", "abc", 10)
        assert journal.entries["k"] == JournalEntry(key="k", sha256="abc", size=10, published=False)
        assert journal.pending_keys() == ("k",)

    def test_mark_published_flips(self) -> None:
        journal = _journal()
        journal.mark_staged("k", "abc", 10)
        journal.mark_published("k")
        assert journal.entries["k"].published is True
        assert journal.pending_keys() == ()

    def test_restage_same_sha_keeps_published(self) -> None:
        # The 0-puts-on-a-clean-re-run law.
        journal = _journal()
        journal.mark_staged("k", "abc", 10)
        journal.mark_published("k")
        journal.mark_staged("k", "abc", 10)
        assert journal.entries["k"].published is True

    def test_restage_different_sha_rearms(self) -> None:
        # A changed lake ⇒ re-render differs ⇒ overwrite is the CORRECT action.
        journal = _journal()
        journal.mark_staged("k", "abc", 10)
        journal.mark_published("k")
        journal.mark_staged("k", "DIFFERENT", 11)
        assert journal.entries["k"].published is False
        assert journal.pending_keys() == ("k",)

    def test_restage_unpublished_stays_pending(self) -> None:
        journal = _journal()
        journal.mark_staged("k", "abc", 10)
        journal.mark_staged("k", "abc", 10)
        assert journal.entries["k"].published is False

    def test_pending_keys_sorted(self) -> None:
        journal = _journal()
        journal.mark_staged("z", "1", 1)
        journal.mark_staged("a", "2", 2)
        assert journal.pending_keys() == ("a", "z")


class TestSaveLoadRoundTrip:
    def test_round_trip(self, tmp_path: Path) -> None:
        journal = _journal()
        journal.mark_staged("p/IDX_I/NIFTY.csv", "sha1", 100)
        journal.mark_staged("p/NSE_EQ/M&M.csv", "sha2", 200)
        journal.mark_published("p/IDX_I/NIFTY.csv")
        journal.withheld = {"RELIANCE": "count_short"}
        journal.save(tmp_path)
        loaded = load_journal(tmp_path, _DAY)
        assert loaded is not None
        assert loaded.day == _DAY
        assert loaded.run_id == "cv-test"
        assert loaded.entries == journal.entries
        assert loaded.withheld == {"RELIANCE": "count_short"}
        assert loaded.pending_keys() == ("p/NSE_EQ/M&M.csv",)

    def test_save_is_atomic_json(self, tmp_path: Path) -> None:
        journal = _journal()
        journal.mark_staged("k", "s", 1)
        journal.save(tmp_path)
        payload = json.loads(journal_path(tmp_path, _DAY).read_text(encoding="utf-8"))
        assert payload["version"] == JOURNAL_VERSION
        assert payload["day"] == "2026-07-10"

    def test_absent_loads_none(self, tmp_path: Path) -> None:
        assert load_journal(tmp_path, _DAY) is None

    @pytest.mark.parametrize(
        "body",
        [
            "not json at all {",
            "[1, 2, 3]",  # non-dict root
            '{"version": 99, "day": "2026-07-10"}',  # unknown FUTURE version
            '{"version": 1, "day": "junk", "run_id": "x", "entries": {}, "withheld": {}}',
            '{"version": 1, "day": "2026-07-10", "run_id": 7, "entries": {}, "withheld": {}}',
            '{"version": 1, "day": "2026-07-10", "run_id": "x", "withheld": {}}',  # no entries
            (
                '{"version": 1, "day": "2026-07-10", "run_id": "x", '
                '"entries": {"k": {"sha256": "s"}}, "withheld": {}}'
            ),  # entry missing fields
            (
                '{"version": 1, "day": "2026-07-10", "run_id": "x", '
                '"entries": {}, "withheld": null}'
            ),  # withheld not a mapping
        ],
    )
    def test_corrupt_loads_none(self, tmp_path: Path, body: str) -> None:
        path = journal_path(tmp_path, _DAY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        assert load_journal(tmp_path, _DAY) is None  # + one WARN (re-render converges)

    def test_invalid_utf8_bytes_load_none_with_warn(self, tmp_path: Path) -> None:
        # Round-2 M1 (refuter-confirmed MAJOR): pre-fix the read sat OUTSIDE
        # the corruption-tolerant path, so invalid-UTF-8 BYTES (bit rot / a
        # torn write) raised UnicodeDecodeError uncaught — crashing the run
        # at both call sites and wedging every future run via the recovery
        # scan. Now: None + ONE crossverify.journal_corrupt WARN.
        import structlog.testing

        path = journal_path(tmp_path, _DAY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe{")
        with structlog.testing.capture_logs() as logs:
            assert load_journal(tmp_path, _DAY) is None
        corrupt = [e for e in logs if e["event"] == "crossverify.journal_corrupt"]
        assert len(corrupt) == 1
        assert corrupt[0]["error_type"] == "UnicodeDecodeError"
        assert corrupt[0]["path"] == str(path)

    def test_directory_at_journal_path_loads_none_with_warn(self, tmp_path: Path) -> None:
        # The OSError arm beyond plain absence: a DIRECTORY planted at the
        # journal path (IsADirectoryError) degrades, never a crash.
        import structlog.testing

        journal_path(tmp_path, _DAY).mkdir(parents=True)
        with structlog.testing.capture_logs() as logs:
            assert load_journal(tmp_path, _DAY) is None
        corrupt = [e for e in logs if e["event"] == "crossverify.journal_corrupt"]
        assert len(corrupt) == 1
        assert corrupt[0]["error_type"] == "IsADirectoryError"

    def test_file_at_crossverify_dir_loads_none_with_warn(self, tmp_path: Path) -> None:
        # The STAT-side OSError arm: a FILE planted where the `.crossverify`
        # DIRECTORY belongs makes the size probe raise NotADirectoryError —
        # the same WARN-and-None degrade, never a crash.
        import structlog.testing

        (tmp_path / ".crossverify").write_bytes(b"not a dir")
        with structlog.testing.capture_logs() as logs:
            assert load_journal(tmp_path, _DAY) is None
        corrupt = [e for e in logs if e["event"] == "crossverify.journal_corrupt"]
        assert len(corrupt) == 1
        assert corrupt[0]["error_type"] == "NotADirectoryError"

    @pytest.mark.parametrize(
        ("body", "error_type"),
        [
            (
                # json.loads accepts the bareword Infinity → int(float("inf"))
                # raises OverflowError at the size coercion — NOT a
                # ValueError/TypeError, so pre-fix it escaped load_journal
                # raw AND wedged the recovery scan (executed repro).
                '{"version": 1, "day": "2026-07-10", "run_id": "x", "entries": '
                '{"k": {"sha256": "s", "size": Infinity, "published": false}}, "withheld": {}}',
                "OverflowError",
            ),
            (
                # A deeply-nested valid-JSON payload blows the parser stack
                # (RecursionError) well under the size cap — same escape.
                "[" * 200_000 + "]" * 200_000,
                "RecursionError",
            ),
        ],
    )
    def test_parser_escape_classes_load_none_with_warn(
        self, tmp_path: Path, body: str, error_type: str
    ) -> None:
        # Round-3 X2 (the SweepCheckpointStore corruption-mapping precedent):
        # every corruption class maps to WARN + None — never a raw
        # OverflowError/RecursionError escaping the tolerant path.
        import structlog.testing

        path = journal_path(tmp_path, _DAY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        with structlog.testing.capture_logs() as logs:
            assert load_journal(tmp_path, _DAY) is None
        corrupt = [e for e in logs if e["event"] == "crossverify.journal_corrupt"]
        assert len(corrupt) == 1
        assert corrupt[0]["error_type"] == error_type

    def test_oversized_journal_refused_before_read(self, tmp_path: Path) -> None:
        # The size-cap half of the precedent: an over-cap planted file is
        # REFUSED at the stat — never an unbounded read (MemoryError class).
        import structlog.testing
        from brutex.crossverify.journal import MAX_JOURNAL_BYTES

        path = journal_path(tmp_path, _DAY)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * (MAX_JOURNAL_BYTES + 1))
        with structlog.testing.capture_logs() as logs:
            assert load_journal(tmp_path, _DAY) is None
        corrupt = [e for e in logs if e["event"] == "crossverify.journal_corrupt"]
        assert len(corrupt) == 1
        assert corrupt[0]["error_type"] == "OversizedJournal"


class TestScanPendingJournals:
    def test_empty_lake_scans_nothing(self, tmp_path: Path) -> None:
        assert scan_pending_journals(tmp_path, today=_DAY, lookback_days=5) == []

    def test_prior_pending_found_oldest_first(self, tmp_path: Path) -> None:
        for offset, key in [(1, "p/a.csv"), (3, "p/b.csv")]:
            journal = DayJournal(
                day=_DAY.replace(day=_DAY.day - offset), run_id=f"cv-{offset}", entries={}
            )
            journal.mark_staged(key, "s", 1)
            journal.save(tmp_path)
        scanned = scan_pending_journals(tmp_path, today=_DAY, lookback_days=5)
        assert [j.day.day for j in scanned] == [7, 9]  # oldest first

    def test_fully_published_prior_skipped(self, tmp_path: Path) -> None:
        journal = DayJournal(day=_DAY.replace(day=9), run_id="cv", entries={})
        journal.mark_staged("k", "s", 1)
        journal.mark_published("k")
        journal.save(tmp_path)
        assert scan_pending_journals(tmp_path, today=_DAY, lookback_days=5) == []

    def test_todays_journal_not_scanned(self, tmp_path: Path) -> None:
        journal = _journal()
        journal.mark_staged("k", "s", 1)
        journal.save(tmp_path)
        assert scan_pending_journals(tmp_path, today=_DAY, lookback_days=5) == []

    def test_corrupt_prior_skipped(self, tmp_path: Path) -> None:
        path = journal_path(tmp_path, _DAY.replace(day=9))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt", encoding="utf-8")
        assert scan_pending_journals(tmp_path, today=_DAY, lookback_days=5) == []

    def test_invalid_utf8_prior_skipped_never_raises(self, tmp_path: Path) -> None:
        # Round-2 M1: ONE bit-rotted prior-day journal in the catchup window
        # must never kill the scan (pre-fix: UnicodeDecodeError propagated,
        # wedging EVERY later run before it reached its own day). A healthy
        # pending sibling is still found.
        import structlog.testing

        rotten = journal_path(tmp_path, _DAY.replace(day=9))
        rotten.parent.mkdir(parents=True, exist_ok=True)
        rotten.write_bytes(b"\xff\xfe{")
        healthy = DayJournal(day=_DAY.replace(day=8), run_id="cv", entries={})
        healthy.mark_staged("p/IDX_I/NIFTY.csv", "s", 1)
        healthy.save(tmp_path)
        with structlog.testing.capture_logs() as logs:
            scanned = scan_pending_journals(tmp_path, today=_DAY, lookback_days=5)
        assert [j.day.day for j in scanned] == [8]  # the rotten day skipped
        assert any(e["event"] == "crossverify.journal_corrupt" for e in logs)

    @pytest.mark.parametrize(
        "body",
        [
            '{"version": 1, "day": "2026-07-08", "run_id": "x", "entries": '
            '{"k": {"sha256": "s", "size": Infinity, "published": false}}, "withheld": {}}',
            "[" * 200_000 + "]" * 200_000,
        ],
    )
    def test_parser_escape_prior_skipped_never_raises(self, tmp_path: Path, body: str) -> None:
        # Round-3 X2, the recovery-scan arm: ONE Infinity-size / deeply-nested
        # prior-day journal in the catchup window must never kill the scan
        # (pre-fix: OverflowError / RecursionError propagated, wedging every
        # later run). A healthy pending sibling is still found.
        import structlog.testing

        rotten = journal_path(tmp_path, _DAY.replace(day=9))
        rotten.parent.mkdir(parents=True, exist_ok=True)
        rotten.write_text(body, encoding="utf-8")
        healthy = DayJournal(day=_DAY.replace(day=8), run_id="cv", entries={})
        healthy.mark_staged("p/IDX_I/NIFTY.csv", "s", 1)
        healthy.save(tmp_path)
        with structlog.testing.capture_logs() as logs:
            scanned = scan_pending_journals(tmp_path, today=_DAY, lookback_days=5)
        assert [j.day.day for j in scanned] == [8]  # the poisoned day skipped
        assert any(e["event"] == "crossverify.journal_corrupt" for e in logs)

    def test_lookback_zero_scans_nothing(self, tmp_path: Path) -> None:
        journal = DayJournal(day=_DAY.replace(day=9), run_id="cv", entries={})
        journal.mark_staged("k", "s", 1)
        journal.save(tmp_path)
        assert scan_pending_journals(tmp_path, today=_DAY, lookback_days=0) == []


class TestScanWithheldJournals:
    """The auto-re-gate scan (DEC-CROSSVERIFY-WITHHELD-REGATE-001)."""

    @staticmethod
    def _seed(lake: Path, day: date, withheld: dict[str, str]) -> None:
        journal = DayJournal(day=day, run_id="cv", entries={})
        journal.withheld = dict(withheld)
        journal.save(lake)

    def test_empty_lake_scans_nothing(self, tmp_path: Path) -> None:
        assert scan_withheld_journals(tmp_path, today=_DAY, lookback_days=5) == []

    def test_withheld_priors_found_oldest_first(self, tmp_path: Path) -> None:
        self._seed(tmp_path, _DAY.replace(day=9), {"RELIANCE": "volume_all_zero"})
        self._seed(tmp_path, _DAY.replace(day=7), {"NIFTY": "count_short"})
        scanned = scan_withheld_journals(tmp_path, today=_DAY, lookback_days=5)
        assert [j.day.day for j in scanned] == [7, 9]  # oldest first
        assert scanned[0].withheld == {"NIFTY": "count_short"}

    def test_clean_and_pending_only_priors_skipped(self, tmp_path: Path) -> None:
        # A day with an EMPTY withheld map never re-gates — even when it
        # still holds PENDING keys (that is the recovery scan's job).
        clean = DayJournal(day=_DAY.replace(day=9), run_id="cv", entries={})
        clean.mark_staged("p/IDX_I/NIFTY.csv", "s", 1)
        clean.save(tmp_path)
        assert scan_withheld_journals(tmp_path, today=_DAY, lookback_days=5) == []

    def test_todays_journal_not_scanned(self, tmp_path: Path) -> None:
        self._seed(tmp_path, _DAY, {"RELIANCE": "volume_all_zero"})
        assert scan_withheld_journals(tmp_path, today=_DAY, lookback_days=5) == []

    def test_outside_window_not_scanned(self, tmp_path: Path) -> None:
        self._seed(tmp_path, _DAY.replace(day=1), {"RELIANCE": "volume_all_zero"})
        assert scan_withheld_journals(tmp_path, today=_DAY, lookback_days=5) == []

    def test_corrupt_prior_skipped_never_raises(self, tmp_path: Path) -> None:
        path = journal_path(tmp_path, _DAY.replace(day=9))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt", encoding="utf-8")
        self._seed(tmp_path, _DAY.replace(day=8), {"M&M": "read_error"})
        scanned = scan_withheld_journals(tmp_path, today=_DAY, lookback_days=5)
        assert [j.day.day for j in scanned] == [8]


class TestLastRunRecordStore:
    """The durable last-run view sidecar (write/load + corruption tolerance)."""

    def test_path_law(self, tmp_path: Path) -> None:
        assert last_run_path(tmp_path) == tmp_path / CROSSVERIFY_DIRNAME / "last_run.json"

    def test_roundtrip(self, tmp_path: Path) -> None:
        record: dict[str, object] = {
            "version": JOURNAL_VERSION,
            "day": "2026-07-10",
            "verdict": "publish_failed",
            "exit_code": 2,
            "failed_keys": ["crossverify/groww/2026-07-10/IDX_I/NIFTY.csv"],
        }
        write_last_run(tmp_path, record)
        assert load_last_run(tmp_path) == record

    def test_overwrite_is_atomic_last_writer_wins(self, tmp_path: Path) -> None:
        write_last_run(tmp_path, {"version": JOURNAL_VERSION, "verdict": "published"})
        write_last_run(tmp_path, {"version": JOURNAL_VERSION, "verdict": "refused"})
        loaded = load_last_run(tmp_path)
        assert loaded is not None and loaded["verdict"] == "refused"

    def test_absent_reads_none(self, tmp_path: Path) -> None:
        assert load_last_run(tmp_path) is None

    @pytest.mark.parametrize(
        "body",
        [
            b"{corrupt",
            b"\xff\xfe{",
            b"[1, 2, 3]",  # valid JSON, not a dict
            b'{"version": 99}',  # unknown future version
            b"[" * 200_000 + b"]" * 200_000,  # the RecursionError escape
        ],
    )
    def test_corrupt_reads_none_with_one_warn(self, tmp_path: Path, body: bytes) -> None:
        import structlog.testing

        path = last_run_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        with structlog.testing.capture_logs() as logs:
            assert load_last_run(tmp_path) is None
        assert any(e["event"] == "crossverify.last_run_corrupt" for e in logs)

    def test_directory_at_path_reads_none(self, tmp_path: Path) -> None:
        last_run_path(tmp_path).mkdir(parents=True)
        assert load_last_run(tmp_path) is None

    def test_oversized_refused_before_the_read(self, tmp_path: Path) -> None:
        import structlog.testing

        path = last_run_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b" " * (MAX_JOURNAL_BYTES + 1))
        with structlog.testing.capture_logs() as logs:
            assert load_last_run(tmp_path) is None
        assert any(
            e["event"] == "crossverify.last_run_corrupt" and e["error_type"] == "OversizedRecord"
            for e in logs
        )
