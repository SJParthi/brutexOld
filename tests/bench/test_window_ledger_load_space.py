"""Per-row SPACE pin for the window-ledger load's interned decode.

DEC-LEDGER-LOAD-SPACE-V1-001 (the Breaker-B finding-1 space audit, measured on
the real classes at b73d3b4): ``load_window_ledger`` materialises EVERY
recorded row, and ``pyarrow``'s ``to_pylist()`` decodes a DISTINCT ``str``
object per CELL — even the constant ``"1minute"`` shared nothing across rows —
measured **794 B/row logical (tracemalloc) / 1,305 B/row RSS** on an
87,573-row real-writer fixture (~60-100× the ~13 B/row on disk). The per-load
intern memo (``_INTERNED_COLUMNS`` — market / security_id / interval /
window dates / status / last_error; timestamps deliberately excluded)
collapses equal cells onto one object.

MEASURED on this harness's 36,053-unique-row realistic fixture (Linux x86-64,
CPython 3.12, 2026-07-09 sandbox):

* interned (the real ``load_window_ledger``): **479 B/row logical**;
* uninterned pre-fix decode shape (the teeth twin below): **770 B/row logical**
  — ratio **1.61×** (~38% saved, matching the design's ~40% projection);
* wall (no tracemalloc): 13.2 µs/row interned vs 12.2 µs/row for the raw
  uninterned decode — the memo's dict op adds ~1 µs/row against an
  I/O-dominated load (the repo's own db_view figure is 13.6 µs/row).

HONEST envelope (Golden Rule #10): tracemalloc LOGICAL bytes, not RSS — the
per-row figure depends on the CPython build's object sizes (str header,
dataclass slots), so the ceilings carry headroom for build drift but the
NUMBERS above are this machine's; the pin is calibrated so a memo REVERT
(770 B/row measured) fails the absolute ceiling AND the ratio floor — the
executable-teeth precedent (DEC-FETCH-PER-WINDOW-GUARANTEE-001). This v1
reduces the PER-ROW constant only: the ledger base itself still grows without
bound (the F1-FU1 archival follow-up — named, not claimed).
"""

from __future__ import annotations

import gc
import random
import tracemalloc
from pathlib import Path
from typing import Any

import brutex_identity
from brutex.data.window_ledger import WindowLedger
from brutex.data.window_ledger_store import (
    ledger_path,
    load_window_ledger,
    save_window_ledger,
)

#: The ledger schema the uninterned teeth twin replays the pre-fix decode over —
#: the kernel's ``LEDGER_COLUMNS`` as a tuple (the ONE schema authority; the
#: store's former ``_COLUMNS`` re-wrap is gone — DEC-RUST-RUNID-STORES-001).
_COLUMNS: tuple[str, ...] = tuple(brutex_identity.LEDGER_COLUMNS)

#: Rows generated (unique window keys land slightly lower — the count is read
#: off the loaded ledger, never assumed).
_N_ROWS = 40_000

#: Absolute per-row ceiling for the REAL (interned) load — measured 479 B/row;
#: the pre-fix uninterned decode measures 770 B/row, so a memo revert FAILS
#: this ceiling while ~35% headroom absorbs allocator/build noise.
_INTERNED_CEILING_B_PER_ROW = 640.0

#: Ratio floor: the uninterned twin must cost at least this much more per row
#: than the real load (measured 1.61×) — the teeth can never go vacuous by
#: both shapes drifting together.
_RATIO_FLOOR = 1.25

_UNDERLYINGS = (
    "NIFTY",
    "BANKNIFTY",
    "SENSEX",
    "RELIANCE",
    "HDFCBANK",
    "TCS",
    "INFY",
    "ICICIBANK",
    "SBIN",
    "BAJFINANCE",
    "MARUTI",
    "ASIANPAINT",
    "AXISBANK",
)
_EXPIRIES = (
    "02Jan25",
    "09Jan25",
    "16Jan25",
    "23Jan25",
    "30Jan25",
    "06Feb25",
    "13Feb25",
    "20Feb25",
    "27Feb25",
    "06Mar25",
    "27Mar25",
    "24Apr25",
    "29May25",
    "26Jun25",
)
_STATUSES = ("SUCCESS",) * 60 + ("SKIPPED",) * 20 + ("EMPTY",) * 15 + ("FAILED",) * 5


def _build_rows(n: int, seed: int = 42) -> list[tuple[Any, ...]]:
    """A production-shaped ledger: options 2 windows/contract, futures 3, spot 4.

    Mirrors what the pull writes (the Breaker-B ``_common.build_rows``
    methodology): Groww option/future symbols, ``1minute`` only, full µs-ISO
    timestamps, a small FAILED share carrying one repeated error string.
    """
    rng = random.Random(seed)
    rows: list[tuple[Any, ...]] = []
    i = 0
    while len(rows) < n:
        i += 1
        kind = rng.random()
        if kind < 0.90:
            u = rng.choice(_UNDERLYINGS)
            exp = rng.choice(_EXPIRIES)
            strike = rng.randrange(15_000, 60_000, 50)
            side = rng.choice(("CE", "PE"))
            market = "groww-option"
            sid = f"NSE-{u}-{exp}-{strike}-{side}"
            n_windows = 2
        elif kind < 0.95:
            u = rng.choice(_UNDERLYINGS)
            exp = rng.choice(_EXPIRIES)
            market = "groww-future"
            sid = f"NSE-{u}-{exp}-FUT"
            n_windows = 3
        else:
            market = "groww"
            sid = rng.choice(_UNDERLYINGS)
            n_windows = 4
        base_m = rng.randrange(1, 11)
        for w in range(n_windows):
            if len(rows) >= n:
                break
            status = rng.choice(_STATUSES)
            d0 = f"2025-{base_m:02d}-{(1 + w * 7) % 28 + 1:02d}"
            d1 = f"2025-{base_m:02d}-{(1 + w * 7 + 6) % 28 + 1:02d}"
            bars = rng.randrange(0, 9_000) if status == "SUCCESS" else 0
            last_error = None if status != "FAILED" else "GrowwTimeoutError: read timed out"
            first_seen = (
                f"2026-07-{rng.randrange(1, 9):02d}"
                f"T0{rng.randrange(1, 9)}:1{w}:33.4{i % 10}1236+00:00"
            )
            last_attempt = f"2026-07-08T1{rng.randrange(0, 9)}:2{w}:07.99{i % 10}104+00:00"
            rows.append(
                (
                    market,
                    sid,
                    "1minute",
                    d0,
                    d1,
                    status,
                    bars,
                    rng.randrange(1, 4),
                    last_error,
                    first_seen,
                    last_attempt,
                    rng.randrange(0, 3),
                    rng.randrange(0, 4),
                    rng.choice((None, 0, bars)),
                    rng.choice((None, 0, 1, 2)),
                    rng.choice((None, 0, 1)),
                )
            )
    return rows


def _uninterned_load(lake_root: Path) -> WindowLedger:
    """The PRE-FIX decode shape — the executable teeth twin.

    Reads the SAME Parquet through ``to_pylist()`` with NO memo (one distinct
    ``str`` per cell — exactly what ``_read_ledger_file_rows`` produced before
    the interned decode) and folds it through the SAME
    :meth:`WindowLedger.from_rows`, so the two measurements differ ONLY by the
    memo.
    """
    import pyarrow.parquet as pq

    table = pq.read_table(ledger_path(lake_root))  # type: ignore[no-untyped-call, unused-ignore]
    cols = [table.column(name).to_pylist() for name in _COLUMNS]
    return WindowLedger.from_rows(list(zip(*cols, strict=True)))


def _traced_per_row(load: Any) -> float:
    """tracemalloc bytes per RESIDENT row of holding *load*'s result."""
    gc.collect()
    tracemalloc.start()
    ledger = load()
    gc.collect()
    current, _peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    n = ledger.count
    del ledger
    gc.collect()
    return current / n


def test_interned_load_per_row_space_pin(tmp_path: Path) -> None:
    """The real load stays under the ceiling; the uninterned twin proves teeth."""
    ledger = WindowLedger.from_rows(_build_rows(_N_ROWS))
    save_window_ledger(ledger, tmp_path)
    del ledger
    # Warm both read paths once (pyarrow pools / lazy imports) so the measured
    # deltas are the ROWS, not one-time allocator arenas.
    load_window_ledger(tmp_path)
    _uninterned_load(tmp_path)
    gc.collect()

    interned = _traced_per_row(lambda: load_window_ledger(tmp_path))
    uninterned = _traced_per_row(lambda: _uninterned_load(tmp_path))

    # The absolute pin (measured 479 B/row; pre-fix 770 — a memo revert fails).
    assert interned <= _INTERNED_CEILING_B_PER_ROW, (
        f"interned load costs {interned:.0f} B/row > ceiling "
        f"{_INTERNED_CEILING_B_PER_ROW:.0f} (measured 479 pre-regression)"
    )
    # The teeth: the uninterned decode is genuinely, measurably worse (1.61×
    # measured) — so the ceiling above is a REAL bound, not a vacuous one.
    assert uninterned / interned >= _RATIO_FLOOR, (
        f"teeth lost: uninterned {uninterned:.0f} B/row vs interned "
        f"{interned:.0f} B/row (ratio {uninterned / interned:.2f} < {_RATIO_FLOOR})"
    )


def test_interned_and_uninterned_rows_are_value_identical(tmp_path: Path) -> None:
    """The memo changes object IDENTITY only — the row values are byte-identical."""
    ledger = WindowLedger.from_rows(_build_rows(2_000, seed=7))
    save_window_ledger(ledger, tmp_path)
    assert load_window_ledger(tmp_path).to_rows() == _uninterned_load(tmp_path).to_rows()
