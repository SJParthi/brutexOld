"""The FIXED per-trade feature vector for the meta-label P(win) model
(DEC-META-LABEL-ANNOTATE-001 — AFML Ch. 3 meta-labeling: the primary model
already decided WHEN to trade; this secondary model estimates the
probability the trade WINS from its entry snapshot).

The vector is a small, DOCUMENTED constant — :data:`FEATURE_NAMES` — so the
Rust inference kernel's O(features)/trade cost is O(1)/trade by construction
(``rust/brutex-meta/src/score.rs``). Every feature derives from PERSISTED
trade rows (``brutex/engine/trade_store.py`` TRADE_COLUMNS) plus the
:class:`brutex.engine.session_lens.SessionLens` date lookups the
results-depth aggregates already use — nothing is re-simulated, nothing
new is measured.

ENTRY-OBSERVABILITY LAW (the hostile-review round-1 MAJOR-1 fix): the lens
labels session ``D`` only AFTER folding day ``D``'s FULL daily OHLC bar
(``session_lens.build_session_lens``), so for an intraday trade entered
mid-day ``D`` the same-day label encodes post-entry information. The regime
one-hots therefore read the PRIOR session's label — the nearest lens date
STRICTLY BEFORE the entry's IST date (:func:`prior_session_regime`; no
prior session → ``"unknown"``) — and are NAMED ``prev_regime_*`` so the
semantics live in the wire name. The event-day flag stays at the ENTRY
date: every event class is a calendar fact (expiry/muhurat/budget-weekend/
rebalance) knowable in advance, never derived from day-``D`` prices —
property-pinned (mutating day ``D``'s + later days' OHLC in the lens
inputs leaves a day-``D`` trade's vector byte-identical).

W4 FOLD-KEY LAW (lane coordination, 2026-07-18): every derivation here keys
on ``trade_seq`` + entry/exit TIMESTAMPS only — NEVER on bar indices or
``holding_bars``-derived quantities, because post-PR-B those become
1-minute-grid quantities and an index-keyed annotation would silently change
meaning. (No index/holding feature exists in this vector; this note pins the
law for future columns.)

Standardization note: ``vix_entry`` is emitted RAW here (missing → ``0.0`` +
the ``vix_missing`` indicator); the per-fold standardization (mean/std
computed on TRAINING rows whose ``vix_missing`` indicator is 0 ONLY — never
the test fold, and never the ``0.0`` placeholder rows, which keep exactly
``0.0`` + indicator — the leakage law + the round-1 M-6 fix) is applied
inside :mod:`brutex.meta.cpcv_fit` at fit time.

Total + deterministic: :func:`trade_features_from_wire` never raises on a
malformed row — it returns ``None`` (the caller counts the skip; the
correct-or-absent law). No I/O, no wall clock, no RNG.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING, Final

from brutex.engine.session_lens import SessionLens, entry_hour_ist, ist_trade_date

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Mapping

__all__ = [
    "FEATURE_NAMES",
    "N_FEATURES",
    "REGIME_TOKENS",
    "TradeFeatures",
    "prior_session_regime",
    "trade_features_from_wire",
]

#: NSE session entry hours get their own one-hot bucket (a regular session
#: yields 9..15 IST — CLAUDE.md §7); anything else (e.g. an evening Muhurat
#: session's 17-21h) folds into ``hour_other``.
_SESSION_HOURS: Final[tuple[int, ...]] = (9, 10, 11, 12, 13, 14, 15)

#: The regime one-hot tokens, in emission order — the
#: :class:`~brutex.engine.session_lens.TrendRegime` values plus the
#: no-prior-session/lens-miss token ``"unknown"`` (the ``combo_aggregates``
#: convention).
REGIME_TOKENS: Final[tuple[str, ...]] = ("up", "down", "sideways", "unknown")

#: THE fixed documented feature vector — order is the wire contract the
#: fitted weights + the Rust kernel + every ``.meta.json`` echo share.
#: 16 features: intercept · raw VIX + missing indicator · 8 entry-hour
#: one-hots · 4 PRIOR-SESSION regime one-hots (``prev_regime_*`` — the
#: entry-observability law in the module docstring) · the event-day flag.
FEATURE_NAMES: Final[tuple[str, ...]] = (
    "intercept",
    "vix_entry",
    "vix_missing",
    *(f"hour_{hour:02d}" for hour in _SESSION_HOURS),
    "hour_other",
    *(f"prev_regime_{token}" for token in REGIME_TOKENS),
    "event_day",
)

#: The fixed vector width (the Rust kernel's ``n_features``).
N_FEATURES: Final[int] = len(FEATURE_NAMES)


@dataclass(frozen=True, slots=True)
class TradeFeatures:
    """One trade's extracted feature vector + label + provenance snapshot.

    ``vector`` is ordered exactly by :data:`FEATURE_NAMES`. ``label_win`` is
    ``pnl_paisa > 0`` (a zero-P&L scratch trade labels as a LOSS — the
    conservative reading, documented). ``prev_regime`` (the PRIOR session's
    trend label — the entry-observability law) / ``vix_entry`` /
    ``event_day`` are kept raw so the abstention replay
    (:mod:`brutex.meta.annotate`) judges the SAME snapshot the model saw.
    """

    trade_seq: int
    entry_ts_us: int
    exit_ts_us: int
    vector: tuple[float, ...]
    label_win: bool
    prev_regime: str
    vix_entry: float | None
    event_day: bool


def _parse_ts_us(value: object) -> int | None:
    """Parse a wire ISO-8601 UTC timestamp to epoch microseconds, or ``None``.

    Accepts the ``trade_store`` wire spelling (tz-aware ISO string) and —
    for direct parquet-record callers — an exact ``int`` epoch-µs. A naive
    string, junk type, or unparsable value reads ``None`` (counted skip):
    a naive timestamp would silently shift the IST session date.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    # Exact integer conversion (the trade_store `_ts_to_us` idiom) — a float
    # `.timestamp()` round-trip could round a sub-second instant by ±0.5 µs.
    return (parsed - _EPOCH_UTC) // timedelta(microseconds=1)


#: The exact epoch anchor (the trade_store conversion twin).
_EPOCH_UTC: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)


def _ts_from_us(ts_us: int) -> datetime | None:
    """Epoch-µs → tz-aware UTC datetime, or ``None`` when out of range."""
    try:
        return _EPOCH_UTC + timedelta(microseconds=ts_us)
    except OverflowError:
        return None


def prior_session_regime(lens: SessionLens, entry_date: date) -> str:
    """The PRIOR session's trend label for a trade entered on *entry_date*.

    The entry-observability law (module docstring): the lens labels day
    ``D`` only after day ``D``'s FULL daily bar, so an intraday trade
    entered mid-day ``D`` may consult sessions STRICTLY BEFORE ``D`` only.
    The nearest lens date ``< entry_date`` wins; no prior session (or an
    empty lens) reads ``"unknown"`` — never the same-day label.

    HONEST O(): one O(lens sessions) scan per call (a lens holds a few
    thousand dates at most; no sorted index is built — the annotation pass
    is offline).
    """
    prior: date | None = None
    for day in lens.trend_by_date:
        if day < entry_date and (prior is None or day > prior):
            prior = day
    if prior is None:
        return "unknown"
    return lens.trend_by_date[prior].value


def trade_features_from_wire(
    row: Mapping[str, object], *, lens: SessionLens
) -> TradeFeatures | None:
    """Extract one trade's feature vector from a wire/stored row — TOTAL.

    *row* is a :class:`~brutex.engine.trade_store.TradeRowWire`-shaped
    mapping (the ``iter_trades_for_export`` shape): ``trade_seq`` (int),
    ``entry_ts``/``exit_ts`` (tz-aware ISO strings; exact epoch-µs ints
    also accepted), ``pnl_paisa`` (int), ``vix_entry`` (float | None).
    A malformed row — missing key, wrong type, naive/unparsable timestamp,
    out-of-range instant, non-finite VIX — returns ``None`` (the caller
    counts the skip; never raises, never fabricates a feature).

    Derivations (all timestamp-keyed — the W4 fold-key law):

    * entry hour → :func:`~brutex.engine.session_lens.entry_hour_ist`
      one-hot over :data:`_SESSION_HOURS` + ``hour_other``;
    * PRIOR-session trend regime → :func:`prior_session_regime` (the
      nearest lens date STRICTLY BEFORE the entry IST date — the
      entry-observability law; no prior → ``"unknown"``);
    * event day → ``lens.event_flags_by_date`` at the entry IST date
      (ANY of the four verified classes — calendar facts, knowable in
      advance, never derived from day-``D`` prices);
    * ``vix_entry`` raw with missing → ``0.0`` + the ``vix_missing``
      indicator (a NaN/±inf stamp reads missing — a NaN feature would
      poison the fit silently).
    """
    trade_seq = row.get("trade_seq")
    pnl = row.get("pnl_paisa")
    if not isinstance(trade_seq, int) or isinstance(trade_seq, bool):
        return None
    if not isinstance(pnl, int) or isinstance(pnl, bool):
        return None
    entry_us = _parse_ts_us(row.get("entry_ts"))
    exit_us = _parse_ts_us(row.get("exit_ts"))
    if entry_us is None or exit_us is None:
        return None
    entry_dt = _ts_from_us(entry_us)
    if entry_dt is None:
        return None

    vix_raw = row.get("vix_entry")
    vix: float | None = None
    if isinstance(vix_raw, (int, float)) and not isinstance(vix_raw, bool):
        vix = float(vix_raw)
        if not isfinite(vix):
            vix = None

    entry_date = ist_trade_date(entry_dt)
    hour = entry_hour_ist(entry_dt)
    prev_regime = prior_session_regime(lens, entry_date)
    event_day = bool(lens.event_flags_by_date.get(entry_date, ()))

    vector = [1.0, vix if vix is not None else 0.0, 0.0 if vix is not None else 1.0]
    vector.extend(1.0 if hour == h else 0.0 for h in _SESSION_HOURS)
    vector.append(1.0 if hour not in _SESSION_HOURS else 0.0)
    vector.extend(1.0 if prev_regime == token else 0.0 for token in REGIME_TOKENS)
    vector.append(1.0 if event_day else 0.0)

    return TradeFeatures(
        trade_seq=trade_seq,
        entry_ts_us=entry_us,
        exit_ts_us=exit_us,
        vector=tuple(vector),
        label_win=pnl > 0,
        prev_regime=prev_regime,
        vix_entry=vix,
        event_day=event_day,
    )
