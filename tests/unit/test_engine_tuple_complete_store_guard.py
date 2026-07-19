"""Unit suite for the resume store guard primitives (DEC-SWEEP-RESUME-STORE-GUARD-001).

Covers the two `brutex.engine.tuple_complete` additions in full:

* :func:`missing_tuple_artifacts` — the INTACT-ARTIFACTS PREDICATE: the
  exhaustive 8-subset presence truth table (T13 of the fix matrix), proving the
  exact sorted missing-name tuple for every combination and ``()`` iff all
  three artifacts are present.
* :func:`resume_store_guard_enabled` — the ``BRUTEX_SWEEP_RESUME_STORE_GUARD``
  kill switch parse law (T10's parse arms): unset/blank → ON (default),
  truthy/falsy literal sets via the COMMON ``env_flag_tristate`` vocabulary,
  junk → fail-loud ``ValueError`` naming the variable AND the raw value.

The predicate is stat-level BY CONTRACT — a present-but-corrupt artifact reads
INTACT (corruption is ``_sentinel_resume``'s job when the tuple re-enters);
the corrupt-content arm below pins that honest envelope.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

import pytest
from brutex.engine.trial_stats import trial_stats_sidecar_path
from brutex.engine.tuple_complete import (
    RESUME_STORE_GUARD_ENV,
    TUPLE_ARTIFACT_NAMES,
    completion_sentinel_path,
    missing_tuple_artifacts,
    resume_store_guard_enabled,
)

if TYPE_CHECKING:
    from pathlib import Path


def _store_path(tmp_path: Path) -> Path:
    return tmp_path / "NIFTY" / "1minute" / "bull_k3_00112233aabbccdd.parquet"


class TestMissingTupleArtifacts:
    @pytest.mark.parametrize(
        ("store", "sentinel", "sidecar"),
        list(itertools.product([False, True], repeat=3)),
    )
    def test_presence_truth_table(
        self, tmp_path: Path, store: bool, sentinel: bool, sidecar: bool
    ) -> None:
        """All 8 presence subsets return the exact sorted missing-name tuple."""
        path = _store_path(tmp_path)
        path.parent.mkdir(parents=True)
        if store:
            path.write_bytes(b"parquet-bytes")
        if sentinel:
            completion_sentinel_path(path).write_bytes(b"{}")
        if sidecar:
            trial_stats_sidecar_path(path).write_bytes(b"{}")
        expected = tuple(
            name
            for name, present in (
                ("sentinel", sentinel),
                ("sidecar", sidecar),
                ("store", store),
            )
            if not present
        )
        assert missing_tuple_artifacts(path) == expected
        assert expected == tuple(sorted(expected))  # the sorted-order law

    def test_intact_returns_empty_tuple(self, tmp_path: Path) -> None:
        path = _store_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")
        completion_sentinel_path(path).write_bytes(b"{}")
        trial_stats_sidecar_path(path).write_bytes(b"{}")
        assert missing_tuple_artifacts(path) == ()

    def test_missing_parent_directory_reports_all_three(self, tmp_path: Path) -> None:
        """A never-created results tree reads all three artifacts missing."""
        assert missing_tuple_artifacts(_store_path(tmp_path)) == TUPLE_ARTIFACT_NAMES

    def test_corrupt_content_still_reads_intact_the_honest_envelope(self, tmp_path: Path) -> None:
        """MISSING-only by contract: zero-byte/corrupt artifacts read INTACT.

        The gate performs stat-level existence probes ONLY — content
        verification is ``_sentinel_resume``'s job when the tuple re-enters
        (Golden Rule #10; stated in the predicate's docstring).
        """
        path = _store_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")  # zero-byte "parquet"
        completion_sentinel_path(path).write_bytes(b"not json at all")
        trial_stats_sidecar_path(path).write_bytes(b"\x00\x01")
        assert missing_tuple_artifacts(path) == ()

    def test_a_directory_at_the_store_path_is_not_a_file(self, tmp_path: Path) -> None:
        """``is_file`` (not ``exists``): a stray directory reads MISSING."""
        path = _store_path(tmp_path)
        path.mkdir(parents=True)
        completion_sentinel_path(path).write_bytes(b"{}")
        trial_stats_sidecar_path(path).write_bytes(b"{}")
        assert missing_tuple_artifacts(path) == ("store",)


class TestResumeStoreGuardEnabled:
    def test_unset_defaults_on(self) -> None:
        assert resume_store_guard_enabled(env={}) is True

    @pytest.mark.parametrize("raw", ["", "   ", "\t"])
    def test_blank_defaults_on(self, raw: str) -> None:
        assert resume_store_guard_enabled(env={RESUME_STORE_GUARD_ENV: raw}) is True

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", " TRUE ", "On"])
    def test_truthy_literals_enable(self, raw: str) -> None:
        assert resume_store_guard_enabled(env={RESUME_STORE_GUARD_ENV: raw}) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", " OFF ", "No"])
    def test_falsy_literals_disable(self, raw: str) -> None:
        assert resume_store_guard_enabled(env={RESUME_STORE_GUARD_ENV: raw}) is False

    @pytest.mark.parametrize("raw", ["maybe", "2", "enable", "0ff"])
    def test_junk_raises_naming_var_and_value(self, raw: str) -> None:
        with pytest.raises(ValueError, match=RESUME_STORE_GUARD_ENV) as exc_info:
            resume_store_guard_enabled(env={RESUME_STORE_GUARD_ENV: raw})
        assert repr(raw) in str(exc_info.value)

    def test_none_env_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RESUME_STORE_GUARD_ENV, "off")
        assert resume_store_guard_enabled() is False
        monkeypatch.delenv(RESUME_STORE_GUARD_ENV)
        assert resume_store_guard_enabled() is True
