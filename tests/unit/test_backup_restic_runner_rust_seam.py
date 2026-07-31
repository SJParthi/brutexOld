"""The BRUTEX_BACKUP_RUST seam legs for :mod:`brutex.backup.restic_runner`.

DEC-BACKUP-RUST-OPS-001. Covers the routed parse/argv wrappers — every
decision code arm (found / empty / constant-None / abstain→oracle /
raise-shaped→oracle), the refuse-poison gates, and (when the wheel is
installed) REAL-crate parity vs the retained Python oracles including the
raise-parity law (a crate abstain serves the oracle's exact exception).
The oracles stay pinned by the UNMODIFIED ``test_backup_restic_runner.py``.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from brutex.backup import backup_rust, restic_runner
from brutex.backup.errors import BackupError
from brutex.backup.restic_runner import (
    SnapshotInfo,
    build_backup_argv,
    parse_backup_summary_snapshot_id,
    parse_ls_file_paths,
    parse_ls_snapshot_time,
    parse_snapshots_latest,
)
from brutex.config.rust_strict import RUST_STRICT_ENV, RustFallbackRefused


@pytest.fixture(autouse=True)
def _fresh_seam() -> Iterator[None]:
    backup_rust.reset_backup_rust_for_tests()
    yield
    backup_rust.reset_backup_rust_for_tests()


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(backup_rust.BACKUP_RUST_ENV, "1")


@pytest.fixture
def strict_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the degrade-to-oracle contract to the mode that still HAS one.

    DEC-RUST-STRICT-NO-FALLBACK-001 ships ``BRUTEX_RUST_STRICT`` default-ON,
    where a Class-A (crate absent/stale/errored) or Class-B (crate answered
    but the RESULT is structurally refused — the ``law_error`` /
    ``result_refused`` reasons this file's poison cases induce) fall-back
    RAISES :class:`RustFallbackRefused` at the seam's choke point instead of
    serving the Python oracle.  The degrade machinery is not deleted — it
    stays reachable through the documented ``BRUTEX_RUST_STRICT=0`` kill
    switch, and THAT is the mode the tests requesting this fixture describe.
    The strict-ON refusal for the SAME induced poison is pinned positively by
    :class:`TestStrictModeRefusesTheFallback`, so no coverage is traded away.

    Uses the SHARED ``monkeypatch`` (never a module-scoped
    ``MonkeyPatch.context()``) so set/unset ordering with sibling env pins in
    this file stays deterministic.
    """
    monkeypatch.setenv(RUST_STRICT_ENV, "0")


def _install_fake(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    module = types.ModuleType("brutex_ops")
    for name in backup_rust._CRATE_API:
        setattr(module, name, overrides.get(name, lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, "brutex_ops", module)


SUMMARY_OK = '{"message_type":"summary","snapshot_id":"abc123"}\n'
SNAPSHOTS_OK = '[{"short_id":"aaa","time":"2026-01-01T05:30:00+05:30"}]'
LS_OK = '{"time":"2026-01-01T00:00:00Z"}\n{"type":"file","path":"/a"}\n{"type":"dir","path":"/d"}\n'


class TestOffArmIdentity:
    def test_off_serves_oracles_crate_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The explicit "0" kill switch (retargeted from the pre-flip unset
        # arm — DEC-RUST-DEFAULT-ON-WAVE-001); assertions unchanged.
        monkeypatch.setenv(backup_rust.BACKUP_RUST_ENV, "0")
        monkeypatch.setitem(sys.modules, "brutex_ops", None)  # the bomb
        assert parse_backup_summary_snapshot_id(SUMMARY_OK) == "abc123"
        assert parse_snapshots_latest("[]") is None
        assert parse_ls_file_paths(LS_OK) == ("/a",)
        assert parse_ls_snapshot_time(LS_OK) == datetime(2026, 1, 1, tzinfo=UTC)
        assert build_backup_argv("restic", [Path("/lake")], excludes=["x"], tags=["t"]) == (
            "restic",
            "backup",
            "/lake",
            "--exclude",
            "x",
            "--exclude-caches",
            "--tag",
            "t",
            "--host",
            "brutex",
            "--json",
        )
        assert backup_rust.disabled_reason() is None


class TestSummaryIdRouting:
    def test_found_code_serves_the_crate_id(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _install_fake(monkeypatch, parse_backup_summary_snapshot_id=lambda stdout: (1, "abc123"))
        assert parse_backup_summary_snapshot_id(SUMMARY_OK) == "abc123"
        assert backup_rust.disabled_reason() is None

    def test_oracle_code_serves_the_oracle_raise_verbatim(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _install_fake(monkeypatch, parse_backup_summary_snapshot_id=lambda stdout: (0, None))
        with pytest.raises(BackupError, match="carries no summary snapshot_id"):
            parse_backup_summary_snapshot_id('{"message_type":"status"}\n')
        assert backup_rust.disabled_reason() is None

    @pytest.mark.parametrize("decision", [(1, None), (1, ""), (1, 42), (2, "x"), "junk-shape"])
    def test_poisoned_decisions_are_refused_to_the_oracle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        flag_on: None,
        strict_off: None,
        decision: object,
    ) -> None:
        _install_fake(monkeypatch, parse_backup_summary_snapshot_id=lambda stdout: decision)
        assert parse_backup_summary_snapshot_id(SUMMARY_OK) == "abc123"
        assert backup_rust.disabled_reason() is None


class TestSnapshotsLatestRouting:
    def test_empty_code_serves_none(self, monkeypatch: pytest.MonkeyPatch, flag_on: None) -> None:
        _install_fake(monkeypatch, parse_snapshots_latest_decision=lambda stdout: (1, None, None))
        assert parse_snapshots_latest("[]") is None

    def test_ok_code_parses_the_time_python_side(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _install_fake(
            monkeypatch,
            parse_snapshots_latest_decision=lambda stdout: (
                2,
                "aaa",
                "2026-01-01T05:30:00+05:30",
            ),
        )
        info = parse_snapshots_latest(SNAPSHOTS_OK)
        assert info == SnapshotInfo(
            snapshot_id="aaa", time_utc=datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        )

    def test_ok_code_with_bad_time_raises_the_oracle_error(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        stdout = '[{"short_id":"aaa","time":"not-a-time"}]'
        _install_fake(
            monkeypatch,
            parse_snapshots_latest_decision=lambda s: (2, "aaa", "not-a-time"),
        )
        with pytest.raises(BackupError, match="unparseable"):
            parse_snapshots_latest(stdout)
        assert backup_rust.disabled_reason() is None, "matching raises are a burn-in MATCH"

    def test_oracle_code_reruns_the_python_raise(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _install_fake(monkeypatch, parse_snapshots_latest_decision=lambda stdout: (0, None, None))
        with pytest.raises(BackupError, match="not a JSON array"):
            parse_snapshots_latest('{"a":1}')

    @pytest.mark.parametrize(
        "decision",
        [(2, None, "t"), (2, "", "t"), (2, "id", None), (2, "id", 42), (7, None, None), "junk"],
    )
    def test_poisoned_decisions_are_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        flag_on: None,
        strict_off: None,
        decision: object,
    ) -> None:
        _install_fake(monkeypatch, parse_snapshots_latest_decision=lambda s: decision)
        assert parse_snapshots_latest(SNAPSHOTS_OK) == SnapshotInfo(
            snapshot_id="aaa", time_utc=datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        )
        assert backup_rust.disabled_reason() is None


class TestLsRouting:
    def test_paths_list_crosses_as_a_tuple(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _install_fake(monkeypatch, parse_ls_file_paths=lambda stdout: ["/a"])
        assert parse_ls_file_paths(LS_OK) == ("/a",)

    def test_paths_abstain_serves_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _install_fake(monkeypatch, parse_ls_file_paths=lambda stdout: None)
        assert parse_ls_file_paths(LS_OK) == ("/a",)

    def test_paths_poison_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None, strict_off: None
    ) -> None:
        _install_fake(monkeypatch, parse_ls_file_paths=lambda stdout: ["/a", 42])
        assert parse_ls_file_paths(LS_OK) == ("/a",)
        assert backup_rust.disabled_reason() is None

    @pytest.mark.parametrize("code", [1, 2])
    def test_time_constant_none_codes(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None, code: int
    ) -> None:
        _install_fake(monkeypatch, parse_ls_snapshot_time_decision=lambda s: (code, None))
        stdout = '{"time":42}\n' if code == 2 else '{"type":"file","path":"/a"}\n'
        assert parse_ls_snapshot_time(stdout) is None

    def test_time_string_parses_python_side(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _install_fake(
            monkeypatch,
            parse_ls_snapshot_time_decision=lambda s: (3, "2026-01-01T00:00:00Z"),
        )
        assert parse_ls_snapshot_time(LS_OK) == datetime(2026, 1, 1, tzinfo=UTC)

    def test_time_string_unparseable_falls_back_to_none(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        stdout = '{"time":"junk"}\n'
        _install_fake(monkeypatch, parse_ls_snapshot_time_decision=lambda s: (3, "junk"))
        assert parse_ls_snapshot_time(stdout) is None

    def test_time_abstain_serves_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        _install_fake(monkeypatch, parse_ls_snapshot_time_decision=lambda s: (0, None))
        assert parse_ls_snapshot_time(LS_OK) == datetime(2026, 1, 1, tzinfo=UTC)

    def test_time_poison_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None, strict_off: None
    ) -> None:
        _install_fake(monkeypatch, parse_ls_snapshot_time_decision=lambda s: (3, 42))
        assert parse_ls_snapshot_time(LS_OK) == datetime(2026, 1, 1, tzinfo=UTC)
        assert backup_rust.disabled_reason() is None


class TestArgvRouting:
    def test_fake_argv_crosses_as_a_tuple(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        expected = list(
            restic_runner._build_backup_argv_python(
                "restic", [Path("/lake")], excludes=["x"], tags=["t"]
            )
        )
        _install_fake(monkeypatch, build_backup_argv=lambda *a: expected)
        assert build_backup_argv("restic", [Path("/lake")], excludes=["x"], tags=["t"]) == tuple(
            expected
        )

    def test_argv_poison_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None, strict_off: None
    ) -> None:
        _install_fake(monkeypatch, build_backup_argv=lambda *a: "not-a-list")
        assert build_backup_argv("restic", [], excludes=[], tags=[]) == (
            "restic",
            "backup",
            "--exclude-caches",
            "--host",
            "brutex",
            "--json",
        )
        assert backup_rust.disabled_reason() is None

    def test_str_tuple_thunk_poison_message(self) -> None:
        with pytest.raises(TypeError, match="not a list of str"):
            restic_runner._str_tuple_thunk(("tuple", "not", "list"))()


#: (function, input) parity matrix — value AND raise arms, incl.
#: serde-dialect abstains (NaN) that must serve the oracle verbatim.
_REAL_WHEEL_CASES = [
    (parse_backup_summary_snapshot_id, SUMMARY_OK),
    (parse_backup_summary_snapshot_id, '{"message_type":"status"}\n'),
    (parse_backup_summary_snapshot_id, '{"x": NaN}\n' + SUMMARY_OK),
    (parse_snapshots_latest, "[]"),
    (parse_snapshots_latest, SNAPSHOTS_OK),
    (parse_snapshots_latest, '[{"id":"full","time":"2026-01-01T00:00:00Z"}]'),
    (parse_snapshots_latest, "not json"),
    (parse_snapshots_latest, '{"a":1}'),
    (parse_snapshots_latest, "[1]"),
    (parse_snapshots_latest, '[{"id":""}]'),
    (parse_snapshots_latest, '[{"id":"x","time":42}]'),
    (parse_snapshots_latest, '[{"id":"x","time":"naive-time"}]'),
    (parse_ls_file_paths, LS_OK),
    (parse_ls_file_paths, "Infinity\n"),
    (parse_ls_snapshot_time, LS_OK),
    (parse_ls_snapshot_time, '{"time":42}\n'),
    (parse_ls_snapshot_time, '{"type":"file","path":"/a"}\n'),
    (parse_ls_snapshot_time, '{"time":"junk"}\n'),
]


class TestRealWheelParity:
    @pytest.fixture(autouse=True)
    def _require_wheel(self) -> None:
        pytest.importorskip("brutex_ops")

    @pytest.mark.parametrize(("func", "stdout"), _REAL_WHEEL_CASES)
    def test_flag_on_matches_the_oracle_outcome(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None, func: object, stdout: str
    ) -> None:
        oracle_map = {
            parse_backup_summary_snapshot_id: restic_runner._parse_backup_summary_snapshot_id_python,
            parse_snapshots_latest: restic_runner._parse_snapshots_latest_python,
            parse_ls_file_paths: restic_runner._parse_ls_file_paths_python,
            parse_ls_snapshot_time: restic_runner._parse_ls_snapshot_time_python,
        }
        oracle = oracle_map[func]  # type: ignore[index]
        try:
            expected: object = ("ok", oracle(stdout))
        except BackupError as exc:
            expected = ("raise", type(exc).__name__, str(exc))
        try:
            actual: object = ("ok", func(stdout))  # type: ignore[operator]
        except BackupError as exc:
            actual = ("raise", type(exc).__name__, str(exc))
        assert actual == expected
        assert backup_rust.disabled_reason() is None

    def test_argv_parity_real_wheel(self, monkeypatch: pytest.MonkeyPatch, flag_on: None) -> None:
        argv = build_backup_argv(
            "restic",
            [Path("/lake"), Path("/audit dir")],
            excludes=[".locks", "*.tmp"],
            tags=["brutex", "pull-quiet"],
        )
        assert argv == restic_runner._build_backup_argv_python(
            "restic",
            [Path("/lake"), Path("/audit dir")],
            excludes=[".locks", "*.tmp"],
            tags=["brutex", "pull-quiet"],
        )
        assert backup_rust.disabled_reason() is None


class TestStrictModeRefusesTheFallback:
    """The no-fallback guarantee, pinned POSITIVELY on all four routed surfaces.

    DEC-RUST-STRICT-NO-FALLBACK-001.  Each test induces the SAME poisoned
    crate decision its ``strict_off`` twin above induces, but leaves
    ``BRUTEX_RUST_STRICT`` at its shipped default (ON) and asserts the seam
    RAISES rather than quietly serving the Python oracle.  Without these the
    ``strict_off`` pins above would be a coverage trade, not a relocation.

    Two reason tokens reach the choke point from this facade: a poison caught
    while ``_compute`` maps the decision reports ``law_error``; a poison
    caught while the returned thunk materialises reports ``result_refused``.
    Both are refused unconditionally — the ``match=`` substrings below are the
    verified tokens for each surface, not a guess.
    """

    def test_summary_id_poison_raises_instead_of_serving_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)  # shipped default = ON
        _install_fake(monkeypatch, parse_backup_summary_snapshot_id=lambda stdout: (2, "x"))
        with pytest.raises(RustFallbackRefused, match="law_error"):
            parse_backup_summary_snapshot_id(SUMMARY_OK)

    def test_snapshots_latest_poison_raises_instead_of_serving_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)  # shipped default = ON
        _install_fake(monkeypatch, parse_snapshots_latest_decision=lambda s: (2, None, "t"))
        with pytest.raises(RustFallbackRefused, match="law_error"):
            parse_snapshots_latest(SNAPSHOTS_OK)

    def test_ls_paths_poison_raises_instead_of_serving_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)  # shipped default = ON
        _install_fake(monkeypatch, parse_ls_file_paths=lambda stdout: ["/a", 42])
        with pytest.raises(RustFallbackRefused, match="result_refused"):
            parse_ls_file_paths(LS_OK)

    def test_ls_time_poison_raises_instead_of_serving_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)  # shipped default = ON
        _install_fake(monkeypatch, parse_ls_snapshot_time_decision=lambda s: (3, 42))
        with pytest.raises(RustFallbackRefused, match="law_error"):
            parse_ls_snapshot_time(LS_OK)

    def test_argv_poison_raises_instead_of_serving_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, flag_on: None
    ) -> None:
        monkeypatch.delenv(RUST_STRICT_ENV, raising=False)  # shipped default = ON
        _install_fake(monkeypatch, build_backup_argv=lambda *a: "not-a-list")
        with pytest.raises(RustFallbackRefused, match="result_refused"):
            build_backup_argv("restic", [], excludes=[], tags=[])
