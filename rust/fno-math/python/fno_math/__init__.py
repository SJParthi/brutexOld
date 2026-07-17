"""fno_math — BruteX Rust hot kernel (PyO3 bridge).

DEC-P0-055 (PR-N+2): mixed Python + Rust layout. The compiled cdylib
ships as the private submodule `fno_math._fno_math`; this `__init__.py`
re-exports its symbols so downstream callers continue to write
`import fno_math; fno_math.popcount_bytes(...)` unchanged.

The matched `__init__.pyi` + `py.typed` marker make every re-export
visible to `pyright --strict` and `mypy --strict` without per-call
`# pyright: ignore` shims (closes the PR-N revert post-mortem at
docs/MIGRATION.md #190).

Wheel-source freshness handshake (DEC-FNOMATH-WHEEL-FRESHNESS-001)
------------------------------------------------------------------
The fno_math twin of trading_core's R2-CC-G1 handshake (MIGRATION.md
#1166 Task 4), made urgent by DEC-SWEEP-RUST-TRADESIM-WIRE-001 (#1182):
``ResidentSweepSim`` now runs UNFLAGGED in the sweep hot path, so a stale
installed ``_fno_math`` wheel is an always-on production risk.

Import-time guard: the compiled ``.so`` is asserted to expose EVERY symbol
this shim re-exports (``_EXPECTED_COMPILED_SYMBOLS``) BEFORE the re-export
runs, so a stale installed wheel that predates a source-tree symbol raises
a clear, actionable ``ImportError`` naming the missing symbol(s) instead of
a bare symbol-not-found deep inside a consumer. ``__build_crate_version__``
(derived from the compiled module's ``CARGO_PKG_VERSION``-baked
``__version__``, never hardcoded) plus the in-repo ``EXPECTED_BUILD.json``
fingerprint give ``brutex doctor`` a wheel-freshness stage
(``brutex.doctor.check_fno_math_wheel_freshness``).

The import-time handshake is deliberately UNCONDITIONAL — there is NO env
escape. One could not help anyway: a ``.so`` genuinely missing a
re-exported symbol would fail the ``from ._fno_math import (...)`` below
regardless, so the only remedy for a symbol-stale install is the rebuild
the ImportError message names.

HONEST residual gap (Golden Rule #10, same envelope as trading_core's):
the version + symbol-set handshake catches a stale wheel that dropped or
added a symbol or changed crate version. It does NOT catch same-version,
symbol-compatible BEHAVIORAL drift (Rust source changed while version +
symbol surface stayed identical). The RUNTIME defence for that class on
the trade-sim path is the CI golden/parity suite (the correctness proof
stays CI-side; this handshake is the runtime stale-wheel tripwire). The
formerly-named "source-content digest baked into the wheel" follow-up is
now CLOSED (DEC-RUST-SOURCE-DIGEST-TWIN-001, MIGRATION.md #1258):
``build.rs`` bakes the crate-source BLAKE3 digest into the compiled
module as ``__source_digest__`` and ``brutex doctor``'s
``wheel_source_digest_fno_math`` stage compares it (WARN, non-fatal)
against the Python-twin recompute over the repo checkout — LOCAL-machine
scope only (CI always builds wheels from source, so it can never see a
stale wheel); this import-time handshake deliberately does NOT gate on
the digest (an older wheel must keep importing so the doctor can warn).
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence

#: Every symbol this shim re-exports from the compiled PyO3 module — the ONE
#: in-code source of truth for the import-time freshness handshake. The
#: in-repo ``EXPECTED_BUILD.json`` (read by ``brutex doctor``) mirrors this
#: list; a test pins the two equal so they cannot drift.
_EXPECTED_COMPILED_SYMBOLS: tuple[str, ...] = (
    "ResidentBars",
    "ResidentSweepSim",
    "ResidentSweepSimU128",
    "__version__",
    "add_u64",
    "bitand_count_u64s",
    "bitcount_u64s",
    "blake3_hash",
    "count_supermask_u64s",
    "count_supermasks_u64s",
    "count_supermasks_u64s_nthreads",
    "popcount_bytes",
    "stats_cpcv_pass_count",
    "sweep_drawdown_stats",
)


def _assert_compiled_surface(
    compiled: object,
    expected: Sequence[str] = _EXPECTED_COMPILED_SYMBOLS,
) -> None:
    """Raise an actionable ``ImportError`` if ``compiled`` misses any expected symbol.

    The teeth of the wheel-source freshness handshake: a symbol-incomplete
    (stale) compiled module fails HERE, at import, naming exactly what is
    missing and how to fix it — never a confusing ``ImportError: cannot import
    name ...`` or an ``AttributeError`` deep inside a consumer (e.g. the sweep
    hot path reaching for a ``ResidentSweepSim`` the installed ``.so``
    predates).
    """
    missing = sorted(name for name in expected if not hasattr(compiled, name))
    if missing:
        location = getattr(compiled, "__file__", "<unknown location>")
        raise ImportError(
            "fno_math: the installed compiled wheel "
            f"(fno_math._fno_math at {location}) is STALE — it is missing "
            f"symbol(s) {', '.join(missing)} that this source tree's shim "
            "re-exports. Rebuild + reinstall the wheel from the current source: "
            "run `maturin develop --release` inside rust/fno-math "
            "(with the project venv active)."
        )


_compiled = importlib.import_module("._fno_math", __name__)
_assert_compiled_surface(_compiled)

# The freshness handshake above MUST run before this re-export so a stale
# wheel raises the actionable message, not a bare from-import failure.
from ._fno_math import (  # noqa: E402
    ResidentBars,
    ResidentSweepSim,
    ResidentSweepSimU128,
    __version__,
    add_u64,
    bitand_count_u64s,
    bitcount_u64s,
    blake3_hash,
    count_supermask_u64s,
    count_supermasks_u64s,
    count_supermasks_u64s_nthreads,
    popcount_bytes,
    stats_cpcv_pass_count,
    sweep_drawdown_stats,
)

#: Crate version of the INSTALLED compiled kernel — derived at import from the
#: compiled module's ``__version__`` (``env!("CARGO_PKG_VERSION")`` baked into
#: the ``.so`` at build time), NEVER hardcoded here. ``brutex doctor``'s
#: fno_math wheel-freshness stage compares this against the in-repo
#: ``EXPECTED_BUILD.json`` fingerprint.
#:
#: Deliberately NOT in ``__all__`` (mirrors trading_core): it is a build
#: stamp, not kernel API — and ``brutex.math`` rebuilds the merged two-kernel
#: facade's frozen public surface from each kernel's ``__all__``, so listing a
#: kernel-specific stamp there would leak it into (and break) the frozen
#: facade golden (``tests/golden/test_math_facade_golden.py``). Consumers
#: (``brutex.doctor.check_fno_math_wheel_freshness``) read it via plain
#: attribute access.
__build_crate_version__: str = __version__

__all__ = [
    "ResidentBars",
    "ResidentSweepSim",
    "ResidentSweepSimU128",
    "__version__",
    "add_u64",
    "bitand_count_u64s",
    "bitcount_u64s",
    "blake3_hash",
    "count_supermask_u64s",
    "count_supermasks_u64s",
    "count_supermasks_u64s_nthreads",
    "popcount_bytes",
    "stats_cpcv_pass_count",
    "sweep_drawdown_stats",
]
