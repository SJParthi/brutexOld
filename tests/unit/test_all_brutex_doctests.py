"""Repo-wide doctest gate — every ``>>>`` example in every brutex module runs in CI.

DEC-TEST-APPARATUS-DEBTS-001(a): before this gate, only ``brutex.engine.
combo_mask`` had its doctests executed (``tests/unit/test_combo_mask_doctests.py``
— kept, it carries an independent VOCAB_BITS==60 pin), so a broken example
anywhere else rotted silently: ``brutex/data/tf_resampler.py`` shipped three
examples that ALL raised ``NameError: name 'df_1m' is not defined`` (fixed in
the same change that lands this gate, as a self-contained runnable block).

Mechanism: generalize the single-module pattern to a ``pkgutil.walk_packages``
sweep over the whole ``brutex`` package — import every module, run
``doctest.testmod``, aggregate. Three teeth:

1. ZERO doctest failures, with the aggregate failure list naming each module.
2. ZERO import/walk errors (``walk_packages`` silently PRUNES a subtree whose
   package fails to import when ``onerror`` is not given — collected and
   asserted empty so a broken import can never quietly shrink the gate).
3. A MEASURED attempted-example floor, so the gate can never silently decay
   into a no-op (the pre-gate failure mode: examples existed, nothing ran
   them). ``# doctest: +SKIP`` is banned across ``brutex/`` for the same
   reason — a SKIP-flagged example is excluded BEFORE ``result.attempted``
   is incremented (CPython's ``DocTestRunner`` counts skips separately), so
   it would bypass teeth 1 AND 3 and fake-green the exact rot class this
   gate exists to catch. The ban is ENFORCED by
   ``test_no_doctest_skip_directive_in_brutex`` below, not merely asserted.

Note: pytest's ``-W error`` addopts apply inside doctest execution too — a
future warning-emitting example fails loudly here, which is desired.
"""

from __future__ import annotations

import doctest
import importlib
import pkgutil
import re
from pathlib import Path

import brutex

#: The doctest SKIP directive, matched anywhere on a line (``# doctest: +SKIP``
#: and multi-flag forms like ``# doctest: +ELLIPSIS, +SKIP``). Scanned as TEXT
#: over every ``brutex/`` source file — a skipped example never executes and
#: never increments ``result.attempted``, so it would silently bypass both the
#: failure tooth and the attempted-example floor of the gate above.
_DOCTEST_SKIP_DIRECTIVE = re.compile(r"#\s*doctest:[^\n]*\+SKIP\b")

#: Modules deliberately excluded from the doctest walk, each with a reason.
#: ``brutex.__main__``: the ``python -m brutex`` console-script alias — its
#: docstring shows SHELL commands (no ``>>>`` examples, nothing to doctest)
#: and importing an entrypoint module by name is skipped on principle so a
#: future dispatch-on-import change can never make the GATE launch the CLI.
_SKIPPED_MODULES: frozenset[str] = frozenset({"brutex.__main__"})

#: Teeth floor = the attempted-example count MEASURED AT LANDING (2026-07-06,
#: post tf_resampler docstring fix): 55 = combo_mask 34 + timeframes 10 +
#: tf_resampler 9 + constituents_cache 2. Verified two ways in the landing
#: run: the dynamic walk below AND an independent AST + DocTestParser static
#: census agreed exactly (55/55, zero import failures). If examples are
#: legitimately removed, lower this deliberately in the same PR — never by
#: silently skipping modules.
_MEASURED_EXAMPLE_FLOOR = 55


def test_all_brutex_module_doctests() -> None:
    """Every doctest example in every ``brutex`` module passes (repo-wide gate)."""
    walk_errors: list[str] = []
    failures: list[tuple[str, int]] = []
    attempted = 0
    for info in pkgutil.walk_packages(
        brutex.__path__, prefix="brutex.", onerror=walk_errors.append
    ):
        if info.name in _SKIPPED_MODULES:
            continue
        module = importlib.import_module(info.name)
        result = doctest.testmod(module, verbose=False)
        attempted += result.attempted
        if result.failed:
            failures.append((info.name, result.failed))
    assert not walk_errors, (
        f"pkgutil.walk_packages could not import package(s) {walk_errors} — "
        f"their whole subtree was pruned from the doctest walk; fix the import."
    )
    assert not failures, f"doctest failures in: {failures}"
    assert attempted >= _MEASURED_EXAMPLE_FLOOR, (
        f"doctest gate decayed: only {attempted} examples attempted across "
        f"brutex/, below the measured landing floor {_MEASURED_EXAMPLE_FLOOR}. "
        f"If examples were legitimately removed, re-measure and lower the "
        f"floor deliberately in the same PR."
    )


def test_no_doctest_skip_directive_in_brutex() -> None:
    """No ``brutex/`` source file carries a ``# doctest: +SKIP`` directive.

    Enforcement teeth for the ban the module docstring documents: CPython's
    ``doctest.DocTestRunner`` skips a SKIP-flagged example BEFORE incrementing
    ``tries``, so such an example never executes AND never registers in
    ``result.attempted`` — it would bypass both the zero-failures tooth and
    the measured attempted-example floor of ``test_all_brutex_module_doctests``
    and rot silently (the exact pre-gate failure class). This scan makes the
    escape hatch fail loudly instead. Scans every ``*.py`` under every
    ``brutex`` package root on disk (a superset of what the doctest walk can
    import — an offender cannot hide behind an import error or a skip list).
    """
    offenders: list[str] = []
    for package_root in brutex.__path__:
        for path in sorted(Path(package_root).rglob("*.py")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if _DOCTEST_SKIP_DIRECTIVE.search(line):
                    offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, (
        "'# doctest: +SKIP' is banned in brutex/ — a skipped example never "
        "runs and never counts toward the doctest gate's attempted floor, "
        "silently reviving the rot class this gate exists to kill. Make the "
        "example runnable (self-contained setup) or remove it and lower the "
        "floor deliberately. Offenders:\n" + "\n".join(offenders)
    )
