"""Dashboard sweep launcher — turn the one-click "🚀 Run Sweep" button into a grid sweep.

The glue between the dashboard's **🚀 Run Sweep** button and the already-built,
already-tested brute-force engine (``brutex sweep-all`` /
:func:`brutex.engine.sweep_orchestrator.run_sweep_grid`). The button POSTs a
handful of form fields; :func:`resolve_sweep_request` validates + normalises them
into a frozen :class:`SweepRunRequest` (400-able on bad input, **ZERO I/O** — a
pure function, fully unit-testable), and :func:`build_sweep_kwargs` maps that
request onto the exact ``cmd_sweep_all`` keyword contract the CLI already uses
(DEC-SWEEP-ORCHESTRATOR-001). The route layer (``app.py``) runs the sweep on the
single-flight background job and points ``state.sweep_progress_path`` at the file
the sweep publishes (:func:`sweep_progress_file`), so the EXISTING
``GET /api/v1/sweep-progress`` serves the live per-tuple tick with no new progress
plumbing (DEC-SWEEP-RUN-BUTTON-001).

The defaults mirror the CLI's ``_add_sweep_grid_args`` declaration (the one source
of truth for the sweep CLI contract, CLAUDE.md §2 Rule 11 COMMON): NSE index
symbols on the ``INDEX`` segment, the ``1minute,5minute,15minute`` interval set,
both directions, ``K∈{1,2}``, no SL/TP legs, top-20. SENSEX rides the BSE
exchange, so a SENSEX sweep is a separate run with ``exchange=BSE`` — never
silently mixed into an NSE grid (Golden Rule #1, honest scope).

Honest envelope (Golden Rule #10): this module is O(1)-per-field validation/mapping
only. The sweep it launches is O(T·N) over the grid — NOT O(1) total; the live
progress file reports the honest tuple/combo counts as they run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

from brutex.vocabulary.bits import TOTAL_CONDITIONS

#: Default LAKE-partition index symbols (bare trading symbols, NOT the
#: ``EXCH-SYMBOL`` groww keys) — mirrors the CLI ``--symbols`` default in
#: ``brutex.cli._add_sweep_grid_args``. SENSEX is BSE-listed, so it is run
#: separately with ``exchange=BSE`` (never silently folded into an NSE grid).
DEFAULT_SYMBOLS: Final[str] = "NIFTY,BANKNIFTY"
#: Default lake interval set to sweep (mirrors the CLI ``--intervals`` default).
DEFAULT_INTERVALS: Final[str] = "1minute,5minute,15minute"
#: Default trade directions (both legs of a directional edge).
DEFAULT_DIRECTIONS: Final[str] = "BUY,SELL"
#: Default conditions-per-combo K values (mirrors the CLI ``--k`` default —
#: since DEC-SWEEP-APRIORI-DEFAULT-001, MIGRATION.md #1752, the
#: Apriori-to-EXTINCTION token: per cell the k-ladder runs k=1,2,3,… until no
#: combo survives min-hits. The ONE sanctioned second copy of the CLI token
#: (`brutex.cli.EXTINCTION_K_TOKEN` — the dashboard must not import the CLI at
#: module top), drift-PINNED against the CLI argparse ``--k`` default by test.
#: A numeric K stays the classic fixed-K sweep, byte-identical.
DEFAULT_K: Final[str] = "extinction"
#: Default stop-loss / take-profit — ``none`` disables that leg (signal-only).
DEFAULT_SL: Final[str] = "none"
DEFAULT_TP: Final[str] = "none"
#: Default top-N composite-ranked combos persisted per tuple.
DEFAULT_TOP: Final[int] = 20
#: Default run-mode identity label stamped on the run hash.
DEFAULT_MODE: Final[str] = "A"
#: Default lake exchange / segment partition for the index sweep.
DEFAULT_EXCHANGE: Final[str] = "NSE"
DEFAULT_SEGMENT: Final[str] = "INDEX"
#: Default Track-1 history start (the rolling ``2020-01-01 → today`` range —
#: GROWW_HISTORY_FLOOR, DEC-PULL-FLOOR-2020-001).
DEFAULT_START: Final[str] = "2020-01-01"

#: Accepted direction tokens (upper-cased before the check).
_DIRECTIONS: Final[frozenset[str]] = frozenset({"BUY", "SELL"})
#: Max conditions-per-combo K — the engine bounds ``k`` by the unique condition
#: count, which is the full 60-condition vocabulary by default (SweepGridConfig
#: docstring; ``TOTAL_CONDITIONS`` is the one source of truth, so a future
#: vocab growth moves this bound automatically — DEC-VOCAB-U128-CODEGEN-001).
_MAX_K: Final[int] = TOTAL_CONDITIONS


@dataclass(frozen=True, slots=True)
class SweepRunRequest:
    """A validated, normalised sweep launch request (the pure form → engine bridge).

    Every field is already validated + normalised by :func:`resolve_sweep_request`
    into the exact CSV / scalar shape ``cmd_sweep_all`` expects, so
    :func:`build_sweep_kwargs` is a trivial 1:1 mapping (no re-validation, no I/O).

    Attributes
    ----------
    symbols, intervals, directions, k, sl, tp
        Normalised comma-separated axis strings (deduped of blank tokens).
    exchange, segment
        Lake partition coordinates the symbols live under.
    start, end
        Inclusive ``YYYY-MM-DD`` window bounds (``end >= start``, both real dates).
    mode
        Run-mode identity label stamped on the reproducibility hash.
    top
        Top-N composite-ranked combos persisted per tuple (``>= 1``).
    """

    symbols: str
    intervals: str
    directions: str
    k: str
    sl: str
    tp: str
    exchange: str
    segment: str
    start: str
    end: str
    mode: str
    top: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe echo of the resolved request (for the route response)."""
        return {
            "symbols": self.symbols,
            "intervals": self.intervals,
            "directions": self.directions,
            "k": self.k,
            "sl": self.sl,
            "tp": self.tp,
            "exchange": self.exchange,
            "segment": self.segment,
            "start": self.start,
            "end": self.end,
            "mode": self.mode,
            "top": self.top,
        }


def _clean_csv(value: str | None, default: str) -> list[str]:
    """Split *value* on commas, strip + drop blanks; fall back to *default* when empty."""
    source = value if value is not None else default
    tokens = [tok.strip() for tok in source.split(",") if tok.strip()]
    if not tokens:
        tokens = [tok.strip() for tok in default.split(",") if tok.strip()]
    return tokens


def _normalise_symbols(value: str | None) -> str:
    """Normalise the symbols axis (non-empty tokens, original case preserved)."""
    return ",".join(_clean_csv(value, DEFAULT_SYMBOLS))


def _normalise_intervals(value: str | None) -> str:
    """Validate every interval label against the engine's known set; return the CSV."""
    from brutex.feed.resampler import INTERVAL_SECONDS

    tokens = _clean_csv(value, DEFAULT_INTERVALS)
    for token in tokens:
        if token not in INTERVAL_SECONDS:
            raise ValueError(f"interval must be one of {sorted(INTERVAL_SECONDS)} — got {token!r}")
    return ",".join(tokens)


def _normalise_directions(value: str | None) -> str:
    """Validate every direction is BUY/SELL (upper-cased); return the normalised CSV."""
    tokens = [tok.upper() for tok in _clean_csv(value, DEFAULT_DIRECTIONS)]
    for token in tokens:
        if token not in _DIRECTIONS:
            raise ValueError(f"direction must be one of ['BUY', 'SELL'] — got {token!r}")
    return ",".join(tokens)


def _normalise_k(value: str | None) -> str:
    """Validate K: the ``extinction`` token OR ints in ``[1, TOTAL_CONDITIONS]``.

    DEC-SWEEP-APRIORI-DEFAULT-001: a blank field falls to :data:`DEFAULT_K`
    (the extinction token — the CLI default), which flows verbatim through the
    1:1 ``cmd_sweep_all`` kwargs to the ONE CLI resolver; a numeric CSV
    validates exactly as before (the legacy byte-identical arm). Mixing the
    token with numbers is a 400 — the two grammars don't compose.
    """
    tokens = _clean_csv(value, DEFAULT_K)
    lowered = [tok.lower() for tok in tokens]
    if DEFAULT_K in lowered:
        if lowered != [DEFAULT_K]:
            raise ValueError(
                f"k must be either '{DEFAULT_K}' (the Apriori extinction default) or a "
                f"CSV of whole numbers 1..{_MAX_K} — got {','.join(tokens)!r}"
            )
        return DEFAULT_K
    out: list[str] = []
    for token in tokens:
        try:
            parsed = int(token)
        except ValueError as exc:
            raise ValueError(f"k must be a whole number 1..{_MAX_K} — got {token!r}") from exc
        if not 1 <= parsed <= _MAX_K:
            raise ValueError(f"k must be a whole number 1..{_MAX_K} — got {token!r}")
        out.append(str(parsed))
    return ",".join(out)


def _normalise_leg(value: str | None, default: str, *, label: str) -> str:
    """Validate a SL/TP leg: literal ``none`` OR a CSV of positive-paisa ints."""
    source = value if value is not None and value.strip() else default
    if source.strip().lower() == "none":
        return "none"
    tokens = [tok.strip() for tok in source.split(",") if tok.strip()]
    out: list[str] = []
    for token in tokens:
        try:
            parsed = int(token)
        except ValueError as exc:
            raise ValueError(
                f"{label} must be 'none' or positive paisa integers — got {token!r}"
            ) from exc
        if parsed <= 0:
            raise ValueError(f"{label} must be 'none' or positive paisa integers — got {token!r}")
        out.append(str(parsed))
    return ",".join(out)


def _normalise_nonblank(value: str | None, default: str) -> str:
    """Strip *value*; fall back to *default* when it is ``None`` or blank."""
    if value is None:
        return default
    stripped = value.strip()
    if not stripped:
        return default
    return stripped


def _normalise_date(value: str | None, default: str, *, label: str) -> str:
    """Validate *value* parses as an ISO ``YYYY-MM-DD`` date; return the canonical string."""
    source = value if value is not None and value.strip() else default
    try:
        return date.fromisoformat(source.strip()).isoformat()
    except ValueError as exc:
        raise ValueError(f"{label} must be a YYYY-MM-DD date — got {source!r}") from exc


def resolve_sweep_request(
    *,
    symbols: str | None = None,
    intervals: str | None = None,
    directions: str | None = None,
    k: str | None = None,
    sl: str | None = None,
    tp: str | None = None,
    top: int | None = None,
    mode: str | None = None,
    exchange: str | None = None,
    segment: str | None = None,
    start: str | None = None,
    end: str | None = None,
    today: date,
) -> SweepRunRequest:
    """Validate + normalise raw dashboard form params into a :class:`SweepRunRequest`.

    A **pure** function (no I/O, deterministic given *today*): every axis is
    cleaned of blank tokens and defaulted when absent, and every value is checked
    so a bad form field raises a clear :class:`ValueError` the route turns into a
    400 — never a deep, opaque background-job crash (Golden Rule #10 honesty). The
    ``end`` default is *today* (injected, never read from the clock here, so the
    mapping is reproducibly testable, Golden Rule #4).

    Parameters
    ----------
    symbols, intervals, directions, k, sl, tp
        Raw CSV axis strings (``None`` / blank ⇒ the CLI-matching default).
    top
        Raw top-N (``None`` ⇒ :data:`DEFAULT_TOP`); must be ``>= 1``.
    mode, exchange, segment
        Raw non-blank identity / partition strings (``None`` / blank ⇒ default).
    start, end
        Raw ``YYYY-MM-DD`` window bounds; ``end`` defaults to *today*. ``end`` must
        not precede ``start``.
    today
        The injected "now" date used as the ``end`` default + ``end >= start`` check.

    Returns
    -------
    SweepRunRequest
        The validated, normalised request.

    Raises
    ------
    ValueError
        On any unknown interval, non-BUY/SELL direction, out-of-range / non-integer
        K, malformed SL/TP leg, non-positive ``top``, unparseable date,
        ``end < start``, or an out-of-engine-scope launch coordinate (the
        :func:`brutex.engine.sweep_scope.check_sweep_scope` refusal —
        DEC-ENGINE-SCOPE-GUARD-001 + its corporate-action extension
        DEC-CORP-ACTION-GATE-001 / ADR-0016 — surfaced eagerly as a 400 via
        the route's existing ``ValueError`` mapping).
    """
    resolved_top = DEFAULT_TOP if top is None else top
    if resolved_top < 1:
        raise ValueError(f"top must be a whole number >= 1 — got {resolved_top!r}")
    resolved_start = _normalise_date(start, DEFAULT_START, label="start")
    resolved_end = _normalise_date(end, today.isoformat(), label="end")
    if resolved_end < resolved_start:
        raise ValueError(f"end {resolved_end!r} must not precede start {resolved_start!r}")
    resolved_symbols = _normalise_symbols(symbols)
    resolved_exchange = _normalise_nonblank(exchange, DEFAULT_EXCHANGE)
    resolved_segment = _normalise_nonblank(segment, DEFAULT_SEGMENT)
    # Engine-scope coordinate law (DEC-ENGINE-SCOPE-GUARD-001, Layer 2): an
    # out-of-engine-scope dashboard launch is a 400 via the existing
    # ValueError → route mapping. There is deliberately NO override parameter
    # on the dashboard — the research escape hatch is CLI-only
    # (--allow-out-of-scope, loud + audit-stamped).
    from brutex.engine.sweep_scope import check_sweep_scope

    scope = check_sweep_scope(
        resolved_symbols.split(","),
        exchange=resolved_exchange,
        segment=resolved_segment,
        allow_out_of_scope=False,
    )
    if not scope.ok:
        raise ValueError(scope.refusal_message())
    return SweepRunRequest(
        symbols=resolved_symbols,
        intervals=_normalise_intervals(intervals),
        directions=_normalise_directions(directions),
        k=_normalise_k(k),
        sl=_normalise_leg(sl, DEFAULT_SL, label="sl"),
        tp=_normalise_leg(tp, DEFAULT_TP, label="tp"),
        exchange=resolved_exchange,
        segment=resolved_segment,
        start=resolved_start,
        end=resolved_end,
        mode=_normalise_nonblank(mode, DEFAULT_MODE),
        top=resolved_top,
    )


def default_results_root(lake_root: Path) -> Path:
    """Return the default per-tuple results root for *lake_root* (``<lake>/sweeps``).

    Mirrors ``cmd_sweep_all``'s ``out_root`` derivation exactly so the dashboard
    and the CLI write/read the SAME sweep tree (CLAUDE.md §2 Rule 11 COMMON).
    """
    return lake_root / "sweeps"


def sweep_progress_file(results_root: Path) -> Path:
    """Return the live progress file path under *results_root* (``progress/sweep-progress.json``).

    The EXACT path ``cmd_sweep_all`` publishes its
    :class:`~brutex.engine.sweep_progress.SweepProgressSnapshot` to — the route
    points ``state.sweep_progress_path`` here so the existing
    ``GET /api/v1/sweep-progress`` serves the running sweep with no new plumbing.
    """
    return results_root / "progress" / "sweep-progress.json"


def build_sweep_kwargs(
    request: SweepRunRequest,
    *,
    lake_root: Path,
    results_root: Path | None = None,
    catalog_path: Path | None = None,
) -> dict[str, Any]:
    """Map a validated *request* onto the ``cmd_sweep_all`` keyword contract.

    A pure 1:1 mapping (the request is already validated). The non-swept knobs are
    pinned to the safe dashboard defaults: ``conditions=None`` (the full
    60-condition vocabulary — ``default_condition_bits()``),
    ``options_sim=False`` (signal-only research — no option lake required),
    ``allow_missing_slices=True`` (a one-click sweep runs on WHATEVER is in the
    lake, skipping empty ``(symbol, interval)`` slices honestly rather than hard
    failing), and the overfit / Stage-3 gates OFF (the operator opts in via the
    CLI for a production run).

    Parameters
    ----------
    request
        The validated launch request.
    lake_root
        The resolved lake root (the sweep reads its Parquet partitions from here).
    results_root
        Per-tuple results tree (``None`` ⇒ :func:`default_results_root`).
    catalog_path
        Catalog DuckDB path (``None`` ⇒ ``cmd_sweep_all``'s ``<lake>/catalog.duckdb``).

    Returns
    -------
    dict[str, Any]
        The exact kwargs to splat into ``cmd_sweep_all``.
    """
    return {
        "lake_root": lake_root,
        "catalog_path": catalog_path,
        "results_root": results_root,
        "exchange": request.exchange,
        "segment": request.segment,
        "symbols": request.symbols,
        "intervals": request.intervals,
        "start": request.start,
        "end": request.end,
        "directions": request.directions,
        "k": request.k,
        "conditions": None,
        "sl": request.sl,
        "tp": request.tp,
        "top": request.top,
        "mode": request.mode,
        "max_tuples": None,
        "max_combos": None,
        "data_snapshot_hash": None,
        "overfit_gate": False,
        "options_sim": False,
        "stage3_enable": False,
        "allow_missing_slices": True,
        # DEC-ENGINE-SCOPE-GUARD-001: the dashboard NEVER overrides the engine
        # scope — explicit here to keep the 1:1 cmd_sweep_all kwargs doctrine
        # visible (the request was already scope-checked in
        # resolve_sweep_request; the CLI escape hatch does not exist on this
        # surface by design).
        "allow_out_of_scope": False,
    }


def run_sweep_from_request(  # pragma: no cover — orchestration shim over the 100%-tested engine
    request: SweepRunRequest,
    *,
    lake_root: Path,
    results_root: Path,
) -> int:
    """Run the full grid sweep for *request* via ``cmd_sweep_all`` (returns its exit code).

    The thin live-lake shim the background job calls: it builds the kwargs
    (:func:`build_sweep_kwargs`) and delegates to the 100%-tested
    ``brutex.cli.cmd_sweep_all`` (imported lazily to keep the dashboard import
    light + avoid a CLI import cycle). Marked ``# pragma: no cover`` exactly like
    ``cmd_sweep_all`` itself — the orchestration is exercised end-to-end by the CLI
    integration tests; the dashboard route tests replace THIS function with an
    offline fake (no lake, no sweep) to keep the unit suite hermetic.

    ENV HYGIENE (DEC-SWEEP-APRIORI-DEFAULT-001, MIGRATION.md #1752): this is
    the ONE in-process embedder of ``cmd_sweep_all``, whose extinction default
    setdefault-writes ``BRUTEX_SWEEP_APRIORI`` into the process env for the
    spawn workers. The dashboard process OUTLIVES the sweep job (unlike a CLI
    run, which exits), so the flag is snapshot-restored here — success AND
    raise — or a leak would silently flip a LATER explicit-K dashboard sweep
    into apriori mode (different tallies + a raised max_combos ⇒ a different
    sweep_id — a byte-identity violation). The single-flight job guarantees no
    concurrent reader DURING the run.
    """
    from brutex.cli import cmd_sweep_all
    from brutex.engine.sweep_apriori import SWEEP_APRIORI_ENV

    before = os.environ.get(SWEEP_APRIORI_ENV)
    try:
        return cmd_sweep_all(
            **build_sweep_kwargs(request, lake_root=lake_root, results_root=results_root)
        )
    finally:
        if before is None:
            os.environ.pop(SWEEP_APRIORI_ENV, None)
        else:
            os.environ[SWEEP_APRIORI_ENV] = before


__all__ = [
    "DEFAULT_DIRECTIONS",
    "DEFAULT_EXCHANGE",
    "DEFAULT_INTERVALS",
    "DEFAULT_K",
    "DEFAULT_MODE",
    "DEFAULT_SEGMENT",
    "DEFAULT_SL",
    "DEFAULT_START",
    "DEFAULT_SYMBOLS",
    "DEFAULT_TOP",
    "DEFAULT_TP",
    "SweepRunRequest",
    "build_sweep_kwargs",
    "default_results_root",
    "resolve_sweep_request",
    "run_sweep_from_request",
    "sweep_progress_file",
]
