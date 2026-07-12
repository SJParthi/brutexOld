"""Doc-consistency pins for ``docs/design/sweep_core/combo_counts.md`` §3.4.

Review round 1 (DEC-SWEEP-ENUM-RUST-001, MIGRATION.md #1738) confirmed a
MAJOR defect: the first-published §3.4 term-2 table carried a uniform ×1000
unit slip (the ns/(mask·bar) coefficient applied as µs) — it printed "≈ 98 h"
per 1-minute k≤5 tuple where the document's OWN constants give 360 s ≈ 6.0
min — and its headline "COUNT-TERM dominated" conclusion INVERTS under the
correction (term 3's finalist re-simulation dominates at ``top=1000``).

These pins recompute the term-2 projections (and the term-3 O(n_bars)
scaling) from the SAME constants the doc states, then assert the printed
table rows and the dominance claim agree — a re-slip of the arithmetic, a
re-inversion of the conclusion, or a drift between the doc's constants and
its projections fails here. Golden Rule #10: the doc is the honest-capacity
record; its flagship numbers must survive independent recomputation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_DOC = Path(__file__).resolve().parents[2] / "docs/design/sweep_core/combo_counts.md"

# ---------------------------------------------------------------------------
# The doc's own constants (each asserted PRESENT in the doc before use, so
# this recomputation can never silently diverge from what the doc states).
# ---------------------------------------------------------------------------
_CF_K5 = 5_188_854  # §1 — conflict-free Σ k≤5 masks per tuple
_COEFF_MEASURED_NS = 0.125  # §3.2 row 5 — fused count coeff @k=5, ns/(mask·bar)
_COEFF_CARRIED_NS = 0.257  # §3.2 — the batch-FFI carried constant
_N_BARS_1M = 555_000  # §3.4 — 2020→2026 1-minute window
_N_BARS_15M = 37_000  # §3.4 — 2020→2026 15-minute window
_PK_TRADE_LO_S = 0.24  # §3.2 row 9 — per-kept finalist bundle @93,750 bars
_PK_TRADE_HI_S = 0.40
_PK_TRADE_BARS = 93_750
_TOP = 1_000  # the production --top
_WORKERS = 8  # the grid-wall amortisation divisor the table states


def _term2_seconds(n_bars: int, coeff_ns: float) -> float:
    """Per-tuple term-2 wall: cf masks × n_bars × coeff (the §3.1 law)."""
    return _CF_K5 * n_bars * coeff_ns * 1e-9


@pytest.fixture(scope="module")
def doc_text() -> str:
    return _DOC.read_text(encoding="utf-8")


def _table_row(doc_text: str, prefix: str) -> str:
    rows = [line for line in doc_text.splitlines() if line.startswith(prefix)]
    assert len(rows) == 1, f"expected exactly one §3.4 term-2 row starting {prefix!r}"
    return rows[0]


class TestTerm2TablePin:
    """The §3.4 term-2 rows must equal the recomputation from the doc's constants."""

    def test_doc_states_the_constants_this_pin_recomputes_from(self, doc_text: str) -> None:
        for token in (
            "5,188,854",  # cf Σk≤5 (§1 + §3.4)
            "0.125",  # measured coeff (§3.2 row 5)
            "0.257",  # carried coeff (§3.2)
            "~555,000",  # 1minute n_bars (§3.4)
            "~37,000",  # 15minute n_bars (§3.4)
            "0.24–0.40",  # PK_trade bundle (§3.2 row 9)
            "93,750",  # the PK_trade measurement's bar count
        ):
            assert token in doc_text, f"doc constant {token!r} missing — repin this test"

    def test_1minute_row_matches_recomputation(self, doc_text: str) -> None:
        row = _table_row(doc_text, "| 1minute | ~555,000 |")
        t_meas = _term2_seconds(_N_BARS_1M, _COEFF_MEASURED_NS)  # 360 s
        t_carr = _term2_seconds(_N_BARS_1M, _COEFF_CARRIED_NS)  # ~740 s
        assert f"≈ {t_meas / 60:.1f} min" in row  # ≈ 6.0 min
        assert f"≈ {t_meas / _WORKERS:.0f} s" in row  # ≈ 45 s
        assert f"≈ {t_carr / 60:.1f} min" in row  # ≈ 12.3 min
        assert f"≈ {t_carr / _WORKERS / 60:.1f} min" in row  # ≈ 1.5 min
        # The ×1000 slip printed hour-scale figures; no hour unit may return.
        assert re.search(r"\d\s*h\b", row) is None, f"hour-scale figure in {row!r}"

    def test_15minute_row_matches_recomputation(self, doc_text: str) -> None:
        row = _table_row(doc_text, "| 15minute | ~37,000 |")
        t_meas = _term2_seconds(_N_BARS_15M, _COEFF_MEASURED_NS)  # 24 s
        t_carr = _term2_seconds(_N_BARS_15M, _COEFF_CARRIED_NS)  # ~49 s
        assert f"≈ {t_meas:.0f} s" in row  # ≈ 24 s
        assert f"≈ {t_meas / _WORKERS:.0f} s" in row  # ≈ 3 s
        assert f"≈ {t_carr:.0f} s" in row  # ≈ 49 s
        assert f"≈ {t_carr / _WORKERS:.0f} s" in row  # ≈ 6 s
        assert re.search(r"\d\s*h\b", row) is None, f"hour-scale figure in {row!r}"


class TestDominanceClaimPin:
    """The corrected dominance conclusion (term 3 > term 2 at top=1000) is pinned."""

    def test_inverted_count_term_claim_is_gone(self, doc_text: str) -> None:
        norm = " ".join(doc_text.split())
        assert "COUNT-TERM dominated under EITHER" not in norm
        assert "is TERM-3 dominated, not count-term dominated" in norm

    def test_term3_scaling_matches_row9_recomputation(self, doc_text: str) -> None:
        norm = " ".join(doc_text.split())
        scale = _N_BARS_1M / _PK_TRADE_BARS
        lo_min = round(_PK_TRADE_LO_S * scale * _TOP / 60)  # 24
        hi_min = round(_PK_TRADE_HI_S * scale * _TOP / 60)  # 39
        assert f"≈ {lo_min}–{hi_min} min" in norm
        # ...and term 3 genuinely exceeds BOTH term-2 arms on the 1m tuple —
        # the arithmetic fact the corrected conclusion rests on.
        assert lo_min * 60 > _term2_seconds(_N_BARS_1M, _COEFF_CARRIED_NS)

    def test_correction_note_is_recorded_not_silent(self, doc_text: str) -> None:
        norm = " ".join(doc_text.split())
        assert "CORRECTED (review round 1, 2026-07-11)" in norm
        assert "×1000 unit slip" in norm


class TestEnumSegmentProvenancePin:
    """Round-5 review MINOR: the flagship 56.8 ns (46.9×) enum-segment rate is
    an n_bars=0 measurement (empty substrate ⇒ the kernel's min-hits-blind
    cells charging bills 0 cells/mask ⇒ max 2^20-mask ranges — the FFI floor).
    A real deep window shrinks each range to ~max_work_cells/n_bars masks and
    inflates the effective per-mask rate ≈1.3–1.5×. The doc must (a) qualify
    row 2 with that provenance, (b) carry the deep-window remeasure (row 2a),
    and (c) state in §4 caveat 2 that the enumerate+prune term is ALSO
    n_bars-sensitive — the pre-fix caveat confined n_bars sensitivity to the
    count term and the walk. Golden Rule #10.
    """

    def test_row2_carries_the_nbars0_ffi_floor_qualifier(self, doc_text: str) -> None:
        row = _table_row(doc_text, "| 2 | `c` — ranged kernel")
        assert "56.8 ns" in row and "46.9×" in row
        assert "n_bars=0 FFI floor" in row, (
            "the flagship 56.8 ns row must name its n_bars=0 provenance — an "
            "unqualified best-case figure is the round-5 defect returning"
        )

    def test_row2a_deep_window_remeasure_present_and_consistent(self, doc_text: str) -> None:
        row = _table_row(doc_text, "| 2a | `c` — ranged kernel at a DEEP window")
        # Same-process pair: the stated inflation band must bracket the
        # stated measurements' own ratio (109.2 / 72.8 = 1.50).
        assert "72.8 ns" in row and "109.2 ns" in row
        assert "≈1.3–1.5×" in row
        assert 1.3 <= 109.2 / 72.8 <= 1.5 + 1e-9
        # The masks-per-call figure must equal the charging-rule recompute:
        # a range ends after the mask that crosses max_work_cells (default
        # 2^28) at n_bars=555,000 → floor(2^28/555,000) + 1 = 484.
        assert 2**28 // 555_000 + 1 == 484
        assert "~484/call" in row

    def test_term1_table_names_the_floor_rate_it_projects_with(self, doc_text: str) -> None:
        norm = " ".join(doc_text.split())
        assert "The kernel column applies row 2's n_bars=0 FFI-floor rate" in norm

    def test_caveat2_names_the_enum_terms_nbars_sensitivity(self, doc_text: str) -> None:
        norm = " ".join(doc_text.split())
        assert "The enumerate+prune term's EFFECTIVE FFI-amortized rate is n_bars-sensitive" in norm
        assert "min-hits-blind cells charging shrinks each range" in norm
