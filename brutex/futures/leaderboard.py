"""Futures execution-leg display: leaderboard render + row annotation + sidecar.

DEC-FUTURES-EXEC-TRANSLATION-001 (R13 / R24 / R27 / R42): the futures leg is
STRICTLY DOWNSTREAM, READ-ONLY display over the SAME persisted Track-1 spot
result row — it can never re-rank, filter, reject, or mutate a winner. Three
surfaces (the options precedent):

* :func:`render_futures_leaderboard` — a PURE text formatter (the futures twin
  of the CLI's ``_render_options_leaderboard``): money via
  :func:`brutex.money.format_inr`, DIRECT attribute access (a field rename
  fails LOUD, never silently renders no footer), and an honesty footer DERIVED
  from :func:`~brutex.costs.futures_costs.futures_etc_unverified_windows` DATA
  whenever trades were skipped on an unverified cost window.
* :func:`project_futures_annotation` — attaches the execution-leg dict onto a
  COPY of the row's display mapping under ONE new key (``futures_exec``);
  every pre-existing key passes through byte-identical and the rank/order is
  untouched (R13 annotation-never-reranks). A STOCK winner annotates its
  honest deferral note, never a fabricated leg.
* :func:`write_translation_sidecar` / :func:`read_translation_sidecar` — the
  R42 atomic, content-hash-STAMPED JSONL stage output: a half-written sidecar
  fails its stamp on read and is discarded + rebuilt; upstream spot artifacts
  are never touched (the write goes through the COMMON
  :func:`~brutex.data.atomic_io.atomic_write_simple`).
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Final

from brutex.costs.futures_costs import futures_etc_unverified_windows
from brutex.futures.executor import STOCK_SPOT_DEFERRAL_NOTE, ExecutionKind, FuturesRuleResult
from brutex.money import format_inr

if TYPE_CHECKING:  # pragma: no cover — annotation-only imports
    from collections.abc import Mapping
    from pathlib import Path

__all__ = [
    "ANNOTATION_KEY",
    "SIDECAR_VERSION",
    "project_futures_annotation",
    "read_translation_sidecar",
    "render_futures_leaderboard",
    "write_translation_sidecar",
]

#: The ONE display key the execution-leg annotation lands under.
ANNOTATION_KEY: Final[str] = "futures_exec"

#: Sidecar payload schema version (bump on any shape change).
SIDECAR_VERSION: Final[int] = 1


def render_futures_leaderboard(row: Any, result: FuturesRuleResult) -> str:
    """Render one winner's futures execution leg as display text — PURE.

    No lake I/O; never mutates *row* or *result*; DIRECT attribute access
    (deliberately NOT ``getattr(..., default)`` — a field rename must fail
    LOUD here, never silently drop the honesty footer forever).

    Parameters
    ----------
    row
        The winner's spot result row (``run_id`` / ``instrument`` / ``tf`` /
        ``direction`` attributes — a ``ResultRow`` in production).
    result
        The aggregated :class:`~brutex.futures.executor.FuturesRuleResult`.

    Returns
    -------
    str
        The rendered block: header, one aggregate line, a reject line, and —
        when any trade fell in an unverified cost window — the honesty footer
        with the window derived from regime-table DATA (never hardcoded).
    """
    lines = [
        f"futures-exec · {row.instrument} {row.tf} {row.direction} · run {row.run_id[:12]}…",
        f"{'trades':>7} {'win%':>6} {'gross':>16} {'charges':>14} {'net':>16}",
        (
            f"{result.n_accepted:>7} {result.win_rate * 100:>5.1f}% "
            f"{format_inr(result.gross_pnl_paisa):>16} "
            f"{format_inr(result.total_charges_paisa):>14} "
            f"{format_inr(result.net_pnl_paisa):>16}"
        ),
        (
            f"rejected: {result.n_rejected} "
            f"(lot {result.n_rejected_lot_size_unknown} · "
            f"cost {result.n_rejected_cost_regime_unverified} · "
            f"contract {result.n_rejected_no_contract} · "
            f"data {result.n_rejected_no_data} · "
            f"stale {result.n_rejected_stale_bar}) · "
            f"vix-missing {result.n_vix_missing}"
        ),
    ]
    if result.n_rejected_cost_regime_unverified:
        windows = ", ".join(
            f"{exch} [{'history-start' if start.toordinal() == 1 else start.isoformat()} → "
            f"{end.isoformat() if end else 'open'})"
            for exch, spans in sorted(futures_etc_unverified_windows().items())
            for start, end in spans
        )
        lines.append(
            f"⚠ {result.n_rejected_cost_regime_unverified} trade(s) fell in an UNVERIFIED "
            f"futures cost window — skipped, never priced with a fabricated rate "
            f"({windows}; DEC-FUTURES-EXEC-TRANSLATION-001)"
        )
    return "\n".join(lines)


def project_futures_annotation(
    row_dict: Mapping[str, object],
    result: FuturesRuleResult | None,
    *,
    kind: ExecutionKind = ExecutionKind.INDEX_FUTURES,
) -> dict[str, object]:
    """Attach the execution-leg annotation to a COPY of the row's display dict.

    R13/R30: the annotation is ADDITIVE-ONLY — every pre-existing key/value
    passes through untouched (byte-identical), the ranking fields are never
    rewritten, and re-annotation is refused (fail-loud) so a leg can never
    silently overwrite another.

    Parameters
    ----------
    row_dict
        The winner row's display mapping (e.g. a ``_project_row`` dict).
    result
        The executed leg's result — REQUIRED for
        :data:`ExecutionKind.INDEX_FUTURES`, and MUST be ``None`` for the
        deferred/unsupported kinds (a deferred stock leg has no numbers; a
        fabricated one is unrepresentable).
    kind
        The :func:`~brutex.futures.executor.route_execution` verdict.

    Returns
    -------
    dict[str, object]
        ``{**row_dict, "futures_exec": {…}}``.

    Raises
    ------
    ValueError
        On a row already annotated, or a *result*/*kind* mismatch.
    """
    if ANNOTATION_KEY in row_dict:
        msg = f"row already carries a {ANNOTATION_KEY!r} annotation — refusing to overwrite"
        raise ValueError(msg)
    annotation: dict[str, object]
    if kind is ExecutionKind.INDEX_FUTURES:
        if result is None:
            msg = "INDEX_FUTURES annotation requires the executed FuturesRuleResult"
            raise ValueError(msg)
        annotation = {
            "status": "executed",
            "n_signals": result.n_signals,
            "n_accepted": result.n_accepted,
            "n_rejected": result.n_rejected,
            "n_rejected_lot_size_unknown": result.n_rejected_lot_size_unknown,
            "n_rejected_cost_regime_unverified": result.n_rejected_cost_regime_unverified,
            "n_rejected_no_contract": result.n_rejected_no_contract,
            "n_rejected_no_data": result.n_rejected_no_data,
            "n_rejected_stale_bar": result.n_rejected_stale_bar,
            "gross_pnl_paisa": result.gross_pnl_paisa,
            "total_charges_paisa": result.total_charges_paisa,
            "net_pnl_paisa": result.net_pnl_paisa,
            "n_winners": result.n_winners,
            "n_losers": result.n_losers,
            "n_vix_missing": result.n_vix_missing,
            "win_rate": result.win_rate,
        }
    else:
        if result is not None:
            msg = f"{kind.value} annotation must not carry a result (nothing was executed)"
            raise ValueError(msg)
        note = (
            STOCK_SPOT_DEFERRAL_NOTE
            if kind is ExecutionKind.STOCK_SPOT_DEFERRED
            else "unsupported — not a tradable underlying (blank / reference-only)"
        )
        annotation = {"status": kind.value, "note": note}
    return {**row_dict, ANNOTATION_KEY: annotation}


def _sidecar_record(result: FuturesRuleResult, *, run_id: str) -> dict[str, object]:
    """Build the stamped sidecar record: payload + its content hash."""
    payload: dict[str, object] = {
        "version": SIDECAR_VERSION,
        "run_id": run_id,
        "instrument": result.instrument,
        "direction": result.direction,
        "n_signals": result.n_signals,
        "n_accepted": result.n_accepted,
        "n_rejected_lot_size_unknown": result.n_rejected_lot_size_unknown,
        "n_rejected_cost_regime_unverified": result.n_rejected_cost_regime_unverified,
        "n_rejected_no_contract": result.n_rejected_no_contract,
        "n_rejected_no_data": result.n_rejected_no_data,
        "n_rejected_stale_bar": result.n_rejected_stale_bar,
        "gross_pnl_paisa": result.gross_pnl_paisa,
        "total_charges_paisa": result.total_charges_paisa,
        "net_pnl_paisa": result.net_pnl_paisa,
        "n_winners": result.n_winners,
        "n_losers": result.n_losers,
        "n_vix_missing": result.n_vix_missing,
    }
    return {"payload": payload, "content_hash": _payload_hash(payload)}


def _payload_hash(payload: Mapping[str, object]) -> str:
    """The canonical content hash of a sidecar payload (sorted-key JSON, sha256)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_translation_sidecar(result: FuturesRuleResult, *, out_root: Path, run_id: str) -> Path:
    """Persist the run's futures leg as an ATOMIC content-hash-stamped sidecar (R42).

    Written to ``<out_root>/audit/futures-exec-<run_id[:12]>.jsonl`` through the
    COMMON :func:`~brutex.data.atomic_io.atomic_write_simple` (tmp → fsync →
    ``os.replace``), so a crash mid-write leaves either the previous file or
    nothing — never a torn record. Re-running the translation link rebuilds a
    byte-identical sidecar (R42: recovery starts at THIS link, upstream spot
    artifacts intact).

    Parameters
    ----------
    result
        The aggregated leg result.
    out_root
        The artifact root (the sidecar lands under its ``audit/`` dir).
    run_id
        The run's single identity (R43).

    Returns
    -------
    Path
        The sidecar path.
    """
    from brutex.data.atomic_io import atomic_write_simple

    dest = out_root / "audit" / f"futures-exec-{run_id[:12]}.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(_sidecar_record(result, run_id=run_id), sort_keys=True)

    def _write(tmp: Path) -> None:
        tmp.write_text(line + "\n", encoding="utf-8")

    atomic_write_simple(dest, _write)
    return dest


def read_translation_sidecar(path: Path) -> dict[str, object]:
    """Read + STAMP-VALIDATE a translation sidecar; the R42 consume gate.

    Parameters
    ----------
    path
        The sidecar path.

    Returns
    -------
    dict[str, object]
        The verified payload.

    Raises
    ------
    ValueError
        On a malformed record or a content-hash mismatch (a half-written /
        tampered sidecar) — the caller discards + rebuilds, never consumes it.
    """
    raw = path.read_text(encoding="utf-8").strip()
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"translation sidecar {path} is not valid JSON — discard + rebuild (R42): {exc}"
        raise ValueError(msg) from exc
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("payload"), dict)
        or not isinstance(record.get("content_hash"), str)
    ):
        msg = f"translation sidecar {path} is malformed — discard + rebuild (R42)"
        raise ValueError(msg)
    payload: dict[str, object] = record["payload"]
    if _payload_hash(payload) != record["content_hash"]:
        msg = f"translation sidecar {path} failed its content-hash stamp — discard + rebuild (R42)"
        raise ValueError(msg)
    return payload
