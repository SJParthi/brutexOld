"""Flag-ON golden arm for the cross-verify Rust seam (DEC-CROSSVERIFY-RUST-CORE).

Re-drives the SAME frozen fixtures (``tests/golden/fixtures/crossverify/``,
BYTE-UNTOUCHED — the dir is hash-pinned before/after in-test) through the
REAL export chain with ``BRUTEX_CROSSVERIFY_RUST=1``, the REAL ``brutex_ops``
wheel and the seam in the post-burn-in STEADY STATE (``remaining=0`` — the
crate bytes ship with no oracle cushion), under a crate-ENGAGEMENT spy so a
silently-degrading seam can never fake a green (the vacuous-green guard).

The flag-OFF golden suite (``test_crossverify_golden.py``) stays UNMODIFIED —
this file is the additive flag-ON twin. Skips loudly when the wheel is absent
(the main CI job never installs it; the ops-rust-parity lane does).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
from brutex.crossverify import rust_seam
from brutex.crossverify.canonical import canonical_identity, s3_key

from tests.golden._crossverify_harness import (
    FIXTURES_DIR,
    GOLDEN_DAY,
    GOLDEN_IDENTITIES,
    GOLDEN_SYMBOLS,
    S3_KEYS_FIXTURE,
    STRUCTURAL_FUT_SYMBOLS,
    build_golden_lake,
    golden_exports,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

brutex_ops = pytest.importorskip(
    "brutex_ops",
    reason="brutex_ops wheel not installed — the flag-ON golden arm runs in the "
    "ops-rust-parity lane (pip install ./rust/brutex-ops)",
)


def _fixtures_digest() -> str:
    """One order-stable digest over every committed fixture byte."""
    digest = hashlib.sha256()
    for path in sorted(FIXTURES_DIR.iterdir()):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture
def rust_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    """Arm the seam: flag ON, steady state, an engagement spy on the crate."""
    monkeypatch.setenv(rust_seam.CROSSVERIFY_RUST_ENV, "1")
    rust_seam.reset_crossverify_rust_for_tests(remaining=0)
    calls = {"render_csv": 0, "canonical": 0, "s3_key": 0}
    real_render = brutex_ops.crossverify_render_csv
    real_canonical = brutex_ops.crossverify_canonical_identity
    real_key = brutex_ops.crossverify_s3_key

    def spy_render(header: str, rows: list[list[str]]) -> object:
        calls["render_csv"] += 1
        return real_render(header, rows)

    def spy_canonical(*args: object) -> object:
        calls["canonical"] += 1
        return real_canonical(*args)

    def spy_key(*args: object) -> object:
        calls["s3_key"] += 1
        return real_key(*args)

    monkeypatch.setattr(brutex_ops, "crossverify_render_csv", spy_render)
    monkeypatch.setattr(brutex_ops, "crossverify_canonical_identity", spy_canonical)
    monkeypatch.setattr(brutex_ops, "crossverify_s3_key", spy_key)
    yield calls
    rust_seam.reset_crossverify_rust_for_tests()


class TestFlagOnGolden:
    """The frozen fixtures reproduce byte-exact through the ENGAGED crate."""

    def test_every_golden_reproduces_flag_on(self, tmp_path: Path, rust_on: dict[str, int]) -> None:
        before = _fixtures_digest()
        lake = tmp_path / "lake"
        build_golden_lake(lake)
        exports = golden_exports(lake)
        for symbol in GOLDEN_SYMBOLS + STRUCTURAL_FUT_SYMBOLS:
            export = exports[symbol]
            assert export.csv_bytes is not None, f"{symbol}: golden must render"
            fixture = (FIXTURES_DIR / f"{symbol}.csv").read_bytes()
            assert export.csv_bytes == fixture, f"{symbol}: flag-ON bytes drifted"
        # The engagement spy: a silently-degrading seam cannot fake green.
        assert rust_on["render_csv"] >= len(GOLDEN_SYMBOLS + STRUCTURAL_FUT_SYMBOLS)
        # Round-1 hostile-review MINOR-2: invocation alone is not
        # crate-byte-SHIP — a crate returning ``(None, refusal)`` for valid
        # golden rows would serve ORACLE fixture bytes and still pass the
        # byte + call-count asserts. Every refusal/degrade path records its
        # reason in the warn-once memo (``_warn_once``), so the memo holding
        # EXACTLY the engaged marker proves zero refusals/errors — the crate
        # bytes are what shipped (the house private-import test pattern).
        assert {rust_seam._ENGAGED_KEY} == rust_seam._OBSERVABILITY_MEMO, (
            f"seam degraded during the golden run: {sorted(rust_seam._OBSERVABILITY_MEMO)}"
        )
        # The mapping law flag-ON reproduces every golden identity (the
        # harness built GOLDEN_IDENTITIES at import time, flag-OFF — this is
        # the flag-ON re-map parity + the canonical engagement).
        for identity in GOLDEN_IDENTITIES.values():
            assert (
                canonical_identity(
                    exchange=identity.exchange,
                    segment=identity.lake_segment,
                    lake_symbol=identity.lake_symbol,
                )
                == identity
            )
        assert rust_on["canonical"] >= len(GOLDEN_IDENTITIES)
        # No guard/autorevert latched during the run (genuine crate bytes).
        assert rust_seam.rust_seam_disabled() is False
        # The committed fixture bytes are UNTOUCHED by the run.
        assert _fixtures_digest() == before

    def test_s3_keys_reproduce_flag_on(self, tmp_path: Path, rust_on: dict[str, int]) -> None:
        lake = tmp_path / "lake"
        build_golden_lake(lake)
        exports = golden_exports(lake)
        keys = sorted(
            s3_key(prefix="crossverify/groww", day=GOLDEN_DAY, identity=exports[s].identity)
            for s in GOLDEN_SYMBOLS
        )
        assert "\n".join(keys) + "\n" == S3_KEYS_FIXTURE.read_text(encoding="utf-8")
        assert rust_on["s3_key"] >= len(GOLDEN_SYMBOLS)
        assert rust_seam.rust_seam_disabled() is False
        # MINOR-2 twin: a clean memo (engaged marker ONLY) proves the crate
        # keys shipped — no refusal/degrade fallback served oracle strings.
        assert {rust_seam._ENGAGED_KEY} == rust_seam._OBSERVABILITY_MEMO, (
            f"seam degraded during the golden run: {sorted(rust_seam._OBSERVABILITY_MEMO)}"
        )
