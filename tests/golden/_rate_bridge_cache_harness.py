"""Shared harness for the rate-bridge-cache decision goldens (DEC-RUST-PULL-STATE-001).

Wave 3H freezes the TWO clockless laws of
``brutex/dashboard/rate_bridge_cache.py`` the port-design names (§2.3 —
"decision-kernel-only, thread core PERMANENT-PYTHON"):

* the three-arm SERVE decision inside
  :meth:`NonBlockingFileMemoBase._serve_bounded` —
  ``(age, ttl, inflight) → {CACHED_HIT, FOLLOWER_UNVERIFIED, OWNER_REFRESH}``;
* the token REVALIDATION decision inside
  :meth:`RateBridgeCache._refresh_body` —
  ``(token, prior) → {revalidate, reread}``.

Both tables are computed by DRIVING the LIVE module (the oracle) with
injected clock / inline guard / counting probe+reader seams — never by
re-deriving its logic; the expected ARM is CLASSIFIED from the OBSERVED
behaviour (served ``source`` token / whether the reader ran).  The thread
machinery (``run_with_deadline`` daemon, ``_inflight`` clearing, degrade
latch) is PERMANENT-PYTHON and is NOT in these tables — its contract stays
pinned by ``tests/unit/test_dashboard_rate_bridge_cache.py`` (906 LOC) plus
the sweep twin's suite, both of which must pass UNMODIFIED.

Absolute clock instants are DYADIC so every ``now - probed_at`` subtraction
is exact — the boundary rows (``age == ttl``, the strict-``<`` law) are
genuinely on the boundary.  Floats are also recorded as IEEE-754 bit
patterns for the serde-free cargo fixture test
(``rust/brutex-pull-state/tests/rate_bridge_golden.rs`` reads the TSV).
"""

# Whitebox drives of module-private state (sanctioned — the unit suite precedent).
# pyright: reportPrivateUsage=none

from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Any

from brutex.dashboard.rate_bridge_cache import (
    MEMO_SOURCE_CACHED,
    MEMO_SOURCE_LIVE,
    MEMO_SOURCE_UNVERIFIED,
    RateBridgeCache,
)
from brutex.data.rate_bridge import RateBridgeSnapshot

#: Serve-arm names, index == the Rust kernel's token value
#: (``rust/brutex-pull-state/src/rate_bridge.rs`` — the shared vocabulary).
SERVE_ARM_NAMES: tuple[str, ...] = (
    "cached_hit",  # 0 — the zero-syscall TTL fast path
    "follower_unverified",  # 1 — a refresh is in flight: serve last-known, labelled
    "owner_refresh",  # 2 — this caller owns the guard-bounded refresh
)

#: Observed ``source`` token → serve-arm index (the classification map).
_SOURCE_TO_ARM: dict[str, int] = {
    MEMO_SOURCE_CACHED: 0,
    MEMO_SOURCE_UNVERIFIED: 1,
    MEMO_SOURCE_LIVE: 2,  # the owner arm COMPLETED its refresh inline
}


def f64_bits(value: float) -> str:
    """The IEEE-754 bit pattern of *value* as 16 lowercase hex chars (``<f8``)."""
    return struct.pack("<d", value).hex()


def f64_from_bits(bits: str) -> float:
    """Invert :func:`f64_bits` (bit-exact)."""
    return float(struct.unpack("<d", bytes.fromhex(bits))[0])


def _snapshot(marker: float) -> RateBridgeSnapshot:
    """A distinguishable memo snapshot (the marker rides ``effective_rps``)."""
    return RateBridgeSnapshot(
        schema=3,
        effective_rps=marker,
        current_rps=5.0,
        recent_minute=180,
        recent_429=0,
        ceiling_per_min=500,
        recent_1s=3,
        recent_600s=1800,
        recent_3600s=9000,
        wall_time=1_700_000_000.0,
        monotonic=1024.0,
    )


def _build_cache(
    *,
    now: float,
    ttl: float,
    token: str | None = "tok",  # noqa: S107 — a content-digest fixture, not a secret
    calls: dict[str, int] | None = None,
) -> RateBridgeCache:
    """A cache with an injected frozen clock, INLINE guard + counting seams."""
    counters = calls if calls is not None else {"probe": 0, "read": 0, "guard": 0}

    def _probe(_target: Path) -> str | None:
        counters["probe"] += 1
        return token

    def _reader(_target: Path) -> RateBridgeSnapshot | None:
        counters["read"] += 1
        return _snapshot(99.0)

    def _guard(fn: Any, _deadline: float) -> None:
        counters["guard"] += 1
        fn()

    return RateBridgeCache(
        ttl_s=ttl,
        read_deadline_s=0.5,
        clock=lambda: now,
        probe=_probe,
        reader=_reader,
        guard=_guard,
    )


#: ``(case_id, probed_at | None, now, ttl, inflight)`` — dyadic instants.
SERVE_CASES: tuple[tuple[str, float | None, float, float, bool], ...] = (
    ("cold_owner", None, 1024.0, 2.0, False),
    ("cold_follower", None, 1024.0, 2.0, True),
    ("fresh_hit", 1023.0, 1024.0, 2.0, False),
    ("fresh_hit_inflight_ttl_wins", 1023.0, 1024.0, 2.0, True),
    ("boundary_exact_owner", 1022.0, 1024.0, 2.0, False),
    ("boundary_exact_follower", 1022.0, 1024.0, 2.0, True),
    ("just_under_boundary", 1022.0 + 2.0**-10, 1024.0, 2.0, False),
    ("stale_owner", 1000.0, 1024.0, 2.0, False),
    ("stale_follower", 1000.0, 1024.0, 2.0, True),
    ("ttl_zero_owner", 1023.5, 1024.0, 0.0, False),
    ("ttl_zero_follower", 1023.5, 1024.0, 0.0, True),
    ("ttl_negative_owner", 1023.5, 1024.0, -1.0, False),
    ("ttl_nan_owner", 1023.5, 1024.0, math.nan, False),
    ("ttl_inf_hit", 1000.0, 1024.0, math.inf, False),
    ("skew_future_probe_clamps_hit", 1026.0, 1024.0, 2.0, False),
    ("skew_future_ttl_zero_owner", 1026.0, 1024.0, 0.0, False),
)


def compute_serve_table() -> list[dict[str, Any]]:
    """The serve-arm lattice, observed from the REAL ``_serve_bounded``."""
    rows: list[dict[str, Any]] = []
    for case_id, probed_at, now, ttl, inflight in SERVE_CASES:
        calls = {"probe": 0, "read": 0, "guard": 0}
        cache = _build_cache(now=now, ttl=ttl, calls=calls)
        cache._probed_at = probed_at
        cache._inflight = inflight
        cache._snapshot = _snapshot(7.0)
        result = cache.get(Path("/nonexistent-lake"))
        # The module's own age computation (mirrored mechanically; for the
        # cached/follower arms the OBSERVED probe_age must equal it).
        age = None if probed_at is None else max(0.0, now - probed_at)
        arm = _SOURCE_TO_ARM[result.source]
        if arm in (0, 1):
            expected_age = age
            assert result.probe_age_s == expected_age or (
                expected_age is not None
                and math.isnan(expected_age)
                and math.isnan(result.probe_age_s or 0.0)
            ), case_id
            assert calls["guard"] == 0, case_id  # no refresh ran
        else:
            assert calls["guard"] == 1, case_id  # the owner ran ONE refresh
        rows.append(
            {
                "id": case_id,
                "age": None if age is None else {"bits": f64_bits(age)},
                "ttl": {"bits": f64_bits(ttl)},
                "inflight": inflight,
                "arm": arm,
                "arm_name": SERVE_ARM_NAMES[arm],
                "source": result.source,
                "probe_age": (
                    None if result.probe_age_s is None else {"bits": f64_bits(result.probe_age_s)}
                ),
                "refresh_ran": calls["guard"] == 1,
            }
        )
    return rows


#: ``(case_id, prior_token | None, probe_token | None)``.
TOKEN_CASES: tuple[tuple[str, str | None, str | None], ...] = (
    ("first_probe_reads", None, "t1"),
    ("unchanged_revalidates", "t1", "t1"),
    ("changed_rereads", "t1", "t2"),
    ("probe_error_rereads", "t1", None),
    ("both_none_rereads", None, None),
    ("equal_empty_revalidates", "", ""),
    ("empty_vs_token_rereads", "", "t1"),
    ("token_vs_empty_rereads", "t1", ""),
    ("unicode_equal_revalidates", "μ:1", "μ:1"),
)


def compute_token_table() -> list[dict[str, Any]]:
    """The token-revalidation law, observed from the REAL ``_refresh_body``."""
    rows: list[dict[str, Any]] = []
    for case_id, prior, token in TOKEN_CASES:
        calls = {"probe": 0, "read": 0, "guard": 0}
        cache = _build_cache(now=2048.0, ttl=2.0, token=token, calls=calls)
        cache._token = prior
        cache._snapshot = _snapshot(7.0)
        cache._refresh_body(Path("/nonexistent-lake"))
        assert calls["probe"] == 1, case_id
        revalidated = calls["read"] == 0
        # The revalidated arm keeps the memoised token; the reread arm stores
        # the freshly probed one (None stored as None — never trusted).
        stored = cache._token
        assert stored == (prior if revalidated else token), case_id
        rows.append(
            {
                "id": case_id,
                "prior": prior,
                "token": token,
                "revalidates": revalidated,
            }
        )
    return rows


def compute_payload() -> dict[str, Any]:
    """The whole JSON fixture payload."""
    return {
        "_comment": (
            "Frozen rate-bridge-cache decision golden (DEC-RUST-PULL-STATE-001 "
            "Wave 3H) — captured from the PRE-conversion "
            "brutex/dashboard/rate_bridge_cache.py; regenerate ONLY under "
            "BRUTEX_REGEN_RATE_BRIDGE_CACHE_GOLDEN=1 with a ledgered reason."
        ),
        "serve": compute_serve_table(),
        "token": compute_token_table(),
    }


def _opt_str(value: str | None) -> str:
    r"""A TSV cell for an optional token (``\N`` = None; tokens are TSV-safe)."""
    if value is None:
        return "\\N"
    assert "\t" not in value and "\n" not in value
    return f"s:{value}"  # the s: prefix keeps the empty string distinguishable


def compute_tsv(payload: dict[str, Any]) -> str:
    """The serde-free cargo-fixture projection of *payload*."""
    lines = [
        "# rate_bridge_cache decision golden (DEC-RUST-PULL-STATE-001 Wave 3H)",
        "# serve\tid\tage_kind\tage_bits\tttl_bits\tinflight\tarm",
        "# token\tid\tprior\ttoken\trevalidates",
    ]
    for row in payload["serve"]:
        age = row["age"]
        age_kind = "0" if age is None else "1"
        age_bits = "-" if age is None else age["bits"]
        inflight = "1" if row["inflight"] else "0"
        lines.append(
            f"serve\t{row['id']}\t{age_kind}\t{age_bits}\t{row['ttl']['bits']}\t"
            f"{inflight}\t{row['arm']}"
        )
    for row in payload["token"]:
        revalidates = "1" if row["revalidates"] else "0"
        lines.append(
            f"token\t{row['id']}\t{_opt_str(row['prior'])}\t{_opt_str(row['token'])}\t{revalidates}"
        )
    return "\n".join(lines) + "\n"
