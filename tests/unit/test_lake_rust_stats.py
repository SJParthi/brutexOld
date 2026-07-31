"""Unit matrix for :mod:`brutex.data.lake_rust_stats` (DEC-LAKE-RUST-STATS-MANIFEST-001).

Flag parsing / import guard / burn-in autorevert / degrade set / panic belt /
refuse-poison / reset seam — every path exercised WITHOUT the wheel (fake
crate modules injected via ``sys.modules``), so the 100% line+branch gate
holds on wheel-less CI. The REAL-crate byte parity corpus lives in
``tests/golden/test_lake_stats_shard_rust_parity.py``.
"""

from __future__ import annotations

import dataclasses
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from brutex.data import lake_rust_render, lake_rust_stats
from brutex.data.lake_rust_stats import ShardRenderPayload
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Iterator

BARS_KEY = {"exchange": "NSE", "segment": "INDEX", "symbol": "NIFTY", "interval": "1minute"}


def _payload() -> ShardRenderPayload:
    return ShardRenderPayload(
        version=1,
        kind="bars",
        key=(("exchange", "NSE"),),
        identity_key=("timestamp",),
        row_count=2,
        min_ts_iso="2026-07-01T09:15:00+00:00",
        max_ts_iso="2026-07-02T09:15:00+00:00",
        files=(("2026/07.parquet", 2, "a", "b", 5, 9, None),),
        updated_utc="2026-07-07T00:00:00+00:00",
    )


def _payload_with_file(
    row: tuple[str, int, str, str, int, int, str | None],
) -> ShardRenderPayload:
    """:func:`_payload` with its ONE file row swapped — the ``_beyond_crate`` seam.

    Row layout is the writer's own tuple:
    ``(rel, rows, min_ts_iso, max_ts_iso, mtime_ns, size, raw_hash)``.
    """
    return dataclasses.replace(_payload(), files=(row,))


def _seam_call(python_body: str = "PY") -> str:
    return lake_rust_stats.render_shard_json(
        payload_factory=_payload, python_render=lambda: python_body
    )


def _fake_crate(render: object) -> types.ModuleType:
    module = types.ModuleType("brutex_lake")
    module.render_stats_shard_json = render  # type: ignore[attr-defined]
    return module


@pytest.fixture(autouse=True)
def _strict_off_degrade_lattice(monkeypatch: pytest.MonkeyPatch) -> None:
    """This module is the *degrade-lattice* suite — pin ``BRUTEX_RUST_STRICT=0``.

    DEC-RUST-STRICT-NO-FALLBACK-001 ships strict mode ON by default, so every
    class-A (crate absent / stale / errored) and class-B (structurally refused
    crate result) reason now RAISES ``RustFallbackRefused`` instead of quietly
    serving the Python oracle.  The tests in this module exist precisely to
    exercise that oracle machinery — the still-supported ``BRUTEX_RUST_STRICT=0``
    mode — so the mode is DECLARED once, here, rather than by weakening any
    assertion.  Strict-ON refusal is pinned separately in ``TestStrictRefusal``
    at the end of this module.

    Uses the SAME shared ``monkeypatch`` object as the sibling autouse fixtures
    (never its own ``MonkeyPatch.context()``): one object means one LIFO undo
    stack, so a ``TestStrictRefusal`` ``delenv`` on that same object cannot be
    restored out of order and leak the value past this module.
    """
    from brutex.config import rust_strict

    monkeypatch.setenv(rust_strict.RUST_STRICT_ENV, "0")


@pytest.fixture(autouse=True)
def _fresh_seam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Test isolation: env unset + latch/burn-in/memo reset both sides."""
    monkeypatch.delenv(lake_rust_stats.LAKE_RUST_STATS_ENV, raising=False)
    lake_rust_stats.reset_lake_rust_stats_for_tests()
    yield
    lake_rust_stats.reset_lake_rust_stats_for_tests()


class TestFlagParsing:
    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", " TRUE "])
    def test_truthy_literals_enable(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(lake_rust_stats.LAKE_RUST_STATS_ENV, raw)
        assert lake_rust_stats.lake_rust_stats_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
    def test_falsy_literals_disable_silently(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(lake_rust_stats.LAKE_RUST_STATS_ENV, raw)
        with capture_logs() as logs:
            assert lake_rust_stats.lake_rust_stats_enabled() is False
        assert logs == []

    def test_unset_and_whitespace_default_on_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Default ON per DEC-RUST-DEFAULT-ON-WAVE-001 (operator directive
        # 2026-07-23) — unset/blank resolve True silently.
        with capture_logs() as logs:
            assert lake_rust_stats.lake_rust_stats_enabled() is True
        monkeypatch.setenv(lake_rust_stats.LAKE_RUST_STATS_ENV, "   ")
        with capture_logs() as more:
            assert lake_rust_stats.lake_rust_stats_enabled() is True
        assert logs == [] and more == []

    def test_junk_fails_loud_off_and_warns_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(lake_rust_stats.LAKE_RUST_STATS_ENV, "onn")
        with capture_logs() as logs:
            assert lake_rust_stats.lake_rust_stats_enabled() is False
            assert lake_rust_stats.lake_rust_stats_enabled() is False
        warns = [entry for entry in logs if entry["event"] == "lake_rust_stats.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "flag_unrecognised"
        assert warns[0]["value"] == "onn"

    def test_env_name_and_burn_in_literals_pinned(self) -> None:
        assert lake_rust_stats.LAKE_RUST_STATS_ENV == "BRUTEX_LAKE_RUST_STATS_MANIFEST"
        assert lake_rust_stats.BURN_IN_SAMPLES == 16
        assert lake_rust_stats._CRATE_API == ("render_stats_shard_json",)


class TestResetSeam:
    def test_reset_restores_fresh_process_view(self) -> None:
        lake_rust_stats._disable("poison")
        lake_rust_stats._warn_once("stale")
        lake_rust_stats.reset_lake_rust_stats_for_tests()
        assert lake_rust_stats.rust_stats_disabled() is False
        assert lake_rust_stats._BURN_IN_REMAINING[0] == lake_rust_stats.BURN_IN_SAMPLES
        assert set() == lake_rust_stats._OBSERVABILITY_MEMO

    def test_reset_remaining_zero_is_steady_state(self) -> None:
        lake_rust_stats.reset_lake_rust_stats_for_tests(remaining=0)
        assert lake_rust_stats._BURN_IN_REMAINING[0] == 0

    def test_warn_once_suppresses_repeats(self) -> None:
        with capture_logs() as logs:
            lake_rust_stats._warn_once("reason_x", detail=1)
            lake_rust_stats._warn_once("reason_x", detail=2)
        assert len(logs) == 1
        assert logs[0]["event"] == "lake_rust_stats.fallback"

    def test_disable_latches_once_and_repeat_is_noop(self) -> None:
        with capture_logs() as logs:
            lake_rust_stats._disable("first")
            lake_rust_stats._disable("second")
        assert lake_rust_stats._DISABLED_REASON[0] == "first"
        assert len(logs) == 1
        assert logs[0]["event"] == "lake_rust_stats.rust_path_disabled"

    def test_disable_error_logs_autoreverted(self) -> None:
        with capture_logs() as logs:
            lake_rust_stats._disable("mismatch", error=True)
        assert logs[0]["event"] == "lake_rust_stats.render_autoreverted"
        assert logs[0]["log_level"] == "error"


class TestImportGuard:
    def test_absent_crate_latches_and_serves_python(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_lake", None)
        with capture_logs() as logs:
            assert _seam_call() == "PY"
        assert lake_rust_stats._DISABLED_REASON[0] == "crate_unavailable"
        assert logs[0]["event"] == "lake_rust_stats.rust_path_disabled"
        # Latched: the second call short-circuits before any import.
        assert _seam_call("PY2") == "PY2"

    def test_module_missing_api_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_lake", types.ModuleType("brutex_lake"))
        assert _seam_call() == "PY"
        assert lake_rust_stats._DISABLED_REASON[0] == "crate_unavailable"

    def test_non_callable_api_attr_latches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_lake", _fake_crate(render="not-callable"))
        assert _seam_call() == "PY"
        assert lake_rust_stats._DISABLED_REASON[0] == "crate_unavailable"

    def test_resolve_crate_success_returns_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        crate = _fake_crate(render=lambda *args: "x")
        monkeypatch.setitem(sys.modules, "brutex_lake", crate)
        assert lake_rust_stats._resolve_crate() is crate


class TestDegradePaths:
    @pytest.mark.parametrize(
        "exc",
        [ValueError("v"), RuntimeError("r"), TypeError("t"), OverflowError("o"), AttributeError()],
    )
    def test_crate_errors_degrade_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        def _boom(*args: object) -> str:
            raise exc

        monkeypatch.setitem(sys.modules, "brutex_lake", _fake_crate(render=_boom))
        with capture_logs() as logs:
            assert _seam_call() == "PY"
            assert _seam_call("PY2") == "PY2"
        warns = [entry for entry in logs if entry["event"] == "lake_rust_stats.fallback"]
        assert len(warns) == 1 and warns[0]["reason"] == "render_error"
        assert lake_rust_stats.rust_stats_disabled() is False  # degrade, not latch

    def test_payload_factory_raise_degrades_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_lake", _fake_crate(render=lambda *a: "x"))

        def _bad_payload() -> ShardRenderPayload:
            raise ValueError("payload boom")

        body = lake_rust_stats.render_shard_json(
            payload_factory=_bad_payload, python_render=lambda: "PY"
        )
        assert body == "PY"

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit, MemoryError])
    def test_escape_set_propagates_bare(
        self, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
    ) -> None:
        def _boom(*args: object) -> str:
            raise exc_type()

        monkeypatch.setitem(sys.modules, "brutex_lake", _fake_crate(render=_boom))
        with pytest.raises(exc_type):
            _seam_call()
        assert lake_rust_stats.rust_stats_disabled() is False

    def test_out_of_degrade_set_exception_latches_crate_panic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*args: object) -> str:
            raise KeyError("foreign")

        monkeypatch.setitem(sys.modules, "brutex_lake", _fake_crate(render=_boom))
        with capture_logs() as logs:
            assert _seam_call() == "PY"
        assert lake_rust_stats._DISABLED_REASON[0] == "crate_panic"
        assert logs[0]["event"] == "lake_rust_stats.rust_path_disabled"

    def test_real_polars_panic_exception_latches_via_the_belt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pl_exc = pytest.importorskip("polars.exceptions")

        def _boom(*args: object) -> str:
            raise pl_exc.PanicException("rust panicked")

        monkeypatch.setitem(sys.modules, "brutex_lake", _fake_crate(render=_boom))
        assert _seam_call() == "PY"
        assert lake_rust_stats._DISABLED_REASON[0] == "crate_panic"

    def test_non_str_result_is_refused_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_lake", _fake_crate(render=lambda *a: b"bytes"))
        with capture_logs() as logs:
            assert _seam_call() == "PY"
        warns = [entry for entry in logs if entry["event"] == "lake_rust_stats.fallback"]
        assert warns[0]["reason"] == "render_result_refused"
        assert warns[0]["result_type"] == "bytes"


class TestBeyondCrateInputs:
    """CLASS C — a legitimate Python value pyo3 cannot carry (``_beyond_crate``).

    The wheel-less twin of the golden corpus' ``TestDegradeLaws``: the SAME
    two out-of-envelope shapes (an ``int`` outside ``i64``; a ``str`` carrying
    a lone surrogate, legal in Python, illegal in Rust's UTF-8 ``String``)
    driven through the seam against a FAKE crate, so the gate's own branches
    are covered on wheel-less CI.

    The law under test is that the gate runs BEFORE the FFI: the crate is
    never called at all, so the fault-only ``render_error`` token keeps its
    honest "the crate ran and broke" meaning and the out-of-envelope input
    takes the exempt ``render_input_beyond_crate`` reason instead.
    """

    @staticmethod
    def _never(*args: object) -> str:
        """Engagement spy — a pre-FFI-gated call must never reach the crate."""
        raise AssertionError("the crate was called for an out-of-envelope input")

    @staticmethod
    def _seam(payload: ShardRenderPayload) -> str:
        return lake_rust_stats.render_shard_json(
            payload_factory=lambda: payload, python_render=lambda: "PY"
        )

    @pytest.mark.parametrize(
        ("row", "field"),
        [
            # int outside i64 — pyo3 cannot extract it into the crate's i64.
            (("2026/07.parquet", 2, "a", "b", 2**63, 9, None), "files[0].mtime_ns"),
            (("2026/07.parquet", 2, "a", "b", 5, -(2**63) - 1, None), "files[0].size"),
            # lone surrogate — legal str, illegal Rust String.
            (("bad\ud800rel", 2, "a", "b", 5, 9, None), "files[0].rel"),
            # ...including in the OPTIONAL raw_hash, whose check is skipped
            # entirely when the column is absent (the None row every other
            # test in this module uses).
            (("2026/07.parquet", 2, "a", "b", 5, 9, "bad\ud800hash"), "files[0].raw_hash"),
        ],
    )
    def test_out_of_envelope_value_routes_to_the_oracle_pre_ffi(
        self,
        monkeypatch: pytest.MonkeyPatch,
        row: tuple[str, int, str, str, int, int, str | None],
        field: str,
    ) -> None:
        monkeypatch.setitem(sys.modules, "brutex_lake", _fake_crate(render=self._never))
        with capture_logs() as logs:
            assert self._seam(_payload_with_file(row)) == "PY"
        warns = [entry for entry in logs if entry["event"] == "lake_rust_stats.fallback"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "render_input_beyond_crate"
        assert warns[0]["field"] == field
        # Degrade, never a latch: the next in-envelope call is crate-served.
        assert lake_rust_stats.rust_stats_disabled() is False

    def test_present_raw_hash_inside_the_envelope_reaches_the_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate's PASS arm with the optional column populated.

        Every other test carries ``raw_hash=None`` (the column is skipped);
        this one populates it with an in-envelope value so the check runs and
        returns clean, proving the gate lets a legitimate raw_hash through
        rather than treating presence itself as out-of-envelope.
        """
        monkeypatch.setitem(sys.modules, "brutex_lake", _fake_crate(render=lambda *a: "RUST"))
        lake_rust_stats.reset_lake_rust_stats_for_tests(remaining=0)
        row = ("2026/07.parquet", 2, "a", "b", 5, 9, "deadbeef")
        with capture_logs() as logs:
            assert self._seam(_payload_with_file(row)) == "RUST"
        assert [e for e in logs if e["event"] == "lake_rust_stats.fallback"] == []


class TestPanicBelt:
    def test_belt_matches_the_lake_rust_render_twin(self) -> None:
        """The LOCAL twin resolves the SAME homes to the SAME classes."""
        assert lake_rust_stats._panic_belt_errors() == lake_rust_render._panic_belt_errors()

    def test_belt_always_carries_exception_first(self) -> None:
        assert lake_rust_stats._panic_belt_errors()[0] is Exception

    def test_non_class_panic_attr_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = types.ModuleType("pyo3_runtime")
        fake.PanicException = "not-a-class"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)
        belt: tuple[object, ...] = lake_rust_stats._panic_belt_errors()
        assert "not-a-class" not in belt

    def test_non_baseexception_class_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = types.ModuleType("pyo3_runtime")
        fake.PanicException = type("NotAnError", (), {})  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)
        belt = lake_rust_stats._panic_belt_errors()
        assert fake.PanicException not in belt

    def test_duplicate_class_across_homes_is_deduped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pl_exc = pytest.importorskip("polars.exceptions")
        fake = types.ModuleType("pyo3_runtime")
        fake.PanicException = pl_exc.PanicException  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyo3_runtime", fake)
        belt = lake_rust_stats._panic_belt_errors()
        assert belt.count(pl_exc.PanicException) == 1


class TestBurnIn:
    def test_clean_burn_in_ships_rust_string_and_completes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "brutex_lake", _fake_crate(render=lambda *a: "SAME"))
        lake_rust_stats.reset_lake_rust_stats_for_tests(remaining=1)
        with capture_logs() as logs:
            assert _seam_call("SAME") == "SAME"
        events = [entry["event"] for entry in logs]
        assert "lake_rust_stats.engaged" in events
        assert "lake_rust_stats.burn_in_complete" in events
        assert lake_rust_stats._BURN_IN_REMAINING[0] == 0

    def test_burn_in_decrement_without_completion_stays_quiet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "brutex_lake", _fake_crate(render=lambda *a: "SAME"))
        lake_rust_stats.reset_lake_rust_stats_for_tests(remaining=2)
        with capture_logs() as logs:
            assert _seam_call("SAME") == "SAME"
        assert lake_rust_stats._BURN_IN_REMAINING[0] == 1
        assert "lake_rust_stats.burn_in_complete" not in [entry["event"] for entry in logs]

    def test_mismatch_ships_python_string_errors_and_autoreverts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "brutex_lake", _fake_crate(render=lambda *a: "RUST"))
        with capture_logs() as logs:
            assert _seam_call("PY") == "PY"  # the ORACLE string ships
        assert lake_rust_stats._DISABLED_REASON[0] == "burn_in_byte_mismatch"
        errors = [
            entry for entry in logs if entry["event"] == "lake_rust_stats.render_autoreverted"
        ]
        assert len(errors) == 1
        assert errors[0]["rust_len"] == 4 and errors[0]["python_len"] == 2
        # Latched forever after: python serves without touching the crate.
        assert _seam_call("PY3") == "PY3"

    def test_post_burn_in_ships_rust_string_without_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "brutex_lake", _fake_crate(render=lambda *a: "RUST"))
        lake_rust_stats.reset_lake_rust_stats_for_tests(remaining=0)

        def _oracle_must_not_run() -> str:
            raise AssertionError("steady state must not run the oracle")

        body = lake_rust_stats.render_shard_json(
            payload_factory=_payload, python_render=_oracle_must_not_run
        )
        assert body == "RUST"

    def test_engaged_info_logged_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_lake", _fake_crate(render=lambda *a: "SAME"))
        with capture_logs() as logs:
            assert _seam_call("SAME") == "SAME"
            assert _seam_call("SAME") == "SAME"
        assert [entry["event"] for entry in logs].count("lake_rust_stats.engaged") == 1

    def test_crate_receives_the_payload_fields_in_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[object, ...]] = []

        def _record(*args: object) -> str:
            seen.append(args)
            return "SAME"

        monkeypatch.setitem(sys.modules, "brutex_lake", _fake_crate(render=_record))
        assert _seam_call("SAME") == "SAME"
        payload = _payload()
        assert seen == [
            (
                payload.version,
                payload.kind,
                payload.key,
                payload.identity_key,
                payload.row_count,
                payload.min_ts_iso,
                payload.max_ts_iso,
                payload.files,
                payload.updated_utc,
            )
        ]


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop.

    The ``TestBenchWorkflowWiring`` precedent (#1797 pattern): the
    module:suite pair sits in the workflow's per-module ``mutmut`` loop with
    the EMPTY third (timeout) field — this deterministic pin fails if the
    pair is ever dropped. HONEST: it pins the WIRING only; the weekly run
    executes on its own schedule.
    """

    _WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        text = self._WORKFLOW.read_text(encoding="utf-8")
        assert '"brutex/data/lake_rust_stats.py:tests/unit/test_lake_rust_stats.py:"' in text, (
            "lake_rust_stats.py is not wired into mutation.yml's per-module mutmut loop"
        )


# ---------------------------------------------------------------------------
# DEC-RUST-STRICT-NO-FALLBACK-001 — strict-ON refusal pins
# ---------------------------------------------------------------------------


class TestStrictRefusal:
    """Strict-ON pins — the companion to ``_strict_off_degrade_lattice``.

    Everything else in this module runs with ``BRUTEX_RUST_STRICT=0`` (the
    degrade lattice).  These tests DELETE the kill switch so strict mode takes
    its shipped default (ON) and pin that the choke points REFUSE instead of
    quietly handing the call to the Python oracle.

    Every reason this facade emits is class A (crate absent / stale /
    errored) or class B (the crate ran and returned a structurally refused
    result) — both REFUSE — EXCEPT the single class-C member of
    ``_STRICT_EXEMPT_REASONS``: ``render_input_beyond_crate``, an input the
    crate never claimed, decided by a Python-side gate BEFORE the FFI. That
    one is exempt because refusing it would crash a legitimate caller over a
    value the CPython oracle was always the intended handler for; its
    exemption is pinned here too, not merely asserted in a docstring.

    ``_strict_on`` uses the SAME shared ``monkeypatch`` object the autouse
    fixture used, so both live on one LIFO undo stack and the value cannot
    leak past this module.
    """

    @staticmethod
    def _strict_on(monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config import rust_strict

        monkeypatch.delenv(rust_strict.RUST_STRICT_ENV, raising=False)
        assert rust_strict.strict_mode() is True

    def test_class_a_absent_crate_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config.rust_strict import RustFallbackRefused

        monkeypatch.setitem(sys.modules, "brutex_lake", None)
        self._strict_on(monkeypatch)
        with pytest.raises(RustFallbackRefused):
            _seam_call()

    def test_same_path_serves_the_oracle_under_strict_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The autouse fixture already pinned BRUTEX_RUST_STRICT=0.
        monkeypatch.setitem(sys.modules, "brutex_lake", None)
        assert _seam_call() == "PY"

    def test_class_a_flag_unrecognised_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config.rust_strict import RustFallbackRefused

        monkeypatch.setenv(lake_rust_stats.LAKE_RUST_STATS_ENV, "onn")
        self._strict_on(monkeypatch)
        with pytest.raises(RustFallbackRefused):
            lake_rust_stats.lake_rust_stats_enabled()

    def test_class_b_burn_in_byte_mismatch_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex.config.rust_strict import RustFallbackRefused

        self._strict_on(monkeypatch)
        with pytest.raises(RustFallbackRefused):
            lake_rust_stats._disable("burn_in_byte_mismatch")

    def test_class_c_beyond_crate_is_exempt_and_serves_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ONE exempt reason — strict-ON still degrades, and says why.

        A lone-surrogate ``rel`` is a value the CRATE cannot carry, not a
        Rust fault: refusing it would strand a legitimate shard write over an
        input pyo3 never accepted. The gate runs pre-FFI, so the crate is
        never called and the oracle serves — under the shipped strict default.
        """
        monkeypatch.setitem(
            sys.modules, "brutex_lake", _fake_crate(render=TestBeyondCrateInputs._never)
        )
        self._strict_on(monkeypatch)
        row = ("bad\ud800rel", 2, "a", "b", 5, 9, None)
        with capture_logs() as logs:
            body = lake_rust_stats.render_shard_json(
                payload_factory=lambda: _payload_with_file(row), python_render=lambda: "PY"
            )
        assert body == "PY"
        warns = [entry for entry in logs if entry["event"] == "lake_rust_stats.fallback"]
        assert warns[0]["reason"] == "render_input_beyond_crate"
