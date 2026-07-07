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

Since DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 (Phase 1) this module ALSO owns the
dashboard's D8 launch fan-out (:func:`resolve_sweep_launches` — blank symbols
⇒ ALL 3 engine indices, NIFTY-first, grouped one launch per
``(exchange, segment)``) and the route-side synchronous, probe-only preflight
(:func:`preflight_sweep_request` — per-launch
:func:`~brutex.engine.sweep_gate.run_sweep_data_preflight` calls, refusals
UNIONED, estimate SUMMED with a per-launch breakdown, ONE ``confirm_token``
minted over the whole fan-out with ``lake_token=lake_revision(lake_root)``).
The dashboard defaults are the backend of R1/R2: blank TFs ⇒ all 21 lake
intervals (rebuilt from ``LAKE_INTERVAL_LABELS``, never hand-typed), ``k`` ≤ 3
(O-f), the 16-cell SL/TP grid (O-g), and ``allow_missing_slices`` pinned
``False`` — the one-click sweep now HARD-REFUSES on a not-ready lake (O-a).

Honest envelope (Golden Rule #10): the resolvers/mappers here are
O(1)-per-field validation/mapping; :func:`preflight_sweep_request` is
probe-only (it NEVER takes the sweep lock and NEVER writes) but its coverage
scan is O(requested symbols × window-months) — see
:mod:`brutex.data.session_coverage`. The route-level gate is advisory UX —
the authoritative gate re-runs inside ``cmd_sweep_all`` under the held sweep
lock. The sweep it launches is O(T·N) over the grid — NOT O(1) total; the
live progress file reports the honest tuple/combo counts as they run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from brutex.data.timeframes import LAKE_INTERVAL_LABELS
from brutex.vocabulary.bits import TOTAL_CONDITIONS

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping, Sequence

    from brutex.engine.sweep_gate import SweepPreflightOutcome

#: Default LAKE-partition index symbols (bare trading symbols, NOT the
#: ``EXCH-SYMBOL`` groww keys) — mirrors the CLI ``--symbols`` default in
#: ``brutex.cli._add_sweep_grid_args``. Consumed by :func:`resolve_sweep_request`
#: only when launch coordinates are EXPLICIT; the one-click blank-symbols path
#: rides :func:`resolve_sweep_launches`, which fans out to ALL 3 engine indices
#: (NIFTY-first) so SENSEX's BSE launch is included, never silently dropped
#: (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 D8).
DEFAULT_SYMBOLS: Final[str] = "NIFTY,BANKNIFTY"
#: Default lake interval set to sweep — ALL 21 Track-1 timeframes, rebuilt from
#: :data:`brutex.data.timeframes.LAKE_INTERVAL_LABELS` (never a hand-typed
#: list, so a timeframe-vocabulary change propagates automatically —
#: DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 "blank TFs ⇒ all 21").
DEFAULT_INTERVALS: Final[str] = ",".join(
    LAKE_INTERVAL_LABELS[minutes] for minutes in sorted(LAKE_INTERVAL_LABELS)
)
#: Default trade directions (both legs of a directional edge).
DEFAULT_DIRECTIONS: Final[str] = "BUY,SELL"
#: Default conditions-per-combo K values (O-f, DEC-SWEEP-PREFLIGHT-ESTIMATOR-001
#: — sanctioned ``"1,2" → "1,2,3"``; deeper k stays allowed-with-WARN, the SLA
#: verdict is the time wall).
DEFAULT_K: Final[str] = "1,2,3"
#: Default stop-loss / take-profit axes (O-g, DEC-SWEEP-PREFLIGHT-ESTIMATOR-001
#: — *sanctioned-pending-operator-confirmation*): the ``none`` arm plus the
#: axis-min / round-on-grid midpoint / axis-max of the LOCKED Mode-A stage-3
#: axes (``MODE_A_SL_AXIS`` = [5, 200]¢ tick 5 → 5/100/200;
#: ``MODE_A_TP_AXIS`` = [10, 500]¢ tick 10 → 10/250/500 —
#: ``brutex/engine/stage3_grid.py``), giving 4×4 = 16 cells = all 4 arms
#: ((None,None) + 3×(SL,None) + 3×(None,TP) + 9×(SL,TP)) over a 3×3 value
#: grid. Traceable-DERIVED, not invented (R27); a small honest grid, NOT the
#: 2,000-cell Mode-A production grid. Revert = these two constants.
DEFAULT_SL: Final[str] = "none,5,100,200"
DEFAULT_TP: Final[str] = "none,10,250,500"
#: Default Fibonacci anchor set — the MINIMUM ``prev_day`` set (O-i,
#: DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 — delivered by NOT expanding). Mirrors
#: ``cmd_sweep_all``'s ``fib_anchors`` keyword default (drift-pinned by test)
#: so the route-side preflight estimates EXACTLY the launch the job runs.
DEFAULT_FIB_ANCHORS: Final[str] = "prev_day"
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
    acknowledge_sla
        The SLA time-envelope consent axis (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001;
        the dashboard twin of ``--acknowledge-sla-overrun``). Its OWN field —
        deliberately NEVER conflated with the estimate-proceed
        ``confirm_token`` consent (consent axes stay separate, excision #4).
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
    acknowledge_sla: bool = False

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
            "acknowledge_sla": self.acknowledge_sla,
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
    """Validate every K parses to an int in ``[1, TOTAL_CONDITIONS]``; return the CSV."""
    tokens = _clean_csv(value, DEFAULT_K)
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
    """Validate a SL/TP leg CSV: each token is ``none`` OR a positive-paisa int.

    ``none`` is a first-class TOKEN (the no-leg arm), mixable with paisa values
    — the exact grammar of the CLI's ``_parse_paisa_list`` — so the O-g default
    ``"none,5,100,200"`` (the 4-arm × 3-value grid) round-trips
    (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001; a lone ``none`` keeps its legacy
    single-token meaning byte-identically).
    """
    source = value if value is not None and value.strip() else default
    tokens = [tok.strip() for tok in source.split(",") if tok.strip()]
    out: list[str] = []
    for token in tokens:
        if token.lower() == "none":
            out.append("none")
            continue
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
    acknowledge_sla: bool = False,
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
    acknowledge_sla
        The SLA time-envelope consent axis (its OWN axis — never conflated
        with the estimate-proceed token; DEC-SWEEP-PREFLIGHT-ESTIMATOR-001).
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
        acknowledge_sla=acknowledge_sla,
    )


def _engine_launch_groups() -> tuple[tuple[tuple[str, str], tuple[str, ...]], ...]:
    """The engine's spot launch groups, NIFTY-first — DERIVED, never hand-typed.

    Orders :data:`~brutex.universe.ENGINE_SWEEP_SPOT_TRIPLES` by the canonical
    :data:`~brutex.universe.BRUTE_FORCE_INDICES` declaration order (NIFTY →
    BANKNIFTY → SENSEX) and groups consecutive symbols by their
    ``(exchange, segment)`` coordinate: ``(("NSE", "INDEX"), ("NIFTY",
    "BANKNIFTY"))`` then ``(("BSE", "INDEX"), ("SENSEX",))`` today — a
    universe change propagates automatically
    (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 D8).
    """
    from brutex.universe import BRUTE_FORCE_INDICES, ENGINE_SWEEP_SPOT_TRIPLES

    rank = {
        (spec.exchange.value, spec.underlying_symbol): index
        for index, spec in enumerate(BRUTE_FORCE_INDICES)
    }
    ordered = sorted(ENGINE_SWEEP_SPOT_TRIPLES, key=lambda triple: rank[(triple[0], triple[2])])
    groups: dict[tuple[str, str], list[str]] = {}
    for exchange, segment, symbol in ordered:
        groups.setdefault((exchange, segment), []).append(symbol)
    return tuple((coords, tuple(symbols)) for coords, symbols in groups.items())


def resolve_sweep_launches(
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
    acknowledge_sla: bool = False,
    today: date,
) -> tuple[SweepRunRequest, ...]:
    """Resolve raw form params into the ordered launch fan-out (D8) — pure.

    The engine grid runs one ``(exchange, segment)`` coordinate per launch, so
    the dashboard's "blank symbols ⇒ ALL 3 indices" law fans one request out
    into per-coordinate launches (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 D8):

    * **Explicit coordinates** (a non-blank *exchange* or *segment*) keep the
      legacy single-launch behaviour byte-identically — the operator chose the
      coordinate, and a mismatched symbol is still refused by the scope gate.
    * **Blank symbols** (and blank coordinates) ⇒ the full engine fan-out from
      :data:`~brutex.universe.ENGINE_SWEEP_SPOT_TRIPLES`, NIFTY-first:
      ``NSE/INDEX (NIFTY, BANKNIFTY)`` then ``BSE/INDEX (SENSEX,)``.
    * **Explicit symbols** (blank coordinates) group by their engine triple
      coordinates in first-seen order (``"NIFTY,SENSEX"`` ⇒ an NSE launch and
      a BSE launch); a symbol with NO engine triple rides the DEFAULT
      coordinates group, where
      :func:`~brutex.engine.sweep_scope.check_sweep_scope` (backed by
      ``classify_engine_sweep_target``) refuses it with the full scope
      message — bad coordinates are still refused, never silently regrouped.

    Every launch is validated through :func:`resolve_sweep_request` (the SAME
    per-launch contract, ValueError ⇒ a clean 400). All non-symbol axes are
    shared verbatim across the fan-out.

    Returns
    -------
    tuple[SweepRunRequest, ...]
        The ordered launches (length ≥ 1).

    Raises
    ------
    ValueError
        Any :func:`resolve_sweep_request` refusal for any launch.
    """
    explicit_coords = bool(exchange is not None and exchange.strip()) or bool(
        segment is not None and segment.strip()
    )
    shared: dict[str, Any] = {
        "intervals": intervals,
        "directions": directions,
        "k": k,
        "sl": sl,
        "tp": tp,
        "top": top,
        "mode": mode,
        "start": start,
        "end": end,
        "acknowledge_sla": acknowledge_sla,
        "today": today,
    }
    if explicit_coords:
        return (
            resolve_sweep_request(symbols=symbols, exchange=exchange, segment=segment, **shared),
        )
    engine_groups = _engine_launch_groups()
    tokens = [tok.strip() for tok in (symbols or "").split(",") if tok.strip()]
    if not tokens:
        groups: list[tuple[tuple[str, str], tuple[str, ...]]] = list(engine_groups)
    else:
        coords_by_symbol = {
            symbol: coords for coords, group_symbols in engine_groups for symbol in group_symbols
        }
        grouped: dict[tuple[str, str], list[str]] = {}
        for token in dict.fromkeys(tokens):
            coords = coords_by_symbol.get(token, (DEFAULT_EXCHANGE, DEFAULT_SEGMENT))
            grouped.setdefault(coords, []).append(token)
        groups = [(coords, tuple(group)) for coords, group in grouped.items()]
    return tuple(
        resolve_sweep_request(
            symbols=",".join(group_symbols),
            exchange=coords[0],
            segment=coords[1],
            **shared,
        )
        for coords, group_symbols in groups
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
    ``allow_missing_slices=False`` (O-a refuse-whole,
    DEC-SWEEP-PREFLIGHT-ESTIMATOR-001 — a one-click sweep HARD-REFUSES when any
    requested ``(symbol, interval)`` slice is empty/not-ready, naming the
    complete not-ready list; it NEVER silently sweeps over holes — this
    supersedes the prior forgiving ``True`` pin), the SLA consent threaded to
    its OWN ``acknowledge_sla_overrun`` axis (never conflated with the
    estimate-proceed ``confirm_token``), and the overfit / Stage-3 gates OFF
    (the operator opts in via the CLI for a production run).

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
        # O-a refuse-whole (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001): the one-click
        # sweep refuses on ANY missing (symbol, interval) slice — the pre-load
        # composed gate names the holes, and this post-load zero-bar belt stays
        # armed too. No dashboard escape exists by design (the CLI flag
        # survives with its legacy zero-bar meaning only).
        "allow_missing_slices": False,
        # Consent axes stay SEPARATE (excision #4): the SLA time-envelope
        # consent maps to its own cmd_sweep_all axis; the estimate-proceed
        # consent is the route-level confirm_token, which never reaches here.
        "acknowledge_sla_overrun": request.acknowledge_sla,
        # DEC-ENGINE-SCOPE-GUARD-001: the dashboard NEVER overrides the engine
        # scope — explicit here to keep the 1:1 cmd_sweep_all kwargs doctrine
        # visible (the request was already scope-checked in
        # resolve_sweep_request; the CLI escape hatch does not exist on this
        # surface by design).
        "allow_out_of_scope": False,
    }


def _leg_paisa_values(csv: str) -> tuple[int | None, ...]:
    """Decode a NORMALISED SL/TP leg CSV into gate values (``none`` → ``None``)."""
    return tuple(None if tok == "none" else int(tok) for tok in csv.split(","))


def _run_launch_preflight(
    request: SweepRunRequest,
    *,
    lake_root: Path,
    out_root: Path,
    today: date | None,
    env: Mapping[str, str] | None,
) -> SweepPreflightOutcome:
    """Run the composed engine gate for ONE resolved launch (probe-only).

    The lazy import keeps the dashboard module light and gives tests the
    ``brutex.engine.sweep_gate`` monkeypatch seam. ``lake_token`` is
    deliberately ``None`` per launch — the ONE merged fan-out token is minted
    by :func:`preflight_sweep_request` (a single launch's token would not bind
    the sibling launches).
    """
    from brutex.engine.sweep_gate import run_sweep_data_preflight

    return run_sweep_data_preflight(
        lake_root=lake_root,
        out_root=out_root,
        exchange=request.exchange,
        segment=request.segment,
        symbols=request.symbols.split(","),
        intervals=request.intervals.split(","),
        directions=request.directions.split(","),
        k_values=[int(token) for token in request.k.split(",")],
        condition_bits=None,
        stop_losses=_leg_paisa_values(request.sl),
        take_profits=_leg_paisa_values(request.tp),
        fib_anchors=(DEFAULT_FIB_ANCHORS,),
        top_k=request.top,
        start=date.fromisoformat(request.start),
        end=date.fromisoformat(request.end),
        acknowledge_sla=request.acknowledge_sla,
        today=today,
        env=env,
    )


def _merged_headline(
    *,
    trials: int,
    est_seconds: float,
    display_bytes: int,
    dedup_map_bytes: int,
    provisional: bool,
    calibration_samples: int,
    calibration_last_ts: str | None,
) -> str:
    """Compose the fan-out confirm headline from the SUMMED figures.

    The exact :meth:`~brutex.engine.sweep_estimate.SweepEstimate.headline`
    shape, rebuilt over the launch sums so the operator confirms ONE honest
    total (per-launch figures ride the payload's ``launches`` breakdown).
    """
    if provisional:
        label = "PROVISIONAL — uncalibrated"
    else:
        last = "unknown" if calibration_last_ts is None else calibration_last_ts[:10]
        label = f"calibrated from {calibration_samples} runs, last {last}"
    dedup_part = ""
    if dedup_map_bytes > 0:
        dedup_part = f" (+{dedup_map_bytes / 2**30:.1f} GiB dedup map reserve)"
    return (
        f"this selection = {trials:,} trials, "
        f"est. {est_seconds / 3600.0:.1f} h ({label}), "
        f"~{display_bytes / 1e9:.2f} GB results{dedup_part} — proceed?"
    )


def preflight_sweep_request(
    launches: Sequence[SweepRunRequest],
    *,
    lake_root: Path,
    results_root: Path | None = None,
    today: date | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Run the composed data-preflight over the WHOLE launch fan-out — probe-only.

    The dashboard's synchronous route-side gate (DEC-SWEEP-PREFLIGHT-ESTIMATOR-001
    §3.3): every resolved launch goes through the SAME
    :func:`~brutex.engine.sweep_gate.run_sweep_data_preflight` core the CLI
    uses, then the verdicts merge refuse-whole ACROSS launches — refusals
    UNIONED (a single not-ready pair anywhere refuses the WHOLE fan-out; ready
    launches are shown but NOT run), the estimate SUMMED with the per-launch
    breakdown, and ONE ``confirm_token`` minted over the whole fan-out with
    ``lake_token=lake_revision(lake_root)`` — so the operator's proceed binds
    the exact numbers + lake state seen, and a pull finishing between estimate
    and click moves the token.

    PROBE-ONLY (R42): never takes the sweep lock, never writes anything — a
    refusal is DATA (the route returns it as an always-200 payload). HONEST
    envelope: this route-level gate is advisory UX — the authoritative gate
    re-runs inside ``cmd_sweep_all`` under the held sweep lock; the coverage
    scan is O(requested symbols × window-months) per launch, NOT O(1).

    Parameters
    ----------
    launches
        The resolved launch fan-out (:func:`resolve_sweep_launches`; length
        ≥ 1 — every launch shares the non-symbol axes by construction).
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    results_root
        The results volume root; ``None`` ⇒ :func:`default_results_root`.
    today
        Injectable clock forwarded to the gate (``None`` ⇒ the current IST
        date inside the gate).
    env
        Environment mapping forwarded to the estimator's per-tuple resolver
        (tests inject; ``None`` = the process environment).

    Returns
    -------
    dict[str, object]
        The merged JSON-safe payload: ``ok`` / unioned ``refusals`` +
        ``refusal_text`` / summed ``estimate`` + ``headline`` / unioned
        ``warnings`` + ``notes`` / the per-launch ``launches`` breakdown /
        ``confirm_token`` (non-None ONLY when every launch passed).

    Raises
    ------
    ValueError
        When *launches* is empty (a resolver bug — never a user shape).
    """
    from brutex.dashboard.lake_revision import lake_revision
    from brutex.engine.sweep_estimate import (
        ESTIMATE_SCHEMA_VERSION,
        selection_confirm_token,
        selection_from_raw,
    )

    if not launches:
        raise ValueError("preflight_sweep_request needs at least one resolved launch")
    out_root = results_root if results_root is not None else default_results_root(lake_root)
    outcomes: list[tuple[SweepRunRequest, SweepPreflightOutcome]] = [
        (
            request,
            _run_launch_preflight(
                request, lake_root=lake_root, out_root=out_root, today=today, env=env
            ),
        )
        for request in launches
    ]

    ok = all(outcome.ok for _, outcome in outcomes)
    refusals: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    text_parts: list[str] = []
    for request, outcome in outcomes:
        for refusal in outcome.refusals:
            key = (str(refusal.code), refusal.message)
            if key in seen:
                continue
            seen.add(key)
            refusals.append(
                {
                    "code": str(refusal.code),
                    "message": refusal.message,
                    "exchange": request.exchange,
                    "segment": request.segment,
                    "symbols": request.symbols,
                }
            )
        if not outcome.ok:
            text_parts.append(
                f"launch {request.exchange}/{request.segment} · {request.symbols}:\n"
                f"{outcome.refusal_text()}"
            )
    refusal_text = "" if ok else "\n\n".join(text_parts)

    estimates = [outcome.estimate for _, outcome in outcomes]
    trials = sum(estimate.trials for estimate in estimates)
    tuples = sum(estimate.tuples for estimate in estimates)
    store_bytes = sum(estimate.store_bytes for estimate in estimates)
    sidecar_bytes = sum(estimate.sidecar_bytes for estimate in estimates)
    audit_bytes = sum(estimate.audit_bytes for estimate in estimates)
    dedup_map_bytes = sum(estimate.dedup_map_bytes for estimate in estimates)
    est_result_bytes = sum(estimate.est_result_bytes for estimate in estimates)
    est_seconds = sum(estimate.est_seconds for estimate in estimates)
    est_seconds_fallback = sum(estimate.est_seconds_fallback for estimate in estimates)
    rows_upper_bound = sum(estimate.rows_upper_bound for estimate in estimates)
    provisional = any(estimate.provisional for estimate in estimates)
    absurd_k = any(estimate.absurd_k for estimate in estimates)
    # Every launch shares out_root + hardware, so the calibration source (and
    # thus the time/bytes model) is structurally identical across the fan-out —
    # the first launch's labels are THE labels.
    first_estimate = estimates[0]
    headline = _merged_headline(
        trials=trials,
        est_seconds=est_seconds,
        display_bytes=store_bytes + sidecar_bytes + audit_bytes,
        dedup_map_bytes=dedup_map_bytes,
        provisional=provisional,
        calibration_samples=first_estimate.calibration_samples,
        calibration_last_ts=first_estimate.calibration_last_ts,
    )
    warnings: list[dict[str, str]] = []
    seen_warnings: set[tuple[str, str]] = set()
    notes: list[str] = []
    for estimate in estimates:
        for warning in estimate.warnings:
            warning_key = (warning.code, warning.text)
            if warning_key not in seen_warnings:
                seen_warnings.add(warning_key)
                warnings.append({"code": warning.code, "text": warning.text})
    for _, outcome in outcomes:
        for note in outcome.notes:
            if note not in notes:
                notes.append(note)

    confirm_token: str | None = None
    if ok:
        first_request, first_outcome = outcomes[0]
        token_selection = selection_from_raw(
            symbols=[symbol for request, _ in outcomes for symbol in request.symbols.split(",")],
            intervals=first_request.intervals.split(","),
            directions=first_request.directions.split(","),
            k_values=[int(token) for token in first_request.k.split(",")],
            stop_losses=_leg_paisa_values(first_request.sl),
            take_profits=_leg_paisa_values(first_request.tp),
            fib_anchors=(DEFAULT_FIB_ANCHORS,),
            condition_bits=None,
            top_k=first_request.top,
            start=first_outcome.clamp.start,
            end=first_outcome.clamp.end,
        )
        confirm_token = selection_confirm_token(
            token_selection,
            launches=tuple((request.exchange, request.segment) for request, _ in outcomes),
            lake_token=lake_revision(lake_root),
        )

    return {
        "schema_version": ESTIMATE_SCHEMA_VERSION,
        "ok": ok,
        "refusals": refusals,
        "refusal_text": refusal_text,
        "headline": headline,
        "estimate": {
            "trials": trials,
            "tuples": tuples,
            "rows_upper_bound": rows_upper_bound,
            "est_result_bytes": est_result_bytes,
            "store_bytes": store_bytes,
            "sidecar_bytes": sidecar_bytes,
            "audit_bytes": audit_bytes,
            "dedup_map_bytes": dedup_map_bytes,
            "est_seconds": est_seconds,
            "est_hours": est_seconds / 3600.0,
            "est_seconds_fallback": est_seconds_fallback,
            "est_hours_fallback": est_seconds_fallback / 3600.0,
            "time_model": first_estimate.time_model,
            "bytes_model": first_estimate.bytes_model,
            "provisional": provisional,
            "calibration_samples": first_estimate.calibration_samples,
            "calibration_last_ts": first_estimate.calibration_last_ts,
            "absurd_k": absurd_k,
        },
        "warnings": warnings,
        "notes": notes,
        "launches": [
            {
                "exchange": request.exchange,
                "segment": request.segment,
                "symbols": request.symbols,
                "payload": outcome.payload(),
            }
            for request, outcome in outcomes
        ],
        "confirm_token": confirm_token,
        "acknowledge_sla": launches[0].acknowledge_sla,
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
    """
    from brutex.cli import cmd_sweep_all

    return cmd_sweep_all(
        **build_sweep_kwargs(request, lake_root=lake_root, results_root=results_root)
    )


__all__ = [
    "DEFAULT_DIRECTIONS",
    "DEFAULT_EXCHANGE",
    "DEFAULT_FIB_ANCHORS",
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
    "preflight_sweep_request",
    "resolve_sweep_launches",
    "resolve_sweep_request",
    "run_sweep_from_request",
    "sweep_progress_file",
]
