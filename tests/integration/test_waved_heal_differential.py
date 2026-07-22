"""W-6a ENDGAME heal gate — Rust-arm determinism + frozen expectations.

THE FLIP LANDED (DEC-RUST-LAKE-WAVED-001 / Wave-D design §E): the Python
per-finding heal core is DELETED, so the pre-endgame flag-ON≡flag-OFF
twin-lake differential has no Python arm left to run. This suite is the
heal seam's real-wheel verification in the endgame shape:

1. **Determinism** — TWIN fresh lakes per corpus case, healed through the
   ONE Rust-backed ``heal_findings`` path, compared FOUR-strength: the
   normalized :class:`~brutex.data.lake_heal.HealReport`, the FULL
   ``relpath → sha256`` tree map (``.quarantine/`` included), the
   audit-line + structlog event sequences, and the convergence RE-SCAN.
2. **Frozen expectations** — the per-case ``(healed, skipped, errors,
   refused_mass_heal, rescan-type multiset)`` table below was captured
   while the W-2..W-5 Python-parity differential was still green (the
   pre-delete parity record); the crate must keep matching it forever,
   by name, per case.
3. **The preserve arm + the isolated-error posture** — since W-6b the
   17-column Greeks-overlay dup and the finite big-price dup HEAL through
   the crate's frame-level verbatim keep-first arm (value-oracle-pinned
   in-test); every remaining out-of-envelope shape (non-finite prices,
   drifted schemas) is a PER-FINDING ISOLATED error: counted,
   byte-untouched, the batch CONTINUES, the finding stays loud on
   re-scan.
4. **Shell-policy pins** — mass-rail refusal + observe mode short-circuit
   BEFORE the seam; crash-safe leg ordering (copy-forward before
   removal); catalog rotation/deletion; the content-token twin's
   determinism + skip-event replay.

Requires the built ``brutex_lake`` wheel — with the Python core deleted
the wheel is a production PREREQUISITE; skips honestly when absent (the
unit suites own the wheel-free typed-refusal story).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
_crate = pytest.importorskip("brutex_lake")
if not callable(getattr(_crate, "waved_endgame_witness", None)):
    pytest.skip(
        "installed brutex_lake wheel predates the W-6a endgame API", allow_module_level=True
    )

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


#: One fixed clock for BOTH builds — the quarantine stamp dir + the recall's
#: forming-session cutoff derive from it, so the twin lakes stay comparable.
_FIXED_NOW = datetime(2026, 6, 15, 18, 30, tzinfo=UTC)

#: The volatile per-event timing keys dropped before the log comparison.
_TIMING_SUFFIXES = ("_ms",)

#: The FROZEN per-case heal verdict — ``(healed, skipped, errors,
#: refused_mass_heal, sorted rescan finding-type tuple)`` — captured from
#: the last green Python-parity differential run (the pre-delete record).
#: A drift here is a CRATE regression: fix the crate, never this table.
_FROZEN_HEAL: Final[Mapping[str, tuple[int, int, int, bool, tuple[str, ...]]]] = {
    "bars_intra_file_dup": (1, 0, 0, False, ()),
    "bars_foreign_month_pure": (1, 0, 0, False, ()),
    "bars_foreign_month_overlap": (1, 0, 0, False, ()),
    "legacy_key_drift_sibling": (1, 0, 0, False, ()),
    "legacy_key_drift_canonical_keys": (1, 0, 0, False, ()),
    # The naive-ts source: the crate OWNS the refusal verdict (isolated
    # error each sweep — the never-converging documented envelope).
    "bars_naive_timestamp_routing": (0, 0, 1, False, ("ForeignMonthRows",)),
    "bars_zero_row_file": (0, 0, 0, False, ()),
    "bars_schema_alien_missing_columns": (1, 0, 0, False, ()),
    # Observe-only finding class — the healer filters it (never value-edits).
    "bars_price_sanity_rows": (0, 0, 0, False, ("PriceSanityRows",)),
    "bars_out_of_range_datetime": (0, 0, 0, False, ()),
    "option_intra_file_dup": (1, 0, 0, False, ()),
    "option_foreign_month_pure": (1, 0, 0, False, ()),
    "option_foreign_month_overlap": (1, 0, 0, False, ()),
    "option_multi_month_overlap": (1, 0, 0, False, ()),
    "option_null_side_foreign_key": (1, 0, 0, False, ()),
    "option_non_canonical_stem": (1, 0, 0, False, ()),
    "option_unicode_digit_filename": (1, 0, 0, False, ()),
    "glob_metachar_series": (3, 0, 0, False, ()),
    "option_nested_dtype_list_side": (1, 0, 0, False, ()),
    "option_nested_dtype_struct_side": (0, 0, 0, False, ()),
    "option_out_of_range_datetime": (1, 0, 0, False, ()),
    "option_mass_poison_majority": (0, 0, 0, True, ("OptionForeignMonthRows",) * 11),
    "symlinked_series_dir": (0, 0, 0, False, ()),
    "symlinked_options_root": (0, 0, 0, False, ()),
    "symlinked_partition_file": (0, 0, 0, False, ()),
    # The symlink-loop reassignment dest: the crate owns the containment
    # refusal (isolated error, nothing mutated).
    "symlink_loop_partition_file": (0, 0, 1, False, ("OptionForeignMonthRows",)),
    "dangling_symlink_option_file": (1, 0, 0, False, ()),
}


@pytest.fixture(autouse=True)
def _endgame_arm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The ONE Rust path: retired flag unset, module state reset each test."""
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
    dangling (both builds plant identical link shapes, so parity holds).
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


def _normalized_logs(logs: Sequence[Mapping[str, Any]], lake: Path) -> list[dict[str, Any]]:
    """The comparable event list — timings dropped, paths folded."""
    return [
        {
            key: _normalize(value, lake)
            for key, value in event.items()
            if not key.endswith(_TIMING_SUFFIXES)
        }
        for event in logs
    ]


@dataclasses.dataclass
class _BuildResult:
    """One fresh build's full observable outcome (everything the gate pins)."""

    raw_report: HealReport
    report: dict[str, Any]
    tree: dict[str, str]
    audit: list[Any]
    logs: list[dict[str, Any]]
    rescan_types: tuple[str, ...]


def _run_build(
    base: Path,
    case: PoisonCase,
    *,
    label: str,
    mode: str | None = None,
) -> _BuildResult:
    """Build a FRESH lake for *case* and heal it through the ONE Rust path."""
    lake = base / label
    lake.mkdir()
    case.build(lake)
    report = scan_lake_invariants(lake, **case.scan_kwargs)
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
    return _BuildResult(
        raw_report=heal,
        report=_normalized_report(heal, lake),
        tree=_tree_state(lake),
        audit=_normalize(audit.lines, lake),
        logs=_normalized_logs(logs, lake),
        rescan_types=tuple(sorted(type(f).__name__ for f in rescan.findings)),
    )


def _assert_builds_equal(build_a: _BuildResult, build_b: _BuildResult) -> None:
    assert build_b.report == build_a.report
    assert build_b.tree == build_a.tree
    assert build_b.audit == build_a.audit
    assert build_b.logs == build_a.logs
    assert build_b.rescan_types == build_a.rescan_types


def _assert_frozen(case: PoisonCase, build: _BuildResult) -> None:
    healed, skipped, errors, refused, rescan = _FROZEN_HEAL[case.name]
    got = (
        build.raw_report.healed,
        build.raw_report.skipped,
        build.raw_report.errors,
        build.raw_report.refused_mass_heal,
        build.rescan_types,
    )
    assert got == (healed, skipped, errors, refused, rescan), (
        f"FROZEN-EXPECTATION drift [{case.name}]: got {got} — the crate no "
        "longer matches the pre-delete Python-parity record; fix the CRATE, "
        "never this table"
    )


class TestHealEndgame:
    """Per-corpus-case determinism + the frozen pre-delete parity record."""

    @pytest.mark.parametrize("case", CORPUS, ids=lambda case: str(case.name))
    def test_deterministic_and_frozen(self, case: PoisonCase, tmp_path: Path) -> None:
        if case.requires == "symlink" and not supports_symlink(tmp_path):
            pytest.skip("filesystem does not support symlinks")
        build_a = _run_build(tmp_path, case, label="a")
        build_b = _run_build(tmp_path, case, label="b")
        _assert_builds_equal(build_a, build_b)
        _assert_frozen(case, build_b)

    def test_frozen_table_is_total(self) -> None:
        """Every corpus case has EXACTLY one frozen expectation row."""
        assert set(_FROZEN_HEAL) == {case.name for case in CORPUS}

    def test_engaged_kind_lockstep_witness(self) -> None:
        """The installed wheel's engaged-kind list covers ALL SIX healable
        kinds (a stale pre-W-2b wheel fails here, never a silent narrowing)."""
        assert set(_crate.waved_heal_engaged_kinds()) == {
            "IntraFileDup",
            "OptionIntraFileDup",
            "SchemaAlienFile",
            "ForeignMonthRows",
            "OptionForeignMonthRows",
            "LegacyKeyDrift",
        }

    def test_mass_rail_refuses_before_the_seam(self, tmp_path: Path) -> None:
        """The mass-heal rail refuses BEFORE the per-finding loop — zero
        mutation, every finding still present on re-scan."""
        case = next(c for c in CORPUS if c.name == "option_mass_poison_majority")
        pre = tmp_path / "pre"
        pre.mkdir()
        case.build(pre)
        before = _tree_state(pre)
        build = _run_build(tmp_path, case, label="a")
        assert build.raw_report.refused_mass_heal is True
        assert build.raw_report.healed == 0
        # Byte-untouched: the healed lake's tree equals an unhealed twin's.
        assert build.tree == before

    def test_observe_mode_short_circuits(self, tmp_path: Path) -> None:
        """Observe mode short-circuits BEFORE the seam — zero mutation."""
        case = next(c for c in CORPUS if c.name == "bars_intra_file_dup")
        pre = tmp_path / "pre"
        pre.mkdir()
        case.build(pre)
        before = _tree_state(pre)
        build = _run_build(tmp_path, case, label="a", mode="observe")
        assert build.raw_report.mode == "observe"
        assert build.raw_report.healed == 0
        assert build.raw_report.quarantine_root is None
        assert build.tree == before

    def test_engaged_dup_heal_convergence_and_forensics(self, tmp_path: Path) -> None:
        """The bars-dup heal: healed, quarantined before-image, audit line,
        second sweep clean — the frozen forensic contract."""
        case = next(c for c in CORPUS if c.name == "bars_intra_file_dup")
        build = _run_build(tmp_path, case, label="a")
        assert build.raw_report.healed == 1
        assert build.rescan_types == ()
        # The forensic before-image landed under .quarantine/.
        assert any(rel.startswith(".quarantine/") for rel in build.tree)
        # The audit chain carries the dedup line.
        assert "LAKE_HEAL_DEDUP" in [line[0] for line in build.audit]


class TestEnvelopeIsolatedErrors:
    """The out-of-envelope posture, W-6b shape.

    Since W-6b the two formerly-refused single-file dedup shapes — the
    17-column Greeks-overlay dup (the production-reachable shape Greeks
    enrichment writes) and the finite beyond-2^53-paisa big-price dup —
    HEAL through the crate's frame-level PRESERVE arm: keep-FIRST unique
    on ``timestamp``, every kept row's stored values preserved VERBATIM
    (strictly row-removing — never value-editing), rendered through the
    PR-1 bars profile. The value-level oracle is computed IN-TEST with
    polars keep-first (the deleted Python core's `_read_existing_bars`
    dict law, confirmed against ``git show 9aa3db8`` — never resurrected
    in-repo).

    Shapes still OUTSIDE every native arm (non-finite prices, drifted
    schemas, law-violating rows) stay PER-FINDING ISOLATED errors:
    counted, byte-untouched, the batch CONTINUES, the finding stays loud
    on re-scan (there is no Python core to degrade to).
    """

    @staticmethod
    def _build_big_price_dup(lake: Path) -> None:
        # A dup file whose prices exceed the 2dp snap envelope (1e17 rupees
        # -> 1e19 paisa >= 2^53).
        from tests.fakes.poison_corpus import _dest, _plant_raw, _ts

        _plant_raw(_dest(lake), [_ts(1), _ts(1), _ts(2)], price=1.0e17)

    @staticmethod
    def _build_overlay_dup(lake: Path) -> None:
        # A 17-column Greeks-overlay F&O dup file: not the canonical
        # 7-column bars shape.
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
        # A dup file PLUS a foreign file in ONE batch — both kinds engage
        # natively; the whole batch runs the crate core.
        from tests.fakes.poison_corpus import _dest, _plant_raw, _ts

        _plant_raw(_dest(lake), [_ts(1), _ts(1), _ts(2)])
        _plant_raw(
            _dest(lake, symbol="BANKNIFTY", month=7),
            [_ts(3), _ts(2, month=7, day=1)],
        )

    @staticmethod
    def _build_nan_price_dup(lake: Path) -> None:
        # A dup file with a NON-finite price — corrupt values are never
        # silently blessed, so the preserve arm refuses (isolated error).
        from tests.fakes.poison_corpus import _dest, _plant_raw, _ts

        _plant_raw(_dest(lake), [_ts(1), _ts(1), _ts(2)], price=float("nan"))

    @staticmethod
    def _build_drifted_overlay_dup(lake: Path) -> None:
        # 17 columns but a DRIFTED overlay name — alien, refused.
        import pyarrow.parquet as pq

        TestEnvelopeIsolatedErrors._build_overlay_dup(lake)
        from tests.fakes.poison_corpus import _dest

        dest = _dest(lake, segment="FNO", symbol="NSE-NIFTY-26Jun25-25000-CE")
        table = pq.read_table(dest)
        table = table.rename_columns(
            ["implied_vol" if name == "iv" else name for name in table.column_names]
        )
        pq.write_table(table, dest)

    @pytest.mark.parametrize(
        ("name", "builder", "dest_kwargs"),
        [
            ("big_price_dup", "_build_big_price_dup", {}),
            (
                "overlay_dup",
                "_build_overlay_dup",
                {"segment": "FNO", "symbol": "NSE-NIFTY-26Jun25-25000-CE"},
            ),
        ],
    )
    def test_preserve_shape_heals_verbatim_keep_first(
        self, tmp_path: Path, name: str, builder: str, dest_kwargs: dict[str, Any]
    ) -> None:
        """W-6b (conscious pin FLIP — this shape was an isolated error until
        the crate widened): the preserve-class dup HEALS — keep-first on
        ``timestamp`` with every column's stored values verbatim, the
        forensic before-image quarantined, the rewritten file readable and
        value-equal to the in-test polars keep-first oracle."""
        import polars as pl
        from polars.testing import assert_frame_equal

        from tests.fakes.poison_corpus import _dest

        case = PoisonCase(name, getattr(self, builder), frozenset({"IntraFileDup"}))
        pre = tmp_path / "pre"
        pre.mkdir()
        case.build(pre)
        rel = _dest(pre, **dest_kwargs).relative_to(pre)
        planted_bytes = (pre / rel).read_bytes()
        # The value-level oracle: the deleted Python core's keep-FIRST dict
        # law (first physical row wins) + the ascending-timestamp render.
        planted = pl.read_parquet(pre / rel)
        expected = planted.unique(subset=["timestamp"], keep="first", maintain_order=True).sort(
            "timestamp"
        )
        assert expected.height < planted.height  # the plant genuinely dups

        build_a = _run_build(tmp_path, case, label="a")
        build_b = _run_build(tmp_path, case, label="b")
        _assert_builds_equal(build_a, build_b)
        assert build_b.raw_report.healed == 1
        assert build_b.raw_report.errors == 0
        assert build_b.rescan_types == ()
        # The roundtrip read + the value/dtype-exact keep-first oracle.
        healed = pl.read_parquet(tmp_path / "b" / rel)
        assert_frame_equal(healed, expected)
        # The forensic before-image is the EXACT planted bytes.
        quarantined = [
            p
            for p in (tmp_path / "b").rglob("*.parquet")
            if ".quarantine" in p.parts and p.name == rel.name
        ]
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == planted_bytes
        # The audit chain carries the dedup line.
        assert "LAKE_HEAL_DEDUP" in [line[0] for line in build_b.audit]

    @pytest.mark.parametrize(
        ("name", "builder", "envelope_token", "rescan"),
        [
            # NaN prices ALSO carry the observe-only PriceSanityRows finding
            # (the #1897 `~is_finite` arm) — filtered by the heal, re-found
            # on the re-scan beside the still-unhealed dup.
            (
                "nan_price_dup",
                "_build_nan_price_dup",
                "not finite",
                ("IntraFileDup", "PriceSanityRows"),
            ),
            (
                "drifted_overlay_dup",
                "_build_drifted_overlay_dup",
                "schema not canonical",
                ("IntraFileDup",),
            ),
        ],
    )
    def test_envelope_shape_is_an_isolated_error(
        self,
        tmp_path: Path,
        name: str,
        builder: str,
        envelope_token: str,
        rescan: tuple[str, ...],
    ) -> None:
        case = PoisonCase(name, getattr(self, builder), frozenset({"IntraFileDup"}))
        pre = tmp_path / "pre"
        pre.mkdir()
        case.build(pre)
        before = _tree_state(pre)
        build_a = _run_build(tmp_path, case, label="a")
        build_b = _run_build(tmp_path, case, label="b")
        _assert_builds_equal(build_a, build_b)
        assert build_b.raw_report.healed == 0
        assert build_b.raw_report.errors == 1
        # Byte-untouched + the finding persists loudly on re-scan.
        assert build_b.tree == before
        assert build_b.rescan_types == rescan
        errs = [e for e in build_b.logs if e.get("event") == "lake_heal.partition_error"]
        assert len(errs) == 1 and envelope_token in str(errs[0].get("error"))

    def test_mixed_batch_heals_both_kinds(self, tmp_path: Path) -> None:
        case = PoisonCase(
            "mixed_batch",
            self._build_mixed_batch,
            frozenset({"IntraFileDup", "ForeignMonthRows"}),
        )
        build_a = _run_build(tmp_path, case, label="a")
        build_b = _run_build(tmp_path, case, label="b")
        _assert_builds_equal(build_a, build_b)
        assert build_b.raw_report.healed == 2
        assert build_b.rescan_types == ()


class TestCrashConvergence:
    """The W-2b reassignment crash-safety law: copy-forward FIRST, remove
    SECOND — a crash between the legs leaves rows in BOTH files, the finding
    re-appears, and the keep-first union converges idempotently.
    """

    def test_copy_forward_precedes_source_removal(self, tmp_path: Path) -> None:
        """Ordering pin: the destination-month write telemetry
        (``lake.partition.write`` — the copy-forward leg) is emitted BEFORE
        the source's ``lake_heal.rows_reassigned`` (the removal leg) — the
        crate preserves the crash-safe leg order."""
        case = next(c for c in CORPUS if c.name == "bars_foreign_month_pure")
        build = _run_build(tmp_path, case, label="a")
        events = [str(event.get("event", "")) for event in build.logs]
        assert "lake.partition.write" in events
        assert "lake_heal.rows_reassigned" in events
        assert events.index("lake.partition.write") < events.index("lake_heal.rows_reassigned")

    def test_mid_crash_state_converges_keep_first(self, tmp_path: Path) -> None:
        """The overlap corpus shapes ARE the post-copy-forward pre-removal
        crash state (the foreign keys already live in their canonical home):
        the finding re-appears on scan and the heal CONVERGES — the
        keep-first union never duplicates a key, the source is cleaned, the
        second sweep is empty (bars + options multi-month)."""
        for name in ("bars_foreign_month_overlap", "option_multi_month_overlap"):
            case = next(c for c in CORPUS if c.name == name)
            base = tmp_path / name
            base.mkdir()
            build_a = _run_build(base, case, label="a")
            build_b = _run_build(base, case, label="b")
            _assert_builds_equal(build_a, build_b)
            assert build_b.raw_report.healed >= 1, name
            assert build_b.rescan_types == (), name


class TestCatalogReplay:
    """The catalog side-effect replay (Golden Rule #4) — rotated rows frozen."""

    def _heal_with_catalog(
        self, base: Path, case: PoisonCase, *, seed: bool
    ) -> tuple[HealReport, dict[str, Any] | None, int]:
        lake = base / "lake"
        lake.mkdir()
        case.build(lake)
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
        return heal, row, count

    def test_bars_dup_catalog_row_rotated(self, tmp_path: Path) -> None:
        case = next(c for c in CORPUS if c.name == "bars_intra_file_dup")
        heal, row, count = self._heal_with_catalog(tmp_path, case, seed=False)
        assert heal.healed == 1
        assert row is not None  # the healed partition's rotated row
        assert row["raw_hash"] == heal.actions[0].raw_hash_after
        assert count == 1

    def test_alien_catalog_row_deleted(self, tmp_path: Path) -> None:
        case = next(c for c in CORPUS if c.name == "bars_schema_alien_missing_columns")
        heal, row, count = self._heal_with_catalog(tmp_path, case, seed=True)
        assert heal.healed == 1
        assert row is None
        assert count == 0


class TestContentToken:
    """The §C-5 token twin — determinism + content sensitivity + skip replay."""

    def test_token_is_deterministic_and_content_sensitive(self, tmp_path: Path) -> None:
        case = next(c for c in CORPUS if c.name == "bars_intra_file_dup")
        lake = tmp_path / "lake"
        lake.mkdir()
        case.build(lake)
        first = lake_content_token(lake)
        second = lake_content_token(lake)
        assert first == second and len(first) == 64
        # A new file under the walked tree MOVES the token.
        extra = lake / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "07.parquet"
        extra.write_bytes(b"not-a-parquet")
        assert lake_content_token(lake) != first

    def test_dangling_symlink_rides_the_lstat_law(self, tmp_path: Path) -> None:
        """A dangling ``*.parquet`` symlink: the R11 lstat law — the link
        contributes its OWN metadata deterministically (never followed,
        never a skip event), and REMOVING it moves the token."""
        if not supports_symlink(tmp_path):
            pytest.skip("filesystem does not support symlinks")
        case = next(c for c in CORPUS if c.name == "dangling_symlink_option_file")
        lake = tmp_path / "lake"
        lake.mkdir()
        case.build(lake)
        with capture_logs() as logs:
            token = lake_content_token(lake)
        assert len(token) == 64
        assert token == lake_content_token(lake)
        # No raced-away entry here — the skip replay channel stays quiet
        # (its emission sites are unit-pinned in test_lake_heal.py).
        assert not [e for e in logs if str(e["event"]).startswith("lake_heal.token_")]
        dangling = next(p for p in lake.rglob("*.parquet") if p.is_symlink() and not p.exists())
        dangling.unlink()
        assert lake_content_token(lake) != token
