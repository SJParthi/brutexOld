#!/usr/bin/env python3
"""Post-close settle probe — WHEN does Groww settle TODAY's 1-minute day?

``DEC-GROWW-POSTCLOSE-SETTLE-PROBE-001`` (MIGRATION.md #1752; companion of
DEC-PULL-TODAY-COMPLETENESS-GATE-001 #1751). The vendor's end-of-day settlement
time is UNDOCUMENTED (the vendor docs are silent; MIGRATION.md #837 is the only
empirical record of the 6-element forming shape). This probe measures it: run it
at 15:35 / 15:45 / 16:00 / 16:30 / 17:30 IST on >= 5 regular trading days
(see ``POSTCLOSE_SETTLE_RUNBOOK.md``) and it records, per symbol, the LITERAL
raw row shapes (6-vs-7 elements, null volume/OI cells — the evidence the parse
destroys), the parsed bar count vs the session's expected minutes, and the
close-edge law — the exact criteria the pre-settle TODAY gate judges.

READ-ONLY by construction: only ``get_historical_candles`` / ``get_expiries`` /
``get_contracts`` + one instrument-master CSV download; ZERO lake writes, ZERO
ledger settles, ZERO sink/writer imports. The client is the doctor kit's
read-only build — mint PHYSICALLY blocked, the shared SSM read-only token, and
the production AIMD controller + 500/min governor pacing every call (CLAUDE.md
§2 Rule 6 — never an unpaced client). ~17 calls per slot ≈ 85/day.

``brutex`` is imported LAZILY inside :func:`main` (so ``--help`` works without
the package); the script lives under ``docs/`` and is outside every lint /
type / coverage gate by design (kept ``py_compile``-clean).

Usage
-----
    # On the operator's production box (SSM access = the turnkey path). The
    # date is TZ-pinned to IST so the .txt name matches the JSONL transcript's
    # IST-stamped filename on a non-IST box:
    python docs/groww/probe/postclose_settle_probe.py \
        | tee docs/groww/probe/postclose-$(TZ=Asia/Kolkata date +%F)-$(TZ=Asia/Kolkata date +%H%M).txt

    # After >= 5 probe days, build the slot-by-check settle matrix:
    python docs/groww/probe/postclose_settle_probe.py --summarize docs/groww/probe/

Exit code 0 = the probe ran to completion (per-symbol errors are recorded
inline); 2 = the probe could not START (bad args / SDK / SSM / credentials).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import sys
import urllib.request
from collections.abc import Sequence
from pathlib import Path

#: Deterministic CASH-pool seed — the SAME 10 names across all five slots AND
#: all probe days, so the 5-day settle-time matrix is a controlled same-symbol
#: comparison (override breadth via --seed / --symbols).
_DEFAULT_SEED = "postclose-settle-v1"
_CASH_COUNT = 10

#: The three ENGINE indices — always probed.
_INDEX_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("NSE-NIFTY", "NSE"),
    ("NSE-BANKNIFTY", "NSE"),
    ("BSE-SENSEX", "BSE"),
)

#: The canonical probe slots (IST wall-clock) the runbook schedules.
_SLOTS = ("15:35", "15:45", "16:00", "16:30", "17:30")

_INSTRUMENT_CSV_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"

#: The per-slot checks the --summarize matrix folds (per record).
_CHECKS = ("rows_all_len7", "count_ok", "last_bar_ok", "fut_fields_ok")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out-dir",
        default="docs/groww/probe",
        help="directory for the timestamped JSONL transcript (default: docs/groww/probe)",
    )
    parser.add_argument(
        "--seed",
        default=_DEFAULT_SEED,
        help=f"CASH-pool sampling seed (default {_DEFAULT_SEED!r} — same 10 names every run)",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="comma-separated CASH groww_symbols overriding the sampled pool",
    )
    parser.add_argument(
        "--summarize",
        default=None,
        metavar="DIR",
        help="read every postclose-*.jsonl in DIR and print the slot-by-check settle matrix",
    )
    return parser.parse_args(argv)


def _nearest_slot(hhmm: str) -> str:
    """Snap an IST HH:MM to the nearest canonical slot label (else verbatim)."""
    minutes = int(hhmm[:2]) * 60 + int(hhmm[3:])
    best, best_delta = hhmm, 16  # snap only within 15 minutes
    for slot in _SLOTS:
        slot_minutes = int(slot[:2]) * 60 + int(slot[3:])
        delta = abs(minutes - slot_minutes)
        if delta < best_delta:
            best, best_delta = slot, delta
    return best


def _fno_eligible_cash_pool() -> list[str]:
    """NSE CASH EQ groww_symbols whose underlying carries F&O legs — sorted."""
    with urllib.request.urlopen(_INSTRUMENT_CSV_URL, timeout=60) as resp:  # noqa: S310
        text = resp.read().decode("utf-8", errors="replace")
    eligible: set[str] = set()
    cash_rows: list[tuple[str, str]] = []  # (trading_symbol, groww_symbol)
    for row in csv.DictReader(io.StringIO(text)):
        itype = (row.get("instrument_type") or "").strip().upper()
        if itype in {"FUT", "CE", "PE"}:
            underlying = (row.get("underlying_symbol") or "").strip()
            if underlying:
                eligible.add(underlying)
        elif (
            itype == "EQ"
            and (row.get("exchange") or "").strip().upper() == "NSE"
            and (row.get("segment") or "").strip().upper() == "CASH"
        ):
            trading = (row.get("trading_symbol") or "").strip()
            groww = (row.get("groww_symbol") or "").strip()
            if trading and groww:
                cash_rows.append((trading, groww))
    return sorted({groww for trading, groww in cash_rows if trading in eligible})


def _summarize(directory: Path) -> int:
    """Fold every ``postclose-*.jsonl`` transcript into the slot × check matrix."""
    records: list[dict[str, object]] = []
    bad_lines = 0
    for path in sorted(directory.glob("postclose-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            # Per-line tolerance (round-1 R3-MINOR-2): a truncated tail line (a
            # killed probe) or a stray non-dict JSON line must never crash the
            # matrix — skip + count, keep exit 0. Both crash shapes were
            # refuter-reproduced (JSONDecodeError and a bare `42` line).
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            if not isinstance(row, dict):
                bad_lines += 1
                continue
            if "slot_summary" not in row:
                records.append(row)
    if bad_lines:
        print(f"{bad_lines} unreadable line(s) skipped")
    if not records:
        print(f"no postclose-*.jsonl records under {directory}")
        return 0
    by_slot: dict[str, list[dict[str, object]]] = {}
    for row in records:
        by_slot.setdefault(str(row.get("slot")), []).append(row)

    def _check(row: dict[str, object], name: str) -> bool:
        if row.get("error"):
            return False
        if name == "rows_all_len7":
            return row.get("rows_len7") == row.get("raw_rows")
        if name == "count_ok":
            return bool(row.get("count_ok"))
        if name == "last_bar_ok":
            return bool(row.get("last_bar_is_1529"))
        # fut_fields_ok — vacuously true for non-FUT records.
        if row.get("kind") != "FUT":
            return True
        return bool(row.get("fut_volume_nonnull")) and bool(row.get("fut_oi_nonnull"))

    print("slot   " + "".join(f"{c:>16}" for c in _CHECKS) + "   records  days")
    ordered = sorted(by_slot, key=lambda s: (s not in _SLOTS, s))
    earliest_all_pass: str | None = None
    for slot in ordered:
        rows = by_slot[slot]
        days = len({str(r.get("probe_ts_ist", ""))[:10] for r in rows})
        cells: list[str] = []
        all_pass = True
        for check in _CHECKS:
            passed = sum(1 for r in rows if _check(r, check))
            cells.append(f"{passed}/{len(rows)}".rjust(16))
            if passed != len(rows):
                all_pass = False
        print(f"{slot:<7}" + "".join(cells) + f"   {len(rows):>7}  {days:>4}")
        if all_pass and earliest_all_pass is None and slot in _SLOTS:
            earliest_all_pass = slot
    if earliest_all_pass is not None:
        print(
            f"\nearliest slot where EVERY check passed on EVERY observed record: "
            f"{earliest_all_pass} IST — schedule the nightly at/after this + margin"
        )
    else:
        print("\nno slot has every check passing yet — keep probing (or inspect the errors)")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.summarize is not None:
        return _summarize(Path(args.summarize))

    # ---- lazy brutex imports (the --help path never needs the package) ----
    try:
        from datetime import UTC, datetime, time, timedelta
        from zoneinfo import ZoneInfo

        from brutex.adapters.groww.aws_client import build_real_aws_client
        from brutex.adapters.groww.candles import fetch_candle_windows
        from brutex.adapters.groww.constants import (
            GROWW_RATE_LIMIT_PER_MIN,
            GROWW_RATE_LIMIT_RPS_CEILING,
            resolve_token_broker_region,
        )
        from brutex.adapters.groww.contracts import resolve_contracts, resolve_expiries
        from brutex.adapters.groww.live import (
            build_live_client,
            mint_blocked_token_factory,
            mint_blocked_totp_minter,
        )
        from brutex.adapters.groww.reauth import _real_sdk_factory
        from brutex.adapters.groww.token_cache import CachedToken, current_session
        from brutex.adapters.groww.token_store_aws import build_readonly_token_store_with
        from brutex.adapters.rate_common import RateController
        from brutex.calendars.market_hours import guard_active_default, pull_allowed_default
        from brutex.calendars.nse import get_session, has_muhurat_session, now_ist
        from brutex.adapters.groww.auth import GrowwCredentials
        from brutex.config.credentials_aws import load_groww_aws_credentials
        from brutex.data.session import IST
    except Exception as exc:  # noqa: BLE001 — a start failure must name itself
        print(f"CANNOT START: brutex import failed: {type(exc).__name__}: {exc}")
        return 2

    # ---- market-hours guard (DEC-GROWW-MARKET-HOURS-GUARD-001) ----
    # The probe is a VENDOR-calling surface like any other: refuse to run inside
    # the NSE trading block [08:45, 15:35) IST on a trading day, so a
    # mis-scheduled cron / a manual mid-session run can never compete with
    # TickVault's live feed on the shared account. The block is HALF-OPEN, so
    # the first runbook slot (15:35:00 sharp) is ALLOWED; weekends/holidays are
    # allowed at any hour. The check runs BEFORE any client build / vendor call.
    if guard_active_default() and not pull_allowed_default():
        print(
            "CANNOT START: inside the NSE market-hours block [08:45, 15:35) IST on a "
            "trading day — the shared Groww account belongs to TickVault's live feed "
            "in this window (DEC-GROWW-MARKET-HOURS-GUARD-001). Run at/after 15:35 "
            "IST (the runbook slots), or set BRUTEX_MARKET_HOURS_GUARD=0 to override "
            "deliberately."
        )
        return 2

    # ---- the read-only doctor-kit client (mint physically blocked, paced) ----
    try:
        aws = build_real_aws_client(resolve_token_broker_region())
        file_creds = load_groww_aws_credentials(client=aws)
        creds = GrowwCredentials(
            api_key=file_creds.api_key, totp_secret=file_creds.totp_secret
        )
        store = build_readonly_token_store_with(aws)
        cached, miss = store.load_with_reason(now_utc=datetime.now(UTC))
        if cached is None:
            print(f"CANNOT START: no read-only SSM token ({miss})")
            return 2
        token = CachedToken(
            access_token=cached.access_token,
            issued_for_session=current_session(datetime.now(UTC)),
        )
        controller = RateController(
            ceiling_rps=GROWW_RATE_LIMIT_RPS_CEILING,
            ceiling_per_min=GROWW_RATE_LIMIT_PER_MIN,
        )

        def _load(*_args: object, **_kwargs: object) -> CachedToken | None:
            return token

        def _store(*_args: object, **_kwargs: object) -> bool:
            return True  # NEVER persists a token (the doctor-kit posture)

        client = build_live_client(
            creds,
            token_factory=mint_blocked_token_factory,
            totp_minter=mint_blocked_totp_minter,
            sdk_factory=_real_sdk_factory,
            pace=controller.pace,
            on_rate_limit=controller.on_rate_limit,
            on_success=controller.on_success,
            token_cache_load=_load,
            token_cache_store=_store,
            readonly_reread=_load,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"CANNOT START: read-only client build failed: {type(exc).__name__}: {exc}")
        return 2

    probe_now = now_ist()
    today = probe_now.date()
    slot = _nearest_slot(f"{probe_now:%H:%M}")
    session = get_session(today)
    if session is None:
        print(f"NON-TRADING DAY? {today} has no NSE session — records will read expected 0")
    if has_muhurat_session(today):
        print(f"MUHURAT DAY {today} — the runbook says SKIP this day's probe")
    expected = 0 if session is None else session.duration_minutes
    close_dt = (
        None
        if session is None
        else datetime.combine(today, session.close_time_ist, tzinfo=ZoneInfo("Asia/Kolkata"))
    )

    # ---- the symbol plan: 3 indices + 10 seeded CASH + front-month NIFTY FUT ----
    plan: list[tuple[str, str, str, str]] = [  # (symbol, kind, exchange, segment)
        (symbol, "INDEX", exchange, "CASH") for symbol, exchange in _INDEX_SYMBOLS
    ]
    if args.symbols:
        cash = [s.strip() for s in str(args.symbols).split(",") if s.strip()]
    else:
        try:
            pool = _fno_eligible_cash_pool()
            cash = random.Random(str(args.seed)).sample(pool, min(_CASH_COUNT, len(pool)))
        except Exception as exc:  # noqa: BLE001 — degrade to indices+FUT, recorded
            print(f"instrument-master CSV unavailable ({exc}) — probing indices + FUT only")
            cash = []
    if cash:
        # Sample-drift honesty (R4-MINOR-1): the seed fixes the SAMPLING, not the
        # POOL — the instrument master is re-downloaded per run, so a listing /
        # eligibility change drifts the pool and therefore the sample. Print the
        # chosen names so the operator can PIN them for the remaining days.
        print(f"CASH sample ({len(cash)}): {','.join(sorted(cash))}")
        print("  (after day 1, pin the sample: re-run with --symbols <the list above>)")
    plan.extend((symbol, "CASH", "NSE", "CASH") for symbol in sorted(cash))
    try:
        expiries = [e for e in resolve_expiries(client, exchange="NSE", underlying_symbol="NIFTY") if e >= today]
        front = min(expiries)
        futs = [
            c
            for c in resolve_contracts(
                client, exchange="NSE", underlying_symbol="NIFTY", expiry=front
            )
            if c.contract_type == "FUT"
        ]
        plan.extend((c.symbol, "FUT", "NSE", "FNO") for c in futs[:1])
    except Exception as exc:  # noqa: BLE001
        print(f"front-month NIFTY FUT resolution failed ({exc}) — recorded, continuing")

    # ---- probe every symbol: raw shape via on_raw_payload + parsed criteria ----
    records: list[dict[str, object]] = []
    for symbol, kind, exchange, segment in plan:
        raw_stats = {"raw_rows": 0, "rows_len6": 0, "rows_len7": 0, "null_volume": 0, "null_oi": 0}

        def _hook(
            payload: object,
            _interval: str,
            _start: str,
            _end: str,
            _issued: str,
            _stats: dict[str, int] = raw_stats,
        ) -> None:
            rows = payload.get("candles") if isinstance(payload, dict) else None
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, (list, tuple)):
                    continue
                _stats["raw_rows"] += 1
                if len(row) == 6:
                    _stats["rows_len6"] += 1
                elif len(row) == 7:
                    _stats["rows_len7"] += 1
                if len(row) >= 6 and row[5] is None:
                    _stats["null_volume"] += 1
                if len(row) >= 7 and row[6] is None:
                    _stats["null_oi"] += 1

        record: dict[str, object] = {
            "probe_ts_ist": now_ist().isoformat(),
            "slot": slot,
            "symbol": symbol,
            "kind": kind,
            "exchange": exchange,
            "segment": segment,
            "expected_minutes": expected,
            "error": None,
        }
        try:
            bars = fetch_candle_windows(
                client,
                exchange=exchange,
                segment=segment,
                groww_symbol=symbol,
                interval="1minute",
                start=today,
                end=today + timedelta(days=1),
                drop_volume=(kind == "INDEX"),
                on_raw_payload=_hook,
            )
            today_bars = [b for b in bars if b.timestamp.astimezone(IST).date() == today]
            minutes = {
                b.timestamp.astimezone(IST).replace(second=0, microsecond=0) for b in today_bars
            }
            last = max(minutes) if minutes else None
            record.update(
                {
                    "raw_rows": raw_stats["raw_rows"],
                    "rows_len6": raw_stats["rows_len6"],
                    "rows_len7": raw_stats["rows_len7"],
                    "raw_null_volume": raw_stats["null_volume"],
                    "raw_null_oi": raw_stats["null_oi"],
                    "parsed_bars": len(today_bars),
                    "count_ok": len(minutes) == expected and expected > 0,
                    "first_bar_ist": f"{min(minutes):%H:%M}" if minutes else None,
                    "last_bar_ist": f"{last:%H:%M}" if last else None,
                    # The close-edge law (last stored bar == close - 1 minute;
                    # 15:29 on a regular day — session-aware, never hardcoded).
                    "last_bar_is_1529": (
                        last is not None
                        and close_dt is not None
                        and last + timedelta(minutes=1) >= close_dt
                    ),
                    "fut_volume_nonnull": (
                        kind == "FUT" and raw_stats["raw_rows"] > 0 and raw_stats["null_volume"] == 0
                    ),
                    "fut_oi_nonnull": (
                        kind == "FUT"
                        and raw_stats["raw_rows"] > 0
                        and raw_stats["rows_len7"] == raw_stats["raw_rows"]
                        and raw_stats["null_oi"] == 0
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001 — per-symbol errors recorded, probe continues
            record["error"] = f"{type(exc).__name__}: {exc}"
        records.append(record)

    settled = sum(
        1
        for r in records
        if not r.get("error")
        and r.get("rows_len7") == r.get("raw_rows")
        and r.get("count_ok")
        and r.get("last_bar_is_1529")
    )
    trailer = {
        "slot_summary": slot,
        "probe_ts_ist": now_ist().isoformat(),
        "symbols_probed": len(records),
        "errors": sum(1 for r in records if r.get("error")),
        "settled_records": settled,
        "note": f"settled at this slot: {settled}/{len(records)}",
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"postclose-{today.isoformat()}-{probe_now:%H%M}.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for row in [*records, trailer]:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    # ---- the human table (tee this stdout into the .txt transcript) ----
    print(f"\npost-close settle probe — {today} slot {slot} IST (expected minutes: {expected})")
    header = (
        f"{'symbol':<28}{'kind':<7}{'raw':>5}{'len6':>6}{'len7':>6}"
        f"{'nullV':>7}{'nullOI':>7}{'bars':>6}{'count':>7}{'last':>7}{'edge':>6}  error"
    )
    print(header)
    for r in records:
        print(
            f"{str(r['symbol']):<28}{str(r['kind']):<7}"
            f"{str(r.get('raw_rows', '-')):>5}{str(r.get('rows_len6', '-')):>6}"
            f"{str(r.get('rows_len7', '-')):>6}{str(r.get('raw_null_volume', '-')):>7}"
            f"{str(r.get('raw_null_oi', '-')):>7}{str(r.get('parsed_bars', '-')):>6}"
            f"{('OK' if r.get('count_ok') else 'short'):>7}"
            f"{str(r.get('last_bar_ist') or '-'):>7}"
            f"{('OK' if r.get('last_bar_is_1529') else 'no'):>6}  {r.get('error') or ''}"
        )
    print(f"\n{trailer['note']}  (transcript: {out_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
