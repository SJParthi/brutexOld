#!/usr/bin/env python3
"""Phase-0 empirical probe for the Dhan -> Groww re-migration (ADR-0009).

`DEC-DHAN-GROWW-REMIGRATE-001` (MIGRATION.md #667). ADR-0009 reverses ADR-0002
(Dhan) back to Groww on operator directive, OVERRIDING a 4-agent hostile-review
NO-GO. The override is conditional on this probe: several Groww facts are
UNVERIFIED and MUST NOT be encoded as fact in adapter code until a committed
transcript resolves them. This script is that gate.

It is a STANDALONE diagnostic — it imports nothing from ``brutex`` and is never
on the engine import path, so it neither affects coverage nor needs the adapter
to exist yet. It imports ``growwapi`` LAZILY inside ``main`` so the module stays
import-clean in CI (where the SDK is absent).

It CANNOT run in the build sandbox (no Groww credentials, no network egress).
Run it on YOUR machine with YOUR Groww credentials, then commit the printed
report to ``docs/groww/probe/`` so PR-B onward can cite it.

The six checks map 1:1 to ADR-0009 §4 (each prints OK / ABSENT / ERROR / SKIP):

  1. India VIX existence            (HARD-GATE; ADR-0009 risk #3)
  2. 1m granularity at 2020 boundary (risk #6 — COVID window)
  3. backtest-vs-live parity         (measures risk #1 — the withdrawn folklore)
  4. cross-vendor 1m drift           (risk #5 — needs a Dhan series alongside)
  5. rate limit / daily quota / token lifetime (risks #2, #4)
  6. options round-trip              (Track-2 / lake readiness)

Environment
-----------
    BRUTEX_GROWW_API_KEY      TOTP api key
    BRUTEX_GROWW_TOTP_SECRET  TOTP shared secret (pyotp generates the 6-digit code)

Examples
--------
    BRUTEX_GROWW_API_KEY=... BRUTEX_GROWW_TOTP_SECRET=... \\
      python scripts/groww_probe.py --checks vix,granularity,ratelimit

    # Full run, writing the transcript to the canonical location:
    python scripts/groww_probe.py --all | tee docs/groww/probe/probe-$(date +%F).txt

Exit code 0 always (a diagnostic, never a CI gate); read the printed report and
commit it. A non-zero exit ONLY signals the script could not start (bad args /
missing SDK / missing credentials), never a probe verdict.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

# India VIX is the HARD-GATE (ADR-0009 risk #3). The exact groww_symbol is one
# of the unknowns the probe resolves; this is the documented best guess to try.
_VIX_SYMBOL_CANDIDATES: tuple[str, ...] = ("NSE-INDIAVIX", "NSE-INDIA VIX", "NSE-INDIAVIX-IDX")

_ALL_CHECKS: tuple[str, ...] = (
    "vix",
    "granularity",
    "parity",
    "drift",
    "ratelimit",
    "options",
)


def _emit(line: str = "") -> None:
    """Write one report line to stdout (the transcript). No ``print`` per CLAUDE.md §4."""
    sys.stdout.write(f"{line}\n")


def _section(title: str) -> None:
    """Print a section header so the committed transcript is self-describing."""
    _emit()
    _emit(f"── {title} ".ljust(72, "─"))


def _resolve_credentials() -> tuple[str, str]:
    """Read Groww TOTP credentials from the environment or exit 2.

    Returns ``(api_key, totp_secret)``. Exiting (not returning a sentinel) keeps
    every probe path past this point credential-safe.
    """
    api_key = os.environ.get("BRUTEX_GROWW_API_KEY", "").strip()
    totp_secret = os.environ.get("BRUTEX_GROWW_TOTP_SECRET", "").strip()
    if not api_key or not totp_secret:
        _emit(
            "ERROR: set BRUTEX_GROWW_API_KEY and BRUTEX_GROWW_TOTP_SECRET "
            "(this probe makes REAL Groww calls — it cannot run unauthenticated)."
        )
        raise SystemExit(2)
    return api_key, totp_secret


def _make_client(api_key: str, totp_secret: str) -> object:
    """Authenticate and return a ``GrowwAPI`` client (lazy SDK import).

    ``growwapi`` + ``pyotp`` are imported HERE, not at module top, so the script
    imports cleanly in CI where the ``groww`` extra is absent (ADR-0009 PR-B
    re-adds it). A missing SDK exits 2 with an actionable message.
    """
    try:
        import pyotp  # type: ignore[import-not-found]
        from growwapi import GrowwAPI  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        _emit(
            f"ERROR: {exc.name!r} is not installed. Install the groww extra "
            "first:  pip install -e '.[groww]'  (re-added by ADR-0009 PR-B)."
        )
        raise SystemExit(2) from exc

    totp_code = pyotp.TOTP(totp_secret).now()
    access_token = GrowwAPI.get_access_token(api_key=api_key, totp=totp_code)
    return GrowwAPI(access_token)


def _check_vix(client: object) -> None:
    """ADR-0009 §4.1 — HARD-GATE: is India VIX present + fetchable on Groww?"""
    _section("CHECK 1 — India VIX existence (HARD-GATE, ADR-0009 risk #3)")
    _emit("Try each candidate groww_symbol; OK on first that returns candles.")
    for symbol in _VIX_SYMBOL_CANDIDATES:
        try:
            resp = client.get_historical_candles(  # type: ignore[attr-defined]
                exchange="NSE",
                segment="CASH",
                groww_symbol=symbol,
                start_time="2024-01-01 09:15:00",
                end_time="2024-01-02 15:30:00",
                candle_interval="1minute",
            )
        except Exception as exc:  # broad: a diagnostic must report any vendor error verbatim
            _emit(f"  {symbol:<22} ERROR  {type(exc).__name__}: {exc}")
            continue
        candles = resp.get("candles", []) if isinstance(resp, dict) else []
        verdict = "OK" if candles else "ABSENT (empty candles)"
        _emit(f"  {symbol:<22} {verdict}  (rows={len(candles)})")
    _emit("VERDICT: VIX is present iff at least one candidate printed OK above.")


def _check_granularity(client: object) -> None:
    """ADR-0009 §4.2 — do NIFTY 1m rows truly start Jan 2020 (COVID window)?"""
    _section("CHECK 2 — 1m granularity at the 2020 boundary (risk #6)")
    try:
        resp = client.get_historical_candles(  # type: ignore[attr-defined]
            exchange="NSE",
            segment="CASH",
            groww_symbol="NSE-NIFTY",
            start_time="2020-01-01 09:15:00",
            end_time="2020-03-31 15:30:00",
            candle_interval="1minute",
        )
    except Exception as exc:  # broad: a diagnostic must report any vendor error verbatim
        _emit(f"  ERROR  {type(exc).__name__}: {exc}")
        return
    candles = resp.get("candles", []) if isinstance(resp, dict) else []
    first = candles[0][0] if candles else "—"
    last = candles[-1][0] if candles else "—"
    _emit(f"  rows={len(candles)}  first_ts={first}  last_ts={last}")
    _emit("VERDICT: confirm first_ts is 2020-01 at 1-minute resolution (not daily/2021).")


def _check_parity(_client: object) -> None:
    """ADR-0009 §4.3 — measure the WITHDRAWN backtest-vs-live folklore (risk #1)."""
    _section("CHECK 3 — backtest-vs-live parity (measures withdrawn risk #1)")
    _emit("MANUAL: on a LIVE session, capture get_historical_candles 1m for today")
    _emit("        vs the live feed for the same bars; diff bar-by-bar. Re-fetch")
    _emit("        the SAME day T+2 days later to detect restatement.")
    _emit("VERDICT: the parity claim stands ONLY on measured zero-drift +")
    _emit("         zero-restatement. Until then it remains 'unverified' (ADR-0009).")


def _check_drift(_client: object) -> None:
    """ADR-0009 §4.4 — Dhan vs Groww 1m bar-by-bar drift (risk #5)."""
    _section("CHECK 4 — cross-vendor 1m drift Dhan vs Groww (risk #5)")
    _emit("MANUAL: pull NIFTY 1m for one full session from BOTH vendors and diff")
    _emit("        OHLC bar-by-bar. Needs a Dhan series alongside (scripts/dhan_*).")
    _emit("VERDICT: quantify mismatch %; >5% triggers the ADR-0009 §7 reversal path.")


def _check_ratelimit(_client: object) -> None:
    """ADR-0009 §4.5 — discover the undocumented backtesting cap + token lifetime."""
    _section("CHECK 5 — rate limit / daily quota / token lifetime (risks #2, #4)")
    _emit("MANUAL: fire get_historical_candles in a tight loop; record the r/s and")
    _emit("        daily-count at which GrowwAPIRateLimitException first appears.")
    _emit("        Separately, confirm how long an unattended TOTP token stays valid.")
    _emit("VERDICT: record the observed cap; it sets the adaptive token-bucket ceiling.")


def _check_options(client: object) -> None:
    """ADR-0009 §4.6 — expiries -> contracts -> candles round-trip (lake readiness)."""
    _section("CHECK 6 — options round-trip get_expiries/get_contracts (Track-2)")
    try:
        expiries = client.get_expiries(  # type: ignore[attr-defined]
            exchange="NSE", underlying_symbol="NIFTY", year=2020, month=1
        )
    except Exception as exc:  # broad: a diagnostic must report any vendor error verbatim
        _emit(f"  get_expiries ERROR  {type(exc).__name__}: {exc}")
        return
    exp_list = expiries.get("expiries", []) if isinstance(expiries, dict) else []
    _emit(f"  expiries(NIFTY 2020-01) = {exp_list}")
    if not exp_list:
        _emit("VERDICT: no 2020-01 expiries — confirm options depth before lake ingest.")
        return
    try:
        contracts = client.get_contracts(  # type: ignore[attr-defined]
            exchange="NSE", underlying_symbol="NIFTY", expiry_date=exp_list[0]
        )
    except Exception as exc:  # broad: a diagnostic must report any vendor error verbatim
        _emit(f"  get_contracts ERROR  {type(exc).__name__}: {exc}")
        return
    con_list = contracts.get("contracts", []) if isinstance(contracts, dict) else []
    _emit(f"  contracts({exp_list[0]}) count={len(con_list)}  sample={con_list[:3]}")
    _emit("VERDICT: a non-empty all-strikes contract list confirms the options flow.")


_CHECK_FUNCS = {
    "vix": _check_vix,
    "granularity": _check_granularity,
    "parity": _check_parity,
    "drift": _check_drift,
    "ratelimit": _check_ratelimit,
    "options": _check_options,
}


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse CLI args: ``--all`` or a comma-separated ``--checks`` subset."""
    parser = argparse.ArgumentParser(description="Groww Phase-0 probe (ADR-0009).")
    parser.add_argument("--all", action="store_true", help="run every check")
    parser.add_argument(
        "--checks",
        default="",
        help=f"comma-separated subset of: {','.join(_ALL_CHECKS)}",
    )
    return parser.parse_args(argv)


def _selected_checks(args: argparse.Namespace) -> list[str]:
    """Resolve the requested check names, or exit 2 on an unknown name."""
    if args.all or not args.checks:
        return list(_ALL_CHECKS)
    requested = [c.strip() for c in args.checks.split(",") if c.strip()]
    unknown = [c for c in requested if c not in _CHECK_FUNCS]
    if unknown:
        _emit(f"ERROR: unknown check(s) {unknown}; valid: {','.join(_ALL_CHECKS)}")
        raise SystemExit(2)
    return requested


def main(argv: Sequence[str] | None = None) -> int:
    """Probe entry point. Returns 0 (diagnostic) unless it could not start (2)."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    checks = _selected_checks(args)

    _emit("Groww Phase-0 probe — ADR-0009 / DEC-DHAN-GROWW-REMIGRATE-001")
    _emit(f"checks: {', '.join(checks)}")

    api_key, totp_secret = _resolve_credentials()
    client = _make_client(api_key, totp_secret)

    for name in checks:
        _CHECK_FUNCS[name](client)

    _section("DONE")
    _emit("Commit this transcript to docs/groww/probe/ before PR-B (ADR-0009 §3.2).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
