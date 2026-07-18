"""The ONE brutex-cascade wheel resolver + flag law (DEC-RUST-CASCADE-RESULTS-001).

The T14 lane converts the cascade / results-depth compute laws to the
``rust/brutex-cascade`` crate under the compressed greenfield cycle
(convert → verify against the FROZEN pre-conversion goldens → default-ON →
DELETE the converted Python law). At this end-state the engine shims
(:mod:`brutex.engine.cascade_join`, :mod:`brutex.engine.minute_path`,
:mod:`brutex.engine.combo_aggregates`) HARD-REQUIRE the compiled wheel — the
``brutex.math`` → ``fno_math`` precedent — resolved through this single leaf.

Flag law (``BRUTEX_CASCADE_RUST``):

* unset / truthy (``1``/``true``/``yes``/``on``, case-insensitive) — the
  Rust laws serve (the default; the flag existed for the conversion cycle).
* explicit falsy (``0``/``false``/``no``/``off``) — a LOUD typed refusal
  naming this DEC: the converted Python implementations are DELETED, so
  there is nothing to fall back to (fail-loud beats a silent pretend-OFF —
  the ``BRUTEX_FILL_MODE`` set-knob-refusal precedent,
  DEC-FILL-WORST-CASE-ONLY-001).
* anything else — a LOUD typed refusal (junk config is never silently
  coerced; the repo-wide fail-loud env parse convention).

A missing / broken wheel is the same refusal class, naming the bootstrap
remediation (``pip install ./rust/brutex-cascade`` /
``scripts/setup/bootstrap.sh``).

O() envelope: :func:`require_cascade_rust` is one ``os.environ`` probe + a
memoised import — O(1) per call after the first.
"""

from __future__ import annotations

import functools
import os
from datetime import UTC, date, datetime
from types import ModuleType
from typing import Final

from brutex.models import BruteXError

__all__ = [
    "CASCADE_RUST_ENV",
    "EPOCH_DATE",
    "CascadeRustError",
    "epoch_day",
    "epoch_us",
    "require_cascade_rust",
]

#: The conversion-cycle flag (see the module docstring for the law).
CASCADE_RUST_ENV: Final[str] = "BRUTEX_CASCADE_RUST"

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSY: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})

_REMEDIATION: Final[str] = (
    "build + install the brutex-cascade wheel: `pip install ./rust/brutex-cascade` "
    "(or run scripts/setup/bootstrap.sh)"
)


class CascadeRustError(BruteXError):
    """Raised when the Rust cascade laws cannot serve (flag off / wheel absent)."""


#: The civil epoch anchor for the crate's day-number marshalling.
EPOCH_DATE: Final[date] = date(1970, 1, 1)

_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)


def epoch_us(ts: datetime) -> int:
    """Exact epoch microseconds of a tz-aware instant (never float-lossy).

    The ONE timestamp-marshalling law of the FFI boundary (a naive instant
    raises ``TypeError`` at the subtraction — the models-layer tz-aware law).
    """
    delta = ts - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def epoch_day(day: date) -> int:
    """Days since 1970-01-01 of a civil date — the day-number marshalling law."""
    return (day - EPOCH_DATE).days


@functools.cache
def _load_module() -> ModuleType:
    """Import the bridge once per process (a raising call is NOT cached —
    installing the wheel mid-process recovers on the next call)."""
    try:
        import brutex_cascade
    except ImportError as exc:
        raise CascadeRustError(
            f"the brutex-cascade Rust wheel is not importable ({exc}); {_REMEDIATION} "
            "(DEC-RUST-CASCADE-RESULTS-001 — the converted Python laws are deleted)"
        ) from exc
    module: ModuleType = brutex_cascade
    return module


def require_cascade_rust() -> ModuleType:
    """Return the ``brutex_cascade`` bridge module, or refuse loudly.

    Raises
    ------
    CascadeRustError
        On an explicit-falsy / junk ``BRUTEX_CASCADE_RUST`` value (the
        deleted-Python end-state has no fallback arm) or a missing/broken
        wheel (remediation named).
    """
    raw = os.environ.get(CASCADE_RUST_ENV)
    if raw is not None:
        token = raw.strip().lower()
        if token in _FALSY:
            raise CascadeRustError(
                f"{CASCADE_RUST_ENV}={raw!r} requests the deleted Python cascade laws — "
                "the conversion cycle is complete (DEC-RUST-CASCADE-RESULTS-001) and the "
                "Rust crate is the ONLY implementation; unset the flag"
            )
        if token not in _TRUTHY:
            raise CascadeRustError(
                f"{CASCADE_RUST_ENV}={raw!r} is not a recognised flag value "
                "(truthy: 1/true/yes/on; the falsy tokens refuse — "
                "DEC-RUST-CASCADE-RESULTS-001)"
            )
    return _load_module()
