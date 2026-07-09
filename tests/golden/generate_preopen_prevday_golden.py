"""Generate the frozen preopen prev-day goldens (DEC-PREOPEN-PREVDAY-COMMON-001).

Run ONCE from the repo root on the PRE-change tree (origin/main 63ab35f):

    python tests/golden/generate_preopen_prevday_golden.py

It builds the contiguous weekend + Holi-2024 + Budget-Saturday golden lake in
a temp dir and freezes the three artifacts
(``preopen_prevday_{day,minute}.xlsx`` + ``preopen_prevday_all.zip``) under
``tests/golden/fixtures/``. The consuming test
(``tests/golden/test_preopen_prevday_golden.py``) rebuilds the SAME lake and
asserts byte equality — so the contiguity fix is proven a pure guard (the
contiguous arm is byte-unchanged). Never regenerate these on the post-fix
tree; the pre-fix capture IS the oracle (Golden Rule #1).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.golden.test_preopen_prevday_golden import (  # noqa: E402
    FIXTURES,
    GOLDEN_DAY_XLSX,
    GOLDEN_MINUTE_XLSX,
    GOLDEN_ZIP,
    build_golden_lake,
    golden_artifacts,
)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lake = Path(tmp)
        build_golden_lake(lake)
        artifacts = golden_artifacts(lake)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for path, key in (
        (GOLDEN_DAY_XLSX, "day"),
        (GOLDEN_MINUTE_XLSX, "minute"),
        (GOLDEN_ZIP, "zip"),
    ):
        path.write_bytes(artifacts[key])
        print(f"wrote {path} ({len(artifacts[key])} bytes)")


if __name__ == "__main__":
    main()
