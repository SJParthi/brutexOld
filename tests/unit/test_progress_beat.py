"""Unit tests for the pull-id-keyed liveness-beat seam.

DEC-PULL-CONTENTION-ANTISTALL-001 GAP #15 (the seam) + DEC-PULL-BEAT-PERRUN-001
(per-run keyed channels). :mod:`brutex.observability.progress_beat` is the decoupled
seam the bounded partition lock-wait + the rate pacer fire into so a
slow-but-progressing pull keeps its watchdog heartbeat alive. 100% line+branch:
register → emit → unregister on the LEGACY ("") channel (the six pre-keying pins,
kept UNMODIFIED — the back-compat contract), the keyed routing matrix (exact hit /
outward-only fallback / silent-empty / drop-WARN once per register-epoch), the
:func:`reset_progress_beats` test seam, and the best-effort swallow of a raising
beat (never crashes the worker).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import pytest
from brutex.observability import progress_beat as pb
from brutex.observability.correlation import pull_id_scope
from brutex.observability.progress_beat import (
    LEGACY_CHANNEL,
    emit_progress_beat,
    register_progress_beat,
    reset_progress_beats,
    unregister_progress_beat,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Each test starts + ends with an EMPTY registry — keyed AND legacy channels.

    Repointed to :func:`reset_progress_beats` (DEC-PULL-BEAT-PERRUN-001): the old bare
    ``unregister_progress_beat()`` clears only the legacy channel, so a keyed
    registration leaked by a test body would bleed into the next test.
    """
    reset_progress_beats()
    yield
    reset_progress_beats()


def _drop_warns(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "foreign_beat_dropped" in r.getMessage()]


@pytest.fixture
def warn_capture(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.WARNING, logger="brutex.observability.progress_beat")
    return caplog


def test_emit_is_noop_when_unregistered() -> None:
    """With nothing registered, emit is a cheap no-op (every non-dashboard caller)."""
    emit_progress_beat()  # must not raise


def test_register_then_emit_fires_beat() -> None:
    beats: list[int] = []
    register_progress_beat(lambda: beats.append(1))
    emit_progress_beat()
    emit_progress_beat()
    assert beats == [1, 1]


def test_unregister_stops_the_beat() -> None:
    beats: list[int] = []
    register_progress_beat(lambda: beats.append(1))
    emit_progress_beat()
    unregister_progress_beat()
    emit_progress_beat()  # no longer fires
    assert beats == [1]


def test_unregister_is_idempotent() -> None:
    unregister_progress_beat()
    unregister_progress_beat()  # no error when nothing is registered


def test_register_last_write_wins() -> None:
    a: list[int] = []
    b: list[int] = []
    register_progress_beat(lambda: a.append(1))
    register_progress_beat(lambda: b.append(1))
    emit_progress_beat()
    assert a == []
    assert b == [1]


def test_raising_beat_is_swallowed() -> None:
    """A buggy beat can never crash the worker that is merely signalling liveness."""
    calls: list[int] = []

    def _boom() -> None:
        calls.append(1)
        raise RuntimeError("buggy beat")

    register_progress_beat(_boom)
    emit_progress_beat()  # the RuntimeError is swallowed + logged, not raised
    assert calls == [1]


# ════════════════════════════════════════════════════════════════════════
# Keyed channels (DEC-PULL-BEAT-PERRUN-001) — the routing matrix
# ════════════════════════════════════════════════════════════════════════


def _counting_beat() -> tuple[list[int], pb.ProgressBeat]:
    beats: list[int] = []
    return beats, lambda: beats.append(1)


class TestKeyedRouting:
    """Exact hit / outward-only fallback / drop — the anti-masking matrix."""

    def test_keyed_registrant_own_scope_emit_fires(self) -> None:
        beats, beat = _counting_beat()
        register_progress_beat(beat, pull_id="abc123def456")
        with pull_id_scope("abc123def456"):
            emit_progress_beat()
        assert beats == [1]

    def test_keyed_registrant_explicit_arg_emit_fires(self) -> None:
        """The explicit ``pull_id`` argument routes without any ambient scope."""
        beats, beat = _counting_beat()
        register_progress_beat(beat, pull_id="abc123def456")
        emit_progress_beat(pull_id="abc123def456")
        assert beats == [1]

    def test_keyed_registrant_unscoped_emit_is_dropped_with_warn(
        self, warn_capture: pytest.LogCaptureFixture
    ) -> None:
        """An unattributed emit can never reach a keyed run — dropped + WARNed."""
        beats, beat = _counting_beat()
        register_progress_beat(beat, pull_id="abc123def456")
        emit_progress_beat()  # ambient "" → no legacy registrant → drop
        assert beats == []
        warns = _drop_warns(warn_capture)
        assert len(warns) == 1
        assert "''" in warns[0].getMessage()  # names repr(channel)
        assert "1 registered channel(s)" in warns[0].getMessage()

    def test_keyed_registrant_different_id_emit_is_dropped(
        self, warn_capture: pytest.LogCaptureFixture
    ) -> None:
        """A foreign RUN's id never routes into another run's keyed channel."""
        beats, beat = _counting_beat()
        register_progress_beat(beat, pull_id="abc123def456")
        with pull_id_scope("feedfacef00d"):
            emit_progress_beat()
        assert beats == []
        assert len(_drop_warns(warn_capture)) == 1

    def test_legacy_registrant_foreign_scope_emit_falls_back_outward(self) -> None:
        """The OUTWARD-only fallback: a keyed miss may reach the legacy slot — the
        preserved shared-channel back-compat (the CLI / pre-keying wiring)."""
        beats, beat = _counting_beat()
        register_progress_beat(beat)  # legacy ""
        with pull_id_scope("feedfacef00d"):
            emit_progress_beat()
        assert beats == [1]

    def test_keyed_hit_wins_over_legacy_fallback(self) -> None:
        """With BOTH registered, an addressed emit takes its own channel, never legacy."""
        keyed_beats, keyed = _counting_beat()
        legacy_beats, legacy = _counting_beat()
        register_progress_beat(legacy)
        register_progress_beat(keyed, pull_id="abc123def456")
        with pull_id_scope("abc123def456"):
            emit_progress_beat()
        assert keyed_beats == [1]
        assert legacy_beats == []

    def test_two_keyed_channels_no_crosstalk(self) -> None:
        beats_a, beat_a = _counting_beat()
        beats_b, beat_b = _counting_beat()
        register_progress_beat(beat_a, pull_id="aaaaaaaaaaaa")
        register_progress_beat(beat_b, pull_id="bbbbbbbbbbbb")
        with pull_id_scope("aaaaaaaaaaaa"):
            emit_progress_beat()
        with pull_id_scope("bbbbbbbbbbbb"):
            emit_progress_beat()
            emit_progress_beat()
        assert beats_a == [1]
        assert beats_b == [1, 1]

    def test_per_channel_last_write_wins(self) -> None:
        old_beats, old = _counting_beat()
        new_beats, new = _counting_beat()
        register_progress_beat(old, pull_id="abc123def456")
        register_progress_beat(new, pull_id="abc123def456")
        emit_progress_beat(pull_id="abc123def456")
        assert old_beats == []
        assert new_beats == [1]

    def test_keyed_unregister_removes_only_its_own_channel(self) -> None:
        _beats_a, beat_a = _counting_beat()
        beats_b, beat_b = _counting_beat()
        register_progress_beat(beat_a, pull_id="aaaaaaaaaaaa")
        register_progress_beat(beat_b, pull_id="bbbbbbbbbbbb")
        unregister_progress_beat(pull_id="aaaaaaaaaaaa")
        emit_progress_beat(pull_id="bbbbbbbbbbbb")
        assert beats_b == [1]  # B untouched by A's unregister
        assert "aaaaaaaaaaaa" not in pb._BEATS  # pyright: ignore[reportPrivateUsage]

    def test_empty_registry_keyed_emit_is_silent_noop(
        self, warn_capture: pytest.LogCaptureFixture
    ) -> None:
        """The CLI contract: an empty registry never warns, keyed or not."""
        with pull_id_scope("abc123def456"):
            emit_progress_beat()
        emit_progress_beat()
        assert _drop_warns(warn_capture) == []

    def test_raising_keyed_beat_is_swallowed(self) -> None:
        calls: list[int] = []

        def _boom() -> None:
            calls.append(1)
            raise RuntimeError("buggy keyed beat")

        register_progress_beat(_boom, pull_id="abc123def456")
        emit_progress_beat(pull_id="abc123def456")  # swallowed + logged, not raised
        assert calls == [1]


class TestDropWarnEpochThrottle:
    """The foreign-drop WARN fires once per register-epoch — loud, never spammy."""

    def test_warn_once_then_suppressed_within_one_epoch(
        self, warn_capture: pytest.LogCaptureFixture
    ) -> None:
        _beats, beat = _counting_beat()
        register_progress_beat(beat, pull_id="abc123def456")
        for _ in range(10):
            emit_progress_beat()  # 10 unroutable drops, one epoch
        assert len(_drop_warns(warn_capture)) == 1

    def test_warn_rearms_after_a_register_bumps_the_epoch(
        self, warn_capture: pytest.LogCaptureFixture
    ) -> None:
        _beats, beat = _counting_beat()
        register_progress_beat(beat, pull_id="abc123def456")
        emit_progress_beat()  # warn #1
        register_progress_beat(beat, pull_id="bbbbbbbbbbbb")  # epoch bump
        emit_progress_beat()  # warn #2 (re-armed)
        assert len(_drop_warns(warn_capture)) == 2

    def test_warn_rearms_after_an_unregister_bumps_the_epoch(
        self, warn_capture: pytest.LogCaptureFixture
    ) -> None:
        _beats, beat = _counting_beat()
        register_progress_beat(beat, pull_id="abc123def456")
        register_progress_beat(beat, pull_id="bbbbbbbbbbbb")
        emit_progress_beat()  # warn #1
        unregister_progress_beat(pull_id="bbbbbbbbbbbb")  # epoch bump
        emit_progress_beat()  # warn #2 (re-armed; registry still non-empty)
        assert len(_drop_warns(warn_capture)) == 2


class TestResetSeam:
    """``reset_progress_beats`` — THE sanctioned cross-test isolation seam."""

    def test_reset_clears_keyed_and_legacy_channels_silently(
        self, warn_capture: pytest.LogCaptureFixture
    ) -> None:
        legacy_beats, legacy = _counting_beat()
        keyed_beats, keyed = _counting_beat()
        register_progress_beat(legacy)
        register_progress_beat(keyed, pull_id="abc123def456")
        reset_progress_beats()
        emit_progress_beat()  # both channels cleared …
        with pull_id_scope("abc123def456"):
            emit_progress_beat()
        assert legacy_beats == []
        assert keyed_beats == []
        assert _drop_warns(warn_capture) == []  # … and cleared SILENTLY (empty registry)

    def test_reset_is_idempotent(self) -> None:
        reset_progress_beats()
        reset_progress_beats()  # no error on an already-empty registry

    def test_reset_rearms_the_drop_warn_epoch(self, warn_capture: pytest.LogCaptureFixture) -> None:
        _beats, beat = _counting_beat()
        register_progress_beat(beat, pull_id="abc123def456")
        emit_progress_beat()  # warn #1
        reset_progress_beats()
        register_progress_beat(beat, pull_id="abc123def456")
        emit_progress_beat()  # warn #2 — the epoch throttle was reset, not stuck
        assert len(_drop_warns(warn_capture)) == 2

    def test_reset_clears_the_keyed_leak_a_bare_unregister_misses(self) -> None:
        """The meta-pin behind the fixture repoint: the OLD bare unregister clears
        only the legacy channel, so a keyed registration would survive it and bleed
        into the next test — ``reset_progress_beats`` clears it."""
        _beats, beat = _counting_beat()
        register_progress_beat(beat, pull_id="abc123def456")
        unregister_progress_beat()  # the old fixture body — legacy channel only
        assert "abc123def456" in pb._BEATS  # pyright: ignore[reportPrivateUsage]
        reset_progress_beats()
        assert pb._BEATS == {}  # pyright: ignore[reportPrivateUsage]

    def test_legacy_channel_constant_is_empty_string(self) -> None:
        assert LEGACY_CHANNEL == ""


class TestHotPathFlatness:
    """Emit stays O(1) in the number of registered channels (dict lookup)."""

    def test_emit_latency_flat_one_vs_many_channels(self) -> None:
        """A generous micro-assertion (never a tight bench): 256 registered channels
        must not make an exact-hit emit an order of magnitude slower than 1."""

        def _time_emits(n_iter: int) -> float:
            start = time.perf_counter()
            for _ in range(n_iter):
                emit_progress_beat(pull_id="abc123def456")
            return time.perf_counter() - start

        _beats, beat = _counting_beat()
        register_progress_beat(beat, pull_id="abc123def456")
        _time_emits(500)  # warm-up
        one = _time_emits(5000)
        for i in range(256):
            register_progress_beat(beat, pull_id=f"chan{i:08x}")
        many = _time_emits(5000)
        # Generous flatness bound — a linear scan over 257 channels would blow it.
        assert many < one * 10 + 0.05
