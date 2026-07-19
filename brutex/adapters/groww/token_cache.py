"""On-disk daily Groww access-token cache — mint ONCE per session per machine.

DEC-GROWW-TOKEN-DISK-CACHE-001. A fresh-clone dashboard "Pull · full pipeline" run
re-mints a brand-new Groww access token on EVERY live-client/pool construction, and a
single pipeline builds three independent refreshers (spot + metadata + F&O), so ≥3
uncoordinated mints fire in seconds and trip the vendor's token-ISSUANCE rate limit
(the operator-reported failure, MIGRATION.md entry #1041). The minted token previously
lived ONLY in :attr:`SdkRefresher._sdks` (in-memory, per-instance), so a restart /
second phase / concurrent process always re-minted from zero.

This module is the SINGLE source of truth (CLAUDE.md §11 COMMON) for PERSISTING and
RE-READING the daily token, so the first phase mints + writes the cache and every later
phase / cold start / concurrent process READS it — collapsing the self-inflicted storm
to ONE real mint per session per machine.

Design (CLAUDE.md §11 COMMON · Golden Rule #10):

* **Pure + stdlib-only · NO SDK import · NO import cycle.** It imports nothing from
  :mod:`brutex.adapters.groww.live` / ``client`` / ``reauth`` (the consumers), and reuses
  the EXISTING :func:`~brutex.adapters.ingest_common.now_ist` IST notion rather than
  inventing a second one (hostile-review m1 fold).
* **Best-effort, NEVER raises.** :func:`load_cached_token` returns ``None`` on ANY
  failure (missing / not-0600 / corrupt JSON / wrong schema / wrong-fingerprint /
  stale-session) and logs the reason; :func:`store_token` logs + returns on a write
  failure. A cache hiccup can therefore NEVER break or block a pull — an uncacheable
  token still works in-process.
* **Session-anchored at the 06:00 IST reset** (``GROWW_TOKEN_RESET_HOUR_IST``): a token
  is valid only for ITS trading session, so a token from a previous session — or one
  minted before today's 06:00 reset — is a cache-MISS that forces a fresh mint.
* **Fingerprinted, NOT the key.** The cache stores ``blake3(api_key)[:16]``, never the
  api_key; a cache minted under a DIFFERENT api_key (credential rotation) is a fingerprint
  mismatch ⇒ ignored.

**Honest envelope (Golden Rule #10):** the daily token is stored PLAINTEXT at mode 0600
under ``~/.brutex/cache/`` — no more sensitive than the account it authenticates — so it
does NOT widen the single-user threat model, but it is NOT encrypted at rest (OS-keyring /
age is an explicit follow-up, out of scope). A
0600 token leak is bounded to ≤1 trading day (the 06:00 reset). The cache is PER-MACHINE,
so a Groww-side ACCOUNT-WIDE issuance limit shared across many hosts is NOT solved here
(that needs a networked token broker — out of scope, not claimed). This eliminates the
self-inflicted 3×/run storm ONCE the first mint succeeds; a throttle on the genuinely-FIRST
mint of a fresh clone is rideable (bounded back-off → quarantine → across-pull re-pull),
NOT eliminated.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Final

import blake3
import structlog

from brutex.adapters.groww.auth import GrowwCredentials
from brutex.adapters.groww.constants import (
    GROWW_TOKEN_CACHE_FILENAME,
    GROWW_TOKEN_CACHE_PATH_ENV,
    GROWW_TOKEN_CACHE_SCHEMA,
    GROWW_TOKEN_RESET_HOUR_ENV,
    GROWW_TOKEN_RESET_HOUR_IST,
)
from brutex.adapters.ingest_common import now_ist
from brutex.data.atomic_io import atomic_write_simple

_LOG = structlog.get_logger("brutex.adapters.groww.token_cache")

#: Length (hex chars) of the api_key blake3 fingerprint stored in the cache. A prefix
#: of the digest — enough to make a cross-credential collision astronomically unlikely,
#: while never storing (a recoverable prefix of) the key itself.
_FINGERPRINT_HEX_LEN: Final[int] = 16

#: File mode for the cache (owner read+write only) — the token is bearer-sensitive.
_CACHE_MODE: Final[int] = 0o600

#: The largest valid clock hour (a reset hour outside ``[0, _MAX_HOUR]`` falls back).
_MAX_HOUR: Final[int] = 23


@dataclass(frozen=True, slots=True)
class CachedToken:
    """A validated cached Groww access token plus the session it belongs to."""

    access_token: str
    issued_for_session: date


def _resolve_reset_hour() -> int:
    """Resolve the 06:00-IST reset hour, env-overridable, clamped to ``[0, 23]``.

    Reads :data:`GROWW_TOKEN_RESET_HOUR_ENV`; falls back to the audited
    :data:`GROWW_TOKEN_RESET_HOUR_IST` default when unset, blank, non-integer, or out
    of the ``0..23`` range — a bad value can never crash a build or push the boundary
    nonsensical (fail-safe to the default, CLAUDE.md §2 Rule 1). O(1).
    """
    raw = os.environ.get(GROWW_TOKEN_RESET_HOUR_ENV)
    if raw is None or not raw.strip():
        return GROWW_TOKEN_RESET_HOUR_IST
    try:
        value = int(raw)
    except ValueError:
        return GROWW_TOKEN_RESET_HOUR_IST
    if value < 0 or value > _MAX_HOUR:
        return GROWW_TOKEN_RESET_HOUR_IST
    return value


def current_session(now_utc: datetime) -> date:
    """The 06:00-IST-anchored trading-session date that *now_utc* belongs to.

    A token minted BEFORE the day's reset hour (default 06:00 IST) belongs to the
    PREVIOUS session; on/after it, the current calendar IST date. Reuses the EXISTING
    :func:`~brutex.adapters.ingest_common.now_ist` IST zone (no second IST notion —
    hostile-review m1 fold) by converting *now_utc* into the same zone.

    Parameters
    ----------
    now_utc
        A timezone-aware instant (the production clock passes
        :func:`now_ist` cast to UTC, or any aware datetime); converted to IST here.

    Returns
    -------
    date
        The session date the token at *now_utc* is valid for.

    Raises
    ------
    ValueError
        If *now_utc* is a NAIVE (tz-unaware) datetime. ``astimezone`` would otherwise
        silently interpret a naive instant as the host's LOCAL time and mis-bucket the
        session (a non-UTC host would assign the wrong session date). Every production
        caller passes an aware UTC instant; this guard fails LOUD on a misuse rather than
        silently corrupting the session bucket (worst-case hardening, CLAUDE.md §2 Rule 1).
    """
    if now_utc.tzinfo is None or now_utc.tzinfo.utcoffset(now_utc) is None:
        raise ValueError("current_session requires a timezone-aware datetime")
    ist = now_utc.astimezone(now_ist().tzinfo)
    if ist.hour < _resolve_reset_hour():
        return date.fromordinal(ist.date().toordinal() - 1)
    return ist.date()


def groww_token_cache_path() -> Path:
    """Resolve the on-disk token-cache path (env-overridable; default under ``~/.brutex``).

    Reads :data:`GROWW_TOKEN_CACHE_PATH_ENV`; falls back to
    ``~/.brutex/cache/groww_access_token.json`` when unset or blank (mirrors the NTM
    cache location). The default lives OUTSIDE the repo (``~/.brutex`` is git-ignored)
    and the basename matches the ``.gitignore`` ``**/groww_access_token*`` glob, so even
    a redirected cache can never be committed (hostile-review M3 fold). O(1).
    """
    raw = os.environ.get(GROWW_TOKEN_CACHE_PATH_ENV)
    if raw is not None and raw.strip():
        return Path(raw).expanduser()
    return Path.home() / ".brutex" / "cache" / GROWW_TOKEN_CACHE_FILENAME


def api_key_fingerprint(api_key: str) -> str:
    """Return ``blake3(api_key)[:16]`` — a non-reversible api_key fingerprint.

    Stored in the cache so a token minted under a DIFFERENT api_key (credential
    rotation) is detected as a mismatch and never reused. NEVER the key itself.
    """
    return blake3.blake3(api_key.encode("utf-8")).hexdigest()[:_FINGERPRINT_HEX_LEN]


def load_cached_token(creds: GrowwCredentials, *, now_utc: datetime) -> CachedToken | None:
    """Load + validate the cached daily token for *creds*, else ``None`` (NEVER raises).

    Returns a :class:`CachedToken` ONLY when EVERY guard passes: the file exists, is
    mode ``0600`` (owner-only — a looser mode is rejected so a world/group-readable token
    is never trusted), parses as JSON, carries ``schema == GROWW_TOKEN_CACHE_SCHEMA``,
    a non-blank ``access_token``, an ``api_key_fingerprint`` matching *creds*, and an
    ``issued_for_session`` EQUAL to :func:`current_session` for *now_utc* (so a stale /
    post-06:00-reset token is a miss). ANY failure → ``None`` with a
    ``groww.token.cache_miss`` debug log naming the reason (never the token/secret).

    Best-effort by contract — a cache problem must never break the build that consults
    it; the caller mints fresh and overwrites on a ``None``.
    """
    path = groww_token_cache_path()
    try:
        stat = path.stat()
    except OSError:
        _LOG.debug("groww.token.cache_miss", reason="absent")
        return None
    # Reject a token file readable by group/other — a bearer token must be 0600.
    if stat.st_mode & 0o077:
        _LOG.debug("groww.token.cache_miss", reason="insecure_mode", mode=oct(stat.st_mode & 0o777))
        return None
    from brutex.data.guarded_read import (
        read_regular_text,
    )

    try:
        raw = read_regular_text(path)
    except OSError:
        _LOG.debug("groww.token.cache_miss", reason="unreadable")
        return None
    except ValueError:
        # A cache file whose bytes are NOT valid UTF-8 raises ``UnicodeDecodeError``,
        # which is a ``ValueError`` subclass (NOT an ``OSError``). Without this arm the
        # decode failure would escape ``load_cached_token`` — breaking the module's
        # documented "returns None on ANY failure, NEVER raises" contract and crashing
        # the cold-start pull build at :attr:`SdkRefresher._cold_start_sdks`. A corrupt
        # (undecodable) cache must be a cache-MISS that re-mints, never a pull-killer.
        _LOG.debug("groww.token.cache_miss", reason="undecodable")
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _LOG.debug("groww.token.cache_miss", reason="corrupt")
        return None
    if not isinstance(payload, dict):
        _LOG.debug("groww.token.cache_miss", reason="not_object")
        return None
    if payload.get("schema") != GROWW_TOKEN_CACHE_SCHEMA:
        _LOG.debug("groww.token.cache_miss", reason="schema_mismatch")
        return None
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        _LOG.debug("groww.token.cache_miss", reason="no_token")
        return None
    if payload.get("api_key_fingerprint") != api_key_fingerprint(creds.api_key):
        _LOG.debug("groww.token.cache_miss", reason="fingerprint_mismatch")
        return None
    session_text = payload.get("issued_for_session")
    if not isinstance(session_text, str):
        _LOG.debug("groww.token.cache_miss", reason="no_session")
        return None
    try:
        session = date.fromisoformat(session_text)
    except ValueError:
        _LOG.debug("groww.token.cache_miss", reason="bad_session")
        return None
    if session != current_session(now_utc):
        _LOG.debug("groww.token.cache_miss", reason="stale_session")
        return None
    _LOG.debug("groww.token.cache_hit", session=session.isoformat())
    return CachedToken(access_token=token, issued_for_session=session)


def store_token(creds: GrowwCredentials, token: str, *, now_utc: datetime) -> bool:
    """Persist *token* as the cached daily token for *creds* (best-effort, NEVER raises).

    Writes the JSON cache ATOMICALLY at mode ``0600`` via the COMMON
    :func:`~brutex.data.atomic_io.atomic_write_simple` (tmp → ``fsync`` → ``os.replace``
    → cleanup; ``chmod=0o600`` so the file is never briefly world-readable). The body
    carries the schema, the access token, the ``current_session`` it belongs to, and the
    api_key FINGERPRINT (never the key). A blank token is NOT cached. Any I/O failure logs
    ``groww.token.cache_store_failed`` and returns ``False`` — the in-process token still
    works, so a caching hiccup never blocks the pull.

    Returns
    -------
    bool
        ``True`` iff the cache was written; ``False`` on a blank token or any I/O failure.
    """
    if not token:
        return False
    path = groww_token_cache_path()
    payload = {
        "schema": GROWW_TOKEN_CACHE_SCHEMA,
        "access_token": token,
        "issued_for_session": current_session(now_utc).isoformat(),
        "api_key_fingerprint": api_key_fingerprint(creds.api_key),
    }
    body = json.dumps(payload)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_simple(
            path,
            lambda tmp: tmp.write_text(body, encoding="utf-8"),
            chmod=_CACHE_MODE,
        )
    except OSError as exc:
        _LOG.warning("groww.token.cache_store_failed", error_type=type(exc).__name__)
        return False
    return True


__all__ = [
    "CachedToken",
    "api_key_fingerprint",
    "current_session",
    "groww_token_cache_path",
    "load_cached_token",
    "store_token",
]
