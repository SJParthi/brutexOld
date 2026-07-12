# pyright: reportPrivateUsage=none
"""End-to-end cross-verify producer chain (DEC-CROSSVERIFY-PRODUCER-001 §9.3).

A REAL lake written through the production ``ParquetBarSink`` (the golden
harness's build law: 3 pinned indices + an ``M&M`` stock enumerated from a
REAL ``instruments.duckdb`` + one NIFTY future enumerated from a REAL
``registry.duckdb``) driven through the FULL :func:`run_crossverify` step
machine over :func:`default_seams` — real locks (tmp-lake flocks + lease),
real reader/gate-seam/export/staging/journal/prebuild legs — with exactly
TWO injected seams: :class:`FakeS3Client` (no AWS in CI) and a recording
alerter (the exactly-once summary pin). The pull leg is exercised in
``--skip-pull`` mode (the vendor is out of CI's reach by design; the pull
leg's call shapes are spy-pinned in ``tests/unit/test_crossverify_pull_leg.py``).

GATE HONESTY: the sibling completeness-gate module
(``brutex.data.today_completeness``, MIGRATION.md #1755) has not merged into
this tree — ``default_seams``' fail-closed binding would honestly REFUSE — so
the classifier seam is swapped to the contract-faithful fake twin
(``tests.fakes.fake_completeness_gate.faithful_classify``) EXCEPT when the
real module imports, in which case the REAL gate drives (the
real-gate-if-importable arm keeps this test meaningful post-merge with zero
edits). Asserted: published object bytes vs the committed golden fixtures,
journal contents + staging cleanup, idempotent second run (0 new puts),
truncated-index withhold → exit 2 with the file ABSENT from S3, crash-mid-
publish → next-run convergence, and the derive leg via the independent
``lake_derived_freshness_verdicts`` oracle.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from brutex.crossverify.config import resolve_crossverify_config
from brutex.crossverify.gate_seam import (
    CompletenessGateUnavailable,
    bind_production_classifier,
)
from brutex.crossverify.journal import load_journal, staging_dir
from brutex.crossverify.run import CrossverifyReport, default_seams, run_crossverify
from brutex.data.instrument_store import InstrumentStore
from brutex.data.registry_store import RegistryStore
from brutex.data.tf_autoderive import lake_derived_freshness_verdicts
from brutex.models import Exchange, Instrument, InstrumentType, Segment

from tests.fakes.fake_completeness_gate import faithful_classify
from tests.fakes.fake_s3_client import FakeS3Client
from tests.golden._crossverify_harness import (
    FIXTURES_DIR,
    GOLDEN_DAY,
    GOLDEN_IDENTITIES,
    _bars,
)

if TYPE_CHECKING:
    from brutex.crossverify.run import CrossverifySeams

_SESSION_MINUTES = 375

#: The keys the clean-lake run publishes (the s3_key law, prefix default).
_KEY_NIFTY = f"crossverify/groww/{GOLDEN_DAY}/IDX_I/NIFTY.csv"
_KEY_BANKNIFTY = f"crossverify/groww/{GOLDEN_DAY}/IDX_I/BANKNIFTY.csv"
_KEY_SENSEX = f"crossverify/groww/{GOLDEN_DAY}/IDX_I/SENSEX.csv"
_KEY_MM = f"crossverify/groww/{GOLDEN_DAY}/NSE_EQ/M&M.csv"
_KEY_FUT = f"crossverify/groww/{GOLDEN_DAY}/NSE_FNO/NIFTY-2026-07-30-FUT.csv"
_ALL_KEYS = {_KEY_NIFTY, _KEY_BANKNIFTY, _KEY_SENSEX, _KEY_MM, _KEY_FUT}


class _RecordingAlerter:
    """The exactly-once-summary pin's recorder (satisfies CrossverifyAlerter)."""

    def __init__(self) -> None:
        self.summaries: list[tuple[str, CrossverifyReport]] = []
        self.refusals: list[tuple[str, str]] = []

    def crossverify_summary(self, *, run_id: str, report: CrossverifyReport) -> bool:
        self.summaries.append((run_id, report))
        return True

    def crossverify_refused(self, *, run_id: str, reason: str) -> bool:
        self.refusals.append((run_id, reason))
        return True


def _eq(symbol: str, security_id: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _build_lake(lake_root: Path, *, truncated_banknifty: bool) -> None:
    """The golden-harness build law over THIS test's enumeration surface."""
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake_root))
    full = range(_SESSION_MINUTES)
    plans: list[tuple[str, list[Any]]] = [
        ("NIFTY", _bars(base_price="24000.00", minutes=full, oi_base=None)),
        ("SENSEX", _bars(base_price="80000.00", minutes=full, oi_base=None)),
        (
            "BANKNIFTY",
            _bars(
                base_price="52000.00",
                minutes=range(3) if truncated_banknifty else full,
                oi_base=None,
            ),
        ),
        ("M&M", _bars(base_price="2900.00", minutes=range(3), oi_base=None)),
        ("NIFTY-2026-07-30-FUT", _bars(base_price="24100.00", minutes=full, oi_base=50_000)),
    ]
    for symbol, bars in plans:
        identity = GOLDEN_IDENTITIES[symbol]
        sink.append(
            exchange=identity.exchange,
            segment=identity.lake_segment,
            symbol=identity.lake_symbol,
            interval="1minute",
            bars=bars,
        )
    # The stock enumeration surface — a REAL instruments.duckdb EQ row.
    store = InstrumentStore.open(lake_root / "instruments.duckdb")
    try:
        store.upsert_many(
            [_eq("M&M", 2031)],
            snapshot_date=GOLDEN_DAY,
            run_id="cv-e2e",
            csv_source_sha256="0" * 64,
        )
    finally:
        store.close()
    # The futures enumeration surface (--skip-pull falls back to the registry).
    with RegistryStore.open(lake_root / "registry.duckdb") as registry:
        registry.upsert_contracts(
            "NSE",
            "NIFTY",
            date(2026, 7, 30),
            [("NSE-NIFTY-30Jul26-FUT", "FUT", None)],
            run_id="cv-e2e",
            fetched_at=datetime(2026, 7, 1, tzinfo=UTC),
        )


def _classifier_factory() -> Any:
    """The real gate when importable, else the contract-faithful fake twin."""
    try:
        return bind_production_classifier()
    except CompletenessGateUnavailable:
        return faithful_classify


def _seams(lake_root: Path, fake_s3: FakeS3Client, alerter: _RecordingAlerter) -> CrossverifySeams:
    config = resolve_crossverify_config(lake_root=lake_root, day=GOLDEN_DAY, skip_pull=True)
    seams = default_seams(
        config,
        account_key_resolver=lambda: None,  # no AWS credentials in CI
        s3_client_factory=lambda: fake_s3,
        alerter=alerter,
    )
    return dataclasses.replace(seams, classifier_factory=_classifier_factory)


def _run(lake_root: Path, fake_s3: FakeS3Client, alerter: _RecordingAlerter) -> CrossverifyReport:
    config = resolve_crossverify_config(lake_root=lake_root, day=GOLDEN_DAY, skip_pull=True)
    return run_crossverify(config, _seams(lake_root, fake_s3, alerter))


@pytest.fixture
def clean_lake(tmp_path: Path) -> Path:
    lake = tmp_path / "lake"
    lake.mkdir()
    _build_lake(lake, truncated_banknifty=False)
    return lake


class TestCleanChain:
    def test_full_chain_publishes_pinned_bytes_then_idempotent_rerun(
        self, clean_lake: Path
    ) -> None:
        fake_s3 = FakeS3Client()
        alerter = _RecordingAlerter()
        report = _run(clean_lake, fake_s3, alerter)

        assert report.verdict == "published"
        assert report.exit_code == 0
        assert report.published == 5
        assert report.blocked == 0
        assert report.withheld == 0
        assert set(fake_s3.objects) == _ALL_KEYS

        # Byte-verified against the COMMITTED golden fixtures (same bar law).
        for key, fixture in (
            (_KEY_NIFTY, "NIFTY.csv"),
            (_KEY_SENSEX, "SENSEX.csv"),
            (_KEY_MM, "M&M.csv"),
            (_KEY_FUT, "NIFTY-2026-07-30-FUT.csv"),
        ):
            assert fake_s3.objects[key] == (FIXTURES_DIR / fixture).read_bytes(), key

        # Journal = the durable done-record; staging dir deleted on full publish.
        journal = load_journal(clean_lake, GOLDEN_DAY)
        assert journal is not None
        assert set(journal.entries) == _ALL_KEYS
        assert journal.pending_keys() == ()
        assert journal.withheld == {}
        assert not staging_dir(clean_lake, GOLDEN_DAY).exists()

        # Exactly ONE summary, zero refusals.
        assert len(alerter.summaries) == 1
        assert alerter.refusals == []
        assert alerter.summaries[0][1].verdict == "published"

        # Idempotent second run: journal-verified skips ⇒ ZERO new puts.
        puts_before = len(fake_s3.puts)
        alerter2 = _RecordingAlerter()
        report2 = _run(clean_lake, fake_s3, alerter2)
        assert report2.verdict == "published"
        assert report2.exit_code == 0
        assert report2.published == 0
        assert report2.skipped_already == 5
        assert len(fake_s3.puts) == puts_before
        assert len(alerter2.summaries) == 1

    def test_derive_leg_verified_by_the_independent_freshness_oracle(
        self, clean_lake: Path
    ) -> None:
        fake_s3 = FakeS3Client()
        report = _run(clean_lake, fake_s3, _RecordingAlerter())
        assert report.derive_failed == 0
        assert report.derived > 0
        # The independent oracle: every stored derived TF equals a fresh
        # re-derivation from the 1-minute base (DEC-CROSSVERIFY-DAY-DERIVE-001).
        verdicts = lake_derived_freshness_verdicts(
            clean_lake,
            exchange="NSE",
            segment="INDEX",
            symbols=["NIFTY", "BANKNIFTY"],
            intervals=["5minute", "15minute"],
        )
        assert verdicts, "expected stored derived partitions to judge"
        assert set(verdicts.values()) == {"fresh"}


class TestWithheldIndex:
    def test_truncated_index_day_withholds_exits_2_and_never_publishes_it(
        self, tmp_path: Path
    ) -> None:
        lake = tmp_path / "lake"
        lake.mkdir()
        _build_lake(lake, truncated_banknifty=True)
        fake_s3 = FakeS3Client()
        alerter = _RecordingAlerter()
        report = _run(lake, fake_s3, alerter)

        assert report.verdict == "withheld_partial"
        assert report.exit_code == 2
        assert report.withheld == 1
        # Never-publish-partial: the truncated index reached NEITHER staging
        # NOR S3; the healthy files still published (per-file law).
        assert _KEY_BANKNIFTY not in fake_s3.objects
        assert _KEY_NIFTY in fake_s3.objects
        journal = load_journal(lake, GOLDEN_DAY)
        assert journal is not None
        assert "BANKNIFTY" in journal.withheld
        assert _KEY_BANKNIFTY not in journal.entries
        # The summary fired once, CRITICAL-bound verdict carried verbatim.
        assert len(alerter.summaries) == 1
        assert alerter.summaries[0][1].verdict == "withheld_partial"


class TestPublishCrashConvergence:
    def test_exhausted_retries_leave_pending_and_the_next_run_converges(
        self, clean_lake: Path
    ) -> None:
        # First run: one key throttles past the whole 3-attempt ladder.
        failing = FakeS3Client(fail_times={_KEY_MM: 99})
        alerter = _RecordingAlerter()
        report = _run(clean_lake, failing, alerter)
        assert report.verdict == "publish_failed"
        assert report.exit_code == 2
        assert report.publish_failed == 1
        journal = load_journal(clean_lake, GOLDEN_DAY)
        assert journal is not None
        assert journal.pending_keys() == (_KEY_MM,)
        # The staged bytes survive for the convergence run.
        assert staging_dir(clean_lake, GOLDEN_DAY).exists()

        # Next run against a healthy backend: the pending key publishes from
        # frozen staged bytes; the already-published keys skip (0 re-puts).
        healthy = FakeS3Client()
        report2 = _run(clean_lake, healthy, _RecordingAlerter())
        assert report2.verdict == "published"
        assert report2.exit_code == 0
        assert report2.skipped_already == 4
        assert report2.published == 1
        assert set(healthy.objects) == {_KEY_MM}
        assert healthy.objects[_KEY_MM] == (FIXTURES_DIR / "M&M.csv").read_bytes()
        journal2 = load_journal(clean_lake, GOLDEN_DAY)
        assert journal2 is not None
        assert journal2.pending_keys() == ()
        assert not staging_dir(clean_lake, GOLDEN_DAY).exists()
