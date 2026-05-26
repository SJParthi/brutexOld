#!/usr/bin/env python3
"""Phase 0b empirical probe for the Groww Trade API (DEC-VENDOR-PIVOT-001).

STANDALONE script — imports ZERO ``brutex/`` modules. Runs from operator's
laptop after they subscribe to the Groww Trade API. Verifies the 4 P0
unknowns that DEC-VENDOR-PIVOT-001 §3.1 documented as accepted risks:

1. **INDIA VIX presence** in Groww's instruments CSV — HARD GATE.
   If VIX is absent → write `docs/adr/0002-groww-vendor-pivot-RESCIND.md`
   and halt all Phase 1+ work.

2. **1-minute candle support for indices** — verify
   ``get_historical_candles(NSE-INDIAVIX, 1m, 2021-01-04)`` returns
   non-empty data.

3. **Rate-limit bucket assignment for the backtesting endpoint** —
   the official docs don't list backtesting in the rate-limit table.
   Empirical probe fires 100 calls in a 10-second window and counts
   GrowwAPIRateLimitException occurrences to discover the practical cap.

4. **Data-quality drift** — fetch 1 trading day of NIFTY 1-minute OHLC
   from Groww and write a deterministic JSON. If you've kept Dhan's
   historical NIFTY 1-min data for the same day archived locally, a
   companion script ``scripts/compare_dhan_groww_drift.py`` (not in this
   PR) can be used to bar-by-bar diff. > 5% mismatches → write a follow-up
   ADR before Phase 1 code lands.

USAGE
=====

    # 1. Subscribe to Groww Trade API at https://groww.in/user/profile/trading-apis
    # 2. Generate TOTP token at https://groww.in/trade-api/api-keys
    # 3. Export env vars:
    export BRUTEX_GROWW_API_KEY="<your-totp-token>"
    export BRUTEX_GROWW_TOTP_SECRET="<your-totp-secret>"

    # 4. Run the probe:
    python scripts/groww_probe.py

Outputs 4 JSON transcripts to ``docs/groww/probe/``:

* ``01_vix_csv_grep.json`` — CSV grep result for INDIA VIX / INDIAVIX
* ``02_vix_1m_candle.json`` — VIX 1m candle fetch result + sample bars
* ``03_rate_limit_probe.json`` — 100-call burst probe + exception counts
* ``04_nifty_1m_dayrun.json`` — 1 trading day of NIFTY 1m for drift compare

EXIT CODES
==========

* 0 — all 4 probes completed; check each JSON's `passed` field
* 2 — env vars missing (BRUTEX_GROWW_API_KEY / BRUTEX_GROWW_TOTP_SECRET)
* 3 — Groww SDK not installed (``pip install growwapi pyotp``)
* 4 — auth failure (invalid token / TOTP)
* 5 — INDIA VIX absent (HARD STOP — DEC-VENDOR-PIVOT-001 rescinded)

DECISION ID
===========

DEC-VENDOR-PIVOT-001 (entry #246) Phase 0b. Builds on docs/groww/api-reference/
authoritative source-of-truth + docs/TRACK1_GROWW_ADAPTER.md §5 spec.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE_OUTPUT_DIR = REPO_ROOT / "docs" / "groww" / "probe"

INSTRUMENT_CSV_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"

# Candidate symbols for INDIA VIX (per CLAUDE.md §6 + docs/groww/api-reference/02-instruments.md)
VIX_SYMBOL_CANDIDATES = ("NSE-INDIAVIX", "NSE-INDIA_VIX", "NSE-INDIAVOL", "NSE-INDIA-VIX")

# NIFTY 1m drift-compare day (mid-week, non-holiday, post-Budget 2024)
NIFTY_DRIFT_DAY = "2024-02-05"  # Monday
NIFTY_SYMBOL = "NSE-NIFTY"

# Rate-limit probe parameters
RATE_PROBE_CALL_COUNT = 100
RATE_PROBE_WINDOW_SEC = 10.0

# ─────────────────────────────────────────────────────────────────
# Result dataclasses
# ─────────────────────────────────────────────────────────────────


@dataclass
class ProbeResult:
    """Single probe-step result. Persisted as JSON."""

    probe_name: str
    passed: bool
    details: dict[str, Any]
    timestamp_utc: str
    duration_sec: float


# ─────────────────────────────────────────────────────────────────
# Env + SDK readiness
# ─────────────────────────────────────────────────────────────────


def _read_required_env(name: str) -> str:
    """Read an env var or exit 2 with a clear message."""
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"❌ Required env var {name} is not set.", file=sys.stderr)
        print(
            "   Read docs/groww/api-reference/01-introduction.md Step 3 (TOTP flow).",
            file=sys.stderr,
        )
        sys.exit(2)
    return value


def _import_groww_sdk() -> Any:
    """Import growwapi or exit 3 with a clear pip-install message."""
    try:
        from growwapi import GrowwAPI

        return GrowwAPI
    except ImportError:
        print("❌ growwapi SDK is not installed.", file=sys.stderr)
        print("   Run: pip install 'growwapi>=1.5,<2.0' 'pyotp>=2.9,<3.0'", file=sys.stderr)
        sys.exit(3)


def _import_pyotp() -> Any:
    """Import pyotp or exit 3."""
    try:
        import pyotp

        return pyotp
    except ImportError:
        print("❌ pyotp is not installed.", file=sys.stderr)
        print("   Run: pip install 'pyotp>=2.9,<3.0'", file=sys.stderr)
        sys.exit(3)


def _build_groww_client() -> Any:
    """Build an authenticated GrowwAPI client. Exits 4 on auth failure."""
    api_key = _read_required_env("BRUTEX_GROWW_API_KEY")
    totp_secret = _read_required_env("BRUTEX_GROWW_TOTP_SECRET")

    groww_sdk = _import_groww_sdk()
    pyotp = _import_pyotp()

    totp = pyotp.TOTP(totp_secret).now()
    try:
        access_token = groww_sdk.get_access_token(api_key=api_key, totp=totp)
    except Exception as exc:
        print(f"❌ Groww auth failed: {exc}", file=sys.stderr)
        print("   Check BRUTEX_GROWW_API_KEY + BRUTEX_GROWW_TOTP_SECRET.", file=sys.stderr)
        sys.exit(4)

    return groww_sdk(access_token)


# ─────────────────────────────────────────────────────────────────
# Probe 1 — INDIA VIX presence in instruments CSV (HARD GATE)
# ─────────────────────────────────────────────────────────────────


def probe_01_vix_in_csv() -> ProbeResult:
    """Download Groww's instrument CSV and grep for INDIA VIX rows.

    HARD GATE: if VIX absent → DEC-VENDOR-PIVOT-001 is rescinded.
    """
    import urllib.request

    started = time.time()
    timestamp_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))

    try:
        request = urllib.request.Request(  # noqa: S310 — instruments CSV is a known Groww asset
            INSTRUMENT_CSV_URL,
            headers={"User-Agent": "BruteX-Phase0b-Probe/1.0"},
        )
        with urllib.request.urlopen(request, timeout=30.0) as response:  # noqa: S310
            csv_text = response.read().decode("utf-8")
    except Exception as exc:
        return ProbeResult(
            probe_name="01_vix_csv_grep",
            passed=False,
            details={"error": str(exc), "url": INSTRUMENT_CSV_URL},
            timestamp_utc=timestamp_utc,
            duration_sec=round(time.time() - started, 3),
        )

    # Greedy match: any row containing 'VIX' in any column
    vix_rows = [line for line in csv_text.splitlines() if "VIX" in line.upper()]
    indiavix_rows = [line for line in vix_rows if "INDIAVIX" in line.upper().replace(" ", "")]

    # Try each candidate groww_symbol explicitly
    candidate_hits = {
        candidate: any(candidate in line for line in csv_text.splitlines())
        for candidate in VIX_SYMBOL_CANDIDATES
    }

    passed = bool(indiavix_rows) or any(candidate_hits.values())

    return ProbeResult(
        probe_name="01_vix_csv_grep",
        passed=passed,
        details={
            "csv_url": INSTRUMENT_CSV_URL,
            "csv_byte_count": len(csv_text),
            "total_rows": len(csv_text.splitlines()),
            "rows_containing_VIX": len(vix_rows),
            "rows_containing_INDIAVIX": len(indiavix_rows),
            "candidate_groww_symbol_hits": candidate_hits,
            "first_5_vix_rows": vix_rows[:5],
            "hard_gate_passed": passed,
            "if_failed_action": "Write docs/adr/0002-groww-vendor-pivot-RESCIND.md and halt all Phase 1+ work.",
        },
        timestamp_utc=timestamp_utc,
        duration_sec=round(time.time() - started, 3),
    )


# ─────────────────────────────────────────────────────────────────
# Probe 2 — INDIA VIX 1-minute candle fetch
# ─────────────────────────────────────────────────────────────────


def probe_02_vix_1m_candle(client: Any) -> ProbeResult:
    """Fetch 1 trading day of INDIA VIX 1m OHLC. Try each candidate symbol."""
    started = time.time()
    timestamp_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))

    attempts: list[dict[str, Any]] = []

    for candidate in VIX_SYMBOL_CANDIDATES:
        try:
            response = client.get_historical_candles(
                exchange=client.EXCHANGE_NSE,
                segment=client.SEGMENT_CASH,
                groww_symbol=candidate,
                start_time="2024-01-02 09:15:00",
                end_time="2024-01-02 15:30:00",
                candle_interval=client.CANDLE_INTERVAL_MIN_1,
            )
            candles = response.get("candles", [])
            attempts.append(
                {
                    "groww_symbol": candidate,
                    "ok": True,
                    "candle_count": len(candles),
                    "sample_first_3_bars": candles[:3] if candles else [],
                    "sample_last_3_bars": candles[-3:] if candles else [],
                }
            )
            if candles:
                # Found a working symbol — stop trying more
                break
        except Exception as exc:
            attempts.append(
                {
                    "groww_symbol": candidate,
                    "ok": False,
                    "error_class": type(exc).__name__,
                    "error_msg": str(exc),
                }
            )

    passed = any(a.get("ok") and a.get("candle_count", 0) > 0 for a in attempts)

    return ProbeResult(
        probe_name="02_vix_1m_candle",
        passed=passed,
        details={
            "trading_day": "2024-01-02",
            "expected_minute_count": 375,  # 09:15-15:30 = 375 minutes
            "attempts": attempts,
            "if_failed_action": "Same as probe 01 — rescind DEC-VENDOR-PIVOT-001.",
        },
        timestamp_utc=timestamp_utc,
        duration_sec=round(time.time() - started, 3),
    )


# ─────────────────────────────────────────────────────────────────
# Probe 3 — Rate-limit bucket assignment empirical discovery
# ─────────────────────────────────────────────────────────────────


def probe_03_rate_limit(client: Any) -> ProbeResult:
    """Fire 100 backtesting calls in 10 sec; count GrowwAPIRateLimitException.

    Reveals whether the backtesting endpoint shares the Live Data bucket
    (10/sec) or has a stricter undocumented limit.
    """
    started = time.time()
    timestamp_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))

    call_log: list[dict[str, Any]] = []
    rate_limit_hits = 0
    other_errors = 0
    success_count = 0

    target_rate_per_sec = RATE_PROBE_CALL_COUNT / RATE_PROBE_WINDOW_SEC

    for call_index in range(RATE_PROBE_CALL_COUNT):
        target_time = started + (call_index / target_rate_per_sec)
        sleep_for = max(0.0, target_time - time.time())
        if sleep_for > 0:
            time.sleep(sleep_for)

        call_started = time.time()
        try:
            client.get_historical_candles(
                exchange=client.EXCHANGE_NSE,
                segment=client.SEGMENT_CASH,
                groww_symbol=NIFTY_SYMBOL,
                start_time="2024-02-05 09:15:00",
                end_time="2024-02-05 09:30:00",
                candle_interval=client.CANDLE_INTERVAL_MIN_1,
            )
            success_count += 1
            call_log.append(
                {
                    "i": call_index,
                    "t_offset_sec": round(call_started - started, 4),
                    "result": "ok",
                }
            )
        except Exception as exc:
            error_class = type(exc).__name__
            is_rate_limit = "RateLimit" in error_class
            if is_rate_limit:
                rate_limit_hits += 1
            else:
                other_errors += 1
            call_log.append(
                {
                    "i": call_index,
                    "t_offset_sec": round(call_started - started, 4),
                    "result": "rate_limit" if is_rate_limit else "other_error",
                    "error_class": error_class,
                    "error_msg": str(exc)[:200],
                }
            )

    total_duration = time.time() - started

    return ProbeResult(
        probe_name="03_rate_limit_probe",
        passed=other_errors == 0,
        details={
            "target_call_count": RATE_PROBE_CALL_COUNT,
            "target_window_sec": RATE_PROBE_WINDOW_SEC,
            "target_rate_per_sec": target_rate_per_sec,
            "actual_duration_sec": round(total_duration, 3),
            "actual_rate_per_sec": round(RATE_PROBE_CALL_COUNT / total_duration, 3),
            "success_count": success_count,
            "rate_limit_hits": rate_limit_hits,
            "other_errors": other_errors,
            "interpretation": _interpret_rate_limit(rate_limit_hits, success_count),
            "call_log_sample_first_10": call_log[:10],
            "call_log_sample_last_10": call_log[-10:],
        },
        timestamp_utc=timestamp_utc,
        duration_sec=round(total_duration, 3),
    )


def _interpret_rate_limit(rate_hits: int, success: int) -> str:
    """Plain-English interpretation of the rate-limit probe result."""
    if rate_hits == 0:
        return (
            f"All {success} calls succeeded at ~10 r/s burst. Backtesting endpoint likely shares the "
            "Live Data bucket (10/sec). BruteX adaptive [4..8] r/s rate-limiter is comfortably within."
        )
    if rate_hits > 50:
        return (
            f"More than half ({rate_hits}) calls hit GrowwAPIRateLimitException at 10 r/s burst. "
            "Backtesting endpoint is in a STRICTER bucket than Live Data (probably 2-5 r/s). "
            "BruteX adaptive rate-limiter MIN_RPS may need to drop below 4."
        )
    return (
        f"Partial throttling: {rate_hits}/{success + rate_hits} calls hit rate limit. "
        "Bucket assignment is ambiguous; adaptive rate-limiter will discover the practical cap empirically."
    )


# ─────────────────────────────────────────────────────────────────
# Probe 4 — NIFTY 1m day-run for drift comparison
# ─────────────────────────────────────────────────────────────────


def probe_04_nifty_1m_dayrun(client: Any) -> ProbeResult:
    """Fetch 1 trading day of NIFTY 1m OHLC for Dhan-vs-Groww drift compare.

    Saves the full bars to disk so a follow-up script (or operator's
    archived Dhan data) can bar-by-bar diff for data-quality verification
    per DEC-VENDOR-PIVOT-001 §3.1 risk #3.
    """
    started = time.time()
    timestamp_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))

    try:
        response = client.get_historical_candles(
            exchange=client.EXCHANGE_NSE,
            segment=client.SEGMENT_CASH,
            groww_symbol=NIFTY_SYMBOL,
            start_time=f"{NIFTY_DRIFT_DAY} 09:15:00",
            end_time=f"{NIFTY_DRIFT_DAY} 15:30:00",
            candle_interval=client.CANDLE_INTERVAL_MIN_1,
        )
    except Exception as exc:
        return ProbeResult(
            probe_name="04_nifty_1m_dayrun",
            passed=False,
            details={
                "trading_day": NIFTY_DRIFT_DAY,
                "groww_symbol": NIFTY_SYMBOL,
                "error_class": type(exc).__name__,
                "error_msg": str(exc),
            },
            timestamp_utc=timestamp_utc,
            duration_sec=round(time.time() - started, 3),
        )

    candles = response.get("candles", [])
    expected_minutes = 375  # 09:15-15:30 IST

    return ProbeResult(
        probe_name="04_nifty_1m_dayrun",
        passed=len(candles) >= expected_minutes * 0.95,  # tolerate up to 5% missing
        details={
            "trading_day": NIFTY_DRIFT_DAY,
            "groww_symbol": NIFTY_SYMBOL,
            "candle_count": len(candles),
            "expected_minute_count": expected_minutes,
            "completeness_pct": (
                round(len(candles) / expected_minutes * 100.0, 2) if candles else 0.0
            ),
            "first_5_bars": candles[:5] if candles else [],
            "last_5_bars": candles[-5:] if candles else [],
            "raw_candles": candles,  # full payload for drift diff
            "note": (
                "Use scripts/compare_dhan_groww_drift.py (future) to bar-by-bar diff "
                "this against archived Dhan NIFTY 1m for the same day. > 5% mismatch "
                "triggers a follow-up ADR before Phase 1 code lands."
            ),
        },
        timestamp_utc=timestamp_utc,
        duration_sec=round(time.time() - started, 3),
    )


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────


def main() -> int:
    """Run all 4 probes; persist each result to PROBE_OUTPUT_DIR."""
    print("=" * 65)
    print("BruteX Phase 0b — Groww empirical probe (DEC-VENDOR-PIVOT-001)")
    print("=" * 65)

    PROBE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Probe 1 — VIX presence in CSV (no Groww auth required; standalone HTTP)
    print("\n[1/4] INDIA VIX presence in instruments CSV (HARD GATE)...")
    result1 = probe_01_vix_in_csv()
    _persist(result1)
    _print_summary(result1)

    if not result1.passed:
        print("\n🛑 INDIA VIX absent from Groww instruments CSV.")
        print("   DEC-VENDOR-PIVOT-001 must be rescinded.")
        print("   Operator action: write docs/adr/0002-groww-vendor-pivot-RESCIND.md")
        return 5

    # Probes 2-4 require authenticated Groww client
    print("\n[Auth] Building authenticated Groww client...")
    client = _build_groww_client()
    print("       OK.")

    print("\n[2/4] INDIA VIX 1-minute candle fetch...")
    result2 = probe_02_vix_1m_candle(client)
    _persist(result2)
    _print_summary(result2)

    print(
        f"\n[3/4] Rate-limit empirical probe ({RATE_PROBE_CALL_COUNT} calls in "
        f"{RATE_PROBE_WINDOW_SEC}s)..."
    )
    result3 = probe_03_rate_limit(client)
    _persist(result3)
    _print_summary(result3)

    print(f"\n[4/4] NIFTY 1m {NIFTY_DRIFT_DAY} day-run for drift compare...")
    result4 = probe_04_nifty_1m_dayrun(client)
    _persist(result4)
    _print_summary(result4)

    print("\n" + "=" * 65)
    print("All 4 probes complete. Transcripts in:")
    print(f"   {PROBE_OUTPUT_DIR}")
    print("=" * 65)
    return 0


def _persist(result: ProbeResult) -> None:
    """Write a probe result as JSON to PROBE_OUTPUT_DIR/<probe_name>.json."""
    path = PROBE_OUTPUT_DIR / f"{result.probe_name}.json"
    path.write_text(json.dumps(asdict(result), indent=2, default=str), encoding="utf-8")
    print(f"       Saved: {path.relative_to(REPO_ROOT)}")


def _print_summary(result: ProbeResult) -> None:
    """Print a one-line summary of a probe result."""
    icon = "✅" if result.passed else "❌"
    print(f"       {icon} {result.probe_name} — passed={result.passed} ({result.duration_sec}s)")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
