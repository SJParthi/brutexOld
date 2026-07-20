"""Unit tests for the Stage-3 survivor selection (`brutex.engine.stage3_select`).

DEC-STAGE3-TOPK-001 (ADR-0007 §5 slice 2) — 100% line+branch of the top-K query
over the persisted leaderboard: per-(instrument, TF, mode) grouping (direction
competes INSIDE a leaderboard, never as a key), the Sharpe-descending /
run_id-ascending deterministic ordering (the proven store discipline), the K
trim + the spec-default constant (10,000 — coded nowhere before this slice),
the ``top_k < 1`` guards on BOTH entry points, the deterministic leaderboard
iteration order, and the real-Parquet disk round-trip through
``load_results_tree`` (missing root raises; empty tree yields {}).

DEC-SWEEP-STREAMING-RANK-002 (#1294) replaced the accumulate-then-two-pass-sort
core with a bounded per-key top-K heap, and DEC-RUST-STAGE3-REFINE-001 moved
that heap into the ``brutex_refine`` Rust wheel (``SelectSession``);
``_group_survivors_reference`` below is a VERBATIM copy of the pre-#1294
pure-Python implementation and the golden-equality suite
(:class:`TestGoldenEqualityVsReference`) pins byte-identical output (``repr``
string equality — key order AND per-board row order AND every row field) over
adversarial fixtures: equal-Sharpe tie clusters larger than K, duplicate
run_ids (full ties AND differing Sharpe), fewer-than-K / exactly-K / K+1,
multiple leaderboards, extreme-finite Sharpe, and seeded random permutations —
the cross-language differential oracle for the wheel-backed selection.
``NaN`` Sharpe is OUT of the domain — ``ResultRow`` rejects non-finite metrics
at construction (pinned here), so neither implementation ever sees one. The
comparator tie-break laws the retired private ``_HeapEntry`` unit tests
pinned (Sharpe desc → run_id asc → insertion order; signed-zero equality) are
now pinned twice: crate-side in ``rust/brutex-refine/src/select.rs``'s unit
matrix, and end-to-end here through the public API.
"""

from __future__ import annotations

import random
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.engine.refine_rust import REFINE_RUST_ENV, RefineRustUnavailableError
from brutex.engine.results_store import ResultRow, ResultsStore, ResultsStoreError
from brutex.engine.stage3_select import (
    DEFAULT_STAGE3_TOP_K,
    LeaderboardKey,
    group_survivors,
    select_stage3_survivors,
)

pytest.importorskip("brutex_refine")  # the Stage-3 Rust wheel — built in the CI unit job


def _rid(seed: int) -> str:
    """Return a valid 64-char lowercase-hex run-identity from *seed*."""
    return f"{seed:064x}"


def _row(**overrides: object) -> ResultRow:
    """Build a valid :class:`ResultRow`, overriding any field by keyword."""
    base: dict[str, object] = {
        "run_id": _rid(1),
        "instrument": "NIFTY",
        "tf": "5m",
        "direction": "bull",
        "mode": "A",
        "mask_bits": 0b101,
        "net_pnl_paisa": 1_000,
        "gross_profit_paisa": 1_500,
        "gross_loss_paisa": 500,
        "max_drawdown_paisa": 250,
        "expectancy_paisa": 25,
        "total_trades": 40,
        "winning_trades": 25,
        "losing_trades": 15,
        "sharpe_ratio": 1.5,
        "sortino_ratio": 2.0,
        "calmar_ratio": 1.25,
        "profit_factor": 1.8,
        "win_rate": 0.625,
        "max_drawdown_pct": 12.5,
        "avg_entry_price": Decimal("14020.50"),
        "avg_exit_price": Decimal("14100.25"),
    }
    base.update(overrides)
    # The store's tag-coherence law (review round 1): a pareto_member=True
    # row must carry the "frontier" basis token (production stamps both
    # from the ONE frontier set — the helper mirrors it).
    if base.get("pareto_member") is True and "selection_basis" not in base:
        base["selection_basis"] = "frontier"
    return ResultRow(**base)  # type: ignore[arg-type]


class TestGroupSurvivors:
    """The pure per-leaderboard grouping + ordering + trim core."""

    def test_groups_by_instrument_tf_mode(self) -> None:
        rows = [
            _row(run_id=_rid(1), instrument="NIFTY", tf="5m", mode="A"),
            _row(run_id=_rid(2), instrument="NIFTY", tf="15m", mode="A"),
            _row(run_id=_rid(3), instrument="BANKNIFTY", tf="5m", mode="A"),
            _row(run_id=_rid(4), instrument="NIFTY", tf="5m", mode="B"),
        ]
        boards = group_survivors(rows)
        assert set(boards) == {
            LeaderboardKey("NIFTY", "5m", "A"),
            LeaderboardKey("NIFTY", "15m", "A"),
            LeaderboardKey("BANKNIFTY", "5m", "A"),
            LeaderboardKey("NIFTY", "5m", "B"),
        }
        assert all(len(board) == 1 for board in boards.values())

    def test_direction_competes_inside_one_leaderboard(self) -> None:
        # Spec §Stage 3: direction is NOT a leaderboard key — a bear mask can
        # outrank a bull mask on the SAME (instrument, TF, mode) board.
        rows = [
            _row(run_id=_rid(1), direction="bull", sharpe_ratio=1.0),
            _row(run_id=_rid(2), direction="bear", sharpe_ratio=2.0),
        ]
        boards = group_survivors(rows, top_k=1)
        (board,) = boards.values()
        assert [r.run_id for r in board] == [_rid(2)]  # the bear mask won

    def test_leaderboard_order_sharpe_desc_runid_tiebreak(self) -> None:
        # The proven store discipline: Sharpe descending, ties run_id ASC —
        # identical for any insertion order (Golden Rule #4).
        rows = [
            _row(run_id=_rid(3), sharpe_ratio=1.0),
            _row(run_id=_rid(1), sharpe_ratio=1.0),
            _row(run_id=_rid(2), sharpe_ratio=2.0),
        ]
        boards = group_survivors(rows)
        (board,) = boards.values()
        assert [r.run_id for r in board] == [_rid(2), _rid(1), _rid(3)]
        # Insertion-order independence: reversed input → identical selection.
        boards_rev = group_survivors(list(reversed(rows)))
        assert boards_rev == boards

    def test_trims_each_leaderboard_to_top_k(self) -> None:
        rows = [_row(run_id=_rid(i), sharpe_ratio=float(i)) for i in range(1, 6)]
        # Legacy single-key arm: the Sharpe top-2 exactly (the byte-identity
        # arm the golden-equality suite pins).
        boards = group_survivors(rows, top_k=2, multi_axis=False)
        (board,) = boards.values()
        assert [r.sharpe_ratio for r in board] == [5.0, 4.0]
        # LANE-W1 union default (DEC-RANK-POOL-UNION-ADMIT-001): the SAME
        # top-2 CAP, membership by round-robin — source 1 (Sharpe) admits
        # 5.0, source 2 (the DEFAULT preset; every non-Sharpe axis is uniform
        # on these rows so its order falls to run_id ASC) admits rid(1) —
        # emitted in TODAY's Sharpe-desc discipline.
        union_boards = group_survivors(rows, top_k=2)
        (union_board,) = union_boards.values()
        assert [r.sharpe_ratio for r in union_board] == [5.0, 1.0]
        assert len(union_board) == 2  # the cap NEVER grows

    def test_smaller_leaderboard_yields_all_rows(self) -> None:
        boards = group_survivors([_row()], top_k=DEFAULT_STAGE3_TOP_K)
        (board,) = boards.values()
        assert len(board) == 1

    def test_spec_default_is_ten_thousand(self) -> None:
        # TRACK1_BACKTEST_SPEC.md:407 / row #37 — documented everywhere,
        # coded nowhere until this slice.
        assert DEFAULT_STAGE3_TOP_K == 10_000

    def test_iteration_order_is_sorted_leaderboard_keys(self) -> None:
        rows = [
            _row(run_id=_rid(1), instrument="NIFTY", tf="5m"),
            _row(run_id=_rid(2), instrument="BANKNIFTY", tf="5m"),
            _row(run_id=_rid(3), instrument="NIFTY", tf="15m"),
        ]
        keys = list(group_survivors(rows))
        assert keys == sorted(keys, key=lambda k: (k.instrument, k.tf, k.mode))

    def test_empty_rows_yield_empty_dict(self) -> None:
        assert group_survivors([]) == {}

    def test_top_k_below_one_rejected(self) -> None:
        with pytest.raises(ResultsStoreError, match="must be >= 1"):
            group_survivors([_row()], top_k=0)


class TestSelectStage3Survivors:
    """The disk-facing composition over the persisted per-tuple tree."""

    def _persist(self, root: Path, *stores: tuple[str, str, list[ResultRow]]) -> None:
        """Flush each (instrument, tf, rows) group as one per-tuple Parquet."""
        for index, (instrument, tf, rows) in enumerate(stores):
            store = ResultsStore(path=root / instrument / tf / f"part{index}.parquet")
            store.extend(rows)
            store.flush()

    def test_round_trips_persisted_tree_per_leaderboard(self, tmp_path: Path) -> None:
        self._persist(
            tmp_path,
            (
                "NIFTY",
                "5m",
                [
                    _row(run_id=_rid(1), sharpe_ratio=1.0),
                    _row(run_id=_rid(2), sharpe_ratio=3.0),
                    _row(run_id=_rid(3), sharpe_ratio=2.0),
                ],
            ),
            (
                "BANKNIFTY",
                "5m",
                [_row(run_id=_rid(4), instrument="BANKNIFTY", sharpe_ratio=9.9)],
            ),
        )
        boards = select_stage3_survivors(tmp_path, top_k=2, multi_axis=False)
        nifty = boards[LeaderboardKey("NIFTY", "5m", "A")]
        assert [r.run_id for r in nifty] == [_rid(2), _rid(3)]  # top-2, Sharpe desc
        bank = boards[LeaderboardKey("BANKNIFTY", "5m", "A")]
        assert [r.run_id for r in bank] == [_rid(4)]
        # The union DEFAULT threads through the disk composition too (LANE
        # W1): same cap, round-robin membership (Sharpe best rid(2) + the
        # preset source's run_id-ASC best rid(1) — uniform non-Sharpe axes).
        union_boards = select_stage3_survivors(tmp_path, top_k=2)
        union_nifty = union_boards[LeaderboardKey("NIFTY", "5m", "A")]
        assert [r.run_id for r in union_nifty] == [_rid(2), _rid(1)]

    def test_existing_empty_tree_yields_empty_dict(self, tmp_path: Path) -> None:
        assert select_stage3_survivors(tmp_path) == {}

    def test_missing_root_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ResultsStoreError, match="results root not found"):
            select_stage3_survivors(tmp_path / "absent")

    def test_top_k_guard_fires_before_the_tree_load(self, tmp_path: Path) -> None:
        # Fail-fast contract: a bad K must raise even when root is ALSO bad —
        # proving the guard precedes the (potentially large) disk load.
        with pytest.raises(ResultsStoreError, match="must be >= 1"):
            select_stage3_survivors(tmp_path / "absent", top_k=0)


def _group_survivors_reference(
    rows: list[ResultRow], *, top_k: int = DEFAULT_STAGE3_TOP_K
) -> dict[LeaderboardKey, tuple[ResultRow, ...]]:
    """VERBATIM pre-#1294 implementation — the golden oracle for byte-identity.

    Copied unchanged from ``group_survivors`` as merged by DEC-STAGE3-TOPK-001
    (the O(candidates)-RAM accumulate-then-two-pass-sort the bounded heap of
    DEC-SWEEP-STREAMING-RANK-002 replaces). Kept in the TEST only.
    """
    if top_k < 1:
        raise ResultsStoreError(f"stage3 top_k must be >= 1, got {top_k}")
    grouped: dict[LeaderboardKey, list[ResultRow]] = {}
    for row in rows:
        key = LeaderboardKey(instrument=row.instrument, tf=row.tf, mode=row.mode)
        grouped.setdefault(key, []).append(row)
    selected: dict[LeaderboardKey, tuple[ResultRow, ...]] = {}
    for key in sorted(grouped, key=lambda k: (k.instrument, k.tf, k.mode)):
        # Stable two-pass sort: run_id asc first (the tie-break), then Sharpe
        # desc — Python's stable sort preserves run_id order inside ties
        # (mirrors ResultsStore.top_k, CLAUDE.md §11 COMMON).
        board = sorted(grouped[key], key=lambda r: r.run_id)
        board.sort(key=lambda r: r.sharpe_ratio, reverse=True)
        selected[key] = tuple(board[:top_k])
    return selected


def _assert_byte_identical(
    new: dict[LeaderboardKey, tuple[ResultRow, ...]],
    ref: dict[LeaderboardKey, tuple[ResultRow, ...]],
) -> None:
    """Assert *new* is byte-identical to *ref* when serialized.

    ``repr`` of the dict serializes key ITERATION order, per-board ROW order,
    and every field of every frozen dataclass row — so string equality is a
    byte-level pin, strictly stronger than ``==`` (which ignores dict order).
    """
    assert list(new) == list(ref)  # key iteration order
    assert new == ref  # structural equality (field-for-field rows)
    assert repr(new) == repr(ref)  # the serialized byte pin


class TestTieBreakLawsViaPublicApi:
    """The comparator laws through the PUBLIC selection (the retired private
    ``_HeapEntry`` pins, re-expressed end-to-end; the crate's unit matrix in
    ``rust/brutex-refine/src/select.rs`` pins the same laws Rust-side)."""

    def test_signed_zero_sharpe_compares_equal_falls_to_run_id(self) -> None:
        # -0.0 == 0.0 (IEEE), so the run_id tie-break decides — and ResultRow
        # canonicalises -0.0 to +0.0 anyway (pinned by the extreme-value
        # golden suite below); the law holds either way.
        rows = [
            _row(run_id=_rid(9), sharpe_ratio=-0.0),
            _row(run_id=_rid(1), sharpe_ratio=0.0),
        ]
        boards = group_survivors(rows, top_k=1)
        (board,) = boards.values()
        assert [r.run_id for r in board] == [_rid(1)]  # run_id asc wins

    def test_equal_sharpe_largest_run_id_evicted_first(self) -> None:
        rows = [
            _row(run_id=_rid(5), sharpe_ratio=1.0),
            _row(run_id=_rid(2), sharpe_ratio=1.0),
            _row(run_id=_rid(1), sharpe_ratio=1.0),
        ]
        boards = group_survivors(rows, top_k=2)
        (board,) = boards.values()
        assert [r.run_id for r in board] == [_rid(1), _rid(2)]  # rid(5) evicted

    def test_full_tie_earlier_insertion_kept(self) -> None:
        rows = [
            _row(run_id=_rid(1), sharpe_ratio=1.0, net_pnl_paisa=100),
            _row(run_id=_rid(1), sharpe_ratio=1.0, net_pnl_paisa=101),
        ]
        boards = group_survivors(rows, top_k=1)
        (board,) = boards.values()
        assert [r.net_pnl_paisa for r in board] == [100]  # the earlier twin

    def test_disabled_rust_runtime_refuses_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # DEC-RUST-STAGE3-REFINE-001: the Python selection math is DELETED —
        # a falsy flag is a loud typed refusal, never a silent fallback.
        monkeypatch.setenv(REFINE_RUST_ENV, "0")
        with pytest.raises(RefineRustUnavailableError, match="NO fallback"):
            group_survivors([_row()], top_k=1)


class TestGoldenEqualityVsReference:
    """New bounded-heap output is byte-identical to the pre-#1294 reference."""

    K = 3

    def _check(self, rows: list[ResultRow], *, top_k: int) -> None:
        # LANE W1 (DEC-RANK-POOL-UNION-ADMIT-001): this suite pins the LEGACY
        # single-key arm (`multi_axis=False`) byte-identical to the
        # pre-#1294 reference — the "axes disabled ⇒ byte-identical" law.
        # The union DEFAULT's own laws live in TestMultiAxisUnion below.
        _assert_byte_identical(
            group_survivors(rows, top_k=top_k, multi_axis=False),
            _group_survivors_reference(rows, top_k=top_k),
        )

    def test_equal_sharpe_tie_cluster_larger_than_k(self) -> None:
        # 7 rows, ONE Sharpe value, K=3: only the run_id tie-break decides.
        # Shuffled insertion order so heap eviction order is exercised.
        rows = [_row(run_id=_rid(i), sharpe_ratio=1.5) for i in (5, 2, 7, 1, 6, 4, 3)]
        self._check(rows, top_k=self.K)
        boards = group_survivors(rows, top_k=self.K)
        (board,) = boards.values()
        assert [r.run_id for r in board] == [_rid(1), _rid(2), _rid(3)]

    def test_duplicate_run_ids_full_tie_insertion_order_decides(self) -> None:
        # Same (sharpe, run_id) repeated >K with DISTINGUISHABLE payloads:
        # the old stable sort kept the EARLIEST insertions — pin that exactly.
        rows = [_row(run_id=_rid(1), sharpe_ratio=1.0, net_pnl_paisa=100 + i) for i in range(5)]
        self._check(rows, top_k=self.K)
        boards = group_survivors(rows, top_k=self.K)
        (board,) = boards.values()
        assert [r.net_pnl_paisa for r in board] == [100, 101, 102]

    def test_duplicate_run_ids_differing_sharpe(self) -> None:
        rows = [
            _row(run_id=_rid(1), sharpe_ratio=2.0, net_pnl_paisa=1),
            _row(run_id=_rid(1), sharpe_ratio=1.0, net_pnl_paisa=2),
            _row(run_id=_rid(2), sharpe_ratio=2.0, net_pnl_paisa=3),
            _row(run_id=_rid(1), sharpe_ratio=3.0, net_pnl_paisa=4),
        ]
        self._check(rows, top_k=self.K)
        self._check(rows, top_k=1)

    @pytest.mark.parametrize("n", [K - 1, K, K + 1])
    def test_boundary_counts_fewer_exact_and_k_plus_one(self, n: int) -> None:
        rows = [_row(run_id=_rid(i), sharpe_ratio=float(i)) for i in range(1, n + 1)]
        self._check(rows, top_k=self.K)

    def test_multiple_leaderboards_interleaved(self) -> None:
        rows: list[ResultRow] = []
        for i in range(12):
            rows.append(
                _row(
                    run_id=_rid(100 + i),
                    instrument=("NIFTY", "BANKNIFTY")[i % 2],
                    tf=("5m", "15m")[(i // 2) % 2],
                    mode=("A", "B")[(i // 4) % 2],
                    sharpe_ratio=float(i % 3),  # forced cross-board ties
                )
            )
        self._check(rows, top_k=2)
        self._check(rows, top_k=1)

    def test_extreme_finite_sharpe_values(self) -> None:
        # NaN/inf are rejected by ResultRow (see the domain pin below); the
        # extreme FINITE domain — huge magnitudes, subnormals, signed zero —
        # must order identically. -0.0 is canonicalised to +0.0 by ResultRow.
        extremes = [1e308, -1e308, 5e-324, -5e-324, 0.0, -0.0, 1e308, 0.0]
        rows = [_row(run_id=_rid(i), sharpe_ratio=s) for i, s in enumerate(extremes, start=1)]
        self._check(rows, top_k=self.K)
        self._check(rows, top_k=len(rows))

    def test_nan_sharpe_is_out_of_domain(self) -> None:
        # WHY the heap comparator need not handle NaN: ResultRow rejects
        # non-finite metrics at construction, so NEITHER implementation can
        # ever receive one (the old two-pass sort's NaN behaviour is moot).
        with pytest.raises(ResultsStoreError, match="finite"):
            _row(sharpe_ratio=float("nan"))

    def test_seeded_random_permutations_match_reference(self) -> None:
        # Adversarial soak: 3 leaderboards, heavy Sharpe ties (5 distinct
        # values over 90 rows), duplicate run_ids — every permutation must
        # match the reference BYTE-for-byte on the SAME permuted input.
        rng = random.Random(1234)
        rows = [
            _row(
                run_id=_rid(rng.randrange(1, 40)),  # forced duplicates
                instrument=rng.choice(("NIFTY", "BANKNIFTY", "SENSEX")),
                sharpe_ratio=float(rng.randrange(5)),
                net_pnl_paisa=i,  # distinguishes full-tie duplicates
            )
            for i in range(90)
        ]
        for _ in range(5):
            rng.shuffle(rows)
            self._check(rows, top_k=4)

    def test_lazy_iterable_consumed_in_one_pass(self) -> None:
        # The streaming contract: a one-shot generator is sufficient input —
        # on BOTH arms (the union arm also streams rows exactly once).
        rows = [_row(run_id=_rid(i), sharpe_ratio=float(i)) for i in range(1, 6)]
        boards = group_survivors(iter(rows), top_k=2, multi_axis=False)
        _assert_byte_identical(boards, _group_survivors_reference(rows, top_k=2))
        union_from_iter = group_survivors(iter(rows), top_k=2)
        _assert_byte_identical(union_from_iter, group_survivors(rows, top_k=2))


class TestMultiAxisUnion:
    """The LANE-W1 union-admission arm (DEC-RANK-POOL-UNION-ADMIT-001)."""

    def test_dd_optimal_row_outside_sharpe_topk_is_admitted(self) -> None:
        # THE rank-1-forever regression (winning-completeness item 3): a
        # DD-optimal / high-win-rate row whose Sharpe sits BELOW the top-K
        # cut previously never reached the Stage-3 pool (annotation-only
        # not_validated forever). Under the union it is admitted via the
        # DD/preset/win-rate sources — at the SAME total cap.
        rows = [
            _row(
                run_id=_rid(i),
                sharpe_ratio=float(10 + i),
                max_drawdown_pct=20.0,
                win_rate=0.5,
            )
            for i in range(1, 6)  # five high-Sharpe, high-DD rows
        ]
        dd_optimal = _row(
            run_id=_rid(99),
            sharpe_ratio=0.8,  # dead last on Sharpe
            max_drawdown_pct=3.0,  # best DD by far
            win_rate=0.9,  # best win rate
        )
        rows.append(dd_optimal)
        legacy = group_survivors(rows, top_k=3, multi_axis=False)
        (legacy_board,) = legacy.values()
        assert dd_optimal not in legacy_board  # the pre-W1 shape: never admitted
        union = group_survivors(rows, top_k=3)
        (union_board,) = union.values()
        assert dd_optimal in union_board  # admitted via the DD/preset sources
        assert len(union_board) == 3  # the cap NEVER grows

    def test_frontier_source_admits_persisted_pareto_member(self) -> None:
        # Item 1's "Stage-3 heap needs explicit frontier inclusion": a
        # persisted ε-Pareto member that loses on EVERY quota axis is still
        # admitted via the frontier source (pareto_member is the persisted
        # column — the sweep's union stamped it).
        rows = [
            _row(
                run_id=_rid(i),
                sharpe_ratio=float(10 + i),
                max_drawdown_pct=1.0 + i / 10.0,
                win_rate=0.9,
            )
            for i in range(1, 7)
        ]
        frontier_row = _row(
            run_id=_rid(50),
            sharpe_ratio=0.1,  # worst on every axis...
            max_drawdown_pct=25.0,
            win_rate=0.1,
            pareto_member=True,  # ...but a persisted frontier member
        )
        rows.append(frontier_row)
        union = group_survivors(rows, top_k=5)
        (board,) = union.values()
        assert frontier_row in board
        # An untagged store (pareto_member None — pre-W1 rows) simply has no
        # frontier source; nothing crashes, the other sources decide.
        legacy_rows = [r for r in rows if r.pareto_member is None]
        boards = group_survivors(legacy_rows, top_k=5)
        assert sum(len(b) for b in boards.values()) == 5

    def test_bounded_union_matches_pure_oracle(self) -> None:
        # The bounded 5-heap streaming arm == the pure sort-based
        # pool_union.union_select_rows oracle, per board (the same shared
        # round-robin law over the same total orders — membership AND order).
        from brutex.engine.pool_union import union_select_rows

        rng = random.Random(20260718)
        rows = [
            _row(
                run_id=_rid(rng.randrange(1, 60)),
                instrument=rng.choice(("NIFTY", "BANKNIFTY")),
                sharpe_ratio=float(rng.randrange(7)),
                max_drawdown_pct=float(rng.randrange(1, 30)),
                win_rate=rng.randrange(1, 100) / 100.0,
                net_pnl_paisa=i,
                pareto_member=rng.choice((None, True, False)),
            )
            for i in range(80)
        ]
        boards = group_survivors(rows, top_k=6)
        for key, board in boards.items():
            pool = [
                r
                for r in rows
                if (r.instrument, r.tf, r.mode) == (key.instrument, key.tf, key.mode)
            ]
            assert board == union_select_rows(pool, cap=6)

    def test_small_board_union_is_byte_identical_to_legacy(self) -> None:
        # A board with <= top_k rows admits EVERYTHING under either arm —
        # the union default is byte-identical there (the common production
        # shape until pools exceed the cap).
        rows = [_row(run_id=_rid(i), sharpe_ratio=float(i)) for i in range(1, 5)]
        _assert_byte_identical(
            group_survivors(rows, top_k=10),
            group_survivors(rows, top_k=10, multi_axis=False),
        )
