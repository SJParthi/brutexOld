"""Unit tests for :mod:`brutex.dashboard.constituents_cache`.

DEC-CONSTITUENTS-CACHE-001 (MIGRATION.md #457). 100% line+branch on every
function in the module using:
  - ``tmp_path`` as an injected cache root (no real filesystem outside tmp);
  - a counting fake opener (never a live network call);
  - an injected fixed ``now`` datetime (fully deterministic fetched_at values).

Every branch of :func:`resolve_constituents`, :func:`read_cache`,
:func:`write_cache`, and :func:`cache_path` is exercised.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from brutex.dashboard.constituents_cache import (
    ConstituentResult,
    cache_path,
    read_cache,
    resolve_constituents,
    write_cache,
)
from brutex.dashboard.nse_constituents import ConstituentRow

# ── helpers ──────────────────────────────────────────────────────────────────

_FIXED_NOW = datetime(2026, 6, 4, 9, 15, 0, tzinfo=UTC)
_FIXED_TS = _FIXED_NOW.isoformat()  # "2026-06-04T09:15:00+00:00"
_FIXED_DATE = "2026-06-04"


def _rows(*symbols: str) -> list[ConstituentRow]:
    """Build symbol-only ConstituentRows (isin=None) for write_cache test calls."""
    return [ConstituentRow(symbol=s, isin=None) for s in symbols]


_FIXTURE_CSV = (
    "Company Name,Industry,Symbol,Series,ISIN Code\n"
    "Reliance Industries Ltd.,Refineries,RELIANCE,EQ,INE002A01018\n"
    "HDFC Bank Ltd.,Banks,HDFCBANK,EQ,INE040A01034\n"
)


class _CountingOpener:
    """Fake opener that records calls and returns a fixed CSV."""

    def __init__(self, csv_text: str = _FIXTURE_CSV) -> None:
        self._csv = csv_text
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        return self._csv


class _RaisingOpener:
    """Fake opener that raises OSError to simulate a network failure."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        raise OSError("Host not in allowlist")


# ── cache_path ────────────────────────────────────────────────────────────────


class TestCachePath:
    def test_sanitises_space(self, tmp_path: Path) -> None:
        p = cache_path(tmp_path, "NIFTY 100")
        assert p == tmp_path / "index_constituents" / "NIFTY_100.json"

    def test_sanitises_mixed_special(self, tmp_path: Path) -> None:
        # Non-alphanumerics → underscore; lowercase → upper.
        p = cache_path(tmp_path, "nifty-bank index")
        assert p.name == "NIFTY_BANK_INDEX.json"

    def test_known_index_no_special_chars(self, tmp_path: Path) -> None:
        p = cache_path(tmp_path, "NIFTY50")
        assert p == tmp_path / "index_constituents" / "NIFTY50.json"


# ── read_cache ────────────────────────────────────────────────────────────────


class TestReadCache:
    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        assert read_cache(tmp_path, "NIFTY50") is None

    def test_returns_none_on_corrupt_json(self, tmp_path: Path) -> None:
        p = cache_path(tmp_path, "NIFTY50")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not json {{{", encoding="utf-8")
        assert read_cache(tmp_path, "NIFTY50") is None

    def test_returns_none_when_not_dict(self, tmp_path: Path) -> None:
        p = cache_path(tmp_path, "NIFTY50")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert read_cache(tmp_path, "NIFTY50") is None

    def test_returns_none_on_missing_key(self, tmp_path: Path) -> None:
        p = cache_path(tmp_path, "NIFTY50")
        p.parent.mkdir(parents=True, exist_ok=True)
        # Missing "source_url"
        doc = {"index": "NIFTY50", "symbols": ["REL"], "fetched_at": _FIXED_TS}
        p.write_text(json.dumps(doc), encoding="utf-8")
        assert read_cache(tmp_path, "NIFTY50") is None

    def test_returns_none_when_symbols_not_list(self, tmp_path: Path) -> None:
        p = cache_path(tmp_path, "NIFTY50")
        p.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "index": "NIFTY50",
            "symbols": "RELIANCE",  # string, not list
            "fetched_at": _FIXED_TS,
            "source_url": "https://example.com/foo.csv",
        }
        p.write_text(json.dumps(doc), encoding="utf-8")
        assert read_cache(tmp_path, "NIFTY50") is None

    def test_returns_none_when_symbols_contains_non_str(self, tmp_path: Path) -> None:
        p = cache_path(tmp_path, "NIFTY50")
        p.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "index": "NIFTY50",
            "symbols": ["RELIANCE", 123],  # int in list
            "fetched_at": _FIXED_TS,
            "source_url": "https://example.com/foo.csv",
        }
        p.write_text(json.dumps(doc), encoding="utf-8")
        assert read_cache(tmp_path, "NIFTY50") is None

    def test_returns_doc_on_valid_file(self, tmp_path: Path) -> None:
        write_cache(
            tmp_path,
            "NIFTY50",
            _rows("RELIANCE", "HDFCBANK"),
            fetched_at=_FIXED_TS,
            source_url="https://example.com/ind_nifty50list.csv",
        )
        doc = read_cache(tmp_path, "NIFTY50")
        assert doc is not None
        assert doc["symbols"] == ["RELIANCE", "HDFCBANK"]
        assert doc["fetched_at"] == _FIXED_TS


# ── write_cache ───────────────────────────────────────────────────────────────


class TestWriteCache:
    def test_creates_parent_dirs_and_writes(self, tmp_path: Path) -> None:
        root = tmp_path / "lake"  # does not exist yet
        write_cache(
            root,
            "NIFTY 100",
            _rows("RELIANCE"),
            fetched_at=_FIXED_TS,
            source_url="https://x.com/ind_nifty100list.csv",
        )
        p = cache_path(root, "NIFTY 100")
        assert p.exists()
        doc = json.loads(p.read_text())
        assert doc["symbols"] == ["RELIANCE"]
        assert doc["fetched_at"] == _FIXED_TS

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        write_cache(
            tmp_path,
            "NIFTY50",
            _rows("OLD"),
            fetched_at="2025-01-01T00:00:00+00:00",
            source_url="u",
        )
        write_cache(tmp_path, "NIFTY50", _rows("NEW"), fetched_at=_FIXED_TS, source_url="u2")
        doc = read_cache(tmp_path, "NIFTY50")
        assert doc is not None
        assert doc["symbols"] == ["NEW"]


# ── resolve_constituents ──────────────────────────────────────────────────────


class TestResolveConstituents:
    # ── branch 1: unknown index ───────────────────────────────────────────────

    def test_unknown_index_returns_unknown(self, tmp_path: Path) -> None:
        # Only a SYMBOLLESS index (no alphanumerics → no verified mapping AND no
        # NSE filename-convention candidate) remains "unknown-index"
        # (DEC-INDEX-CONSTITUENTS-COVERAGE-001). The opener is never called.
        opener = _CountingOpener()
        result = resolve_constituents(
            "@@@",
            cache_root=tmp_path,
            opener=opener,
            now=_FIXED_NOW,
        )
        assert result == ConstituentResult((), (), "unknown-index", None)
        assert opener.calls == []

    def test_unmapped_index_resolves_via_nse_convention(self, tmp_path: Path) -> None:
        # An index with no explicit/override mapping now resolves via NSE's PUBLIC
        # filename convention (``ind_<symbol>list.csv``), fetched + cached
        # (DEC-INDEX-CONSTITUENTS-COVERAGE-001). The symbols come from NSE's
        # response (the fixture), never fabricated.
        opener = _CountingOpener()
        result = resolve_constituents(
            "NIFTYPHARMA",
            cache_root=tmp_path,
            opener=opener,
            now=_FIXED_NOW,
        )
        assert result.source == "nse"
        assert result.symbols == ("RELIANCE", "HDFCBANK")
        assert len(opener.calls) == 1
        assert opener.calls[0].endswith("/ind_niftypharmalist.csv")

    def test_symbolless_index_still_serves_stale_cache_on_force_refresh(
        self, tmp_path: Path
    ) -> None:
        # force_refresh skips the early cache return; for a symbolless index the
        # URL is None, but a prior cache is STILL served (never lost).
        write_cache(tmp_path, "@@@", _rows("RELIANCE"), fetched_at=_FIXED_TS, source_url="seed")
        opener = _CountingOpener()
        result = resolve_constituents(
            "@@@",
            cache_root=tmp_path,
            opener=opener,
            now=_FIXED_NOW,
            force_refresh=True,
        )
        assert result.source == "cache"
        assert result.symbols == ("RELIANCE",)
        assert opener.calls == []

    # ── branch 2: cache miss + fetch success ──────────────────────────────────

    def test_cache_miss_fetch_success(self, tmp_path: Path) -> None:
        opener = _CountingOpener(_FIXTURE_CSV)
        result = resolve_constituents(
            "NIFTY",
            cache_root=tmp_path,
            opener=opener,
            now=_FIXED_NOW,
        )
        assert result.source == "nse"
        assert result.as_of == _FIXED_TS
        assert "RELIANCE" in result.symbols
        assert "HDFCBANK" in result.symbols
        assert len(opener.calls) == 1
        # Cache file should now exist.
        assert cache_path(tmp_path, "NIFTY").exists()

    # ── branch 3: cache hit (force_refresh=False, cache present) ─────────────

    def test_cache_hit_returns_cache_without_calling_opener(self, tmp_path: Path) -> None:
        # Pre-seed the cache.
        write_cache(
            tmp_path,
            "NIFTY",
            _rows("RELIANCE", "HDFCBANK"),
            fetched_at=_FIXED_TS,
            source_url="https://x.com/ind_nifty50list.csv",
        )
        opener = _CountingOpener()
        result = resolve_constituents(
            "NIFTY",
            cache_root=tmp_path,
            opener=opener,
            now=_FIXED_NOW,
        )
        assert result.source == "cache"
        assert result.as_of == _FIXED_DATE
        assert set(result.symbols) == {"RELIANCE", "HDFCBANK"}
        assert opener.calls == [], "opener must NOT be called on a cache hit"

    # ── branch 4: force_refresh=True with existing cache ─────────────────────

    def test_force_refresh_ignores_cache_and_refetches(self, tmp_path: Path) -> None:
        old_ts = "2025-01-01T00:00:00+00:00"
        write_cache(
            tmp_path,
            "NIFTY",
            _rows("OLD_SYMBOL"),
            fetched_at=old_ts,
            source_url="https://x.com/ind_nifty50list.csv",
        )
        opener = _CountingOpener(_FIXTURE_CSV)
        result = resolve_constituents(
            "NIFTY",
            cache_root=tmp_path,
            opener=opener,
            now=_FIXED_NOW,
            force_refresh=True,
        )
        assert result.source == "nse"
        assert result.as_of == _FIXED_TS
        assert "RELIANCE" in result.symbols
        assert len(opener.calls) == 1
        # Cache should be refreshed.
        doc = read_cache(tmp_path, "NIFTY")
        assert doc is not None
        assert doc["fetched_at"] == _FIXED_TS

    # ── branch 5: fetch raises OSError, no prior cache ────────────────────────

    def test_fetch_oserror_no_cache_returns_unavailable(self, tmp_path: Path) -> None:
        opener = _RaisingOpener()
        result = resolve_constituents(
            "NIFTY",
            cache_root=tmp_path,
            opener=opener,
            now=_FIXED_NOW,
        )
        assert result == ConstituentResult((), (), "unavailable", None)
        assert len(opener.calls) == 1

    # ── branch 6: force_refresh + fetch raises OSError, stale cache present ───
    # Note: with force_refresh=False and a valid cache, branch 3 returns
    # immediately without calling the opener. To exercise "tried live fetch,
    # got OSError, fell back to stale" we use force_refresh=True.

    def test_force_refresh_oserror_falls_back_to_stale(self, tmp_path: Path) -> None:
        stale_ts = "2025-12-01T10:00:00+00:00"
        write_cache(
            tmp_path,
            "NIFTY",
            _rows("RELIANCE"),
            fetched_at=stale_ts,
            source_url="https://x.com/ind_nifty50list.csv",
        )
        opener = _RaisingOpener()
        result = resolve_constituents(
            "NIFTY",
            cache_root=tmp_path,
            opener=opener,
            now=_FIXED_NOW,
            force_refresh=True,
        )
        assert result.source == "cache"
        assert result.as_of == "2025-12-01"
        assert result.symbols == ("RELIANCE",)
        assert len(opener.calls) == 1

    # ── branch 7: fetch returns empty list, no prior cache ───────────────────

    def test_fetch_empty_list_no_cache_returns_unavailable(self, tmp_path: Path) -> None:
        opener = _CountingOpener("")  # empty CSV → parse_symbols → []
        result = resolve_constituents(
            "NIFTY",
            cache_root=tmp_path,
            opener=opener,
            now=_FIXED_NOW,
        )
        assert result == ConstituentResult((), (), "unavailable", None)
        assert len(opener.calls) == 1

    # ── branch 8: force_refresh + empty list, stale cache present ────────────
    # Same rationale as branch 6: use force_refresh=True to bypass branch 3.

    def test_force_refresh_empty_list_stale_cache_serves_cache(self, tmp_path: Path) -> None:
        stale_ts = "2025-11-01T08:00:00+00:00"
        write_cache(
            tmp_path,
            "NIFTY",
            _rows("HDFCBANK"),
            fetched_at=stale_ts,
            source_url="https://x.com/ind_nifty50list.csv",
        )
        opener = _CountingOpener("")  # empty CSV
        result = resolve_constituents(
            "NIFTY",
            cache_root=tmp_path,
            opener=opener,
            now=_FIXED_NOW,
            force_refresh=True,
        )
        assert result.source == "cache"
        assert result.as_of == "2025-11-01"
        assert result.symbols == ("HDFCBANK",)
        assert len(opener.calls) == 1

    # ── default now (production path) ────────────────────────────────────────

    def test_default_now_is_used_when_none(self, tmp_path: Path) -> None:
        opener = _CountingOpener(_FIXTURE_CSV)
        result = resolve_constituents("NIFTY", cache_root=tmp_path, opener=opener)
        # Should succeed and store a timestamp (not raise).
        assert result.source == "nse"
        assert result.as_of is not None


# ── ISIN-carrying rows (DEC-INDEX-CONSTITUENTS-NSE-001) ───────────────────────


class TestIsinRows:
    def test_fresh_fetch_carries_isins_in_rows(self, tmp_path: Path) -> None:
        # A live fetch parses the ISIN column → result.rows carry (symbol, isin).
        opener = _CountingOpener(_FIXTURE_CSV)
        result = resolve_constituents("NIFTY", cache_root=tmp_path, opener=opener, now=_FIXED_NOW)
        assert result.source == "nse"
        assert result.rows == (
            ConstituentRow(symbol="RELIANCE", isin="INE002A01018"),
            ConstituentRow(symbol="HDFCBANK", isin="INE040A01034"),
        )

    def test_isins_persisted_and_reloaded_from_cache(self, tmp_path: Path) -> None:
        # write_cache stores the parallel isins array; a cache hit reloads rows WITH isin.
        write_cache(
            tmp_path,
            "NIFTY",
            [
                ConstituentRow(symbol="RELIANCE", isin="INE002A01018"),
                ConstituentRow(symbol="TCS", isin=None),
            ],
            fetched_at=_FIXED_TS,
            source_url="u",
        )
        doc = read_cache(tmp_path, "NIFTY")
        assert doc is not None
        assert doc["symbols"] == ["RELIANCE", "TCS"]
        assert doc["isins"] == ["INE002A01018", None]
        result = resolve_constituents("NIFTY", cache_root=tmp_path, opener=_CountingOpener())
        assert result.source == "cache"
        assert result.rows == (
            ConstituentRow(symbol="RELIANCE", isin="INE002A01018"),
            ConstituentRow(symbol="TCS", isin=None),
        )

    def test_legacy_cache_without_isins_degrades_to_none(self, tmp_path: Path) -> None:
        # A pre-DEC-INDEX-CONSTITUENTS-NSE-001 cache file has NO "isins" key → rows
        # carry isin=None (ticker-only fallback) until the next refresh re-writes it.
        p = cache_path(tmp_path, "NIFTY")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "index": "NIFTY",
                    "symbols": ["RELIANCE", "HDFCBANK"],
                    "fetched_at": _FIXED_TS,
                    "source_url": "u",
                }
            ),
            encoding="utf-8",
        )
        result = resolve_constituents("NIFTY", cache_root=tmp_path, opener=_CountingOpener())
        assert result.source == "cache"
        assert result.rows == (
            ConstituentRow(symbol="RELIANCE", isin=None),
            ConstituentRow(symbol="HDFCBANK", isin=None),
        )

    def test_isins_length_mismatch_degrades_to_none(self, tmp_path: Path) -> None:
        # A corrupt cache whose isins array length != symbols → all ISINs degrade to None
        # (never mis-pairs a symbol with the wrong ISIN — Golden Rule #1).
        p = cache_path(tmp_path, "NIFTY")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "index": "NIFTY",
                    "symbols": ["RELIANCE", "HDFCBANK"],
                    "isins": ["INE002A01018"],
                    "fetched_at": _FIXED_TS,
                    "source_url": "u",
                }
            ),
            encoding="utf-8",
        )
        result = resolve_constituents("NIFTY", cache_root=tmp_path, opener=_CountingOpener())
        assert result.rows == (
            ConstituentRow(symbol="RELIANCE", isin=None),
            ConstituentRow(symbol="HDFCBANK", isin=None),
        )
