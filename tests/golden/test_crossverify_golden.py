"""Golden byte pins — the cross-verify CSV contract (DEC-CROSSVERIFY-PRODUCER-001).

Rebuilds the SAME deterministic lake the generator built (through the REAL
``ParquetBarSink``) and asserts the REAL export chain reproduces the frozen
fixture bytes EXACTLY — header, LF + trailing newline, no BOM, 2dp prices,
oi empty-for-spot / int-for-futures, raw-``&`` keys. Plus the NEGATIVE
golden: a 3-row truncated index day is UNPUBLISHABLE (the gate withholds —
never a 3-row file on S3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

pytest.importorskip("polars")

from brutex.crossverify.csv_render import CSV_HEADER

from tests.golden._crossverify_harness import (
    FIXTURES_DIR,
    GOLDEN_SYMBOLS,
    S3_KEYS_FIXTURE,
    STRUCTURAL_FUT_SYMBOLS,
    TRUNCATED_INDEX_SYMBOL,
    build_golden_lake,
    golden_exports,
    golden_key_lines,
)

if TYPE_CHECKING:
    from pathlib import Path

    from brutex.crossverify.day_export import SymbolExport

pytestmark = pytest.mark.golden


@pytest.fixture(scope="module")
def exports(tmp_path_factory: pytest.TempPathFactory) -> dict[str, SymbolExport]:
    lake = tmp_path_factory.mktemp("crossverify-golden-lake")
    build_golden_lake(lake)
    return golden_exports(lake)


class TestGoldenBytes:
    @pytest.mark.parametrize("symbol", GOLDEN_SYMBOLS + STRUCTURAL_FUT_SYMBOLS)
    def test_fixture_bytes_exact(self, exports: dict[str, SymbolExport], symbol: str) -> None:
        # SPOT publish goldens + the STRUCTURAL FUT renderer goldens (#1788 —
        # the FUT export chain is retained direct-covered; never producer-run).
        export = exports[symbol]
        assert export.disposition == "publish", (symbol, export.reason)
        assert export.csv_bytes is not None
        frozen = (FIXTURES_DIR / f"{symbol}.csv").read_bytes()
        assert export.csv_bytes == frozen

    def test_header_and_terminator_laws(self, exports: dict[str, SymbolExport]) -> None:
        body = exports["NIFTY"].csv_bytes
        assert body is not None
        assert body.split(b"\n", 1)[0].decode() == CSV_HEADER
        assert not body.startswith(b"\xef\xbb\xbf")  # no BOM
        assert b"\r" not in body  # LF only
        assert body.endswith(b"\n") and not body.endswith(b"\n\n")

    def test_index_files_are_the_strict_375_row_surface(
        self, exports: dict[str, SymbolExport]
    ) -> None:
        for symbol in ("NIFTY", "SENSEX"):
            assert exports[symbol].row_count == 375

    def test_cash_file_holds_missing_minute_honestly(
        self, exports: dict[str, SymbolExport]
    ) -> None:
        export = exports["RELIANCE"]
        assert export.row_count == 374  # one genuinely-absent minute — no fabrication
        assert export.csv_bytes is not None
        assert b"09:47:00" not in export.csv_bytes  # the missing minute stays missing
        assert b",0,\n" in export.csv_bytes  # the volume=0 minute IS present

    def test_futures_oi_rendered_including_zero(self, exports: dict[str, SymbolExport]) -> None:
        export = exports["NIFTY-2026-07-30-FUT"]
        assert export.csv_bytes is not None
        first_row = export.csv_bytes.split(b"\n")[1]
        assert first_row.endswith(b",0")  # oi=0 renders 0 — never blank on futures
        assert b",50001\n" in export.csv_bytes  # a real OI int rides through
        lines = export.csv_bytes.decode().strip().split("\n")[1:]
        assert all(line.rsplit(",", 1)[1] != "" for line in lines)  # never an empty oi

    def test_spot_oi_cells_are_empty(self, exports: dict[str, SymbolExport]) -> None:
        body = exports["NIFTY"].csv_bytes
        assert body is not None
        lines = body.decode().strip().split("\n")[1:]
        assert all(line.endswith(",") for line in lines)  # None → "", never a fabricated 0

    def test_s3_key_strings_pinned(self, exports: dict[str, SymbolExport]) -> None:
        assert golden_key_lines(exports) == S3_KEYS_FIXTURE.read_text(encoding="utf-8")
        keys = S3_KEYS_FIXTURE.read_text(encoding="utf-8")
        assert "crossverify/groww/2026-07-10/NSE_EQ/M&M.csv" in keys  # raw &, no %26
        assert "%26" not in keys
        # SPOT-ONLY key surface (#1788): no futures key ever pins.
        assert "_FNO" not in keys
        assert "-FUT" not in keys

    def test_negative_truncated_index_day_unpublishable(
        self, exports: dict[str, SymbolExport]
    ) -> None:
        export = exports[TRUNCATED_INDEX_SYMBOL]
        assert export.disposition == "withheld"
        assert export.reason == "count_short"
        assert export.csv_bytes is None  # NEVER a partial index file

    def test_render_is_deterministic_across_builds(self, tmp_path: Path) -> None:
        build_golden_lake(tmp_path)
        rebuilt = golden_exports(tmp_path)
        for symbol in GOLDEN_SYMBOLS + STRUCTURAL_FUT_SYMBOLS:
            frozen = (FIXTURES_DIR / f"{symbol}.csv").read_bytes()
            assert rebuilt[symbol].csv_bytes == frozen

    def test_generator_refuses_overwrite_without_escape_hatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.golden.generate_crossverify_golden import REGEN_ENV, main

        monkeypatch.delenv(REGEN_ENV, raising=False)
        with pytest.raises(SystemExit, match="REFUSED"):
            main()
