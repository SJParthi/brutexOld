"""Bench — the /db SPOT-cascade FNO-pruned walk is FLAT vs F&O contract count (G2).

DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001 (MIGRATION.md #1751 G2 — the
round-5 review's committed bench artifact). **Honest, narrow claims (Golden
Rule #10):**

* the FNO-pruned discovery walk — ``list_candle_datasets(lake,
  exclude_segments={FNO})``, the spot-cascade ``_build`` composition in
  ``app.py`` — performs walk work INDEPENDENT of the F&O contract-dir count:
  the prune happens at SEGMENT-dir descent
  (DEC-COMPLETENESS-FNO-SKIP-001), so the ~10⁵-10⁶ per-contract partition
  dirs of a materialised options lake are never even enumerated. Pinned
  DETERMINISTICALLY via the walk's own per-symbol-dir liveness beat
  (``emit_progress_beat`` fires once per WALKED symbol dir — the descent
  proxy): the pruned walk's beat count is IDENTICAL on a 3-contract and a
  120-contract lake (and equals the SPOT symbol count), while the UNFILTERED
  walk's beat count grows with the contract count — the teeth proving the spy
  measures real descent, so a prune regression cannot pass both pins.
* what is **NOT** claimed in-CI: the wall-clock figures in the ``_build``
  docstring + MIGRATION #1751 (42× on the synthetic repro's WALK, 420.0 ms →
  10.0 ms; 29.7 min at 2.36M files for the production unfiltered walk) are
  OUT-OF-CI measurements — this bench pins the STRUCTURAL flatness those
  figures follow from, never shared-CI milliseconds (tiny-tree wall clock is
  noise; the deterministic op count is the primary teeth — the #1614 house
  pattern).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.dashboard import db_view
from brutex.dashboard.db_view import list_candle_datasets
from brutex.dashboard.discovery_warm import filter_spot_datasets
from brutex.dashboard.spot_cascade import build_spot_cascade
from brutex.data.fno_candle_lake import FNO_CANDLE_LAKE_SEGMENT

_FNO_EXCLUDE = frozenset({FNO_CANDLE_LAKE_SEGMENT})
#: One beat fires per walked SYMBOL dir — the fixture holds exactly these two
#: spot symbols, so the pruned walk's beat count is this constant on EVERY lake.
_SPOT_SYMBOL_DIRS = 2
_SMALL_CONTRACTS = 3
_BIG_CONTRACTS = 120


def _build_lake(root: Path, *, fno_contracts: int) -> Path:
    """A manifest-less lake: 2 spot symbols + *fno_contracts* option contract dirs.

    No window ledger is written, so ``ledger_stat_token`` is ``None`` and the
    UNFILTERED walk never rebuilds a dataset manifest here — every call below
    measures the real ``bars/`` tree walk, never a manifest-first serve.
    """
    for segment, symbol in (("INDEX", "NIFTY"), ("CASH", "RELIANCE")):
        tf_dir = root / "bars" / "NSE" / segment / symbol / "1minute" / "2024"
        tf_dir.mkdir(parents=True)
        (tf_dir / "01.parquet").write_bytes(b"")
    for i in range(fno_contracts):
        contract = f"NSE-NIFTY-30Jan25-{24000 + i * 50}-CE"
        tf_dir = root / "bars" / "NSE" / "FNO" / contract / "1minute" / "2024"
        tf_dir.mkdir(parents=True)
        (tf_dir / "01.parquet").write_bytes(b"")
    return root


def _walk_beats(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Spy the walk's per-symbol-dir liveness beat (the descent op counter)."""
    beats = [0]

    def _beat() -> None:
        beats[0] += 1

    monkeypatch.setattr(db_view, "emit_progress_beat", _beat)
    return beats


class TestSpotCascadeFnoPrunedWalkFlat:
    """G2: pruned-walk descent ops are FLAT vs the F&O contract count."""

    def test_pruned_walk_ops_flat_vs_fno_contract_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The FNO-pruned walk never descends the contract tree: its beat
        count is the SPOT symbol count on the small AND the big lake —
        identical (flat), not merely bounded — and the dataset rows match."""
        small = _build_lake(tmp_path / "small", fno_contracts=_SMALL_CONTRACTS)
        big = _build_lake(tmp_path / "big", fno_contracts=_BIG_CONTRACTS)
        beats = _walk_beats(monkeypatch)

        beats[0] = 0
        small_rows = list_candle_datasets(small, exclude_segments=_FNO_EXCLUDE)
        small_beats = beats[0]
        beats[0] = 0
        big_rows = list_candle_datasets(big, exclude_segments=_FNO_EXCLUDE)
        big_beats = beats[0]

        assert small_beats == _SPOT_SYMBOL_DIRS
        assert big_beats == _SPOT_SYMBOL_DIRS  # FLAT: 40× the contracts, same ops
        assert small_rows == big_rows  # the identical spot dataset list
        assert all(row["segment"] != FNO_CANDLE_LAKE_SEGMENT for row in big_rows)

    def test_unfiltered_walk_ops_grow_with_contracts_teeth(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TEETH: the UNFILTERED walk's beat count is spot + every contract
        dir — proving the spy measures real per-symbol descent, so a prune
        regression (descending FNO then filtering rows) fails the flat pin."""
        big = _build_lake(tmp_path / "big", fno_contracts=_BIG_CONTRACTS)
        beats = _walk_beats(monkeypatch)

        # Pruned FIRST (defensive ordering): the unfiltered walk is the only
        # arm that may rebuild a manifest — token-less here (no ledger), but
        # the order keeps the pruned measurement walk-only by construction.
        beats[0] = 0
        pruned_rows = list_candle_datasets(big, exclude_segments=_FNO_EXCLUDE)
        assert beats[0] == _SPOT_SYMBOL_DIRS
        beats[0] = 0
        unfiltered_rows = list_candle_datasets(big)
        assert beats[0] == _SPOT_SYMBOL_DIRS + _BIG_CONTRACTS
        # The provable-no-op belt (the app.py `_build` composition): pruning
        # at descent equals filtering the unfiltered rows after the fact.
        assert pruned_rows == filter_spot_datasets(unfiltered_rows)

    def test_pruned_build_composition_matches_unfiltered_filtered(self, tmp_path: Path) -> None:
        """The full G2 composition — ``build_spot_cascade(filter_spot_datasets(
        pruned))`` — yields the SAME cascade as filtering the unfiltered walk
        (byte-equal symbol leaves), so the prune can never drop a spot row."""
        big = _build_lake(tmp_path / "big", fno_contracts=_SMALL_CONTRACTS)
        pruned = build_spot_cascade(
            filter_spot_datasets(list_candle_datasets(big, exclude_segments=_FNO_EXCLUDE))
        )
        unfiltered = build_spot_cascade(filter_spot_datasets(list_candle_datasets(big)))
        assert pruned.symbols() == unfiltered.symbols()
        assert [sym.symbol for sym in pruned.symbols()] == ["NIFTY", "RELIANCE"]
