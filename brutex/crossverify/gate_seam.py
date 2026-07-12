"""Narrow, fail-closed binding to the today-completeness classifier.

DEC-CROSSVERIFY-PRODUCER-001 §3.4. The producer codes against a structural
Protocol matching the sibling gate module
(``brutex/data/today_completeness.py``, DEC-PULL-TODAY-COMPLETENESS-GATE-001
MIGRATION.md #1751) and binds it LAZILY: no gate module ⇒
:class:`CompletenessGateUnavailable` ⇒ the run is FAIL-CLOSED (no gate, no
publish — exit 1 + CRITICAL). If the sibling's hostile review changes
signatures, ONLY this adapter moves — the producer NEVER re-implements gate
logic. O(1) per bind; the classifier call itself is the sibling's O(bars).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Protocol

from brutex.models import BruteXError

if TYPE_CHECKING:  # pragma: no cover — typing-only imports
    from collections.abc import Callable, Sequence
    from datetime import date
    from types import ModuleType

    from brutex.data.models import CandleBar

#: The gate module the production binding imports (the sibling's home).
GATE_MODULE: str = "brutex.data.today_completeness"


class VerdictLike(Protocol):
    """Structural subset of the sibling's ``TodayCompletenessVerdict``."""

    @property
    def covers_today(self) -> bool:
        """``True`` iff the bar sequence covers the clamp day."""
        ...

    @property
    def incomplete(self) -> bool:
        """``True`` ⇒ withhold (the producer must NOT publish)."""
        ...

    @property
    def reason(self) -> str:
        """The verdict token the per-class decision table branches on."""
        ...

    @property
    def expected_minutes(self) -> int | None:
        """The session's expected minute count, when known."""
        ...

    @property
    def distinct_minutes(self) -> int | None:
        """The distinct stored minute count, when known."""
        ...

    @property
    def last_bar_ist(self) -> str | None:
        """The last stored bar's IST ``HH:MM``, when known."""
        ...


class TodayCompletenessClassifier(Protocol):
    """The one classifier call shape the producer depends on."""

    def __call__(
        self,
        *,
        bars_today: Sequence[CandleBar],
        clamp_today: date,
        window_end: date,
        instrument_class: str,
    ) -> VerdictLike:
        """Classify one day's bar sequence; ``instrument_class`` is the STR token."""
        ...


class CompletenessGateUnavailable(BruteXError):
    """The gate module is absent — the producer refuses to publish (fail-closed)."""


def bind_production_classifier(
    importer: Callable[[str], ModuleType] = importlib.import_module,
) -> TodayCompletenessClassifier:
    """Bind the REAL gate module lazily, or raise :class:`CompletenessGateUnavailable`.

    The adapter maps the producer's ``instrument_class`` STR token to the
    sibling's ``InstrumentClass`` enum BY VALUE (``"index_spot"`` /
    ``"cash_spot"`` / ``"fut"``). *importer* is injectable so unit tests cover
    both branches without the gate module present.
    """
    try:
        module = importer(GATE_MODULE)
    except ImportError as exc:
        raise CompletenessGateUnavailable(
            f"the today-completeness gate module ({GATE_MODULE}, MIGRATION.md #1751) "
            "is not importable — refusing to publish without the gate (fail-closed)"
        ) from exc
    classify = module.classify_today_completeness
    instrument_class_enum = module.InstrumentClass

    def _classifier(
        *,
        bars_today: Sequence[CandleBar],
        clamp_today: date,
        window_end: date,
        instrument_class: str,
    ) -> VerdictLike:
        verdict: VerdictLike = classify(
            bars_today=bars_today,
            clamp_today=clamp_today,
            window_end=window_end,
            instrument_class=instrument_class_enum(instrument_class),
        )
        return verdict

    return _classifier


__all__ = [
    "GATE_MODULE",
    "CompletenessGateUnavailable",
    "TodayCompletenessClassifier",
    "VerdictLike",
    "bind_production_classifier",
]
