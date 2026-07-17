"""Tests for the vocabulary codegen (DEC-VOCAB-U128-CODEGEN-001).

``scripts/codegen_vocab.py`` is the ONE generator that emits BOTH ``bits.rs`` and
``bits.py`` from ``rust/trading-core/conditions.toml``. These tests pin:

* the manifest invariants (dense bits, no dup name, contiguous groups, mask_width);
* the rendered Rust + Python carry the ``@generated`` banner and the expected
  constants / counts;
* ``main(['--check'])`` returns 0 when up-to-date and 1 on drift (both branches);
* the committed ``bits.rs`` / ``bits.py`` match a fresh render (no drift in-repo).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover — path may already be present
    sys.path.insert(0, str(_REPO_ROOT))

from scripts import codegen_vocab as cg  # noqa: E402  (needs the path shim above)


def _manifest() -> cg.Manifest:
    return cg.load_manifest()


def test_manifest_loads_and_is_valid() -> None:
    m = _manifest()
    assert m.mask_width == 128
    assert m.total_conditions == 66
    # Bits are dense 0..65.
    assert sorted(c.bit for c in m.conditions) == list(range(66))
    # IMPLEMENTED_MASK = bits 0..51 + 54..65: the VWAP pair 52/53 is TOMBSTONED
    # (the VWAP/volume wipe — reserved=true, implemented=false, positions held
    # forever); CPR (60..63) and — since the vocab-66 implemented-flip, crate
    # 0.5.0 — the Supertrend pair (64/65) are implemented.
    assert m.implemented_mask == ((1 << 66) - 1) ^ (1 << 52) ^ (1 << 53)
    # RETIRED_MASK = exactly the tombstoned VWAP pair, disjoint from IMPLEMENTED.
    assert m.retired_mask == (1 << 52) | (1 << 53)
    assert m.retired_mask & m.implemented_mask == 0
    for c in m.conditions:
        if 54 <= c.bit <= 63:
            assert c.implemented is True
    # The tombstoned VWAP bits: reserved, unimplemented, positions kept.
    vwap = {c.name: c for c in m.conditions if c.group == "VWAP Position"}
    assert vwap["close_above_vwap"].bit == 52
    assert vwap["close_below_vwap"].bit == 53
    for c in vwap.values():
        assert c.reserved is True
        assert c.implemented is False
        assert c.applies_to_1day is False
    # The Supertrend bits: IMPLEMENTED since the vocab-66 flip (crate
    # 0.5.0), never reserved, 1day-applicable from declaration.
    st = {c.name: c for c in m.conditions if c.group == "Supertrend"}
    assert st["close_above_supertrend"].bit == 64
    assert st["close_below_supertrend"].bit == 65
    for c in st.values():
        assert c.reserved is False
        assert c.implemented is True
        assert c.applies_to_1day is True
    # CPR occupies 60..63, all implemented + 1day-applicable.
    cpr = {c.name: c for c in m.conditions if c.group == "Central Pivot Range"}
    assert {c.bit for c in cpr.values()} == {60, 61, 62, 63}
    assert all(c.implemented and c.applies_to_1day for c in cpr.values())


def test_applies_1day_mask_excludes_vwap_and_time_of_day() -> None:
    m = _manifest()
    name_to_bit = {c.name: c.bit for c in m.conditions}
    # VWAP + the four time-of-day bits are NOT 1day-applicable.
    for name in ("close_above_vwap", "early_morning", "afternoon"):
        assert (m.applies_1day_mask >> name_to_bit[name]) & 1 == 0
    # A plain EMA condition IS 1day-applicable.
    assert (m.applies_1day_mask >> name_to_bit["close_above_ema20"]) & 1 == 1


def test_manifest_rejects_non_128_mask_width(tmp_path: Path) -> None:
    """``load_manifest`` rejects a manifest whose ``mask_width`` is not 128."""
    bad_toml = tmp_path / "conditions.toml"
    bad_toml.write_text(
        "schema_version = 2\nmask_width = 64\n"
        '[[condition]]\nbit = 0\nname = "a"\ngroup = "G"\n'
        'description = "d"\napplies_to_1day = true\nimplemented = true\n',
        encoding="utf-8",
    )
    with pytest.raises(cg.ManifestError, match="mask_width must be 128"):
        cg.load_manifest(bad_toml)


def test_manifest_rejects_non_dense_bits() -> None:
    bad = (
        cg.Condition(0, "a", "G", "d", False, True, True),
        cg.Condition(2, "b", "G", "d", False, True, True),  # gap at 1
    )
    with pytest.raises(cg.ManifestError, match="dense"):
        cg._validate_manifest(bad, 128)


def test_manifest_rejects_duplicate_names() -> None:
    bad = (
        cg.Condition(0, "dup", "G", "d", False, True, True),
        cg.Condition(1, "dup", "G", "d", False, True, True),
    )
    with pytest.raises(cg.ManifestError, match="duplicate condition name"):
        cg._validate_manifest(bad, 128)


def test_manifest_rejects_bit_above_mask_width() -> None:
    # Dense-but-shifted is impossible, so build a single bit at >= width with a
    # matching dense range is contradictory; instead test the width guard directly
    # with a 2-bit dense set then a tiny width.
    bad = (
        cg.Condition(0, "a", "G", "d", False, True, True),
        cg.Condition(1, "b", "G", "d", False, True, True),
    )
    with pytest.raises(cg.ManifestError, match="exceeds mask_width"):
        cg._validate_manifest(bad, 1)


def test_manifest_rejects_non_contiguous_group() -> None:
    bad = (
        cg.Condition(0, "a", "GA", "d", False, True, True),
        cg.Condition(1, "b", "GB", "d", False, True, True),
        cg.Condition(2, "c", "GA", "d", False, True, True),  # GA split by GB
    )
    with pytest.raises(cg.ManifestError, match="not contiguous"):
        cg._validate_manifest(bad, 128)


def test_render_rust_has_banner_and_constants() -> None:
    rs = cg.render_rust(_manifest())
    assert rs.startswith("// @generated by scripts/codegen_vocab.py")
    assert "pub const BIT_CLOSE_ABOVE_VWAP: u32 = 52;" in rs
    assert "pub const BIT_ABOVE_CPR_TC: u32 = 60;" in rs
    assert "pub const BIT_CLOSE_ABOVE_SUPERTREND: u32 = 64;" in rs
    assert "pub const TOTAL_CONDITIONS: u32 = 66;" in rs
    assert "pub const MASK_WIDTH: u32 = 128;" in rs
    assert "pub const IMPLEMENTED_MASK: u128 = 73773465495956094975;" in rs
    # TOMB-04: the RETIRED tombstones render distinctly; NO LAYOUT-ONLY bit
    # exists on today's manifest (the SuperTrend pair left that class at the
    # vocab-66 implemented-flip), so the layout-only suffix renders nowhere.
    assert "(RETIRED — reserved=true, position held forever, never gets math)" in rs
    assert "(LAYOUT ONLY — implemented=false, math lands in a later PR)" not in rs
    assert "fn bit_positions_are_dense()" in rs


def test_render_python_has_banner_and_constants() -> None:
    py = cg.render_python(_manifest())
    assert "# @generated by scripts/codegen_vocab.py" in py
    assert "BIT_CLOSE_ABOVE_VWAP: Final[int] = 52" in py
    assert "BIT_NARROW_CPR_DAY: Final[int] = 63" in py
    assert "TOTAL_CONDITIONS: Final[int] = 66" in py
    assert "MASK_WIDTH: Final[int] = 128" in py
    assert "IMPLEMENTED_MASK: Final[int] = 73773465495956094975" in py
    # TOMB-01: the tombstone mask is emitted so renderers can suffix retired
    # bits without a hand-kept copy (0x30000000000000 = bits 52|53).
    assert f"RETIRED_MASK: Final[int] = {(1 << 52) | (1 << 53)}" in py
    # TOMB-04: RETIRED renders distinctly; no LAYOUT-ONLY bit exists on
    # today's manifest (the vocab-66 SuperTrend implemented-flip).
    assert "RETIRED (tombstone, never gets math): bit 52" in py
    assert "LAYOUT-ONLY (math in a later PR): bit 64" not in py
    assert '"close_above_vwap": BIT_CLOSE_ABOVE_VWAP,' in py


def test_render_python_exports_public_masks_in_all() -> None:
    """The codegen-emitted masks are part of the module's public ``__all__``.

    ``IMPLEMENTED_MASK`` / ``APPLIES_TO_1DAY_MASK`` are public API for downstream
    (PR-2) consumers, so they must be declared in ``__all__`` — otherwise a static
    analyser flags them as unused module-level globals (DEC-VOCAB-U128-CODEGEN-001).
    """
    py = cg.render_python(_manifest())
    assert "__all__ = [" in py
    for name in (
        "IMPLEMENTED_MASK",
        "APPLIES_TO_1DAY_MASK",
        "RETIRED_MASK",
        "BIT_CLOSE_ABOVE_VWAP",
    ):
        assert f'    "{name}",' in py
    # The ``__all__`` block carries ruff's RUF022 isort-style natural sort (so
    # ``ruff check brutex`` stays green): FIB_0 < FIB_50 < FIB_100 < FIB_200.
    fib0 = py.index('"BIT_NEAR_FIB_0",')
    fib100 = py.index('"BIT_NEAR_FIB_100",')
    fib200 = py.index('"BIT_NEAR_FIB_200",')
    assert fib0 < fib100 < fib200


def test_committed_artifacts_match_fresh_render() -> None:
    """The in-repo bits.rs / bits.py have NO drift from the manifest."""
    assert cg.check_artifacts(_manifest()) == []


def test_check_mode_returns_zero_when_up_to_date(capsys: pytest.CaptureFixture[str]) -> None:
    assert cg.main(["--check"]) == 0
    assert "up-to-date" in capsys.readouterr().out


def test_check_mode_returns_one_on_drift(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--check`` exits 1 + names the stale file when a render would change."""
    # Force a drift by making render_python emit something different.
    monkeypatch.setattr(cg, "render_python", lambda _m: "DRIFTED\n")
    assert cg.main(["--check"]) == 1
    err = capsys.readouterr().err
    assert "STALE" in err
    assert "bits.py" in err


def test_write_artifacts_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``write_artifacts`` then ``check_artifacts`` reports no drift (round-trip)."""
    rs_dst = tmp_path / "bits.rs"
    py_dst = tmp_path / "bits.py"
    monkeypatch.setattr(cg, "RUST_BITS", rs_dst)
    monkeypatch.setattr(cg, "PY_BITS", py_dst)
    m = _manifest()
    cg.write_artifacts(m)
    assert rs_dst.exists()
    assert py_dst.exists()
    assert cg.check_artifacts(m) == []


def test_main_write_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main([])`` (no --check) writes both files and reports the count."""
    # Point ROOT + both artifacts at tmp so the `.relative_to(ROOT)` print works.
    monkeypatch.setattr(cg, "ROOT", tmp_path)
    monkeypatch.setattr(cg, "RUST_BITS", tmp_path / "bits.rs")
    monkeypatch.setattr(cg, "PY_BITS", tmp_path / "bits.py")
    assert cg.main([]) == 0
    out = capsys.readouterr().out
    assert "66 conditions" in out
    assert (tmp_path / "bits.rs").exists()
    assert (tmp_path / "bits.py").exists()
