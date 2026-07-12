"""Hypothesis property suite for the stage-stamp law (R42, I-5).

Two invariants over arbitrary artifact bytes:

1. **Roundtrip.** stamp → validate is ``OK`` for any content, stage,
   run id, and extra map — and the parsed stamp round-trips its fields.
2. **Single-byte sensitivity.** Flipping ANY single byte of the artifact
   (any index, any non-zero XOR delta) after stamping yields
   ``HASH_MISMATCH`` — the always-recompute SHA-256 can never be fooled by
   a same-length corruption (torn/foreign bytes are strictly harder cases).

Per-example temp dirs come from ``tempfile.TemporaryDirectory`` (the house
property-test idiom — Hypothesis forbids function-scoped ``tmp_path``).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Final

import pytest
from brutex.observability.stage_stamp import (
    StampVerdict,
    read_stage_stamp,
    validate_stage_stamp,
    write_stage_stamp,
)
from hypothesis import given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.property

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r42_stamp_validate_roundtrip_ok": ("R42",),
    "test_r42_any_single_byte_flip_is_hash_mismatch": ("R42",),
}

_RUN_ID: Final[str] = "77" * 32

_content = st.binary(min_size=0, max_size=512)
_stage = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=16
)
_extra = st.dictionaries(
    keys=st.text(
        alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=8
    ),
    values=st.text(max_size=16),
    max_size=3,
)


class TestStampRoundtrip:
    @settings(max_examples=100, deadline=None)
    @given(content=_content, stage=_stage, extra=_extra)
    def test_r42_stamp_validate_roundtrip_ok(
        self, content: bytes, stage: str, extra: dict[str, str]
    ) -> None:
        """Any stamped artifact validates OK and the stamp round-trips."""
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "artifact.bin"
            artifact.write_bytes(content)
            write_stage_stamp(artifact, stage=stage, run_id=_RUN_ID, extra=extra)
            verdict, stamp = validate_stage_stamp(
                artifact, expected_stage=stage, expected_run_id=_RUN_ID
            )
            assert verdict is StampVerdict.OK
            assert stamp is not None
            assert stamp.stage == stage
            assert stamp.run_id == _RUN_ID
            assert stamp.extra == extra
            assert read_stage_stamp(artifact) == stamp

    @settings(max_examples=100, deadline=None)
    @given(content=st.binary(min_size=1, max_size=512), data=st.data())
    def test_r42_any_single_byte_flip_is_hash_mismatch(
        self, content: bytes, data: st.DataObject
    ) -> None:
        """Flipping ONE byte anywhere in the artifact after stamping refuses."""
        index = data.draw(st.integers(min_value=0, max_value=len(content) - 1), label="index")
        delta = data.draw(st.integers(min_value=1, max_value=255), label="xor_delta")
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "artifact.bin"
            artifact.write_bytes(content)
            write_stage_stamp(artifact, stage="stage3_cell", run_id=_RUN_ID)
            flipped = bytearray(content)
            flipped[index] ^= delta
            artifact.write_bytes(bytes(flipped))
            verdict, stamp = validate_stage_stamp(artifact, expected_stage="stage3_cell")
            assert verdict is StampVerdict.HASH_MISMATCH
            assert stamp is not None  # forensics: the stamp itself is intact
