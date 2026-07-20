"""The rate-bridge Rust kernel path (``DEC-RUST-PULL-STATE-001``, Wave 3H).

Flip phase — covers the ``brutex/data/rate_bridge.py`` half of the port:
the payload-verdict LATTICE is the kernel, no flag, no oracle.

* the mechanical classifier (`_classify_bridge_payload_for_kernel`) — every
  concrete-type / coercibility arm, the ``OverflowError`` envelope (a
  FIRST-failure overflow propagates verbatim; an overflow AFTER an earlier
  caught failure folds to the ``None`` sentinel) and the 1:1 drift pin
  against the golden harness's mirror;
* the kernel verdict is AUTHORITATIVE — an INVALID verdict answers ``None``,
  the VALID verdict constructs the snapshot from the probed-OK payload, and
  a poisoned VALID inconsistent with the classified shape is REFUSED;
* every wheel fault RAISES the typed :class:`PullStateKernelError`; a
  still-SET removed ``BRUTEX_PULL_STATE_RUST`` knob raises
  :class:`PullStateFlagRemovedError`;
* the REAL wheel replays the WHOLE frozen golden + the frozen TSV rows.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from brutex import pull_state_rust
from brutex.data.rate_bridge import (
    _classify_bridge_payload_for_kernel,
    rate_bridge_path,
    read_rate_snapshot,
)

from tests.golden import _rate_bridge_harness as harness

FIXTURES = Path(__file__).resolve().parents[1] / "golden" / "fixtures"
JSON_FIXTURE = FIXTURES / "rate_bridge_verdicts.json"


class _BombKernelModule:
    """A ``sys.modules`` plant that RECORDS every attribute access, then
    raises ``AttributeError`` — the teeth live in ``touches`` staying EMPTY
    on the paths that must never consult the kernel."""

    def __init__(self) -> None:
        self.touches: list[str] = []

    def __getattr__(self, name: str) -> object:
        object.__getattribute__(self, "touches").append(name)
        raise AttributeError(name)


def _install(monkeypatch: pytest.MonkeyPatch, **symbols: Any) -> None:
    """Plant a fake ``brutex_pull_state`` module exposing *symbols*."""
    monkeypatch.setitem(sys.modules, "brutex_pull_state", SimpleNamespace(**symbols))


def _write(lake: Path, raw: bytes) -> None:
    """Write *raw* to the lake's bridge-file path."""
    dest = rate_bridge_path(lake)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)


def _valid_bytes() -> bytes:
    """One fully-valid v3 bridge payload as bytes."""
    return json.dumps(
        {
            "schema": 3,
            "effective_rps": 3.25,
            "current_rps": 5.0,
            "recent_minute": 180,
            "recent_429": 2,
            "ceiling_per_min": 500,
            "recent_1s": 4,
            "recent_600s": 1800,
            "recent_3600s": 9000,
            "wall_time": 1_700_000_000.5,
            "monotonic": 12345.25,
            "pull_active": True,
        }
    ).encode()


# ---------------------------------------------------------------------------
# The mechanical classifier — every arm + the harness drift pin.
# ---------------------------------------------------------------------------


class TestClassifier:
    """The mechanical classifier: type/coercibility facts, no law."""

    def test_non_mapping_root_zeroes_field_slots(self) -> None:
        """A non-mapping root classifies as the zeroed root_mapping=0 codes."""
        for root in ([1], "x", 7, None, True):
            assert _classify_bridge_payload_for_kernel(root) == (
                0,
                pull_state_rust.BRIDGE_SCHEMA_MISSING,
                0,
                0.0,
                [pull_state_rust.BRIDGE_FIELD_ABSENT] * 10,
                pull_state_rust.BRIDGE_PA_NONE,
            )

    @pytest.mark.parametrize(
        ("schema", "kind", "int_value"),
        [
            (None, pull_state_rust.BRIDGE_SCHEMA_MISSING, 0),
            (True, pull_state_rust.BRIDGE_SCHEMA_BOOL, 1),
            (False, pull_state_rust.BRIDGE_SCHEMA_BOOL, 0),
            (3, pull_state_rust.BRIDGE_SCHEMA_INT, 3),
            (-7, pull_state_rust.BRIDGE_SCHEMA_INT, -7),
            (10**30, pull_state_rust.BRIDGE_SCHEMA_INT_HUGE, 0),
            (-(10**30), pull_state_rust.BRIDGE_SCHEMA_INT_HUGE, 0),
            ("3", pull_state_rust.BRIDGE_SCHEMA_OTHER, 0),
            ([3], pull_state_rust.BRIDGE_SCHEMA_OTHER, 0),
        ],
    )
    def test_schema_kinds(self, schema: object, kind: int, int_value: int) -> None:
        """Every schema concrete-type arm classifies mechanically."""
        classified = _classify_bridge_payload_for_kernel({"schema": schema})
        assert classified is not None
        assert (classified[1], classified[2]) == (kind, int_value)

    def test_schema_float_carries_the_value(self) -> None:
        """A float schema carries its value in the float slot."""
        classified = _classify_bridge_payload_for_kernel({"schema": 3.5})
        assert classified is not None
        assert classified[1] == pull_state_rust.BRIDGE_SCHEMA_FLOAT
        assert classified[3] == 3.5

    def test_field_kinds_probe_cpython_taxonomy(self) -> None:
        """The field probes ARE CPython's own ``float()``/``int()`` taxonomy."""
        payload = {
            "schema": 3,
            "effective_rps": "2.5",  # numeric string → OK
            "current_rps": None,  # TypeError → BAD
            "recent_minute": True,  # bool → int(True) → OK
            "recent_429": "abc",  # ValueError → BAD
            # ceiling_per_min absent → ABSENT
            "recent_1s": 2.9,  # int(2.9) → OK
            "recent_600s": [1],  # TypeError → BAD
            "recent_3600s": 10**30,  # int(int) never overflows → OK
            "wall_time": float("nan"),  # float(nan) → OK
            "monotonic": {},  # TypeError → BAD
        }
        classified = _classify_bridge_payload_for_kernel(payload)
        assert classified is not None
        ok, bad, absent = (
            pull_state_rust.BRIDGE_FIELD_OK,
            pull_state_rust.BRIDGE_FIELD_BAD,
            pull_state_rust.BRIDGE_FIELD_ABSENT,
        )
        assert classified[4] == [ok, bad, ok, bad, absent, ok, bad, ok, ok, bad]

    def test_first_failure_overflow_propagates(self) -> None:
        """A FIRST-failure overflow raises verbatim (the documented
        pre-existing raises envelope — the classifier's own arm)."""
        with pytest.raises(OverflowError):
            _classify_bridge_payload_for_kernel({"schema": 3, "effective_rps": 10**400})
        with pytest.raises(OverflowError):
            _classify_bridge_payload_for_kernel(
                {
                    "schema": 3,
                    "effective_rps": 1.0,
                    "current_rps": 2.0,
                    "recent_minute": float("inf"),
                }
            )

    def test_overflow_after_an_earlier_failure_folds_to_none(self) -> None:
        """An overflow probed AFTER an earlier caught failure folds to the
        ``None`` sentinel — the constructor-order first failure was the
        caught one, so the payload reads ``None`` (the frozen
        ``pythonarm_bad_then_huge`` disposition)."""
        assert (
            _classify_bridge_payload_for_kernel(
                {"schema": 3, "effective_rps": "abc", "recent_minute": float("inf")}
            )
            is None
        )

    @pytest.mark.parametrize(
        ("raw_pa", "kind"),
        [
            (True, pull_state_rust.BRIDGE_PA_TRUE),
            (False, pull_state_rust.BRIDGE_PA_FALSE),
            (None, pull_state_rust.BRIDGE_PA_NONE),
            ("yes", pull_state_rust.BRIDGE_PA_NONE),
            (1, pull_state_rust.BRIDGE_PA_NONE),
        ],
    )
    def test_pull_active_kinds(self, raw_pa: object, kind: int) -> None:
        """The pull_active bool-or-None concrete-type tags."""
        classified = _classify_bridge_payload_for_kernel({"pull_active": raw_pa})
        assert classified is not None
        assert classified[5] == kind

    def test_absent_pull_active_reads_none_kind(self) -> None:
        """An absent pull_active key classifies PA_NONE."""
        classified = _classify_bridge_payload_for_kernel({"schema": 3})
        assert classified is not None
        assert classified[5] == pull_state_rust.BRIDGE_PA_NONE

    def test_module_classifier_mirrors_the_golden_harness(self) -> None:
        """The 1:1 drift pin: module classifier ≡ harness mirror on EVERY
        decodable frozen case.  The frozen ``classified: null`` rows are the
        overflow envelope — the module classifier now either RAISES the
        overflow (first-failure) or folds to ``None`` (an earlier failure
        won); both mean the kernel is never consulted, the pin's intent."""
        payload = json.loads(JSON_FIXTURE.read_text(encoding="utf-8"))
        checked = 0
        for row in payload["rows"]:
            if not row["decode_ok"]:
                continue
            decoded = json.loads(bytes.fromhex(row["raw_hex"]))
            expected = row["classified"]
            if expected is None:
                try:
                    got = _classify_bridge_payload_for_kernel(decoded)
                except OverflowError:
                    got = None  # the first-failure raise arm — also kernel-free
                assert got is None, row["id"]
            else:
                classified = _classify_bridge_payload_for_kernel(decoded)
                assert classified is not None, row["id"]
                # The float slot compares by BIT PATTERN (a NaN schema value
                # would fail ``==`` against itself — the fixture's contract
                # is bit-exact, so the pin is too).
                assert (
                    classified[0],
                    classified[1],
                    classified[2],
                    harness.f64_bits(classified[3]),
                    classified[4],
                    classified[5],
                ) == (
                    expected["root_mapping"],
                    expected["schema_kind"],
                    expected["schema_int"],
                    expected["schema_float_bits"],
                    expected["field_kinds"],
                    expected["pa_kind"],
                ), row["id"]
            checked += 1
        assert checked >= 40  # anti-vacuous


# ---------------------------------------------------------------------------
# The kernel-authoritative read path (injected kernels).
# ---------------------------------------------------------------------------


class TestKernelAuthoritative:
    """The kernel verdict decides the read — never a Python re-derivation."""

    def test_invalid_verdict_is_served(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An INVALID kernel verdict answers None even on a valid file —
        the kernel-authoritative serve path (proves the read consults)."""
        _install(monkeypatch, bridge_payload_verdict=lambda *_a: (2, 0))
        _write(tmp_path, _valid_bytes())
        assert read_rate_snapshot(tmp_path) is None

    def test_valid_verdict_constructs_with_the_echoed_pull_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The VALID verdict constructs the snapshot; the kernel's echoed
        pull_active arm is the served value (a deliberately-flipped echo
        proves the kernel owns it)."""
        _install(monkeypatch, bridge_payload_verdict=lambda *_a: (0, 1))  # echo FALSE
        _write(tmp_path, _valid_bytes())  # the file itself says True
        snapshot = read_rate_snapshot(tmp_path)
        assert snapshot is not None
        assert snapshot.effective_rps == 3.25
        assert snapshot.pull_active is False

    def test_poisoned_valid_on_a_field_broken_payload_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A poisoned VALID whose classified codes say a field cannot coerce
        is REFUSED loudly — serving it would crash the construction."""
        _install(monkeypatch, bridge_payload_verdict=lambda *_a: (0, 0))
        _write(tmp_path, b'{"schema": 2}')  # every required field ABSENT
        with pytest.raises(pull_state_rust.PullStateKernelError, match="inconsistent"):
            read_rate_snapshot(tmp_path)

    def test_poisoned_valid_on_a_non_mapping_root_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A poisoned VALID for a non-mapping root is refused the same way."""
        _install(monkeypatch, bridge_payload_verdict=lambda *_a: (0, 0))
        _write(tmp_path, b"[1, 2]")
        with pytest.raises(pull_state_rust.PullStateKernelError, match="inconsistent"):
            read_rate_snapshot(tmp_path)


class TestFailClosed:
    """Wheel faults raise the typed error out of the read — no oracle."""

    def test_missing_wheel_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unimportable wheel refuses loudly (``wheel_missing``)."""
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        _write(tmp_path, _valid_bytes())
        with pytest.raises(pull_state_rust.PullStateKernelError, match="wheel_missing"):
            read_rate_snapshot(tmp_path)

    def test_stale_wheel_raises_naming_the_symbol(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wheel predating the symbol refuses naming it (``wheel_stale``)."""
        _install(monkeypatch)  # importable, symbol absent
        _write(tmp_path, _valid_bytes())
        with pytest.raises(pull_state_rust.PullStateKernelError, match="bridge_payload_verdict"):
            read_rate_snapshot(tmp_path)

    def test_raising_kernel_raises_kernel_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raising kernel becomes the typed ``kernel_error`` raise."""

        def _boom(*_args: object) -> object:
            raise RuntimeError("kernel exploded")

        _install(monkeypatch, bridge_payload_verdict=_boom)
        _write(tmp_path, _valid_bytes())
        with pytest.raises(pull_state_rust.PullStateKernelError, match="kernel_error"):
            read_rate_snapshot(tmp_path)

    @pytest.mark.parametrize(
        "poison",
        [
            None,  # literal None
            7,  # not a tuple
            (0,),  # wrong arity
            (True, 0),  # bool pun in the verdict slot
            (13, 0),  # out-of-vocabulary verdict
            (255, 0),  # the kernel's own bad-input answer
            (0, 3),  # out-of-vocabulary pull_active arm
        ],
    )
    def test_poison_verdicts_raise_kernel_invalid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, poison: object
    ) -> None:
        """Every out-of-contract verdict shape is refused (``kernel_invalid``)."""
        _install(monkeypatch, bridge_payload_verdict=lambda *_a: poison)
        _write(tmp_path, _valid_bytes())
        with pytest.raises(pull_state_rust.PullStateKernelError, match="kernel_invalid"):
            read_rate_snapshot(tmp_path)

    def test_set_removed_flag_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A still-SET removed knob refuses loudly — truthy AND falsy alike."""
        _write(tmp_path, _valid_bytes())
        for value in ("1", "0"):
            monkeypatch.setenv(pull_state_rust.PULL_STATE_RUST_ENV, value)
            with pytest.raises(pull_state_rust.PullStateFlagRemovedError):
                read_rate_snapshot(tmp_path)

    def test_overflow_payload_never_consults_the_kernel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OverflowError-probing payload skips the kernel entirely and
        preserves the documented raise byte-for-byte."""
        bomb = _BombKernelModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        raw = ("{" + '"schema": 3, "effective_rps": 1' + "0" * 400 + "}").encode()
        _write(tmp_path, raw)
        with pytest.raises(OverflowError):
            read_rate_snapshot(tmp_path)
        assert bomb.touches == []

    def test_bad_then_overflow_payload_reads_none_kernel_free(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The earlier-caught-failure disposition: BAD-then-overflow reads
        ``None`` without consulting the kernel (the frozen
        ``pythonarm_bad_then_huge`` case's outcome, preserved)."""
        bomb = _BombKernelModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        _write(
            tmp_path,
            json.dumps(
                {"schema": 3, "effective_rps": "abc", "recent_minute": float("inf")}
            ).encode(),
        )
        assert read_rate_snapshot(tmp_path) is None
        assert bomb.touches == []

    def test_corrupt_bytes_never_consult_the_kernel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A decode failure answers ``None`` before any kernel consult."""
        bomb = _BombKernelModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        _write(tmp_path, b'{"schema": 3')
        assert read_rate_snapshot(tmp_path) is None
        assert bomb.touches == []


# ---------------------------------------------------------------------------
# The REAL wheel — the frozen golden replays end-to-end.
# ---------------------------------------------------------------------------


def test_real_wheel_replays_the_frozen_golden() -> None:
    """The WHOLE frozen module-level golden replays byte-identically through
    the LIVE (kernel-only) module — outcomes (snapshot fields, None answers,
    the raises family) all equal the pre-conversion capture."""
    live = json.loads(json.dumps(harness.compute_payload(), allow_nan=False))
    frozen = json.loads(JSON_FIXTURE.read_text(encoding="utf-8"))
    assert live["rows"] == frozen["rows"]


def test_real_wheel_kernel_differential_over_the_frozen_tsv() -> None:
    """Every frozen TSV row through the REAL kernel via the facade — the
    binary verdict + the echoed pull_active arm match row-for-row."""
    rows = 0
    for line in (FIXTURES / "rate_bridge_kernel_cases.tsv").read_text().splitlines():
        if line.startswith("#") or not line:
            continue
        cells = line.split("\t")
        assert len(cells) == 18
        verdict, pa = pull_state_rust.rust_bridge_payload_verdict(
            int(cells[1]),
            int(cells[2]),
            int(cells[3]),
            harness.f64_from_bits(cells[4]),
            [int(c) for c in cells[5:15]],
            int(cells[15]),
        )
        assert (verdict == pull_state_rust.BRIDGE_VERDICT_VALID) == (cells[16] == "1"), cells[0]
        assert pa == int(cells[17]), cells[0]
        rows += 1
    assert rows >= 40  # anti-vacuous
