"""The Stage-3 Rust optimizer runtime resolver (DEC-RUST-STAGE3-REFINE-001).

The ONE seam through which the stage3 thin wrappers (:mod:`stage3_refine`,
:mod:`stage3_grid`, :mod:`stage3_select`) reach the ``brutex_refine`` wheel
(``rust/brutex-refine`` — the restarted CMA-ES + Mode-A grid bijection +
bounded top-K selection cores). DEFAULT-ON by the compressed-cycle operator
ruling: the pycma Python optimizer math is DELETED in the same lane, so there
is NO Python fallback —

* ``BRUTEX_REFINE_RUST`` unset / blank / truthy (``1/true/yes/on``) resolves
  the wheel;
* a FALSY value (``0/false/no/off``) refuses LOUDLY (a deliberate disable has
  nothing to fall back to — the error names the DEC + the re-enable);
* any OTHER value refuses LOUDLY too (fail-loud junk-env law — never a
  silent guess);
* a missing/stale wheel raises the typed
  :class:`RefineRustUnavailableError` naming the build step, at CALL time
  only — importing this module (or any stage3 wrapper) NEVER touches the
  wheel, so the dashboard boots wheel-free (the brutex_feed guard pattern,
  minus the fallback).

Everything here is O(1) per call: one env read + one ``sys.modules``-cached
import. Deliberately NOT memoized — tests monkeypatch the env per case and a
cached verdict would leak across them.

MAINTAINER NOTE (hostile-review round 1, NIT-2): the wheel is built with the
house ``panic = "abort"`` profile, so an UNGUARDED crate panic does not
surface as a catchable exception — it SIGABRTs the whole Python process. The
crate-side FFI input validation (``RefineDriver::new``/``tell``,
``SelectCore::push`` — every verdict mapped to ``ValueError`` before any core
``assert!`` can fire) is therefore PROCESS-SURVIVAL load-bearing: a
validation regression in the crate is fatal, not degradable, so never treat
those checks as redundant with Python-side guards.
"""

from __future__ import annotations

import importlib
import os
from typing import Final, Protocol, cast

from brutex.models import BruteXError

#: The runtime flag (default ON). Falsy/junk values refuse loudly — the
#: Python optimizer oracle is deleted, so OFF is a refusal, never a fallback.
REFINE_RUST_ENV: Final = "BRUTEX_REFINE_RUST"

#: Recognised truthy spellings (the house env-flag vocabulary — mirrors
#: ``brutex.engine.sweep_lookahead``'s sets; inlined per the feed_rust
#: precedent so this leaf imports nothing heavy, and drift-pinned by tests).
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})
_FALSY: Final = frozenset({"0", "false", "no", "off"})

#: The one remediation string every wheel-absent error carries.
BUILD_HINT: Final = (
    "build + install the wheel with `maturin develop --release` inside "
    "rust/brutex-refine (with the project venv active), or run `make setup`"
)


class RefineRustUnavailableError(BruteXError):
    """The Stage-3 Rust optimizer runtime cannot be engaged.

    Raised for a falsy/unrecognised ``BRUTEX_REFINE_RUST`` value or a
    missing/stale ``brutex_refine`` wheel. There is deliberately NO Python
    fallback (DEC-RUST-STAGE3-REFINE-001 deleted the pycma oracle), so every
    Stage-3 grid/selection/refinement call surfaces this loudly instead of
    degrading.
    """


class RefineSessionLike(Protocol):
    """One mask's restarted-CMA-ES session (`brutex_refine.RefineSession`)."""

    def ask(self) -> list[tuple[int, int]]:
        """Propose the next generation as on-grid ``(sl, tp)`` int pairs."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def tell(self, values: list[float]) -> None:
        """Report the minimization objective values for the last ``ask``."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def done(self) -> bool:
        """Whether every restart has finished."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def restarts_run(self) -> int:
        """Restarts run so far (== ``max_restarts`` once done)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class SelectSessionLike(Protocol):
    """The streaming top-K selection (`brutex_refine.SelectSession`)."""

    def push(
        self, instrument: str, tf: str, mode: str, sharpe: float, run_id: str
    ) -> tuple[bool, int | None]:
        """Stream one row; return ``(kept, evicted_seq)``."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def finish(self) -> list[tuple[tuple[str, str, str], list[int]]]:
        """Emit every board in sorted key order with its kept seqs."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class RefineRuntime(Protocol):
    """The ``brutex_refine`` wheel surface the stage3 wrappers consume."""

    __version__: str
    SOURCE_DIGEST: str

    def RefineSession(  # noqa: N802 — the wheel's pyclass constructor name
        self,
        x0: tuple[float, float],
        sigma0: float,
        seed: int,
        max_fevals: int,
        max_restarts: int,
        sl_axis: tuple[int, int, int],
        tp_axis: tuple[int, int, int],
    ) -> RefineSessionLike:
        """Open one mask's refinement session."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def SelectSession(self, top_k: int) -> SelectSessionLike:  # noqa: N802
        """Open one streaming top-K selection session."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def grid_validate(self, name: str, lo_paisa: int, hi_paisa: int, tick_paisa: int) -> int:
        """Validate one axis; return its step count (ValueError on junk)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def grid_denormalize(
        self, name: str, lo_paisa: int, hi_paisa: int, tick_paisa: int, u: float
    ) -> int:
        """Clamp + snap *u* to the nearest on-grid int paisa."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def grid_normalize(
        self, name: str, lo_paisa: int, hi_paisa: int, tick_paisa: int, paisa: int
    ) -> float:
        """Map an on-grid int paisa to its exact [0, 1] coordinate."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def require_refine_rust() -> RefineRuntime:
    """Resolve the Stage-3 Rust runtime, or raise the typed refusal.

    The flag verdict is re-read per call (cheap; keeps tests hermetic) and
    the wheel import rides ``sys.modules`` caching. Every refusal names the
    DEC + the remediation — never a silent fallback (there is none).

    Raises
    ------
    RefineRustUnavailableError
        Falsy flag (deliberate disable — no fallback exists), unrecognised
        flag value (fail-loud junk-env law), or an unimportable/stale wheel.
    """
    raw = os.environ.get(REFINE_RUST_ENV)
    if raw is not None and raw.strip():
        token = raw.strip().lower()
        if token in _FALSY:
            raise RefineRustUnavailableError(
                f"{REFINE_RUST_ENV}={raw!r} disables the Stage-3 Rust optimizer, but "
                "DEC-RUST-STAGE3-REFINE-001 deleted the Python optimizer math — there is "
                "NO fallback. Unset the flag (or set it truthy) to run Stage-3 "
                "grid/selection/refinement."
            )
        if token not in _TRUTHY:
            raise RefineRustUnavailableError(
                f"{REFINE_RUST_ENV}={raw!r} is not a recognised flag value "
                f"(truthy: {sorted(_TRUTHY)}; falsy: {sorted(_FALSY)}) — refusing "
                "loudly rather than guessing (DEC-RUST-STAGE3-REFINE-001)."
            )
    try:
        module = importlib.import_module("brutex_refine")
    except ImportError as exc:
        raise RefineRustUnavailableError(
            "the brutex_refine wheel is not importable — Stage-3 "
            "grid/selection/refinement needs it (DEC-RUST-STAGE3-REFINE-001; the "
            f"Python optimizer math is deleted). Remedy: {BUILD_HINT}. "
            f"(import failed: {exc})"
        ) from exc
    return cast(RefineRuntime, module)


def optimizer_versions() -> tuple[str, str]:
    """Return ``(optimizer_version, optimizer_build)`` — the identity tokens.

    ``optimizer_version`` is ``brutex-refine-<crate semver>``;
    ``optimizer_build`` is the wheel's baked crate-source BLAKE3 digest
    (DEC-RUST-SOURCE-DIGEST-TWIN-001), so ANY optimizer source change re-keys
    every refinement-run identity (Golden Rule #4) even at an unchanged
    version number — the load the retired ``(pycma, numpy)`` pair carried.

    Raises
    ------
    RefineRustUnavailableError
        When the runtime cannot be engaged (see :func:`require_refine_rust`).
    """
    module = require_refine_rust()
    return f"brutex-refine-{module.__version__}", str(module.SOURCE_DIGEST)


__all__ = [
    "BUILD_HINT",
    "REFINE_RUST_ENV",
    "RefineRuntime",
    "RefineRustUnavailableError",
    "RefineSessionLike",
    "SelectSessionLike",
    "optimizer_versions",
    "require_refine_rust",
]
