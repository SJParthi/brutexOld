"""Every O(1) CLAIM in the audit must name a bench gate that EXISTS.

DEC-O1-CLAIM-BACKING-001.

THE PROBLEM THIS SOLVES. ``docs/O1_GUARANTEE_AUDIT.md`` is the authoritative
per-path complexity record (DEC-O1-GUARANTEE-HARDEN-001, MIGRATION.md
#1791). It is prose. Prose cannot fail CI. So the honest status quo was:
"we claim O(1) here, and we believe a bench covers it" — believable only to
someone who already trusts the author.

This module converts every claim into a MECHANICAL OBLIGATION:

* a row whose honest class contains ``O(1)`` MUST cite at least one bench
  file in its "Bench gate" column, and every cited file MUST exist;
* a row that cites ``none`` MUST appear in :data:`NO_BENCH_REASONS` with a
  written reason — silence is not an option;
* the parser itself is anti-vacuously pinned, so a table rename or a
  markdown reshape can never turn this gate into a green no-op.

WHAT IT DOES NOT DO (stated so the guarantee is not oversold). This proves
the claim is BACKED, not that the backing is CORRECT — a cited bench could
in principle be weak. Bench STRENGTH is the separate two-tier ratio gate of
CLAUDE.md §2 Rule 5 (``tests/bench/_o1_tiers.py``) plus each bench's own
anti-vacuous teeth. The three layers compose:

    1. this gate    — every O(1) claim cites a live bench      (claim -> test)
    2. the bench    — the ratio is measured, not asserted      (test -> number)
    3. the tier     — the number must hold under a ceiling     (number -> gate)

Break any one and the chain is a promise again. This module is layer 1.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
AUDIT_DOC: Final[Path] = REPO_ROOT / "docs" / "O1_GUARANTEE_AUDIT.md"
TESTS_ROOT: Final[Path] = REPO_ROOT / "tests"
BENCH_DIR: Final[Path] = TESTS_ROOT / "bench"

#: Where a cited test file may legitimately live. The audit cites two
#: DIFFERENT strengths of backing and is honest about which is which:
#: a bench MEASURES a ratio under a ceiling, while a unit-level "source pin"
#: only proves a shape holds. Both are real citations, so both must RESOLVE
#: — but only the bench tree satisfies an O(1) OBLIGATION (see
#: :meth:`TestEveryO1ClaimIsBacked.test_every_o1_claim_cites_a_measured_bench`).
_TEST_ROOTS: Final[tuple[str, ...]] = (
    "bench",
    "unit",
    "integration",
    "property",
    "golden",
    "stress",
    "smoke",
    "redteam",
)


def _resolve_cited(name: str) -> list[str]:
    """Return every test root under which ``name`` exists (possibly empty)."""
    return [root for root in _TEST_ROOTS if (TESTS_ROOT / root / name).is_file()]


#: Rows that legitimately cite no bench, each with the reason the audit
#: itself gives. A row may live here ONLY because its honest class is not an
#: O(1) claim in the first place, or because the enforcement genuinely lives
#: at another layer that IS itself gated. Adding a row here is a conscious,
#: reviewable act — the alternative (silent "none") is what this gate exists
#: to forbid.
NO_BENCH_REASONS: Final[dict[str, str]] = {
    "E11": "O(rows) snapshot + O(n log n) sort — never claimed O(1); a "
    "single end-of-tuple call site, so there is no per-append accumulation "
    "to guard.",
    "E12": "O(n log n) full sort, documented as such. The enforcement is the "
    "route-layer per-revision cache (#1533), which has its own gate.",
    "E13": "O(rows) per file, a load-boundary op — never claimed O(1).",
}

#: The class tokens that constitute an O(1) CLAIM and therefore an
#: obligation. ``O(1)-on-repeat`` counts: it claims constant behaviour on
#: the repeat path and that repeat path is exactly what a bench must show.
_O1_CLAIM_RE: Final[re.Pattern[str]] = re.compile(r"O\(1\)", re.IGNORECASE)

#: A cited bench file. The audit writes them bare (``test_bloom_o1.py``) or
#: in backticks; both forms are accepted, nothing else is.
_BENCH_FILE_RE: Final[re.Pattern[str]] = re.compile(r"\btest_[A-Za-z0-9_]+\.py\b")

#: A table row: ``| ID | Path | class | evidence | limit | bench |``.
_ROW_RE: Final[re.Pattern[str]] = re.compile(r"^\|\s*([A-Z]+\d+)\s*\|(.+)\|\s*$")


class AuditRow:
    """One parsed classification row."""

    __slots__ = ("bench_cell", "honest_class", "row_id")

    def __init__(self, row_id: str, honest_class: str, bench_cell: str) -> None:
        self.row_id = row_id
        self.honest_class = honest_class
        self.bench_cell = bench_cell

    @property
    def claims_o1(self) -> bool:
        return bool(_O1_CLAIM_RE.search(self.honest_class))

    @property
    def cited_benches(self) -> list[str]:
        return _BENCH_FILE_RE.findall(self.bench_cell)

    @property
    def cites_none(self) -> bool:
        return not self.cited_benches and "none" in self.bench_cell.lower()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"AuditRow({self.row_id!r}, class={self.honest_class!r})"


def _parse_rows(text: str) -> list[AuditRow]:
    """Parse every classification row out of the audit's markdown tables.

    Deliberately structural, not positional-by-heading: any table whose
    first cell is an ``<LETTERS><DIGITS>`` row id and which has at least six
    cells is a classification row. That survives a section being added,
    renamed or reordered — a reshape that silently drops rows would show up
    in the anti-vacuous count pins below.
    """
    rows: list[AuditRow] = []
    for line in text.splitlines():
        match = _ROW_RE.match(line.strip())
        if match is None:
            continue
        row_id = match.group(1)
        cells = [cell.strip() for cell in match.group(2).split("|")]
        if len(cells) < 5:
            continue
        # cells = [Path, honest class, Evidence, Honest limit, Bench gate, ...]
        rows.append(AuditRow(row_id=row_id, honest_class=cells[1], bench_cell=cells[-1]))
    return rows


@pytest.fixture(scope="module")
def audit_rows() -> list[AuditRow]:
    assert AUDIT_DOC.is_file(), (
        f"the O(1) audit is MISSING at {AUDIT_DOC} — it is the authoritative "
        "complexity record (DEC-O1-GUARANTEE-HARDEN-001); deleting it would "
        "silently retire every claim it carries"
    )
    return _parse_rows(AUDIT_DOC.read_text(encoding="utf-8"))


class TestParserIsNotVacuous:
    """The gate below is only worth anything if the parser SEES the table.

    A markdown reshape that broke parsing would make every assertion pass
    over an empty list. These pins make that failure loud instead.
    """

    def test_parser_finds_a_substantial_number_of_rows(self, audit_rows: list[AuditRow]) -> None:
        assert len(audit_rows) >= 40, (
            f"parsed only {len(audit_rows)} audit rows — the audit documents "
            "51 classified paths, so the parser has almost certainly stopped "
            "matching the table shape. Fix the parser; do NOT lower this floor."
        )

    def test_parser_finds_rows_from_every_tier(self, audit_rows: list[AuditRow]) -> None:
        prefixes = {re.match(r"^([A-Z]+)", row.row_id).group(1) for row in audit_rows}  # type: ignore[union-attr]
        for tier in ("E", "D", "F"):
            assert tier in prefixes, (
                f"no {tier}-prefixed rows parsed — the audit covers engine (E), "
                f"DB/lake (D) and frontend (F) tiers; parsed prefixes: "
                f"{sorted(prefixes)}"
            )

    def test_parser_finds_o1_claims(self, audit_rows: list[AuditRow]) -> None:
        claims = [row for row in audit_rows if row.claims_o1]
        assert len(claims) >= 20, (
            f"only {len(claims)} rows parsed as O(1) claims — the class column "
            "is almost certainly not being read. An empty claim set would make "
            "the backing gate vacuously green."
        )

    def test_parser_reads_bench_citations(self, audit_rows: list[AuditRow]) -> None:
        cited = {name for row in audit_rows for name in row.cited_benches}
        assert len(cited) >= 15, (
            f"only {len(cited)} distinct bench files parsed from the citation "
            "column — the parser is reading the wrong cell."
        )


class TestEveryO1ClaimIsBacked:
    """THE GATE. An O(1) claim with no live bench fails the build."""

    def test_every_o1_claim_cites_a_bench_or_a_written_reason(
        self, audit_rows: list[AuditRow]
    ) -> None:
        unbacked: list[str] = []
        for row in audit_rows:
            if not row.claims_o1:
                continue
            if row.cited_benches:
                continue
            if row.row_id in NO_BENCH_REASONS:
                continue
            unbacked.append(f"{row.row_id} ({row.honest_class!r} -> {row.bench_cell!r})")
        assert not unbacked, (
            "O(1) is CLAIMED with no bench gate and no written exemption for:\n  "
            + "\n  ".join(unbacked)
            + "\n\nEvery O(1) claim must be backed by a measurement that can "
            "fail. Either add a bench to tests/bench/ and cite it in the "
            "audit's 'Bench gate' column, or add the row to NO_BENCH_REASONS "
            "with the reason the class is not an O(1) obligation."
        )

    def test_every_cited_test_file_exists(self, audit_rows: list[AuditRow]) -> None:
        """A citation that resolves to nothing is a claim wearing a costume."""
        missing: dict[str, list[str]] = {}
        for row in audit_rows:
            for name in row.cited_benches:
                if not _resolve_cited(name):
                    missing.setdefault(name, []).append(row.row_id)
        assert not missing, (
            "the audit cites test files that DO NOT EXIST anywhere under "
            f"{TESTS_ROOT}/{{{','.join(_TEST_ROOTS)}}} — a dangling citation "
            "is an unbacked claim wearing a citation:\n  "
            + "\n  ".join(
                f"{name} (cited by {', '.join(rows)})" for name, rows in sorted(missing.items())
            )
        )

    def test_every_o1_claim_cites_a_measured_bench(self, audit_rows: list[AuditRow]) -> None:
        """A source pin is not a measurement.

        The audit cites two strengths, and only one of them can FAIL on a
        number: a file under ``tests/bench/`` runs under the two-tier ratio
        ceiling (CLAUDE.md §2 Rule 5), whereas a unit-level source pin only
        asserts that some code shape exists. Backing an O(1) claim with a
        source pin alone would satisfy the letter of the previous test while
        measuring nothing — exactly the illusion this DEC exists to remove.

        This currently holds for every claiming row; the pin locks it in.
        """
        source_pin_only: list[str] = []
        for row in audit_rows:
            if not row.claims_o1 or row.row_id in NO_BENCH_REASONS:
                continue
            if not any("bench" in _resolve_cited(name) for name in row.cited_benches):
                located = {name: _resolve_cited(name) for name in row.cited_benches}
                source_pin_only.append(f"{row.row_id} -> {located or 'no citation'}")
        assert not source_pin_only, (
            "these rows CLAIM O(1) but cite no file under tests/bench/, so "
            "nothing measures a ratio against a ceiling:\n  "
            + "\n  ".join(source_pin_only)
            + "\n\nA source pin proves a shape; only a bench proves a number."
        )

    def test_no_bench_reasons_are_all_live_rows(self, audit_rows: list[AuditRow]) -> None:
        """A stale exemption is a hole. Every exempted id must still exist."""
        known = {row.row_id for row in audit_rows}
        stale = sorted(set(NO_BENCH_REASONS) - known)
        assert not stale, (
            f"NO_BENCH_REASONS exempts row ids that no longer exist in the "
            f"audit: {stale}. Remove them — a stale exemption silently "
            "pre-approves whatever row later takes that id."
        )

    def test_no_bench_reasons_carry_a_real_reason(self) -> None:
        for row_id, reason in NO_BENCH_REASONS.items():
            assert len(reason) >= 40, (
                f"the exemption for {row_id} is {len(reason)} chars — too "
                "short to be a reason. State WHY the row carries no O(1) "
                "obligation."
            )


class TestTierGateIsWiredIn:
    """Layer 3 of the chain: the ceiling every bench asserts against.

    Layer 1 (this file) proves a claim cites a bench. Layer 2 is the bench.
    Layer 3 is the shared tier module — if it vanished, every bench would
    silently lose its ceiling, so its existence is pinned here alongside.
    """

    def test_the_shared_tier_module_exists(self) -> None:
        tiers = BENCH_DIR / "_o1_tiers.py"
        assert tiers.is_file(), (
            "tests/bench/_o1_tiers.py is MISSING — it is the single source of "
            "the robust/strict/cache-tier ceilings (CLAUDE.md §2 Rule 5). "
            "Without it every bench's ceiling is ad-hoc."
        )

    def test_the_tier_module_defines_both_tiers(self) -> None:
        source = (BENCH_DIR / "_o1_tiers.py").read_text(encoding="utf-8")
        for token in ("ratio_ceiling", "M4PRO_STRICT"):
            assert token in source, (
                f"tests/bench/_o1_tiers.py no longer mentions {token!r} — the "
                "two-tier split (robust shared-CI vs strict M4-Pro) is the "
                "documented enforcement of Golden Rule #5."
            )
