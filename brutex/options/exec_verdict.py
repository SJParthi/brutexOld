"""The per-entry ``options_exec`` verdict — the post-sweep options-sim fold.

`DEC-OPTIONS-EXEC-FEEDBACK-001` (W4 item #10). The automatic post-sweep
options sim (``cli._maybe_run_exec_sims_on_winners`` →
:func:`brutex.options.executor.execute_options_sim`) already prices every top
winner's replayed signals under the 7 offset strike rules — but its output
previously died on stdout. This leaf folds ONE winner's rule results into the
ADDITIVE per-entry ``options_exec`` object persisted into ``top100.json``
(the cascade-object precedent — annotation only, R13: never re-ranks or
hides).

## The headline rule = ATM (decided, disclosed)

The persisted headline is the **ATM rule** (offset 0 — the canonical
zero-parameter expression of "translate this signal to an option"), named via
``rule_name``. Deliberately NOT best-of-7: picking the best net AFTER seeing
each rule's P&L is in-sample selection bias on the execution leg — exactly
the overfit class the cascade punishes — and rule comparisons are honest
within-verified-window only (the executor docstring's date-keyed-truncation
law). All 7 rules stay fully visible on stdout + the per-rule audit rows.

## ETC honesty (Golden Rule #1)

Net-after-cost is priceable only within the VERIFIED ETC window (≥ 2024-10-01
— DEC-ETC-REGIME-HISTORY-001): a pre-window signal is a COUNTED
``cost_regime_unverified`` reject that depresses ``priced_trade_pct``, and the
``window.etc_verified_from`` boundary is DERIVED from
:func:`brutex.costs.constants.etc_unverified_windows` (the regime-table DATA,
the ``_render_options_leaderboard`` max-refusal-end law) — never hardcoded,
never an invented rate.

Pure — no lake I/O; the pragma'd CLI hook only composes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Sequence

    from brutex.options.executor import OptionsRuleResult

__all__ = [
    "OPTIONS_EXEC_HEADLINE_RULE",
    "OPTIONS_EXEC_NOT_PRICED_REASONS",
    "OPTIONS_EXEC_STATUS_NOT_PRICED",
    "OPTIONS_EXEC_STATUS_PRICED",
    "etc_verified_from_token",
    "options_exec_not_priced",
    "options_exec_verdict",
]

#: The persisted headline strike rule (module docstring — ATM, offset 0).
OPTIONS_EXEC_HEADLINE_RULE: Final[str] = "atm"

#: Status vocabulary of the ``options_exec`` object.
OPTIONS_EXEC_STATUS_PRICED: Final[str] = "priced"
OPTIONS_EXEC_STATUS_NOT_PRICED: Final[str] = "not_priced"

#: The NAMED not-priced reasons a wired provider may emit: the entry was not
#: in the replayed top-K winner set, its replay raised (caught + skipped), or
#: the replay produced zero signals. Each is an honest disclosure — never a
#: fabricated verdict.
OPTIONS_EXEC_NOT_PRICED_REASONS: Final[tuple[str, ...]] = (
    "not_in_replay_set",
    "replay_failed",
    "no_signals",
)


def options_exec_not_priced(reason: str) -> dict[str, object]:
    """The NAMED degrade object of a wired-but-unpriced entry.

    Raises
    ------
    ValueError
        On a reason outside :data:`OPTIONS_EXEC_NOT_PRICED_REASONS` — the
        vocabulary is the wire contract; a typo'd reason fails loud at the
        writer, never ships an unknown token.
    """
    if reason not in OPTIONS_EXEC_NOT_PRICED_REASONS:
        raise ValueError(
            f"options_exec_not_priced: unknown reason {reason!r}; expected one of "
            f"{OPTIONS_EXEC_NOT_PRICED_REASONS}"
        )
    return {"status": OPTIONS_EXEC_STATUS_NOT_PRICED, "reason": reason}


def etc_verified_from_token() -> str | None:
    """The verified-ETC boundary date, DERIVED from the regime-table DATA.

    The ``_render_options_leaderboard`` max-refusal-end law: the latest CLOSED
    refusal-window end across exchanges, ISO-formatted — the first date every
    exchange's ETC rate is verified from. ``None`` if the table ever reads no
    closed refusal window (never a hardcoded literal — kill-the-constant law).
    """
    from brutex.costs.constants import etc_unverified_windows  # lazy — cycle-safe

    ends = [end for spans in etc_unverified_windows().values() for _start, end in spans if end]
    if not ends:
        return None
    return max(ends).isoformat()


def options_exec_verdict(
    results: Sequence[OptionsRuleResult],
    *,
    expiry_kind: str,
    broker: str,
    n_lots: int = 1,
    replay_start: str,
    replay_end: str,
) -> dict[str, object]:
    """Fold one winner's per-rule options results into the ``options_exec`` object.

    Parameters
    ----------
    results
        The :func:`~brutex.options.executor.execute_options_sim` output (one
        row per swept rule) — the headline row is selected by
        :data:`OPTIONS_EXEC_HEADLINE_RULE`.
    expiry_kind, broker, n_lots
        The run-level sim parameters, disclosed verbatim (``n_lots`` is the
        sizing hypothetical — the ``margin_roi`` lots=1 precedent, Ruling 18).
    replay_start, replay_end
        The sweep window the signals were replayed over (the honest coverage
        envelope beside ``etc_verified_from``).

    Returns
    -------
    dict[str, object]
        The priced object (module docstring). Null semantics — the honesty
        core: ``priced_trade_pct`` (an EXACT float unit projection
        ``n_accepted / n_signals * 100.0`` — the ``cpcv_pass_pct`` law) is
        ``null`` when ``n_signals == 0``; ``drag_per_trade_paisa``
        (``(gross − net) / n_accepted`` full-precision — per-accepted-trade
        charges under the calculator identity ``net = gross − charges``) and
        ``sign_flip`` (``gross > 0 and net <= 0`` — the leg was
        gross-profitable but the cost stack flipped it) are ``null`` when
        ``n_accepted == 0``. Never a fabricated 0/false.

    Raises
    ------
    ValueError
        When no result row carries the headline rule — a code defect at the
        composing hook (the executor's default rules always include ATM),
        surfaced loudly, never silently annotated with a different rule.
    """
    headline = next((r for r in results if r.rule_name == OPTIONS_EXEC_HEADLINE_RULE), None)
    if headline is None:
        raise ValueError(
            f"options_exec_verdict: no {OPTIONS_EXEC_HEADLINE_RULE!r} rule row among "
            f"{[r.rule_name for r in results]!r} — the headline rule must be swept"
        )
    priced_trade_pct: float | None
    if headline.n_signals == 0:
        priced_trade_pct = None
    else:
        priced_trade_pct = headline.n_accepted / headline.n_signals * 100.0
    drag_per_trade_paisa: float | None
    sign_flip: bool | None
    if headline.n_accepted == 0:
        drag_per_trade_paisa = None
        sign_flip = None
    else:
        drag_per_trade_paisa = (
            headline.gross_pnl_paisa - headline.net_pnl_paisa
        ) / headline.n_accepted
        sign_flip = headline.gross_pnl_paisa > 0 and headline.net_pnl_paisa <= 0
    return {
        "status": OPTIONS_EXEC_STATUS_PRICED,
        "rule_name": headline.rule_name,
        "expiry_kind": expiry_kind,
        "broker": broker,
        "n_lots": n_lots,
        "n_signals": headline.n_signals,
        "n_accepted": headline.n_accepted,
        "n_rejected": headline.n_rejected,
        "rejects": {
            "no_data": headline.n_rejected_no_data,
            "tick_floor": headline.n_rejected_tick_floor,
            "lot_size_unknown": headline.n_rejected_lot_size_unknown,
            "stale_bar": headline.n_rejected_stale_bar,
            "cost_regime_unverified": headline.n_rejected_cost_regime_unverified,
        },
        "gross_pnl_paisa": headline.gross_pnl_paisa,
        "total_charges_paisa": headline.total_charges_paisa,
        "net_after_cost_paisa": headline.net_pnl_paisa,
        "n_winners": headline.n_winners,
        "n_losers": headline.n_losers,
        "priced_trade_pct": priced_trade_pct,
        "drag_per_trade_paisa": drag_per_trade_paisa,
        "sign_flip": sign_flip,
        "window": {
            "replay_start": replay_start,
            "replay_end": replay_end,
            "etc_verified_from": etc_verified_from_token(),
        },
    }
