"""Generate the frozen cross-verify CSV goldens (DEC-CROSSVERIFY-PRODUCER-001).

Run ONCE from the repo root:

    python tests/golden/generate_crossverify_golden.py

Builds the deterministic golden lake in a temp dir through the REAL
``ParquetBarSink``, renders every golden symbol through the REAL export chain
(``tests/golden/_crossverify_harness.py`` — shared with the consuming test,
so the generator never imports the test module), and freezes the CSV bytes +
the pinned S3 key strings under ``tests/golden/fixtures/crossverify/``.

The frozen bytes ARE the contract oracle (Golden Rule #1): regeneration on a
changed tree is a deliberate, ledger-worthy act — when any fixture already
exists, :func:`main` raises ``SystemExit`` unless the explicit escape hatch
``BRUTEX_REGEN_CROSSVERIFY_GOLDEN=1`` is set (the preopen precedent).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.golden._crossverify_harness import (
    FIXTURES_DIR,
    GOLDEN_SYMBOLS,
    S3_KEYS_FIXTURE,
    STRUCTURAL_FUT_SYMBOLS,
    build_golden_lake,
    golden_exports,
    golden_key_lines,
)

#: The explicit overwrite escape hatch (a deliberate act, never an accident).
REGEN_ENV = "BRUTEX_REGEN_CROSSVERIFY_GOLDEN"


def main() -> None:
    """Build + freeze the golden CSVs, refusing to clobber an existing oracle."""
    all_symbols = GOLDEN_SYMBOLS + STRUCTURAL_FUT_SYMBOLS
    fixture_paths = [FIXTURES_DIR / f"{symbol}.csv" for symbol in all_symbols]
    fixture_paths.append(S3_KEYS_FIXTURE)
    existing = [p for p in fixture_paths if p.exists()]
    if existing and os.environ.get(REGEN_ENV) != "1":
        names = ", ".join(str(p) for p in existing)
        raise SystemExit(
            "REFUSED: golden fixture(s) already exist and are the byte-contract "
            f"oracle ({names}); regenerating on a changed tree must be a "
            f"deliberate act (Golden Rule #1). Set {REGEN_ENV}=1 to overwrite."
        )
    with tempfile.TemporaryDirectory() as tmp:
        lake = Path(tmp)
        build_golden_lake(lake)
        exports = golden_exports(lake)
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        # SPOT publish goldens + the STRUCTURAL FUT renderer goldens (#1788 —
        # the FUT bytes are export-chain pins only; never producer-published).
        for symbol in all_symbols:
            export = exports[symbol]
            if export.disposition != "publish" or export.csv_bytes is None:
                raise SystemExit(
                    f"golden symbol {symbol} did not render publishable bytes "
                    f"({export.disposition}: {export.reason})"
                )
            (FIXTURES_DIR / f"{symbol}.csv").write_bytes(export.csv_bytes)
            print(f"wrote {symbol}.csv ({export.row_count} rows)")
        S3_KEYS_FIXTURE.write_text(golden_key_lines(exports), encoding="utf-8")
        print(f"wrote {S3_KEYS_FIXTURE.name} (spot-only keys — #1788)")


if __name__ == "__main__":
    main()
