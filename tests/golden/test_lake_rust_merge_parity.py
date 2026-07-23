"""Rust-vs-Python merge/dedup DECISION parity corpus (ADR-0020 PR-3 — the core gate).

DEC-LAKE-RUST-MERGE-DEDUP-001 (MIGRATION.md #1911 — allocator-final). Drives
the handoff's six-case corpus through the REAL :meth:`ParquetWriter.write`
against the COMPILED ``brutex_lake`` crate's ``merge_decision_from_ipc`` and
asserts every ON-vs-OFF pair is identical: partition BYTES (sha256 + raw),
every ``WriteResult`` field (``bars_added``/``file_rewritten`` laws — 1g of
the handoff), and the conflicts tuple. The Rust decisions are obtained
through the PRODUCTION seam in steady-state mode (burn-in budget 0 — the
materialized fold is the crate decision's, no oracle compare cushions the
equality), so nothing here replicates the frame construction: any drift in
``_decision_frame``'s schema or the action vocabulary is automatically part
of the tested surface. Every engaged merge additionally runs
:func:`_assert_crate_decided` (the PR-1 ``_assert_crate_rendered`` witness):
the seam's warn-only degrade paths (``merge_error`` / ``decision_refused``)
serve the PYTHON fold WITHOUT latching, so without that witness a crate
raising on every call would green the gate comparing python-to-python.

``file_rewritten`` equality across every case IS the #1614 stats-manifest
keying guarantee (the handoff's case (d)): the stats hook keys on
``WriteResult.file_rewritten`` — NEVER ``bars_added`` — so proving it equal
ON-vs-OFF on the no-op / restate / heal shapes proves the stat-token
residual did not widen.

REQUIRES the compiled crate + version lockstep. Auto-SKIPS loudly when
either is missing — the ``.github/workflows/lake-rust-parity.yml`` lane (and
a local ``pip install rust/brutex-lake``) is where this suite has teeth; the
main CI test job deliberately never installs the crate.

Whitebox: drives private seam internals (DEC-P0-017), so
``reportPrivateUsage`` is silenced.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

brutex_lake = pytest.importorskip(
    "brutex_lake",
    reason=(
        "brutex-lake crate not installed — the merge-decision parity corpus runs in "
        "the lake-rust-parity.yml lane (or locally after `pip install rust/brutex-lake`)"
    ),
)
pl = pytest.importorskip("polars")

from brutex.data import lake_rust_merge as lrm  # noqa: E402
from brutex.data.models import CandleBar, FnoGreeksBar  # noqa: E402
from brutex.data.parquet_writer import ParquetWriter, WriteResult  # noqa: E402

if not callable(getattr(brutex_lake, "merge_decision_from_ipc", None)):
    pytest.skip(
        "installed brutex_lake wheel predates the PR-3 merge kernel — rebuild "
        "(`pip install rust/brutex-lake`)",
        allow_module_level=True,
    )

if brutex_lake.pinned_polars_version() != pl.__version__:
    pytest.skip(
        "version-guard mismatch: installed polars "
        f"{pl.__version__} != crate pin {brutex_lake.pinned_polars_version()} — "
        "the facade would refuse to engage (fail-closed by design); re-run after "
        "aligning the lockstep (docs/runbooks/lake-rust-render-lockstep.md)",
        allow_module_level=True,
    )

COORDS = {"exchange": "NSE", "segment": "CASH", "symbol": "NSE-NIFTY", "interval": "1minute"}


@pytest.fixture(autouse=True)
def _steady_state() -> Iterator[None]:
    """Post-burn-in steady state: the materialized fold IS the crate decision's."""
    lrm.reset_lake_rust_merge_for_tests(remaining=0)
    yield
    lrm.reset_lake_rust_merge_for_tests()


def _bar(minute: int, *, volume: int = 1_000, oi: int | None = None) -> CandleBar:
    return CandleBar(
        timestamp=datetime(2024, 1, 1, 4, minute, tzinfo=UTC),
        open=Decimal("100.25"),
        high=Decimal("101.00"),
        low=Decimal("99.75"),
        close=Decimal("100.50"),
        volume=volume,
        open_interest=oi,
    )


def _greeks_bar(minute: int, *, iv: float | None = 0.21) -> FnoGreeksBar:
    return FnoGreeksBar(
        timestamp=datetime(2024, 1, 1, 4, minute, tzinfo=UTC),
        open=Decimal("100.25"),
        high=Decimal("101.00"),
        low=Decimal("99.75"),
        close=Decimal("100.50"),
        volume=1_000,
        open_interest=25,
        iv=iv,
        delta=0.5,
        spot_at_bar=Decimal("100.00"),
        greeks_provenance_id=7,
    )


def _assert_crate_decided() -> None:
    """The engagement witness: green must MEAN the crate decided.

    The warn-only degrade paths serve the Python fold without latching
    ``rust_merge_disabled`` — a crate raising on every merge would otherwise
    green this corpus comparing python-to-python.
    """
    assert not lrm.rust_merge_disabled(), f"latched: {lrm._DISABLED_REASON[0]!r}"
    assert "__engaged__" in lrm._OBSERVABILITY_MEMO, "the crate never engaged"
    degrades = {"merge_error", "decision_refused"} & lrm._OBSERVABILITY_MEMO
    assert not degrades, f"warn-only degrades fired: {degrades}"


def _result_fields(result: WriteResult) -> tuple[object, ...]:
    return (
        result.row_count,
        result.raw_hash,
        result.first_ts,
        result.last_ts,
        result.bytes_written,
        result.bars_added,
        tuple((c.timestamp, c.stored, c.vendor) for c in result.conflicts),
        result.vendor_dup_rows_dropped,
        result.file_rewritten,
    )


def _twin_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    steps: list[tuple[list[CandleBar], str]],
    *,
    coords: dict[str, str] | None = None,
    poison_after_first: bool = False,
) -> tuple[list[WriteResult], list[WriteResult]]:
    """Run the SAME write sequence flag-OFF and flag-ON (real crate); assert
    per-step WriteResult field equality + final partition byte equality and
    the crate-engagement witness; return both result lists."""
    coords = dict(COORDS if coords is None else coords)

    def _drive(lake_root: Path) -> list[WriteResult]:
        writer = ParquetWriter(lake_root=lake_root)
        results: list[WriteResult] = []
        for index, (bars, merge_mode) in enumerate(steps):
            results.append(writer.write(bars=list(bars), merge_mode=merge_mode, **coords))
            if poison_after_first and index == 0:
                # Hand-poison the stored file with a duplicated-timestamp row
                # (the pre-DEC-LAKE-WRITE-UNIQUE-001 shape) so the NEXT merge
                # takes the dedup-heal force-rewrite path.
                df = pl.read_parquet(results[0].path)
                pl.concat([df, df.slice(0, 1)]).sort("timestamp").write_parquet(results[0].path)
        return results

    # Explicit "0" (kill switch), NOT delenv-to-unset — the wave default is ON,
    # so an unset OFF arm would be a vacuous Rust-vs-Rust byte compare.
    monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "0")
    off = _drive(tmp_path / "off")
    monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")
    lrm.reset_lake_rust_merge_for_tests(remaining=0)
    on = _drive(tmp_path / "on")
    if len(steps) > 1:  # a merge actually engaged the seam
        _assert_crate_decided()
    for step, (off_result, on_result) in enumerate(zip(off, on, strict=True)):
        assert _result_fields(off_result) == _result_fields(on_result), f"step {step}"
    off_bytes = off[-1].path.read_bytes()
    on_bytes = on[-1].path.read_bytes()
    assert hashlib.sha256(off_bytes).hexdigest() == hashlib.sha256(on_bytes).hexdigest()
    assert off_bytes == on_bytes
    return off, on


class TestMergeDecisionParity:
    def test_case_a_fresh_render_control_never_engages_the_seam(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(a) A FRESH partition write takes the fresh branch — the merge seam
        must not engage even with the flag ON (spy)."""
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")

        def _must_not_route(*_a: object, **_k: object) -> object:
            raise AssertionError("the fresh branch must never call merge_fold")

        monkeypatch.setattr(lrm, "merge_fold", _must_not_route)
        writer = ParquetWriter(lake_root=tmp_path / "lake")
        result = writer.write(bars=[_bar(0), _bar(1)], **COORDS)
        assert result.bars_added == 2 and result.file_rewritten is True

    def test_case_b_merge_into_existing_keep_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(b) new + identical-dup + keep-first conflict — bytes, counters and
        the conflicts tuple identical ON vs OFF."""
        off, on = _twin_run(
            tmp_path,
            monkeypatch,
            [
                ([_bar(0)], "keep-first"),
                ([_bar(0), _bar(0, volume=9_999), _bar(1)], "keep-first"),
            ],
        )
        assert on[1].bars_added == 1
        assert len(on[1].conflicts) == 1 and on[1].conflicts[0].vendor.volume == 9_999
        assert off[1].file_rewritten is True is on[1].file_rewritten

    def test_case_b_twin_latest_wins_restate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(b-twin) a latest-wins value restate overwrites with NO conflict —
        the derived-1day law."""
        _off, on = _twin_run(
            tmp_path,
            monkeypatch,
            [
                ([_bar(0)], "keep-first"),
                ([_bar(0, volume=5_555)], "latest-wins"),
            ],
        )
        assert on[1].bars_added == 0 and on[1].conflicts == ()
        assert on[1].file_rewritten is True
        stored = pl.read_parquet(on[1].path)
        assert stored["volume"].to_list() == [5_555]

    def test_case_c_dup_poisoned_destination_force_rewrites_at_zero_added(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(c) the dedup self-heal: bars_added==0 yet file_rewritten True
        (``lake.partition.dedup_healed``) — identical ON vs OFF."""
        _off, on = _twin_run(
            tmp_path,
            monkeypatch,
            [
                ([_bar(0), _bar(1)], "keep-first"),
                ([_bar(0)], "keep-first"),
            ],
            poison_after_first=True,
        )
        assert on[1].bars_added == 0
        assert on[1].file_rewritten is True  # the forced heal rewrite
        assert on[1].row_count == 2

    def test_case_d_file_rewritten_keying_equal_on_vs_off_everywhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(d) the #1614 stats-hook keying law: ``file_rewritten`` — the ONE
        field the stats-manifest hook keys on — is equal ON-vs-OFF across the
        no-op / net-new / restate shapes in one sequence."""
        off, on = _twin_run(
            tmp_path,
            monkeypatch,
            [
                ([_bar(0)], "keep-first"),
                ([_bar(0)], "keep-first"),  # no-op: False
                ([_bar(1)], "keep-first"),  # net-new: True
                ([_bar(1, volume=7)], "latest-wins"),  # restate: True
            ],
        )
        assert [r.file_rewritten for r in off] == [True, False, True, True]
        assert [r.file_rewritten for r in on] == [True, False, True, True]

    def test_case_e_fully_duplicate_merge_is_a_no_op_with_zero_renders(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(e) an EMPTY merge (batch fully duplicate) — no rewrite,
        ``file_rewritten`` False, ZERO render calls on BOTH arms (spy)."""
        renders: list[int] = []
        real_render = ParquetWriter._render_bytes

        def _counting_render(sorted_bars: list[CandleBar]) -> bytes:
            renders.append(len(sorted_bars))
            return real_render(sorted_bars)

        off, on = _twin_run(
            tmp_path,
            monkeypatch,
            [
                ([_bar(0), _bar(1)], "keep-first"),
                ([_bar(0), _bar(1)], "keep-first"),
            ],
        )
        assert off[1].file_rewritten is False is on[1].file_rewritten
        assert off[1].bars_added == 0 == on[1].bars_added
        # Zero-render proof on a fresh ON lake with the spy armed.
        monkeypatch.setattr(ParquetWriter, "_render_bytes", staticmethod(_counting_render))
        writer = ParquetWriter(lake_root=tmp_path / "spy")
        writer.write(bars=[_bar(0), _bar(1)], **COORDS)
        renders.clear()
        result = writer.write(bars=[_bar(0), _bar(1)], **COORDS)
        assert result.file_rewritten is False
        assert renders == []  # the no-op path never rendered

    def test_case_f_overlay_only_difference_both_modes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(f) the ``_overlay_equal`` law: keep-first keeps the stored Greek
        SILENTLY (no conflict, no rewrite); latest-wins adopts the incoming
        overlay — identical ON vs OFF."""
        coords = {**COORDS, "segment": "FNO"}
        _off, on = _twin_run(
            tmp_path,
            monkeypatch,
            [
                ([_greeks_bar(0, iv=0.21)], "keep-first"),
                ([_greeks_bar(0, iv=0.34)], "keep-first"),  # silent keep-first
                ([_greeks_bar(0, iv=0.34)], "latest-wins"),  # overlay adoption
            ],
            coords=coords,
        )
        assert on[1].file_rewritten is False and on[1].conflicts == ()
        assert on[2].file_rewritten is True and on[2].bars_added == 0
        stored = pl.read_parquet(on[2].path)
        assert stored["iv"].to_list() == [0.34]

    def test_burn_in_armed_run_is_clean_against_the_real_crate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh-process (burn-in ARMED) run over the corpus shapes never
        autoreverts — the crate's decisions bit-match the Python fold on
        every compared merge."""
        lrm.reset_lake_rust_merge_for_tests()  # full budget, shape gate armed
        monkeypatch.setenv(lrm.LAKE_RUST_MERGE_ENV, "1")
        writer = ParquetWriter(lake_root=tmp_path / "lake")
        writer.write(bars=[_bar(0)], **COORDS)
        writer.write(bars=[_bar(0), _bar(0, volume=9_999), _bar(1)], **COORDS)
        writer.write(bars=[_bar(1, volume=7)], merge_mode="latest-wins", **COORDS)
        coords = {**COORDS, "segment": "FNO"}
        writer.write(bars=[_greeks_bar(0)], **coords)
        writer.write(bars=[_greeks_bar(0, iv=0.34)], merge_mode="latest-wins", **coords)
        assert not lrm.rust_merge_disabled()
        _assert_crate_decided()
