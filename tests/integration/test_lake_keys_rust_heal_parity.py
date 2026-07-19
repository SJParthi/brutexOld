"""The HEAL-FINDINGS-PARITY pin — the lake_keys Rust seam TIER GATE (§6.4).

DEC-DATA-RUST-LAKE-KEYS-001 (T10 PR-2). ``spot_key_candidates`` decides
:class:`LegacyKeyDrift` findings that ``lake_heal._heal_drift`` executes as
dir-MERGE writes (drift heals FIRST), so this pin is a TWO-IMPLEMENTATION
differential over a real-``ParquetWriter`` drifted lake (the
``test_waved_poison_corpus_differential`` shape): tier 1 scan-findings
field-for-field equality flag-OFF vs flag-ON; tier 2 ``heal_findings`` on
TWIN lakes — HealReport equality (root-relativized) + sha-identical tree
walks; tier 3 anti-vacuity (≥ 3 drift findings, the strays unflagged, and
the ON arm crate-ENGAGED — ``lake_keys_rust_disabled() is False`` + the
engaged INFO, the PR-1 golden ON-arm idiom — so a same-version STALE wheel
can never green the gate vacuously).

Wheel-gated: module-level skip when ``brutex_data`` is absent (an
OFF-vs-OFF run is vacuous) — skips in main CI, teeth in the
data-rust-parity lane.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")

if importlib.util.find_spec("brutex_data") is None:  # pragma: no cover - lane teeth
    pytest.skip(
        "brutex_data wheel not installed — the data-rust-parity lane builds it (teeth there)",
        allow_module_level=True,
    )

from brutex.data import fno_symbol_rust as fsr
from brutex.data import lake_keys_rust as lkr
from brutex.data.catalog import Catalog
from brutex.data.lake_heal import HealReport, heal_findings
from brutex.data.lake_invariants import (
    LegacyKeyDrift,
    detect_legacy_drift,
    scan_lake_invariants,
)
from brutex.data.models import CandleBar, CatalogEntry
from brutex.data.parquet_writer import ParquetWriter
from structlog.testing import capture_logs

# ── deterministic clock (quarantine stamp dirs must not differ) ────────────

_NOW = datetime(2024, 7, 1, 0, 0, 0, tzinfo=UTC)


def _now() -> datetime:
    return _NOW


def _ts(minute: int, *, day: int = 3, month: int = 6, year: int = 2024) -> datetime:
    """A UTC 1-minute instant inside the 2024-06 partition."""
    return datetime(year, month, day, 4, minute, tzinfo=UTC)


def _bar(ts: datetime, *, close: str = "100.00", volume: int = 10) -> CandleBar:
    c = Decimal(close)
    return CandleBar(
        timestamp=ts, open=c, high=c, low=c, close=c, volume=volume, open_interest=None
    )


# ── fixture: the real-writer drifted lake (design §6.4) ────────────────────

#: The three drift classes that MUST be flagged (anti-vacuity floor).
_DRIFTED: tuple[str, ...] = ("NSE-M&M", "M&M ", "M&M\x1f")
#: Must stay UNFLAGGED both arms (the genuinely-unknown-stray law).
_UNFLAGGED: tuple[str, ...] = ("ZZSTRAY", "INDIA VIX")


def _write(lake: Path, symbol: str, bars: list[CandleBar], *, interval: str = "1minute") -> None:
    ParquetWriter(lake_root=lake).write(
        exchange="NSE", segment="CASH", symbol=symbol, interval=interval, bars=bars
    )


def _drifted_lake(lake: Path) -> Path:
    """Seed a drifted CASH lake through the REAL ``ParquetWriter`` only.

    ``"M&M\\x1f"`` is writer-legal (risk #10 RESOLVED-POSITIVE: ``0x1F`` is
    not in ``_FORBIDDEN_SYMBOL_CHARS``) and ``"M&M\\x1f".isascii()`` is True,
    so flag-ON it genuinely reaches Rust — real strip-set teeth.
    """
    # Canonical M&M: ts0 + ts1 (the values that must WIN keep-first).
    _write(lake, "M&M", [_bar(_ts(0), close="100.00"), _bar(_ts(1), close="101.00")])
    # NSE-prefixed drifted twin: a conflicting duplicate ts0 + a new ts5.
    _write(
        lake,
        "NSE-M&M",
        [_bar(_ts(0), close="999.00", volume=11), _bar(_ts(5), close="77.00", volume=9)],
    )
    # Legacy DERIVED 5minute under the drifted dir — quarantine-MOVED, never merged.
    _write(lake, "NSE-M&M", [_bar(_ts(0), close="999.00", volume=20)], interval="5minute")
    # Legacy 1day — merged (the test_lake_heal drift-merge fixture mirror).
    daily_ts = datetime(2024, 6, 2, 18, 30, tzinfo=UTC)
    _write(lake, "NSE-M&M", [_bar(daily_ts, close="105.00", volume=21)], interval="1day")
    # Space-wrapped drifted dir (the strip class).
    _write(lake, "M&M ", [_bar(_ts(7), close="88.00")])
    # US-wrapped drifted dir (the 0x1F strip-set class ON DISK).
    _write(lake, "M&M\x1f", [_bar(_ts(9), close="66.00")])
    # Strays that must stay unflagged both arms.
    _write(lake, "ZZSTRAY", [_bar(_ts(2))])
    _write(lake, "INDIA VIX", [_bar(_ts(3))])
    return lake


def _symbol_dirs(lake: Path) -> dict[tuple[str, str], tuple[str, ...]]:
    cash = lake / "bars" / "NSE" / "CASH"
    return {("NSE", "CASH"): tuple(sorted(p.name for p in cash.iterdir() if p.is_dir()))}


def _tree_state(lake: Path) -> dict[str, str]:
    """``relpath → sha256`` of every file under ``bars/`` + ``.quarantine/``."""
    out: dict[str, str] = {}
    for sub in ("bars", ".quarantine"):
        root = lake / sub
        if not root.exists():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = path.relative_to(lake).as_posix()
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _normalized(report: HealReport, lake: Path) -> Any:
    """Root-relativizing normalizer: HealReport → plain data, lake root folded."""
    token = str(lake)

    def norm(value: Any) -> Any:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {f.name: norm(getattr(value, f.name)) for f in dataclasses.fields(value)}
        if isinstance(value, tuple):
            return tuple(norm(v) for v in value)
        if isinstance(value, Path):
            value = str(value)
        if isinstance(value, str):
            return value.replace(token, "<LAKE>")
        return value

    return norm(report)


class _FakeAudit:
    """A capturing audit sink — no timestamp bytes on disk."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict[str, Any] | None]] = []

    def append(self, *, action: str, subject: str, payload: dict[str, Any] | None = None) -> object:
        self.lines.append((action, subject, payload))
        return None


def _upsert_drifted_rows(catalog: Catalog) -> None:
    """Identical catalog rows per lake (the drift heal deletes legacy keys)."""
    for interval in ("1minute", "5minute", "1day"):
        catalog.upsert(
            CatalogEntry(
                exchange="NSE",
                segment="CASH",
                symbol="NSE-M&M",
                interval=interval,
                year=2024,
                month=6,
                raw_hash="0" * 64,
                row_count=1,
                first_ts=_ts(0),
                last_ts=_ts(5),
                fetched_at=_NOW,
            )
        )


# ── flag arms ──────────────────────────────────────────────────────────────


def _flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(fsr.DATA_RUST_ENV, raising=False)
    lkr.reset_lake_keys_rust_for_tests()


def _flag_on_steady(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(fsr.DATA_RUST_ENV, "1")
    lkr.reset_lake_keys_rust_for_tests(remaining=0)  # post-burn-in steady state


@pytest.fixture(autouse=True)
def _restore_seam() -> Iterator[None]:
    yield
    lkr.reset_lake_keys_rust_for_tests()


# ── the tier gate ──────────────────────────────────────────────────────────


class TestHealFindingsParity:
    def test_tier1_scan_findings_parity_off_vs_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier 1 + 3: scan findings EQUAL field-for-field, crate engaged."""
        lake = _drifted_lake(tmp_path / "lake")
        dirs = _symbol_dirs(lake)

        _flag_off(monkeypatch)
        off_direct = detect_legacy_drift(dirs, lake_root=lake)
        off_scan = scan_lake_invariants(lake)

        _flag_on_steady(monkeypatch)
        with capture_logs() as logs:
            on_direct = detect_legacy_drift(dirs, lake_root=lake)
            on_scan = scan_lake_invariants(lake)
        # Anti-vacuity (R1 INT MINOR-1): the crate genuinely ran — no stale
        # wheel latched the seam OFF into a vacuous oracle-vs-oracle green.
        assert lkr.lake_keys_rust_disabled() is False
        engaged = [e for e in logs if e["event"] == "lake_keys_rust.engaged"]
        assert len(engaged) == 1
        assert engaged[0]["crate_version"]

        # Field-for-field equality (frozen dataclasses — direct ==).
        assert on_direct == off_direct
        assert on_scan.findings == off_scan.findings
        assert on_scan.files_scanned == off_scan.files_scanned

        # Anti-vacuity floor: >= 3 drift findings, exactly the drift classes.
        drift = [f for f in off_scan.findings if isinstance(f, LegacyKeyDrift)]
        assert len(drift) >= 3
        assert {f.drifted_symbol for f in drift} == set(_DRIFTED)
        assert all(f.canonical_symbol == "M&M" for f in drift)
        assert tuple(off_direct) == tuple(drift)
        # The genuinely-unknown-stray law: strays appear in NO finding.
        for finding in off_scan.findings:
            text = repr(finding)
            for stray in _UNFLAGGED:
                assert stray not in text

    def test_tier2_heal_twin_lakes_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tier 2: twin lakes healed flag-OFF vs flag-ON land byte-identical."""
        lake_a = _drifted_lake(tmp_path / "a")
        lake_b = _drifted_lake(tmp_path / "b")
        # Twin-validity guard: the two fixtures start byte-identical.
        assert _tree_state(lake_a) == _tree_state(lake_b)

        normalized: dict[str, Any] = {}
        for lake, arm in ((lake_a, "off"), (lake_b, "on")):
            if arm == "off":
                _flag_off(monkeypatch)
            else:
                _flag_on_steady(monkeypatch)
            scan = scan_lake_invariants(lake)
            drift = [f for f in scan.findings if isinstance(f, LegacyKeyDrift)]
            assert len(drift) >= 3  # anti-vacuity per arm
            with Catalog.open(lake / "catalog.duckdb") as catalog:
                _upsert_drifted_rows(catalog)
                report = heal_findings(
                    scan.findings,
                    lake_root=lake,
                    files_scanned=scan.files_scanned,
                    mode="auto",
                    catalog=catalog,
                    audit=_FakeAudit(),
                    recall=False,
                    now=_now,
                )
            if arm == "on":
                assert lkr.lake_keys_rust_disabled() is False
            assert report.errors == 0
            assert report.refused_mass_heal is False
            normalized[arm] = _normalized(report, lake)

        # HealReport field-for-field equality (lake-root relativized).
        assert normalized["on"] == normalized["off"]
        assert normalized["off"]["healed"] >= 3

        # sha-identical tree walk across the two healed lakes.
        state_a = _tree_state(lake_a)
        state_b = _tree_state(lake_b)
        assert state_a == state_b
        assert state_a  # non-empty — the walk genuinely saw files
        # The drifted dirs merged away on BOTH lakes (heal really ran).
        for lake in (lake_a, lake_b):
            for name in _DRIFTED:
                assert not (lake / "bars" / "NSE" / "CASH" / name).exists()
            assert (lake / "bars" / "NSE" / "CASH" / "M&M").is_dir()
