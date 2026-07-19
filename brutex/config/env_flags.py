"""The ONE env-flag literal vocabulary + tristate classifier (CLAUDE.md §11 COMMON).

DEC-PREBUILD-FRAME-WRITE-001: the truthy/falsy literal sets and the
:func:`env_flag_tristate` classifier historically lived in
:mod:`brutex.engine.sweep_lookahead` (their first home — every sweep env flag
consumed them from there). The prebuild frame-write kill switch
(:mod:`brutex.data.frame_write_gate`) needs the SAME vocabulary, but a
``brutex.data`` leaf must stay WHEEL-FREE: importing ``brutex.engine`` executes
the engine package ``__init__``, whose ``combo_mask`` import requires the
compiled ``fno_math`` Rust wheel — a dependency the data-lake modules
deliberately never carry. So the vocabulary is PROMOTED to this dependency-free
``brutex/config/`` leaf (the coordinator-pre-approved house alias pattern);
:mod:`brutex.engine.sweep_lookahead` re-exports it byte-compatibly, so every
existing ``from brutex.engine.sweep_lookahead import env_flag_tristate``
consumer (``rust_metrics`` / ``rust_stage1a`` / ``sweep_apriori`` /
``results_depth_pass``) is untouched.

Pure stdlib — no brutex imports, no heavy deps; importable from any layer.
"""

from __future__ import annotations

from typing import Final

#: The accepted truthy literals (case-insensitive, whitespace-stripped).
TRUTHY_LITERALS: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

#: The accepted falsy literals (case-insensitive, whitespace-stripped) — the
#: explicit kill-switch vocabulary, the mirror of :data:`TRUTHY_LITERALS`.
#: A default-ON flag (``brutex.engine.rust_metrics``,
#: DEC-SWEEP-RUST-METRICS-DEFAULT-ON-001) recognises an explicit OFF via
#: :func:`env_flag_tristate`; a default-OFF flag needs no falsy branch —
#: anything non-truthy is already OFF.
FALSY_LITERALS: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

__all__ = [
    "FALSY_LITERALS",
    "TRUTHY_LITERALS",
    "env_flag_tristate",
]


def env_flag_tristate(raw: str) -> bool | None:
    """Classify *raw* against the ONE env-flag vocabulary.

    The COMMON classifier for every brutex env flag (CLAUDE.md §11 — the
    truthy/falsy literal sets live HERE, their vocabulary home; the sweep
    flags in :mod:`brutex.engine` and the prebuild frame-write flag in
    :mod:`brutex.data.frame_write_gate` all delegate to this helper instead
    of copying the ``strip().lower()`` + membership logic).

    Parameters
    ----------
    raw
        The raw env value (surrounding whitespace ignored,
        case-insensitive).

    Returns
    -------
    bool | None
        ``True`` for a :data:`TRUTHY_LITERALS` member (``1/true/yes/on``),
        ``False`` for a :data:`FALSY_LITERALS` member (``0/false/no/off``),
        and ``None`` for anything else — unset/empty AND unrecognised both
        map to ``None``; the CALLER owns the default + warn policy for
        those.
    """
    value = raw.strip().lower()
    if value in TRUTHY_LITERALS:
        return True
    if value in FALSY_LITERALS:
        return False
    return None
