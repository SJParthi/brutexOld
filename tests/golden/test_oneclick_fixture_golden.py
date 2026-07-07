"""Golden gate for the one-click artifact fixture corpus (DEC-OPERATOR-PAGE-REAL-001).

Pins the ``tests/fixtures/oneclick`` corpus byte-for-byte (SHA-256 manifest —
the fixtures/README.md fixture law) and proves the corpus round-trips through
the REAL adapter: the manifest/top100 load ``ok``, the events chain verifies
end-to-end, the funnel reconciles to the digit, and every twin
(empty/short/corrupt/hostile) produces its NAMED status.

CONTRACT-ONLY honesty (T-M1): the chain golden proves fixture↔verifier
SELF-consistency (the generator imports :func:`chain_hash` from the adapter —
ONE algorithm definition), NOT P2 compatibility; the algorithm is ASSUMED
until P2 merges and every verdict detail says so.

Regenerate deliberately via ``python tests/golden/generate_oneclick_fixture.py``
then refresh ``_EXPECTED_SHA256`` here.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

from brutex.dashboard import oneclick_artifacts as oc
from brutex.dashboard import oneclick_view as ov

from tests.golden import generate_oneclick_fixture as gen

_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "oneclick"
_RUNS = _FIXTURE_ROOT / "runs"

#: The committed corpus, byte-pinned (regenerate + refresh deliberately only).
_EXPECTED_SHA256: dict[str, str] = {
    "runs/r-20260707-fixt01/events.jsonl": (
        "9fc78ff471650116ab4efa8a1318ece908027d7485d0d9ea75655f71ac6171ad"
    ),
    "runs/r-20260707-fixt01/manifest.json": (
        "344184a57b869707f2497d8a4a78687c051da7a9c8dca6191c18a0f010cb4a7a"
    ),
    "runs/r-20260707-fixt01/top100.json": (
        "85c6bf848afd8c89ebf32b0c0bd674d6dbbe59d785eee92e1adbe0da2f0b0e69"
    ),
    "runs/r-20260707-fixt01/trades/rank-0001.json": (
        "3b2c4ce7d42ff046c27e6a262128f1fd2273f51ef78932ac6d9f4c1edf977386"
    ),
    "runs/r-corrupt01/events.jsonl": (
        "98d5bf94846e2a31a9251c5bef552346de8c9b8a9e485a79dd88338d29b134f5"
    ),
    "runs/r-corrupt01/top100.json": (
        "cb42f6c86411c80e6987d6de8c79e00a67eb5199ab8d6e13b18843f2ff0ac544"
    ),
    "runs/r-empty01/top100.json": (
        "37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570"
    ),
    "runs/r-hostile01/events.jsonl": (
        "c8150cf2c68dfd432632e96c6fed3221caa8e524bd9849458ba7a59c1c3b5ba9"
    ),
    "runs/r-hostile01/manifest.json": (
        "cdcabe6fdd16bf59275c8c2bc3b9470e0f07830f17714bd381d06c456b4a7422"
    ),
    "runs/r-hostile01/top100.json": (
        "08aa0ac074cf5408adc83351d4d4fdbbcfda39723fa1b7ecc6d9b4ea93dc59db"
    ),
    "runs/r-short01/top100.json": (
        "9bfd33205aeefd7db6278858c9e9a0160128d6a0098864912f386ef4e669678b"
    ),
}


class TestCorpusPinned:
    def test_sha256_manifest_matches_the_committed_corpus(self) -> None:
        actual = {
            path.relative_to(_FIXTURE_ROOT).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(_FIXTURE_ROOT.rglob("*"))
            if path.is_file()
        }
        assert actual == _EXPECTED_SHA256

    def test_generator_is_deterministic(self, tmp_path: Path) -> None:
        """A regeneration into tmp reproduces the committed bytes exactly."""
        gen.generate(tmp_path)
        for rel, expected in _EXPECTED_SHA256.items():
            regenerated = tmp_path / rel
            assert hashlib.sha256(regenerated.read_bytes()).hexdigest() == expected, rel


class TestMainRunRoundTrips:
    def test_manifest_loads_ok_and_funnel_reconciles(self) -> None:
        manifest = oc.load_manifest(_RUNS / gen.RUN_ID)
        assert manifest.status == "ok"
        verdict = ov.reconcile_funnel(manifest)
        assert verdict["ok"] is True  # hand-derived arithmetic, to the digit

    def test_top100_loads_ok_with_100_rows_and_edge_classes(self) -> None:
        top100 = oc.load_top100(_RUNS / gen.RUN_ID)
        assert top100.status == "ok"
        assert len(top100.rows) == 100
        by_rank = {row["rank"]: row for row in top100.rows}
        assert by_rank[gen.ZERO_TRADES_RANK]["metrics"]["trades"] == 0
        assert by_rank[gen.NEGATIVE_PF_RANK]["metrics"]["profit_factor"] == -0.5
        assert by_rank[gen.HUGE_DD_RANK]["metrics"]["max_dd_paisa"] == -9_999_999_999
        assert by_rank[gen.NULL_VIX_RANK]["vix_band"] is None
        tie_a, tie_b = gen.TIE_RANKS
        assert by_rank[tie_a]["metrics"]["sharpe"] == by_rank[tie_b]["metrics"]["sharpe"]

    def test_events_chain_verifies_end_to_end(self) -> None:
        """Self-consistency ONLY: generator and verifier share ONE chain_hash."""
        ok, checked, break_line, detail = oc.verify_events_chain(_RUNS / gen.RUN_ID)
        assert (ok, break_line) == (True, None)
        assert checked == 118  # 8 starts + 100 tuples + tripwire + checkpoint + 8 dones
        assert "(algorithm assumed until P2 merges)" in detail

    def test_trades_fixture_pages_through_the_reader(self) -> None:
        reader = oc.FixtureTradesReader(_FIXTURE_ROOT)
        page = reader.page(gen.RUN_ID, 1, offset=0, limit=1000)
        assert page.status == "ok"
        assert page.total == gen.TRADE_ROW_COUNT
        assert len(page.rows) == 412
        # R31 honest NULL: every 13th row carries null VIX.
        assert page.rows[0]["vix_entry"] is None
        assert page.rows[1]["vix_entry"] is not None


class TestTwinsProduceNamedStatuses:
    def test_empty_twin_is_ok_zero_rows(self) -> None:
        result = oc.load_top100(_RUNS / gen.EMPTY_RUN_ID)
        assert result.status == "ok"
        assert result.rows == ()

    def test_short_twin_is_ok_seventeen_rows(self) -> None:
        result = oc.load_top100(_RUNS / gen.SHORT_RUN_ID)
        assert result.status == "ok"
        assert len(result.rows) == gen.SHORT_ROW_COUNT

    def test_corrupt_twin_top100_is_corrupt(self) -> None:
        assert oc.load_top100(_RUNS / gen.CORRUPT_RUN_ID).status == "corrupt"

    def test_corrupt_twin_events_have_the_half_written_tail(self) -> None:
        page = oc.tail_events(_RUNS / gen.CORRUPT_RUN_ID, limit=1000)
        assert page.status == "ok"
        assert page.truncated_tail is True
        assert len(page.records) == 2
        ok, checked, _, _ = oc.verify_events_chain(_RUNS / gen.CORRUPT_RUN_ID)
        assert (ok, checked) == (True, 2)  # the partial tail is tolerated, not a break

    def test_hostile_twin_parses_with_strings_as_data(self) -> None:
        manifest = oc.load_manifest(_RUNS / gen.HOSTILE_RUN_ID)
        assert manifest.status == "ok"
        assert manifest.manifest is not None
        assert manifest.manifest["suspect"] is True
        top100 = oc.load_top100(_RUNS / gen.HOSTILE_RUN_ID)
        assert top100.status == "ok"
        assert top100.rows[0]["conditions"][0] == gen.HOSTILE_STRINGS[0]
        page = oc.tail_events(_RUNS / gen.HOSTILE_RUN_ID)
        assert page.records[0]["kind"] == gen.HOSTILE_STRINGS[0]
        ok, checked, _, _ = oc.verify_events_chain(_RUNS / gen.HOSTILE_RUN_ID)
        assert (ok, checked) == (True, 2)  # hostile strings chain like any data

    def test_hostile_strings_are_verbatim_in_the_committed_bytes(self) -> None:
        text = (_RUNS / gen.HOSTILE_RUN_ID / "top100.json").read_text(encoding="utf-8")
        assert "<script>alert(1)</script>" in text


class TestGeneratorContract:
    def test_generator_imports_the_adapter_chain_seam(self) -> None:
        """ONE algorithm definition: the generator has no chain copy of its own."""
        source = (Path(gen.__file__)).read_text(encoding="utf-8")
        assert "from brutex.dashboard.oneclick_artifacts import" in source
        assert "def chain_hash" not in source  # never re-minted
        assert "sha256(" not in source  # the hashing lives in the adapter only

    def test_manifest_arithmetic_constants_agree(self) -> None:
        assert gen.ENUMERATED == gen.TESTED + gen.SKIPPED
        stages = dict(gen.FUNNEL_STAGES)
        kills = dict(gen.DOOR_KILLS)
        names = [name for name, _ in gen.FUNNEL_STAGES]
        for prev_name, cur_name in itertools.pairwise(names):
            assert stages[cur_name] == stages[prev_name] - kills[cur_name]

    def test_manifest_json_matches_the_builder(self) -> None:
        committed = json.loads((_RUNS / gen.RUN_ID / "manifest.json").read_text(encoding="utf-8"))
        assert committed == gen.build_manifest()
