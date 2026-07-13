"""Tests for `brutex.dashboard.pull_intent` (DEC-PULL-INTENT-RESUME-001).

100% line+branch on the durable pull-intent module: the escape-hatch resolver,
the frozen :class:`PullIntent` payload round-trip + strict shape validation, the
atomic best-effort :class:`PullIntentStore` (write / load / clear / bump — a
corrupt or unreadable file NEVER raises), the dry-run-never-persists law, the
single-flight acquire-probe, the over-cap CRITICAL alert, and the whole
:func:`run_auto_resume` boot decision (no-intent / peer-active / over-cap /
bump-before-launch / fire / launch-failure) — every branch pinned with fakes,
plus the REAL ``fcntl.flock`` probe against a tmp lake root.
"""

from __future__ import annotations

import json
import os
import signal
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar

import pytest
from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock
from brutex.dashboard import pull_intent as pull_intent_module
from brutex.dashboard.pull_intent import (
    MAX_AUTO_RESTARTS,
    PULL_INTENT_FILENAME,
    PULL_INTENT_RESUME_ENV,
    PullIntent,
    PullIntentStore,
    ResumeAction,
    ResumeBanner,
    clear_banner_after_start,
    emit_restart_cap_alert,
    persist_intent_for_launch,
    probe_pull_singleflight,
    pull_intent_resume_active,
    read_sidecar_bytes,
    resolve_pull_intent_resume,
    restart_cap_message,
    restore_intent_after_refused_start,
    run_auto_resume,
    run_legacy_resume,
    synthesize_legacy_intent,
)
from structlog.testing import capture_logs


@contextmanager
def _hang_watchdog(seconds: int = 5) -> Generator[None]:
    """Turn an indefinite block inside the body into a LOUD failure.

    The fails-before shape of the round-7 P-1 defect is a read that blocks
    FOREVER (a FIFO read waits for a writer; ``/dev/zero`` yields infinite
    zeros), so a plain regression would HANG rather than fail; the
    ``SIGALRM`` handler interrupts the blocked syscall and fails the test
    instead (the #1614 round-6 watchdog idiom).
    """

    def _alarm(_signum: int, _frame: object) -> None:
        raise AssertionError(f"intent load blocked > {seconds}s (non-regular-inode hang)")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _intent(**overrides: Any) -> PullIntent:
    base: dict[str, Any] = {
        "symbols": "NIFTY,BANKNIFTY",
        "symbol": None,
        "from_date": "2024-01-01",
        "to_date": "2024-02-01",
        "restarts": 0,
        "created_at": "2026-07-04T00:00:00+00:00",
    }
    base.update(overrides)
    return PullIntent(**base)


# ── resolve_pull_intent_resume (escape hatch, default ON) ────────────────────


def test_resolve_unset_defaults_on() -> None:
    assert resolve_pull_intent_resume({}) is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", " No ", "off", "OFF"])
def test_resolve_explicit_falsy_disables(value: str) -> None:
    assert resolve_pull_intent_resume({PULL_INTENT_RESUME_ENV: value}) is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "", "  ", "maybe"])
def test_resolve_anything_else_stays_on(value: str) -> None:
    # Default-ON: only an EXPLICIT falsy token disables (blank/junk keeps ON).
    assert resolve_pull_intent_resume({PULL_INTENT_RESUME_ENV: value}) is True


def test_resolve_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PULL_INTENT_RESUME_ENV, "off")
    assert resolve_pull_intent_resume() is False


# ── PullIntent payload round-trip + strict shape validation ──────────────────


def test_payload_round_trip() -> None:
    intent = _intent(restarts=2)
    parsed = PullIntent.from_payload(intent.to_payload())
    assert parsed == intent


def test_from_payload_rejects_non_mapping() -> None:
    assert PullIntent.from_payload(["not", "a", "mapping"]) is None
    assert PullIntent.from_payload(None) is None


@pytest.mark.parametrize("field", ["symbols", "symbol", "from_date", "to_date", "scope"])
def test_from_payload_rejects_non_string_request_field(field: str) -> None:
    payload = _intent().to_payload()
    payload[field] = 42
    assert PullIntent.from_payload(payload) is None


def test_scope_round_trips_and_pre_scope_payload_defaults_none() -> None:
    """DEC-PULL-SCOPED-ALL-ONECLICK-001: a persisted spot-only intent resumes
    SPOT-ONLY (the scope rides the payload verbatim); a PRE-scope intent file
    (no ``scope`` key at all — every file written before #1779) parses to
    ``None`` = the full chain (optional-field back-compat)."""
    intent = PullIntent(symbols=None, symbol=None, from_date=None, to_date=None, scope="spot-only")
    parsed = PullIntent.from_payload(intent.to_payload())
    assert parsed is not None
    assert parsed.scope == "spot-only"
    legacy = _intent().to_payload()
    del legacy["scope"]
    legacy_parsed = PullIntent.from_payload(legacy)
    assert legacy_parsed is not None
    assert legacy_parsed.scope is None


def test_from_payload_accepts_none_request_fields() -> None:
    payload = _intent(symbols=None, symbol=None, from_date=None, to_date=None).to_payload()
    parsed = PullIntent.from_payload(payload)
    assert parsed is not None
    assert parsed.symbols is None
    assert parsed.to_date is None


@pytest.mark.parametrize("restarts", [True, "2", -1, 1.5, None])
def test_from_payload_rejects_bad_restarts(restarts: Any) -> None:
    # A JSON ``true`` is NOT a count (bool is an int subclass — pinned).
    payload = _intent().to_payload()
    payload["restarts"] = restarts
    assert PullIntent.from_payload(payload) is None


def test_from_payload_rejects_non_string_created_at() -> None:
    payload = _intent().to_payload()
    payload["created_at"] = 123
    assert PullIntent.from_payload(payload) is None


def test_from_payload_missing_created_at_defaults_empty() -> None:
    payload = _intent().to_payload()
    del payload["created_at"]
    parsed = PullIntent.from_payload(payload)
    assert parsed is not None
    assert parsed.created_at == ""


def test_from_payload_ignores_unknown_extra_keys() -> None:
    payload = _intent().to_payload()
    payload["future_field"] = {"anything": 1}
    assert PullIntent.from_payload(payload) == _intent()


# ── ResumeBanner ─────────────────────────────────────────────────────────────


def test_banner_starts_unset_and_set_clear_roundtrip() -> None:
    banner = ResumeBanner()
    assert banner.snapshot() is None
    banner.set(level="critical", message="boom")
    assert banner.snapshot() == {"level": "critical", "message": "boom"}
    assert banner.clear() is True, "a REAL clear reports the payload it dropped"
    assert banner.snapshot() is None
    assert banner.clear() is False, "an idempotent no-op clear reports nothing dropped"
    assert banner.snapshot() is None


def test_banner_snapshot_returns_a_copy() -> None:
    banner = ResumeBanner()
    banner.set(level="critical", message="boom")
    snap = banner.snapshot()
    assert snap is not None
    snap["message"] = "mutated"
    assert banner.snapshot() == {"level": "critical", "message": "boom"}


# ── PullIntentStore (atomic, best-effort, never raises) ──────────────────────


def test_store_paths(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    assert store.lake_root == tmp_path
    assert store.path == tmp_path / PULL_INTENT_FILENAME


def test_write_then_load_round_trip(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    intent = _intent(restarts=1)
    wrote = store.write(intent)
    assert wrote is True
    assert store.load() == intent
    # The on-disk shape is plain JSON (forensics-readable).
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["restarts"] == 1
    assert raw["version"] == 1


def test_write_is_atomic_overwrite_and_leaves_no_temp(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    wrote_first = store.write(_intent(restarts=0))
    assert wrote_first is True
    wrote_second = store.write(_intent(restarts=2))
    assert wrote_second is True
    loaded = store.load()
    assert loaded is not None
    assert loaded.restarts == 2
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".brutex-write-")]
    assert leftovers == []


def test_write_failure_is_logged_false_and_preserves_prior_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A torn write can never corrupt the prior intent: the atomic core raising
    # mid-write returns False and the previous file content survives byte-for-byte.
    store = PullIntentStore(tmp_path)
    wrote = store.write(_intent(restarts=1))
    assert wrote is True
    before = store.path.read_bytes()

    def _boom(dest: Path, write_to: Any, **kwargs: Any) -> None:
        raise OSError("disk exploded mid-write")

    monkeypatch.setattr(pull_intent_module, "atomic_write_simple", _boom)
    wrote_after_boom = store.write(_intent(restarts=2))
    assert wrote_after_boom is False
    assert store.path.read_bytes() == before


def test_write_failure_on_unwritable_root_returns_false(tmp_path: Path) -> None:
    # lake_root's parent dir chain blocked by a plain FILE → mkdir raises OSError.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file, not dir", encoding="utf-8")
    store = PullIntentStore(blocker / "lake")
    wrote = store.write(_intent())
    assert wrote is False


def test_load_missing_is_none(tmp_path: Path) -> None:
    assert PullIntentStore(tmp_path).load() is None


def test_load_directory_is_none_and_never_raises(tmp_path: Path) -> None:
    """A DIRECTORY at the intent path is a non-regular inode → the corrupt-arm
    ``None`` naming the shape (pre-round-7 it folded via the read's OSError —
    same answer); the best-effort delete cannot unlink a directory, which the
    swallowed ``pull_intent.clear_failed`` already covers."""
    store = PullIntentStore(tmp_path)
    store.path.mkdir()
    with capture_logs() as logs:
        assert store.load() is None
    corrupt = [entry for entry in logs if entry["event"] == "pull_intent.corrupt"]
    assert len(corrupt) == 1
    assert corrupt[0]["shape"] == "not_regular"
    assert store.path.is_dir()  # unlink cannot remove it — logged, never raised


def test_load_fifo_is_none_deleted_no_hang(tmp_path: Path) -> None:
    """[F/B — refuter round 7 P-1] a FIFO planted at the intent path reads as
    corrupt (``None`` + one WARN naming the shape) and is DELETED (healed),
    WITHOUT blocking. Fails-before (executed at 68d18dea in a bounded
    subprocess): the plain ``read_bytes`` blocked FOREVER — the launcher's
    held-mutex ``prior_intent = store.load()`` snapshot wedged a REAL
    /pull-pipeline click's request thread with no exception to catch."""
    store = PullIntentStore(tmp_path)
    os.mkfifo(store.path)
    with _hang_watchdog(), capture_logs() as logs:
        assert store.load() is None
    corrupt = [entry for entry in logs if entry["event"] == "pull_intent.corrupt"]
    assert len(corrupt) == 1
    assert corrupt[0]["shape"] == "not_regular"
    assert "not a regular file" in str(corrupt[0]["detail"])
    assert not store.path.exists(), "the planted FIFO is unlinked — the path heals"


def test_load_devzero_symlink_is_none_unlinks_link_only(tmp_path: Path) -> None:
    """[F/B — refuter round 7 P-1] a symlink to ``/dev/zero`` (infinite-read
    character device) reads as corrupt without reading forever; the
    best-effort delete removes the LINK, never its target."""
    store = PullIntentStore(tmp_path)
    store.path.symlink_to("/dev/zero")
    with _hang_watchdog(), capture_logs() as logs:
        assert store.load() is None
    corrupt = [entry for entry in logs if entry["event"] == "pull_intent.corrupt"]
    assert len(corrupt) == 1
    assert corrupt[0]["shape"] == "not_regular"
    assert not store.path.is_symlink(), "the LINK is unlinked"
    assert Path("/dev/zero").exists(), "the target is untouched"


def test_load_dangling_symlink_is_absent_no_warn(tmp_path: Path) -> None:
    """A DANGLING symlink opens ``FileNotFoundError`` → the normal absent
    ``None`` (no corruption WARN, nothing deleted — nothing readable ever
    existed behind it)."""
    store = PullIntentStore(tmp_path)
    store.path.symlink_to(tmp_path / "nowhere.json")
    with capture_logs() as logs:
        assert store.load() is None
    assert [entry for entry in logs if entry["event"] == "pull_intent.corrupt"] == []
    assert [entry for entry in logs if entry["event"] == "pull_intent.read_failed"] == []


def test_load_open_denied_is_read_failed_none_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permission-denied ``os.open`` (the ``read_error`` arm) keeps the
    pre-round-7 posture: ``pull_intent.read_failed`` + ``None``, the file
    KEPT (it may become readable again). Deterministic via a monkeypatched
    ``os.open`` — a real ``chmod 0`` file does not deny a root-run CI user
    (the #1614 ``test_shard_read_error`` idiom)."""
    store = PullIntentStore(tmp_path)
    store.write(_intent())
    real_open = os.open

    def _boom(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(os.fspath(path)) == store.path:
            raise PermissionError("read denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _boom)
    with capture_logs() as logs:
        assert store.load() is None
    failed = [entry for entry in logs if entry["event"] == "pull_intent.read_failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == "PermissionError"
    monkeypatch.undo()
    assert store.path.exists(), "a read_error keeps the file — never deleted"


def test_load_failure_after_open_is_read_failed_and_closes_the_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``OSError`` AFTER a successful ``os.open`` (fstat / fdopen / read on
    the OPEN descriptor) maps to the same ``read_error`` arm via the second
    handler of the owned-flag split, and the still-owned descriptor is closed
    exactly once (the #1614 fd-ownership discipline, mirrored)."""
    store = PullIntentStore(tmp_path)
    store.write(_intent())
    closed: list[int] = []
    real_close = os.close

    def _close_spy(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def _fstat_boom(fd: int) -> os.stat_result:
        raise PermissionError("fstat denied")

    monkeypatch.setattr(os, "close", _close_spy)
    monkeypatch.setattr(os, "fstat", _fstat_boom)
    with capture_logs() as logs:
        assert store.load() is None
    monkeypatch.undo()
    failed = [entry for entry in logs if entry["event"] == "pull_intent.read_failed"]
    assert len(failed) == 1
    assert len(closed) == 1  # owned-flag: closed exactly once, never leaked


def test_load_corrupt_json_is_none_and_deleted(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.load() is None
    assert not store.path.exists(), "a corrupt intent is deleted so it never re-trips"


def test_load_wrong_shape_is_none_and_deleted(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    store.path.write_text(json.dumps({"restarts": "three"}), encoding="utf-8")
    assert store.load() is None
    assert not store.path.exists()


def test_load_invalid_utf8_is_none_and_deleted(tmp_path: Path) -> None:
    """[F/B — refuter round 6 N-1 inherited note, the #1776 round-2 M1
    template] a bit-rotted intent file (ONE bad byte) reads as corrupt →
    ``None`` + deleted. Pre-fix ``read_text`` raised ``UnicodeDecodeError``
    (a ValueError subclass the OSError-only read guard missed) — executed:
    the launcher's held-mutex ``prior_intent`` snapshot 500'd a REAL
    /pull-pipeline click."""
    store = PullIntentStore(tmp_path)
    store.path.write_bytes(b'\xff{"version": 1}')
    assert store.load() is None
    assert not store.path.exists(), "a corrupt intent is deleted so it never re-trips"


def test_load_nesting_bomb_is_none_and_deleted(tmp_path: Path) -> None:
    """[refuter round 6 N-1] a deeply-nested payload's ``RecursionError``
    folds into the corrupt-file answer instead of escaping."""
    store = PullIntentStore(tmp_path)
    depth = 200_000
    store.path.write_text("[" * depth + "]" * depth, encoding="utf-8")
    assert store.load() is None
    assert not store.path.exists()


def test_load_oversized_is_none_and_deleted_before_read(tmp_path: Path) -> None:
    """[refuter round 6 N-1, the size-cap belt] an over-cap intent file is
    refused BEFORE the read. TEETH: the content is a VALID intent payload
    padded past the cap with legal JSON whitespace — were it read + parsed
    it would load — so the ``None`` answer proves the cap decided."""
    from brutex.dashboard.pull_intent import MAX_INTENT_BYTES

    store = PullIntentStore(tmp_path)
    body = json.dumps(_intent().to_payload()).encode()
    store.path.write_bytes(body + b" " * (MAX_INTENT_BYTES + 1 - len(body)))
    assert store.load() is None
    assert not store.path.exists()


def test_read_sidecar_bytes_read_is_bounded_even_if_file_grows_after_fstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[refuter round 8 S-2 — mutant L4 killer] the loader's ``read(max_bytes)``
    bound is load-bearing for the same-inode GROW-after-fstat vector (the one
    class the fstat-on-fd size cap cannot see — a swap cannot reach the open
    descriptor, but the SAME inode growing between the fstat and the read
    can): even when the fstat reports an under-cap size over a file whose
    on-disk bytes are 4x the cap, the served bytes are EXACTLY the cap —
    never the whole grown file. Deterministic via a monkeypatched ``os.fstat``
    returning a small ``st_size`` (the grow simulated by writing the big file
    first — the loader sees a small stat, then reads from the big inode);
    under the unbounded-read mutant (``handle.read()``) all 4x-cap bytes come
    back and this pin fails."""
    cap = 64
    target = tmp_path / "grow.json"
    target.write_bytes(b"x" * (cap * 4))  # the inode already holds 4x the cap
    real_fstat = os.fstat

    def _small_fstat(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        return os.stat_result(
            (
                st.st_mode,
                st.st_ino,
                st.st_dev,
                st.st_nlink,
                st.st_uid,
                st.st_gid,
                cap - 1,  # under-cap: the size gate passes, the read decides
                int(st.st_atime),
                int(st.st_mtime),
                int(st.st_ctime),
            )
        )

    monkeypatch.setattr(os, "fstat", _small_fstat)
    result = read_sidecar_bytes(target, max_bytes=cap)
    monkeypatch.undo()
    assert result.shape == "ok"
    assert result.raw is not None
    assert len(result.raw) == cap  # BOUNDED: never the 4x-cap on-disk bytes


def test_clear_is_idempotent(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    store.write(_intent())
    store.clear()
    assert not store.path.exists()
    store.clear()  # missing_ok — never raises


def test_clear_swallows_oserror(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    store.path.mkdir()
    (store.path / "child").write_text("x", encoding="utf-8")
    store.clear()  # unlink on a non-empty dir raises OSError → logged + swallowed
    assert store.path.exists()


def test_bump_restarts_increments_and_persists(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    store.write(_intent(restarts=0))
    bumped = store.bump_restarts()
    assert bumped is not None
    assert bumped.restarts == 1
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.restarts == 1


def test_bump_restarts_without_intent_is_none(tmp_path: Path) -> None:
    bumped = PullIntentStore(tmp_path).bump_restarts()
    assert bumped is None


# ── persist_intent_for_launch (the dry-run-never-persists law) ───────────────


def test_dry_run_never_persists_an_intent(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    wrote = persist_intent_for_launch(
        store,
        dry_run=True,
        symbols="NIFTY",
        symbol=None,
        from_date=None,
        to_date=None,
    )
    assert wrote is False
    assert not store.path.exists()


def test_real_launch_persists_quadruple_and_restarts(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    wrote = persist_intent_for_launch(
        store,
        dry_run=False,
        symbols=None,
        symbol="NIFTY",
        from_date="2024-01-01",
        to_date=None,
        restarts=2,
    )
    assert wrote is True
    loaded = store.load()
    assert loaded is not None
    assert (loaded.symbols, loaded.symbol, loaded.from_date, loaded.to_date) == (
        None,
        "NIFTY",
        "2024-01-01",
        None,
    )
    assert loaded.restarts == 2, "the resume path's bumped counter must survive the re-write"
    assert loaded.created_at != ""


# ── resume-banner auto-clear (DEC-RESUME-BANNER-AUTOCLEAR-001, started-gated) ─


def test_persist_intent_never_touches_the_banner(tmp_path: Path) -> None:
    # Round-3 hostile-review regression: persist_intent_for_launch runs BEFORE
    # BackgroundPullJob.start, where the launch is still only an ATTEMPT — it
    # must NOT drop the CRITICAL crash-loop banner (previously it cleared it
    # unconditionally, so a REFUSED start silently retired the banner).
    store = PullIntentStore(tmp_path)
    store.banner.set(level="critical", message="pull auto-resume HALTED: cap")
    wrote = persist_intent_for_launch(
        store,
        dry_run=False,
        symbols="NIFTY",
        symbol=None,
        from_date=None,
        to_date=None,
    )
    assert wrote is True
    assert store.banner.snapshot() == {
        "level": "critical",
        "message": "pull auto-resume HALTED: cap",
    }, "the pre-start intent write alone must never retire the banner"


def test_started_launch_autoclears_a_stale_resume_banner(tmp_path: Path) -> None:
    # The banner's own remediation is "start the pull manually from /universe";
    # following it (start() acquired the guards ⇒ started=True) must retire the
    # banner instead of leaving a stale CRITICAL up for the process lifetime.
    store = PullIntentStore(tmp_path)
    store.banner.set(level="critical", message="pull auto-resume HALTED: cap")
    cleared = clear_banner_after_start(store, dry_run=False, started=True)
    assert cleared is True
    assert store.banner.snapshot() is None, "the started real launch retires the banner"


def test_refused_start_keeps_the_resume_banner(tmp_path: Path) -> None:
    # THE round-3 finding: started=False (a pull already running, flock
    # contention, or the foreign-host lease refusal) launches NOTHING — the
    # crash-loop banner must survive the refused attempt.
    store = PullIntentStore(tmp_path)
    store.banner.set(level="critical", message="pull auto-resume HALTED: cap")
    cleared = clear_banner_after_start(store, dry_run=False, started=False)
    assert cleared is False
    assert store.banner.snapshot() == {
        "level": "critical",
        "message": "pull auto-resume HALTED: cap",
    }, "a refused start resolves no crash loop — the banner stays up"


def test_dry_run_never_clears_the_resume_banner(tmp_path: Path) -> None:
    # A synthetic pull resolves no crash loop: neither the dry-run persist
    # (no intent write) nor a dry-run start touches the banner.
    store = PullIntentStore(tmp_path)
    store.banner.set(level="critical", message="pull auto-resume HALTED: cap")
    wrote = persist_intent_for_launch(
        store,
        dry_run=True,
        symbols="NIFTY",
        symbol=None,
        from_date=None,
        to_date=None,
    )
    assert wrote is False
    assert not store.path.exists()
    cleared = clear_banner_after_start(store, dry_run=True, started=True)
    assert cleared is False
    assert store.banner.snapshot() == {
        "level": "critical",
        "message": "pull auto-resume HALTED: cap",
    }


def test_started_autoclear_is_independent_of_a_failed_intent_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed best-effort intent write never blocks the pull — the started
    # launch that follows it still retires the banner.
    store = PullIntentStore(tmp_path)
    store.banner.set(level="critical", message="pull auto-resume HALTED: cap")

    def _boom(dest: Path, write_to: Any, **kwargs: Any) -> None:
        raise OSError("disk exploded mid-write")

    monkeypatch.setattr(pull_intent_module, "atomic_write_simple", _boom)
    wrote = persist_intent_for_launch(
        store,
        dry_run=False,
        symbols=None,
        symbol="NIFTY",
        from_date=None,
        to_date=None,
    )
    assert wrote is False
    cleared = clear_banner_after_start(store, dry_run=False, started=True)
    assert cleared is True
    assert store.banner.snapshot() is None


def test_started_launch_with_no_banner_stays_silent_and_unchanged(tmp_path: Path) -> None:
    # The no-op branch: an unset banner is a silent False clear — the launch's
    # persisted intent is byte-identical to the pre-change behaviour.
    store = PullIntentStore(tmp_path)
    assert store.banner.snapshot() is None
    wrote = persist_intent_for_launch(
        store,
        dry_run=False,
        symbols="NIFTY",
        symbol=None,
        from_date="2024-01-01",
        to_date="2024-02-01",
    )
    assert wrote is True
    cleared = clear_banner_after_start(store, dry_run=False, started=True)
    assert cleared is False
    assert store.banner.snapshot() is None
    loaded = store.load()
    assert loaded is not None
    assert (loaded.symbols, loaded.from_date, loaded.to_date) == (
        "NIFTY",
        "2024-01-01",
        "2024-02-01",
    )


# ── restore_intent_after_refused_start (refuter round 3 F-1) ─────────────────


def test_refused_start_restores_the_prior_intent_verbatim(tmp_path: Path) -> None:
    # THE r3 F-1 root fix: a refused start's pre-start persist has clobbered
    # the RUNNING pull's on-disk intent — the restore puts the prior snapshot
    # back VERBATIM (scope + restarts + created_at included), so a crash in
    # the window resumes the RUNNING pull's scope, never the refused click's.
    store = PullIntentStore(tmp_path)
    prior = _intent(scope="spot-only", restarts=1)
    assert store.write(prior) is True
    # The refused click's persist (the clobber the pre-start ordering leaves).
    persist_intent_for_launch(
        store, dry_run=False, symbols=None, symbol=None, from_date=None, to_date=None
    )
    clobbered = store.load()
    assert clobbered is not None and clobbered.scope is None, "precondition: clobber landed"
    restored = restore_intent_after_refused_start(store, prior=prior, dry_run=False, started=False)
    assert restored is True
    assert store.load() == prior, "the prior intent must round-trip field-for-field"


def test_refused_start_with_no_prior_clears_the_orphan_clobber(tmp_path: Path) -> None:
    # The A2 sibling shape: a launch refused by a FOREIGN-process flock left a
    # fresh intent on disk with nothing in-process to ever clear it — a later
    # boot would "resume" a pull that never started. prior=None ⇒ clear.
    store = PullIntentStore(tmp_path)
    persist_intent_for_launch(
        store, dry_run=False, symbols="NIFTY", symbol=None, from_date=None, to_date=None
    )
    assert store.path.exists()
    restored = restore_intent_after_refused_start(store, prior=None, dry_run=False, started=False)
    assert restored is True
    assert store.load() is None
    assert not store.path.exists()


def test_refused_start_current_absent_never_resurrects(tmp_path: Path) -> None:
    # The winner's on_finally clear raced between the persist and the restore
    # (its pull FINISHED) — "no intent" is the correct durable record; writing
    # the prior back would resurrect an intent for a completed pull.
    store = PullIntentStore(tmp_path)
    prior = _intent(scope="spot-only")
    restored = restore_intent_after_refused_start(store, prior=prior, dry_run=False, started=False)
    assert restored is False
    assert store.load() is None
    assert not store.path.exists()


def test_started_launch_restores_nothing(tmp_path: Path) -> None:
    # A STARTED launch's own persisted intent is the correct durable record —
    # the restore must not touch it (the started gate).
    store = PullIntentStore(tmp_path)
    persist_intent_for_launch(
        store, dry_run=False, symbols="NIFTY", symbol=None, from_date=None, to_date=None
    )
    restored = restore_intent_after_refused_start(store, prior=None, dry_run=False, started=True)
    assert restored is False
    loaded = store.load()
    assert loaded is not None and loaded.symbols == "NIFTY"


def test_dry_run_restores_nothing(tmp_path: Path) -> None:
    # A dry-run never persisted an intent (persist_intent_for_launch's law) —
    # there is nothing to undo, and a pre-existing REAL intent must survive.
    store = PullIntentStore(tmp_path)
    prior = _intent()
    assert store.write(prior) is True
    restored = restore_intent_after_refused_start(store, prior=None, dry_run=True, started=False)
    assert restored is False
    assert store.load() == prior


def test_refused_start_restore_write_failure_degrades_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Best-effort like every store I/O: a failed restore write is logged
    # (pull_intent.write_failed + restored="write_failed") and returns False —
    # never a raise into the launch path.
    store = PullIntentStore(tmp_path)
    prior = _intent(scope="spot-only")
    assert store.write(prior) is True
    persist_intent_for_launch(
        store, dry_run=False, symbols=None, symbol=None, from_date=None, to_date=None
    )

    def _boom(dest: Path, write_to: Any, **kwargs: Any) -> None:
        raise OSError("disk exploded mid-restore")

    monkeypatch.setattr(pull_intent_module, "atomic_write_simple", _boom)
    restored = restore_intent_after_refused_start(store, prior=prior, dry_run=False, started=False)
    assert restored is False


# ── probe_pull_singleflight (the REAL flock against a tmp lake root) ──────────


def test_probe_free_lock_is_true_and_releases(tmp_path: Path) -> None:
    probed_free = probe_pull_singleflight(tmp_path)
    assert probed_free is True
    # The probe released — a real acquire succeeds afterwards.
    with pull_singleflight_lock(tmp_path):
        pass


def test_probe_held_lock_is_false(tmp_path: Path) -> None:
    with pull_singleflight_lock(tmp_path):
        probed_held = probe_pull_singleflight(tmp_path)
        assert probed_held is False


def test_probe_error_reads_not_acquirable(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file, not dir", encoding="utf-8")
    # ``.locks`` mkdir under a FILE raises → when uncertain, do not fire.
    probed_blocked = probe_pull_singleflight(blocker)
    assert probed_blocked is False


# ── pull_intent_resume_active (the ONE decision predicate) ───────────────────


def test_active_requires_intent_present(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    assert pull_intent_resume_active(store, env={}) is False
    store.write(_intent())
    assert pull_intent_resume_active(store, env={}) is True


def test_active_escape_hatch_off_short_circuits(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    store.write(_intent())
    assert pull_intent_resume_active(store, env={PULL_INTENT_RESUME_ENV: "0"}) is False


# ── restart_cap_message + emit_restart_cap_alert ─────────────────────────────


def test_cap_message_names_scope_and_dates() -> None:
    msg = restart_cap_message(_intent(restarts=2), MAX_AUTO_RESTARTS)
    assert "2 automatic restart(s)" in msg
    assert "NIFTY,BANKNIFTY" in msg
    assert "2024-01-01" in msg
    assert PULL_INTENT_FILENAME in msg


def test_cap_message_scope_fallbacks() -> None:
    single = restart_cap_message(_intent(symbols=None, symbol="NIFTY"), 2)
    assert "NIFTY" in single
    universe = restart_cap_message(
        _intent(symbols=None, symbol=None, from_date=None, to_date=None), 2
    )
    assert "whole universe" in universe
    assert "from: default" in universe


class _SpyAlerter:
    """Records ``pull_halted`` reasons (the SweepAlerter double)."""

    reasons: ClassVar[list[str]] = []

    def pull_halted(self, *, reason: str) -> bool:
        _SpyAlerter.reasons.append(reason)
        return True


class _BoomAlerter:
    """A SweepAlerter double whose ``pull_halted`` raises."""

    def pull_halted(self, *, reason: str) -> bool:
        raise RuntimeError("telegram exploded")


def test_emit_restart_cap_alert_fires_pull_halted(monkeypatch: pytest.MonkeyPatch) -> None:
    from brutex.notifications import sweep_alerts

    _SpyAlerter.reasons = []
    monkeypatch.setattr(sweep_alerts, "SweepAlerter", _SpyAlerter)
    emit_restart_cap_alert(_intent(restarts=2))
    assert _SpyAlerter.reasons == ["pull_restart_cap"]


def test_emit_restart_cap_alert_swallows_alerter_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brutex.notifications import sweep_alerts

    monkeypatch.setattr(sweep_alerts, "SweepAlerter", _BoomAlerter)
    emit_restart_cap_alert(_intent(restarts=2))  # must not raise


# ── run_auto_resume — the whole boot decision ────────────────────────────────


def _spies() -> tuple[list[PullIntent], list[PullIntent]]:
    return [], []


def test_run_no_intent_short_circuits(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    launched: list[PullIntent] = []
    probed: list[bool] = []

    def _probe() -> bool:
        probed.append(True)
        return True

    action = run_auto_resume(store, launch=launched.append, probe_singleflight=_probe)
    assert action is ResumeAction.NO_INTENT
    assert launched == []
    assert probed == [], "no intent ⇒ the flock is never even probed"


def test_run_peer_active_burns_no_token_and_never_launches(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    store.write(_intent(restarts=1))
    launched: list[PullIntent] = []
    action = run_auto_resume(store, launch=launched.append, probe_singleflight=lambda: False)
    assert action is ResumeAction.PEER_ACTIVE
    assert launched == []
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.restarts == 1, "a healthy peer boot must not burn a restart token"
    assert store.banner.snapshot() is None


def test_run_fire_bumps_before_launch_and_threads_count(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    store.write(_intent(restarts=0))
    seen_on_disk: list[int] = []
    launched: list[PullIntent] = []

    def _launch(intent: PullIntent) -> None:
        on_disk = store.load()
        assert on_disk is not None
        seen_on_disk.append(on_disk.restarts)
        launched.append(intent)

    action = run_auto_resume(store, launch=_launch, probe_singleflight=lambda: True)
    assert action is ResumeAction.FIRED
    assert [i.restarts for i in launched] == [1]
    assert seen_on_disk == [1], "the token is burnt (persisted) BEFORE the launch"
    assert store.banner.snapshot() is None


def test_run_cap_boundary_second_restart_allowed_third_refused(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    store.write(_intent(restarts=1))
    launched: list[PullIntent] = []
    alerts: list[PullIntent] = []
    # restarts=1 < cap=2 → the SECOND automatic restart fires…
    action = run_auto_resume(
        store, launch=launched.append, probe_singleflight=lambda: True, alert=alerts.append
    )
    assert action is ResumeAction.FIRED
    assert [i.restarts for i in launched] == [2]
    # …and the next boot (restarts now 2 == cap) refuses.
    action = run_auto_resume(
        store, launch=launched.append, probe_singleflight=lambda: True, alert=alerts.append
    )
    assert action is ResumeAction.OVER_CAP
    assert len(launched) == 1
    assert len(alerts) == 1


def test_run_over_cap_alerts_banners_and_keeps_intent(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    store.write(_intent(restarts=MAX_AUTO_RESTARTS))
    launched, alerts = _spies()
    action = run_auto_resume(
        store, launch=launched.append, probe_singleflight=lambda: True, alert=alerts.append
    )
    assert action is ResumeAction.OVER_CAP
    assert launched == []
    assert [i.restarts for i in alerts] == [MAX_AUTO_RESTARTS]
    banner = store.banner.snapshot()
    assert banner is not None
    assert banner["level"] == "critical"
    assert "HALTED" in banner["message"]
    assert store.path.exists(), "the over-cap intent is KEPT for forensics"


def test_run_over_cap_default_alert_is_the_critical_telegram_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PullIntentStore(tmp_path)
    store.write(_intent(restarts=MAX_AUTO_RESTARTS))
    fired: list[PullIntent] = []
    monkeypatch.setattr(pull_intent_module, "emit_restart_cap_alert", fired.append)
    action = run_auto_resume(store, launch=lambda _i: None, probe_singleflight=lambda: True)
    assert action is ResumeAction.OVER_CAP
    assert len(fired) == 1


def test_run_over_cap_raising_alert_is_swallowed(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    store.write(_intent(restarts=MAX_AUTO_RESTARTS))

    def _boom(_intent_arg: PullIntent) -> None:
        raise RuntimeError("alert exploded")

    action = run_auto_resume(
        store, launch=lambda _i: None, probe_singleflight=lambda: True, alert=_boom
    )
    assert action is ResumeAction.OVER_CAP


def test_run_bump_lost_race_reads_no_intent(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    store.write(_intent(restarts=0))

    def _probe_that_races_the_file_away() -> bool:
        store.path.unlink()
        return True

    action = run_auto_resume(
        store, launch=lambda _i: None, probe_singleflight=_probe_that_races_the_file_away
    )
    assert action is ResumeAction.NO_INTENT


def test_run_launch_valueerror_clears_intent_and_banners(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    store.write(_intent(restarts=0))

    def _stale(_intent_arg: PullIntent) -> None:
        raise ValueError("unknown selection: DEADSYM")

    action = run_auto_resume(store, launch=_stale, probe_singleflight=lambda: True)
    assert action is ResumeAction.LAUNCH_FAILED
    assert not store.path.exists(), "a deterministic validation refusal clears the intent"
    banner = store.banner.snapshot()
    assert banner is not None
    assert banner["level"] == "critical"
    assert "DEADSYM" in banner["message"]


def test_run_launch_transient_error_keeps_intent_for_next_boot(tmp_path: Path) -> None:
    store = PullIntentStore(tmp_path)
    store.write(_intent(restarts=0))

    def _transient(_intent_arg: PullIntent) -> None:
        raise RuntimeError("credentials not yet fixed")

    action = run_auto_resume(store, launch=_transient, probe_singleflight=lambda: True)
    assert action is ResumeAction.LAUNCH_FAILED
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.restarts == 1, "the intent survives (token burnt) — retried ≤ cap"
    banner = store.banner.snapshot()
    assert banner is not None
    assert "RuntimeError" in banner["message"]


def test_run_default_probe_uses_the_real_flock(tmp_path: Path) -> None:
    # probe_singleflight=None binds the REAL acquire-probe on store.lake_root:
    # a held peer flock refuses without burning a token; a free one fires.
    store = PullIntentStore(tmp_path)
    store.write(_intent(restarts=0))
    launched: list[PullIntent] = []
    with pull_singleflight_lock(tmp_path):
        held_action = run_auto_resume(store, launch=launched.append)
        assert held_action is ResumeAction.PEER_ACTIVE
    assert launched == []
    free_action = run_auto_resume(store, launch=launched.append)
    assert free_action is ResumeAction.FIRED
    assert len(launched) == 1


# ── synthesize_legacy_intent + run_legacy_resume (DEC-AUTO-RESUME-INPROCESS-001) ──


def test_synthesize_legacy_intent_is_whole_universe_fresh() -> None:
    """The synthesized legacy intent is the whole-universe default pull, restarts=0."""
    from datetime import datetime

    intent = synthesize_legacy_intent()
    assert intent.symbols is None
    assert intent.symbol is None
    assert intent.from_date is None
    assert intent.to_date is None
    assert intent.restarts == 0
    # created_at is a real UTC ISO timestamp (forensics), parseable round-trip.
    parsed = datetime.fromisoformat(intent.created_at)
    assert parsed.tzinfo is not None
    # The synthesized intent survives the store's strict payload round trip.
    assert PullIntent.from_payload(intent.to_payload()) == intent


def test_legacy_resume_fires_whole_universe_with_bumped_token(tmp_path: Path) -> None:
    """Fresh lake ⇒ synthesized intent persisted, token bumped BEFORE launch, FIRED."""
    store = PullIntentStore(tmp_path)
    launched: list[PullIntent] = []

    def _launch(intent: PullIntent) -> None:
        on_disk = store.load()
        assert on_disk is not None
        assert on_disk.restarts == 1, "the token is burnt (persisted) BEFORE the launch"
        launched.append(intent)

    action = run_legacy_resume(store, launch=_launch, probe_singleflight=lambda: True)
    assert action is ResumeAction.FIRED
    assert len(launched) == 1
    fired = launched[0]
    assert (fired.symbols, fired.symbol, fired.from_date, fired.to_date) == (
        None,
        None,
        None,
        None,
    ), "the legacy resume re-fires the WHOLE-universe pipeline pull"
    assert fired.restarts == 1


def test_legacy_resume_peer_active_skips_quietly_and_cleans_synth_intent(
    tmp_path: Path,
) -> None:
    """A held peer flock ⇒ PEER_ACTIVE — no launch, no banner, and the intent
    THIS call synthesized is cleaned up (the next boot re-decides from the
    ledger, never an unconditional intent-arm whole-universe re-fire)."""
    store = PullIntentStore(tmp_path)
    launched: list[PullIntent] = []
    action = run_legacy_resume(store, launch=launched.append, probe_singleflight=lambda: False)
    assert action is ResumeAction.PEER_ACTIVE
    assert launched == []
    assert store.load() is None, "the synthesized intent must not outlive the skip"
    assert store.banner.snapshot() is None


def test_legacy_resume_peer_active_preserves_operator_intent_written_in_window(
    tmp_path: Path,
) -> None:
    """Boot-window race regression: an operator intent persisted BETWEEN the
    synthetic write and the PEER_ACTIVE cleanup is PRESERVED — ownership is
    re-derived at cleanup time (exact equality against the instance this call
    wrote), never assumed from entry-time state, so the cleanup can never
    delete the operator's click."""
    store = PullIntentStore(tmp_path)
    operator_intent = _intent(symbols="NIFTY", restarts=0)

    def _probe() -> bool:
        # The race window: our synthetic intent is already on disk; the
        # operator's REAL click lands before the PEER_ACTIVE cleanup runs.
        assert store.load() is not None, "the synthetic intent was persisted first"
        wrote = store.write(operator_intent)
        assert wrote, "the operator's click must persist inside the race window"
        return False  # a peer pull holds the flock ⇒ PEER_ACTIVE

    launched: list[PullIntent] = []
    action = run_legacy_resume(store, launch=launched.append, probe_singleflight=_probe)
    assert action is ResumeAction.PEER_ACTIVE
    assert launched == []
    assert store.load() == operator_intent, "the operator's intent survives the cleanup"
    assert store.banner.snapshot() is None


def test_legacy_resume_peer_active_created_at_alone_defeats_a_false_match(
    tmp_path: Path,
) -> None:
    """An operator intent that HAPPENS to be whole-universe (the synthetic
    shape: all-``None`` quadruple, ``restarts=0``) but carries a DIFFERENT
    ``created_at`` must NOT false-match the synthetic intent — ``created_at``
    participates in the frozen-dataclass equality, so it is preserved."""
    store = PullIntentStore(tmp_path)
    operator_intent = _intent(
        symbols=None,
        symbol=None,
        from_date=None,
        to_date=None,
        restarts=0,
        created_at="2020-01-01T00:00:00+00:00",
    )

    def _probe() -> bool:
        wrote = store.write(operator_intent)
        assert wrote, "the operator's click must persist inside the race window"
        return False

    launched: list[PullIntent] = []
    action = run_legacy_resume(store, launch=launched.append, probe_singleflight=_probe)
    assert action is ResumeAction.PEER_ACTIVE
    assert launched == []
    assert store.load() == operator_intent, (
        "a whole-universe operator intent with its own created_at is NOT ours"
    )


def test_legacy_resume_peer_active_leaves_preexisting_intent_untouched(
    tmp_path: Path,
) -> None:
    """PEER_ACTIVE over a PRE-existing intent burns no token and deletes nothing."""
    store = PullIntentStore(tmp_path)
    store.write(_intent(restarts=1))
    launched: list[PullIntent] = []
    action = run_legacy_resume(store, launch=launched.append, probe_singleflight=lambda: False)
    assert action is ResumeAction.PEER_ACTIVE
    assert launched == []
    persisted = store.load()
    assert persisted is not None
    assert persisted == _intent(restarts=1), "a pre-existing intent survives verbatim"
    assert store.banner.snapshot() is None


def test_legacy_resume_over_cap_alerts_banners_and_never_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A surviving over-cap intent refuses through the legacy arm: ONE CRITICAL
    alert (the default `emit_restart_cap_alert` binding) + red banner, no launch,
    intent KEPT for forensics — the inherited cap surface, verbatim."""
    store = PullIntentStore(tmp_path)
    store.write(_intent(symbols=None, symbol=None, restarts=MAX_AUTO_RESTARTS))
    alerts: list[PullIntent] = []
    monkeypatch.setattr(pull_intent_module, "emit_restart_cap_alert", alerts.append)
    launched: list[PullIntent] = []
    action = run_legacy_resume(store, launch=launched.append, probe_singleflight=lambda: True)
    assert action is ResumeAction.OVER_CAP
    assert launched == []
    assert len(alerts) == 1, "exactly ONE CRITICAL alert on the over-cap refusal"
    banner = store.banner.snapshot()
    assert banner is not None
    assert banner["level"] == "critical"
    assert store.path.exists(), "the over-cap intent is kept for forensics"


def test_legacy_resume_never_overwrites_a_preexisting_intent(tmp_path: Path) -> None:
    """A pre-existing intent (escape hatch OFF boots) keeps its quadruple + count."""
    store = PullIntentStore(tmp_path)
    store.write(_intent(symbols="NIFTY", restarts=1))
    launched: list[PullIntent] = []
    action = run_legacy_resume(store, launch=launched.append, probe_singleflight=lambda: True)
    assert action is ResumeAction.FIRED
    assert len(launched) == 1
    assert launched[0].symbols == "NIFTY", "the existing quadruple survives verbatim"
    assert launched[0].restarts == 2, "the existing crash-loop count keeps accruing"


def test_legacy_resume_write_failure_raises_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unpersistable synthesized intent REFUSES with RuntimeError (never silent).

    The raise lands in ``maybe_resume_on_boot``'s guard in production — one
    honest ``auto_resume.error`` + verdict ``error``, never a false ``resumed``.
    """
    store = PullIntentStore(tmp_path)

    def _boom(dest: Path, write_to: Any, **kwargs: Any) -> None:
        raise OSError("unwritable lake volume")

    monkeypatch.setattr(pull_intent_module, "atomic_write_simple", _boom)
    launched: list[PullIntent] = []
    with pytest.raises(RuntimeError, match="could not be persisted"):
        run_legacy_resume(store, launch=launched.append, probe_singleflight=lambda: True)
    assert launched == []


def test_legacy_resume_launch_valueerror_clears_intent_and_banners(tmp_path: Path) -> None:
    """The inherited ValueError-clears law: a deterministic refusal never loops."""
    store = PullIntentStore(tmp_path)

    def _launch(intent: PullIntent) -> None:
        raise ValueError("stale selection no longer resolves")

    action = run_legacy_resume(store, launch=_launch, probe_singleflight=lambda: True)
    assert action is ResumeAction.LAUNCH_FAILED
    assert store.load() is None, "a deterministic refusal clears the intent (no loop)"
    banner = store.banner.snapshot()
    assert banner is not None
    assert banner["level"] == "critical"
    assert "stale" in banner["message"]


def test_legacy_resume_transient_launch_error_keeps_intent(tmp_path: Path) -> None:
    """The inherited RuntimeError-keeps law: a transient failure retries ≤ cap."""
    store = PullIntentStore(tmp_path)

    def _launch(intent: PullIntent) -> None:
        raise RuntimeError("credentials not yet fixed at boot")

    action = run_legacy_resume(store, launch=_launch, probe_singleflight=lambda: True)
    assert action is ResumeAction.LAUNCH_FAILED
    persisted = store.load()
    assert persisted is not None
    assert persisted.restarts == 1, "the intent survives (token burnt) — retried ≤ cap"
    assert store.banner.snapshot() is not None
