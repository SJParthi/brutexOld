"""Unit coverage for :mod:`brutex.data.spot_drift` (DEC-GREEKS-SPOTJOIN-STOCK-001).

100% line + branch on ``brutex/data/spot_drift.py``: the evidence scan over
real tmp lakes (junk files, broken symlinks, OSError injection), the pure
builder's verification / precedence / ordering / refusal semantics, and the
fail-soft loader over a REAL DuckDB instrument master.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from brutex.data.spot_drift import (
    _AMBIGUITY_LOG_CAP,
    _build_stock_drift_map_with_stats,
    _master_read_errors,
    _prefix_stripped_fold,
    build_stock_drift_map,
    load_stock_drift_map,
    scan_spot_symbol_dirs,
)
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Iterator

    from brutex.data.instrument_store import Instrument


def _mk_lake(tmp_path: Path, layout: dict[str, list[str]]) -> Path:
    """Build ``bars/<exch>/<seg>/<symbol>`` dirs from ``{"NSE/CASH": [...]}``."""
    lake = tmp_path / "lake"
    for exch_seg, symbols in layout.items():
        for symbol in symbols:
            (lake / "bars" / exch_seg / symbol).mkdir(parents=True, exist_ok=True)
    lake.mkdir(parents=True, exist_ok=True)
    return lake


class TestScanSpotSymbolDirs:
    def test_missing_bars_dir_is_an_empty_scan(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            assert scan_spot_symbol_dirs(tmp_path) == {}
        assert not [e for e in logs if e["event"] == "spot_drift.scan_error"]

    def test_scans_index_and_cash_unioned_and_sorted(self, tmp_path: Path) -> None:
        lake = _mk_lake(
            tmp_path,
            {
                "NSE/INDEX": ["NIFTY", "SHARED"],
                "NSE/CASH": ["BAJAJ-AUTO", "M&M", "SHARED"],
                "BSE/INDEX": ["SENSEX"],
            },
        )
        assert scan_spot_symbol_dirs(lake) == {
            "BSE": ("SENSEX",),
            "NSE": ("BAJAJ-AUTO", "M&M", "NIFTY", "SHARED"),
        }

    def test_non_spot_segments_are_ignored(self, tmp_path: Path) -> None:
        lake = _mk_lake(tmp_path, {"NSE/FNO": ["NSE-NIFTY-25Jan24-21500-CE"]})
        (lake / "bars" / "NSE" / "CASH" / "M&M").mkdir(parents=True)
        assert scan_spot_symbol_dirs(lake) == {"NSE": ("M&M",)}

    def test_junk_files_and_broken_symlinks_skipped_symlinked_dir_followed(
        self, tmp_path: Path
    ) -> None:
        lake = _mk_lake(tmp_path, {"NSE/CASH": ["REAL"]})
        cash = lake / "bars" / "NSE" / "CASH"
        (cash / "junk.txt").write_text("not a dir")
        (cash / "broken-link").symlink_to(cash / "does-not-exist")
        target = tmp_path / "elsewhere"
        target.mkdir()
        (cash / "LINKED").symlink_to(target)
        # A stray FILE at the exchange level is not an exchange dir either.
        (lake / "bars" / "README.md").write_text("stray")
        assert scan_spot_symbol_dirs(lake) == {"NSE": ("LINKED", "REAL")}

    def test_exchange_with_no_spot_symbol_dirs_is_omitted(self, tmp_path: Path) -> None:
        lake = _mk_lake(tmp_path, {"NSE/CASH": ["M&M"]})
        (lake / "bars" / "BSE" / "FNO").mkdir(parents=True)  # F&O-only exchange
        assert scan_spot_symbol_dirs(lake) == {"NSE": ("M&M",)}

    def test_segment_iterdir_oserror_skips_that_subtree_partial_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deterministic OSError injection (NOT chmod — unreliable under root
        CI): the CASH subtree raises, the INDEX subtree still contributes —
        PARTIAL evidence, one ``scan_error`` WARN, never a raise."""
        lake = _mk_lake(tmp_path, {"NSE/INDEX": ["NIFTY"], "NSE/CASH": ["M&M"]})
        real_iterdir = Path.iterdir

        def _flaky(self: Path) -> Iterator[Path]:
            if self.name == "CASH":
                raise OSError("I/O error on the CASH subtree")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", _flaky)
        with capture_logs() as logs:
            result = scan_spot_symbol_dirs(lake)
        assert result == {"NSE": ("NIFTY",)}
        errors = [e for e in logs if e["event"] == "spot_drift.scan_error"]
        assert len(errors) == 1
        assert errors[0]["error_type"] == "OSError"
        assert errors[0]["path"].endswith(os.path.join("NSE", "CASH"))

    def test_bars_iterdir_oserror_is_an_empty_scan_with_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = _mk_lake(tmp_path, {"NSE/CASH": ["M&M"]})
        real_iterdir = Path.iterdir

        def _flaky(self: Path) -> Iterator[Path]:
            if self.name == "bars":
                raise OSError("bars/ unreadable")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", _flaky)
        with capture_logs() as logs:
            assert scan_spot_symbol_dirs(lake) == {}
        errors = [e for e in logs if e["event"] == "spot_drift.scan_error"]
        assert len(errors) == 1
        assert errors[0]["path"].endswith("bars")

    @pytest.mark.parametrize("failing_dir_name", ["bars", "CASH"])
    def test_memory_error_reraises_raw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_dir_name: str
    ) -> None:
        """The honest-OOM convention: MemoryError is NEVER degraded to a WARN,
        at either iterdir site."""
        lake = _mk_lake(tmp_path, {"NSE/CASH": ["M&M"]})
        real_iterdir = Path.iterdir

        def _oom(self: Path) -> Iterator[Path]:
            if self.name == failing_dir_name:
                raise MemoryError("scan OOM")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", _oom)
        with pytest.raises(MemoryError):
            scan_spot_symbol_dirs(lake)

    def test_segment_is_dir_stat_error_skips_that_subtree_partial_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fix-round MAJOR: pathlib's ``is_dir`` swallows only
        ENOENT-class errnos and RE-RAISES EACCES/EPERM/EIO, so the seg-dir
        stat probe must degrade exactly like the iterdir sites — the INDEX
        subtree still contributes, one ``scan_error`` WARN, never a raise
        (deterministic monkeypatch injection, NOT chmod — unreliable under
        root CI)."""
        lake = _mk_lake(tmp_path, {"NSE/INDEX": ["NIFTY"], "NSE/CASH": ["M&M"]})
        real_is_dir = Path.is_dir

        def _flaky(self: Path) -> bool:
            if self.name == "CASH":
                raise PermissionError(13, "Permission denied", str(self))
            return real_is_dir(self)

        monkeypatch.setattr(Path, "is_dir", _flaky)
        with capture_logs() as logs:
            result = scan_spot_symbol_dirs(lake)
        assert result == {"NSE": ("NIFTY",)}
        errors = [e for e in logs if e["event"] == "spot_drift.scan_error"]
        assert len(errors) == 1
        assert errors[0]["error_type"] == "PermissionError"
        assert errors[0]["path"].endswith(os.path.join("NSE", "CASH"))

    def test_bars_is_dir_stat_error_is_an_empty_scan_with_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bars-level head probe rides the SAME ``_safe_is_dir`` degrade:
        an EIO stat on ``bars/`` is an empty scan + one WARN, never a raise."""
        lake = _mk_lake(tmp_path, {"NSE/CASH": ["M&M"]})
        real_is_dir = Path.is_dir

        def _flaky(self: Path) -> bool:
            if self.name == "bars":
                raise OSError(5, "Input/output error", str(self))
            return real_is_dir(self)

        monkeypatch.setattr(Path, "is_dir", _flaky)
        with capture_logs() as logs:
            assert scan_spot_symbol_dirs(lake) == {}
        errors = [e for e in logs if e["event"] == "spot_drift.scan_error"]
        assert len(errors) == 1
        assert errors[0]["path"].endswith("bars")

    @pytest.mark.parametrize("failing_dir_name", ["bars", "CASH"])
    def test_memory_error_at_is_dir_reraises_raw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_dir_name: str
    ) -> None:
        """``_safe_is_dir`` keeps the honest-OOM convention at BOTH stat sites."""
        lake = _mk_lake(tmp_path, {"NSE/CASH": ["M&M"]})
        real_is_dir = Path.is_dir

        def _oom(self: Path) -> bool:
            if self.name == failing_dir_name:
                raise MemoryError("stat OOM")
            return real_is_dir(self)

        monkeypatch.setattr(Path, "is_dir", _oom)
        with pytest.raises(MemoryError):
            scan_spot_symbol_dirs(lake)


class TestPrefixStrippedFold:
    def test_own_prefix_stripped_case_robust(self) -> None:
        assert _prefix_stripped_fold("NSE-BAJAJ-AUTO", "NSE") == "BAJAJAUTO"
        assert _prefix_stripped_fold("nse-bajaj-auto", "NSE") == "BAJAJAUTO"
        assert _prefix_stripped_fold(" NSE-M&M ", "NSE") == "M&M"

    def test_foreign_or_absent_prefix_is_none(self) -> None:
        assert _prefix_stripped_fold("BSE-M&M", "NSE") is None
        assert _prefix_stripped_fold("BAJAJ-AUTO", "NSE") is None

    def test_blank_exchange_yields_none(self) -> None:
        assert _prefix_stripped_fold("NSE-FOO", "") is None
        assert _prefix_stripped_fold("-FOO", "  ") is None

    def test_pathological_bare_prefix_dir_folds_empty(self) -> None:
        # "NSE-" strips to "" — the builder treats a falsy stripped fold as
        # unverified, so this can never bucket.
        assert _prefix_stripped_fold("NSE-", "NSE") == ""

    def test_tab_or_newline_wrapped_prefix_dir_is_none(self) -> None:
        """The fix-round docstring-honesty NIT: the strip is ASCII-space-ONLY,
        so a tab-/newline-wrapped own-prefix dir is NOT recognised — the
        ``stock_drift_fold`` "tab-bearing dir is junk → unverified" pledge
        holds on the prefix arm too."""
        assert _prefix_stripped_fold("\tNSE-FOO", "NSE") is None
        assert _prefix_stripped_fold("\nNSE-FOO", "NSE") is None
        # A TRAILING newline survives both the strip and the fold — the fold
        # result can never match a (newline-free) master, so still unverified.
        assert _prefix_stripped_fold("NSE-FOO\n", "NSE") == "FOO\n"


class TestBuildStockDriftMap:
    def test_hyphen_drift_verifies(self) -> None:
        mapping = build_stock_drift_map({"NSE": ["BAJAJ-AUTO"]}, {"NSE": ["BAJAJ-AUTO"]})
        assert mapping == {("NSE", "BAJAJAUTO"): ("BAJAJ-AUTO",)}

    def test_trailing_and_leading_space_drift_verifies(self) -> None:
        mapping = build_stock_drift_map({"NSE": ["M&M ", " M&MFIN"]}, {"NSE": ["M&M", "M&MFIN"]})
        assert mapping == {
            ("NSE", "M&M"): ("M&M ",),
            ("NSE", "M&MFIN"): (" M&MFIN",),
        }

    def test_interior_space_drift_verifies(self) -> None:
        mapping = build_stock_drift_map({"NSE": ["BAJAJ AUTO"]}, {"NSE": ["BAJAJ-AUTO"]})
        assert mapping == {("NSE", "BAJAJAUTO"): ("BAJAJ AUTO",)}

    def test_case_drift_verifies_with_literal_dir_name(self) -> None:
        mapping = build_stock_drift_map({"NSE": ["bajaj-auto"]}, {"NSE": ["BAJAJ-AUTO"]})
        assert mapping == {("NSE", "BAJAJAUTO"): ("bajaj-auto",)}

    def test_own_prefix_dir_verifies_disk_side_only(self) -> None:
        mapping = build_stock_drift_map({"NSE": ["NSE-BAJAJ-AUTO"]}, {"NSE": ["BAJAJ-AUTO"]})
        assert mapping == {("NSE", "BAJAJAUTO"): ("NSE-BAJAJ-AUTO",)}

    def test_tab_wrapped_own_prefix_dir_is_unverified(self) -> None:
        """The fix-round NIT repro pinned CLOSED: ``'\\tNSE-BAJAJ-AUTO'`` was
        mapped pre-fix (the old ``.strip()`` stripped the tab); the
        ASCII-space-only strip now leaves it unverified — the tab-junk pledge
        holds on every arm."""
        with capture_logs():
            mapping, stats = _build_stock_drift_map_with_stats(
                {"NSE": ["\tNSE-BAJAJ-AUTO"]}, {"NSE": ["BAJAJ-AUTO"]}
            )
        assert mapping == {}
        assert stats.unverified_dirs == 1

    def test_percent26_form_is_refused(self) -> None:
        """The honest-envelope refusal pin: the stored-key %26 encoding is
        UNVERIFIED (never observed on disk) — no decode arm may erode in."""
        with capture_logs():
            mapping, stats = _build_stock_drift_map_with_stats({"NSE": ["M%26M"]}, {"NSE": ["M&M"]})
        assert mapping == {}
        assert stats.unverified_dirs == 1

    def test_master_collision_poisons_fold_even_with_one_dir(self) -> None:
        """The named regression: ≥2 master symbols sharing one fold POISON it
        even when only ONE dir exists — never guess between real instruments."""
        with capture_logs() as logs:
            mapping, stats = _build_stock_drift_map_with_stats(
                {"NSE": ["TATASTEEL "]}, {"NSE": ["TATA-STEEL", "TATASTEEL"]}
            )
        assert mapping == {}
        assert stats.poisoned_dirs == 1
        assert stats.ambiguous_master_folds == 1
        warns = [e for e in logs if e["event"] == "spot_drift.ambiguous_master"]
        assert len(warns) == 1
        assert warns[0]["exchange"] == "NSE"
        assert warns[0]["fold"] == "TATASTEEL"
        assert "TATA-STEEL" in warns[0]["symbols"]

    def test_foreign_prefix_dir_is_unverified(self) -> None:
        """Mirrors the lake_keys foreign-prefix pin: the prefix strip is
        own-exchange only."""
        _mapping, stats = _build_stock_drift_map_with_stats({"NSE": ["BSE-M&M"]}, {"NSE": ["M&M"]})
        assert _mapping == {}
        assert stats.unverified_dirs == 1

    def test_multi_dir_ordering_master_exact_first_then_lexicographic(self) -> None:
        dirs = ["NSE-BAJAJ-AUTO", "BAJAJAUTO ", "BAJAJ-AUTO"]
        mapping = build_stock_drift_map({"NSE": dirs}, {"NSE": ["BAJAJ-AUTO"]})
        assert mapping == {
            ("NSE", "BAJAJAUTO"): ("BAJAJ-AUTO", "BAJAJAUTO ", "NSE-BAJAJ-AUTO"),
        }

    def test_multi_dir_without_master_exact_dir_is_lexicographic(self) -> None:
        mapping = build_stock_drift_map(
            {"NSE": ["NSE-BAJAJ-AUTO", "BAJAJAUTO "]}, {"NSE": ["BAJAJ-AUTO"]}
        )
        assert mapping == {("NSE", "BAJAJAUTO"): ("BAJAJAUTO ", "NSE-BAJAJ-AUTO")}

    def test_deterministic_under_permutation(self) -> None:
        from itertools import permutations

        dirs = ["NSE-BAJAJ-AUTO", "BAJAJAUTO ", "BAJAJ-AUTO"]
        masters = ["BAJAJ-AUTO", "M&M"]
        baseline = build_stock_drift_map({"NSE": dirs}, {"NSE": masters})
        for dir_perm in permutations(dirs):
            for master_perm in permutations(masters):
                assert (
                    build_stock_drift_map({"NSE": list(dir_perm)}, {"NSE": list(master_perm)})
                    == baseline
                )

    def test_raw_fold_precedence_over_prefix_strip(self) -> None:
        """A dir whose RAW fold matches a master maps under the raw fold ONLY —
        never falsely bridged to the prefix-stripped one."""
        mapping = build_stock_drift_map({"NSE": ["NSE-FOO"]}, {"NSE": ["NSEFOO", "FOO"]})
        assert mapping == {("NSE", "NSEFOO"): ("NSE-FOO",)}

    def test_raw_fold_poisoned_never_falls_through_to_stripped(self) -> None:
        """A poisoned raw interpretation is FINAL — the dir is refused, not
        re-interpreted under its prefix-stripped fold."""
        with capture_logs():
            mapping, stats = _build_stock_drift_map_with_stats(
                {"NSE": ["NSE-FOO"]}, {"NSE": ["NSEFOO", "NSE FOO", "FOO"]}
            )
        assert mapping == {}  # "FOO" would have matched had it fallen through
        assert stats.poisoned_dirs == 1

    def test_junk_dir_matching_no_master_is_unverified(self) -> None:
        _mapping, stats = _build_stock_drift_map_with_stats(
            {"NSE": ["GHOST", "NSE-"]}, {"NSE": ["M&M"]}
        )
        assert _mapping == {}
        assert stats.unverified_dirs == 2

    def test_blank_folding_master_is_skipped(self) -> None:
        """A master symbol folding to the empty string never indexes the
        vocabulary (an empty fold can never verify a dir)."""
        mapping = build_stock_drift_map({"NSE": ["-"]}, {"NSE": ["-", " "]})
        assert mapping == {}

    def test_identity_shaped_entry_is_kept(self) -> None:
        mapping = build_stock_drift_map({"NSE": ["RELIANCE"]}, {"NSE": ["RELIANCE"]})
        assert mapping == {("NSE", "RELIANCE"): ("RELIANCE",)}

    def test_cross_exchange_isolation(self) -> None:
        """An NSE dir never verifies against a BSE master (and a weird-cased
        exchange dir matches no master group → all its dirs unverified)."""
        mapping, stats = _build_stock_drift_map_with_stats(
            {"NSE": ["BAJAJ-AUTO"], "nse": ["BAJAJ-AUTO"]}, {"BSE": ["BAJAJ-AUTO"]}
        )
        assert mapping == {}
        assert stats.unverified_dirs == 2

    def test_duplicate_input_dirs_are_deduped(self) -> None:
        mapping, stats = _build_stock_drift_map_with_stats(
            {"NSE": ["BAJAJ-AUTO", "BAJAJ-AUTO"]}, {"NSE": ["BAJAJ-AUTO"]}
        )
        assert mapping == {("NSE", "BAJAJAUTO"): ("BAJAJ-AUTO",)}
        assert stats.dirs_scanned == 1

    def test_empty_inputs(self) -> None:
        assert build_stock_drift_map({}, {}) == {}
        assert build_stock_drift_map({}, {"NSE": ["M&M"]}) == {}
        # A master with NO dir contributes no entry — vocabulary without
        # evidence is exactly as unmapped as evidence without vocabulary.
        assert build_stock_drift_map({"NSE": []}, {"NSE": ["M&M"]}) == {}

    def test_ambiguity_warns_capped_full_count_in_stats(self) -> None:
        """Cap + 1 poisoned folds → exactly _AMBIGUITY_LOG_CAP warns, the FULL
        count on the stats (→ the loader's map_built)."""
        n = _AMBIGUITY_LOG_CAP + 1
        masters = []
        for i in range(n):
            masters.extend([f"SYM{i:03d}-A", f"SYM{i:03d}A"])  # every pair collides
        with capture_logs() as logs:
            _mapping, stats = _build_stock_drift_map_with_stats({"NSE": []}, {"NSE": masters})
        warns = [e for e in logs if e["event"] == "spot_drift.ambiguous_master"]
        assert len(warns) == _AMBIGUITY_LOG_CAP
        assert stats.ambiguous_master_folds == n


def _eq_instrument(symbol: str) -> Instrument:
    """A minimal NSE cash-equity master row (the test_universe_explorer_store
    ``_eq`` fixture pattern)."""
    from brutex.data.instrument_store import Instrument
    from brutex.models import Exchange, InstrumentType, Segment

    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
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


def _seed_master(lake: Path, *symbols: str) -> None:
    """Write a REAL DuckDB instrument master at ``<lake>/instruments.duckdb``."""
    from brutex.data.instrument_store import InstrumentStore

    lake.mkdir(parents=True, exist_ok=True)
    store = InstrumentStore.open(lake / "instruments.duckdb")
    try:
        if symbols:
            store.upsert_many(
                [_eq_instrument(s) for s in symbols],
                snapshot_date=date(2024, 1, 1),
                run_id="test-spot-drift",
                csv_source_sha256="0" * 64,
            )
    finally:
        store.close()


class TestLoadStockDriftMap:
    def test_absent_master_returns_none_with_debug(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            assert load_stock_drift_map(tmp_path) is None
        events = [e for e in logs if e["event"] == "spot_drift.no_master"]
        assert len(events) == 1
        assert events[0]["log_level"] == "debug"

    def test_master_exists_stat_error_degrades_to_none_with_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fix-round MAJOR, loader arm: pathlib's ``exists`` re-raises a
        non-ENOENT stat errno (EACCES on the master file), which must degrade
        to the SAME honest ``master_error`` → ``None`` as a store-read error —
        never a raise out to the #1188 fence."""
        real_exists = Path.exists

        def _flaky(self: Path, **kwargs: bool) -> bool:
            if self.name == "instruments.duckdb":
                raise PermissionError(13, "Permission denied", str(self))
            return real_exists(self, **kwargs)

        monkeypatch.setattr(Path, "exists", _flaky)
        with capture_logs() as logs:
            assert load_stock_drift_map(tmp_path) is None
        errors = [e for e in logs if e["event"] == "spot_drift.master_error"]
        assert len(errors) == 1
        assert errors[0]["error_type"] == "PermissionError"
        # ENOENT (a genuinely absent master) keeps its OWN no_master branch.
        assert not [e for e in logs if e["event"] == "spot_drift.no_master"]

    def test_master_exists_memory_error_reraises_raw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_exists = Path.exists

        def _oom(self: Path, **kwargs: bool) -> bool:
            if self.name == "instruments.duckdb":
                raise MemoryError("stat OOM")
            return real_exists(self, **kwargs)

        monkeypatch.setattr(Path, "exists", _oom)
        with pytest.raises(MemoryError):
            load_stock_drift_map(tmp_path)

    def test_real_master_and_lake_builds_verified_map(self, tmp_path: Path) -> None:
        pytest.importorskip("duckdb")
        lake = _mk_lake(tmp_path, {"NSE/CASH": ["BAJAJ-AUTO", "GHOST"]})
        _seed_master(lake, "BAJAJ-AUTO")
        with capture_logs() as logs:
            mapping = load_stock_drift_map(lake)
        assert mapping == {("NSE", "BAJAJAUTO"): ("BAJAJ-AUTO",)}
        built = [e for e in logs if e["event"] == "spot_drift.map_built"]
        assert len(built) == 1
        assert built[0]["exchanges"] == 1
        assert built[0]["dirs_scanned"] == 2
        assert built[0]["masters"] == 1
        assert built[0]["entries"] == 1
        assert built[0]["multi_dir_entries"] == 0
        assert built[0]["unverified_dirs"] == 1
        assert built[0]["poisoned_dirs"] == 0
        assert built[0]["ambiguous_master_folds"] == 0

    def test_multi_dir_entry_counted_in_map_built(self, tmp_path: Path) -> None:
        pytest.importorskip("duckdb")
        lake = _mk_lake(tmp_path, {"NSE/CASH": ["BAJAJ-AUTO", "NSE-BAJAJ-AUTO"]})
        _seed_master(lake, "BAJAJ-AUTO")
        with capture_logs() as logs:
            mapping = load_stock_drift_map(lake)
        assert mapping == {("NSE", "BAJAJAUTO"): ("BAJAJ-AUTO", "NSE-BAJAJ-AUTO")}
        built = [e for e in logs if e["event"] == "spot_drift.map_built"]
        assert built[0]["multi_dir_entries"] == 1

    def test_empty_master_short_circuits_without_lake_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("duckdb")
        lake = _mk_lake(tmp_path, {"NSE/CASH": ["BAJAJ-AUTO"]})
        _seed_master(lake)  # zero rows

        def _must_not_scan(_lake_root: Path) -> dict[str, tuple[str, ...]]:
            raise AssertionError("the empty-master short-circuit must not walk the lake")

        import brutex.data.spot_drift as spot_drift_module

        monkeypatch.setattr(spot_drift_module, "scan_spot_symbol_dirs", _must_not_scan)
        with capture_logs() as logs:
            assert load_stock_drift_map(lake) == {}
        built = [e for e in logs if e["event"] == "spot_drift.map_built"]
        assert len(built) == 1
        assert built[0]["masters"] == 0
        assert built[0]["dirs_scanned"] == 0
        assert built[0]["entries"] == 0

    def test_confirmed_store_error_classes_degrade_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("duckdb")
        import duckdb
        from brutex.data import instrument_store
        from brutex.data.errors import CatalogError

        lake = _mk_lake(tmp_path, {"NSE/CASH": ["BAJAJ-AUTO"]})
        _seed_master(lake, "BAJAJ-AUTO")
        for exc in (
            CatalogError("store wedged"),
            duckdb.Error("catalog corrupt"),
            OSError("read-only parent"),
        ):

            def _raise(_path: Path, _exc: Exception = exc) -> object:
                raise _exc

            monkeypatch.setattr(instrument_store, "open_instrument_store", _raise)
            with capture_logs() as logs:
                assert load_stock_drift_map(lake) is None
            errors = [e for e in logs if e["event"] == "spot_drift.master_error"]
            assert len(errors) == 1
            assert errors[0]["error_type"] == type(exc).__name__

    def test_memory_error_from_store_read_reraises_raw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("duckdb")
        from brutex.data import instrument_store

        lake = _mk_lake(tmp_path, {"NSE/CASH": ["BAJAJ-AUTO"]})
        _seed_master(lake, "BAJAJ-AUTO")

        def _oom(_path: Path) -> object:
            raise MemoryError("store read OOM")

        monkeypatch.setattr(instrument_store, "open_instrument_store", _oom)
        with pytest.raises(MemoryError):
            load_stock_drift_map(lake)

    def test_unexpected_defect_propagates_to_the_fence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NO blanket ``except Exception``: a genuine code bug propagates to
        the #1188 ``fno.greeks_enrich.error`` fence instead of a silent None."""
        pytest.importorskip("duckdb")
        from brutex.data import instrument_store

        lake = _mk_lake(tmp_path, {"NSE/CASH": ["BAJAJ-AUTO"]})
        _seed_master(lake, "BAJAJ-AUTO")

        def _bug(_path: Path) -> object:
            raise ValueError("a genuine defect")

        monkeypatch.setattr(instrument_store, "open_instrument_store", _bug)
        with pytest.raises(ValueError, match="a genuine defect"):
            load_stock_drift_map(lake)


class TestMasterReadErrors:
    def test_confirmed_classes_with_duckdb_installed(self) -> None:
        duckdb = pytest.importorskip("duckdb")
        from brutex.data.errors import CatalogError

        assert _master_read_errors() == (CatalogError, OSError, duckdb.Error)

    def test_without_duckdb_the_tuple_is_complete_without_its_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When duckdb is not importable the store's own ``open`` raises
        CatalogError before any duckdb.Error could exist."""
        import sys

        from brutex.data.errors import CatalogError

        monkeypatch.setitem(sys.modules, "duckdb", None)
        assert _master_read_errors() == (CatalogError, OSError)


class TestMasterDbNameConstantPin:
    def test_master_db_name_mirrors_the_store_constant(self) -> None:
        """The lazy-import twin pin: ``spot_drift._MASTER_DB_NAME`` must equal
        ``instrument_store.DEFAULT_DB_NAME`` (the module deliberately does not
        import the heavy store at module level)."""
        from brutex.data.instrument_store import DEFAULT_DB_NAME
        from brutex.data.spot_drift import _MASTER_DB_NAME

        assert _MASTER_DB_NAME == DEFAULT_DB_NAME
