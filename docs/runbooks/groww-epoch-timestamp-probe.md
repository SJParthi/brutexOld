# Runbook — Groww epoch-timestamp semantics probe (close the UNVERIFIED gap)

**Decision:** DEC-GROWW-EPOCH-VERDICT-001 (follow-up to
DEC-INGEST-BOUNDARY-INTEGRITY-001 (b), MIGRATION.md #1294).

## The open question

Groww documents candle timestamps as **native-IST strings**
(`"yyyy-MM-ddTHH:mm:ss"`, `docs/groww/api-reference/03-backtesting.md`). The
parser (`brutex/adapters/groww/parser.py::parse_timestamp_ist`) also carries a
DEFENSIVE branch for **numeric epoch seconds**, which it interprets as
**Unix-UTC**. That interpretation is **UNVERIFIED** — no probe transcript
exists. If Groww ever sent **IST-naive** epoch seconds (seconds of IST wall
clock since `1970-01-01T00:00:00`), every bar stored through the epoch branch
would silently shift by **+05:30**.

The branch has never been observed to fire in production (production payloads
are strings). It is instrumented LOUDLY anyway:

- `groww.parse.epoch_timestamp` — ONE WARNING per pull the moment the branch
  fires (DEC-INGEST-BOUNDARY-INTEGRITY-001 (b)).
- `groww.parse.epoch_timestamp_verdict` — ONE WARNING per parsed window
  payload carrying numeric timestamps, with the **deterministic
  session-plausibility verdict** below (DEC-GROWW-EPOCH-VERDICT-001).

## The deterministic cross-check (what runs automatically)

`brutex/adapters/groww/epoch_verdict.py::classify_epoch_timestamps` classifies
each numeric timestamp against BOTH candidate interpretations using the real
NSE session calendar (`brutex.calendars.nse.get_session_checked` —
Muhurat/special-weekend aware; out-of-coverage dates ABSTAIN):

| verdict | meaning | action |
|---|---|---|
| `unix_utc_consistent` | every directional sample fits the current assumption | log the transcript; still run the live probe below to VERIFY |
| `ist_naive_consistent` | every directional sample fits the FEARED IST-naive reading | **STOP trusting the affected windows** — quarantine/re-pull after the live probe settles the semantics; bars parsed from this payload are likely shifted +05:30 |
| `contradictory` | samples point both ways | vendor payload is internally inconsistent — escalate to the vendor with the transcript |
| `no_evidence` | all samples ambiguous / out-of-session / abstained / invalid | no conclusion; run the live probe |

**Honesty (Golden Rule #1):** the verdict is *evidence*, not verification.
In-session plausibility cannot prove vendor intent — most intraday instants
discriminate the two readings (they differ by exactly +05:30), but a sample in
the ~45-minute overlap (wall 14:45–15:30 under Unix-UTC) is ambiguous, and
non-session data (if the vendor ever returned any) would read `out_of_session`.
Only a live probe transcript closes the UNVERIFIED stamp.

## The live probe (what an operator must run to VERIFY)

Prerequisites: live Groww credentials (AWS SSM, see
`docs/runbooks/aws-token-broker.md`), a machine with vendor egress.

1. **Pick a known regular session day** within holiday coverage (e.g. a recent
   mid-week trading day; avoid Muhurat/special sessions for the primary probe).
2. **Fetch one 1-minute window** for `NSE-NIFTY` over that day via the SDK
   (`get_historical_candles`), and capture the **raw payload**
   (`{"candles": [[ts, ...], ...]}`) verbatim to a transcript file — before any
   BruteX parsing.
3. **Inspect the timestamp type**:
   - Strings → the documented form; the epoch branch stays defensive-only.
     Record the transcript and close the probe: "vendor sends strings".
   - Numerics → continue.
4. **Cross-check deterministically** (offline, no further vendor calls):

   ```python
   from brutex.adapters.groww.epoch_verdict import classify_epoch_timestamps
   verdict = classify_epoch_timestamps([row[0] for row in payload["candles"]])
   print(verdict)
   ```

5. **Anchor against a known bar**: the first bar of a regular session must be
   `09:15` IST. Compute both interpretations of the first timestamp:
   - `datetime.fromtimestamp(ts, tz=UTC).astimezone(IST)` — Unix-UTC reading;
   - `datetime.fromtimestamp(ts, tz=UTC).replace(tzinfo=None)` read as IST
     wall — IST-naive reading.
   Exactly one should land `09:15` on the probed date. That, plus the verdict,
   plus (if available) a same-window fetch that returns the string form for
   comparison, is the verification transcript.
6. **Land the result**: commit the transcript under `docs/groww/probe/`, add a
   MIGRATION.md entry superseding the UNVERIFIED stamp, and update
   `parse_timestamp_ist`'s docstring (and, ONLY if the verdict was IST-naive,
   fix the branch in a dedicated reviewed PR — never as a side effect).

## Why this could not be closed here

This environment has no vendor egress and no live credentials (the same
envelope as the ETC-regime fetch round, CLAUDE.md header): a live probe is
impossible, so this change ships the deterministic cross-check + this runbook
and leaves the semantics honestly UNVERIFIED. **Parsing behaviour is
unchanged.**
