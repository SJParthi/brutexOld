"""Byte-equality golden for the meta-annotation pass + the frozen
scorer/abstain parity vectors (DEC-META-LABEL-ANNOTATE-001 +
DEC-ABSTAIN-GATE-STREAM-001).

* The 12-trade golden run (the shared ``_meta_golden_harness`` builder,
  written through the REAL ``write_tuple_sidecars``) is rebuilt fresh and
  annotated by the REAL :func:`annotate_run`; the produced ``.meta.json`` +
  ``meta_annotation.json`` must equal the committed fixtures BYTE-FOR-BYTE
  (schema + canonical serialization + reason vocabulary + abstain-block
  shape are all frozen; the degenerate-small capture keeps the bytes free
  of libm/BLAS floats — deliberately, so the golden is box-stable).
* The frozen scorer vectors pin the Python oracle bit-for-bit (IEEE-exact
  arm + libm-captured arm — the greeks_golden precedent) AND the compiled
  ``brutex_meta`` kernel against the SAME bits when the wheel is present.
* The frozen abstain vectors (pure comparisons — exact everywhere) pin the
  Python oracle and the Rust kernel to the SAME reason bitmasks.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any

import pytest
from brutex.meta.abstain import abstain_masks_python
from brutex.meta.annotate import annotate_run
from brutex.meta.logistic import score_logistic

from tests.golden._meta_golden_harness import (
    FIXTURE_DIRNAME,
    FIXTURE_META_NAME,
    FIXTURE_SUMMARY_NAME,
    GOLDEN_RUN_ID,
    golden_artifact_paths,
    seed_golden_run,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN_DIR = FIXTURES / FIXTURE_DIRNAME
VECTORS_PATH = FIXTURES / "meta_scorer_vectors_v1.json"


def _floats(hex_bits: list[str]) -> list[float]:
    return [struct.unpack("<d", bytes.fromhex(h))[0] for h in hex_bits]


def _float(hex_bit: str) -> float:
    return _floats([hex_bit])[0]


def _bits(values: list[float]) -> list[str]:
    return [struct.pack("<d", v).hex() for v in values]


@pytest.fixture(scope="module")
def vectors() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    return payload


class TestAnnotationBytesGolden:
    def test_fresh_annotation_equals_the_committed_fixture_bytes(self, tmp_path: Path) -> None:
        sweeps = seed_golden_run(tmp_path)
        report = annotate_run(sweeps, GOLDEN_RUN_ID)
        assert report.ok is True
        assert report.stems_annotated == 1
        meta_path, summary_path = golden_artifact_paths(tmp_path)
        assert meta_path.read_bytes() == (GOLDEN_DIR / FIXTURE_META_NAME).read_bytes()
        assert summary_path.read_bytes() == (GOLDEN_DIR / FIXTURE_SUMMARY_NAME).read_bytes()

    def test_fixture_is_float_free_canonical_json(self) -> None:
        # The frozen bytes must stay libm/BLAS-independent: every p_win is an
        # honest null (the degenerate-small design) and the serialization is
        # the canonical compact form with a trailing newline.
        raw = (GOLDEN_DIR / FIXTURE_META_NAME).read_bytes()
        assert raw.endswith(b"\n")
        payload = json.loads(raw)
        combo = payload["combos"]["combo-ffffffffff"]
        assert combo["oof"]["degenerate_reason"] == "too_few_trades"
        assert all(p is None for p in combo["p_win"].values())
        recanonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        assert raw == recanonical


class TestScorerVectorsGolden:
    # Libm-bits residual (round-1 N-10, also noted in the fixture's own
    # "note" key): the non-IEEE-exact cases freeze the GENERATING box's
    # libm exp() bit patterns under the glibc correctly-rounded-exp
    # assumption (the greeks_golden precedent — greeks_golden.json pins the
    # same class of bits for Black-76). A libm/toolchain change fails these
    # asserts LOUDLY; the remedy is a conscious, ledgered re-freeze via
    # BRUTEX_REGEN_META_GOLDEN=1 — never a tolerance widening.
    def test_python_oracle_matches_the_frozen_bits(self, vectors: dict[str, Any]) -> None:
        for case in vectors["scorer"]:
            got = score_logistic(
                _floats(case["features"]),
                case["n_features"],
                _floats(case["weights"]),
                _float(case["bias"]),
            )
            assert _bits(got) == case["expected"], case

    def test_exact_ieee_arm_reads_the_literal_values(self, vectors: dict[str, Any]) -> None:
        # The first four cases are IEEE-exact on ANY conforming box:
        # 0.5 · [1.0, 0.0] · NaN · 0.5 (signed zero).
        first_four = [_floats(case["expected"]) for case in vectors["scorer"][:4]]
        assert first_four[0] == [0.5]
        assert first_four[1] == [1.0, 0.0]
        assert math.isnan(first_four[2][0])
        assert first_four[3] == [0.5]

    def test_rust_kernel_matches_the_frozen_bits(self, vectors: dict[str, Any]) -> None:
        brutex_meta = pytest.importorskip("brutex_meta", reason="wheel not built")
        for case in vectors["scorer"]:
            got = brutex_meta.score_logistic(
                _floats(case["features"]),
                case["n_features"],
                _floats(case["weights"]),
                _float(case["bias"]),
            )
            assert _bits(list(got)) == case["expected"], case


class TestAbstainVectorsGolden:
    def _args(
        self, case: dict[str, Any]
    ) -> tuple[list[int], int, list[float], float, float, list[int], bool, list[float], float]:
        return (
            case["regimes"],
            case["allowed_mask"],
            _floats(case["vix"]),
            _float(case["lo"]),
            _float(case["hi"]),
            case["event_flags"],
            case["block_event_days"],
            _floats(case["confidence"]),
            _float(case["tau"]),
        )

    def test_python_oracle_matches_the_frozen_masks(self, vectors: dict[str, Any]) -> None:
        for case in vectors["abstain"]:
            assert abstain_masks_python(*self._args(case)) == case["expected_masks"], case

    def test_rust_kernel_matches_the_frozen_masks(self, vectors: dict[str, Any]) -> None:
        brutex_meta = pytest.importorskip("brutex_meta", reason="wheel not built")
        for case in vectors["abstain"]:
            assert list(brutex_meta.abstain_masks(*self._args(case))) == case["expected_masks"], (
                case
            )
