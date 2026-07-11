"""Type stubs for the `fno_math` Rust extension (DEC-P0-055 / PR-N+2).

Every public symbol re-exported by `__init__.py` is declared here so
that `from fno_math import X` resolves under pyright-strict + mypy-strict
without `# pyright: ignore` shims.

The signatures mirror PyO3 0.28.3's runtime extract behavior (the version
pinned in `rust/fno-math/Cargo.lock:520-521`):
- Rust `u64` ⇆ Python `int`
- Rust `Vec<u64>` ⇆ Python `list[int]`
- Rust `&[u8]` / `PyBytes` ⇆ Python `bytes`

Length-mismatch and overflow conditions raise `ValueError` / `OverflowError`
at runtime; pyright does not encode raised exceptions in stubs, so callers
must consult the docstrings below.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "AuditEnvelopeError",
    "CalendarVerdictEngine",
    "ResidentBars",
    "ResidentSweepSim",
    "ResidentSweepSimU128",
    "__version__",
    "add_u64",
    "audit_entry_hash",
    "audit_entry_line",
    "bitand_count_u64s",
    "bitcount_u64s",
    "blake3_hash",
    "canonical_json",
    "count_supermask_u64s",
    "count_supermasks_u64s",
    "count_supermasks_u64s_nthreads",
    "expected_bars_per_day_for_tf",
    "forced_exit_seconds_s",
    "is_forced_exit_bar_s",
    "popcount_bytes",
    "py_float_repr",
    "session_last_bar_complete_s",
]

__version__: str

__build_crate_version__: str
"""Crate version of the INSTALLED compiled kernel (build stamp, not API).

Derived at import from the compiled module's ``CARGO_PKG_VERSION``-baked
``__version__``; read by ``brutex doctor``'s fno_math wheel-freshness stage
(DEC-FNOMATH-WHEEL-FRESHNESS-001). Deliberately NOT in ``__all__``.
"""

_EXPECTED_COMPILED_SYMBOLS: tuple[str, ...]

def _assert_compiled_surface(
    compiled: object,
    expected: Sequence[str] = ...,
) -> None:
    """Raise an actionable ``ImportError`` if ``compiled`` misses an expected symbol."""
    ...

class AuditEnvelopeError(Exception):
    """A value is outside the Rust audit-chain kernel's bounded model.

    DEC-AUDIT-CHAIN-RUST-PORT-001 (slice 9, ADR-0019): raised by
    ``audit_entry_hash`` / ``audit_entry_line`` / ``py_float_repr`` /
    ``canonical_json`` on ANY out-of-model input (non-finite float, int
    outside i64, non-str dict key, subclassed container, lone-surrogate
    string, unsupported type, ``|`` in a free-form field). The Python seam
    in ``brutex.orchestrator.audit`` catches it and falls back LOUDLY to
    the retained Python oracle — the type never escapes the seam.
    """

def audit_entry_hash(
    seq: int,
    prev_hash: str,
    ts_utc: float,
    actor: str,
    action: str,
    subject: str,
    payload: object,
) -> str:
    """``AuditEntry.compute_hash``'s Rust twin — SHA-256 hexdigest of the
    ``|``-joined preimage over the bounded value model.

    Raises ``AuditEnvelopeError`` on any out-of-model value; ``OverflowError``
    for ``seq`` outside i64 (PyO3 conversion).
    """
    ...

def audit_entry_line(
    seq: int,
    prev_hash: str,
    ts_utc: float,
    actor: str,
    action: str,
    subject: str,
    payload: object,
    entry_hash: str,
) -> str:
    """``AuditEntry.to_json_line``'s Rust twin — the canonical 8-key JSON
    line (no trailing newline), byte-equal to the Python writer.

    Raises ``AuditEnvelopeError`` / ``OverflowError`` as ``audit_entry_hash``.
    """
    ...

def py_float_repr(x: float) -> str:
    """CPython ``repr(float)`` / ``str(float)`` — shortest round-trip digits
    + the pystrtod format-code-'r' notation. Raises ``AuditEnvelopeError``
    on a non-finite input.
    """
    ...

def canonical_json(payload: object) -> str:
    """``brutex.orchestrator.audit._canonical_json``'s Rust twin (json.dumps
    ``sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    allow_nan=False``). Raises ``AuditEnvelopeError`` out of model.
    """
    ...

class CalendarVerdictEngine:
    """Frozen NSE/BSE calendar-verdict engine (DEC-CALENDAR-VERDICT-RUST-001).

    Built once per process by ``brutex.calendars.nse`` from the live module
    constants — calendar DATA stays in Python (ADR-0012 D8) and crosses here
    as ``date.toordinal()`` ordinals + seconds-of-day. ``sel``: 0 = ALL
    (``exchange=None``), 1 = NSE, 2 = BSE — exchange STRING validation stays
    Python. ``*_checked`` verdicts: 0 = False/None, 1 = True/Some, 2 = RAISE
    (the Python seam raises ``HolidayDataUnavailableError``).
    """

    def __init__(
        self,
        common_holidays: list[int],
        nse_only: list[int],
        bse_only: list[int],
        muhurat: list[tuple[int, int, int]],
        special_weekend: list[tuple[int, int, int]],
        coverage_first_year: int,
        coverage_last_year: int,
        session_open_s: int,
        session_close_s: int,
    ) -> None: ...
    def is_holiday(self, ordinal: int, sel: int) -> bool: ...
    def is_holiday_checked(self, ordinal: int, sel: int) -> int: ...
    def is_weekend(self, ordinal: int) -> bool: ...
    def is_trading_day(self, ordinal: int, sel: int) -> bool: ...
    def is_trading_day_checked(self, ordinal: int, sel: int) -> int: ...
    def has_muhurat(self, ordinal: int) -> bool: ...
    def is_special_weekend(self, ordinal: int) -> bool: ...
    def get_session(self, ordinal: int, sel: int) -> tuple[int, int, int] | None:
        """``(session_type, open_s, close_s)`` — type 0 REGULAR / 1 MUHURAT."""
        ...

    def get_session_checked(self, ordinal: int, sel: int) -> tuple[int, int, int, int]:
        """``(verdict, session_type, open_s, close_s)`` — verdict 0/1/2."""
        ...

    def previous_trading_session(
        self, ordinal: int, sel: int, observed: list[int]
    ) -> tuple[int, int]:
        """``(1, prev_ordinal)`` or ``(2, probe_ordinal_that_raised)``."""
        ...

    def trading_days_between(self, start: int, end: int, sel: int) -> list[int]: ...
    def trading_days_between_checked(
        self, start: int, end: int, sel: int
    ) -> tuple[int, list[int], int]:
        """``(verdict, day_ordinals, offending_ordinal)`` — verdict 1/2."""
        ...

    def daily_derive_cutoff(self, ordinal: int, now_us: int, sel: int) -> int:
        """The cutoff ORDINAL (strict ``>`` at close; ``now_us`` =
        microseconds-of-day)."""
        ...

def expected_bars_per_day_for_tf(tf_minutes: int) -> int:
    """``ceil(375 / tf)``; ``tf >= 375`` → 1; ``tf < 1`` → ``-1`` (the
    Python wrapper raises ``ValueError`` before calling)."""
    ...

def is_forced_exit_bar_s(open_s: int, tf_minutes: int, boundary_s: int) -> int:
    """1/0 verdict of the strict-``>`` forced-exit law over seconds-of-day;
    ``tf < 1`` → ``-1`` (the Python wrapper raises before calling)."""
    ...

def forced_exit_seconds_s(close_s: int) -> int:
    """Session close − 600 s (saturating) — the forced-exit boundary."""
    ...

def session_last_bar_complete_s(close_s: int, last_bar_s: int) -> bool:
    """``last_bar + 1 minute >= close`` over SAME-DATE seconds-of-day."""
    ...

def add_u64(a: int, b: int) -> int:
    """Add two `u64` values.

    Raises `OverflowError` if `a + b >= 2**64`.
    """
    ...

def bitcount_u64s(values: list[int]) -> int:
    """Sum of `popcount(v)` over a list of `u64` values."""
    ...

def bitand_count_u64s(a: list[int], b: list[int]) -> int:
    """Sum of `popcount(a[i] & b[i])` over two equal-length `u64` lists.

    Raises `ValueError` if `len(a) != len(b)`.
    """
    ...

def blake3_hash(data: bytes) -> bytes:
    """Return the 32-byte BLAKE3 digest of `data`."""
    ...

def count_supermask_u64s(bar_bits: list[int], mask: int) -> int:
    """Count bars that are a SUPERSET of a single `mask`.

    Returns the number of `bar` in `bar_bits` with `(bar & mask) == mask`.
    `mask == 0` matches every bar (returns `len(bar_bits)`).
    """
    ...

def count_supermasks_u64s(bar_bits: list[int], masks: list[int]) -> list[int]:
    """Batched per-mask SUPERSET count — the Stage-2 hot kernel.

    Returns a list of length `len(masks)` where `out[j]` is the number of
    `bar` in `bar_bits` with `(bar & masks[j]) == masks[j]`. Positionally
    aligned with `masks`. Empty `masks` → `[]`; empty `bar_bits` → `0` per
    mask.
    """
    ...

def count_supermasks_u64s_nthreads(
    bar_bits: list[int], masks: list[int], n_threads: int
) -> list[int]:
    """BENCH-ONLY: batched SUPERSET count on a SCOPED rayon pool of `n_threads`.

    DEC-RUST-RAYON-SUPERMASK-001 (MIGRATION.md #414). Production uses rayon's
    DEFAULT global pool via `count_supermasks_u64s`; this shim exists only so
    the scaling bench + thread-count-independence parity tests can pin a worker
    count without touching the global pool. The result is bit-identical to
    `count_supermasks_u64s` for any `n_threads >= 1` (order-preserving collect).
    `n_threads == 0` lets rayon choose its default count. Raises `ValueError` if
    the scoped pool fails to build.
    """
    ...

def popcount_bytes(data: bytes) -> int:
    """Return the total set-bit count across a byte buffer."""
    ...

def sweep_drawdown_stats(
    n_bars: int,
    start_equity_paisa: int,
    exits: list[tuple[int, int]],
) -> tuple[int, float, int, float]:
    """Fused per-mask equity fold + drawdown scan (DEC-SWEEP-RUST-METRICS-001).

    `exits` is `(exit_bar_idx, pnl_paisa)` per closed trade. Returns
    `(max_dd_paisa, max_dd_pct, max_dd_duration_bars, ulcer_index)` exactly as
    `brutex.metrics.compute._drawdown_stats` returns them (percentages ×100),
    bit-identical inside the `start + Σ|pnl| ≤ 2**52` exactness envelope the
    Python wrapper pre-guards. Raises `ValueError` outside the envelope.
    """
    ...

class ResidentBars:
    """Resident bar buffer — holds `bar_bits` Rust-side across batch calls.

    The `O(N_bars)` Python→Rust copy is paid ONCE in `__init__`; subsequent
    `count_supermasks` calls marshal only `masks`. Frozen + immutable: only
    `n_bars` is exposed (a `bars` getter would copy `O(N)` per access).
    """

    def __init__(self, bar_bits: list[int]) -> None: ...
    @property
    def n_bars(self) -> int: ...
    def count_supermasks(self, masks: list[int]) -> list[int]: ...
    def count_supermask(self, mask: int) -> int: ...

class ResidentSweepSim:
    """Resident Stage-2 trade-sim buffer — OHLC paisa + forced flags + sessions + bar_bits.

    DEC-SWEEP-RUST-TRADESIM-001 (MIGRATION.md #563). The per-bar SL/TP/forced-exit
    state machine (the Python `TradeSimWalker`) ported to Rust + fused with the
    per-mask entry projection. The `O(N_bars)` Python→Rust copy of the columns +
    flags + session ordinals + bar_bits is paid ONCE in `__init__`; subsequent
    `simulate_masks` / `count_trades` calls marshal only `masks`. Frozen +
    immutable (only `n_bars` is exposed). All money is int64 paisa; the
    price→paisa conversion, the forced-exit detection AND the per-bar session
    identity (`session_idx[i]` = bar `i`'s IST calendar-date ordinal) are done in
    Python (the single source of truth) so the trades are bit-identical to
    `brutex.engine.trade_sim.simulate_trades`. A pending entry only fills on a
    bar with the SAME `session_idx` it armed on — a cross-session fill (the
    overnight-gap leak, R2-OC-2) is dropped.

    Unequal column lengths raise `ValueError`.
    """

    def __init__(
        self,
        open: list[int],
        high: list[int],
        low: list[int],
        close: list[int],
        is_forced: list[bool],
        session_idx: list[int],
        bar_bits: list[int],
    ) -> None: ...
    @property
    def n_bars(self) -> int: ...
    def simulate_masks(
        self,
        masks: list[int],
        is_buy: bool,
        sl_paisa: int | None,
        tp_paisa: int | None,
        sl_next_open: bool = ...,
        tp_next_open: bool = ...,
        tp_first: bool = ...,
    ) -> list[list[tuple[int, int, int, int, int, int]]]:
        """Simulate a batch of masks → per-mask round-trip trade tuples.

        `out[j]` is the trades for `masks[j]`, each a tuple
        `(pnl_paisa, mfe_paisa, mae_paisa, holding_bars, entry_idx, exit_idx)`.
        Empty `masks` → `[]`.
        """
        ...

    def count_trades(
        self,
        masks: list[int],
        is_buy: bool,
        sl_paisa: int | None,
        tp_paisa: int | None,
        sl_next_open: bool = ...,
        tp_next_open: bool = ...,
        tp_first: bool = ...,
    ) -> list[int]:
        """Per-mask round-trip trade COUNT only (the throughput-bench fast path).

        Same state machine as `simulate_masks` without the per-trade allocation.
        `out[j]` aligns with `masks[j]`.
        """
        ...

class ResidentSweepSimU128:
    """Resident Stage-2 trade-sim buffer over a >64-bit vocabulary (u128 twin).

    DEC-TRADE-SIM-U128-KERNEL-001. Identical contract to `ResidentSweepSim`
    with `bar_bits` / `masks` widened to u128 — both widths share the ONE
    width-generic MaskWord core in `rust/fno-math/src/trade_sim.rs`, so the
    walk semantics cannot drift (an anti-drift test pins the two kernels
    field-by-field equal on u64-fit inputs). A `bar_bits` / mask value outside
    `[0, 2**128)` raises `OverflowError` at the PyO3 extraction edge; unequal
    column lengths raise `ValueError`. Today's production vocabulary is 60
    bits, so this kernel is future headroom — only tests (and a future >64-bit
    vocabulary) drive it.
    """

    def __init__(
        self,
        open: list[int],
        high: list[int],
        low: list[int],
        close: list[int],
        is_forced: list[bool],
        session_idx: list[int],
        bar_bits: list[int],
    ) -> None: ...
    @property
    def n_bars(self) -> int: ...
    def simulate_masks(
        self,
        masks: list[int],
        is_buy: bool,
        sl_paisa: int | None,
        tp_paisa: int | None,
        sl_next_open: bool = ...,
        tp_next_open: bool = ...,
        tp_first: bool = ...,
    ) -> list[list[tuple[int, int, int, int, int, int]]]:
        """Simulate a batch of u128 masks → per-mask round-trip trade tuples.

        `out[j]` is the trades for `masks[j]`, each a tuple
        `(pnl_paisa, mfe_paisa, mae_paisa, holding_bars, entry_idx, exit_idx)`.
        Empty `masks` → `[]`.
        """
        ...

    def count_trades(
        self,
        masks: list[int],
        is_buy: bool,
        sl_paisa: int | None,
        tp_paisa: int | None,
        sl_next_open: bool = ...,
        tp_next_open: bool = ...,
        tp_first: bool = ...,
    ) -> list[int]:
        """Per-mask round-trip trade COUNT only (no per-trade allocation).

        `out[j]` aligns with `masks[j]`.
        """
        ...
