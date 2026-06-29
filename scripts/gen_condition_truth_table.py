#!/usr/bin/env python3
"""Generate the 52-condition truth table CSV from the frozen golden vectors.

DEC-PHASE2-TRUTH-TABLE-001 (Phase 2, ``docs/TRACK1_PHASE_PLAN.md`` corpus #7 —
"52-condition truth table / per-condition determinism (G9)").

The human-readable companion to ``tests/golden/fixtures/vocab_golden_vectors.json``
(the language-neutral ``input → u64`` oracle locked by ``test_vocab_golden.py``).
This script projects that **already-proven** corpus into a flat, auditable
``(snippet, condition, bit, expected_bool)`` table so a human — or the future
Rust ``trading_core`` crate's own per-condition tests — can read off exactly
which of the 52 atomic conditions fires for a concrete worked input.

Grounding (Golden Rule #1 — no hallucination): every ``expected_bool`` is
DECODED from the frozen, evaluator-produced ``expected_u64`` in the JSON corpus
(``generated_from = brutex.vocabulary.evaluator.evaluate_atoms``). Nothing is
hand-authored. The bit↔name mapping is read from the ONE source of truth,
``brutex/vocabulary/bits.py`` (CLAUDE.md §11 COMMON).

Case selection: a deterministic greedy set-cover picks the fewest golden cases
whose union SETS every one of the 52 bits at least once AND CLEARS every bit at
least once (the G9 both-ways guarantee), then pads in case-id order until the
table has at least ``MIN_ROWS`` rows (``docs/TRACK1_PHASE_PLAN.md`` Week 2:
"expands to ≥ 200 rows"). The output is fully deterministic — re-running yields
byte-identical CSV (the consumer test pins its SHA-256).

Usage::

    python scripts/gen_condition_truth_table.py            # write the CSV
    python scripts/gen_condition_truth_table.py --check     # fail if stale

Note: this script imports ``bits.py`` **standalone** (via importlib from the
file path) rather than ``import brutex.vocabulary`` so it runs WITHOUT the Rust
``trading_core`` wheel — the package ``__init__`` pulls in the evaluator (and
thus ``trading_core``), but the bit registry itself is pure-Python. The frozen
``expected_u64`` values mean no evaluator call is needed to regenerate the table.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_BITS_PATH = _ROOT / "brutex" / "vocabulary" / "bits.py"
_VECTORS_PATH = _ROOT / "tests" / "golden" / "fixtures" / "vocab_golden_vectors.json"
_OUT_PATH = _ROOT / "tests" / "golden" / "condition_truth_table.csv"

HEADER = ("snippet_id", "condition_name", "bit_position", "expected_bool", "description")

#: Minimum row count (``docs/TRACK1_PHASE_PLAN.md`` Week 2 — "≥ 200 rows").
MIN_ROWS = 200


def load_bit_registry() -> tuple[dict[int, str], int, int]:
    """Load ``BIT_TO_NAME`` + ``TOTAL_CONDITIONS`` + ``IMPLEMENTED_MASK`` standalone.

    Imports the module by file path so the package ``__init__`` (which pulls in
    the evaluator → ``trading_core``) is never executed — the registry is the
    single source of truth and is pure-Python.

    Returns
    -------
    tuple[dict[int, str], int, int]
        The bit-position → condition-name map, the total condition count, and the
        IMPLEMENTED bitmask (the bits the evaluator actually computes; the
        LAYOUT-ONLY bits are excluded from the both-ways cover + the table rows).
    """
    spec = importlib.util.spec_from_file_location("_brutex_bits_standalone", _BITS_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover — defensive, path is fixed
        raise RuntimeError(f"cannot load bit registry from {_BITS_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BIT_TO_NAME, module.TOTAL_CONDITIONS, module.IMPLEMENTED_MASK


def _case_mask(case: dict[str, Any]) -> int:
    """Parse a case's ``expected_mask`` hex string into the u128 mask int."""
    return int(case["expected_mask"], 16)


def select_cases(
    cases: list[dict[str, Any]], impl_bits: list[int], *, min_rows: int
) -> list[dict[str, Any]]:
    """Greedy both-ways set-cover over *cases*, padded to ``min_rows`` rows.

    Picks the fewest cases (in corpus order) whose union sets AND clears every
    IMPLEMENTED bit, then appends further unused cases (in case-id order) until
    ``len(selected) * len(impl_bits) >= min_rows``. Deterministic. The LAYOUT-ONLY
    bits are excluded — the evaluator never sets them, so a both-ways cover over
    them is impossible (they are proven always-clear by the golden tests).

    Parameters
    ----------
    cases
        The golden corpus cases (each with an integer ``id`` and ``expected_mask``).
    impl_bits
        The implemented condition bit positions — one row per bit per selected case.
    min_rows
        The floor on the emitted row count.

    Returns
    -------
    list[dict]
        The selected cases, sorted by ``id`` for a stable, reproducible table.
    """
    need_set = set(impl_bits)
    need_clear = set(impl_bits)
    chosen: list[dict[str, Any]] = []
    chosen_ids: set[int] = set()
    for case in cases:
        mask = _case_mask(case)
        gained_set = {b for b in need_set if (mask >> b) & 1}
        gained_clear = {b for b in need_clear if not (mask >> b) & 1}
        if gained_set or gained_clear:
            chosen.append(case)
            chosen_ids.add(case["id"])
            need_set -= gained_set
            need_clear -= gained_clear
        if not need_set and not need_clear:
            break
    if need_set or need_clear:  # pragma: no cover — the 400-case corpus covers all bits
        raise RuntimeError(
            f"corpus does not cover every implemented bit both ways: "
            f"unset={sorted(need_set)} uncleared={sorted(need_clear)}"
        )
    for case in cases:
        if len(chosen) * len(impl_bits) >= min_rows:
            break
        if case["id"] not in chosen_ids:
            chosen.append(case)
            chosen_ids.add(case["id"])
    chosen.sort(key=lambda c: c["id"])
    return chosen


def build_rows(
    cases: list[dict[str, Any]], bit_to_name: dict[int, str], impl_bits: list[int]
) -> list[tuple[str, str, int, str, str]]:
    """Decode each selected case's ``expected_mask`` into one row per implemented condition.

    Parameters
    ----------
    cases
        The selected golden cases (already ordered by id).
    bit_to_name
        Bit-position → condition-name map (the source-of-truth registry).
    impl_bits
        The implemented condition bit positions (one row per bit per case). The
        LAYOUT-ONLY bits are excluded — they are always false (no row needed).

    Returns
    -------
    list[tuple]
        Rows ``(snippet_id, condition_name, bit_position, expected_bool, description)``
        in (case-id, bit-position) order.
    """
    rows: list[tuple[str, str, int, str, str]] = []
    for case in cases:
        snippet_id = f"CASE-{case['id']:03d}"
        mask = _case_mask(case)
        ts = case["timestamp_ist"]
        for bit in impl_bits:
            name = bit_to_name[bit]
            value = bool((mask >> bit) & 1)
            expected = "true" if value else "false"
            description = f"golden case {case['id']} @ {ts} IST: {name}={expected}"
            rows.append((snippet_id, name, bit, expected, description))
    return rows


def render_csv(rows: list[tuple[str, str, int, str, str]]) -> str:
    """Render *rows* (with the canonical header) to a CSV string (``\\n`` line endings)."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    writer.writerows(rows)
    return buffer.getvalue()


def generate() -> str:
    """Build the full truth-table CSV text from the frozen corpus + bit registry."""
    bit_to_name, total, implemented_mask = load_bit_registry()
    impl_bits = [b for b in range(total) if (implemented_mask >> b) & 1]
    data = json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))
    cases = select_cases(data["cases"], impl_bits, min_rows=MIN_ROWS)
    rows = build_rows(cases, bit_to_name, impl_bits)
    return render_csv(rows)


def main(argv: list[str] | None = None) -> int:
    """CLI entry: write the CSV, or (with ``--check``) fail if it would change."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the on-disk CSV differs from a fresh generation",
    )
    args = parser.parse_args(argv)
    fresh = generate()
    if args.check:
        current = _OUT_PATH.read_text(encoding="utf-8") if _OUT_PATH.exists() else ""
        if current != fresh:
            sys.stderr.write(
                f"{_OUT_PATH} is stale — re-run `python {Path(__file__).name}` to regenerate.\n"
            )
            return 1
        sha = hashlib.sha256(fresh.encode("utf-8")).hexdigest()
        sys.stdout.write(
            f"OK: truth table up to date ({fresh.count(chr(10)) - 1} rows, sha256={sha})\n"
        )
        return 0
    _OUT_PATH.write_text(fresh, encoding="utf-8")
    sha = hashlib.sha256(fresh.encode("utf-8")).hexdigest()
    sys.stdout.write(f"wrote {_OUT_PATH} ({fresh.count(chr(10)) - 1} rows, sha256={sha})\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
