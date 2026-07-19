"""Generate the frozen preopen prev-day goldens (DEC-PREOPEN-PREVDAY-COMMON-002).

Run ONCE from the repo root on the PRE-change tree (origin/main 63ab35f):

    python tests/golden/generate_preopen_prevday_golden.py

It builds the contiguous weekend + Holi-2024 + Budget-Saturday golden lake in
a temp dir and freezes the three artifacts
(``preopen_prevday_{day,minute}.xlsx`` + ``preopen_prevday_all.zip``) under
``tests/golden/fixtures/``. The consuming test
(``tests/golden/test_preopen_prevday_golden.py``) rebuilds the SAME lake and
asserts byte equality — so the contiguity fix is proven a pure guard (the
contiguous arm is byte-unchanged). The lake builder + renderers live in the
shared ``tests/golden/_preopen_prevday_harness.py`` (imported by BOTH sides,
so the generator never imports the test module — the CodeQL
``py/cyclic-import`` break, alerts 887 + 888). Never regenerate these on the post-fix
tree; the pre-fix capture IS the oracle (Golden Rule #1) — and the refusal is
now MECHANICAL (the DEC-PREOPEN-PREVDAY-COMMON-002 hostile-review fold): when
any fixture already exists, :func:`main` raises ``SystemExit`` instead of
silently overwriting the oracle, unless the explicit escape hatch
``BRUTEX_REGEN_PREOPEN_GOLDEN=1`` is set (a deliberate, ledger-worthy act,
never an accident).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.golden._preopen_prevday_harness import (
    FIXTURES,
    GOLDEN_DAY_XLSX,
    GOLDEN_MINUTE_XLSX,
    GOLDEN_ZIP,
    build_golden_lake,
    golden_artifacts,
)

#: The explicit overwrite escape hatch — the ONLY way to regenerate an
#: existing fixture (the pre-fix capture is the oracle; Golden Rule #1).
REGEN_ENV = "BRUTEX_REGEN_PREOPEN_GOLDEN"


def main() -> None:
    existing = [p for p in (GOLDEN_DAY_XLSX, GOLDEN_MINUTE_XLSX, GOLDEN_ZIP) if p.exists()]
    if existing and os.environ.get(REGEN_ENV) != "1":
        names = ", ".join(str(p) for p in existing)
        raise SystemExit(
            "REFUSED: golden fixture(s) already exist and are the PRE-fix oracle "
            f"({names}); a post-fix tree must never silently regenerate them "
            f"(Golden Rule #1). Set {REGEN_ENV}=1 to overwrite deliberately."
        )
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
