# Groww SDK — Exceptions Reference

> **Source:** https://groww.in/trade-api/docs/python-sdk/exceptions
> **Captured:** 2026-04-14 (pasted verbatim by product owner)
> **SDK module:** `growwapi.groww.exceptions`
>
> **Purpose:** Authoritative project-local reference for the Groww Python
> SDK exception hierarchy. BruteX's adapter uses this to classify errors
> into "retry", "fail fast", and "halt run" buckets — see §3 below.
>
> If upstream changes, **update this file in the same PR** that updates
> the adapter. Do not let drift creep in.

---

## 1. Exception Hierarchy (verbatim)

All exceptions live in `growwapi.groww.exceptions`:

```
GrowwBaseException
├── GrowwAPIException                  (msg, code)
│   ├── GrowwAPIAuthenticationException    (msg, code)
│   ├── GrowwAPIAuthorisationException     (msg, code)   ← British spelling
│   ├── GrowwAPIBadRequestException        (msg, code)
│   ├── GrowwAPINotFoundException          (msg, code)
│   ├── GrowwAPIRateLimitException         (msg, code)  ⭐ CRITICAL
│   └── GrowwAPITimeoutException           (msg, code)
└── GrowwFeedException                 (msg)
    ├── GrowwFeedConnectionException       (msg)
    └── GrowwFeedNotSubscribedException    (msg, topic)
```

### Attribute inventory per exception class

| Exception | Attributes |
|---|---|
| `GrowwBaseException` | `msg: str` |
| `GrowwAPIException` (and all children) | `msg: str`, `code: str` |
| `GrowwFeedException` (and most children) | `msg: str` |
| `GrowwFeedNotSubscribedException` | `msg: str`, `topic: str` |

---

## 2. Per-Exception Meaning (verbatim from Groww docs)

### `GrowwBaseException`

> This is the base class for all exceptions in the Groww SDK. It captures
> the general error message.
>
> Expect this exception as a generic catch-all for errors that do not
> fall into more specific categories.

### `GrowwAPIException`

> This exception is raised for client-related errors, such as invalid
> requests or authentication failures.

### `GrowwAPIAuthenticationException`

> This exception is raised when authentication with the Groww API fails.
>
> Expect this exception to handle scenarios where the SDK fails to
> authenticate with the Groww API, indicating issues with the API key
> or authentication process.

### `GrowwAPIAuthorisationException` (British spelling)

> This exception is raised when authorization with the Groww API fails.
>
> Expect this exception to handle scenarios where the SDK fails to
> authorize with the Groww API, indicating issues with the API key or
> access permissions.

**⚠️ Spelling note:** Groww uses the British spelling `Authorisation`,
not `Authorization`. BruteX's adapter must reference the exact name.

### `GrowwAPIBadRequestException`

> This exception is raised when a bad request is made to the Groww API.
>
> Expect this exception to handle scenarios where the SDK sends a
> malformed request to the API, indicating issues with the request
> payload or parameters.

### `GrowwAPINotFoundException`

> This exception is raised when the requested resource is not found on
> the Groww API.
>
> Expect this exception to handle scenarios where the SDK requests a
> resource that does not exist, indicating a logical error in the code
> or an outdated reference.

### `GrowwAPIRateLimitException` ⭐ **CRITICAL for BruteX**

> This exception is raised when the rate limit for the Groww API is
> exceeded.
>
> Expect this exception to handle scenarios where the SDK makes too many
> requests to the API within a short period, indicating a need to
> throttle the request rate.

### `GrowwAPITimeoutException`

> This exception is raised when a request to the Groww API times out.
>
> Expect this exception to handle scenarios where the API request takes
> too long to respond, indicating potential network issues or server
> overload.

### `GrowwFeedException` + children

Out of scope for BruteX v1 — we do not use the live feed. Captured for
completeness.

---

## 3. BruteX Retry / Fail-Fast / Halt Classification

This table is the authoritative policy for `FR-ING-008` retry semantics.
Every Groww SDK call in BruteX's adapter must be wrapped in a handler
that classifies exceptions into one of three buckets:

| Groww exception | Bucket | Action | Why |
|---|---|---|---|
| `GrowwAPIRateLimitException` | **RETRY** | Exponential backoff (2s → 4s → 8s → 16s) with ±20% jitter; after 4 failures, mark chunk `rate_limit_exhausted` and halt the run to alert the operator | Transient — tightening the token bucket may have been insufficient |
| `GrowwAPITimeoutException` | **RETRY** | Same backoff ladder; after 4 failures, mark chunk `timeout_exhausted` and move to the next | Network / server overload, typically transient |
| `GrowwAPIException` (generic, not a subclass) | **RETRY once** | One retry after 5s; if still failing, fail the chunk and move on | Treated as "unknown error" — could be 5xx wrapped by SDK |
| `GrowwBaseException` (generic base) | **RETRY once** | Same as above | Catch-all |
| `GrowwAPIAuthenticationException` | **HALT RUN** | Do NOT retry. Stop the entire ingest run. Alert operator. | Token is invalid or expired — retrying wastes calls and can trigger account lockout |
| `GrowwAPIAuthorisationException` | **HALT RUN** | Do NOT retry. Stop the entire ingest run. Alert operator. | Token lacks permission — retrying cannot help |
| `GrowwAPIBadRequestException` | **FAIL FAST** | Do NOT retry this chunk. Log + mark `permanently_failed`. Continue to next chunk. | Bug in our adapter code (wrong params) — re-running is pointless until code is fixed |
| `GrowwAPINotFoundException` | **FAIL FAST** | Do NOT retry. Log + mark `not_found`. Continue to next chunk. | Symbol does not exist — common for edge cases like expired-and-delisted contracts. Expected, not an error |

### Why "HALT RUN" is stronger than "FAIL FAST"

- **FAIL FAST:** skip this one chunk, move on to the next. Run continues.
- **HALT RUN:** stop the entire ingest job immediately. Partial state is
  preserved; operator must investigate.

Authentication / authorization failures warrant HALT because:

1. Retrying them wastes rate-limit budget we'll need when the token is fixed
2. Continuing to hammer the API with a bad token can trigger Groww's
   account-level lockout (which affects live trading too if the same
   token is used there)
3. Every subsequent call will fail identically — continuing is just slow
   abort with extra steps

---

## 4. Python implementation skeleton for the adapter

```python
# brutex/adapters/groww/retry.py (illustrative, not yet written)

import time
from typing import Callable, TypeVar

from growwapi.groww.exceptions import (
    GrowwBaseException,
    GrowwAPIException,
    GrowwAPIAuthenticationException,
    GrowwAPIAuthorisationException,
    GrowwAPIBadRequestException,
    GrowwAPINotFoundException,
    GrowwAPIRateLimitException,
    GrowwAPITimeoutException,
)

T = TypeVar("T")

RETRY_BACKOFF_SECONDS = [2, 4, 8, 16]  # exponential
GENERIC_RETRY_DELAY = 5


class IngestHaltSignal(Exception):
    """Raised to terminate the entire ingest run."""


class ChunkPermanentFailure(Exception):
    """Raised to skip the current chunk but continue the run."""


def call_with_retry(
    fn: Callable[..., T],
    *args,
    _run_id: str,
    _chunk_id: str,
    **kwargs,
) -> T:
    """
    Wrap every Groww SDK call in this. Classifies exceptions per
    docs/groww-sdk/exceptions.md §3.
    """
    for attempt, delay in enumerate([0, *RETRY_BACKOFF_SECONDS]):
        if delay:
            time.sleep(delay + random_jitter(delay, pct=0.2))
        try:
            return fn(*args, **kwargs)

        # HALT RUN — do not retry, terminate ingest
        except GrowwAPIAuthenticationException as e:
            audit_log("halt_auth", _run_id, _chunk_id, e)
            raise IngestHaltSignal(
                f"Authentication failed: {e.msg} (code={e.code}). "
                f"Fix GROWW_API_AUTH_TOKEN and re-run."
            ) from e
        except GrowwAPIAuthorisationException as e:
            audit_log("halt_authz", _run_id, _chunk_id, e)
            raise IngestHaltSignal(
                f"Authorization failed: {e.msg} (code={e.code}). "
                f"Token lacks required permissions. Re-issue token."
            ) from e

        # FAIL FAST — skip this chunk, continue run
        except GrowwAPIBadRequestException as e:
            audit_log("fail_bad_request", _run_id, _chunk_id, e)
            raise ChunkPermanentFailure(
                f"Bad request: {e.msg} (code={e.code}). "
                f"Adapter bug — inspect chunk inputs."
            ) from e
        except GrowwAPINotFoundException as e:
            audit_log("fail_not_found", _run_id, _chunk_id, e)
            raise ChunkPermanentFailure(
                f"Resource not found: {e.msg} (code={e.code}). "
                f"Expected for delisted/invalid symbols."
            ) from e

        # RETRY — transient
        except GrowwAPIRateLimitException as e:
            audit_log("retry_rate_limit", _run_id, _chunk_id, e, attempt=attempt)
            if attempt >= len(RETRY_BACKOFF_SECONDS):
                audit_log("exhaust_rate_limit", _run_id, _chunk_id, e)
                raise IngestHaltSignal(
                    f"Rate limit exhausted after {attempt} retries. "
                    f"Lower BRUTEX_OHLC_RATE_LIMIT_RPS and re-run."
                ) from e
            continue
        except GrowwAPITimeoutException as e:
            audit_log("retry_timeout", _run_id, _chunk_id, e, attempt=attempt)
            if attempt >= len(RETRY_BACKOFF_SECONDS):
                raise ChunkPermanentFailure(
                    f"Timeout exhausted after {attempt} retries."
                ) from e
            continue

        # Generic API / base — one-shot retry
        except (GrowwAPIException, GrowwBaseException) as e:
            audit_log("retry_generic", _run_id, _chunk_id, e, attempt=attempt)
            if attempt >= 1:  # only retry once for unknown
                raise ChunkPermanentFailure(
                    f"Unknown Groww error: {type(e).__name__}: {e}"
                ) from e
            time.sleep(GENERIC_RETRY_DELAY)
            continue

    # Should be unreachable
    raise RuntimeError("call_with_retry fell through without return or raise")
```

### Behavioural contract for every call in the adapter

1. The Groww SDK is **only** called via `call_with_retry`
2. No bare `try/except Exception:` anywhere — only the specific Groww types
3. Every exception path writes exactly one audit-log entry before raising
4. `IngestHaltSignal` bubbles up to the ingest CLI which writes `run.state=halted` and exits non-zero
5. `ChunkPermanentFailure` is caught by the per-chunk worker, which marks
   the chunk and continues with the next one

---

## 5. Integration with BruteX audit chain

Every exception event appends one entry to the hash-chained audit log
(per `NFR-OBS-003`). The `action` field is one of:

| Action | Meaning |
|---|---|
| `halt_auth` | Authentication failed — run halted |
| `halt_authz` | Authorization failed — run halted |
| `exhaust_rate_limit` | Rate limit retries exhausted — run halted |
| `retry_rate_limit` | Rate limit transient — retry N |
| `retry_timeout` | Timeout transient — retry N |
| `retry_generic` | Unknown Groww error — one-shot retry |
| `fail_bad_request` | Malformed request — chunk permanently failed |
| `fail_not_found` | Resource not found — chunk permanently failed (expected for delisted) |

`brutex audit verify` recomputes the entire chain; any break → exit
non-zero → CI fails.

---

## 6. Unknown behaviours to verify empirically in P1c

These questions are not answered by the Groww docs and must be probed:

| Question | Resolution |
|---|---|
| Does `GrowwAPIRateLimitException.code` contain a `Retry-After` value? | Probe: inspect the exception object when a deliberate 429 is triggered |
| Does `GrowwAPIException` (generic) wrap HTTP 5xx, or are 5xx surfaced differently? | Probe: simulate 5xx via network disruption |
| Is the rate-limit bucket per-token, per-IP, or per-account? | Probe: run from 2 machines simultaneously with the same token |
| What's the exact HTTP status code (or lack thereof) that triggers each exception type? | Probe: monkey-patch the SDK's internal HTTP client to log raw responses |

All four are tracked as `OQ-GROWW-*` items in `REQUIREMENTS.md §26`.

---

## 7. Feed exceptions — explicitly out of scope

`GrowwFeedException`, `GrowwFeedConnectionException`, and
`GrowwFeedNotSubscribedException` are only raised by the live data feed.
BruteX v1 does not use the feed — only `get_expiries`, `get_contracts`,
`get_historical_candles`, and the instrument CSV methods. These
exceptions are captured here for reference only.

If v1.2's paper-mode `LiveFeed` is built on top of the Groww feed, this
section will be expanded.

---

## 8. Changelog

| Date | Change | Commit |
|---|---|---|
| 2026-04-14 | Initial capture of Groww Exceptions page | `(this commit)` |

---

_End of `docs/groww-sdk/exceptions.md`_
