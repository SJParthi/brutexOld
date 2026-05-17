#!/usr/bin/env python3
"""Verify ``brutex/math/__init__.pyi`` stub signatures match the PyO3 wheel.

DEC-P0-019 (PR #41a): closes PR #38's deferred maturin verifier (PARTIAL
envelope marker). Parses ``rust/fno-math-py/src/lib.rs`` for the
``#[pyo3(signature = (...))]`` declarations on each ``#[pyfunction]`` and
asserts the parameter NAMES match the corresponding signature in the
``.pyi`` stub at ``brutex/math/__init__.pyi``.

Why not ``inspect.signature(fno_math_py.delta)``? Per Agent 3 of PR #41
hostile review: PyO3 functions reflect as ``(*args, **kwargs)`` at runtime
UNLESS ``#[pyo3(signature=...)]`` is set, AND `inspect.signature` reads the
attribute Python-side which may or may not show the PyO3 declaration. Parsing
the Rust source directly is the authoritative ground truth.

Scope (intentional per PR #41a):
- 6 Greeks (delta, gamma, theta, vega, rho, implied_volatility) are checked.
- 6 indicators (atr, bollinger_bands, ema, macd, rsi, sma) are NOT checked
  because the Rust crate documents them as "Phase 2 per MG-043 — not yet
  present"; stub deliberately keeps them as `(*args: Any, **kwargs: Any)`.

Exit code: 0 if all 6 Greeks match; 1 if any mismatch. Stdout has a table.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUST_LIB = REPO_ROOT / "rust" / "fno-math-py" / "src" / "lib.rs"
PYI_STUB = REPO_ROOT / "brutex" / "math" / "__init__.pyi"

# Greeks only — see module docstring for why indicators are excluded.
GREEK_SYMBOLS: tuple[str, ...] = (
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "implied_volatility",
)


def _parse_rust_signatures(text: str) -> dict[str, list[str]]:
    """Extract `#[pyo3(name = "X")]` + `#[pyo3(signature = (a, b, ...))]`.

    Returns a dict ``{python_name: [param_name_1, param_name_2, ...]}``
    for every PyO3 function in the Rust source.

    Walks line-by-line because the `signature = (...)` payload contains
    parentheses that a single regex would need balanced-paren matching to
    capture. Line-walking is simpler and bug-resistant.
    """
    results: dict[str, list[str]] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() == "#[pyfunction]":
            # Scan forward through attribute lines until the `fn` line.
            py_name: str | None = None
            sig_params: list[str] | None = None
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith("fn "):
                line = lines[j].strip()
                name_match = re.match(r'#\[pyo3\(name\s*=\s*"([^"]+)"\)\]', line)
                if name_match:
                    py_name = name_match.group(1)
                # signature = (a, b, c) — capture everything between the
                # first `(` AFTER `signature` and the final matching `)`.
                sig_match = re.match(r"#\[pyo3\(signature\s*=\s*\((.*)\)\)\]$", line)
                if sig_match:
                    raw = sig_match.group(1)
                    sig_params = [p.strip() for p in raw.split(",") if p.strip()]
                j += 1
            if py_name and sig_params is not None:
                results[py_name] = sig_params
            i = j
        i += 1
    return results


def _parse_pyi_signatures(text: str, names: Iterable[str]) -> dict[str, list[str]]:
    """Extract ``def NAME(a: t1, b: t2, ...) -> R: ...`` parameter names.

    Multi-line ``def`` signatures are joined before parsing.
    """
    # Strip backslash continuations and join multi-line `def` blocks.
    joined = re.sub(r"\n\s+", " ", text)
    results: dict[str, list[str]] = {}
    for name in names:
        match = re.search(rf"def\s+{re.escape(name)}\s*\(([^)]*)\)", joined)
        if match is None:
            continue
        raw = match.group(1).strip()
        if not raw:
            results[name] = []
            continue
        # Each param is `NAME: TYPE` — split on commas at depth 0.
        params = []
        depth = 0
        current = ""
        for ch in raw + ",":
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            if ch == "," and depth == 0:
                if current.strip():
                    params.append(current.strip().split(":")[0].strip())
                current = ""
            else:
                current += ch
        results[name] = params
    return results


def main() -> int:
    if not RUST_LIB.exists():
        print(f"FAIL: Rust source missing: {RUST_LIB}", file=sys.stderr)
        return 1
    if not PYI_STUB.exists():
        print(f"FAIL: stub missing: {PYI_STUB}", file=sys.stderr)
        return 1

    rust_text = RUST_LIB.read_text()
    pyi_text = PYI_STUB.read_text()

    rust_sigs = _parse_rust_signatures(rust_text)
    pyi_sigs = _parse_pyi_signatures(pyi_text, GREEK_SYMBOLS)

    print(f"{'symbol':<22}{'rust params':<55}{'stub params':<55}{'status':<8}")
    print("-" * 140)

    mismatches: list[str] = []
    for symbol in GREEK_SYMBOLS:
        rust_params = rust_sigs.get(symbol)
        pyi_params = pyi_sigs.get(symbol)
        if rust_params is None:
            mismatches.append(f"{symbol}: missing from Rust source")
            print(f"{symbol:<22}{'<MISSING>':<55}{pyi_params!s:<55}FAIL")
            continue
        if pyi_params is None:
            mismatches.append(f"{symbol}: missing from .pyi stub")
            print(f"{symbol:<22}{rust_params!s:<55}{'<MISSING>':<55}FAIL")
            continue
        if rust_params == pyi_params:
            status = "OK"
        else:
            status = "FAIL"
            mismatches.append(
                f"{symbol}: rust={rust_params} pyi={pyi_params}",
            )
        print(f"{symbol:<22}{rust_params!s:<55}{pyi_params!s:<55}{status:<8}")

    print()
    if mismatches:
        print("MISMATCHES:", file=sys.stderr)
        for m in mismatches:
            print(f"  - {m}", file=sys.stderr)
        return 1
    print(f"OK: all {len(GREEK_SYMBOLS)} Greek signatures match between Rust + stub")
    return 0


if __name__ == "__main__":
    sys.exit(main())
