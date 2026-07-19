"""Unit tests for :mod:`brutex.nightly.champions` (LANE W5).

100% line + branch of the champion/challenger + run-delta leg
(DEC-CHAMPION-CHALLENGER-001 + DEC-NIGHTLY-RUN-DELTA-001): env knobs,
eligibility/scope laws, durable state I/O, the snapshot-free run-delta
join, the pooled cascade re-validation (REAL cascade over the REAL tmp
lake — the honest non-promotion arm — plus deterministic doubles for the
transition machine), the seed → challenge → promote transition-chain
golden with the HASH-CHAINED ledger verified, retire-flag annotation, and
every guarded degrade.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import structlog.testing

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
pytest.importorskip("duckdb", reason="Catalog needs duckdb")

from brutex.engine.top100_artifact import load_top100
from brutex.nightly import champions
from brutex.nightly.champions import (
    CHAMPION_PROMOTE_NIGHTS,
    CHAMPION_PROMOTE_NIGHTS_ENV,
    CHAMPIONS_ENABLED_ENV,
    LEDGER_FILENAME,
    RUN_DELTA_FILENAME,
    STATE_FILENAME,
    _PooledVerdict,
    build_run_delta,
    challenger_eligible,
    champion_scope_key,
    load_champions_state,
    nightly_champions_enabled,
    resolve_promote_nights,
    run_champions_leg,
    run_champions_leg_guarded,
    save_champions_state,
)
from brutex.nightly.decay import entry_join_key
from brutex.nightly.decay_math import DECAY_RUST_ENV
from brutex.orchestrator.audit import read_entries, verify_chain

from tests.fakes.top100_fixture import (
    FIXTURE_NOW as NOW,
)
from tests.fakes.top100_fixture import (
    make_entry as _entry,
)
from tests.fakes.top100_fixture import (
    plant_index_lake as _plant_lake,
)
from tests.fakes.top100_fixture import (
    write_top100_fixture as write_artifact,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for var in (CHAMPIONS_ENABLED_ENV, CHAMPION_PROMOTE_NIGHTS_ENV, DECAY_RUST_ENV):
        monkeypatch.delenv(var, raising=False)
    return


def _dominating_verdict(*, dominates: bool = True, survives: bool = True) -> _PooledVerdict:
    return _PooledVerdict(
        challenger_survives=survives,
        challenger_provisional=False,
        dominates=dominates,
        challenger_lockbox_sharpe=1.5,
        champion_lockbox_sharpe=1.0,
        challenger_lockbox_mdd=0.01,
        champion_lockbox_mdd=0.02,
        detail="test verdict",
    )


def _patch_challenge(monkeypatch: pytest.MonkeyPatch, verdict: _PooledVerdict | None) -> None:
    monkeypatch.setattr(champions, "_challenge", lambda *a, **k: verdict)


class TestEnvKnobs:
    def test_enabled_default_on(self) -> None:
        assert nightly_champions_enabled() is True

    def test_enabled_blank_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CHAMPIONS_ENABLED_ENV, " ")
        assert nightly_champions_enabled() is True

    def test_enabled_falsy_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CHAMPIONS_ENABLED_ENV, "no")
        assert nightly_champions_enabled() is False

    def test_enabled_junk_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CHAMPIONS_ENABLED_ENV, "perhaps")
        with pytest.raises(ValueError, match=CHAMPIONS_ENABLED_ENV):
            nightly_champions_enabled()

    def test_promote_nights_default(self) -> None:
        assert resolve_promote_nights() == CHAMPION_PROMOTE_NIGHTS == 3

    def test_promote_nights_blank_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CHAMPION_PROMOTE_NIGHTS_ENV, "")
        assert resolve_promote_nights() == CHAMPION_PROMOTE_NIGHTS

    def test_promote_nights_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CHAMPION_PROMOTE_NIGHTS_ENV, "2")
        assert resolve_promote_nights() == 2

    def test_promote_nights_junk_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CHAMPION_PROMOTE_NIGHTS_ENV, "soon")
        with pytest.raises(ValueError, match="not an integer"):
            resolve_promote_nights()

    def test_promote_nights_zero_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(CHAMPION_PROMOTE_NIGHTS_ENV, "0")
        with pytest.raises(ValueError, match="must be >= 1"):
            resolve_promote_nights()


class TestScopeAndEligibility:
    def test_scope_key(self) -> None:
        assert champion_scope_key(_entry(1)) == "NIFTY/1minute/bull"

    def test_eligible_only_non_provisional_pass(self) -> None:
        assert challenger_eligible(_entry(1, cascade_status="pass")) is True
        assert challenger_eligible(_entry(1, cascade_status="provisional_pass")) is False
        assert challenger_eligible(_entry(1, cascade_status="fail")) is False
        entry = _entry(1)
        entry["cascade"] = "junk"
        assert challenger_eligible(entry) is False


class TestState:
    def test_absent_is_fresh(self, tmp_path: Path) -> None:
        state = load_champions_state(tmp_path)
        assert state == {"schema_version": 1, "champions": {}}

    def test_corrupt_json_is_fresh_with_warn(self, tmp_path: Path) -> None:
        (tmp_path / "champions").mkdir()
        (tmp_path / "champions" / STATE_FILENAME).write_text("{not json", encoding="utf-8")
        with structlog.testing.capture_logs() as logs:
            state = load_champions_state(tmp_path)
        assert state["champions"] == {}
        assert any(e["event"] == "nightly.champions.state_corrupt" for e in logs)

    def test_bad_shape_is_fresh_with_warn(self, tmp_path: Path) -> None:
        (tmp_path / "champions").mkdir()
        (tmp_path / "champions" / STATE_FILENAME).write_text('{"champions": 7}', encoding="utf-8")
        with structlog.testing.capture_logs() as logs:
            state = load_champions_state(tmp_path)
        assert state["champions"] == {}
        assert any(e["event"] == "nightly.champions.state_bad_shape" for e in logs)

    def test_roundtrip(self, tmp_path: Path) -> None:
        path = save_champions_state(tmp_path, {"schema_version": 1, "champions": {"k": {"era": 1}}})
        assert path == tmp_path / "champions" / STATE_FILENAME
        assert load_champions_state(tmp_path)["champions"] == {"k": {"era": 1}}


class TestRunDelta:
    def _artifact(self, tmp_path: Path, run_id: str, entries: list[dict[str, object]]):
        write_artifact(tmp_path, run_id, entries)
        art = load_top100(tmp_path / "sweep_runs", run_id)
        assert art is not None
        return art

    def test_no_prior(self, tmp_path: Path) -> None:
        current = self._artifact(tmp_path, "a" * 64, [_entry(1)])
        delta = build_run_delta(None, current)
        assert delta["status"] == "no_prior"
        assert delta["prior_run_id"] is None
        entered = delta["entered"]
        assert isinstance(entered, list) and len(entered) == 1

    def test_entered_dropped_moved(self, tmp_path: Path) -> None:
        prior = self._artifact(
            tmp_path,
            "a" * 64,
            [_entry(1, mask_hex="1".zfill(32)), _entry(2, mask_hex="2".zfill(32))],
        )
        current = self._artifact(
            tmp_path,
            "b" * 64,
            [_entry(1, mask_hex="2".zfill(32)), _entry(2, mask_hex="3".zfill(32))],
        )
        delta = build_run_delta(prior, current)
        assert delta["status"] == "ok"
        entered = delta["entered"]
        dropped = delta["dropped"]
        moves = delta["rank_moves"]
        assert isinstance(entered, list) and isinstance(dropped, list) and isinstance(moves, list)
        assert [e["join_key"] for e in entered] == [
            entry_join_key(_entry(2, mask_hex="3".zfill(32)))
        ]
        assert [d["join_key"] for d in dropped] == [
            entry_join_key(_entry(1, mask_hex="1".zfill(32)))
        ]
        # mask 2 moved rank 2 → 1.
        assert moves == [
            {
                "join_key": entry_join_key(_entry(1, mask_hex="2".zfill(32))),
                "from_rank": 2,
                "to_rank": 1,
            }
        ]
        assert delta["common"] == 1

    def test_same_rank_not_moved(self, tmp_path: Path) -> None:
        prior = self._artifact(tmp_path, "a" * 64, [_entry(1)])
        current = self._artifact(tmp_path, "b" * 64, [_entry(1)])
        delta = build_run_delta(prior, current)
        assert delta["rank_moves"] == []
        assert delta["common"] == 1


class TestTransitions:
    """The transition machine over deterministic pooled verdicts."""

    def _night(
        self,
        tmp_path: Path,
        run_id: str,
        entries: list[dict[str, object]],
        *,
        mtime: float,
        decay_block: Mapping[str, object] | None = None,
        alerter: object | None = None,
    ) -> dict[str, object]:
        # The leg WRITES run-delta.json into tonight's run dir, which
        # resets that dir's mtime to the wall clock — so the synthetic
        # night ordering must sit ABOVE any wall-clock value or a prior
        # night's delta-written dir would outrank a later synthetic one.
        write_artifact(tmp_path, run_id, entries, mtime=4.0e9 + mtime)
        return run_champions_leg(
            lake_root=tmp_path / "lake",
            results_root=tmp_path,
            decay_block=decay_block,
            alerter=alerter,  # type: ignore[arg-type]
            now_utc=NOW,
        )

    def test_no_artifact_skips(self, tmp_path: Path) -> None:
        block = run_champions_leg(lake_root=tmp_path, results_root=tmp_path, now_utc=NOW)
        assert block == {"status": "skipped", "reason": "no_top100_artifact"}

    def test_invalid_artifact_skips(self, tmp_path: Path) -> None:
        from brutex.engine.top100_artifact import TOP100_STAMP_FILENAME

        run_dir = write_artifact(tmp_path, "a" * 64, [_entry(1)])
        (run_dir / TOP100_STAMP_FILENAME).unlink()
        block = run_champions_leg(lake_root=tmp_path, results_root=tmp_path, now_utc=NOW)
        assert block["status"] == "skipped"
        assert block["reason"] == "top100_invalid"

    def test_seed_challenge_promote_chain_golden(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 4-night seed → challenge ×2 → PROMOTE transition golden.

        The pooled verdict is a deterministic dominating double (the REAL
        pooled cascade path is covered by ``TestChallengeRealPath`` — a
        full-real promotion needs a cascade-passing dataset, an honest
        limit disclosed in the PR); everything else is REAL: state.json,
        the hash-chained champions.jsonl ledger (verified below),
        run-delta.json, the report blocks.
        """
        _patch_challenge(monkeypatch, _dominating_verdict())
        champion = _entry(1, mask_hex="a".zfill(32))
        challenger = _entry(2, mask_hex="b".zfill(32))

        class _Alerter:
            def __init__(self) -> None:
                self.promoted: list[dict[str, object]] = []

            def champion_promoted(self, **kwargs: object) -> bool:
                self.promoted.append(dict(kwargs))
                return True

        alerter = _Alerter()
        # Night 1: no champion → SEED from the best eligible entry.
        block1 = self._night(tmp_path, "a" * 64, [champion], mtime=1000.0, alerter=alerter)
        assert block1["status"] == "ok"
        actions1 = [t["action"] for t in block1["transitions"]]  # type: ignore[index, union-attr]
        assert actions1 == ["SEEDED"]
        # Nights 2-3: the challenger dominates → CHALLENGED (streak 1, 2).
        block2 = self._night(
            tmp_path, "b" * 64, [champion, challenger], mtime=2000.0, alerter=alerter
        )
        block3 = self._night(
            tmp_path, "c" * 64, [champion, challenger], mtime=3000.0, alerter=alerter
        )
        streaks = [
            t["streak"]
            for b in (block2, block3)
            for t in b["transitions"]  # type: ignore[union-attr]
            if t["action"] == "CHALLENGED"
        ]
        assert streaks == [1, 2]
        # Night 4: streak reaches CHAMPION_PROMOTE_NIGHTS → PROMOTED.
        block4 = self._night(
            tmp_path, "d" * 64, [champion, challenger], mtime=4000.0, alerter=alerter
        )
        actions4 = [t["action"] for t in block4["transitions"]]  # type: ignore[index, union-attr]
        assert actions4 == ["PROMOTED"]
        assert len(alerter.promoted) == 1
        # State: the new champion carries era 2 + the era-tagged former.
        state = load_champions_state(tmp_path)
        record = state["champions"]["NIFTY/1minute/bull"]  # type: ignore[index, call-overload]
        assert record["era"] == 2
        assert record["join_key"] == entry_join_key(challenger)
        former = record["former"]
        assert isinstance(former, list) and len(former) == 1
        assert former[0]["retired_reason"] == "superseded_by_promotion"
        assert former[0]["join_key"] == entry_join_key(champion)
        # The hash-chained ledger verifies end-to-end (Golden Rule #8) and
        # carries the exact transition-chain golden.
        ledger_path = tmp_path / "champions" / LEDGER_FILENAME
        assert verify_chain(ledger_path) == 4
        entries = read_entries(ledger_path)
        assert [e.action for e in entries] == ["SEEDED", "CHALLENGED", "CHALLENGED", "PROMOTED"]
        assert {e.subject for e in entries} == {"champion:NIFTY/1minute/bull"}
        assert entries[-1].payload["era"] == 2
        assert entries[-1].payload["streak"] == 3
        # run-delta.json landed in tonight's run dir.
        delta = json.loads(
            (tmp_path / "sweep_runs" / ("d" * 64) / RUN_DELTA_FILENAME).read_text("utf-8")
        )
        assert delta["schema"] == "brutex.nightly.run_delta"
        assert delta["prior_run_id"] == "c" * 64

    def test_different_challenger_resets_streak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_challenge(monkeypatch, _dominating_verdict())
        champion = _entry(1, mask_hex="a".zfill(32))
        chall_b = _entry(2, mask_hex="b".zfill(32))
        chall_c = _entry(2, mask_hex="c".zfill(32))
        self._night(tmp_path, "a" * 64, [champion], mtime=1000.0)
        self._night(tmp_path, "b" * 64, [champion, chall_b], mtime=2000.0)
        block = self._night(tmp_path, "c" * 64, [champion, chall_c], mtime=3000.0)
        challenged = [t for t in block["transitions"] if t["action"] == "CHALLENGED"]  # type: ignore[union-attr]
        assert challenged[0]["streak"] == 1  # a NEW identity restarts the count

    def test_non_dominating_retains_and_resets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        champion = _entry(1, mask_hex="a".zfill(32))
        challenger = _entry(2, mask_hex="b".zfill(32))
        _patch_challenge(monkeypatch, _dominating_verdict())
        self._night(tmp_path, "a" * 64, [champion], mtime=1000.0)
        self._night(tmp_path, "b" * 64, [champion, challenger], mtime=2000.0)
        _patch_challenge(monkeypatch, _dominating_verdict(dominates=False))
        block = self._night(tmp_path, "c" * 64, [champion, challenger], mtime=3000.0)
        retained = [t for t in block["transitions"] if t["action"] == "RETAINED"]  # type: ignore[union-attr]
        assert retained[0]["reason"] == "challenger_did_not_dominate"
        # The streak state was cleared — a later domination restarts at 1.
        _patch_challenge(monkeypatch, _dominating_verdict())
        block = self._night(tmp_path, "d" * 64, [champion, challenger], mtime=4000.0)
        challenged = [t for t in block["transitions"] if t["action"] == "CHALLENGED"]  # type: ignore[union-attr]
        assert challenged[0]["streak"] == 1

    def test_pooled_check_failure_retains(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        champion = _entry(1, mask_hex="a".zfill(32))
        challenger = _entry(2, mask_hex="b".zfill(32))
        _patch_challenge(monkeypatch, _dominating_verdict())
        self._night(tmp_path, "a" * 64, [champion], mtime=1000.0)
        _patch_challenge(monkeypatch, None)
        block = self._night(tmp_path, "b" * 64, [champion, challenger], mtime=2000.0)
        retained = [t for t in block["transitions"] if t["action"] == "RETAINED"]  # type: ignore[union-attr]
        assert retained[0]["reason"] == "pooled_check_failed"

    def test_no_challenger_retains(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_challenge(monkeypatch, _dominating_verdict())
        champion = _entry(1, mask_hex="a".zfill(32))
        self._night(tmp_path, "a" * 64, [champion], mtime=1000.0)
        block = self._night(tmp_path, "b" * 64, [champion], mtime=2000.0)
        retained = [t for t in block["transitions"] if t["action"] == "RETAINED"]  # type: ignore[union-attr]
        assert retained[0]["reason"] == "no_challenger"

    def test_promotion_without_alerter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A promotion with NO alerter bound still lands (alert is optional)."""
        monkeypatch.setenv(CHAMPION_PROMOTE_NIGHTS_ENV, "1")
        _patch_challenge(monkeypatch, _dominating_verdict())
        champion = _entry(1, mask_hex="a".zfill(32))
        challenger = _entry(2, mask_hex="b".zfill(32))
        self._night(tmp_path, "a" * 64, [champion], mtime=1000.0)
        block = self._night(tmp_path, "b" * 64, [champion, challenger], mtime=2000.0)
        actions = [t["action"] for t in block["transitions"]]  # type: ignore[index, union-attr]
        assert actions == ["PROMOTED"]

    def test_decayed_join_keys_shapes(self) -> None:
        keys = champions._decayed_join_keys
        assert keys(None) == frozenset()
        assert keys({"status": "ok"}) == frozenset()
        assert keys({"entries": "junk"}) == frozenset()
        assert keys(
            {
                "entries": [
                    {"join_key": "k1", "verdict": "decayed"},
                    "junk",
                    {"join_key": "k2", "verdict": "healthy"},
                ]
            }
        ) == frozenset({"k1"})

    def test_seed_needs_an_eligible_entry(self, tmp_path: Path) -> None:
        block = self._night(tmp_path, "a" * 64, [_entry(1, cascade_status="fail")], mtime=1000.0)
        assert block["status"] == "ok"
        assert block["transitions"] == []
        assert load_champions_state(tmp_path)["champions"] == {}

    def test_decay_retire_flag_annotation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DECAYED champion is retire-FLAGGED (annotation, never deleted)."""
        _patch_challenge(monkeypatch, _dominating_verdict())
        champion = _entry(1, mask_hex="a".zfill(32))
        self._night(tmp_path, "a" * 64, [champion], mtime=1000.0)
        decay_block = {
            "status": "ok",
            "entries": [{"join_key": entry_join_key(champion), "verdict": "decayed"}],
        }
        block = self._night(tmp_path, "b" * 64, [champion], mtime=2000.0, decay_block=decay_block)
        actions = [t["action"] for t in block["transitions"]]  # type: ignore[index, union-attr]
        assert "RETIRE_FLAGGED" in actions
        record = load_champions_state(tmp_path)["champions"]["NIFTY/1minute/bull"]  # type: ignore[index, call-overload]
        assert record["retire_flag"] == {"reason": "decayed", "flagged_utc": NOW.isoformat()}
        # Idempotent: a second decayed night does not re-flag.
        block2 = self._night(tmp_path, "c" * 64, [champion], mtime=3000.0, decay_block=decay_block)
        assert "RETIRE_FLAGGED" not in [t["action"] for t in block2["transitions"]]  # type: ignore[index, union-attr]

    def test_corrupt_champion_record_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_challenge(monkeypatch, _dominating_verdict())
        save_champions_state(
            tmp_path, {"schema_version": 1, "champions": {"NIFTY/1minute/bull": "junk"}}
        )
        with structlog.testing.capture_logs() as logs:
            block = self._night(tmp_path, "a" * 64, [_entry(1)], mtime=1000.0)
        assert block["status"] == "ok"
        assert any(e["event"] == "nightly.champions.record_corrupt" for e in logs)

    def test_state_champions_not_dict_is_reset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_challenge(monkeypatch, _dominating_verdict())
        (tmp_path / "champions").mkdir()
        (tmp_path / "champions" / STATE_FILENAME).write_text(
            '{"schema_version": 1, "champions": {}, "extra": 1}', encoding="utf-8"
        )
        # Sabotage AFTER load-shape validation: patch load to return a
        # champions value of the wrong type (the belt inside the leg).
        monkeypatch.setattr(
            champions,
            "load_champions_state",
            lambda root: {"schema_version": 1, "champions": "junk"},
        )
        block = self._night(tmp_path, "a" * 64, [_entry(1)], mtime=1000.0)
        assert block["status"] == "ok"
        assert block["champions"] == 1  # reseeded over a fresh dict

    def test_delta_write_failure_is_soft(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_challenge(monkeypatch, _dominating_verdict())
        real_write = champions.atomic_write_simple

        def _refuse_delta(path: Path, write: object) -> object:
            if path.name == RUN_DELTA_FILENAME:
                raise OSError("disk says no")
            return real_write(path, write)  # type: ignore[arg-type]

        monkeypatch.setattr(champions, "atomic_write_simple", _refuse_delta)
        with structlog.testing.capture_logs() as logs:
            block = self._night(
                tmp_path, "a" * 64, [_entry(1, cascade_status="fail")], mtime=1000.0
            )
        assert block["status"] == "ok"
        summary = block["run_delta"]
        assert isinstance(summary, dict)
        assert summary["status"] == "write_failed"
        assert any(e["event"] == "nightly.champions.delta_write_failed" for e in logs)
        # The state save (the SAME helper) still landed — only the delta failed.
        assert (tmp_path / "champions" / STATE_FILENAME).is_file()

    def test_promoted_alerter_raise_is_suppressed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CHAMPION_PROMOTE_NIGHTS_ENV, "1")
        _patch_challenge(monkeypatch, _dominating_verdict())

        class _Raises:
            def champion_promoted(self, **kwargs: object) -> bool:
                raise RuntimeError("telegram down")

        champion = _entry(1, mask_hex="a".zfill(32))
        challenger = _entry(2, mask_hex="b".zfill(32))
        self._night(tmp_path, "a" * 64, [champion], mtime=1000.0)
        block = self._night(
            tmp_path, "b" * 64, [champion, challenger], mtime=2000.0, alerter=_Raises()
        )
        actions = [t["action"] for t in block["transitions"]]  # type: ignore[index, union-attr]
        assert actions == ["PROMOTED"]  # best-effort — never a leg failure


class TestChallengeRealPath:
    """The REAL pooled path: bars → Stage-1A → build_candidate_input →
    run_stage4_cascade. On the tiny fixture lake the cascade honestly
    REJECTS (the 500-trade lockbox floor cannot hold) — the domination
    verdict fields are still measured and the leg RETAINS, never promotes
    on unproven evidence."""

    def test_real_cascade_retains_honestly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        _plant_lake(lake)
        champion = _entry(1, mask_hex="a".zfill(32), direction="bear")
        challenger = _entry(2, mask_hex="0".zfill(32), direction="bear")
        # Future-based mtimes: the night-1 delta write resets run "a"'s
        # dir mtime to the wall clock (see TestTransitions._night).
        write_artifact(tmp_path, "a" * 64, [champion], mtime=4.0e9 + 1000.0)
        run_champions_leg(lake_root=lake, results_root=tmp_path, now_utc=NOW)
        write_artifact(tmp_path, "b" * 64, [champion, challenger], mtime=4.0e9 + 2000.0)
        # Bound the pooled window to the fixture month (cheap + hermetic).
        block = run_champions_leg(
            lake_root=lake,
            results_root=tmp_path,
            now_utc=NOW,
            from_date="2024-06-01",
            to_date="2024-06-19",
        )
        assert block["status"] == "ok"
        transitions = block["transitions"]
        assert isinstance(transitions, list)
        retained = [t for t in transitions if t["action"] == "RETAINED"]
        assert retained and retained[0]["reason"] == "challenger_did_not_dominate"

    def test_challenge_out_of_scope_instrument(self, tmp_path: Path) -> None:
        verdict = champions._challenge(
            {"mask_hex": "0" * 32, "direction": "bull"},
            _entry(1, instrument="RELIANCE"),
            lake_root=tmp_path,
            sweep_run_id="a" * 64,
            from_date=None,
            to_date=None,
            now=NOW,
        )
        assert verdict is None

    def test_challenge_unknown_tf(self, tmp_path: Path) -> None:
        verdict = champions._challenge(
            {"mask_hex": "0" * 32, "direction": "bull"},
            _entry(1, tf="banana"),
            lake_root=tmp_path,
            sweep_run_id="a" * 64,
            from_date=None,
            to_date=None,
            now=NOW,
        )
        assert verdict is None

    def test_challenge_empty_lake(self, tmp_path: Path) -> None:
        verdict = champions._challenge(
            {"mask_hex": "0" * 32, "direction": "bull"},
            _entry(1),
            lake_root=tmp_path / "lake",
            sweep_run_id="a" * 64,
            from_date="2024-06-01",
            to_date="2024-06-19",
            now=NOW,
        )
        assert verdict is None

    def test_challenge_fault_isolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = tmp_path / "lake"
        _plant_lake(lake)

        def _boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("stage1a exploded")

        from brutex.engine import stage1a

        monkeypatch.setattr(stage1a, "build_bar_bits", _boom)
        with structlog.testing.capture_logs() as logs:
            verdict = champions._challenge(
                {"mask_hex": "0" * 32, "direction": "bull"},
                _entry(1),
                lake_root=lake,
                sweep_run_id="a" * 64,
                from_date="2024-06-01",
                to_date="2024-06-19",
                now=NOW,
            )
        assert verdict is None
        assert any(e["event"] == "nightly.champions.pooled_check_failed" for e in logs)

    def test_pooled_check_undefined_lockbox_sharpe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A one-sided undefined lockbox Sharpe → no domination claim."""
        lake = tmp_path / "lake"
        _plant_lake(lake)
        monkeypatch.setattr(champions, "_lockbox_metrics", lambda returns: (None, 0.0))
        verdict = champions._challenge(
            {"mask_hex": "a".zfill(32), "direction": "bear", "sl_paisa": None, "tp_paisa": None},
            _entry(1, direction="bear"),
            lake_root=lake,
            sweep_run_id="a" * 64,
            from_date="2024-06-01",
            to_date="2024-06-19",
            now=NOW,
        )
        assert verdict is not None
        assert verdict.dominates is False
        assert "undefined" in verdict.detail

    def test_pooled_check_defined_sharpes_measure_domination(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both lockbox Sharpes defined → the kernel domination arm runs.

        The tiny fixture lake cannot produce a 500-trade lockbox, so the
        lockbox metrics are stubbed per call (challenger measured FIRST in
        ``_pooled_check``); the challenger strictly dominates. The
        challenger entry also carries a NON-Mapping ``arm`` — the
        arm-absent fold branch of ``_challenge``.
        """
        lake = tmp_path / "lake"
        _plant_lake(lake)
        measured = iter([(1.5, 0.01), (1.0, 0.02)])  # challenger, champion
        monkeypatch.setattr(champions, "_lockbox_metrics", lambda returns: next(measured))
        challenger = _entry(1, direction="bear")
        challenger["arm"] = None  # the non-Mapping arm branch
        verdict = champions._challenge(
            {"mask_hex": "a".zfill(32), "direction": "bear", "sl_paisa": None, "tp_paisa": None},
            challenger,
            lake_root=lake,
            sweep_run_id="a" * 64,
            from_date="2024-06-01",
            to_date="2024-06-19",
            now=NOW,
        )
        assert verdict is not None
        assert verdict.dominates is True
        assert verdict.challenger_lockbox_sharpe == 1.5
        assert verdict.champion_lockbox_sharpe == 1.0
        assert "lockbox sharpe" in verdict.detail

    def test_window_bounds_defaults(self) -> None:
        from brutex.dashboard.pull_controller import GROWW_HISTORY_FLOOR

        start, end = champions._window_bounds(None, None, now=NOW)
        assert start.date() == GROWW_HISTORY_FLOOR
        assert end == NOW
        start2, end2 = champions._window_bounds("2024-06-01", "2024-06-19", now=NOW)
        assert start2.date().isoformat() == "2024-06-01"
        assert end2.date().isoformat() == "2024-06-20"  # +1 day inclusive law


class TestGuardedWrapper:
    def test_disabled(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(CHAMPIONS_ENABLED_ENV, "false")
        block = run_champions_leg_guarded(lake_root=tmp_path, results_root=tmp_path)
        assert block == {"status": "disabled"}

    def test_flag_junk_is_env_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(CHAMPIONS_ENABLED_ENV, "junk")
        block = run_champions_leg_guarded(lake_root=tmp_path, results_root=tmp_path)
        assert block["status"] == "env_error"

    def test_knob_junk_is_env_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(CHAMPION_PROMOTE_NIGHTS_ENV, "junk")
        write_artifact(tmp_path, "a" * 64, [_entry(1)])
        block = run_champions_leg_guarded(lake_root=tmp_path, results_root=tmp_path)
        assert block["status"] == "env_error"

    def test_unexpected_error_is_fail_open(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def _boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("leg exploded")

        monkeypatch.setattr(champions, "discover_top100_runs", _boom)
        block = run_champions_leg_guarded(lake_root=tmp_path, results_root=tmp_path)
        assert block["status"] == "error"
        assert block["error_type"] == "RuntimeError"

    def test_success_passthrough(self, tmp_path: Path) -> None:
        block = run_champions_leg_guarded(lake_root=tmp_path, results_root=tmp_path)
        assert block == {"status": "skipped", "reason": "no_top100_artifact"}
