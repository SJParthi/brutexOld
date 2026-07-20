"""Unit suite for :mod:`brutex.observability.chain_map` (R41, D-18).

Pins the exact link vocabulary, the full per-link state-derivation matrix
(stamp evidence, running inference, terminal states, suspect amber floor),
and the atomic fail-soft snapshot writer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest
from brutex.observability.chain_map import (
    CHAIN_MAP_FILENAME,
    CHAIN_MAP_SCHEMA_VERSION,
    LINKS,
    LinkState,
    build_chain_map,
    chain_map_path,
    write_chain_map,
)
from brutex.observability.stage_stamp import StampVerdict
from structlog.testing import capture_logs

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r41_links_and_states_pinned": ("R41",),
    "test_r41_chain_map_path_law": ("R41",),
    "test_r41_no_evidence_all_pending": ("R41",),
    "test_r41_running_stage_inference": ("R41",),
    "test_r41_running_unknown_stage_amber": ("R41",),
    "test_r41_red_stamp_wins_over_everything": ("R41",),
    "test_r41_ok_stamp_green_even_while_earlier_stage_runs": ("R41",),
    "test_r41_terminal_complete_all_green": ("R41",),
    "test_r41_terminal_refused_preflight_red": ("R41",),
    "test_r41_terminal_failed_at_known_stage": ("R41",),
    "test_r41_terminal_failed_stage_unknown_amber": ("R41",),
    "test_r41_suspect_amber_floor": ("R41",),
    "test_r41_run_id_sourced_exit_status_then_progress": ("R41",),
    "test_r41_write_chain_map_atomic_snapshot": ("R41",),
    "test_r41_write_chain_map_fail_soft": ("R41",),
}

_RUN_ID: Final[str] = "bb" * 32


def _states(chain: dict[str, object]) -> dict[str, str]:
    links = chain["links"]
    assert isinstance(links, dict)
    return {name: link["state"] for name, link in links.items()}


class TestDerivation:
    def test_r41_links_and_states_pinned(self) -> None:
        """The link pipeline + state vocabulary are the exact design-§2 values."""
        assert LINKS == ("preflight", "sweep", "stage3", "cascade", "artifacts")
        assert [s.value for s in LinkState] == ["pending", "running", "green", "amber", "red"]
        assert CHAIN_MAP_SCHEMA_VERSION == 1

    def test_r41_chain_map_path_law(self, tmp_path: Path) -> None:
        assert chain_map_path(tmp_path, _RUN_ID) == tmp_path / "runs" / _RUN_ID / CHAIN_MAP_FILENAME
        assert CHAIN_MAP_FILENAME == "chain-map.json"

    def test_r41_no_evidence_all_pending(self) -> None:
        """No progress, no stamps, no exit status → every link pending."""
        chain = build_chain_map(progress=None, stamps={}, suspect=False, exit_status=None)
        assert _states(chain) == dict.fromkeys(LINKS, "pending")
        assert chain["run_id"] == ""
        assert chain["suspect"] is False
        assert chain["schema_version"] == 1

    def test_r41_running_stage_inference(self) -> None:
        """Running at stage3: earlier links green (inferred), stage3 running,
        later links pending."""
        chain = build_chain_map(
            progress={"running": True, "stage": "stage3", "run_id": _RUN_ID},
            stamps={},
            suspect=False,
            exit_status=None,
        )
        assert _states(chain) == {
            "preflight": "green",
            "sweep": "green",
            "stage3": "running",
            "cascade": "pending",
            "artifacts": "pending",
        }
        links = chain["links"]
        assert isinstance(links, dict)
        assert links["stage3"]["detail"] == "in progress"
        assert links["sweep"]["detail"] == "completed (inferred)"
        assert chain["run_id"] == _RUN_ID

    def test_r41_running_unknown_stage_amber(self) -> None:
        """Running with an unknown/missing stage → honest amber, never invented."""
        for progress in (
            {"running": True},
            {"running": True, "stage": "warp_drive"},
            {"running": True, "stage": 7},
        ):
            chain = build_chain_map(progress=progress, stamps={}, suspect=False, exit_status=None)
            assert _states(chain) == dict.fromkeys(LINKS, "amber")
            links = chain["links"]
            assert isinstance(links, dict)
            assert links["sweep"]["detail"] == "running; stage unknown"

    def test_r41_red_stamp_wins_over_everything(self) -> None:
        """A HASH_MISMATCH/MALFORMED stamp is red even on the running stage /
        a complete run."""
        chain = build_chain_map(
            progress={"running": True, "stage": "cascade"},
            stamps={"cascade": StampVerdict.HASH_MISMATCH, "stage3": StampVerdict.MALFORMED},
            suspect=False,
            exit_status=None,
        )
        states = _states(chain)
        assert states["cascade"] == "red"
        assert states["stage3"] == "red"
        links = chain["links"]
        assert isinstance(links, dict)
        assert links["cascade"]["detail"] == "stamp hash_mismatch"
        assert links["stage3"]["detail"] == "stamp malformed"
        done = build_chain_map(
            progress=None,
            stamps={"artifacts": StampVerdict.HASH_MISMATCH},
            suspect=False,
            exit_status={"state": "complete"},
        )
        assert _states(done)["artifacts"] == "red"

    def test_r41_ok_stamp_green_even_while_earlier_stage_runs(self) -> None:
        """Stamp evidence outranks positional inference: an OK-stamped cascade
        stays green while sweep is running; MISSING_* verdicts carry no info."""
        chain = build_chain_map(
            progress={"running": True, "stage": "sweep"},
            stamps={
                "cascade": StampVerdict.OK,
                "stage3": StampVerdict.MISSING_STAMP,
                "artifacts": StampVerdict.MISSING_ARTIFACT,
            },
            suspect=False,
            exit_status=None,
        )
        assert _states(chain) == {
            "preflight": "green",
            "sweep": "running",
            "stage3": "pending",
            "cascade": "green",
            "artifacts": "pending",
        }
        links = chain["links"]
        assert isinstance(links, dict)
        assert links["cascade"]["detail"] == "stamp valid"

    def test_r41_terminal_complete_all_green(self) -> None:
        """state=complete (and its SUSPECT_COMPLETE twin pre-floor) → all green;
        the state comparison is case-insensitive (RunState StrEnum tolerance)."""
        for state in ("complete", "COMPLETE", "suspect_complete"):
            chain = build_chain_map(
                progress={"running": False, "stage": "artifacts"},
                stamps={},
                suspect=False,
                exit_status={"state": state, "run_id": _RUN_ID},
            )
            assert _states(chain) == dict.fromkeys(LINKS, "green")

    def test_r41_terminal_refused_preflight_red(self) -> None:
        """A refusal at launch: preflight red carrying the refusal text, all
        later links pending — nothing was touched (R42 preflight law)."""
        chain = build_chain_map(
            progress=None,
            stamps={},
            suspect=False,
            exit_status={"state": "refused", "refusal_text": "missing spans: NIFTY 2020-03"},
        )
        states = _states(chain)
        assert states == {
            "preflight": "red",
            "sweep": "pending",
            "stage3": "pending",
            "cascade": "pending",
            "artifacts": "pending",
        }
        links = chain["links"]
        assert isinstance(links, dict)
        assert links["preflight"]["detail"] == "refused: missing spans: NIFTY 2020-03"
        bare = build_chain_map(
            progress=None, stamps={}, suspect=False, exit_status={"state": "refused"}
        )
        bare_links = bare["links"]
        assert isinstance(bare_links, dict)
        assert bare_links["preflight"]["detail"] == "refused: "

    def test_r41_terminal_failed_at_known_stage(self) -> None:
        """failed/halted at a known stage: earlier green, that stage red naming
        error_type, later pending."""
        for state in ("failed", "halted"):
            chain = build_chain_map(
                progress={"running": False, "stage": "stage3"},
                stamps={},
                suspect=False,
                exit_status={"state": state, "error_type": "LakeWriteHalt"},
            )
            assert _states(chain) == {
                "preflight": "green",
                "sweep": "green",
                "stage3": "red",
                "cascade": "pending",
                "artifacts": "pending",
            }
            links = chain["links"]
            assert isinstance(links, dict)
            assert links["stage3"]["detail"] == f"run {state} at this stage (LakeWriteHalt)"
        anon = build_chain_map(
            progress={"running": False, "stage": "sweep"},
            stamps={},
            suspect=False,
            exit_status={"state": "failed"},
        )
        anon_links = anon["links"]
        assert isinstance(anon_links, dict)
        assert anon_links["sweep"]["detail"] == "run failed at this stage (unknown error)"

    def test_r41_terminal_failed_stage_unknown_amber(self) -> None:
        """failed/halted with NO known stage → every link amber (honest)."""
        chain = build_chain_map(
            progress=None, stamps={}, suspect=False, exit_status={"state": "halted"}
        )
        assert _states(chain) == dict.fromkeys(LINKS, "amber")
        links = chain["links"]
        assert isinstance(links, dict)
        assert links["sweep"]["detail"] == "run halted; stage unknown"

    def test_r41_suspect_amber_floor(self) -> None:
        """D-12: suspect downgrades every green to amber; red/running/pending
        keep their states — a suspect run can never render all-green."""
        chain = build_chain_map(
            progress={"running": True, "stage": "stage3"},
            stamps={"cascade": StampVerdict.HASH_MISMATCH},
            suspect=True,
            exit_status=None,
        )
        assert _states(chain) == {
            "preflight": "amber",
            "sweep": "amber",
            "stage3": "running",
            "cascade": "red",
            "artifacts": "pending",
        }
        links = chain["links"]
        assert isinstance(links, dict)
        assert links["sweep"]["detail"] == "completed (inferred); run marked SUSPECT"
        assert chain["suspect"] is True

    def test_r41_run_id_sourced_exit_status_then_progress(self) -> None:
        """run_id resolution: exit-status wins, then progress, else ''."""
        both = build_chain_map(
            progress={"run_id": "p" * 8},
            stamps={},
            suspect=False,
            exit_status={"run_id": "e" * 8},
        )
        assert both["run_id"] == "e" * 8
        progress_only = build_chain_map(
            progress={"run_id": "p" * 8}, stamps={}, suspect=False, exit_status=None
        )
        assert progress_only["run_id"] == "p" * 8


class TestWrite:
    def test_r41_write_chain_map_atomic_snapshot(self, tmp_path: Path) -> None:
        """The snapshot stamps run_id + updated_utc + schema_version and lands
        atomically (no temp remnant); rewrite replaces in place."""
        chain = build_chain_map(progress=None, stamps={}, suspect=False, exit_status=None)
        write_chain_map(tmp_path, _RUN_ID, chain, wall=lambda: 555.5)
        path = chain_map_path(tmp_path, _RUN_ID)
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["run_id"] == _RUN_ID
        assert doc["updated_utc"] == 555.5
        assert doc["schema_version"] == 1
        assert doc["links"] == {
            name: {"state": "pending", "detail": "no evidence"} for name in LINKS
        }
        write_chain_map(tmp_path, _RUN_ID, {"links": {}}, wall=lambda: 556.0)
        doc2 = json.loads(path.read_text(encoding="utf-8"))
        assert doc2["updated_utc"] == 556.0
        assert doc2["schema_version"] == 1  # setdefault fills a bare mapping
        assert [p.name for p in path.parent.iterdir()] == [CHAIN_MAP_FILENAME]

    def test_r41_write_chain_map_fail_soft(self, tmp_path: Path) -> None:
        """A write failure is ONE chain_map.write_failed WARN — never a raise."""
        (tmp_path / "runs").write_text("blocker", encoding="utf-8")
        with capture_logs() as logs:
            write_chain_map(tmp_path, _RUN_ID, {"links": {}})
        warns = [e for e in logs if e["event"] == "chain_map.write_failed"]
        assert len(warns) == 1
        assert warns[0]["run_id"] == _RUN_ID


# ────────────────────────────────────────────────────────────────────────
# DEC-RUST-SWEEP-OPS-001 — flag-ON routing parity (wheel-free fake crate)
# ────────────────────────────────────────────────────────────────────────


class TestSweepOpsRouting:
    """Flag-ON routing parity for the per-link state ladder.

    Wheel-free: the shared behavior-correct FAKE crate is injected by
    patching the facade's ``importlib`` binding. Flag ON must be
    document-identical to the flag-OFF Python ladder (which every OTHER
    test in this file drives) — including the suspect amber floor, which
    stays Python ABOVE the routed law on both arms.
    """

    @staticmethod
    def _arm_fake_crate(monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from brutex.engine import sweep_ops_rust

        from tests.fakes.sweep_ops_crate import build_fake_sweep_ops_crate

        crate = build_fake_sweep_ops_crate(version="fake")
        monkeypatch.setattr(
            sweep_ops_rust,
            "importlib",
            SimpleNamespace(import_module=lambda name: crate),
        )
        monkeypatch.setenv(sweep_ops_rust.SWEEP_OPS_RUST_ENV, "1")
        sweep_ops_rust.reset_sweep_ops_rust_for_tests()

    @staticmethod
    def _reset_facade() -> None:
        from brutex.engine.sweep_ops_rust import reset_sweep_ops_rust_for_tests

        reset_sweep_ops_rust_for_tests()

    def test_flag_on_matches_python_ladder_matrix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cases: list[dict[str, object]] = [
            {
                "progress": {"running": True, "stage": "stage3", "run_id": "r1"},
                "stamps": {"preflight": StampVerdict.OK},
                "suspect": False,
                "exit_status": None,
            },
            {
                "progress": {"running": True},
                "stamps": {},
                "suspect": False,
                "exit_status": None,
            },
            {
                "progress": None,
                "stamps": {"sweep": StampVerdict.HASH_MISMATCH},
                "suspect": False,
                "exit_status": {"state": "complete"},
            },
            {
                "progress": None,
                "stamps": {},
                "suspect": False,
                "exit_status": {"state": "refused", "refusal_text": ""},
            },
            {
                "progress": {"running": False, "stage": "cascade"},
                "stamps": {},
                "suspect": False,
                "exit_status": {"state": "halted"},
            },
            {
                "progress": None,
                "stamps": {"artifacts": StampVerdict.OK},
                "suspect": True,
                "exit_status": {"state": "complete"},
            },
        ]

        def maps() -> list[dict[str, object]]:
            return [
                build_chain_map(
                    progress=case["progress"],  # type: ignore[arg-type]
                    stamps=case["stamps"],  # type: ignore[arg-type]
                    suspect=case["suspect"],  # type: ignore[arg-type]
                    exit_status=case["exit_status"],  # type: ignore[arg-type]
                )
                for case in cases
            ]

        off = maps()
        self._arm_fake_crate(monkeypatch)
        try:
            assert maps() == off
        finally:
            self._reset_facade()
