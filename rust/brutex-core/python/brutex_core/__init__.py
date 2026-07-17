"""brutex_core — BruteX root-leaf Rust seams (PyO3 bridge, slice T35).

DEC-CONFIG-RUST-LEAVES-001 (MIGRATION.md #1943). The compiled cdylib ships
as the private submodule ``brutex_core._brutex_core``; this ``__init__.py``
re-exports its symbols (the fno-math DEC-P0-055 mixed layout, via
rust/brutex-lake) so callers write ``import brutex_core;
brutex_core.round_money_str(...)``.

The ONE consumer is the default-OFF facade ``brutex/money_rust.py``
(``BRUTEX_CORE_RUST_MONEY``) — milestone 1's money seam over
``brutex/money.py`` (DEC-MONEY-2DP-001): 2dp half-away-from-zero rounding,
the exact ×100 / ÷100 paisa shifts and the ``₹``-grouped display render,
strings-only across the FFI. Nothing else in ``brutex/`` imports this
package, and the facade treats an absent/stale install as "stay on the
Decimal oracle" (one loud WARN), so a missing wheel can never change a
money value.

Honesty banner (ADR-0020, mandatory verbatim): expected payoff ≈ 0 —
conversion is for uniformity, not speed.
"""

from __future__ import annotations

from brutex_core._brutex_core import (
    __version__,
    format_inr_str,
    paisa_to_rupees_str,
    round_money_str,
    rupees_to_paisa_str,
)

__all__ = [
    "__version__",
    "format_inr_str",
    "paisa_to_rupees_str",
    "round_money_str",
    "rupees_to_paisa_str",
]
