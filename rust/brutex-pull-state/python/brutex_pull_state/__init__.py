"""brutex_pull_state — dashboard pull-state decision kernel (PyO3 bridge).

``DEC-RUST-PULL-STATE-001``: mixed Python + Rust layout (the ``brutex_feed`` /
``trading_core`` shape). The compiled cdylib ships as the private submodule
``brutex_pull_state._brutex_pull_state``; this ``__init__.py`` re-exports its
symbols so the consumer (``brutex/pull_state_rust.py``) writes
``import brutex_pull_state; brutex_pull_state.pull_allowed_us(...)``.

Exposed surface:

* ``pull_allowed_us`` — the ONE pure decision function: the market-hours
  guard predicate (DEC-GROWW-MARKET-HOURS-GUARD-001, MIGRATION.md #1692)
  over INTEGERS ONLY — microsecond-of-day + the caller-resolved trading-day
  flag → ``bool``. The NSE calendar stays Python; no float/string/timestamp
  ever crosses the FFI (the DEC-FEED-RUST-CORE-001 discipline).
* ``SOURCE_DIGEST`` / ``__source_digest__`` — the crate-source BLAKE3 digest
  baked by ``build.rs`` (DEC-RUST-SOURCE-DIGEST-TWIN-001, MIGRATION.md #1258).

Wheel-source freshness handshake (the brutex_feed pattern)
----------------------------------------------------------
Import-time guard: the compiled ``.so`` is asserted to expose EVERY symbol
this shim re-exports (``_EXPECTED_COMPILED_SYMBOLS``) BEFORE the re-export
runs, so a stale installed wheel that predates a source-tree symbol raises a
clear, actionable ``ImportError`` naming the missing symbol(s) instead of a
bare symbol-not-found deep inside the consumer. The handshake is deliberately
UNCONDITIONAL — no env escape; the CONSUMER side is equally FAIL-CLOSED
(DEC-RUST-PULL-STATE-001, flip phase): the ``brutex/pull_state_rust.py``
resolver catches this ImportError and raises the typed
``PullStateKernelError`` (``reason=wheel_missing`` / ``wheel_stale``) — the
Python oracles are DELETED, so a stale/absent wheel STOPS the pull-state
decision; for the market-hours guard chain a pull is REFUSED, never
silently allowed.

HONEST residual gap (Golden Rule #10, the siblings' envelope): the version +
symbol-set handshake catches a stale wheel that dropped/added a symbol or
changed crate version. It does NOT catch same-version, symbol-compatible
BEHAVIORAL drift at runtime — the catches for that class are the frozen
guard-verdict golden (``tests/golden/test_market_hours_guard_golden.py``, a
CI-time net over the whole covered calendar), the committed pre-conversion
verdict goldens, and the hypothesis differentials against in-test INLINE
laws (``tests/property/test_pull_state_rust_parity.py`` — the pre-flip
Python oracles are deleted, so the differential partner is the inline law).
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence

#: Every symbol this shim re-exports from the compiled PyO3 module — the ONE
#: in-code source of truth for the import-time freshness handshake. The
#: in-repo ``EXPECTED_BUILD.json`` mirrors the non-dunder names; a test pins
#: the two equal so they cannot drift.
_EXPECTED_COMPILED_SYMBOLS: tuple[str, ...] = (
    "SOURCE_DIGEST",
    "__version__",
    "alive_log_due",  # pull_stall (Wave 1C)
    "auto_gate_decision",  # auto_pull_gate (Wave 2D)
    "auto_state_verdict",  # auto_pull_scheduler (Wave 3G)
    "auto_tick_decision",  # auto_pull_scheduler (Wave 3G)
    "bridge_payload_verdict",  # rate_bridge (Wave 3H)
    "census_fold",  # pull_census (Wave 3G)
    "classify_pull_outcome",  # pull_outcome (Wave 1A)
    "fno_only_note_surgery",  # pull_scope (Wave 1B)
    "intent_refused_restore_decision",  # pull_intent (Wave 2E)
    "intent_resume_decision",  # pull_intent (Wave 2E)
    "intent_resume_enabled",  # pull_intent (Wave 2E)
    "intent_shape_verdict",  # pull_intent (Wave 2E)
    "market_hours_note_rewrite",  # pull_scope (Wave 1B)
    "memo_serve_arm",  # rate_bridge_cache (Wave 3H)
    "muhurat_window_active",  # auto_pull_gate (Wave 2D)
    "progress_derived",  # pull_progress (Wave 2F)
    "pull_allowed_us",
    "scope_token",  # pull_scope (Wave 1B)
    "settle_token_verdict",  # auto_pull_gate (Wave 2D)
    "should_reload_master",  # auto_pull (Wave 3G)
    "spot_only_note_surgery",  # pull_scope (Wave 1B)
    "token_revalidates",  # rate_bridge_cache (Wave 3H)
    "verdict_correlates",  # pull_outcome (Wave 1A)
    "wedge_decision",  # pull_stall (Wave 1C)
)


def _assert_compiled_surface(
    compiled: object,
    expected: Sequence[str] = _EXPECTED_COMPILED_SYMBOLS,
) -> None:
    """Raise an actionable ``ImportError`` if ``compiled`` misses any expected symbol.

    The teeth of the wheel-source freshness handshake: a symbol-incomplete
    (stale) compiled module fails HERE, at import, naming exactly what is
    missing and how to fix it — never a confusing ``ImportError: cannot
    import name ...`` deep inside a consumer.
    """
    missing = sorted(name for name in expected if not hasattr(compiled, name))
    if missing:
        location = getattr(compiled, "__file__", "<unknown location>")
        raise ImportError(
            "brutex_pull_state: the installed compiled wheel "
            f"(brutex_pull_state._brutex_pull_state at {location}) is STALE — "
            f"it is missing symbol(s) {', '.join(missing)} that this source "
            "tree's shim re-exports. Rebuild + reinstall the wheel from the "
            "current source: run `maturin develop --release` inside "
            "rust/brutex-pull-state (with the project venv active)."
        )


_compiled = importlib.import_module("._brutex_pull_state", __name__)
_assert_compiled_surface(_compiled)

# The freshness handshake above MUST run before this re-export so a stale
# wheel raises the actionable message, not a bare from-import failure.
from ._brutex_pull_state import (  # noqa: E402
    SOURCE_DIGEST,
    __version__,
    alive_log_due,
    auto_gate_decision,
    auto_state_verdict,
    auto_tick_decision,
    bridge_payload_verdict,
    census_fold,
    classify_pull_outcome,
    fno_only_note_surgery,
    intent_refused_restore_decision,
    intent_resume_decision,
    intent_resume_enabled,
    intent_shape_verdict,
    market_hours_note_rewrite,
    memo_serve_arm,
    muhurat_window_active,
    progress_derived,
    pull_allowed_us,
    scope_token,
    settle_token_verdict,
    should_reload_master,
    spot_only_note_surgery,
    token_revalidates,
    verdict_correlates,
    wedge_decision,
)

#: Crate version of the INSTALLED compiled kernel — derived at import from the
#: compiled module's ``__version__`` (``env!("CARGO_PKG_VERSION")`` baked into
#: the ``.so`` at build time), NEVER hardcoded here. Deliberately NOT in
#: ``__all__`` (mirrors the siblings): a build stamp, not kernel API.
__build_crate_version__: str = __version__

#: Crate-source BLAKE3 digest of the INSTALLED compiled kernel (FRAMING v1;
#: DEC-RUST-SOURCE-DIGEST-TWIN-001). Same value as ``SOURCE_DIGEST`` — the
#: dunder spelling mirrors the sibling wheels' doctor-facing surface.
__source_digest__: str = SOURCE_DIGEST

__all__ = [
    "SOURCE_DIGEST",
    "__version__",
    "alive_log_due",
    "auto_gate_decision",
    "auto_state_verdict",
    "auto_tick_decision",
    "bridge_payload_verdict",
    "census_fold",
    "classify_pull_outcome",
    "fno_only_note_surgery",
    "intent_refused_restore_decision",
    "intent_resume_decision",
    "intent_resume_enabled",
    "intent_shape_verdict",
    "market_hours_note_rewrite",
    "memo_serve_arm",
    "muhurat_window_active",
    "progress_derived",
    "pull_allowed_us",
    "scope_token",
    "settle_token_verdict",
    "should_reload_master",
    "spot_only_note_surgery",
    "token_revalidates",
    "verdict_correlates",
    "wedge_decision",
]
