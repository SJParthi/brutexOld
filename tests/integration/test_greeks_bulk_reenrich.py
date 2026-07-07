# pyright: reportPrivateUsage=none
"""End-to-end bulk Greeks v1→v2 re-enrich round-trips (DEC-GREEKS-BULK-REENRICH-001).

The two MIGRATION.md #1188 FAIL-BEFORE regression pins — an EXPIRED v1-stamped
contract is discovered + restamped v2 with NO pull context, and a
ParquetBarSink-only (CLI-shaped) lake gets STATUS_OK non-null iv end-to-end via
``cmd_greeks_backfill`` — plus the byte-identity idempotence pins, the FUT /
derived-TF / spot untouched pins, crash-resume convergence (kill after batch K
→ re-run → bytes identical to one uninterrupted run, looped), the
corrupt-partition per-target quarantine, mixed 7/17-column trees, dry-run
whole-tree inertness, and the exactly-once Telegram discipline. REUSES the
roundtrip lake kit (``tests.integration.test_greeks_enrich_lake_roundtrip`` —
COMMON, never copied).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("trading_core", reason="greeks enrichment needs the trading_core wheel")
pytest.importorskip("polars", reason="the lake round-trip needs polars")

import polars as pl
from brutex.cli import EXIT_OK, cmd_greeks_backfill
from brutex.data.errors import LakeWriteHalt
from brutex.data.greeks_backfill import (
    checkpoint_path,
    plan_greeks_backfill,
    run_greeks_backfill,
    scan_stale_greeks_targets,
)
from brutex.data.greeks_enrich import (
    GREEKS_FORMULA_VERSION,
    STATUS_OK,
    GreeksEnrichReport,
    enrich_greeks_for_targets,
    pack_greeks_provenance,
    unpack_greeks_provenance,
)
from brutex.notifications.telegram import TelegramEvent

from tests.integration.test_greeks_enrich_lake_roundtrip import (
    _CE,
    _FUT,
    _PE,
    _RATE,
    _option_partition,
    _seed_option,
    _seed_spot,
    _sha,
    _targets,
)


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """A fresh temp lake root (mirrors the roundtrip kit's fixture shape)."""
    root = tmp_path / "lake"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def _sequential_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the driver sequential (the BRUTEX_GREEKS_WORKERS=1 kill-switch)."""
    monkeypatch.setenv("BRUTEX_GREEKS_WORKERS", "1")


def _restamp_v1(partition: Path) -> None:
    """Fabricate the #1188 frozen-expired class: rewrite every stamp to formula v1."""
    frame = pl.read_parquet(partition)
    v1 = []
    for pid in frame["greeks_provenance_id"].to_list():
        stored = unpack_greeks_provenance(int(pid))
        v1.append(
            pack_greeks_provenance(
                rate_source_id=stored.rate_source_id,
                rate_version=stored.rate_version,
                style_flag=stored.style_flag,
                status=stored.status,
                formula_version=1,
                q_bias_id=stored.q_bias_id,
                tte_convention_id=stored.tte_convention_id,
            )
        )
    frame.with_columns(pl.Series("greeks_provenance_id", v1, dtype=pl.Int32)).write_parquet(
        partition
    )


def _scan_plan(lake_root: Path, **kwargs: Any) -> Any:
    kwargs.setdefault("rate_source", _RATE)
    force = bool(kwargs.pop("force", False))
    expired_only = bool(kwargs.pop("expired_only", False))
    scan = scan_stale_greeks_targets(lake_root, force=force, expired_only=expired_only, **kwargs)
    return plan_greeks_backfill(scan, rate_source=_RATE, force=force, expired_only=expired_only)


def _tree_shas(root: Path, *, exclude: tuple[str, ...] = ()) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not any(str(p.relative_to(root)).startswith(e) for e in exclude)
    }


class TestExpiredV1Restamp:
    def test_expired_v1_contract_discovered_and_restamped_v2(self, lake: Path) -> None:
        """#1188 pin (a): an EXPIRED contract frozen at v1 is reached with NO pull context."""
        _seed_spot(lake)
        _seed_option(lake)
        enrich_greeks_for_targets(targets=_targets(_CE), lake_root=lake, rate_source=_RATE)
        _restamp_v1(_option_partition(lake))
        plan = _scan_plan(lake, expired_only=True)  # 25Jan24 expiry — long expired
        assert plan.scan.stale_v1 == 1 and len(plan.targets) == 1
        report = run_greeks_backfill(lake, plan=plan, rate_source=_RATE)
        assert report.enriched == 1 and report.errored == 0
        # The WET report carries the scan census verbatim (fix round 2: four
        # census-zeroing mutants survived every suite — the done-log would
        # have reported an all-zero census on a real stale lake).
        assert report.stale_v1 == 1
        assert report.stale_null == 0 and report.stale_inputs_drift == 0
        assert report.probe_failed == 0
        for pid in pl.read_parquet(_option_partition(lake))["greeks_provenance_id"].to_list():
            stored = unpack_greeks_provenance(int(pid))
            assert stored.formula_version == GREEKS_FORMULA_VERSION == 2
            assert stored.status == STATUS_OK

    def test_cli_pulled_lake_gets_enriched_end_to_end(
        self, lake: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """#1188 pin (b): a sink-only (CLI-shaped) lake enriches via cmd_greeks_backfill."""
        _seed_spot(lake)
        _seed_option(lake)  # raw 7-column partition — never enriched by any pull
        assert pl.read_parquet(_option_partition(lake)).width == 7
        exit_code = cmd_greeks_backfill(
            lake_root=lake,
            dry_run=False,
            force=False,
            expired_only=False,
            batch=None,
            limit=None,
            pause_s=None,
        )
        assert exit_code == EXIT_OK
        frame = pl.read_parquet(_option_partition(lake))
        assert frame["iv"].null_count() == 0
        for pid in frame["greeks_provenance_id"].to_list():
            assert unpack_greeks_provenance(int(pid)).status == STATUS_OK
        assert "re-stamped v2" in capsys.readouterr().out


class TestIdempotence:
    def test_second_full_run_is_a_byte_identical_no_op(self, lake: Path) -> None:
        _seed_spot(lake)
        _seed_option(lake)
        first = _scan_plan(lake)
        run_greeks_backfill(lake, plan=first, rate_source=_RATE)
        sha1 = _sha(_option_partition(lake))
        second = _scan_plan(lake)
        assert second.targets == ()  # the stamps ARE the resume/done record
        report = run_greeks_backfill(lake, plan=second, rate_source=_RATE)
        assert report.planned == 0 and report.batches == 0
        assert _sha(_option_partition(lake)) == sha1

    def test_force_rerun_reuses_and_never_rewrites(self, lake: Path) -> None:
        _seed_spot(lake)
        _seed_option(lake)
        run_greeks_backfill(lake, plan=_scan_plan(lake), rate_source=_RATE)
        sha1 = _sha(_option_partition(lake))
        forced = _scan_plan(lake, force=True)
        assert len(forced.targets) == 1  # the probe is skipped — the driver gate decides
        report = run_greeks_backfill(lake, plan=forced, rate_source=_RATE)
        assert report.already_current == 1 and report.enriched == 0
        assert _sha(_option_partition(lake)) == sha1


class TestZeroEngineImpact:
    def test_fut_derived_tf_and_spot_partitions_are_byte_untouched(self, lake: Path) -> None:
        _seed_spot(lake)
        _seed_option(lake)
        _seed_option(lake, symbol=_FUT, close="21510")
        # A derived-TF partition (never Greeks-bearing — deliberately unscanned).
        derived = lake / "bars" / "NSE" / "FNO" / _CE / "5minute" / "2024" / "01.parquet"
        derived.parent.mkdir(parents=True)
        pl.DataFrame({"close": [1.0]}).write_parquet(derived)
        spot = lake / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "01.parquet"
        before = {p: _sha(p) for p in (_option_partition(lake, _FUT), derived, spot)}
        plan = _scan_plan(lake)
        assert [t.symbol for t in plan.targets] == [_CE]  # FUT/derived never targeted
        run_greeks_backfill(lake, plan=plan, rate_source=_RATE)
        for path, sha in before.items():
            assert _sha(path) == sha, f"{path} was touched"


class TestCrashResume:
    def test_kill_mid_run_then_resume_converges_to_the_uninterrupted_bytes(
        self, tmp_path: Path
    ) -> None:
        """Stress-shaped loop: a kill after EVERY batch boundary converges bit-for-bit."""

        def _seed(root: Path) -> None:
            root.mkdir()
            _seed_spot(root)
            _seed_option(root, symbol=_CE)
            _seed_option(root, symbol=_PE, close="180")

        clean = tmp_path / "clean"
        _seed(clean)
        run_greeks_backfill(
            clean, plan=_scan_plan(clean, force=True), rate_source=_RATE, batch_size=1
        )
        expected = {
            _CE: _sha(_option_partition(clean, _CE)),
            _PE: _sha(_option_partition(clean, _PE)),
        }

        def _make_killing_enrich(kill_on_call: int) -> Any:
            calls = {"n": 0}

            def _killing_enrich(**kwargs: Any) -> GreeksEnrichReport:
                calls["n"] += 1
                if calls["n"] == kill_on_call:
                    raise LakeWriteHalt("disk_full")
                return enrich_greeks_for_targets(**kwargs)

            return _killing_enrich

        for kill_on_call in (1, 2):
            crashed = tmp_path / f"crashed_{kill_on_call}"
            _seed(crashed)
            plan = _scan_plan(crashed, force=True)  # force ⇒ stamp-independent stable plan
            with pytest.raises(LakeWriteHalt):
                run_greeks_backfill(
                    crashed,
                    plan=plan,
                    rate_source=_RATE,
                    batch_size=1,
                    enrich=_make_killing_enrich(kill_on_call),
                )
            # A crash AFTER batch 1's record leaves the checkpoint for resume;
            # a crash BEFORE any record leaves nothing (a fresh start — correct).
            assert checkpoint_path(crashed).exists() == (kill_on_call == 2)
            resumed = run_greeks_backfill(crashed, plan=plan, rate_source=_RATE, batch_size=1)
            assert resumed.resumed_from == kill_on_call - 1
            assert _sha(_option_partition(crashed, _CE)) == expected[_CE]
            assert _sha(_option_partition(crashed, _PE)) == expected[_PE]
            assert not checkpoint_path(crashed).exists()  # deleted on clean completion


class TestFailureIsolation:
    def test_corrupt_partition_is_quarantined_per_target_never_fatal(self, lake: Path) -> None:
        _seed_spot(lake)
        _seed_option(lake, symbol=_CE)
        _seed_option(lake, symbol=_PE, close="180")
        _option_partition(lake, _PE).write_bytes(b"garbage - not a parquet file")
        plan = _scan_plan(lake, force=True)  # force targets BOTH (the probe would skip PE)
        report = run_greeks_backfill(lake, plan=plan, rate_source=_RATE)
        assert report.errored == 1  # PE quarantined by the driver, counted honestly
        assert report.enriched == 1  # CE still enriched — one bad contract never sinks the run
        assert pl.read_parquet(_option_partition(lake, _CE))["iv"].null_count() == 0

    def test_mixed_seven_and_seventeen_column_tree_fully_stamps(self, lake: Path) -> None:
        from datetime import timedelta

        from tests.integration.test_greeks_enrich_lake_roundtrip import _TS, _bar, _sink

        _seed_spot(lake)
        _seed_option(lake)  # January (will be enriched → 17-column)
        enrich_greeks_for_targets(targets=_targets(_CE), lake_root=lake, rate_source=_RATE)
        # A LATER raw month for the SAME contract (a crash-mid-enrich shape):
        feb_ts = _TS + timedelta(days=17)  # 2024-02-01 03:45 UTC
        _sink(lake).append(
            exchange="NSE",
            segment="FNO",
            symbol=_CE,
            interval="1minute",
            bars=[_bar(feb_ts, "150")],
        )
        _sink(lake).append(
            exchange="NSE",
            segment="INDEX",
            symbol="NIFTY",
            interval="1minute",
            bars=[_bar(feb_ts, "21500", oi=None)],
        )
        plan = _scan_plan(lake)
        assert plan.scan.stale_null == 1  # the raw Feb month makes the contract stale
        run_greeks_backfill(lake, plan=plan, rate_source=_RATE)
        feb = lake / "bars" / "NSE" / "FNO" / _CE / "1minute" / "2024" / "02.parquet"
        for partition in (_option_partition(lake), feb):
            for pid in pl.read_parquet(partition)["greeks_provenance_id"].to_list():
                assert pid is not None
                assert unpack_greeks_provenance(int(pid)).formula_version == 2


class TestUnfixableConvergence:
    def test_nonfinite_ohlc_null_stamp_converges_to_stale_unfixable(self, lake: Path) -> None:
        """Fix round 4 (refuter-confirmed): the census CONVERGES on a poisoned row.

        A previously-enriched contract with ONE out-of-band-poisoned row
        (``close=NaN`` + a NULL stamp — the IVX-2 class the driver skips
        per-bar by design; ``lake_heal`` is strictly row-removing and cannot
        value-edit it) used to classify ``stale_null`` on EVERY scan while the
        driver reported it ``already_current`` with zero writes: the plan
        never shrank, the run read a dishonest green (✅/EXIT_OK), forever.
        The probe now mirrors the driver's skip predicate: the contract lands
        in ``stale_unfixable``, is never targeted, and the note says so.
        """
        _seed_spot(lake)
        _seed_option(lake)
        enrich_greeks_for_targets(targets=_targets(_CE), lake_root=lake, rate_source=_RATE)
        partition = _option_partition(lake)
        frame = pl.read_parquet(partition)
        n = frame.height
        poisoned = frame.with_columns(
            pl.when(pl.arange(0, n) == 0)
            .then(pl.lit(float("nan"), dtype=pl.Float64))
            .otherwise(pl.col("close"))
            .alias("close"),
            pl.when(pl.arange(0, n) == 0)
            .then(pl.lit(None, dtype=pl.Int32))
            .otherwise(pl.col("greeks_provenance_id"))
            .alias("greeks_provenance_id"),
        )
        poisoned.write_parquet(partition)
        for _ in range(3):  # every rescan reproduces the SAME stable verdict
            plan = _scan_plan(lake)
            assert plan.targets == ()  # never re-targeted — the forever-loop is dead
            assert plan.scan.stale_unfixable == 1
            assert plan.scan.stale_null == 0 and plan.scan.current == 0
            report = run_greeks_backfill(lake, plan=plan, rate_source=_RATE)
            assert report.stale_unfixable == 1 and report.errored == 0
            assert "1 contract(s) UNFIXABLE" in report.note
        # The healthy rows keep their v2 OK stamps — nothing was rewritten.
        stamps = pl.read_parquet(partition)["greeks_provenance_id"].to_list()
        assert stamps[0] is None  # the poisoned row honestly stays unstamped
        for pid in stamps[1:]:
            assert unpack_greeks_provenance(int(pid)).status == STATUS_OK


class TestDryRunInertness:
    def test_dry_run_writes_nothing_anywhere(
        self,
        lake: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_spot(lake)
        _seed_option(lake)  # a genuinely STALE (unstamped) lake
        published: list[TelegramEvent] = []

        class _CapturingAlerter:
            def __init__(self, **kwargs: Any) -> None: ...

            def greeks_backfill_complete(self, report: Any) -> bool:
                published.append(report)
                return True

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _CapturingAlerter)

        def _never(**kwargs: Any) -> GreeksEnrichReport:
            raise AssertionError("dry-run must NEVER reach the enrich driver")

        monkeypatch.setattr("brutex.data.greeks_backfill.enrich_greeks_for_targets", _never)
        before = _tree_shas(lake, exclude=(".manifest",))
        exit_code = cmd_greeks_backfill(
            lake_root=lake,
            dry_run=True,
            force=False,
            expired_only=False,
            batch=None,
            limit=None,
            pause_s=None,
        )
        assert exit_code == EXIT_OK
        # Every byte of lake DATA unchanged. The ONE documented read-path side
        # effect is the shared `.manifest/` dataset-cache self-heal — a derived
        # cache ANY dashboard read (e.g. opening /db) also rebuilds; it is
        # excluded here and pinned as the only difference below.
        assert _tree_shas(lake, exclude=(".manifest",)) == before
        assert not (lake / ".greeks_backfill").exists()  # no checkpoint dir conjured
        assert published == []  # zero Telegram
        assert "DRY RUN — would re-enrich 1 contract(s)" in capsys.readouterr().out


class TestTelegramExactlyOnce:
    def _capture(self, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        published: list[Any] = []

        class _CapturingAlerter:
            def __init__(self, **kwargs: Any) -> None: ...

            def greeks_backfill_complete(self, report: Any) -> bool:
                published.append(report)
                return True

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _CapturingAlerter)
        return published

    def test_wet_cli_run_fires_exactly_one_summary(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_spot(lake)
        _seed_option(lake)
        published = self._capture(monkeypatch)
        cmd_greeks_backfill(
            lake_root=lake,
            dry_run=False,
            force=False,
            expired_only=False,
            batch=1,
            limit=None,
            pause_s=None,
        )
        assert len(published) == 1 and published[0].enriched == 1

    def test_zero_target_run_still_gets_its_one_honest_answer(
        self, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        published = self._capture(monkeypatch)
        cmd_greeks_backfill(
            lake_root=lake,  # an empty lake — nothing stale
            dry_run=False,
            force=False,
            expired_only=False,
            batch=None,
            limit=None,
            pause_s=None,
        )
        assert len(published) == 1 and published[0].planned == 0


class TestExistingGoldensUntouched:
    def test_formula_version_is_still_two_no_bump(self) -> None:
        """The backfill NEVER bumps the formula — the frozen enrich goldens stand."""
        assert GREEKS_FORMULA_VERSION == 2
