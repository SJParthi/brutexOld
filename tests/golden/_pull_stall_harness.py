"""Shared harness for the pull-stall wedge/alive goldens (DEC-RUST-PULL-STATE-001).

Wave 1C of the Rust pull-state lane freezes the DECISION plane of
``brutex/dashboard/pull_stall.py`` (DEC-PULL-STALL-DETECTION-001 +
DEC-PULL-WEDGE-ESCALATE-001 + DEC-PULL-TELEMETRY-TRIAD-001) as the CONTRACT
the Rust port must satisfy bit-for-bit. This module is imported by BOTH the
generator (``generate_pull_stall_golden.py``) and the consuming test
(``test_pull_stall_golden.py``) — the ``_market_hours_guard_harness`` shape
(never generator-imports-test, the CodeQL ``py/cyclic-import`` break).

Three verdict tables are computed by DRIVING the LIVE Python module (the
oracle) — never by re-deriving its logic in the harness:

* :func:`compute_wedge_table` — the DUAL-signal wedge-escalation decision
  lattice (``_escalation_wants_probe_locked`` + ``_maybe_escalate_locked``):
  guard-flag combos × last-beat-age boundaries (exact ``multiplier ×
  timeout``, ±1 dyadic ULP-ish steps, clock-skew negatives, NaN/inf) ×
  baseline/current token shapes (None / equal / divergent / empty-string) ×
  wedge-window-restart ages. The expected ARM is CLASSIFIED from the
  watchdog's OBSERVED mutations (baseline adopted / rebaselined, escalation
  fired) — the arm names mirror the Rust kernel's token vocabulary.
* :func:`compute_alive_table` — the throttled alive-beat due/not-due law of
  ``_alive_payload_locked`` (strict ``since < interval`` refusal; the
  first-check ``-inf`` seed ⇒ due; NaN falls through ⇒ due).
* :func:`compute_resolver_table` — the two FAIL-SAFE env resolvers
  (``resolve_stall_timeout_seconds`` / ``resolve_wedge_multiplier``): junk /
  blank / non-positive values silently fall back to the audited default (the
  DELIBERATE opposite of the lane's fail-loud env family — mirrors
  ``resolve_pool_size``; the quirk arms — ``"inf"`` accepted, ``"nan"``
  passed through — are pinned AS-IS, never "fixed").

Every float crossing a table is ALSO recorded as its IEEE-754 bit pattern
(``<f8`` little-endian hex via :func:`f64_bits`) so the fixture is bit-exact
across JSON round-trips AND trivially parseable by the serde-free cargo
fixture test (``rust/brutex-pull-state/tests/stall_golden.rs`` reads the TSV
projection). Absolute clock instants use DYADIC values (integers + powers of
two) so every ``now - stored`` subtraction is EXACT — the boundary rows
(``age == multiplier × timeout``) are genuinely on the boundary.
"""

# Whitebox drives of module-private state (sanctioned — the unit suite precedent).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import math
import struct
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from brutex.dashboard.pull_stall import (
    DEFAULT_STALL_TIMEOUT_SECONDS,
    DEFAULT_WEDGE_MULTIPLIER,
    PULL_STALL_ENV,
    PULL_WEDGE_ENV,
    PullWatchdog,
    resolve_stall_timeout_seconds,
    resolve_wedge_multiplier,
)
from brutex.observability.watchdog import WorkerEvent, WorkerState

#: Wedge-decision arm names, index == the Rust kernel's token value
#: (``rust/brutex-pull-state/src/stall.rs`` — the shared vocabulary).
WEDGE_ARM_NAMES: tuple[str, ...] = (
    "no_probe",  # 0 — the _escalation_wants_probe_locked guards refuse
    "abstain_probe_none",  # 1 — this poll's probe token is None
    "adopt_baseline",  # 2 — None HUNG-edge baseline replaced by the first real token
    "rebaseline",  # 3 — the token moved: re-baseline + restart the window
    "defer_window",  # 4 — restarted window not yet spanned
    "escalate",  # 5 — the DUAL signal holds: fire on_wedged (terminal latch)
)

#: The fixed evaluation instant (dyadic → every ``now - stored`` is exact).
T_EVAL: float = 8192.0


def f64_bits(value: float) -> str:
    """The IEEE-754 bit pattern of *value* as 16 lowercase hex chars (``<f8``)."""
    return struct.pack("<d", value).hex()


def f64_from_bits(bits: str) -> float:
    """Inverse of :func:`f64_bits` (used by the consuming tests)."""
    unpacked = struct.unpack("<d", bytes.fromhex(bits))[0]
    return float(unpacked)


def _float_json(value: float) -> Any:
    """JSON-safe float encoding: finite floats as-is, else the spelled string."""
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0.0 else "-inf"
    return value


@dataclass(frozen=True)
class WedgeCase:
    """One wedge-decision lattice point (ages are relative to :data:`T_EVAL`)."""

    label: str
    hung: bool
    already_fired: bool
    hook_bound: bool
    baseline_captured: bool
    last_beat_age: float
    timeout: float
    multiplier: float
    restart_age: float | None
    baseline: str | None
    current: str | None


def _drive_wedge_case(case: WedgeCase) -> int:
    """Drive the REAL ``PullWatchdog`` through *case* and classify the observed arm.

    The classification reads OBSERVATIONS (mutations + fired hooks) plus the
    two input None-shapes that name behaviourally-identical no-op arms
    (``abstain_probe_none`` vs ``defer_window``) — never a re-derivation of
    the module's guard logic.
    """

    class _Clock:
        def __init__(self) -> None:
            self.t = 0.0

        def __call__(self) -> float:
            return self.t

    clock = _Clock()
    hungs: list[WorkerEvent] = []
    wd = PullWatchdog(
        on_hung=hungs.append,
        timeout_seconds=case.timeout,
        spawn=lambda _fn: None,
        clock=clock,
        wedge_multiplier=case.multiplier,
    )
    wd.start()
    if case.hung:
        # Drive the REAL underlying watchdog into HUNG via genuine silence.
        clock.t = case.timeout + 1.0
        wd._wd.check_once()
        assert wd.state() is WorkerState.HUNG, case.label
        assert len(hungs) == 1, case.label
    clock.t = T_EVAL
    wedges: list[WorkerEvent] = []
    # Whitebox state install (the test suite's whitebox precedent): every
    # stored instant is an ABSOLUTE derived from the case's relative age so
    # the module's own ``clock() - stored`` recovers the age bit-exactly
    # (dyadic values) or propagates NaN/inf faithfully.
    wd.on_wedged = wedges.append if case.hook_bound else None
    wd._wedged_fired = case.already_fired
    wd._hung_baseline_captured = case.baseline_captured
    wd._hung_baseline_token = case.baseline
    wd._baseline_capture_pending = False
    wd._last_beat_at = T_EVAL - case.last_beat_age
    wd._wedge_window_restart_at = None if case.restart_age is None else T_EVAL - case.restart_age
    wants = wd._escalation_wants_probe_locked()
    baseline_before = wd._hung_baseline_token
    wd._maybe_escalate_locked(case.current)
    fired_now = wd._wedged_fired and not case.already_fired
    baseline_after = wd._hung_baseline_token
    if fired_now:
        assert len(wedges) == 1, case.label
        return WEDGE_ARM_NAMES.index("escalate")
    assert not wedges, case.label
    if baseline_after != baseline_before:
        assert wd._wedge_window_restart_at == T_EVAL, case.label
        if baseline_before is None:
            return WEDGE_ARM_NAMES.index("adopt_baseline")
        return WEDGE_ARM_NAMES.index("rebaseline")
    if not wants:
        return WEDGE_ARM_NAMES.index("no_probe")
    if case.current is None:
        return WEDGE_ARM_NAMES.index("abstain_probe_none")
    return WEDGE_ARM_NAMES.index("defer_window")


def _wedge_cases() -> list[WedgeCase]:
    """The frozen lattice (labels stable — they key the fixture rows)."""
    t, m = 600.0, 6.0  # the audited defaults → window 3600.0
    w = m * t
    eps = 2.0**-10  # dyadic — exact under the harness arithmetic
    nan, inf = float("nan"), float("inf")
    cases: list[WedgeCase] = []
    # 1. The full guard-flag lattice at an over-window age (only T/F/T/T fires).
    for hung in (False, True):
        for fired in (False, True):
            for hook in (False, True):
                for captured in (False, True):
                    cases.append(
                        WedgeCase(
                            label=(f"guards-h{int(hung)}f{int(fired)}k{int(hook)}c{int(captured)}"),
                            hung=hung,
                            already_fired=fired,
                            hook_bound=hook,
                            baseline_captured=captured,
                            last_beat_age=w + 1.0,
                            timeout=t,
                            multiplier=m,
                            restart_age=None,
                            baseline="revA",
                            current="revA",
                        )
                    )
    # 2. The last-beat-age boundary lattice (strict `<` at the exact window).
    for label, age in (
        ("age-zero", 0.0),
        ("age-half", w / 2.0),
        ("age-window-minus-eps", w - eps),
        ("age-window-exact", w),
        ("age-window-plus-eps", w + eps),
        ("age-double", 2.0 * w),
        ("age-negative-skew", -10.0),
        ("age-nan", nan),
        ("age-inf", inf),
    ):
        cases.append(
            WedgeCase(
                label=label,
                hung=True,
                already_fired=False,
                hook_bound=True,
                baseline_captured=True,
                last_beat_age=age,
                timeout=t,
                multiplier=m,
                restart_age=None,
                baseline="revA",
                current="revA",
            )
        )
    # 3. The baseline/current token lattice (None / equal / divergent / "").
    for label, baseline, current in (
        ("tok-none-none", None, None),
        ("tok-none-real", None, "revB"),
        ("tok-none-empty", None, ""),
        ("tok-real-none", "revA", None),
        ("tok-real-divergent", "revA", "revB"),
        ("tok-real-empty", "revA", ""),
        ("tok-empty-real", "", "revA"),
        ("tok-empty-empty", "", ""),
        ("tok-real-equal", "revA", "revA"),
    ):
        cases.append(
            WedgeCase(
                label=label,
                hung=True,
                already_fired=False,
                hook_bound=True,
                baseline_captured=True,
                last_beat_age=w + 1.0,
                timeout=t,
                multiplier=m,
                restart_age=None,
                baseline=baseline,
                current=current,
            )
        )
    # 4. The wedge-window-restart lattice (strict `<` at the exact window).
    for label, restart_age in (
        ("restart-zero", 0.0),
        ("restart-window-minus-eps", w - eps),
        ("restart-window-exact", w),
        ("restart-window-plus", w + 1.0),
        ("restart-negative-skew", -4.0),
        ("restart-nan", nan),
        ("restart-inf", inf),
    ):
        cases.append(
            WedgeCase(
                label=label,
                hung=True,
                already_fired=False,
                hook_bound=True,
                baseline_captured=True,
                last_beat_age=w + 1.0,
                timeout=t,
                multiplier=m,
                restart_age=restart_age,
                baseline="revA",
                current="revA",
            )
        )
    # 5. Precedence interactions: a restarted window never outranks the
    #    abstain/adopt/rebaseline arms.
    cases.append(
        WedgeCase(
            label="restart-vs-rebaseline",
            hung=True,
            already_fired=False,
            hook_bound=True,
            baseline_captured=True,
            last_beat_age=w + 1.0,
            timeout=t,
            multiplier=m,
            restart_age=1.0,
            baseline="revA",
            current="revB",
        )
    )
    cases.append(
        WedgeCase(
            label="restart-vs-adopt",
            hung=True,
            already_fired=False,
            hook_bound=True,
            baseline_captured=True,
            last_beat_age=w + 1.0,
            timeout=t,
            multiplier=m,
            restart_age=1.0,
            baseline=None,
            current="revB",
        )
    )
    cases.append(
        WedgeCase(
            label="restart-vs-abstain",
            hung=True,
            already_fired=False,
            hook_bound=True,
            baseline_captured=True,
            last_beat_age=w + 1.0,
            timeout=t,
            multiplier=m,
            restart_age=1.0,
            baseline="revA",
            current=None,
        )
    )
    # 6. Non-default window shapes (fractional multiplier / dyadic timeout).
    for label, timeout, mult, age in (
        ("window-tiny-exact", 0.5, 0.25, 0.125),
        ("window-tiny-below", 0.5, 0.25, 0.125 - 2.0**-20),
        ("window-frac-exact", 100.0, 1.5, 150.0),
        ("window-frac-below", 100.0, 1.5, 150.0 - eps),
    ):
        cases.append(
            WedgeCase(
                label=label,
                hung=True,
                already_fired=False,
                hook_bound=True,
                baseline_captured=True,
                last_beat_age=age,
                timeout=timeout,
                multiplier=mult,
                restart_age=None,
                baseline="revA",
                current="revA",
            )
        )
    return cases


def compute_wedge_table() -> list[dict[str, Any]]:
    """The wedge-decision golden rows (inputs + kernel-shaped ages + the arm)."""
    rows: list[dict[str, Any]] = []
    for case in _wedge_cases():
        token = _drive_wedge_case(case)
        window = case.multiplier * case.timeout
        rows.append(
            {
                "label": case.label,
                "hung": case.hung,
                "already_fired": case.already_fired,
                "hook_bound": case.hook_bound,
                "baseline_captured": case.baseline_captured,
                "last_beat_age": _float_json(case.last_beat_age),
                "last_beat_age_bits": f64_bits(case.last_beat_age),
                "timeout": case.timeout,
                "multiplier": case.multiplier,
                "wedge_window_bits": f64_bits(window),
                "restart_age": (
                    None if case.restart_age is None else _float_json(case.restart_age)
                ),
                "restart_age_bits": (
                    None if case.restart_age is None else f64_bits(case.restart_age)
                ),
                "baseline": case.baseline,
                "current": case.current,
                "expected_token": token,
                "expected_arm": WEDGE_ARM_NAMES[token],
            }
        )
    return rows


def _drive_alive_case(since_last: float, interval: float) -> bool:
    """Drive the REAL throttle: True iff a line would be emitted (payload not None)."""

    class _Clock:
        def __init__(self) -> None:
            self.t = 0.0

        def __call__(self) -> float:
            return self.t

    clock = _Clock()
    wd = PullWatchdog(
        on_hung=lambda _e: None,
        timeout_seconds=600.0,
        spawn=lambda _fn: None,
        clock=clock,
    )
    wd.start()
    clock.t = T_EVAL
    wd._alive_interval = interval
    wd._last_alive_log = T_EVAL - since_last
    payload = wd._alive_payload_locked()
    return payload is not None


def compute_alive_table() -> list[dict[str, Any]]:
    """The alive-log throttle golden rows (due-ness only — payload stays Python)."""
    nan, inf = float("nan"), float("inf")
    cases: list[tuple[str, float, float]] = [
        ("first-check-seed", inf, 200.0),  # the -inf seed ⇒ since == +inf
        ("just-logged", 0.0, 200.0),
        ("mid-interval", 100.0, 200.0),
        ("interval-minus-eps", 200.0 - 2.0**-10, 200.0),
        ("interval-exact", 200.0, 200.0),
        ("interval-plus-eps", 200.0 + 2.0**-10, 200.0),
        ("long-idle", 3600.0, 200.0),
        ("negative-skew", -5.0, 200.0),
        ("nan-since", nan, 200.0),
        ("frac-interval-exact", 333.25, 333.25),
        ("frac-interval-below", 333.0, 333.25),
    ]
    rows: list[dict[str, Any]] = []
    for label, since, interval in cases:
        due = _drive_alive_case(since, interval)
        rows.append(
            {
                "label": label,
                "since_last": _float_json(since),
                "since_last_bits": f64_bits(since),
                "interval": _float_json(interval),
                "interval_bits": f64_bits(interval),
                "expected_due": due,
            }
        )
    return rows


@contextmanager
def _env(monkey_env: dict[str, str | None]) -> Generator[None]:
    """Apply/restore env vars (harness-local — the generator has no monkeypatch)."""
    import os

    saved: dict[str, str | None] = {}
    try:
        for key, value in monkey_env.items():
            saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


#: The frozen resolver raw-value sweep (fail-SAFE law + its pinned quirks).
_RESOLVER_RAWS: tuple[str | None, ...] = (
    None,  # unset → default
    "",  # blank → default
    "   ",  # whitespace → default
    "junk",  # non-numeric → default (SILENT — the fail-safe posture)
    "12abc",
    "-1",  # non-positive → default
    "-0.0",  # -0.0 <= 0.0 → default
    "0",
    "0.0",
    "750",
    " 750 ",  # float() strips whitespace
    "1e3",  # scientific accepted
    "1_000",  # Python float() underscore grouping accepted
    "6.5",
    "\t2.5\n",
    "inf",  # inf > 0 → ACCEPTED as-is (pinned quirk, never "fixed")
    "-inf",  # <= 0 → default
    "nan",  # NaN <= 0.0 is False → NaN passes through (pinned quirk)
)


def compute_resolver_table() -> dict[str, list[dict[str, Any]]]:
    """The two fail-SAFE env resolvers over the frozen raw-value sweep."""
    tables: dict[str, list[dict[str, Any]]] = {}
    for kind, env_name, resolver, default in (
        (
            "stall_timeout",
            PULL_STALL_ENV,
            resolve_stall_timeout_seconds,
            DEFAULT_STALL_TIMEOUT_SECONDS,
        ),
        ("wedge_multiplier", PULL_WEDGE_ENV, resolve_wedge_multiplier, DEFAULT_WEDGE_MULTIPLIER),
    ):
        rows: list[dict[str, Any]] = []
        for raw in _RESOLVER_RAWS:
            with _env({env_name: raw}):
                value = resolver()
            rows.append(
                {
                    "raw": raw,
                    "resolved": _float_json(value),
                    "resolved_bits": f64_bits(value),
                    "is_default": (value == default and not math.isnan(value)),
                }
            )
        tables[kind] = rows
    return tables


def wedge_tsv_row(row: dict[str, Any]) -> str:
    """The serde-free TSV projection of one wedge row (cargo consumes this)."""
    restart = row["restart_age_bits"] if row["restart_age_bits"] is not None else "-"
    baseline = row["baseline"] if row["baseline"] is not None else "\\N"
    current = row["current"] if row["current"] is not None else "\\N"
    return "\t".join(
        [
            "wedge",
            row["label"],
            str(int(row["hung"])),
            str(int(row["already_fired"])),
            str(int(row["hook_bound"])),
            str(int(row["baseline_captured"])),
            row["last_beat_age_bits"],
            row["wedge_window_bits"],
            restart,
            baseline,
            current,
            str(row["expected_token"]),
        ]
    )


def alive_tsv_row(row: dict[str, Any]) -> str:
    """The serde-free TSV projection of one alive row (cargo consumes this)."""
    return "\t".join(
        [
            "alive",
            row["label"],
            row["since_last_bits"],
            row["interval_bits"],
            str(int(row["expected_due"])),
        ]
    )


def compute_tsv(payload: dict[str, Any]) -> str:
    """The whole TSV projection of the JSON *payload* (one truth, two encodings).

    The consuming Python golden test pins the committed ``.tsv`` byte-equal to
    this projection of the committed ``.json`` — so the two fixtures can never
    drift apart.
    """
    lines = [wedge_tsv_row(r) for r in payload["wedge"]]
    lines.extend(alive_tsv_row(r) for r in payload["alive"])
    return "\n".join(lines) + "\n"


def compute_payload() -> dict[str, Any]:
    """The full golden payload (wedge + alive + resolvers)."""
    return {
        "wedge": compute_wedge_table(),
        "alive": compute_alive_table(),
        "resolvers": compute_resolver_table(),
    }
