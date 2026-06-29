"""Wiring tests for the COMMON coalescing-batched-write build/drain helpers.

DEC-WRITE-BATCH-COALESCE-001 + DEC-CLI-FETCH-ISOLATION-001 — these helpers were relocated
from ``brutex.dashboard.pull_controller`` to the COMMON
:mod:`brutex.adapters.groww.pull_pipeline` module (the single source of truth shared by the
dashboard AND CLI build sites). Every branch of the best-effort
:func:`~brutex.adapters.groww.pull_pipeline.build_write_consumer` +
:func:`~brutex.adapters.groww.pull_pipeline.drain_write_consumer`:

* a dry-run never builds a consumer (no real writes);
* the ``BRUTEX_WRITE_BATCH`` flag OFF (the default) keeps the synchronous path;
* the flag ON builds a live :class:`~brutex.data.write_consumer.BatchingWriteConsumer`;
* a construction failure is swallowed → ``None`` (binding it can never crash the pull);
* drain is a guarded no-op on ``None`` + swallows a drain error.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from typing import Any

import pytest
from brutex.adapters.groww.pull_pipeline import (
    build_write_consumer as _build_write_consumer,
)
from brutex.adapters.groww.pull_pipeline import (
    drain_write_consumer as _drain_write_consumer,
)
from brutex.data.sink import NullBarSink
from brutex.data.write_consumer import BatchingWriteConsumer


class TestBuildWriteConsumer:
    def test_dry_run_never_builds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even with the flag ON, a dry-run builds NO consumer (no real writes happen).
        monkeypatch.setenv("BRUTEX_WRITE_BATCH", "1")
        assert _build_write_consumer(NullBarSink(), dry_run=True) is None

    def test_flag_off_keeps_synchronous_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BRUTEX_WRITE_BATCH", raising=False)
        assert _build_write_consumer(NullBarSink(), dry_run=False) is None

    def test_flag_off_explicit_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_WRITE_BATCH", "0")
        assert _build_write_consumer(NullBarSink(), dry_run=False) is None

    def test_flag_on_builds_batching_consumer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_WRITE_BATCH", "1")
        consumer = _build_write_consumer(NullBarSink(), dry_run=False)
        try:
            assert isinstance(consumer, BatchingWriteConsumer)
        finally:
            _drain_write_consumer(consumer)

    def test_construction_failure_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A failure constructing the consumer yields None (never crashes the pull).
        monkeypatch.setenv("BRUTEX_WRITE_BATCH", "1")

        def _boom(*_a: Any, **_k: Any) -> BatchingWriteConsumer:
            raise RuntimeError("construction boom")

        # Patch by dotted path (single import style for the module — CodeQL
        # py/import-and-import-from); the helper resolves the symbol at call time.
        monkeypatch.setattr("brutex.data.write_consumer.BatchingWriteConsumer", _boom)
        assert _build_write_consumer(NullBarSink(), dry_run=False) is None


class TestDrainWriteConsumer:
    def test_drain_none_is_noop(self) -> None:
        _drain_write_consumer(None)  # no raise

    def test_drain_calls_consumer_drain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_WRITE_BATCH", "1")
        consumer = _build_write_consumer(NullBarSink(), dry_run=False)
        assert consumer is not None
        _drain_write_consumer(consumer)
        # A second drain is idempotent (the thread is already dead).
        _drain_write_consumer(consumer)

    def test_drain_error_is_swallowed(self) -> None:
        class _ExplodingConsumer:
            def drain(self) -> None:
                raise RuntimeError("drain boom")

        # The guarded drain logs + swallows — it must NEVER re-raise into the pull finally.
        _drain_write_consumer(_ExplodingConsumer())  # type: ignore[arg-type]
