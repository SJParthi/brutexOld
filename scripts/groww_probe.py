"""Phase 0 Groww probe — empirically verify API before any ingest code runs.

Gated by ``BRUTEX_GROWW_SMOKE=1``. All findings are printed to stdout and
written to ``docs/groww/probe/probe_results_<date>.json``.

Checks performed:
    1. Auth — TOTP token generation + API access
    2. NIFTY 1m bars — fetch 1 trading day; count bars; spot-check timestamps
    3. INDIA VIX bars — confirm ``NSE-INDIAVIX`` returns data (Phase 0 hard-gate)
    4. BANKNIFTY + SENSEX — confirm all 4 universe instruments work
    5. Expired options — ``get_expiries`` + ``get_contracts`` for NIFTY 2021-01
    6. Rate limit — measure effective requests/second to backtesting endpoint
    7. Data depth — earliest available NIFTY bar (confirm goes back to 2020-01-01)

Usage::

    BRUTEX_GROWW_API_KEY=xxx BRUTEX_GROWW_TOTP_SECRET=yyy BRUTEX_GROWW_SMOKE=1 \\
        python scripts/groww_probe.py

Exit codes:
    0 — all gates passed
    1 — one or more P0 gates failed
    2 — auth failure (cannot proceed)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# Ensure brutex is importable from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

_SMOKE_FLAG = "BRUTEX_GROWW_SMOKE"

if os.environ.get(_SMOKE_FLAG, "") != "1":
    print(
        f"Probe is gated on {_SMOKE_FLAG}=1 (prevents accidental live API calls).\n"
        f"Set {_SMOKE_FLAG}=1 and re-run.",
        file=sys.stderr,
    )
    sys.exit(0)

# ── imports after guard so the module is importable without env vars ──────
from brutex.adapters.groww.auth import build_from_env  # noqa: E402
from brutex.adapters.groww.client import GrowwClient  # noqa: E402

# ── helpers ───────────────────────────────────────────────────────────────

RESULTS: dict[str, object] = {}
FAILURES: list[str] = []


def _ok(gate: str, detail: object) -> None:
    print(f"  ✅ {gate}: {detail}")
    RESULTS[gate] = {"status": "PASS", "detail": detail}


def _fail(gate: str, detail: object) -> None:
    print(f"  ❌ {gate}: {detail}")
    RESULTS[gate] = {"status": "FAIL", "detail": detail}
    FAILURES.append(gate)


def _probe_candles(
    client: GrowwClient,
    label: str,
    groww_symbol: str,
    exchange: str,
    segment: str,
    probe_date: date,
) -> list[object]:
    """Fetch 1 trading day of 1m bars for *groww_symbol*. Returns candle list."""
    start_time = f"{probe_date.isoformat()} 09:15:00"
    end_time = f"{probe_date.isoformat()} 15:30:00"
    payload = client.get_candles(
        exchange=exchange,
        segment=segment,
        groww_symbol=groww_symbol,
        start_time=start_time,
        end_time=end_time,
        candle_interval="1minute",
    )
    candles = payload.get("candles", [])
    n = len(candles)
    expected = 375  # 09:15 to 15:29 = 375 1m bars
    if n > 0:
        _ok(label, f"{n} bars (expected ~{expected}); first={candles[0][0]}")
    else:
        _fail(label, "no candles returned")
    return candles  # type: ignore[return-value]


def _probe_rate_limit(client: GrowwClient, groww_symbol: str) -> None:
    """Measure effective requests/sec over 20 rapid sequential calls."""
    probe_date = date(2024, 6, 3)  # a known trading day
    n_calls = 20
    start = time.perf_counter()
    ok = 0
    for day_offset in range(n_calls):
        d = probe_date + timedelta(days=day_offset)
        try:
            client.get_candles(
                exchange="NSE",
                segment="CASH",
                groww_symbol=groww_symbol,
                start_time=f"{d.isoformat()} 09:15:00",
                end_time=f"{d.isoformat()} 15:30:00",
                candle_interval="1minute",
            )
            ok += 1
        except Exception:
            break
    elapsed = time.perf_counter() - start
    rps = ok / elapsed if elapsed > 0 else 0.0
    _ok("rate_limit_empirical", f"{ok}/{n_calls} calls in {elapsed:.1f}s = {rps:.1f} r/s")
    RESULTS["effective_rps"] = round(rps, 2)


# ── main probe ────────────────────────────────────────────────────────────

def main() -> int:
    print("\n=== BruteX Groww Phase 0 Probe ===\n")

    # 1. Auth
    print("Gate 1: Authentication")
    try:
        auth = build_from_env()
        token = auth.get_token()
        _ok("auth_totp", f"token starts with {token[:8]}…")
        client = GrowwClient(auth=auth)
    except Exception as exc:
        _fail("auth_totp", str(exc))
        print("\nCannot proceed without auth. Fix BRUTEX_GROWW_API_KEY / BRUTEX_GROWW_TOTP_SECRET.")
        sys.exit(2)

    probe_date = date(2024, 6, 3)  # a known NSE trading day

    # 2. NIFTY 1m bars
    print("\nGate 2: NIFTY 1m bars")
    _probe_candles(client, "nifty_1m", "NSE-NIFTY", "NSE", "CASH", probe_date)

    # 3. INDIA VIX (P0 HARD-GATE)
    print("\nGate 3: INDIA VIX (P0 hard-gate)")
    _probe_candles(client, "indiavix_1m", "NSE-INDIAVIX", "NSE", "CASH", probe_date)

    # 4. BANKNIFTY + SENSEX
    print("\nGate 4: BANKNIFTY + SENSEX")
    _probe_candles(client, "banknifty_1m", "NSE-BANKNIFTY", "NSE", "CASH", probe_date)
    _probe_candles(client, "sensex_1m", "BSE-SENSEX", "BSE", "CASH", probe_date)

    # 5. Expired options — NIFTY 2021-01
    print("\nGate 5: Expired options (NIFTY, Jan 2021)")
    try:
        expiries = client.get_expiries("NSE", "NIFTY", year=2021, month=1)
        if expiries:
            _ok("nifty_expiries_2021_01", f"{len(expiries)} expiries: {expiries[:3]}…")
            # fetch contracts for first expiry
            contracts = client.get_contracts("NSE", "NIFTY", expiries[0])
            if contracts:
                _ok("nifty_contracts_2021_01", f"{len(contracts)} contracts; first={contracts[0]}")
            else:
                _fail("nifty_contracts_2021_01", "no contracts for first expiry")
        else:
            _fail("nifty_expiries_2021_01", "no expiries returned for 2021-01")
    except Exception as exc:
        _fail("nifty_options_2021_01", str(exc))

    # 6. Data depth — 2020-01-02 (first trading day of 2020)
    print("\nGate 6: Data depth (NIFTY 2020-01-02)")
    try:
        payload = client.get_candles(
            exchange="NSE",
            segment="CASH",
            groww_symbol="NSE-NIFTY",
            start_time="2020-01-02 09:15:00",
            end_time="2020-01-02 15:30:00",
            candle_interval="1minute",
        )
        candles = payload.get("candles", [])
        if candles:
            _ok("data_depth_2020", f"{len(candles)} bars on 2020-01-02; first={candles[0][0]}")
        else:
            _fail("data_depth_2020", "no bars returned for 2020-01-02")
    except Exception as exc:
        _fail("data_depth_2020", str(exc))

    # 7. Rate limit measurement
    print("\nGate 7: Rate limit measurement (20 rapid calls)")
    try:
        _probe_rate_limit(client, "NSE-NIFTY")
    except Exception as exc:
        _fail("rate_limit_empirical", str(exc))

    # ── summary ──────────────────────────────────────────────────────────
    print("\n=== Summary ===")
    if FAILURES:
        print(f"❌ {len(FAILURES)} gate(s) FAILED: {FAILURES}")
        if "indiavix_1m" in FAILURES:
            print("\nP0 HARD-GATE FAILED: INDIA VIX not available on Groww.")
            print("Per ADR-0009: Dhan adapter remains if India VIX is absent.")
    else:
        print(f"✅ All {len(RESULTS)} gates passed. Groww adapter is cleared for implementation.")

    # Write results file
    out_dir = Path("docs/groww/probe")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"probe_results_{date.today().isoformat()}.json"
    out_file.write_text(json.dumps(RESULTS, indent=2, default=str))
    print(f"\nResults written to {out_file}")

    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
