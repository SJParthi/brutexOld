"""brutex_calendar — calendar-leaf decision laws (PyO3 bridge).

``DEC-RUST-CALENDAR-LEAVES-001``: mixed Python + Rust layout (the
``brutex_feed`` / ``trading_core`` shape). The compiled cdylib ships as the
private submodule ``brutex_calendar._brutex_calendar``; this ``__init__.py``
re-exports its symbols so the consumer writes ``import brutex_calendar;
brutex_calendar.validate_settle_evidence(...)``.

Exposed surface (ints/strings/bools/bytes ONLY across the FFI):

* ``classify_month_drift`` — the pure observed-sessions admission/veto/cap
  core over ``(day_iso, zero_bars_all_indices, indices_with_month_data,
  clean_success_windows, failed_windows_touching)`` evidence rows; returns
  ``(candidates, flagged[(day, clean_success_windows)], vetoed_failed,
  no_evidence, over_month_cap, over_year_cap)`` — the caller re-attaches the
  decision-inert ``month`` / ``detected_at`` / ``zero_bar_indices``. The
  counters cross the FFI as i64: a beyond-i64 int (e.g.
  ``existing_year_overlay_days = 2**63``) raises ``OverflowError`` at the
  conversion boundary — the INTENDED loud refusal, never a wrong verdict.
* ``validate_settle_evidence(payload_json, path_repr)`` — the full
  settle-evidence truth table over the RAW artifact bytes; ``None`` =
  ABSTAIN (the Rust JSON engine refused what Python's ``json.loads``
  accepted — NaN/Infinity literals, lone-surrogate escapes, deep nesting
  [measured: from payload depth 127 — total JSON depth 128, serde's
  recursion limit]; degrade to the Python validator), else ``(valid, message,
  adopted_hhmm, generated_at_raw)`` with every refusal ``message``
  BYTE-IDENTICAL to the Python ``ValueError`` text (``path_repr`` is passed
  pre-``repr()``'d by the caller).
* ``parse_observed_sidecar(payload_json)`` — the overlay loader's
  whole-accept/whole-reject decision; ``Some`` = the sorted unique ISO day
  list, ``None`` = the overlay is REJECTED (one bad entry rejects all); a
  JSON parse-engine divergence raises ``ValueError`` (a LOUD abstain — the
  caller degrades to the Python loader, which owns the WARN wording).
* ``SOURCE_DIGEST`` / ``__source_digest__`` — the crate-source BLAKE3 digest
  baked by ``build.rs`` (DEC-RUST-SOURCE-DIGEST-TWIN-001, MIGRATION.md #1258).

Wheel-source freshness handshake (the fno_math/trading_core pattern)
--------------------------------------------------------------------
Import-time guard: the compiled ``.so`` is asserted to expose EVERY symbol
this shim re-exports (``_EXPECTED_COMPILED_SYMBOLS``) BEFORE the re-export
runs, so a stale installed wheel raises a clear, actionable ``ImportError``
naming the missing symbol(s). The handshake is deliberately UNCONDITIONAL —
no env escape; the CONSUMER side stays soft (the Python-side seam catches
this ImportError and degrades to the byte-identical Python oracle).

HONEST residual gap (Golden Rule #10, the sibling wheels' envelope): the
version + symbol-set handshake catches a stale wheel that dropped/added a
symbol or changed crate version; same-version symbol-compatible BEHAVIORAL
drift is not caught at runtime — the probe-generated golden battery in
``rust/brutex-calendar/tests/goldens/`` is the drift gate, at build time.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence

#: Every symbol this shim re-exports from the compiled PyO3 module — the ONE
#: in-code source of truth for the import-time freshness handshake. The
#: in-repo ``EXPECTED_BUILD.json`` mirrors the non-dunder names.
_EXPECTED_COMPILED_SYMBOLS: tuple[str, ...] = (
    "SOURCE_DIGEST",
    "__version__",
    "classify_month_drift",
    "parse_observed_sidecar",
    "validate_settle_evidence",
)


def _assert_compiled_surface(
    compiled: object,
    expected: Sequence[str] = _EXPECTED_COMPILED_SYMBOLS,
) -> None:
    """Raise an actionable ``ImportError`` if ``compiled`` misses any expected symbol."""
    missing = sorted(name for name in expected if not hasattr(compiled, name))
    if missing:
        location = getattr(compiled, "__file__", "<unknown location>")
        raise ImportError(
            "brutex_calendar: the installed compiled wheel "
            f"(brutex_calendar._brutex_calendar at {location}) is STALE — it is missing "
            f"symbol(s) {', '.join(missing)} that this source tree's shim "
            "re-exports. Rebuild + reinstall the wheel from the current source: "
            "run `maturin develop --release` inside rust/brutex-calendar "
            "(with the project venv active)."
        )


_compiled = importlib.import_module("._brutex_calendar", __name__)
_assert_compiled_surface(_compiled)

# The freshness handshake above MUST run before this re-export so a stale
# wheel raises the actionable message, not a bare from-import failure.
from ._brutex_calendar import (  # noqa: E402
    SOURCE_DIGEST,
    __version__,
    classify_month_drift,
    parse_observed_sidecar,
    validate_settle_evidence,
)

#: Crate version of the INSTALLED compiled kernel — derived at import from
#: the compiled module's ``__version__``, NEVER hardcoded here. Deliberately
#: NOT in ``__all__`` (mirrors the siblings): a build stamp, not kernel API.
__build_crate_version__: str = __version__

#: Crate-source BLAKE3 digest of the INSTALLED compiled kernel (FRAMING v1;
#: DEC-RUST-SOURCE-DIGEST-TWIN-001). Same value as ``SOURCE_DIGEST``.
__source_digest__: str = SOURCE_DIGEST

__all__ = [
    "SOURCE_DIGEST",
    "__version__",
    "classify_month_drift",
    "parse_observed_sidecar",
    "validate_settle_evidence",
]
