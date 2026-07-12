# Post-close settle probe — measuring Groww's EOD settlement time

**Decision:** DEC-GROWW-POSTCLOSE-SETTLE-PROBE-001 (MIGRATION.md #1752; companion of
DEC-PULL-TODAY-COMPLETENESS-GATE-001, MIGRATION.md #1751).

## The open question

Groww's end-of-day settlement time is **UNDOCUMENTED**. The vendor docs
(`docs/groww/api-reference/03-backtesting.md` and the `docs/groww/trade-api-2026-07-03/`
snapshot) are silent on when the CURRENT trading day's 1-minute series stops being the
FORMING shape MIGRATION.md #837 (DEC-GROWW-FORMING-BAR-PARSE-001) recorded empirically:
6-element rows (no `open_interest` slot, `volume` typically `None`) "until the vendor's
end-of-day settlement fills them in". The pre-settle TODAY gate (#1751) defers a
covers-today window until the fetched day passes its per-class completeness criteria —
but NOBODY has measured **when** those criteria start passing after the 15:30 close.
This probe measures exactly that, per instrument class (INDEX / CASH / FUT), so the
nightly schedule can be grounded in evidence instead of a guess.

## Safety envelope

* **READ-ONLY calls only:** `get_historical_candles`, `get_expiries`, `get_contracts`,
  plus one unauthenticated instrument-master CSV download. ZERO lake writes, ZERO
  ledger settles — the script imports no sink/writer/ledger module (structurally
  read-only).
* **Shared-account safety:** all five slots run at/after **15:35 IST**, and the
  probe ENFORCES it — it refuses to start (exit 2) inside the NSE trading block
  on a trading day via the market-hours guard predicate
  (DEC-GROWW-MARKET-HOURS-GUARD-001; `BRUTEX_MARKET_HOURS_GUARD=0` is the
  deliberate operator override). The block is `[08:45, 15:35)` **half-open**
  (`brutex/calendars/market_hours.py` — `BLOCK_START`/`BLOCK_END`), so 15:35 itself is
  ALLOWED: the probe never competes with TickVault's live-feed window, by code
  and not just by convention. Weekends/holidays are allowed at any hour.
* **Credentials:** the doctor-kit read-only build — the api-key/TOTP-secret + the
  SHARED daily access token are read from AWS SSM (DEC-AWS-ONLY-CREDS-001), a token
  **mint is PHYSICALLY blocked** (`mint_blocked_token_factory`/`mint_blocked_totp_minter`),
  and every call rides the production AIMD controller + the 500/min governor
  (CLAUDE.md §2 Rule 6 — never an unpaced client). The operator's production box has
  SSM access — that IS the turnkey path; there is deliberately NO env-creds escape
  hatch (it would require a mint against the shared token; a cred-less machine keeps
  `scripts/groww_probe.py` as the documented Phase-0 flow).
* **Budget:** 3 indices + 10 CASH + expiries/contracts + 1 FUT ≈ 17 calls per slot
  ≈ 85 per day — trivially inside the 500/min governor, and the paced client enforces
  it regardless.

## Step-by-step

1. On ≥ **5 REGULAR trading days** (skip Muhurat days — the probe prints a banner;
   a weekend/holiday prints "NON-TRADING DAY?"), run at each of the five slots
   **15:35 / 15:45 / 16:00 / 16:30 / 17:30 IST**:

   ```bash
   python docs/groww/probe/postclose_settle_probe.py \
     | tee docs/groww/probe/postclose-$(TZ=Asia/Kolkata date +%F)-$(TZ=Asia/Kolkata date +%H%M).txt
   ```

   Each run writes the machine transcript
   `docs/groww/probe/postclose-<YYYY-MM-DD>-<HHMM>.jsonl` (one record per symbol +
   one `slot_summary` trailer) and prints the human table (tee it to the `.txt`).
   Commit both — the Phase-0 transcript flow.

2. The CASH pool is 10 F&O-eligible NSE stocks picked with the FIXED seed
   `postclose-settle-v1` over the sorted instrument-master pool, and the probe
   PRINTS the 10 chosen symbols at startup. HONEST caveat: the seed fixes the
   SAMPLING, not the POOL — the instrument master is re-downloaded per run, so a
   listing/eligibility change naturally drifts the pool and therefore the sample.
   **After day 1, pin the sample by re-running with `--symbols <the printed
   list>`** so the 5-day matrix stays a controlled same-symbol comparison.
   `--seed` overrides breadth (note the trade: a changed pool breaks
   day-over-day comparability).

3. After ≥ 5 probe days, build the settle matrix:

   ```bash
   python docs/groww/probe/postclose_settle_probe.py --summarize docs/groww/probe/
   ```

### Optional scheduling

Manual runs (the five slots, `tee`'d) are the simplest story and are what the
reference macOS box should start with; a launchd plist per slot
(`StartCalendarInterval` at the IST-equivalent local times — the
`docs/runbooks/backup-templates/` pattern) is the macOS automation. The cron
form below uses `CRON_TZ`, which is **Linux (cronie/vixie-cron) only** — macOS
launchd/cron does not honour it, so on a Mac either set the box's TZ to IST or
use launchd:

```cron
CRON_TZ=Asia/Kolkata
35 15 * * 1-5  cd /path/to/bruteX && python docs/groww/probe/postclose_settle_probe.py | tee -a docs/groww/probe/postclose-$(date +\%F)-1535.txt
45 15 * * 1-5  cd /path/to/bruteX && python docs/groww/probe/postclose_settle_probe.py | tee -a docs/groww/probe/postclose-$(date +\%F)-1545.txt
0  16 * * 1-5  cd /path/to/bruteX && python docs/groww/probe/postclose_settle_probe.py | tee -a docs/groww/probe/postclose-$(date +\%F)-1600.txt
30 16 * * 1-5  cd /path/to/bruteX && python docs/groww/probe/postclose_settle_probe.py | tee -a docs/groww/probe/postclose-$(date +\%F)-1630.txt
30 17 * * 1-5  cd /path/to/bruteX && python docs/groww/probe/postclose_settle_probe.py | tee -a docs/groww/probe/postclose-$(date +\%F)-1730.txt
```

(Tee filenames in the examples use `date` in the BOX's local TZ — on a non-IST
box prefix `TZ=Asia/Kolkata date ...` so the `.txt` name matches the JSONL
transcript's IST-stamped filename.)

## Interpreting the matrix

Per slot, the summary counts records passing each check:

| Check | Meaning |
|---|---|
| `rows_all_len7` | every RAW row was the settled 7-element shape (`rows_len7 == raw_rows`) |
| `count_ok` | distinct parsed minutes == the session's `duration_minutes` (375 regular / 60 Muhurat) |
| `last_bar_ok` | the last stored minute proves the close edge (`15:29` on a regular day) |
| `fut_fields_ok` | FUT records carry non-null volume + OI on every raw row |

**The earliest slot where ALL checks hold for EVERY record across EVERY probed day is
the measured settle time.** Schedule the nightly pull at/after that slot **plus
margin** (the 01:30 IST nightly is safely past any plausible answer; a same-evening
post-close pull should not start before the measured slot). Until the matrix exists,
nothing about the settle time is claimed (Golden Rule #1) — the #1751 gate simply
defers a still-forming day and re-pulls.

## Failure modes

* **Holiday/weekend:** the probe prints `NON-TRADING DAY?` and records expected 0 —
  discard that day's records (do not feed them to the matrix conclusion).
* **Muhurat day:** the probe prints a `MUHURAT DAY … SKIP` banner — skip it (the
  evening session's timing makes the five slots meaningless).
* **Per-symbol errors** (a 429 burst, a dead symbol) are recorded inline in the JSONL
  (`error` field) and the probe continues; a record with an error counts as FAILING
  every check in the matrix (honest — no evidence is not passing evidence).
* **CANNOT START (exit 2):** inside the market-hours block `[08:45, 15:35)` IST
  on a trading day (the probe's own guard — run at/after 15:35 IST, or
  `BRUTEX_MARKET_HOURS_GUARD=0` for a deliberate override) / bad args /
  `growwapi` missing / SSM unreachable / no read-only token — nothing was
  probed; fix the environment and re-run.
* **Operational note for the GATE (not the probe):** the #1751 TODAY gate is
  day-scoped — an evening pull must not be left spanning IST midnight against a
  still-unsettled day (a post-midnight round no longer covers day D, goes
  gate-inert for it, and keep-first would freeze a still-forming day D). The
  probe-measured settle time + margin is exactly what makes the nightly
  schedule safe.

## Operator heuristics — reading a day's table

* **Normal (pre-settle):** `rows_len6 > 0`, null volume/OI on FNO, possibly a short
  count at 15:35 that fills by a later slot — the expected forming shape; the #1751
  gate is deferring exactly this.
* **Vendor problem:** a LATE slot (17:30) still failing `count_ok`/`last_bar_ok` on
  the INDICES across multiple days — the vendor is not serving the full day at all;
  raise it, and expect the gate to defer to the next nightly (data lands there).
* **Calendar gap:** every symbol failing with expected 375 on a day the exchange did
  not actually trade a full session — check the NSE calendar / the observed-sessions
  overlay before blaming the vendor.

## Pre-fix lake remediation (already-stored partial days)

The #1751 gate protects pulls going FORWARD. A partial day stored under an
already-settled PRE-fix window is permanent under keep-first and is neither detected
nor repaired by the gate. Manual remediation:

1. **Detect** (DuckDB over the suspect series/month — still-settling FNO rows):

   ```sql
   SELECT count(*) FROM read_parquet('<lake>/bars/NSE/FNO/<contract>/1minute/<yyyy>/<mm>.parquet')
   WHERE volume = 0 AND open_interest IS NULL;
   ```

2. **Quarantine-copy + excise** the affected month file (copy the original under
   `<lake>/.quarantine/<ts>/` first — the DEC-LAKE-HEAL-001 forensic posture), then
   remove the affected IST day's rows (or the whole month file for a re-pull).
3. **Re-pull** the explicit date range (the window ledger row for the span must be
   re-opened / the month re-fetched via an explicit from/to pull).

Mechanising this audit is the named follow-up **DEC-LAKE-AUDIT-SETTLING-ROWS-001**
(not built here).
