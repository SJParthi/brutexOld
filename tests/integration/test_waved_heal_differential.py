"""Wave-D W-2 heal differential — flag-ON ≡ flag-OFF over the poison corpus.

The real-wheel parity gate for the ``BRUTEX_LAKE_WAVED_RUST`` HEAL seam
(Wave-D design §E): heal has NO in-process burn-in (it MUTATES — the §B2
recorded asymmetry), so THIS suite is its verification. For every
heal-relevant poison-corpus case — plus the observe-mode and mass-rail
policy arms — TWIN fresh lakes are built by the SAME
:mod:`tests.fakes.poison_corpus` builder, scanned by the SAME scanner, and
healed by ``heal_findings`` flag-OFF (the verbatim Python core) vs flag-ON
in the post-burn-in steady state (``reset_lake_rust_waved_for_tests(remaining=0)``).
The comparison is FOUR-strength:

1. the normalized :class:`~brutex.data.lake_heal.HealReport` (counts, mode,
   rails, actions incl. ``raw_hash_before/after``, recall);
2. the FULL ``relpath → sha256`` tree map — every file under the lake root
   INCLUDING ``.quarantine/`` (byte-identical mutation + forensics);
3. the audit-line sequence AND the captured structlog event sequence
   (event names + levels + fields, paths normalized, timings dropped);
4. a convergence RE-SCAN (the second sweep's finding-type multiset — equal
   across arms; empty for the fully-healable classes).

Engagement is asserted anti-vacuously: the W-2 ENGAGED cases must run the
crate core (no ``heal_degrade`` fallback event), the unported/enveloped
cases must DEGRADE pre-mutation to the Python core (the crate preflight's
``{"degrade": ...}`` verdict) — either way the four-strength parity holds.
The content-token twin is compared per case too (token string + skip-event
replay parity).

Skips cleanly when the installed wheel predates the Wave-D W-2 API (the
lockstep law: crate and repo move together).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
_crate = pytest.importorskip("brutex_lake")
if not callable(getattr(_crate, "waved_heal_batch", None)):
    pytest.skip("installed brutex_lake wheel lacks the Wave-D W-2 API", allow_module_level=True)

from brutex.data import lake_rust_waved as lrw  # noqa: E402
from brutex.data.catalog import Catalog, CatalogEntry  # noqa: E402
from brutex.data.lake_heal import (  # noqa: E402
    HealReport,
    heal_findings,
    lake_content_token,
)
from brutex.data.lake_invariants import scan_lake_invariants  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

from tests.fakes.poison_corpus import CORPUS, PoisonCase, supports_symlink  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator


#: One fixed clock for BOTH arms — the quarantine stamp dir + the recall's
#: forming-session cutoff derive from it, so the twin lakes stay comparable.
_FIXED_NOW = datetime(2026, 6, 15, 18, 30, tzinfo=UTC)

#: Corpus cases whose batch the W-2 crate core must ENGAGE natively
#: (IntraFileDup / OptionIntraFileDup / SchemaAlienFile inside the
#: byte-parity envelope). Everything else with findings must DEGRADE
#: pre-mutation (unported kind or out-of-envelope file); finding-less cases
#: never reach the seam (the empty-batch short-circuit).
_ENGAGED_CASES = frozenset(
    {
        "bars_intra_file_dup",
        "bars_schema_alien_missing_columns",
        "option_intra_file_dup",
        "option_non_canonical_stem",
        "option_unicode_digit_filename",
        "glob_metachar_series",
        # These three plant an envelope-violating SIBLING the SCANNER skips
        # (round-12/18 gates) beside a clean dup file — so the BATCH the
        # healer receives is fully in-envelope and engages natively.
        "option_nested_dtype_list_side",
        "option_out_of_range_datetime",
        "dangling_symlink_option_file",
    }
)

#: Cases carrying an UNPORTED finding kind (foreign / drift): the shell's
#: ``_serialize_rust_findings`` short-circuits BEFORE the FFI — the crate is
#: never consulted (no facade event at all) and the Python core runs.
#: (No CORPUS case feeds an out-of-envelope file into the batch itself —
#: the scanner's dtype/range gates skip such files BEFORE a finding exists;
#: the crate-preflight degrade arm is exercised by
#: :class:`TestCrateEnvelopeDegrade`'s custom plants below.)
_UNPORTED_KIND_CASES = frozenset(
    {
        "bars_foreign_month_pure",
        "bars_foreign_month_overlap",
        "legacy_key_drift_sibling",
        "legacy_key_drift_canonical_keys",
        "bars_naive_timestamp_routing",
        "option_foreign_month_pure",
        "option_foreign_month_overlap",
        "option_multi_month_overlap",
        "option_null_side_foreign_key",
        "option_mass_poison_majority",
        "symlink_loop_partition_file",
    }
)

#: The volatile per-event timing keys dropped before the log comparison.
_TIMING_SUFFIXES = ("_ms",)


@pytest.fixture(autouse=True)
def _fresh_facade(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Default every test to flag OFF + a fresh facade view."""
    monkeypatch.delenv(lrw.LAKE_WAVED_RUST_ENV, raising=False)
    lrw.reset_lake_rust_waved_for_tests()
    yield
    lrw.reset_lake_rust_waved_for_tests()


class _FakeAudit:
    """A ``LakeAuditSink`` capturing every line in order."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict[str, Any] | None]] = []

    def append(self, *, action: str, subject: str, payload: dict[str, Any] | None = None) -> object:
        self.lines.append((action, subject, payload))
        return None


def _tree_state(root: Path) -> dict[str, str]:
    """Recursive ``relpath → sha256`` of EVERY file under *root* (byte pin).

    Includes ``.quarantine/`` (the forensic before-images), the healed
    partitions, the recall's re-derived files, catalogs and sidecars alike —
    lstat'd symlinks are hashed by their TARGET when readable, skipped when
    dangling (both arms build identical link shapes, so parity holds).
    ``.locks/`` is EXCLUDED: the advisory flock sidecars are empty files
    whose NAMES are ``sha256(realpath(dest))`` — the twin lakes live under
    different roots, so the names can never match (mechanical, not data).
    """
    out: dict[str, str] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".locks/"):
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _normalize(value: Any, lake: Path) -> Any:
    """Recursively replace the lake-root prefix with ``<LAKE>`` in strings."""
    if isinstance(value, str):
        return value.replace(str(lake), "<LAKE>")
    if isinstance(value, Mapping):
        return {key: _normalize(item, lake) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item, lake) for item in value]
    return value


def _normalized_report(report: HealReport, lake: Path) -> dict[str, Any]:
    return _normalize(dataclasses.asdict(report), lake)


def _normalized_logs(
    logs: Sequence[Mapping[str, Any]], lake: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(comparable events, facade-own events)`` — timings dropped, paths folded."""
    comparable: list[dict[str, Any]] = []
    facade: list[dict[str, Any]] = []
    for event in logs:
        name = str(event.get("event", ""))
        if name.startswith("lake_rust_waved."):
            facade.append(dict(event))
            continue
        cleaned = {
            key: _normalize(value, lake)
            for key, value in event.items()
            if not key.endswith(_TIMING_SUFFIXES)
        }
        comparable.append(cleaned)
    return comparable, facade


@dataclasses.dataclass
class _ArmResult:
    """One arm's full observable outcome (everything the differential pins)."""

    report: dict[str, Any]
    tree: dict[str, str]
    audit: list[Any]
    logs: list[dict[str, Any]]
    facade_events: list[dict[str, Any]]
    rescan_types: tuple[str, ...]


def _run_arm(
    base: Path,
    case: PoisonCase,
    *,
    flag_on: bool,
    monkeypatch: pytest.MonkeyPatch,
    mode: str | None = None,
) -> _ArmResult:
    """Build a FRESH lake for *case* and heal it under one flag arm."""
    lake = base / ("rust" if flag_on else "python")
    lake.mkdir()
    case.build(lake)
    if flag_on:
        monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
    else:
        monkeypatch.delenv(lrw.LAKE_WAVED_RUST_ENV, raising=False)
        lrw.reset_lake_rust_waved_for_tests()
    report = scan_lake_invariants(lake, **case.scan_kwargs)
    if flag_on:
        # W-4 fold: the enumeration scan above now ALSO routes through the
        # facade flag-ON (the scan seam), consuming the once-per-process
        # ``lake_rust_waved.engaged`` INFO before the capture window opens.
        # Re-arm the process view so the HEAL call's engagement witness
        # (`_engaged`) keeps its teeth — the assertion set is unchanged.
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
    # The L3 denominator law (round 16-1): option files join the mass-rail
    # denominator — files_scanned = bars + options scanned.
    files_scanned = report.files_scanned + report.option_files_scanned
    audit = _FakeAudit()
    kwargs: dict[str, Any] = {}
    if mode is not None:
        kwargs["mode"] = mode
    with capture_logs() as logs:
        heal = heal_findings(
            report.findings,
            lake_root=lake,
            files_scanned=files_scanned,
            audit=audit,
            now=lambda: _FIXED_NOW,
            **kwargs,
        )
    rescan = scan_lake_invariants(lake, **case.scan_kwargs)
    comparable, facade = _normalized_logs(logs, lake)
    return _ArmResult(
        report=_normalized_report(heal, lake),
        tree=_tree_state(lake),
        audit=_normalize(audit.lines, lake),
        logs=comparable,
        facade_events=facade,
        rescan_types=tuple(sorted(type(f).__name__ for f in rescan.findings)),
    )


def _assert_arms_equal(python_arm: _ArmResult, rust_arm: _ArmResult) -> None:
    assert rust_arm.report == python_arm.report
    assert rust_arm.tree == python_arm.tree
    assert rust_arm.audit == python_arm.audit
    assert rust_arm.logs == python_arm.logs
    assert rust_arm.rescan_types == python_arm.rescan_types
    # NOTE: the content token is deliberately NOT compared across the twin
    # lakes — it folds mtime_ns, which differs per build instant; the
    # same-lake rust-vs-oracle token parity lives in
    # TestContentTokenDifferential below.


def _degraded(arm: _ArmResult) -> bool:
    return any(
        event.get("event") == "lake_rust_waved.fallback" and event.get("reason") == "heal_degrade"
        for event in arm.facade_events
    )


def _engaged(arm: _ArmResult) -> bool:
    return any(event.get("event") == "lake_rust_waved.engaged" for event in arm.facade_events)


class TestHealDifferential:
    """Per-corpus-case twin-lake heal parity (the §E gate)."""

    @pytest.mark.parametrize("case", CORPUS, ids=lambda case: str(case.name))
    def test_flag_on_equals_flag_off(
        self, case: PoisonCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if case.requires == "symlink" and not supports_symlink(tmp_path):
            pytest.skip("filesystem does not support symlinks")
        python_arm = _run_arm(tmp_path, case, flag_on=False, monkeypatch=monkeypatch)
        rust_arm = _run_arm(tmp_path, case, flag_on=True, monkeypatch=monkeypatch)
        _assert_arms_equal(python_arm, rust_arm)
        # Anti-vacuous engagement census: the engaged set runs the crate
        # core (crate consulted, no pre-mutation degrade); the enveloped set
        # reaches the crate and MUST degrade; the unported-kind set
        # short-circuits shell-side (the crate is never consulted); every
        # finding-less batch never reaches the seam either.
        if case.name in _ENGAGED_CASES:
            assert _engaged(rust_arm) and not _degraded(rust_arm), case.name
        else:
            assert not rust_arm.facade_events, case.name

    def test_corpus_expectation_census_is_total(self) -> None:
        """Every corpus case with healable findings is classified exactly once."""
        buckets = (_ENGAGED_CASES, _UNPORTED_KIND_CASES)
        for i, left in enumerate(buckets):
            for right in buckets[i + 1 :]:
                assert not (left & right)
        names = {case.name for case in CORPUS}
        for bucket in buckets:
            assert bucket <= names
        healable_kinds = {
            "IntraFileDup",
            "OptionIntraFileDup",
            "SchemaAlienFile",
            "ForeignMonthRows",
            "OptionForeignMonthRows",
            "LegacyKeyDrift",
        }
        with_healable = {
            case.name for case in CORPUS if case.expected_finding_types & healable_kinds
        }
        assert with_healable == _ENGAGED_CASES | _UNPORTED_KIND_CASES

    def test_mass_rail_arm_refuses_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The mass-heal rail refuses BEFORE the seam — parity + zero mutation."""
        case = next(c for c in CORPUS if c.name == "option_mass_poison_majority")
        python_arm = _run_arm(tmp_path, case, flag_on=False, monkeypatch=monkeypatch)
        rust_arm = _run_arm(tmp_path, case, flag_on=True, monkeypatch=monkeypatch)
        _assert_arms_equal(python_arm, rust_arm)
        assert rust_arm.report["refused_mass_heal"] is True
        assert rust_arm.report["healed"] == 0
        assert not rust_arm.facade_events  # the rail returns before the seam

    def test_observe_mode_arm_short_circuits_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Observe mode short-circuits BEFORE the seam — parity + zero mutation."""
        case = next(c for c in CORPUS if c.name == "bars_intra_file_dup")
        python_arm = _run_arm(
            tmp_path, case, flag_on=False, monkeypatch=monkeypatch, mode="observe"
        )
        rust_arm = _run_arm(tmp_path, case, flag_on=True, monkeypatch=monkeypatch, mode="observe")
        _assert_arms_equal(python_arm, rust_arm)
        assert rust_arm.report["mode"] == "observe"
        assert rust_arm.report["healed"] == 0
        assert rust_arm.report["quarantine_root"] is None
        # Observe returns pre-seam for the HEAL; the token twin still engages.
        assert not any(e.get("event") == "lake_rust_waved.fallback" for e in rust_arm.facade_events)

    def test_engaged_dup_heal_convergence_and_forensics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The engaged bars-dup arm: healed, byte-identical, second sweep clean."""
        case = next(c for c in CORPUS if c.name == "bars_intra_file_dup")
        python_arm = _run_arm(tmp_path, case, flag_on=False, monkeypatch=monkeypatch)
        rust_arm = _run_arm(tmp_path, case, flag_on=True, monkeypatch=monkeypatch)
        _assert_arms_equal(python_arm, rust_arm)
        assert rust_arm.report["healed"] == 1
        assert rust_arm.rescan_types == ()
        # The forensic before-image landed under .quarantine/ in BOTH trees.
        assert any(rel.startswith(".quarantine/") for rel in rust_arm.tree)
        # The audit chain carries the dedup line in both arms.
        assert [line[0] for line in rust_arm.audit] == [line[0] for line in python_arm.audit]
        assert "LAKE_HEAL_DEDUP" in [line[0] for line in rust_arm.audit]


class TestCrateEnvelopeDegrade:
    """The crate-preflight degrade arm — batches OUTSIDE the W-2 byte-parity
    envelope must degrade PRE-MUTATION to the Python core with full parity.

    No poison-corpus case reaches this arm (the scanner's dtype/range gates
    skip envelope-violating files before a finding exists), so the plants
    here build IN-envelope-for-the-SCANNER files the CRATE refuses: a
    beyond-2^53-paisa price, a 17-column Greeks-overlay schema, and a MIXED
    engaged+unported batch (the shell-side short-circuit).
    """

    @staticmethod
    def _build_big_price_dup(lake: Path) -> None:
        # A dup file whose prices exceed the 2dp snap envelope (1e17 rupees
        # -> 1e19 paisa >= 2^53): the crate degrades, Python's Decimal path
        # heals it exactly.
        from tests.fakes.poison_corpus import _dest, _plant_raw, _ts

        _plant_raw(_dest(lake), [_ts(1), _ts(1), _ts(2)], price=1.0e17)

    @staticmethod
    def _build_overlay_dup(lake: Path) -> None:
        # A 17-column Greeks-overlay F&O dup file: the schema is not the
        # canonical 7-column bars shape, so the crate degrades and Python's
        # overlay-preserving decode heals it (the overlay must round-trip).
        import pyarrow as pa
        import pyarrow.parquet as pq
        from brutex.data.models import GREEKS_OVERLAY_FIELDS

        from tests.fakes.poison_corpus import _dest, _ts

        dest = _dest(lake, segment="FNO", symbol="NSE-NIFTY-26Jun25-25000-CE")
        dest.parent.mkdir(parents=True, exist_ok=True)
        stamps = [_ts(1), _ts(1), _ts(2)]
        columns: dict[str, Any] = {
            "timestamp": pa.array(stamps, type=pa.timestamp("us", tz="UTC")),
            "open": [100.0, 200.0, 101.0],
            "high": [100.0, 200.0, 101.0],
            "low": [100.0, 200.0, 101.0],
            "close": [100.0, 200.0, 101.0],
            "volume": pa.array([10, 20, 30], type=pa.int64()),
            "open_interest": pa.array([5, 6, 7], type=pa.int64()),
        }
        for field in GREEKS_OVERLAY_FIELDS:
            if field == "greeks_provenance_id":
                columns[field] = pa.array([7, None, 7], type=pa.int32())
            else:
                columns[field] = pa.array([0.5, None, 0.25], type=pa.float64())
        pq.write_table(pa.table(columns), dest)

    @staticmethod
    def _build_mixed_batch(lake: Path) -> None:
        # An engaged-kind dup file PLUS an unported-kind foreign file in ONE
        # batch: the shell short-circuits the whole batch to Python.
        from tests.fakes.poison_corpus import _dest, _plant_raw, _ts

        _plant_raw(_dest(lake), [_ts(1), _ts(1), _ts(2)])
        _plant_raw(
            _dest(lake, symbol="BANKNIFTY", month=7),
            [_ts(3), _ts(2, month=7, day=1)],
        )

    def _case(self, name: str, build: Any, kinds: frozenset[str]) -> PoisonCase:
        return PoisonCase(name, build, kinds)

    def test_big_price_dup_degrades_with_parity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = self._case("big_price_dup", self._build_big_price_dup, frozenset({"IntraFileDup"}))
        python_arm = _run_arm(tmp_path, case, flag_on=False, monkeypatch=monkeypatch)
        rust_arm = _run_arm(tmp_path, case, flag_on=True, monkeypatch=monkeypatch)
        _assert_arms_equal(python_arm, rust_arm)
        assert _engaged(rust_arm) and _degraded(rust_arm)
        assert rust_arm.report["healed"] == 1
        assert rust_arm.rescan_types == ()

    def test_overlay_dup_degrades_with_parity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = self._case("overlay_dup", self._build_overlay_dup, frozenset({"IntraFileDup"}))
        python_arm = _run_arm(tmp_path, case, flag_on=False, monkeypatch=monkeypatch)
        rust_arm = _run_arm(tmp_path, case, flag_on=True, monkeypatch=monkeypatch)
        _assert_arms_equal(python_arm, rust_arm)
        assert _engaged(rust_arm) and _degraded(rust_arm)
        assert rust_arm.report["healed"] == 1
        # The overlay survived the Python heal byte-identically in BOTH arms
        # (tree equality above); the healed file still carries 17 columns.
        import polars as pl

        healed = next(
            path
            for rel, _ in rust_arm.tree.items()
            if rel.startswith("bars/NSE/FNO") and not rel.startswith(".quarantine")
            for path in [tmp_path / "rust" / rel]
        )
        assert len(pl.read_parquet(healed, glob=False).columns) == 17

    def test_mixed_batch_short_circuits_with_parity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = self._case(
            "mixed_batch",
            self._build_mixed_batch,
            frozenset({"IntraFileDup", "ForeignMonthRows"}),
        )
        python_arm = _run_arm(tmp_path, case, flag_on=False, monkeypatch=monkeypatch)
        rust_arm = _run_arm(tmp_path, case, flag_on=True, monkeypatch=monkeypatch)
        _assert_arms_equal(python_arm, rust_arm)
        # The unported kind short-circuits shell-side: no crate consult.
        assert not rust_arm.facade_events
        assert rust_arm.report["healed"] == 2
        assert rust_arm.rescan_types == ()


class TestCatalogReplayParity:
    """The catalog side-effect replay (Golden Rule #4) — rotated rows equal."""

    def _heal_with_catalog(
        self,
        base: Path,
        case: PoisonCase,
        *,
        flag_on: bool,
        monkeypatch: pytest.MonkeyPatch,
        seed: bool,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
        lake = base / ("rust" if flag_on else "python")
        lake.mkdir()
        case.build(lake)
        if flag_on:
            monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
            lrw.reset_lake_rust_waved_for_tests(remaining=0)
        else:
            monkeypatch.delenv(lrw.LAKE_WAVED_RUST_ENV, raising=False)
            lrw.reset_lake_rust_waved_for_tests()
        report = scan_lake_invariants(lake, **case.scan_kwargs)
        files_scanned = report.files_scanned + report.option_files_scanned
        with Catalog.open(lake / "catalog.duckdb") as catalog:
            if seed:
                # A pre-heal row at the poisoned coords, so a DELETING heal
                # (the alien quarantine-move) has a row to drop.
                catalog.upsert(
                    CatalogEntry(
                        exchange="NSE",
                        segment="INDEX",
                        symbol="NIFTY",
                        interval="1minute",
                        year=2024,
                        month=6,
                        raw_hash="0" * 64,
                        row_count=1,
                        first_ts=datetime(2024, 6, 3, 4, 0, tzinfo=UTC),
                        last_ts=datetime(2024, 6, 3, 4, 0, tzinfo=UTC),
                        fetched_at=_FIXED_NOW,
                    )
                )
            heal = heal_findings(
                report.findings,
                lake_root=lake,
                files_scanned=files_scanned,
                catalog=catalog,
                now=lambda: _FIXED_NOW,
            )
            entry = catalog.get(
                exchange="NSE",
                segment="INDEX",
                symbol="NIFTY",
                interval="1minute",
                year=2024,
                month=6,
            )
            row: dict[str, Any] | None = None
            if entry is not None:
                row = dataclasses.asdict(entry)
                # fetched_at (now()-stamped by make_catalog_entry) is the ONE
                # sanctioned volatile column — dropped from the comparison.
                row.pop("fetched_at")
            count = catalog.count()
        return _normalized_report(heal, lake), row, count

    def test_bars_dup_catalog_row_rotated_equally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = next(c for c in CORPUS if c.name == "bars_intra_file_dup")
        py_report, py_row, py_count = self._heal_with_catalog(
            tmp_path, case, flag_on=False, monkeypatch=monkeypatch, seed=False
        )
        rust_report, rust_row, rust_count = self._heal_with_catalog(
            tmp_path, case, flag_on=True, monkeypatch=monkeypatch, seed=False
        )
        assert rust_report == py_report
        assert rust_row == py_row
        assert rust_row is not None  # the healed partition's rotated row
        assert rust_row["raw_hash"] == py_report["actions"][0]["raw_hash_after"]
        assert rust_count == py_count == 1

    def test_alien_catalog_row_deleted_equally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        case = next(c for c in CORPUS if c.name == "bars_schema_alien_missing_columns")
        py_report, py_row, py_count = self._heal_with_catalog(
            tmp_path, case, flag_on=False, monkeypatch=monkeypatch, seed=True
        )
        rust_report, rust_row, rust_count = self._heal_with_catalog(
            tmp_path, case, flag_on=True, monkeypatch=monkeypatch, seed=True
        )
        assert rust_report == py_report
        assert rust_row is None
        assert py_row is None
        assert rust_count == py_count == 0


class TestContentTokenDifferential:
    """The §C-5 token twin — parity + skip-event replay on hostile trees."""

    def test_token_matches_oracle_on_every_corpus_case(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.data.lake_heal import _lake_content_token_python

        for case in CORPUS:
            if case.requires == "symlink" and not supports_symlink(tmp_path):
                continue
            lake = tmp_path / f"tok-{case.name}"
            lake.mkdir()
            case.build(lake)
            monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
            lrw.reset_lake_rust_waved_for_tests(remaining=0)
            assert lake_content_token(lake) == _lake_content_token_python(lake), case.name

    def test_dangling_symlink_skip_events_replay_identically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dangling ``*.parquet`` symlink: token + WARN parity (lstat law)."""
        if not supports_symlink(tmp_path):
            pytest.skip("filesystem does not support symlinks")
        from brutex.data.lake_heal import _lake_content_token_python

        case = next(c for c in CORPUS if c.name == "dangling_symlink_option_file")
        lake = tmp_path / "lake"
        lake.mkdir()
        case.build(lake)
        with capture_logs() as off_logs:
            off_token = _lake_content_token_python(lake)
        monkeypatch.setenv(lrw.LAKE_WAVED_RUST_ENV, "1")
        lrw.reset_lake_rust_waved_for_tests(remaining=0)
        with capture_logs() as on_logs:
            on_token = lake_content_token(lake)
        assert on_token == off_token
        skip_events = [e for e in off_logs if str(e["event"]).startswith("lake_heal.token_")]
        on_skip_events = [e for e in on_logs if str(e["event"]).startswith("lake_heal.token_")]
        assert on_skip_events == skip_events
