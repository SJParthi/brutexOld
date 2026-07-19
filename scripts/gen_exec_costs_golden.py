#!/usr/bin/env python3
"""Generate the cross-language golden answer-key for the exec integer cores.

DEC-EXEC-RUST-CORE-001 (MIGRATION.md #1960), the ``vocab_golden_vectors``
discipline: freeze the *current* Python charge-stack / fill / money /
materialize outputs over a diverse, deterministic case set into ONE
language-neutral JSON fixture (``tests/golden/fixtures/
exec_costs_golden_vectors.json``). The fixture is the **oracle**:

* ``tests/golden/test_exec_rust_golden.py`` asserts the Python bodies AND
  the flag-ON Rust seam each reproduce every frozen value — locking both
  arms against drift;
* ``rust/fno-math/tests/exec_golden.rs`` asserts the native crate
  reproduces the same values bit-for-bit with no Python in the loop.

Schema v2 (the DEC-FILL-WORST-CASE-ONLY-001 reconcile — MIGRATION.md #1825
landed on main while the exec slice was in flight; main's law WINS
structurally, so the fixture is re-expressed on it): fills anchor on the
fill bars' FOUR adverse extremes (entry high/low + exit high/low — the
retired OPEN anchors are gone), levy rounding is the permanent ADVERSE
CEILING (`_round_levy_paisa` / `_round_levy_rupee`), and the stale gate is
UNCONDITIONAL (the `reject_stale` opt-in column is deleted).

Sections + laws covered:

* ``money`` — the CEIL boundary vectors: exact-multiple fixpoints BOTH
  signs at BOTH scales, one-past boundaries, zero, and the int64 extremes
  on the representable side (i64::MAX itself CEILs out of range — the
  kernel refuses; the largest in-envelope multiple is pinned instead).
  Values serialised as STRINGS — a JSON double cannot carry > 2^53 exactly
  (the vocab mask-hex precedent).
* ``fills`` — a bar-pair grid over the boundary anchors (tick-floor
  degeneracies, flat bars, wide bars, big paisa) × both sides × both fill
  laws (options ``_apply_slippage`` / futures ``apply_futures_fill``).
* ``option_stacks`` / ``futures_stacks`` — both charge stacks across every
  REGIME WINDOW BOUNDARY date on the VALID side (rates resolved from the
  REAL Python accessors and recorded as arguments — dates never cross the
  FFI; refusal windows themselves stay Python raises, out of scope here),
  plus the Groww min-law both arms, the Zerodha flat arm, the cost-free
  arm, and seeded random wide-domain cases.
* ``options_materialize`` / ``futures_materialize`` — verdict edges
  (unconditional stale, tick floor `<=`/`<` OPEN boundaries, the
  stale-precedes-tick order pin) + MAE/MFE paths off the worst-case fills.

Regeneration is REFUSED unless ``BRUTEX_REGEN_EXEC_GOLDEN=1`` (a frozen
oracle is a deliberate, ledger-worthy artifact — the preopen-golden
precedent). Run: ``python scripts/gen_exec_costs_golden.py``.
"""

from __future__ import annotations

import json
import os
import random
import sys
from datetime import date
from pathlib import Path
from typing import Any

from brutex.costs import constants as const
from brutex.costs.calculator import (
    _apply_slippage,  # the generator IS the oracle capture
    _charge_stack_py,
    _round_levy_paisa,
    _round_levy_rupee,
)
from brutex.costs.futures_costs import (
    GROWW_FUTURES_BROKERAGE_TURNOVER_BPS_X100,
    _futures_charge_stack_py,
    apply_futures_fill,
    stt_futures_rate_bps_x100,
)
from brutex.costs.types import UnverifiedCostRegimeError

REGEN_ENV = "BRUTEX_REGEN_EXEC_GOLDEN"
_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "golden" / "fixtures"
_FIXTURE_PATH = _FIXTURE / "exec_costs_golden_vectors.json"

_SEED = 20260711
_I64_MAX = 2**63 - 1
_I64_MIN = -(2**63)
#: The largest multiple of 100 that fits i64 — the in-envelope CEIL ceiling
#: (i64::MAX itself would CEIL to +100 past it; the kernel refuses that).
_I64_MAX_RUPEE_MULTIPLE = (_I64_MAX // 100) * 100

#: Rate-set basis dates — every VERIFIED regime boundary on its VALID side
#: (refusal-adjacent where the adjacent window refuses). The dates are
#: generator METADATA only (rates cross as integers).
_OPTION_RATE_DATES = (
    date(2024, 9, 30),  # pre-Oct-2024 STT (verified 6_250; ETC refusal-adjacent)
    date(2024, 10, 1),  # the ETC/STT boundary day itself
    date(2026, 3, 31),  # last day of the FY-2025 STT regime
    date(2026, 4, 1),  # Finance Act 2026 STT step-up
)
_FUTURES_STT_DATES = (
    date(2024, 10, 1),  # futures STT 0.02% boundary (pre-window refuses)
    date(2026, 3, 31),
    date(2026, 4, 1),  # futures STT 0.05% step-up
)

#: Synthetic circular-cited rates for the arms whose PRODUCTION tables are
#: all-refusal (futures ETC / IPFT / stamp) — the sanctioned test-fixture
#: modelling of DEC-ETC-REGIME-HISTORY-001 §9 (values mirror the
#: test_futures_costs.py synthetic table; never shipped as production data).
_SYNTH_FUT_ETC = 173
_SYNTH_FUT_IPFT = 10
_SYNTH_FUT_STAMP = 200

#: Fill-bar grid — (high, low) pairs: tick-floor degeneracies, flat bars,
#: narrow/wide ranges, and big-paisa index-scale bars.
_BAR_GRID: tuple[tuple[int, int], ...] = (
    (5, 5),  # flat at the tick floor
    (6, 5),
    (7, 6),
    (10, 7),
    (25, 10),
    (26, 25),
    (9_995, 9_990),
    (12_005, 11_900),
    (2_200_000, 2_190_000),
)


def _money_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    paisa_inputs = [
        0,
        1,
        4_999_999,
        5_000_000,  # the old half-up tie — CEILs to 1 like every fraction
        5_000_001,
        9_999_999,
        10_000_000,  # exact multiple — fixpoint
        10_000_001,  # one past → CEILs up
        -1,  # negative fraction → CEIL toward +∞ = 0
        -4_999_999,
        -5_000_000,
        -5_000_001,
        -9_999_999,
        -10_000_000,  # exact negative multiple — fixpoint
        -10_000_001,
        15_000_000,
        -15_000_000,
        123_456_789_012_345,
        -123_456_789_012_345,
        _I64_MAX,
        _I64_MIN,
        _I64_MAX * 10_000_000,  # scaled i128 edge → exactly i64::MAX
        _I64_MIN * 10_000_000,  # scaled i128 edge → exactly i64::MIN
    ]
    for i, x in enumerate(paisa_inputs):
        cases.append(
            {
                "id": f"rtp-{i:03d}",
                "op": "round_to_paisa",
                "x": str(x),
                "expected": str(_round_levy_paisa(x)),
            }
        )
    rupee_inputs = [
        0,
        1,  # any fraction CEILs up to 100
        49,
        50,
        51,
        99,
        100,  # exact multiple — fixpoint
        101,
        149,
        150,
        199,
        200,
        -1,  # negative fraction → CEIL toward +∞ = 0
        -49,
        -50,
        -51,
        -99,
        -100,  # exact negative multiple — fixpoint
        -101,
        -150,
        12_345,
        -12_345,
        _I64_MAX_RUPEE_MULTIPLE,  # in-envelope ceiling (exact multiple)
        _I64_MAX_RUPEE_MULTIPLE - 99,  # CEILs up to the ceiling exactly
        _I64_MIN,  # CEILs toward +∞ — in range
    ]
    for i, p in enumerate(rupee_inputs):
        cases.append(
            {
                "id": f"rtr-{i:03d}",
                "op": "round_to_rupee",
                "x": str(p),
                "expected": str(_round_levy_rupee(p)),
            }
        )
    return cases


def _fill_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    i = 0
    for entry_high, entry_low in _BAR_GRID:
        for exit_high, exit_low in _BAR_GRID:
            for is_long in (True, False):
                side = "long" if is_long else "short"
                bar_args = {
                    "entry_high_paisa": entry_high,
                    "entry_low_paisa": entry_low,
                    "exit_high_paisa": exit_high,
                    "exit_low_paisa": exit_low,
                }
                cases.append(
                    {
                        "id": f"fill-opt-{i:03d}",
                        "op": "slippage",
                        "entry_high": entry_high,
                        "entry_low": entry_low,
                        "exit_high": exit_high,
                        "exit_low": exit_low,
                        "is_long": is_long,
                        "expected": list(_apply_slippage(side, **bar_args)),
                    }
                )
                cases.append(
                    {
                        "id": f"fill-fut-{i:03d}",
                        "op": "futures_fill",
                        "entry_high": entry_high,
                        "entry_low": entry_low,
                        "exit_high": exit_high,
                        "exit_low": exit_low,
                        "is_long": is_long,
                        "expected": list(apply_futures_fill(side=side, **bar_args)),
                    }
                )
                i += 1
    return cases


def _bar(
    anchor: int, rng: random.Random | None = None, spread: int | None = None
) -> tuple[int, int]:
    """A (high, low) bar around *anchor*: deterministic ``spread`` when given,
    else a seeded random spread; low floored at 1."""
    if spread is None:
        assert rng is not None
        spread = rng.randint(0, max(1, anchor // 20))
    high = anchor + spread
    low = max(1, anchor - spread)
    return high, low


def _option_rate_sets() -> list[dict[str, Any]]:
    sets: list[dict[str, Any]] = []
    for d in _OPTION_RATE_DATES:
        stt = const.stt_options_rate_bps_x100(d)
        try:
            etc_nse = const.exchange_charge_bps_x100("NIFTY", d)
            etc_bse = const.exchange_charge_bps_x100("SENSEX", d)
            etc_note = "verified"
        except UnverifiedCostRegimeError:  # the pre-boundary ETC refusal window
            etc_nse, etc_bse = 3_503, 3_250  # the post-boundary VERIFIED values
            etc_note = "post-boundary rates carried (ETC refuses on this date)"
        for instrument, etc, ipft in (
            ("NSE", etc_nse, const.ipft_bps_x100("NIFTY")),
            ("BSE", etc_bse, const.ipft_bps_x100("SENSEX")),
        ):
            sets.append(
                {
                    "rate_basis_date": d.isoformat(),
                    "venue": instrument,
                    "note": etc_note,
                    "stt_rate": stt,
                    "etc_rate": etc,
                    "sebi_rate": const.SEBI_FEE_BPS_X100,
                    "ipft_rate": ipft,
                    "stamp_rate": const.STAMP_DUTY_OPTIONS_BPS_X100,
                    "gst_rate": const.GST_TOTAL_BPS_X100,
                }
            )
    return sets


def _option_stack_cases(rng: random.Random) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    i = 0

    def add(
        entry_bar: tuple[int, int],
        exit_bar: tuple[int, int],
        *,
        is_long: bool,
        lot: int,
        n_lots: int,
        rates: dict[str, Any],
    ) -> None:
        nonlocal i
        args = {
            "is_long": is_long,
            "lot_size": lot,
            "n_lots": n_lots,
            "brokerage_round_trip_paisa": const.brokerage_paisa("groww") * 2,
            "stt_rate": rates["stt_rate"],
            "etc_rate": rates["etc_rate"],
            "sebi_rate": rates["sebi_rate"],
            "ipft_rate": rates["ipft_rate"],
            "stamp_rate": rates["stamp_rate"],
            "gst_rate": rates["gst_rate"],
        }
        stack = _charge_stack_py(entry_bar[0], entry_bar[1], exit_bar[0], exit_bar[1], **args)
        cases.append(
            {
                "id": f"ostack-{i:03d}",
                "entry_high": entry_bar[0],
                "entry_low": entry_bar[1],
                "exit_high": exit_bar[0],
                "exit_low": exit_bar[1],
                **args,
                "rate_basis": {k: rates[k] for k in ("rate_basis_date", "venue", "note")},
                "expected": list(stack),
            }
        )
        i += 1

    rate_sets = _option_rate_sets()
    # Every boundary rate set × the canonical §12.1-ish trade, both sides.
    for rates in rate_sets:
        for is_long in (True, False):
            add((10_000, 9_990), (12_010, 12_000), is_long=is_long, lot=65, n_lots=1, rates=rates)
    base = rate_sets[2]  # 2024-10-01 NSE — the fully-verified set
    # Boundary bars through the full stack (flat + narrow + crossing bars).
    for entry_bar, exit_bar in (
        ((5, 5), (5, 5)),
        ((7, 6), (6, 5)),
        ((25, 10), (26, 25)),
        ((26, 25), (25, 10)),
        ((12_050, 11_950), (15_100, 14_900)),
    ):
        add(entry_bar, exit_bar, is_long=True, lot=75, n_lots=2, rates=base)
    # Seeded random wide-domain cases over every rate set.
    for _ in range(60):
        rates = rng.choice(rate_sets)
        add(
            _bar(rng.randint(26, 5_000_000), rng),
            _bar(rng.randint(5, 5_000_000), rng),
            is_long=rng.random() < 0.5,
            lot=rng.choice([15, 20, 30, 35, 65, 75, 500, 1_000]),
            n_lots=rng.randint(1, 100),
            rates=rates,
        )
    return cases


def _futures_stack_cases(rng: random.Random) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    i = 0

    def add(
        entry_bar: tuple[int, int],
        exit_bar: tuple[int, int],
        *,
        is_long: bool,
        lot: int,
        n_lots: int,
        cost_free: bool,
        flat: int,
        turnover: int | None,
        stt: int,
        basis: str,
    ) -> None:
        nonlocal i
        args: dict[str, Any] = {
            "is_long": is_long,
            "lot_size": lot,
            "n_lots": n_lots,
            "cost_free": cost_free,
            "brokerage_flat_paisa": flat,
            "brokerage_turnover_bps_x100": turnover,
            "stt_rate": 0 if cost_free else stt,
            "etc_rate": 0 if cost_free else _SYNTH_FUT_ETC,
            "sebi_rate": 0 if cost_free else const.SEBI_FEE_BPS_X100,
            "ipft_rate": 0 if cost_free else _SYNTH_FUT_IPFT,
            "stamp_rate": 0 if cost_free else _SYNTH_FUT_STAMP,
            "gst_rate": 0 if cost_free else const.GST_TOTAL_BPS_X100,
        }
        stack = _futures_charge_stack_py(
            entry_bar[0], entry_bar[1], exit_bar[0], exit_bar[1], **args
        )
        cases.append(
            {
                "id": f"fstack-{i:03d}",
                "entry_high": entry_bar[0],
                "entry_low": entry_bar[1],
                "exit_high": exit_bar[0],
                "exit_low": exit_bar[1],
                **args,
                "rate_basis": basis,
                "expected": list(stack),
            }
        )
        i += 1

    groww_flat = const.brokerage_paisa("groww")
    turnover = GROWW_FUTURES_BROKERAGE_TURNOVER_BPS_X100
    # The cost-free index arm (net == gross), both sides + the boundary bars.
    for entry_bar, exit_bar in (
        ((2_201_000, 2_199_000), (2_211_000, 2_209_000)),
        ((5, 5), (5, 5)),
        ((26, 25), (25, 10)),
    ):
        for is_long in (True, False):
            add(
                entry_bar,
                exit_bar,
                is_long=is_long,
                lot=75,
                n_lots=1,
                cost_free=True,
                flat=groww_flat,
                turnover=turnover,
                stt=0,
                basis="cost-free index (rates untouched)",
            )
    # Cost-bearing at every futures STT boundary date (valid side), both
    # brokers, both sides; Groww min-law BOTH arms (small notional → the
    # turnover leg wins; large notional → the ₹20 flat cap wins).
    for d in _FUTURES_STT_DATES:
        stt = stt_futures_rate_bps_x100(d)
        for is_long in (True, False):
            # small notional: the turnover leg wins vs the 2 000 flat
            add(
                (205, 195),
                (225, 215),
                is_long=is_long,
                lot=500,
                n_lots=1,
                cost_free=False,
                flat=groww_flat,
                turnover=turnover,
                stt=stt,
                basis=f"groww turnover-arm @ {d.isoformat()} (synthetic ETC/IPFT/stamp)",
            )
            # large notional: the flat ₹20 cap wins
            add(
                (2_201_000, 2_199_000),
                (2_211_000, 2_209_000),
                is_long=is_long,
                lot=500,
                n_lots=1,
                cost_free=False,
                flat=groww_flat,
                turnover=turnover,
                stt=stt,
                basis=f"groww flat-arm @ {d.isoformat()} (synthetic ETC/IPFT/stamp)",
            )
            # zerodha: flat only (turnover None)
            add(
                (2_201_000, 2_199_000),
                (2_211_000, 2_209_000),
                is_long=is_long,
                lot=500,
                n_lots=1,
                cost_free=False,
                flat=const.brokerage_paisa("zerodha"),
                turnover=None,
                stt=stt,
                basis=f"zerodha flat-only @ {d.isoformat()} (synthetic ETC/IPFT/stamp)",
            )
    # Seeded random wide-domain cases.
    stt_2024 = stt_futures_rate_bps_x100(date(2024, 10, 1))
    for _ in range(40):
        cost_free = rng.random() < 0.3
        zerodha = rng.random() < 0.3
        add(
            _bar(rng.randint(5, 5_000_000), rng),
            _bar(rng.randint(5, 5_000_000), rng),
            is_long=rng.random() < 0.5,
            lot=rng.choice([15, 30, 75, 500, 1_000]),
            n_lots=rng.randint(1, 50),
            cost_free=cost_free,
            flat=const.brokerage_paisa("zerodha" if zerodha else "groww"),
            turnover=None if zerodha else turnover,
            stt=stt_2024,
            basis="seeded random",
        )
    return cases


def _options_materialize_cases() -> list[dict[str, Any]]:
    """Verdict edges through the SAME Python laws the executor tail runs —
    the expected values derive from the pinned gate order (stale → tick →
    stack), never re-minted. The tick floor gates on the OPENS; the fills
    anchor on the extremes."""
    base = _option_rate_sets()[2]
    args = {
        "is_long": True,
        "lot_size": 65,
        "n_lots": 1,
        "brokerage_round_trip_paisa": const.brokerage_paisa("groww") * 2,
        "stt_rate": base["stt_rate"],
        "etc_rate": base["etc_rate"],
        "sebi_rate": base["sebi_rate"],
        "ipft_rate": base["ipft_rate"],
        "stamp_rate": base["stamp_rate"],
        "gst_rate": base["gst_rate"],
    }
    grid: list[tuple[int, int, tuple[int, int], tuple[int, int], int, int]] = [
        # columns: entry_open · exit_open · entry_bar · exit_bar · ev · xv
        (12_000, 15_000, (12_050, 11_950), (15_100, 14_900), 0, 10),  # stale entry
        (12_000, 15_000, (12_050, 11_950), (15_100, 14_900), 10, 0),  # stale exit
        (12_000, 15_000, (12_050, 11_950), (15_100, 14_900), 0, 0),  # stale both
        (25, 15_000, (26, 24), (15_100, 14_900), 0, 10),  # stale precedes tick
        (25, 15_000, (26, 24), (15_100, 14_900), 10, 10),  # tick `<=` boundary (25 rejects)
        (26, 15_000, (27, 25), (15_100, 14_900), 10, 10),  # 26 prices (one past the floor)
        (26, 4, (27, 25), (5, 4), 10, 10),  # sub-tick exit `<` boundary (4 rejects)
        (26, 5, (27, 25), (6, 5), 10, 10),  # exit exactly one tick prices
        (9_995, 12_005, (10_000, 9_990), (12_010, 12_000), 10, 10),  # plain trade
    ]
    cases: list[dict[str, Any]] = []
    for i, (entry_open, exit_open, entry_bar, exit_bar, ev, xv) in enumerate(grid):
        if ev == 0 or xv == 0:
            kind, expected = "stale_bar", None
        elif entry_open <= 25 or exit_open < 5:
            kind, expected = "tick_floor", None
        else:
            kind = "trade"
            expected = list(
                _charge_stack_py(entry_bar[0], entry_bar[1], exit_bar[0], exit_bar[1], **args)
            )
        cases.append(
            {
                "id": f"omat-{i:03d}",
                "entry_open": entry_open,
                "exit_open": exit_open,
                "entry_high": entry_bar[0],
                "entry_low": entry_bar[1],
                "exit_high": exit_bar[0],
                "exit_low": exit_bar[1],
                "entry_volume": ev,
                "exit_volume": xv,
                **args,
                "expected_kind": kind,
                "expected": expected,
            }
        )
    return cases


def _futures_materialize_cases() -> list[dict[str, Any]]:
    args: dict[str, Any] = {
        "is_long": True,
        "lot_size": 75,
        "n_lots": 1,
        "cost_free": True,
        "brokerage_flat_paisa": const.brokerage_paisa("groww"),
        "brokerage_turnover_bps_x100": GROWW_FUTURES_BROKERAGE_TURNOVER_BPS_X100,
        "stt_rate": 0,
        "etc_rate": 0,
        "sebi_rate": 0,
        "ipft_rate": 0,
        "stamp_rate": 0,
        "gst_rate": 0,
    }
    entry_bar = (2_201_000, 2_199_000)
    exit_bar = (2_211_000, 2_209_000)
    loser_exit = (2_191_000, 2_189_000)
    grid: list[tuple[tuple[int, int], tuple[int, int], int, int, bool, list[int], list[int]]] = [
        # columns: entry_bar · exit_bar · ev · xv · is_long · highs · lows
        (entry_bar, exit_bar, 0, 10, True, [2_201_000], [2_199_000]),  # stale entry
        (entry_bar, exit_bar, 10, 0, True, [2_201_000], [2_199_000]),  # stale exit
        (entry_bar, exit_bar, 0, 0, True, [2_201_000], [2_199_000]),  # stale both
        (entry_bar, exit_bar, 10, 10, True, [], []),  # empty path → 0/0
        (
            entry_bar,
            exit_bar,
            10,
            10,
            True,
            [2_201_000, 2_208_000, 2_203_000],
            [2_199_000, 2_204_000, 2_195_000],
        ),
        (
            entry_bar,
            exit_bar,
            10,
            10,
            False,  # short — basis = sell_fill, favorable = down
            [2_201_000, 2_208_000, 2_203_000],
            [2_199_000, 2_204_000, 2_195_000],
        ),
        (entry_bar, loser_exit, 10, 10, True, [2_230_000], [2_170_000]),  # loser
    ]
    cases: list[dict[str, Any]] = []
    for i, (e_bar, x_bar, ev, xv, is_long, highs, lows) in enumerate(grid):
        case_args = {**args, "is_long": is_long}
        if ev == 0 or xv == 0:
            kind, expected = "stale_bar", None
        else:
            stack = _futures_charge_stack_py(e_bar[0], e_bar[1], x_bar[0], x_bar[1], **case_args)
            basis = stack.buy_fill_paisa if is_long else stack.sell_fill_paisa
            mae_u = 0
            mfe_u = 0
            for high, low in zip(highs, lows, strict=True):
                fav = (high - basis) if is_long else (basis - low)
                adv = (basis - low) if is_long else (high - basis)
                mfe_u = max(mfe_u, fav)
                mae_u = max(mae_u, adv)
            kind = "trade"
            expected = [*stack, mae_u * stack.n_shares, mfe_u * stack.n_shares]
        cases.append(
            {
                "id": f"fmat-{i:03d}",
                "entry_high": e_bar[0],
                "entry_low": e_bar[1],
                "exit_high": x_bar[0],
                "exit_low": x_bar[1],
                "entry_volume": ev,
                "exit_volume": xv,
                "path_highs": highs,
                "path_lows": lows,
                **case_args,
                "expected_kind": kind,
                "expected": expected,
            }
        )
    return cases


def build_payload() -> dict[str, Any]:
    rng = random.Random(_SEED)  # noqa: S311 — deterministic fixture seed, not crypto
    payload: dict[str, Any] = {
        "_comment": (
            "FROZEN cross-language oracle for the exec integer cores "
            "(DEC-EXEC-RUST-CORE-001, MIGRATION.md #1960). Generated from "
            "the Python bodies by scripts/gen_exec_costs_golden.py; "
            f"regeneration refused without {REGEN_ENV}=1. Schema v2 = the "
            "DEC-FILL-WORST-CASE-ONLY-001 (#1825) reconcile: bar-extreme "
            "worst-case fills, ADVERSE-CEILING levy rounding, unconditional "
            "stale gate. Money values are STRINGS (JSON doubles lose >2^53). "
            "Charge-stack wires are the pinned 17-slot ChargeStack order; "
            "futures materialize wires append [mae, mfe]."
        ),
        "schema_version": 2,
        "seed": _SEED,
        "money": _money_cases(),
        "fills": _fill_cases(),
        "option_stacks": _option_stack_cases(rng),
        "futures_stacks": _futures_stack_cases(rng),
        "options_materialize": _options_materialize_cases(),
        "futures_materialize": _futures_materialize_cases(),
    }

    # Every non-money value must survive a JSON double round-trip exactly.
    def _check(obj: Any) -> None:
        if isinstance(obj, dict):
            for v in obj.values():
                _check(v)
        elif isinstance(obj, list):
            for v in obj:
                _check(v)
        elif isinstance(obj, int) and not isinstance(obj, bool) and abs(obj) >= 2**53:
            msg = f"non-string fixture int {obj} exceeds the 2^53 JSON-double envelope"
            raise AssertionError(msg)

    for key in (
        "fills",
        "option_stacks",
        "futures_stacks",
        "options_materialize",
        "futures_materialize",
    ):
        _check(payload[key])
    return payload


def main() -> int:
    if _FIXTURE_PATH.exists() and os.environ.get(REGEN_ENV, "") != "1":
        sys.stderr.write(
            f"REFUSED: {_FIXTURE_PATH} exists and {REGEN_ENV}=1 is not set.\n"
            "The frozen oracle is the contract both languages satisfy — "
            "regenerating it is a deliberate, ledger-worthy act.\n"
        )
        return 1
    payload = build_payload()
    _FIXTURE_PATH.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    n = sum(len(payload[k]) for k in payload if isinstance(payload[k], list))
    sys.stderr.write(f"wrote {_FIXTURE_PATH} ({n} cases)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
