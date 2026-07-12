"""Cross-verify producer configuration — fail-loud env resolution.

DEC-CROSSVERIFY-PRODUCER-001. The one place the ``BRUTEX_CROSSVERIFY_*``
environment surface is parsed (the ``resolve_market_hours_guard_enabled``
idiom, :mod:`brutex.calendars.market_hours`): unset / blank → the safe
default; a recognised token → its value; anything else RAISES ``ValueError``
— junk is never silently coerced (the DEC-RAM-BUDGETS-001 fail-loud env
convention). Resolution is O(1) and happens ONCE at run start; nothing here
re-reads the environment mid-run.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

#: India Standard Time — the producer's "today" is the IST calendar date.
_IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

#: Tokens accepted as "on" (unset/blank → the default, which is ON for both switches).
_ENV_ON_TOKENS: Final[frozenset[str]] = frozenset({"", "1", "on", "true", "yes"})

#: Tokens accepted as "off".
_ENV_OFF_TOKENS: Final[frozenset[str]] = frozenset({"0", "off", "false", "no"})

ENV_KILL_SWITCH: Final[str] = "BRUTEX_CROSSVERIFY"
ENV_PUBLISH: Final[str] = "BRUTEX_CROSSVERIFY_PUBLISH"
ENV_BUCKET: Final[str] = "BRUTEX_CROSSVERIFY_S3_BUCKET"
ENV_PREFIX: Final[str] = "BRUTEX_CROSSVERIFY_S3_PREFIX"
ENV_REGION: Final[str] = "BRUTEX_CROSSVERIFY_AWS_REGION"
ENV_PULL_LOOKBACK_DAYS: Final[str] = "BRUTEX_CROSSVERIFY_PULL_LOOKBACK_DAYS"
ENV_LOCK_WAIT_S: Final[str] = "BRUTEX_CROSSVERIFY_LOCK_WAIT_S"
ENV_LOCK_RETRY_S: Final[str] = "BRUTEX_CROSSVERIFY_LOCK_RETRY_S"
ENV_DEADLINE_S: Final[str] = "BRUTEX_CROSSVERIFY_DEADLINE_S"
ENV_SETTLE_RETRY_ROUNDS: Final[str] = "BRUTEX_CROSSVERIFY_SETTLE_RETRY_ROUNDS"
ENV_SETTLE_RETRY_DELAY_S: Final[str] = "BRUTEX_CROSSVERIFY_SETTLE_RETRY_DELAY_S"
ENV_CATCHUP_DAYS: Final[str] = "BRUTEX_CROSSVERIFY_CATCHUP_DAYS"

DEFAULT_BUCKET: Final[str] = "tv-prod-cold"
DEFAULT_PREFIX: Final[str] = "crossverify/groww"
DEFAULT_REGION: Final[str] = "ap-south-1"
DEFAULT_PULL_LOOKBACK_DAYS: Final[int] = 7
DEFAULT_LOCK_WAIT_S: Final[float] = 2700.0  # 45 min bounded lock wait
DEFAULT_LOCK_RETRY_S: Final[float] = 60.0
DEFAULT_DEADLINE_S: Final[float] = 14400.0  # 4 h hard wall
DEFAULT_SETTLE_RETRY_ROUNDS: Final[int] = 1
DEFAULT_SETTLE_RETRY_DELAY_S: Final[float] = 600.0
DEFAULT_CATCHUP_DAYS: Final[int] = 5

#: Upper bound on the day-count knobs (~10 years). A junk-huge env value now
#: fails LOUDLY at config time instead of surfacing as an ``OverflowError``
#: deep inside date arithmetic mid-run (round-2 hostile review S3a).
MAX_WINDOW_DAYS: Final[int] = 3660


@dataclass(frozen=True, slots=True)
class CrossverifyConfig:
    """The resolved, immutable configuration of one cross-verify producer run."""

    lake_root: Path
    day: date
    bucket: str
    key_prefix: str
    region: str
    enabled: bool
    publish: bool
    pull_lookback_days: int
    lock_wait_s: float
    lock_retry_s: float
    deadline_s: float
    settle_retry_rounds: int
    settle_retry_delay_s: float
    catchup_days: int
    dry_run: bool
    skip_pull: bool


def _resolve_switch(mapping: Mapping[str, str], name: str) -> bool:
    """Fail-loud on/off switch (default ON when unset/blank)."""
    raw = mapping.get(name, "")
    token = raw.strip().lower()
    if token in _ENV_ON_TOKENS:
        return True
    if token in _ENV_OFF_TOKENS:
        return False
    raise ValueError(
        f"{name}={raw!r} is not a recognised switch value; expected one of "
        f"'1', 'on', 'true', 'yes' (enable — the default when unset) or "
        f"'0', 'off', 'false', 'no' (disable)"
    )


def _resolve_int(
    mapping: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int | None = None
) -> int:
    """Fail-loud bounded integer env value (junk / out-of-bounds RAISES)."""
    raw = mapping.get(name, "")
    token = raw.strip()
    if not token:
        return default
    try:
        value = int(token)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not an integer") from exc
    if value < minimum:
        raise ValueError(f"{name}={raw!r} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name}={raw!r} must be <= {maximum}")
    return value


def _resolve_float(
    mapping: Mapping[str, str], name: str, default: float, *, minimum: float
) -> float:
    """Fail-loud finite float env value with an inclusive lower bound."""
    raw = mapping.get(name, "")
    token = raw.strip()
    if not token:
        return default
    try:
        value = float(token)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} is not a number") from exc
    # Reject NaN (never orderable) + out-of-bound / infinite values in ONE
    # comparison shape: NaN fails both comparisons below.
    if not (minimum <= value < float("inf")):
        raise ValueError(f"{name}={raw!r} must be a finite number >= {minimum}")
    return value


def _resolve_str(mapping: Mapping[str, str], name: str, default: str) -> str:
    """Fail-loud non-blank string env value (unset/blank → *default*)."""
    raw = mapping.get(name, "")
    token = raw.strip()
    if not token:
        return default
    if token.startswith("/") or token.endswith("/"):
        raise ValueError(f"{name}={raw!r} must not start or end with '/'")
    return token


def resolve_crossverify_config(
    *,
    lake_root: Path,
    env: Mapping[str, str] | None = None,
    day: date | None = None,
    dry_run: bool = False,
    skip_pull: bool = False,
    no_publish: bool = False,
    now: Callable[[], datetime] | None = None,
) -> CrossverifyConfig:
    """Resolve the whole ``BRUTEX_CROSSVERIFY_*`` env surface ONCE — fail-loud.

    ``day`` defaults to today's IST calendar date at call time (*now* is the
    injectable clock; production leaves it ``None``). ``no_publish`` (the CLI
    ``--no-publish`` flag) forces ``publish=False`` regardless of the env
    tristate — the render+stage-only bring-up mode.

    Raises
    ------
    ValueError
        Any set-but-unrecognised env value (junk is never coerced).
    """
    mapping: Mapping[str, str] = os.environ if env is None else env
    if day is None:
        clock = now if now is not None else lambda: datetime.now(tz=_IST)
        day = clock().astimezone(_IST).date()
    publish_env = _resolve_switch(mapping, ENV_PUBLISH)
    return CrossverifyConfig(
        lake_root=lake_root,
        day=day,
        bucket=_resolve_str(mapping, ENV_BUCKET, DEFAULT_BUCKET),
        key_prefix=_resolve_str(mapping, ENV_PREFIX, DEFAULT_PREFIX),
        region=_resolve_str(mapping, ENV_REGION, DEFAULT_REGION),
        enabled=_resolve_switch(mapping, ENV_KILL_SWITCH),
        publish=publish_env and not no_publish,
        pull_lookback_days=_resolve_int(
            mapping,
            ENV_PULL_LOOKBACK_DAYS,
            DEFAULT_PULL_LOOKBACK_DAYS,
            minimum=0,
            maximum=MAX_WINDOW_DAYS,
        ),
        lock_wait_s=_resolve_float(mapping, ENV_LOCK_WAIT_S, DEFAULT_LOCK_WAIT_S, minimum=0.0),
        lock_retry_s=_resolve_float(mapping, ENV_LOCK_RETRY_S, DEFAULT_LOCK_RETRY_S, minimum=0.1),
        deadline_s=_resolve_float(mapping, ENV_DEADLINE_S, DEFAULT_DEADLINE_S, minimum=1.0),
        settle_retry_rounds=_resolve_int(
            mapping, ENV_SETTLE_RETRY_ROUNDS, DEFAULT_SETTLE_RETRY_ROUNDS, minimum=0
        ),
        settle_retry_delay_s=_resolve_float(
            mapping, ENV_SETTLE_RETRY_DELAY_S, DEFAULT_SETTLE_RETRY_DELAY_S, minimum=0.0
        ),
        catchup_days=_resolve_int(
            mapping, ENV_CATCHUP_DAYS, DEFAULT_CATCHUP_DAYS, minimum=0, maximum=MAX_WINDOW_DAYS
        ),
        dry_run=dry_run,
        skip_pull=skip_pull,
    )


__all__ = [
    "DEFAULT_BUCKET",
    "DEFAULT_CATCHUP_DAYS",
    "DEFAULT_DEADLINE_S",
    "DEFAULT_LOCK_RETRY_S",
    "DEFAULT_LOCK_WAIT_S",
    "DEFAULT_PREFIX",
    "DEFAULT_PULL_LOOKBACK_DAYS",
    "DEFAULT_REGION",
    "DEFAULT_SETTLE_RETRY_DELAY_S",
    "DEFAULT_SETTLE_RETRY_ROUNDS",
    "ENV_BUCKET",
    "ENV_CATCHUP_DAYS",
    "ENV_DEADLINE_S",
    "ENV_KILL_SWITCH",
    "ENV_LOCK_RETRY_S",
    "ENV_LOCK_WAIT_S",
    "ENV_PREFIX",
    "ENV_PUBLISH",
    "ENV_PULL_LOOKBACK_DAYS",
    "ENV_REGION",
    "ENV_SETTLE_RETRY_DELAY_S",
    "ENV_SETTLE_RETRY_ROUNDS",
    "MAX_WINDOW_DAYS",
    "CrossverifyConfig",
    "resolve_crossverify_config",
]
