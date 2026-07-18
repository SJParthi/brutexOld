"""Generate the frozen meta-annotation golden fixtures
(DEC-META-LABEL-ANNOTATE-001 + DEC-ABSTAIN-GATE-STREAM-001).

Run ONCE from the repo root:

    python tests/golden/generate_meta_annotation_golden.py

It seeds the deterministic 12-trade golden run (the shared
``_meta_golden_harness`` builder — the SAME builder the consuming test
rebuilds with) in a temp dir, runs the REAL :func:`annotate_run`, and
freezes the produced ``.meta.json`` + ``meta_annotation.json`` bytes under
``tests/golden/fixtures/meta_annotation_v1/``. It ALSO freezes the
scorer/abstain parity vectors (``meta_scorer_vectors_v1.json``): the exact
IEEE arm (0.5 / 1.0 / 0.0 / NaN) plus libm-captured sigmoid bit patterns —
the greeks_golden precedent, a libm change fails the golden LOUDLY.

Regeneration of an EXISTING fixture is REFUSED (the frozen capture is the
oracle — Golden Rule #1) unless the explicit escape hatch
``BRUTEX_REGEN_META_GOLDEN=1`` is set (a deliberate, ledger-worthy act).
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from brutex.meta.abstain import abstain_masks_python
from brutex.meta.annotate import annotate_run
from brutex.meta.logistic import score_logistic

from tests.golden._meta_golden_harness import (
    FIXTURE_DIRNAME,
    FIXTURE_META_NAME,
    FIXTURE_SUMMARY_NAME,
    golden_artifact_paths,
    seed_golden_run,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN_DIR = FIXTURES / FIXTURE_DIRNAME
VECTORS_PATH = FIXTURES / "meta_scorer_vectors_v1.json"

#: The explicit overwrite escape hatch — the frozen capture is the oracle.
REGEN_ENV = "BRUTEX_REGEN_META_GOLDEN"

NAN = float("nan")

#: Scorer cases: (features, n_features, weights, bias). The first four are
#: IEEE-EXACT on any conforming box; the rest capture THIS box's libm bits
#: (the greeks_golden precedent — deterministic, loud on a libm change).
SCORER_CASES: list[tuple[list[float], int, list[float], float]] = [
    ([0.0], 1, [1.0], 0.0),  # z = 0 → exactly 0.5
    ([1.0, -1.0], 1, [1000.0], 0.0),  # saturation → exactly 1.0 / 0.0
    ([NAN], 1, [1.0], 0.0),  # NaN propagates
    ([-0.0], 1, [5.0], 0.0),  # signed zero → exactly 0.5
    ([1.0, 1.0, 1.0], 3, [1e16, 1.0, -1e16], 0.0),  # ascending-index pin → 0.5
    ([0.5, 1.5, -2.0, 0.25], 2, [0.3, -0.7], 0.1),  # libm-dependent bits
    ([13.75, 1.0], 2, [0.0125, -0.4], 0.2),  # finance-range libm bits
]

#: Abstain cases (pure comparisons — exact everywhere):
#: (regimes, allowed_mask, vix, lo, hi, flags, block, conf, tau) → masks.
ABSTAIN_CASES: list[
    tuple[list[int], int, list[float], float, float, list[int], bool, list[float], float]
] = [
    (
        [0, 1, 2, 3],
        0b0101,
        [12.0, NAN, 40.0, 20.0],
        10.0,
        30.0,
        [0, 1, 0, 1],
        True,
        [0.9, 0.5, NAN, 0.7],
        0.7,
    ),
    ([0], 0, [NAN], NAN, NAN, [0], False, [NAN], NAN),
    ([70], 2**64 - 1, [15.0], 15.0, 15.0, [1], False, [0.7], 0.7),
]


def _hex_bits(values: list[float]) -> list[str]:
    return [struct.pack("<d", v).hex() for v in values]


def main() -> None:
    existing = [
        p
        for p in (GOLDEN_DIR / FIXTURE_META_NAME, GOLDEN_DIR / FIXTURE_SUMMARY_NAME, VECTORS_PATH)
        if p.exists()
    ]
    if existing and os.environ.get(REGEN_ENV) != "1":
        names = ", ".join(str(p) for p in existing)
        raise SystemExit(
            "REFUSED: golden fixture(s) already exist and are the frozen oracle "
            f"({names}); set {REGEN_ENV}=1 to overwrite deliberately (Golden Rule #1)."
        )
    with tempfile.TemporaryDirectory() as tmp:
        lake = Path(tmp)
        sweeps = seed_golden_run(lake)
        report = annotate_run(sweeps, "meta-golden-run")
        if not report.ok or report.stems_annotated != 1:
            raise SystemExit(f"golden annotate pass did not complete cleanly: {report}")
        meta_path, summary_path = golden_artifact_paths(lake)
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        (GOLDEN_DIR / FIXTURE_META_NAME).write_bytes(meta_path.read_bytes())
        (GOLDEN_DIR / FIXTURE_SUMMARY_NAME).write_bytes(summary_path.read_bytes())

    vectors = {
        "schema_version": 1,
        # The libm-bits residual, documented IN the fixture (round-1 N-10):
        # the non-IEEE-exact scorer cases freeze the generating box's libm
        # exp() bit patterns — glibc's correctly-rounded exp on the CI/dev
        # toolchains, the greeks_golden precedent. A libm/toolchain change
        # fails the golden LOUDLY; re-freezing is a conscious, ledgered act
        # (BRUTEX_REGEN_META_GOLDEN=1), never a silent drift.
        "note": (
            "libm-bits residual: non-IEEE-exact scorer cases freeze the "
            "generating box's libm exp() bit patterns (glibc "
            "correctly-rounded exp assumption — the greeks_golden "
            "precedent); a libm change fails this golden loudly and any "
            "re-freeze is a conscious, ledgered act."
        ),
        "scorer": [
            {
                "features": _hex_bits(features),
                "n_features": n_features,
                "weights": _hex_bits(weights),
                "bias": _hex_bits([bias])[0],
                "expected": _hex_bits(score_logistic(features, n_features, weights, bias)),
            }
            for features, n_features, weights, bias in SCORER_CASES
        ],
        "abstain": [
            {
                "regimes": regimes,
                "allowed_mask": allowed,
                "vix": _hex_bits(vix),
                "lo": _hex_bits([lo])[0],
                "hi": _hex_bits([hi])[0],
                "event_flags": flags,
                "block_event_days": block,
                "confidence": _hex_bits(conf),
                "tau": _hex_bits([tau])[0],
                "expected_masks": abstain_masks_python(
                    regimes, allowed, vix, lo, hi, flags, block, conf, tau
                ),
            }
            for regimes, allowed, vix, lo, hi, flags, block, conf, tau in ABSTAIN_CASES
        ],
    }
    VECTORS_PATH.write_text(json.dumps(vectors, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {GOLDEN_DIR / FIXTURE_META_NAME}")
    print(f"wrote {GOLDEN_DIR / FIXTURE_SUMMARY_NAME}")
    print(f"wrote {VECTORS_PATH}")


if __name__ == "__main__":
    main()
