"""The wheel-REQUIRED feed-materialization seam (DEC-FEED-RUST-MATERIALIZE-002).

The ``brutex_feed`` Rust wheel carries the DECISION plane of the feed
package — the strict scan behind
:func:`brutex.feed.replay.arrow_table_to_bars` (finiteness + positivity
refusal order + the bad-tick keep/skip screen), the P-06
``LookAheadBarrier`` state machine, and the ``series_load`` decision
arithmetic — each returning INTEGERS ONLY while Python keeps ALL
materialization (``Decimal``/``CandleBar``/error f-strings/structlog
WARNs), so run identity (``bxsnap2``, CLAUDE.md Golden Rule #4) is
byte-preserved by construction.

This module owns the seam law:

* :func:`require_feed_kernel` — the ONE loud resolver. The Python oracle
  bodies were DELETED (the T17 zero-Python endgame), so a missing/stale
  wheel is a typed :class:`~brutex.feed.errors.FeedError` naming the
  rebuild remediation — never a silent degrade, never a fabricated bar.
* :func:`require_materialize_flags` — the retired-axis flag law (the
  ``BRUTEX_FILL_MODE`` precedent, DEC-FILL-WORST-CASE-ONLY-001): a SET
  ``BRUTEX_FEED_MATERIALIZE_RUST`` or legacy ``BRUTEX_FEED_RUST`` value
  that asks for the deleted Python arm (explicit falsy) — or that cannot
  be interpreted (junk) — REFUSES loudly naming the DEC; unset/empty and
  explicit truthy proceed.
* :func:`validate_strict_scan` — the refuse-poison structural gate over
  the strict kernel's output (the ``rust_metrics._valid_drawdown``
  precedent): a stale/corrupt wheel whose output violates ANY shape law
  makes the caller RAISE — a poisoned wheel can only refuse, never serve.

SUPERSEDES the DEC-FEED-RUST-CORE-001 two-arm machinery that lived here
(``feed_rust_enabled`` / ``resolve_feed_kernel`` / ``validate_scan`` /
the ``warn_degrade_once`` registry): with the oracle deleted there is
nothing to degrade TO, so every former degrade token is now a loud typed
refusal at its seam.

LEAF DISCIPLINE (verified import cycle): ``brutex.feed.__init__`` imports
``replay`` which imports THIS module, so module scope stays stdlib-only
(no ``brutex.*`` / third-party edge — the inline ``_TRUTHY``/``_FALSY``
vocabulary is the ``bad_tick.py`` precedent, mirror-pinned against
``sweep_lookahead``'s sets by a test). The typed ``FeedError`` refusals
import ``brutex.feed.errors`` LAZILY in-function — cycle-safe because
every caller reaches them only after the package is fully imported.
"""

from __future__ import annotations

import os
from typing import Final

#: The RETIRED DEC-FEED-RUST-CORE-001 opt-in flag. Both its arms collapsed
#: into the wheel-required strict path; a SET falsy/junk value now refuses
#: loudly (see :func:`require_materialize_flags`).
FEED_RUST_ENV: Final[str] = "BRUTEX_FEED_RUST"

#: The T17 materialization flag (DEC-FEED-RUST-MATERIALIZE-002) — ON is
#: the only shipping arm; a SET falsy/junk value refuses loudly.
MATERIALIZE_RUST_ENV: Final[str] = "BRUTEX_FEED_MATERIALIZE_RUST"

#: The lane's decision id — woven into every wheel-required refusal.
MATERIALIZE_DECISION: Final[str] = "DEC-FEED-RUST-MATERIALIZE-002"

#: The ONE named remediation for a missing/stale wheel.
WHEEL_REMEDIATION: Final[str] = (
    "run `maturin develop --release` inside rust/brutex-feed with the "
    "project venv active (or `make setup`)"
)

#: Inline flag vocabulary (LEAF discipline — see the module docstring; the
#: mirror-pin test asserts byte-equality with ``sweep_lookahead``'s sets).
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

#: Structural arities of the strict kernel result (the frozen FFI shape).
_RESULT_ARITY: Final[int] = 3
_STRICT_STOP_ARITY: Final[int] = 3
_EVENT_ARITY: Final[int] = 5

#: Highest stop field index (0/1/2/3 = open/high/low/close).
_MAX_FIELD_INDEX: Final[int] = 3

#: One kernel warn event: ``(emit_at_row, session_first_row, skipped,
#: first_prev_row, first_skip_row)`` — integer row coordinates only.
ScanEvent = tuple[int, int, int, int, int]

#: The STRICT kernel's stop coordinate: ``(row, field, kind)`` — kind is
#: :data:`STOP_NONPOSITIVE` or :data:`STOP_NONFINITE`.
StrictStop = tuple[int, int, int]

#: The strict kernel's outcome (DEC-FEED-RUST-MATERIALIZE-002):
#: ``(kept_rows, warn_events, stop)``.
StrictScanResult = tuple[list[int], list[ScanEvent], StrictStop | None]

#: Strict-stop kind: a non-positive price (Python raises the verbatim
#: ``_positivity_error``).
STOP_NONPOSITIVE: Final[int] = 0

#: Strict-stop kind: a non-finite price (Python raises the verbatim
#: ``_nonfinite_error``).
STOP_NONFINITE: Final[int] = 1


def _feed_error(message: str) -> Exception:
    """Build a :class:`brutex.feed.errors.FeedError` via a LAZY import.

    The leaf discipline forbids a MODULE-scope ``brutex.feed`` import
    (``brutex.feed.__init__`` imports ``replay`` which imports THIS module
    — a module-scope edge here would be a cycle); an in-FUNCTION import is
    cycle-safe because every caller reaches this only after the package is
    fully imported. The exception TYPE matters: consumers catch the typed
    ``FeedError`` family, never a bare RuntimeError.
    """
    from brutex.feed.errors import FeedError

    return FeedError(message)


def require_materialize_flags() -> None:
    """The retired-axis flag law — refuse a value asking for the dead arm.

    DEC-FEED-RUST-MATERIALIZE-002 deleted the Python feed-materialization
    oracle bodies, so there is no alternate arm any flag could select:

    * UNSET / empty / explicit truthy — proceed (the shipping Rust arm);
    * explicit FALSY — the value asks for the deleted Python arm: a loud
      typed :class:`~brutex.feed.errors.FeedError` naming the DEC and the
      remediation (unset the variable);
    * JUNK — uninterpretable: the same loud refusal (fail-loud beats a
      silently ignored operator intent — the ``BRUTEX_FILL_MODE``
      retired-axis precedent).

    Both the lane flag (:data:`MATERIALIZE_RUST_ENV`) and the retired
    DEC-FEED-RUST-CORE-001 flag (:data:`FEED_RUST_ENV`) are checked —
    the legacy flag's falsy/unset arm used to mean "the Python oracle",
    which no longer exists; its truthy arm meant "the Rust kernel", now
    the only arm, so truthy is a harmless no-op.
    """
    for env in (MATERIALIZE_RUST_ENV, FEED_RUST_ENV):
        value = os.environ.get(env)
        if value is None:
            continue
        stripped = value.strip().lower()
        if stripped == "" or stripped in _TRUTHY:
            continue
        asks = (
            "asks for the DELETED Python feed-materialization arm"
            if stripped in _FALSY
            else "is unrecognised (and no alternate arm exists to select)"
        )
        raise _feed_error(
            f"{env}={value!r} {asks}: the Python oracle bodies were removed "
            f"by {MATERIALIZE_DECISION} and feed materialization is "
            f"Rust-only — unset {env} (truthy/unset proceed)"
        )


def require_feed_kernel() -> object:
    """Return the ``brutex_feed`` wheel module, or raise LOUDLY.

    DEC-FEED-RUST-MATERIALIZE-002: the Python feed-materialization oracle
    bodies are deleted, so the wheel is REQUIRED — a missing/stale wheel
    is a typed :class:`~brutex.feed.errors.FeedError` naming the
    remediation, never a silent degrade and never a fabricated bar. The
    retired-axis flag law runs FIRST so a set-falsy flag refuses with its
    own named message even on a wheel-less box.
    """
    require_materialize_flags()
    try:
        import brutex_feed
    except (ImportError, AttributeError) as exc:
        raise _feed_error(
            f"the brutex_feed Rust wheel is REQUIRED for feed "
            f"materialization ({MATERIALIZE_DECISION} deleted the Python "
            f"oracle bodies) but it failed to import: {exc} — "
            f"{WHEEL_REMEDIATION}"
        ) from exc
    return brutex_feed


def validate_strict_scan(result: object, n_rows: int) -> str | None:
    """Refuse-poison gate over a STRICT kernel result — ``None`` = valid,
    else the NAMED violated-law token (woven into the caller's raise).

    Structural laws (proven for every well-formed input by the crate's own
    test suite — a violation here can only mean a stale/corrupt wheel):
    the 3-tuple shape; ``kept_rows`` ints strictly ascending in
    ``[0, n_rows)``; ``warn_events`` 5-int tuples strictly ascending by
    ``emit_at_row`` (a finish event anchors AT ``n_rows``) with the
    row-coordinate range laws; ``stop`` ``None`` or ``(row, field, kind)``
    with ``row in [0, n_rows)``, ``field in [0, 3]`` and ``kind`` in
    ``{STOP_NONPOSITIVE, STOP_NONFINITE}`` — and, when present, the
    truncation law (nothing recorded at/after the stop row). Negative or
    out-of-range indices are refused OUTRIGHT — a negative index would
    silently wrap Python list selection. The wheel-required caller RAISES
    on a non-``None`` token (a poisoned wheel can only refuse, never
    serve).
    """
    if not (isinstance(result, tuple) and len(result) == _RESULT_ARITY):
        return "result_shape"
    kept, warns, stop = result
    if not (isinstance(kept, list) and isinstance(warns, list)):
        return "column_shape"
    if stop is None:
        kept_bound = n_rows
        emit_bound = n_rows + 1  # a finish event anchors AT n_rows
        row_bound = n_rows
    else:
        if not (isinstance(stop, tuple) and len(stop) == _STRICT_STOP_ARITY):
            return "stop_shape"
        stop_row, stop_field, stop_kind = stop
        if not all(isinstance(v, int) for v in (stop_row, stop_field, stop_kind)):
            return "stop_types"
        if not (0 <= stop_row < n_rows and 0 <= stop_field <= _MAX_FIELD_INDEX):
            return "stop_range"
        if stop_kind not in (STOP_NONPOSITIVE, STOP_NONFINITE):
            return "stop_kind"
        kept_bound = stop_row
        emit_bound = stop_row
        row_bound = stop_row
    prev_kept = -1
    for idx in kept:
        if not isinstance(idx, int):
            return "kept_type"
        if idx <= prev_kept or idx >= kept_bound:
            return "kept_order"
        prev_kept = idx
    prev_emit = -1
    for event in warns:
        if not (isinstance(event, tuple) and len(event) == _EVENT_ARITY):
            return "event_shape"
        emit, session_first, skipped, first_prev, first_skip = event
        if not all(isinstance(v, int) for v in event):
            return "event_types"
        if emit <= prev_emit or emit >= emit_bound:
            return "event_order"
        if not 0 <= session_first < row_bound:
            return "session_first_range"
        if skipped < 1:
            return "skipped_floor"
        if not 0 <= first_prev < first_skip < row_bound:
            return "offender_range"
        prev_emit = emit
    return None


__all__ = [
    "FEED_RUST_ENV",
    "MATERIALIZE_DECISION",
    "MATERIALIZE_RUST_ENV",
    "STOP_NONFINITE",
    "STOP_NONPOSITIVE",
    "WHEEL_REMEDIATION",
    "ScanEvent",
    "StrictScanResult",
    "StrictStop",
    "require_feed_kernel",
    "require_materialize_flags",
    "validate_strict_scan",
]
