"""The rate-bridge Rust seam (``DEC-RUST-PULL-STATE-001``, Wave 3H).

Covers the ``brutex/data/rate_bridge.py`` half of the Wave-3H port:

* FLAG OFF (default) is byte-identical AND provably never touches the wheel
  (the bomb-kernel spy);
* the mechanical classifier (`_classify_bridge_payload_for_kernel`) — every
  concrete-type / coercibility arm, the ``OverflowError`` Python-arm
  abstain, and the 1:1 drift pin against the golden harness's mirror;
* the facade accessor ``rust_bridge_payload_verdict`` — degrade tiers
  (``wheel_missing`` / ``wheel_stale`` / ``kernel_error``) + the
  refuse-poison vocabulary gate (``kernel_invalid``);
* the seam law — an INVALID kernel verdict is SERVED (the ``None`` answer),
  a VALID one falls THROUGH to the byte-identical oracle, every degrade
  falls through too;
* FLAG ON with the REAL wheel (``importorskip`` + a symbol capability
  probe): the WHOLE frozen golden replays byte-identically end-to-end, and
  the kernel differential over the frozen TSV rows holds.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from brutex import pull_state_rust
from brutex.data import rate_bridge
from brutex.data.rate_bridge import (
    _classify_bridge_payload_for_kernel,
    rate_bridge_path,
    read_rate_snapshot,
)
from tests.golden import _rate_bridge_harness as harness

FLAG_ENV = "BRUTEX_PULL_STATE_RUST"
FIXTURES = Path(__file__).resolve().parents[1] / "golden" / "fixtures"
JSON_FIXTURE = FIXTURES / "rate_bridge_verdicts.json"


@pytest.fixture(autouse=True)
def _clean_warn_registry() -> Any:
    pull_state_rust.reset_for_tests()
    yield
    pull_state_rust.reset_for_tests()


class _BombKernelModule:
    """A ``sys.modules`` plant that RECORDS every attribute access, then
    raises ``AttributeError`` (the CodeQL-standard shape ``getattr``/
    ``hasattr`` swallow — the teeth live in ``touches`` staying EMPTY)."""

    def __init__(self) -> None:
        self.touches: list[str] = []

    def __getattr__(self, name: str) -> object:
        object.__getattribute__(self, "touches").append(name)
        raise AttributeError(name)


def _install(monkeypatch: pytest.MonkeyPatch, **symbols: Any) -> None:
    monkeypatch.setitem(sys.modules, "brutex_pull_state", SimpleNamespace(**symbols))


def _write(lake: Path, raw: bytes) -> None:
    dest = rate_bridge_path(lake)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)


def _valid_bytes() -> bytes:
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
# Flag OFF — byte-identical oracle, wheel provably never touched.
# ---------------------------------------------------------------------------


class TestFlagOffNeverImports:
    @pytest.mark.parametrize("flag", [None, "0"])
    def test_read_is_oracle_and_wheel_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str | None
    ) -> None:
        if flag is None:
            monkeypatch.delenv(FLAG_ENV, raising=False)
        else:
            monkeypatch.setenv(FLAG_ENV, flag)
        bomb = _BombKernelModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        _write(tmp_path, _valid_bytes())
        snapshot = read_rate_snapshot(tmp_path)
        assert snapshot is not None and snapshot.pull_active is True
        _write(tmp_path, b'{"schema": 2}')
        assert read_rate_snapshot(tmp_path) is None
        assert bomb.touches == []


# ---------------------------------------------------------------------------
# The mechanical classifier — every arm + the harness drift pin.
# ---------------------------------------------------------------------------


class TestClassifier:
    def test_non_mapping_root_zeroes_field_slots(self) -> None:
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
        classified = _classify_bridge_payload_for_kernel({"schema": schema})
        assert classified is not None
        assert (classified[1], classified[2]) == (kind, int_value)

    def test_schema_float_carries_the_value(self) -> None:
        classified = _classify_bridge_payload_for_kernel({"schema": 3.5})
        assert classified is not None
        assert classified[1] == pull_state_rust.BRIDGE_SCHEMA_FLOAT
        assert classified[3] == 3.5

    def test_field_kinds_probe_cpython_taxonomy(self) -> None:
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

    def test_overflow_probe_abstains_to_the_python_arm(self) -> None:
        assert _classify_bridge_payload_for_kernel({"schema": 3, "effective_rps": 10**400}) is None
        assert (
            _classify_bridge_payload_for_kernel({"schema": 3, "recent_minute": float("inf")})
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
        classified = _classify_bridge_payload_for_kernel({"pull_active": raw_pa})
        assert classified is not None
        assert classified[5] == kind

    def test_absent_pull_active_reads_none_kind(self) -> None:
        classified = _classify_bridge_payload_for_kernel({"schema": 3})
        assert classified is not None
        assert classified[5] == pull_state_rust.BRIDGE_PA_NONE

    def test_module_classifier_mirrors_the_golden_harness(self) -> None:
        """The 1:1 drift pin: module classifier ≡ harness mirror on EVERY
        decodable frozen case (the harness froze the fixture — a divergence
        means the seam's classification drifted from the recorded one)."""
        payload = json.loads(JSON_FIXTURE.read_text(encoding="utf-8"))
        checked = 0
        for row in payload["rows"]:
            if not row["decode_ok"]:
                continue
            decoded = json.loads(bytes.fromhex(row["raw_hex"]))
            classified = _classify_bridge_payload_for_kernel(decoded)
            expected = row["classified"]
            if expected is None:
                assert classified is None, row["id"]
            else:
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
# The facade accessor — degrade tiers + the refuse-poison gate.
# ---------------------------------------------------------------------------

_GOOD_ARGS = (1, pull_state_rust.BRIDGE_SCHEMA_INT, 3, 0.0, [1] * 10, 2)


class TestFacadeAccessor:
    def test_missing_wheel_degrades_wheel_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)  # import fails
        with capture_logs() as logs:
            assert pull_state_rust.rust_bridge_payload_verdict(*_GOOD_ARGS) is None
        assert any(log.get("reason") == "wheel_missing" for log in logs)

    def test_stale_wheel_degrades_naming_the_symbol(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch)  # importable, symbol absent
        with capture_logs() as logs:
            assert pull_state_rust.rust_bridge_payload_verdict(*_GOOD_ARGS) is None
        assert any(
            log.get("reason") == "wheel_stale"
            and log.get("symbol") == "bridge_payload_verdict"
            for log in logs
        )

    def test_raising_kernel_degrades_kernel_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_args: object) -> object:
            raise RuntimeError("kernel exploded")

        _install(monkeypatch, bridge_payload_verdict=_boom)
        with capture_logs() as logs:
            assert pull_state_rust.rust_bridge_payload_verdict(*_GOOD_ARGS) is None
        assert any(log.get("reason") == "kernel_error" for log in logs)

    @pytest.mark.parametrize(
        "poison",
        [
            None,  # literal None must NOT read as a degrade sentinel
            7,  # not a tuple
            (0,),  # wrong arity
            (0, 1, 2),  # wrong arity
            (True, 0),  # bool pun in the verdict slot
            (0, True),  # bool pun in the pull_active slot
            (13, 0),  # out-of-vocabulary verdict (first past the band)
            (255, 0),  # the kernel's own bad-input answer
            (0, 3),  # out-of-vocabulary pull_active arm
            ("0", 0),  # string pun
        ],
    )
    def test_refuse_poison_gate(self, monkeypatch: pytest.MonkeyPatch, poison: object) -> None:
        _install(monkeypatch, bridge_payload_verdict=lambda *_a: poison)
        with capture_logs() as logs:
            assert pull_state_rust.rust_bridge_payload_verdict(*_GOOD_ARGS) is None
        assert any(log.get("reason") == "kernel_invalid" for log in logs)

    def test_valid_pair_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, bridge_payload_verdict=lambda *_a: (0, 2))
        assert pull_state_rust.rust_bridge_payload_verdict(*_GOOD_ARGS) == (0, 2)
        _install(monkeypatch, bridge_payload_verdict=lambda *_a: (12, 0))
        assert pull_state_rust.rust_bridge_payload_verdict(*_GOOD_ARGS) == (12, 0)


# ---------------------------------------------------------------------------
# The seam law (flag ON, injected kernels).
# ---------------------------------------------------------------------------


class TestSeamLaw:
    def test_invalid_verdict_is_served(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An INVALID kernel verdict answers None even on a valid file —
        the kernel-authoritative serve path (proves the seam consults)."""
        monkeypatch.setenv(FLAG_ENV, "1")
        _install(monkeypatch, bridge_payload_verdict=lambda *_a: (2, 0))
        _write(tmp_path, _valid_bytes())
        assert read_rate_snapshot(tmp_path) is None

    def test_valid_verdict_falls_through_to_the_oracle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A (poisoned) VALID verdict on a corrupt payload still serves the
        ORACLE's answer — construction happens on the ONE Python path."""
        monkeypatch.setenv(FLAG_ENV, "1")
        _install(monkeypatch, bridge_payload_verdict=lambda *_a: (0, 0))
        _write(tmp_path, b'{"schema": 2}')  # oracle rejects the schema
        assert read_rate_snapshot(tmp_path) is None
        _write(tmp_path, _valid_bytes())  # and accepts the valid file
        snapshot = read_rate_snapshot(tmp_path)
        assert snapshot is not None and snapshot.effective_rps == 3.25

    def test_degrade_falls_through_to_the_oracle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(FLAG_ENV, "1")
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)  # wheel missing
        _write(tmp_path, _valid_bytes())
        snapshot = read_rate_snapshot(tmp_path)
        assert snapshot is not None and snapshot.pull_active is True

    def test_python_arm_payload_never_consults_the_kernel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OverflowError-probing payload skips the kernel entirely and
        preserves the oracle's documented raise byte-for-byte."""
        monkeypatch.setenv(FLAG_ENV, "1")
        bomb = _BombKernelModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        raw = ("{" + '"schema": 3, "effective_rps": 1' + "0" * 400 + "}").encode()
        _write(tmp_path, raw)
        with pytest.raises(OverflowError):
            read_rate_snapshot(tmp_path)
        assert bomb.touches == []

    def test_corrupt_bytes_never_consult_the_kernel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(FLAG_ENV, "1")
        bomb = _BombKernelModule()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        _write(tmp_path, b'{"schema": 3')
        assert read_rate_snapshot(tmp_path) is None
        assert bomb.touches == []


# ---------------------------------------------------------------------------
# Flag ON — the REAL wheel (capability-probed; skips on a stale wheel).
# ---------------------------------------------------------------------------


def _require_wave3h_wheel() -> None:
    wheel = pytest.importorskip("brutex_pull_state")
    if not hasattr(wheel, "bridge_payload_verdict"):
        pytest.skip("installed brutex_pull_state wheel predates Wave 3H")


def test_real_wheel_replays_the_frozen_golden_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The WHOLE frozen module-level golden replays byte-identically with
    the flag ON — outcomes (snapshot fields, None answers, the raises
    family) all equal the pre-conversion capture."""
    _require_wave3h_wheel()
    monkeypatch.setenv(FLAG_ENV, "1")
    live = json.loads(json.dumps(harness.compute_payload(), allow_nan=False))
    frozen = json.loads(JSON_FIXTURE.read_text(encoding="utf-8"))
    assert live["rows"] == frozen["rows"]


def test_real_wheel_kernel_differential_over_the_frozen_tsv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every frozen TSV row through the REAL kernel via the facade — the
    binary verdict + the echoed pull_active arm match row-for-row."""
    _require_wave3h_wheel()
    monkeypatch.setenv(FLAG_ENV, "1")
    rows = 0
    for line in (FIXTURES / "rate_bridge_kernel_cases.tsv").read_text().splitlines():
        if line.startswith("#") or not line:
            continue
        cells = line.split("\t")
        assert len(cells) == 18
        result = pull_state_rust.rust_bridge_payload_verdict(
            int(cells[1]),
            int(cells[2]),
            int(cells[3]),
            harness.f64_from_bits(cells[4]),
            [int(c) for c in cells[5:15]],
            int(cells[15]),
        )
        assert result is not None, cells[0]
        verdict, pa = result
        assert (verdict == pull_state_rust.BRIDGE_VERDICT_VALID) == (cells[16] == "1"), cells[0]
        assert pa == int(cells[17]), cells[0]
        rows += 1
    assert rows >= 40  # anti-vacuous
