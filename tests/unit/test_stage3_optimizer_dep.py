"""Stage-3 optimizer runtime smoke (DEC-RUST-STAGE3-REFINE-001).

The Stage-3 refinement hashes the optimizer version + build digest into every
refinement-run identity, so the runtime contract is load-bearing: the
`brutex_refine` wheel's version/digest stamps, the in-repo
``EXPECTED_BUILD.json`` fingerprint (the fno_math/trading_core R2-CC-G1
pattern), and same-environment seed determinism (ADR-0007 condition 5) are
pinned here, in CI, on every push. The CI unit job installs the wheel from
the shared wheelhouse so these tests RUN rather than silently skip (the
DEC-DASHBOARD-CI-UNMASK-001 masking lesson).

Supersedes the retired pycma pin suite (DEC-STAGE3-OPTIMIZE-EXTRA-001 —
`cma==4.4.4` is deleted from the dependency tree; the exact-pin law's job is
now carried by the wheel's baked source digest, which re-keys every
refinement identity on ANY optimizer source change).

Honest envelope (Golden Rule #10 / ADR-0007 condition 5): determinism is
asserted WITHIN one environment (same process, same wheel, same platform) —
libm transcendentals are platform-rounded, so NO cross-platform bit-identity
is claimed or tested here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

brutex_refine = pytest.importorskip("brutex_refine")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXPECTED_BUILD = (
    _REPO_ROOT / "rust" / "brutex-refine" / "python" / "brutex_refine" / "EXPECTED_BUILD.json"
)


def _run(seed: int) -> tuple[list[tuple[int, int]], int]:
    """One tiny seeded refinement session via the manual ask/tell loop.

    Mirrors the locked Stage-3 shape (single-threaded, seeded, bounded
    budget) on the sphere objective over the Mode-A grid.
    """
    session = brutex_refine.RefineSession((0.5, 0.5), 0.3, seed, 40, 2, (5, 200, 5), (10, 500, 10))
    asked: list[tuple[int, int]] = []
    while not session.done():
        pairs = session.ask()
        asked.extend(pairs)
        session.tell([float((sl - 100) ** 2 + (tp - 250) ** 2) for sl, tp in pairs])
    return asked, session.restarts_run()


class TestStage3OptimizerContract:
    """The runtime facts Stage-3's reproducibility hash relies on."""

    def test_version_and_digest_match_expected_build(self) -> None:
        # The in-repo fingerprint must track the wheel surface in the SAME
        # commit (a drift here means the wheelhouse serves a stale build —
        # cached refinement-run identities would no longer match reality).
        expected = json.loads(_EXPECTED_BUILD.read_text(encoding="utf-8"))
        assert brutex_refine.__version__ == expected["crate_version"]
        assert expected["symbols"] == sorted(expected["symbols"])
        exported = sorted(
            name for name in brutex_refine._EXPECTED_COMPILED_SYMBOLS if name != "__version__"
        )
        assert exported == expected["symbols"]
        for symbol in expected["symbols"]:
            assert hasattr(brutex_refine, symbol), symbol

    def test_source_digest_is_baked_and_hex(self) -> None:
        # The refinement identity's `optimizer_build` token — 64-hex BLAKE3
        # (FRAMING v1, DEC-RUST-SOURCE-DIGEST-TWIN-001).
        digest = brutex_refine.SOURCE_DIGEST
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")
        assert brutex_refine.__source_digest__ == digest

    def test_seeded_run_is_deterministic_within_env(self) -> None:
        # ADR-0007 condition 5: same seed + same env ⇒ bit-identical
        # trajectory. Two independent sessions must agree EXACTLY (the asked
        # sequence and the restart count — the values that feed the DSR
        # trial count N).
        assert _run(seed=11) == _run(seed=11)

    def test_seed_actually_drives_the_trajectory(self) -> None:
        # A different seed must change the sampled trajectory — otherwise the
        # hash-derived per-mask seeds (condition 5) would be decorative.
        assert _run(seed=11)[0] != _run(seed=12)[0]

    def test_hard_budget_is_strict(self) -> None:
        # DEC-RUST-STAGE3-REFINE-001 closes the retired pycma overshoot
        # (measured 121 asks on a 60-ask budget): at most fevals × restarts.
        asked, restarts = _run(seed=11)
        assert restarts == 2
        assert len(asked) <= 40 * 2
