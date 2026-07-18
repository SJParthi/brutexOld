"""Shared harness for the pull-progress derived-metrics goldens (`DEC-RUST-PULL-STATE-001`).

Wave 2F of the Rust pull-state lane freezes the DERIVED-METRICS plane of
``brutex/dashboard/pull_progress.py`` (DEC-PULL-PROGRESS-HONESTY-001 — the
``ProgressSnapshot`` ``percent`` / ``elapsed_seconds`` / ``units_per_sec`` /
``eta_seconds`` properties) as the CONTRACT the Rust port must satisfy
bit-for-bit. This module is imported by BOTH the generator
(``generate_pull_progress_golden.py``) and the consuming test
(``test_pull_progress_golden.py``) — the ``_market_hours_guard_harness``
shape (never generator-imports-test, the CodeQL ``py/cyclic-import`` break).

The verdict table is computed by DRIVING the LIVE Python properties (the
oracle) — never by re-deriving their logic in the harness. The case lattice
covers the module's hostile boundaries:

* ``percent``: 0 totals, ``completed > total`` puns (the min-100 clamp),
  EXACT banker's-rounding ties (``completed/total`` on the 1/16 grid puts
  ``x*100`` on odd multiples of ``0.25`` — ``6.25% -> 6.2`` / ``18.75% ->
  18.8``, the half-EVEN teeth), repr traps (thirds), the ``99.96 -> 100.0``
  round-up-to-clamp boundary, and huge counts at the ``2**53`` kernel-domain
  ceiling (both operands still exactly representable — f64 division equals
  CPython's correctly-rounded int/int true division there).
* ``units_per_sec`` / ``eta_seconds``: the phase-baseline law (baseline
  ahead of ``completed`` -> 0.0), zero/negative spans, the open-ended and
  non-running honest-``None`` arms, ``remaining <= 0 -> 0.0``, and NaN/inf
  timestamps (IEEE-faithful comparison fall-throughs — a NaN span
  legitimately yields a NaN rate; pinned AS-IS, never "fixed").

Every float crossing a table is ALSO recorded as its IEEE-754 bit pattern
(``<f8`` little-endian hex via :func:`f64_bits`) so the fixture is bit-exact
across JSON round-trips AND trivially parseable by the serde-free cargo
fixture test (``rust/brutex-pull-state/tests/progress_golden.rs`` reads the
TSV projection). Non-finite floats travel ONLY as bit patterns (the JSON
payload stays ``allow_nan=False``-clean). NaN OUTPUT rows are compared
NaN-to-NaN (not bit-to-bit) by both consumers: IEEE leaves NaN *payload*
propagation implementation-defined across architectures, so a bit-compare of
a produced NaN would be a false pin (the inputs use the canonical quiet-NaN
pattern; the outputs assert ``isnan`` only).

Absolute clock instants use DYADIC values (integers + small powers of two)
so every ``a - b`` subtraction in the properties is EXACT — boundary rows
(zero span, equal instants) are genuinely on the boundary.
"""

from __future__ import annotations

import math
import struct
from typing import Any

from brutex.dashboard.pull_progress import (
    STATE_DONE,
    STATE_ERROR,
    STATE_IDLE,
    STATE_RUNNING,
    ProgressSnapshot,
)

#: The kernel count-domain ceiling (`DEC-RUST-PULL-STATE-001` Wave 2F): both
#: division operands exactly representable in f64, so the Rust kernel's f64
#: division equals CPython's correctly-rounded ``int/int`` true division.
COUNT_DOMAIN_MAX: int = 2**53

#: The canonical quiet-NaN / infinity bit patterns used for hostile inputs
#: (``struct.pack('<d', nan/inf).hex()`` — little-endian).
NAN_BITS: str = "000000000000f87f"
INF_BITS: str = "000000000000f07f"


def f64_bits(value: float) -> str:
    """The IEEE-754 bit pattern of *value* as 16 lowercase hex chars (``<f8``)."""
    return struct.pack("<d", value).hex()


def f64_from_bits(pattern: str) -> float:
    """Inverse of :func:`f64_bits`."""
    return struct.unpack("<d", bytes.fromhex(pattern))[0]


#: (total, completed, phase_base_completed) hostile lattice.
COUNT_TRIPLES: tuple[tuple[int, int, int], ...] = (
    (0, 0, 0),  # pre-start
    (0, 5, 0),  # total 0 with progress (percent must stay 0.0)
    (1, 0, 0),
    (1, 1, 0),
    (8, 1, 0),  # 12.5% — exact one-decimal value, no rounding
    (16, 1, 0),  # 6.25% — EXACT tie -> 6.2 (half-even DOWN)
    (16, 3, 0),  # 18.75% — EXACT tie -> 18.8 (half-even UP)
    (16, 10, 4),  # tie + a real phase baseline
    (3, 1, 0),  # 33.333…% — repr trap
    (3, 2, 0),  # 66.666…%
    (7, 3, 1),
    (400, 1, 0),  # 0.25% — sub-tenth tie shape
    (750, 375, 100),  # the universe-pull scale
    (1000, 999, 0),  # 99.9%
    (2500, 2499, 0),  # 99.96% -> rounds to 100.0 -> min-clamp boundary
    (4, 9, 0),  # completed > total pun (225% -> clamped 100.0)
    (10, 2, 5),  # baseline AHEAD of completed (negative progressed -> 0.0 rate)
    (33333, 12345, 678),
    (COUNT_DOMAIN_MAX, COUNT_DOMAIN_MAX - 1, 0),  # huge, still exact
    (COUNT_DOMAIN_MAX, COUNT_DOMAIN_MAX, COUNT_DOMAIN_MAX),
    (COUNT_DOMAIN_MAX, 1, 0),  # tiniest ratio at the ceiling
)

#: (updated_at, started_at, phase_started_at) — dyadic instants (exact
#: subtractions) + the canonical non-finite hostiles.
TIME_TRIPLES: tuple[tuple[float, float, float], ...] = (
    (1056.0, 1024.0, 1024.0),  # normal forward spans
    (1056.0, 1024.0, 1040.0),  # phase re-anchored mid-run
    (1024.0, 1024.0, 1024.0),  # zero spans (rate refused)
    (1000.0, 1024.0, 1024.0),  # clock skew: negative spans
    (1024.5, 1024.0, 1024.25),  # fractional dyadic
    (1e9 + 0.03125, 1e9, 1e9),  # large monotonic base, exact tiny span
    (0.0, 0.0, 0.0),  # never-started shape
    (f64_from_bits(NAN_BITS), 1024.0, 1024.0),  # NaN updated_at
    (f64_from_bits(INF_BITS), 1024.0, 1024.0),  # inf updated_at
    (1056.0, 1024.0, f64_from_bits(NAN_BITS)),  # NaN phase anchor
)

_ALL_STATES: tuple[str, ...] = (STATE_IDLE, STATE_RUNNING, STATE_DONE, STATE_ERROR)


def _snapshot(
    state: str,
    open_ended: bool,
    counts: tuple[int, int, int],
    times: tuple[float, float, float],
) -> ProgressSnapshot:
    total, completed, base = counts
    updated_at, started_at, phase_started_at = times
    return ProgressSnapshot(
        state=state,
        total=total,
        completed=completed,
        failed=0,
        bars=0,
        current="",
        detail="",
        updated_at=updated_at,
        started_at=started_at,
        phase_started_at=phase_started_at,
        phase_base_completed=base,
        open_ended=open_ended,
    )


def _float_cell(value: float) -> dict[str, Any]:
    """A float as a JSON-safe cell: the bit pattern always, repr when finite."""
    cell: dict[str, Any] = {"bits": f64_bits(value)}
    if math.isfinite(value):
        cell["value"] = value
    return cell


def _row(
    state: str,
    open_ended: bool,
    counts: tuple[int, int, int],
    times: tuple[float, float, float],
) -> dict[str, Any]:
    snap = _snapshot(state, open_ended, counts, times)
    eta = snap.eta_seconds
    return {
        "case": {
            "state": state,
            "open_ended": open_ended,
            "total": counts[0],
            "completed": counts[1],
            "phase_base_completed": counts[2],
            "updated_at": _float_cell(times[0]),
            "started_at": _float_cell(times[1]),
            "phase_started_at": _float_cell(times[2]),
        },
        "out": {
            "percent": _float_cell(snap.percent),
            "elapsed_seconds": _float_cell(snap.elapsed_seconds),
            "units_per_sec": _float_cell(snap.units_per_sec),
            "eta_present": eta is not None,
            "eta_seconds": None if eta is None else _float_cell(eta),
            "running": snap.running,
        },
    }


def compute_rows() -> list[dict[str, Any]]:
    """The full frozen lattice, driven through the LIVE properties."""
    rows: list[dict[str, Any]] = []
    # Every count x time combo under the eta-live shapes (running x open_ended).
    for counts in COUNT_TRIPLES:
        for times in TIME_TRIPLES:
            for open_ended in (False, True):
                rows.append(_row(STATE_RUNNING, open_ended, counts, times))
    # The state projection: every state over one representative combo.
    for state in _ALL_STATES:
        for open_ended in (False, True):
            rows.append(_row(state, open_ended, (16, 3, 0), (1056.0, 1024.0, 1024.0)))
    return rows


def compute_payload() -> dict[str, Any]:
    """The canonical JSON payload."""
    return {
        "_doc": (
            "Frozen pull-progress derived-metrics verdicts "
            "(DEC-RUST-PULL-STATE-001 Wave 2F; generator "
            "generate_pull_progress_golden.py; pre-conversion oracle — "
            "never regenerate on a post-conversion tree). Floats carry "
            "IEEE-754 <f8 bit patterns; non-finite floats carry bits only; "
            "NaN OUTPUTS are compared NaN-to-NaN, never bit-to-bit."
        ),
        "count_domain_max": COUNT_DOMAIN_MAX,
        "rows": compute_rows(),
    }


def _tsv_float(cell: dict[str, Any]) -> str:
    bits = cell["bits"]
    assert isinstance(bits, str)
    return bits


def compute_tsv(payload: dict[str, Any]) -> str:
    """The serde-free line projection of the SAME rows for the cargo test.

    Columns (tab-separated): ``state_running`` ``open_ended`` ``total``
    ``completed`` ``phase_base_completed`` ``updated_bits`` ``started_bits``
    ``phase_started_bits`` ``percent_bits`` ``elapsed_bits`` ``ups_bits``
    ``eta_present`` ``eta_bits`` (``-`` when eta is None). Bools are 0/1.
    """
    lines = [
        "# pull_progress derived-metrics golden (DEC-RUST-PULL-STATE-001 Wave 2F)",
        "# state_running\topen_ended\ttotal\tcompleted\tbase\tupdated\tstarted"
        "\tphase_started\tpercent\telapsed\tups\teta_present\teta",
    ]
    for row in payload["rows"]:
        case = row["case"]
        out = row["out"]
        eta_bits = "-" if out["eta_seconds"] is None else _tsv_float(out["eta_seconds"])
        lines.append(
            "\t".join(
                (
                    "1" if case["state"] == STATE_RUNNING else "0",
                    "1" if case["open_ended"] else "0",
                    str(case["total"]),
                    str(case["completed"]),
                    str(case["phase_base_completed"]),
                    _tsv_float(case["updated_at"]),
                    _tsv_float(case["started_at"]),
                    _tsv_float(case["phase_started_at"]),
                    _tsv_float(out["percent"]),
                    _tsv_float(out["elapsed_seconds"]),
                    _tsv_float(out["units_per_sec"]),
                    "1" if out["eta_present"] else "0",
                    eta_bits,
                )
            )
        )
    return "\n".join(lines) + "\n"
