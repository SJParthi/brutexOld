"""Unit tests for the per-symbol derive stamp (DEC-PREBUILD-INCREMENTAL-001).

The pure skip rule + the source token + the sidecar load/save/remove — the
primitives behind the incremental prebuild that turns a nothing-changed re-run
into O(changed symbols).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from brutex.data.derive_stamp import (
    DERIVE_STAMP_DIR,
    DeriveStamp,
    derive_stamp_path,
    incremental_prebuild_enabled,
    load_derive_stamp,
    remove_derive_stamp,
    save_derive_stamp,
    should_skip_derive,
    source_1m_token,
)


def _stamp(
    *,
    token: str = "tok",  # noqa: S107 - a content-digest fixture, not a secret
    tfs: tuple[str, ...] = ("15m", "5m"),
    include_daily: bool = True,
    daily_before: str | None = "2026-07-04",
    last_1m_ist_date: str | None = "2026-07-03",
) -> DeriveStamp:
    return DeriveStamp(
        token=token,
        tfs=tfs,
        include_daily=include_daily,
        daily_before=daily_before,
        last_1m_ist_date=last_1m_ist_date,
        written_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def _seed_1m(lake_root: Path, *, months: int = 2) -> Path:
    base = lake_root / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024"
    base.mkdir(parents=True)
    for m in range(1, months + 1):
        (base / f"{m:02d}.parquet").write_bytes(b"x" * (100 + m))
    return base


# ── env toggle ─────────────────────────────────────────────────────────────


def test_incremental_enabled_by_default() -> None:
    assert incremental_prebuild_enabled({}) is True


@pytest.mark.parametrize("value", ["0", "off", "FALSE", "No", " Off "])
def test_incremental_disabled_values(value: str) -> None:
    assert incremental_prebuild_enabled({"BRUTEX_PREBUILD_INCREMENTAL": value}) is False


@pytest.mark.parametrize("value", ["1", "on", "true", "", "anything-else"])
def test_incremental_enabled_values(value: str) -> None:
    assert incremental_prebuild_enabled({"BRUTEX_PREBUILD_INCREMENTAL": value}) is True


def test_incremental_reads_process_env_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRUTEX_PREBUILD_INCREMENTAL", "off")
    assert incremental_prebuild_enabled() is False
    monkeypatch.delenv("BRUTEX_PREBUILD_INCREMENTAL")
    assert incremental_prebuild_enabled() is True


# ── source token ───────────────────────────────────────────────────────────


def test_token_none_when_no_1m_dir(tmp_path: Path) -> None:
    assert source_1m_token(tmp_path, "NSE", "INDEX", "NIFTY") is None


def test_token_none_when_dir_empty(tmp_path: Path) -> None:
    (tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute").mkdir(parents=True)
    assert source_1m_token(tmp_path, "NSE", "INDEX", "NIFTY") is None


def test_token_stable_for_unchanged_files(tmp_path: Path) -> None:
    _seed_1m(tmp_path)
    t1 = source_1m_token(tmp_path, "NSE", "INDEX", "NIFTY")
    t2 = source_1m_token(tmp_path, "NSE", "INDEX", "NIFTY")
    assert t1 is not None and t1 == t2


def test_token_moves_on_size_change(tmp_path: Path) -> None:
    base = _seed_1m(tmp_path)
    t1 = source_1m_token(tmp_path, "NSE", "INDEX", "NIFTY")
    (base / "01.parquet").write_bytes(b"y" * 999)  # size change (an appended pull)
    assert source_1m_token(tmp_path, "NSE", "INDEX", "NIFTY") != t1


def test_token_moves_on_mtime_change_same_size(tmp_path: Path) -> None:
    # An atomic os.replace rewrite can keep the byte size — the mtime_ns still
    # moves the token (the heal/keep-first rewrite case).
    base = _seed_1m(tmp_path)
    t1 = source_1m_token(tmp_path, "NSE", "INDEX", "NIFTY")
    target = base / "01.parquet"
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert source_1m_token(tmp_path, "NSE", "INDEX", "NIFTY") != t1


def test_token_moves_on_new_file(tmp_path: Path) -> None:
    base = _seed_1m(tmp_path)
    t1 = source_1m_token(tmp_path, "NSE", "INDEX", "NIFTY")
    (base / "03.parquet").write_bytes(b"z" * 50)
    assert source_1m_token(tmp_path, "NSE", "INDEX", "NIFTY") != t1


def test_token_none_on_stat_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A file deleted mid-walk (concurrent heal quarantine) → no stable token →
    # the caller derives in full (the conservative direction, never a stale skip).
    _seed_1m(tmp_path)
    real_stat = Path.stat

    def _boom(self: Path, **kwargs: object) -> os.stat_result:
        if self.suffix == ".parquet":
            raise OSError("vanished mid-walk")
        return real_stat(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", _boom)
    assert source_1m_token(tmp_path, "NSE", "INDEX", "NIFTY") is None


# ── stamp load / save / remove ─────────────────────────────────────────────


def test_save_load_roundtrip(tmp_path: Path) -> None:
    path = derive_stamp_path(tmp_path, "NSE", "INDEX", "NIFTY")
    assert DERIVE_STAMP_DIR in str(path)
    stamp = _stamp()
    save_derive_stamp(path, stamp)
    assert load_derive_stamp(path) == stamp


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert load_derive_stamp(tmp_path / "absent.json") is None


def test_load_corrupt_json_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_derive_stamp(path) is None


def test_load_wrong_shape_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "shape.json"
    path.write_text('{"token": "t"}', encoding="utf-8")  # missing keys
    assert load_derive_stamp(path) is None


def test_save_failure_is_logged_never_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed sidecar save costs one future full re-derive — never a raise.
    def _boom(dest: Path, write_to: object, *, chmod: object = None) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("brutex.data.derive_stamp.atomic_write_simple", _boom)
    save_derive_stamp(tmp_path / "s.json", _stamp())  # must not raise
    assert not (tmp_path / "s.json").exists()


def test_remove_stamp_and_missing_is_noop(tmp_path: Path) -> None:
    path = derive_stamp_path(tmp_path, "NSE", "INDEX", "NIFTY")
    save_derive_stamp(path, _stamp())
    assert path.exists()
    remove_derive_stamp(tmp_path, "NSE", "INDEX", "NIFTY")
    assert not path.exists()
    remove_derive_stamp(tmp_path, "NSE", "INDEX", "NIFTY")  # idempotent no-op


def test_remove_stamp_oserror_is_logged_never_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(self: Path, missing_ok: bool = False) -> None:
        raise OSError("busy")

    monkeypatch.setattr(Path, "unlink", _boom)
    remove_derive_stamp(tmp_path, "NSE", "INDEX", "NIFTY")  # must not raise


# ── the pure skip rule ─────────────────────────────────────────────────────


def test_skip_requires_a_stamp() -> None:
    assert (
        should_skip_derive(None, token="tok", tfs=("5m",), include_daily=False, daily_before=None)
        is False
    )


def test_skip_requires_matching_token() -> None:
    assert (
        should_skip_derive(
            _stamp(token="old"),
            token="new",
            tfs=("15m", "5m"),
            include_daily=True,
            daily_before="2026-07-04",
        )
        is False
    )


def test_skip_requires_same_tf_set_order_insensitive() -> None:
    stamp = _stamp(tfs=("5m", "15m"), include_daily=False, daily_before=None)
    assert (
        should_skip_derive(
            stamp, token="tok", tfs=("15m", "5m"), include_daily=False, daily_before=None
        )
        is True
    )
    assert (
        should_skip_derive(
            stamp, token="tok", tfs=("15m", "5m", "60m"), include_daily=False, daily_before=None
        )
        is False
    )


def test_skip_requires_matching_include_daily() -> None:
    stamp = _stamp(include_daily=False, daily_before=None)
    assert (
        should_skip_derive(
            stamp, token="tok", tfs=("15m", "5m"), include_daily=True, daily_before="2026-07-04"
        )
        is False
    )


def test_skip_daily_same_cutoff_skips() -> None:
    stamp = _stamp(daily_before="2026-07-04", last_1m_ist_date="2026-07-04")
    assert (
        should_skip_derive(
            stamp, token="tok", tfs=("15m", "5m"), include_daily=True, daily_before="2026-07-04"
        )
        is True
    )


def test_skip_daily_later_cutoff_skips_only_when_data_already_final() -> None:
    # Stamped cutoff 07-04 with data ending 07-03 (< cutoff): every session was
    # final at stamp time — a later run (cutoff 07-05) derives identically → skip.
    final = _stamp(daily_before="2026-07-04", last_1m_ist_date="2026-07-03")
    assert (
        should_skip_derive(
            final, token="tok", tfs=("15m", "5m"), include_daily=True, daily_before="2026-07-05"
        )
        is True
    )
    # Stamped cutoff 07-04 with data ENDING 07-04 (== cutoff): the stamped run
    # EXCLUDED that forming session, so a later cutoff would finalise one more
    # daily bar — MUST re-derive (the stale-1day trap this rule closes).
    forming = _stamp(daily_before="2026-07-04", last_1m_ist_date="2026-07-04")
    assert (
        should_skip_derive(
            forming, token="tok", tfs=("15m", "5m"), include_daily=True, daily_before="2026-07-05"
        )
        is False
    )


def test_skip_daily_unknown_last_date_requires_exact_cutoff() -> None:
    unknown = _stamp(daily_before="2026-07-04", last_1m_ist_date=None)
    assert (
        should_skip_derive(
            unknown, token="tok", tfs=("15m", "5m"), include_daily=True, daily_before="2026-07-05"
        )
        is False
    )


def test_skip_daily_none_stamped_cutoff_requires_exact_match() -> None:
    # Defensive: a daily stamp with no cutoff recorded can only skip on the
    # (equally-None) exact match — never via the data-final rule.
    stamp = _stamp(daily_before=None, last_1m_ist_date="2026-07-03")
    assert (
        should_skip_derive(
            stamp, token="tok", tfs=("15m", "5m"), include_daily=True, daily_before="2026-07-05"
        )
        is False
    )
    assert (
        should_skip_derive(
            stamp, token="tok", tfs=("15m", "5m"), include_daily=True, daily_before=None
        )
        is True
    )


def test_skip_non_daily_ignores_cutoffs() -> None:
    stamp = _stamp(include_daily=False, daily_before=None, last_1m_ist_date=None)
    assert (
        should_skip_derive(
            stamp, token="tok", tfs=("5m", "15m"), include_daily=False, daily_before=None
        )
        is True
    )


# ── grounded-stamp interplay (DEC-DAILY-CUTOFF-GROUNDED-001 — NO rule change:
#    the EFFECTIVE-cutoff stamp rides the existing clauses; these adversarial
#    permutations pin that no combination yields a WRONG skip) ────────────────


class TestGroundedStampInterplay:
    def test_deferred_stamp_never_fast_path_skips_on_the_clock_verdict(self) -> None:
        # A grounded run DEFERRED the run day: it stamps the EFFECTIVE cutoff
        # (= the run day) with the data ending that same day. Every later run's
        # clock verdict (run day + 1, or beyond) fails BOTH clauses — the
        # equality (07-04 != 07-05) AND the data-final escape (07-04 < 07-04 is
        # False) — so the deferred day is honestly re-derived, never stranded.
        deferred = _stamp(daily_before="2026-07-04", last_1m_ist_date="2026-07-04")
        for later_clock in ("2026-07-05", "2026-07-06"):
            assert (
                should_skip_derive(
                    deferred,
                    token="tok",
                    tfs=("15m", "5m"),
                    include_daily=True,
                    daily_before=later_clock,
                )
                is False
            )

    def test_deferred_stamp_with_kill_switch_flip_still_rederives(self) -> None:
        # Grounding switched OFF after a deferred stamp: the next run compares
        # the pure clock iso against the deferred stamp — unequal, escape fails
        # → re-derive under the clock law (the graceful-revert path).
        deferred = _stamp(daily_before="2026-07-04", last_1m_ist_date="2026-07-04")
        assert (
            should_skip_derive(
                deferred,
                token="tok",
                tfs=("15m", "5m"),
                include_daily=True,
                daily_before="2026-07-05",
            )
            is False
        )

    def test_old_clock_stamp_read_by_a_grounded_run_skips_byte_correctly(self) -> None:
        # An OLD pre-grounding stamp finalized the day under the clock law
        # (daily_before = run day + 1, data ending the run day) and the source
        # is UNCHANGED: the grounded run's PRE-read skip check compares the
        # SAME clock iso → equality → skip. Byte-correct even if the stamped
        # run wrote a partial bar: a grounded re-derive would DEFER (write 0
        # daily bars) and deletes nothing, so skip and re-derive leave the
        # identical lake; the partial bar heals when the re-pull moves the
        # token (which invalidates this stamp).
        old_clock = _stamp(daily_before="2026-07-05", last_1m_ist_date="2026-07-04")
        assert (
            should_skip_derive(
                old_clock,
                token="tok",
                tfs=("15m", "5m"),
                include_daily=True,
                daily_before="2026-07-05",
            )
            is True
        )

    def test_completed_data_token_move_always_rederives(self) -> None:
        # The re-pull that completes a deferred day MOVES the source token —
        # the stamp is dead regardless of any cutoff arithmetic.
        deferred = _stamp(daily_before="2026-07-04", last_1m_ist_date="2026-07-04")
        assert (
            should_skip_derive(
                deferred,
                token="tok-moved",
                tfs=("15m", "5m"),
                include_daily=True,
                daily_before="2026-07-04",
            )
            is False
        )
