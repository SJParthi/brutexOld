#!/usr/bin/env python3
"""Probe a Dhan symbol's DAILY vs 1-MINUTE candle endpoints to diagnose DH-905.

DEC-DHAN-NO-DATA-KNOWN-GAP-001 (Q4 of the `make run` deep-check). An operator
``make run`` showed ``NIFTY NEXT 50`` 1-DAY rejected by Dhan with
``DH-905`` ("incorrect parameters or no data present") while its 1-MINUTE pulled
fine (501,945 bars). DH-905 is ambiguous — a genuinely-empty range (an index
whose daily history starts AFTER the requested ``fromDate``) OR a true param bug.
This probe RESOLVES the ambiguity empirically: it fires the SAME two requests the
ingest spine fires (``POST /v2/charts/historical`` daily + ``POST
/v2/charts/intraday`` 1-minute) for one ``securityId`` and prints each call's
outcome — so the operator can confirm whether daily is a real Dhan gap, a
range-too-early boundary (auto-narrow then resolves it), or a param mismatch.

Run it on YOUR machine with YOUR token (it is NOT part of the engine; it makes
real Dhan calls, gated by the 5 r/s ceiling is irrelevant for 2 calls). The
``securityId`` + ``exchangeSegment`` come from the dashboard ``/db`` instruments
table (column ``security_id``); indices use ``IDX_I`` + instrument ``INDEX``.

Examples
--------
    # NIFTY NEXT 50 (resolve its security_id from /db first), full Track-1 range
    BRUTEX_DHAN_ACCESS_TOKEN=... BRUTEX_DHAN_CLIENT_ID=... \\
      python scripts/dhan_daily_probe.py --security-id 26013 \\
        --from 2021-01-01 --to 2026-06-07

    # Narrow the daily start to test the "range too early" hypothesis
    python scripts/dhan_daily_probe.py --security-id 26013 --from 2024-01-01

Exit code 0 always (it is a diagnostic, never a gate); read the printed report.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, time, timedelta

from brutex.adapters.dhan.auth import resolve_dhan_credentials
from brutex.adapters.dhan.body import build_historical_body, build_intraday_body
from brutex.adapters.dhan.client import DhanClient, urllib_transport
from brutex.adapters.dhan.constants import ENDPOINT_HISTORICAL, ENDPOINT_INTRADAY
from brutex.adapters.dhan.errors import DhanError, is_no_data_error


def _parse_date(value: str) -> date:
    """Parse an ISO ``YYYY-MM-DD`` date (argparse type)."""
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).date()


def _probe_one(client: DhanClient, *, label: str, endpoint: str, body: dict[str, object]) -> None:
    """Fire one endpoint and print a one-line OUTCOME (OK / NO_DATA / ERROR)."""
    sys.stdout.write(f"\n── {label}  (POST /v2/{endpoint}) ──\n")
    sys.stdout.write(f"   request: {body}\n")
    # Gate the INITIAL physical POST through the client's rate-bucket hook
    # (no-op unless a controller is wired into ``DhanClient.pace``), so EVERY
    # physical Dhan POST stays bucket-bounded ≤5 r/s even if this diagnostic is
    # ever threaded into a live pull — the fast-retry path self-gates inside
    # ``post_json`` (DEC-DHAN-RATE-RESIDUAL-HARDEN-001, CLAUDE.md §2 Rule 6).
    client.pace()
    try:
        payload = client.post_json(endpoint, body)
    except DhanError as exc:
        kind = (
            "NO_DATA (DH-905 — treated as a KNOWN_GAP, re-tried next pull)"
            if is_no_data_error(exc)
            else f"ERROR ({type(exc).__name__})"
        )
        sys.stdout.write(f"   outcome: {kind}\n   detail : {exc}\n")
        return
    data = payload.get("data") if isinstance(payload, dict) else None
    source = data if isinstance(data, dict) else payload
    n = len(source.get("close", [])) if isinstance(source, dict) else 0
    sys.stdout.write(f"   outcome: OK — {n} candles returned\n")


def main(argv: list[str] | None = None) -> int:
    """CLI entry: probe the daily + 1-minute endpoints for one ``securityId``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--security-id", required=True, help="Dhan SECURITY_ID (from /db)")
    parser.add_argument(
        "--segment", default="IDX_I", help="exchangeSegment (default IDX_I — indices)"
    )
    parser.add_argument("--instrument", default="INDEX", help="Dhan instrument (default INDEX)")
    parser.add_argument("--from", dest="from_date", type=_parse_date, default=date(2021, 1, 1))
    parser.add_argument("--to", dest="to_date", type=_parse_date, default=None)
    args = parser.parse_args(argv)

    to_date = args.to_date or (datetime.now(UTC).date())
    # Dhan's toDate is non-inclusive → add a day so the end day is covered.
    from_dt = datetime.combine(args.from_date, time())
    to_dt = datetime.combine(to_date + timedelta(days=1), time())

    creds = resolve_dhan_credentials()
    client = DhanClient(
        access_token=creds.access_token,
        client_id=creds.client_id,
        transport=urllib_transport,
    )

    sys.stdout.write(
        f"Dhan daily-vs-intraday probe — securityId={args.security_id} "
        f"segment={args.segment} instrument={args.instrument} "
        f"range=[{args.from_date} .. {to_date}]\n"
    )
    _probe_one(
        client,
        label="DAILY",
        endpoint=ENDPOINT_HISTORICAL,
        body=build_historical_body(
            security_id=args.security_id,
            exchange_segment=args.segment,
            instrument=args.instrument,
            from_date=from_dt,
            to_date=to_dt,
        ),
    )
    _probe_one(
        client,
        label="1-MINUTE",
        endpoint=ENDPOINT_INTRADAY,
        body=build_intraday_body(
            security_id=args.security_id,
            exchange_segment=args.segment,
            instrument=args.instrument,
            from_date=from_dt,
            to_date=to_dt,
        ),
    )
    sys.stdout.write(
        "\nInterpretation:\n"
        "  • DAILY NO_DATA + 1-MINUTE OK  → genuine Dhan daily gap for this index\n"
        "    (or daily history starts later — re-run --from a recent year to confirm).\n"
        "  • DAILY OK with a narrower --from → range-too-early; auto-narrow will fix it.\n"
        "  • BOTH ERROR → a credential / securityId / segment problem, not a data gap.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
