"""ALPHA-vs-BUY&HOLD benchmark metric (DEC-ALPHA-BUYHOLD-METRIC-001).

The operator-ordered institutional precision layer: for every persisted
strategy candidate, ``alpha_pct`` = the strategy's percent return on its
sweep notional MINUS the instrument's buy-&-hold percent return over the
SAME window. A DISPLAY metric only — additive on the top100 artifact, never
in run identity, never in the 60-condition vocabulary.

## The one formula (units + window law — the comparability verdict)

Strategy P&L is signal-only gross **int paisa** on the sweep notional
``grid.start_equity_paisa``; the R27 buy-&-hold baseline
(:mod:`brutex.engine.baseline`) is a notional-free **price return** of the
instrument (anchor = first session's first bar OPEN, close = last session's
last bar CLOSE — both from the sweep's OWN bars). The two are NOT directly
comparable in paisa (the baseline has no lot/notional), so alpha is defined
in RETURN-PERCENT terms::

    strategy_return_pct = float(net_pnl_paisa) / float(start_equity_paisa) * 100.0
    bh_return_pct       = float(last_close_paisa - anchor_open_paisa)
                          / float(anchor_open_paisa) * 100.0
    alpha_pct           = strategy_return_pct - bh_return_pct

All four inputs are exact integers (prices as 2-dp tick-grid paisa — the
lake's own grid, via :func:`price_to_paisa`); the ``float(int)`` conversions
are EXPLICIT and load-bearing: convert-first (IEEE round-to-nearest,
ties-to-even) then divide fixes the op order so the Rust kernel
(``fno_math.alpha_vs_buyhold_pct`` — ``as f64`` then ``/``) is BIT-IDENTICAL
to this oracle (a bare ``int / int`` would be ONE correctly-rounded
exact-rational division, which differs for |values| > 2**53). Window law:
both legs derive from the sweep's own loaded bars over the grid window
(the R27 same-bars law) — same instrument, same window, by construction.

## BORN-RUST + the retained oracle

The computation lives in the ``rust/fno-math`` kernel
(``alpha_vs_buyhold_pct``, an additive registration — NO crate version
bump, the DEC-ENUM-KERNEL-U128-001 precedent; run identity /
``_indicator_version`` untouched). This module retains the verbatim
pure-Python law as the LIVE parity oracle
(:func:`alpha_vs_buyhold_pct_python`) and routes through the kernel when
the wheel is importable: wheel-missing degrades to the oracle with ONE
``alpha_metric.kernel_unavailable`` warning per process (the standard
facade pattern); a kernel raise or a shape/value-poisoned result degrades
the same way (refuse-poison — a poisoned wheel can only degrade, never
serve a wrong number: :func:`compute_alpha_values` cross-checks the kernel
value against the leg subtraction, which any bit-identical kernel satisfies
by construction). ``MemoryError`` re-raises raw. HONEST (Golden Rule #10):
payoff ≈ 0 — seven IEEE ops per candidate, the FFI hop costs more than the
arithmetic; the kernel exists for the BORN-RUST uniformity mandate.

## None-handling (Golden Rule #1)

A candidate whose instrument has NO baseline for the window carries NO
alpha — the writer omits the keys entirely and every display surface
renders the honest "—", NEVER a fabricated 0.

## The rejection gate (DEFAULT-OFF, operator-gated)

:data:`ALPHA_GATE_ENV` (``BRUTEX_ALPHA_GATE``) — unset/blank/falsy = OFF
(byte-identical artifacts); truthy = the top100 BUILD excludes entries whose
COMPUTABLE alpha is <= 0, counted + named (never silent truncation). Junk
raises (the fail-loud env convention). ARTIFACT-LEVEL scope ONLY: the gate
runs inside :func:`brutex.engine.top100_artifact.build_top100` — persisted
tuple stores, run identity and the ``sweep_identity`` tuple digest are
byte-untouched (the gate is post-hoc display selection, deliberately NOT a
store-address fold — the honest, simpler scope; flipping it ON is an
operator action recorded in MIGRATION.md #2057).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

import structlog

from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.models import BruteXError
from brutex.money import round_money

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Callable, Mapping
    from decimal import Decimal

    from brutex.engine.baseline import BaselineSeries

_LOG = structlog.get_logger("brutex.engine.alpha_metric")

__all__ = [
    "ALPHA_GATE_ENV",
    "AlphaBaseline",
    "AlphaMetricError",
    "AlphaValues",
    "alpha_baseline_from_series",
    "alpha_vs_buyhold_pct",
    "alpha_vs_buyhold_pct_python",
    "compute_alpha_values",
    "price_to_paisa",
    "reset_alpha_kernel_for_tests",
    "resolve_alpha_gate_enabled",
]

#: The DEFAULT-OFF alpha rejection-gate switch (module docstring).
ALPHA_GATE_ENV: Final[str] = "BRUTEX_ALPHA_GATE"

#: The i64 FFI envelope — values outside it never cross into the kernel
#: (they cannot arise from real paisa magnitudes; refused loudly).
_I64_MIN: Final[int] = -(2**63)
_I64_MAX: Final[int] = 2**63 - 1

#: Kernel resolution memo: ``False`` = unresolved; ``None`` = resolved
#: absent/disabled (oracle serves); a callable = the live kernel.
_KERNEL_MEMO: dict[str, object] = {"fn": False}

#: Warn-once memo (per process) for the degrade log lines.
_WARNED: Final[set[str]] = set()


class AlphaMetricError(BruteXError):
    """Raised on a domain violation (non-int input, non-positive denominator)."""


def _validate_inputs(
    net_pnl_paisa: int,
    start_equity_paisa: int,
    anchor_open_paisa: int,
    last_close_paisa: int,
) -> None:
    """The ONE domain law both arms share (the kernel mirrors it verbatim).

    Every input must be a genuine int (bool refused) inside the i64 FFI
    envelope; the two denominators must be >= 1 and the close >= 0 (a
    stored price is never negative). Raises :class:`AlphaMetricError`
    naming the field — the kernel's own guards are structurally
    unreachable through this facade.
    """
    for name, value in (
        ("net_pnl_paisa", net_pnl_paisa),
        ("start_equity_paisa", start_equity_paisa),
        ("anchor_open_paisa", anchor_open_paisa),
        ("last_close_paisa", last_close_paisa),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise AlphaMetricError(f"alpha_vs_buyhold_pct: {name} must be an int, got {value!r}")
        if not _I64_MIN <= value <= _I64_MAX:
            raise AlphaMetricError(
                f"alpha_vs_buyhold_pct: {name} outside the i64 envelope, got {value}"
            )
    if start_equity_paisa < 1:
        raise AlphaMetricError(
            f"alpha_vs_buyhold_pct: start_equity_paisa must be >= 1, got {start_equity_paisa}"
        )
    if anchor_open_paisa < 1:
        raise AlphaMetricError(
            f"alpha_vs_buyhold_pct: anchor_open_paisa must be >= 1, got {anchor_open_paisa}"
        )
    if last_close_paisa < 0:
        raise AlphaMetricError(
            f"alpha_vs_buyhold_pct: last_close_paisa must be >= 0, got {last_close_paisa}"
        )


def _strategy_return_pct(net_pnl_paisa: int, start_equity_paisa: int) -> float:
    """The strategy leg — convert-FIRST then divide (the bit-identity law)."""
    return float(net_pnl_paisa) / float(start_equity_paisa) * 100.0


def _bh_return_pct(anchor_open_paisa: int, last_close_paisa: int) -> float:
    """The buy-&-hold leg — exact int subtraction, convert-first division."""
    return float(last_close_paisa - anchor_open_paisa) / float(anchor_open_paisa) * 100.0


def alpha_vs_buyhold_pct_python(
    net_pnl_paisa: int,
    start_equity_paisa: int,
    anchor_open_paisa: int,
    last_close_paisa: int,
) -> float:
    """The LIVE pure-Python parity oracle (the module-docstring formula, verbatim).

    Bit-identical to the Rust kernel by the convert-first fixed-op-order law
    (proven by the hypothesis differential over the hostile ±2**60-paisa
    domain). Raises :class:`AlphaMetricError` on a domain violation.
    """
    _validate_inputs(net_pnl_paisa, start_equity_paisa, anchor_open_paisa, last_close_paisa)
    strat_pct = _strategy_return_pct(net_pnl_paisa, start_equity_paisa)
    bh_pct = _bh_return_pct(anchor_open_paisa, last_close_paisa)
    return strat_pct - bh_pct


def _warn_once(event: str, **context: object) -> None:
    """Log *event* at WARNING exactly once per process (the facade idiom)."""
    if event in _WARNED:
        return
    _WARNED.add(event)
    _LOG.warning(event, **context)


def _resolve_kernel() -> Callable[[int, int, int, int], object] | None:
    """Lazily resolve the fno_math kernel; absent/stale = ``None`` (oracle).

    Memoized per process. Wheel-missing (ImportError — incl. the shim's own
    stale-wheel handshake refusal) or a symbol-less foreign module each log
    ONE ``alpha_metric.kernel_unavailable`` and serve the oracle forever —
    graceful degrade, never a crash (the standard facade pattern).
    """
    cached = _KERNEL_MEMO["fn"]
    if cached is not False:
        return cached  # type: ignore[return-value]  # memo holds None | callable
    try:
        import fno_math
    except ImportError as exc:
        _warn_once(
            "alpha_metric.kernel_unavailable",
            reason="wheel_missing",
            error=str(exc),
            note="alpha served by the pure-Python oracle (bit-identical by construction)",
        )
        _KERNEL_MEMO["fn"] = None
        return None
    candidate: object = getattr(fno_math, "alpha_vs_buyhold_pct", None)
    if not callable(candidate):
        _warn_once(
            "alpha_metric.kernel_unavailable",
            reason="symbol_missing",
            note="installed fno_math lacks alpha_vs_buyhold_pct — oracle serves",
        )
        _KERNEL_MEMO["fn"] = None
        return None
    kernel = cast("Callable[[int, int, int, int], object]", candidate)
    _KERNEL_MEMO["fn"] = kernel
    return kernel


def _disable_kernel(reason: str, **context: object) -> None:
    """Latch the kernel OFF for the process (refuse-poison / fault law)."""
    _KERNEL_MEMO["fn"] = None
    _warn_once("alpha_metric.kernel_disabled", reason=reason, **context)


def reset_alpha_kernel_for_tests() -> None:
    """Reset the kernel memo + warn-once registry (test isolation seam)."""
    _KERNEL_MEMO["fn"] = False
    _WARNED.clear()


def alpha_vs_buyhold_pct(
    net_pnl_paisa: int,
    start_equity_paisa: int,
    anchor_open_paisa: int,
    last_close_paisa: int,
) -> float:
    """ALPHA vs buy-&-hold, percent — kernel-routed with the oracle fallback.

    Validates the shared domain FIRST (:func:`_validate_inputs` — one law,
    both arms), then serves the Rust kernel when importable, else the
    retained oracle. A kernel raise or a non-float result latches the
    kernel OFF (one warn) and serves the oracle — a broken/poisoned wheel
    can only degrade, never crash nor serve junk. ``MemoryError`` re-raises
    raw (a global stop is never swallowed).
    """
    _validate_inputs(net_pnl_paisa, start_equity_paisa, anchor_open_paisa, last_close_paisa)
    kernel = _resolve_kernel()
    if kernel is not None:
        try:
            result = kernel(net_pnl_paisa, start_equity_paisa, anchor_open_paisa, last_close_paisa)
        except MemoryError:
            raise
        except Exception as exc:
            _disable_kernel("kernel_raised", error_type=type(exc).__name__, error=str(exc))
        else:
            if type(result) is float:
                return result
            _disable_kernel("result_not_float", result_type=type(result).__name__)
    return alpha_vs_buyhold_pct_python(
        net_pnl_paisa, start_equity_paisa, anchor_open_paisa, last_close_paisa
    )


@dataclass(frozen=True, slots=True)
class AlphaBaseline:
    """The R27 baseline's two alpha inputs as exact 2-dp-paisa integers.

    ``anchor_open_paisa`` = the first session's first bar OPEN;
    ``last_close_paisa`` = the last session's last bar CLOSE — both snapped
    to the lake's own 2-dp tick grid (:func:`price_to_paisa`).
    """

    anchor_open_paisa: int
    last_close_paisa: int

    def __post_init__(self) -> None:
        if self.anchor_open_paisa < 1:
            raise AlphaMetricError(
                f"AlphaBaseline.anchor_open_paisa must be >= 1, got {self.anchor_open_paisa}"
            )
        if self.last_close_paisa < 0:
            raise AlphaMetricError(
                f"AlphaBaseline.last_close_paisa must be >= 0, got {self.last_close_paisa}"
            )


@dataclass(frozen=True, slots=True)
class AlphaValues:
    """One candidate's alpha triple — the three additive display metrics.

    ``alpha_pct`` is EXACTLY ``strategy_return_pct - bh_return_pct`` (one
    IEEE subtraction — the structural exact-subtraction law, enforced by
    :func:`compute_alpha_values`'s refuse-poison cross-check).
    """

    strategy_return_pct: float
    bh_return_pct: float
    alpha_pct: float


def price_to_paisa(value: Decimal) -> int:
    """Snap a rupee price to the 2-dp tick grid and convert to exact int paisa.

    ``round_money`` (the ONE DEC-MONEY-2DP-001 law) quantises to two
    places; ``scaleb(2)`` then shifts the exact Decimal into integral paisa
    — no float ever touches the value. Stored lake prices are already
    2-dp-snapped, so the snap is an identity there (belt, not surgery).
    """
    return int(round_money(value).scaleb(2))


def alpha_baseline_from_series(series: BaselineSeries) -> AlphaBaseline:
    """Project a computed :class:`~brutex.engine.baseline.BaselineSeries`.

    Anchor = ``series.anchor_open``; close = the LAST session's close —
    exactly the two prices ``total_return_pct`` itself derives from (the
    same-window law by construction).
    """
    return AlphaBaseline(
        anchor_open_paisa=price_to_paisa(series.anchor_open),
        last_close_paisa=price_to_paisa(series.sessions[-1][1]),
    )


def compute_alpha_values(
    net_pnl_paisa: int, start_equity_paisa: int, baseline: AlphaBaseline
) -> AlphaValues:
    """One candidate's alpha triple — kernel-routed alpha + Python legs.

    The legs are the SAME two sub-expressions the kernel evaluates
    (bit-identical by the convert-first law), so ``alpha_pct`` from a
    correct kernel EQUALS their subtraction exactly. The cross-check is the
    refuse-poison belt: a kernel value that fails it latches the kernel OFF
    (one warn) and the oracle's subtraction serves — a poisoned wheel can
    only degrade, never ship a wrong alpha (Golden Rule #1).
    """
    alpha = alpha_vs_buyhold_pct(
        net_pnl_paisa,
        start_equity_paisa,
        baseline.anchor_open_paisa,
        baseline.last_close_paisa,
    )
    strat_pct = _strategy_return_pct(net_pnl_paisa, start_equity_paisa)
    bh_pct = _bh_return_pct(baseline.anchor_open_paisa, baseline.last_close_paisa)
    expected = strat_pct - bh_pct
    if alpha != expected:
        _disable_kernel(
            "alpha_cross_check_failed",
            kernel_alpha=alpha,
            expected=expected,
            note="the leg-subtraction oracle value ships; the kernel is latched OFF",
        )
        alpha = expected
    return AlphaValues(strategy_return_pct=strat_pct, bh_return_pct=bh_pct, alpha_pct=alpha)


def resolve_alpha_gate_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Resolve the DEFAULT-OFF :data:`ALPHA_GATE_ENV` switch — fail-loud on junk.

    unset / ``""`` → ``False`` (the artifact is byte-identical — flipping
    the gate ON is an operator action); truthy/falsy tokens per the ONE
    sweep env vocabulary (:func:`~brutex.engine.sweep_lookahead.env_flag_tristate`
    — never copied); anything else raises (the fail-loud env convention).

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Raises
    ------
    ValueError
        If the value is set to something outside the two token sets.
    """
    mapping = os.environ if env is None else env
    raw = mapping.get(ALPHA_GATE_ENV, "")
    if not raw.strip():
        return False
    flag = env_flag_tristate(raw)
    if flag is None:
        raise ValueError(
            f"{ALPHA_GATE_ENV}={raw!r} is not a recognised switch value; expected one of "
            "'1', 'on', 'true', 'yes' (enable the alpha rejection gate) or "
            "'0', 'off', 'false', 'no' (disable — the default when unset)"
        )
    return flag
