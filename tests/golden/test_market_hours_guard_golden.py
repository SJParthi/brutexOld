"""Golden gate: the market-hours guard verdict contract (DEC-RUST-PULL-STATE-001).

The Rust pull-state conversion lane's frozen ORACLE: the committed fixtures
(``market_hours_guard_verdicts.json`` + ``auto_pull_gate_verdicts.json``,
captured from the pre-conversion Python at origin/main 415e863 by
``generate_market_hours_guard_golden.py``) pin every guard verdict the lane
must preserve bit-for-bit:

* ``pull_allowed_now`` over the FULL covered NSE-calendar span (every date of
  2020-01-01..2026-12-31) × the window boundaries (08:44:59 / 08:45:00 /
  15:34:59 / 15:35:00) + representative in/out-of-window times — weekends,
  holidays, Muhurat days (guard-blind by design — a Muhurat date is a listed
  holiday) and the Budget special-weekend sessions included by construction;
* the out-of-coverage FAIL-SAFE cases (an out-of-coverage weekday BLOCKS
  in-window; a weekend stays allowed) — the shared-TOTP account protection of
  DEC-GROWW-MARKET-HOURS-GUARD-001 (MIGRATION.md #1692);
* the ``--auto`` gate's deferral verdicts (allowed + machine reason under the
  hermetic tier-1 settle env) + the Muhurat margined-window boundaries
  (DEC-PULL-SCOPED-ALL-ONECLICK-001, MIGRATION.md #1779).

Any divergence — a Rust-arm drift, an accidental predicate edit, OR a
legitimate calendar move (a new holiday year) — fails LOUDLY here and forces a
deliberate regeneration via ``BRUTEX_REGEN_MARKET_HOURS_GOLDEN=1``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.golden._market_hours_guard_harness import (
    GUARD_PROBE_TIMES,
    compute_auto_gate_table,
    compute_auto_gate_tsv,
    compute_guard_table,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GUARD_FIXTURE = FIXTURES / "market_hours_guard_verdicts.json"
AUTO_GATE_FIXTURE = FIXTURES / "auto_pull_gate_verdicts.json"
AUTO_GATE_TSV_FIXTURE = FIXTURES / "auto_pull_gate_verdicts.tsv"


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_guard_fixture_exists_and_has_full_coverage() -> None:
    """The fixture spans EVERY covered date (anti-vacuous: no thin capture)."""
    frozen = _load(GUARD_FIXTURE)
    days = frozen["days"]
    # 2020-01-01..2026-12-31 = 2557 dates (2020 + 2024 are leap years).
    assert len(days) == 2557
    assert frozen["coverage"] == {"first": "2020-01-01", "last": "2026-12-31"}
    assert frozen["block_start"] == "08:45:00"
    assert frozen["block_end"] == "15:35:00"
    assert frozen["probe_times"] == [t.isoformat() for t in GUARD_PROBE_TIMES]
    # Every row is [0/1, 10-bit verdict string].
    for row in days.values():
        trading, bits = row
        assert trading in (0, 1)
        assert isinstance(bits, str) and len(bits) == len(GUARD_PROBE_TIMES)
        assert set(bits) <= {"0", "1"}


def test_guard_verdicts_match_frozen_oracle() -> None:
    """The LIVE guard reproduces the frozen verdict table structurally EQUAL.

    This is the lane's byte-identity contract: the LIVE decision path — the
    fail-closed Rust kernel consulted through ``pull_allowed_now``
    (DEC-RUST-PULL-STATE-001 flip phase; no Python fallback) — must reproduce
    the pre-conversion capture over the whole covered calendar + the
    out-of-coverage fail-safe cases. A mismatch means either a predicate
    drift (a bug) or a legitimate calendar edit (regenerate DELIBERATELY).
    """
    frozen = _load(GUARD_FIXTURE)
    live = json.loads(
        json.dumps(compute_guard_table(), sort_keys=True, separators=(",", ":"), allow_nan=False)
    )
    assert live == frozen


def test_guard_fixture_pins_known_verdict_shapes() -> None:
    """Spot-check load-bearing rows (anti-vacuous teeth on the capture itself)."""
    frozen = _load(GUARD_FIXTURE)
    days = frozen["days"]
    # A regular trading Tuesday: allowed at 00:00/03:30/08:44:59, BLOCKED
    # 08:45:00..15:34:59, allowed again at 15:35:00 + 20:00.
    assert days["2024-01-02"] == [1, "1110000011"]
    # A Saturday (non-trading): always allowed.
    assert days["2024-01-06"] == [0, "1111111111"]
    # Republic Day 2024 (Friday holiday): always allowed.
    assert days["2024-01-26"] == [0, "1111111111"]
    # Muhurat 2024 (holiday-listed): the GUARD is blind by design — allowed
    # all day (the auto-gate + Muhurat belt own that deferral).
    assert days["2024-11-01"] == [0, "1111111111"]
    # Budget Saturday 2025-02-01: a special weekend TRADING session — the
    # guard BLOCKS in-window exactly like a weekday session.
    assert days["2025-02-01"] == [1, "1110000011"]
    # Out-of-coverage fail-safe: a 2027 weekday BLOCKS in-window; a 2027
    # Saturday stays allowed.
    assert frozen["out_of_coverage"]["2027-01-04"] == [1, "1110000011"]
    assert frozen["out_of_coverage"]["2027-01-02"] == [0, "1111111111"]
    assert frozen["out_of_coverage"]["2019-06-12"] == [1, "1110000011"]
    assert frozen["out_of_coverage"]["2030-07-18"] == [1, "1110000011"]


def test_auto_gate_verdicts_match_frozen_oracle() -> None:
    """The LIVE auto-pull gate reproduces the frozen deferral verdicts."""
    frozen = _load(AUTO_GATE_FIXTURE)
    live = json.loads(
        json.dumps(
            compute_auto_gate_table(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    )
    assert live == frozen


def test_auto_gate_fixture_pins_known_verdicts() -> None:
    """Spot-check the frozen auto-gate decisions (anti-vacuous teeth)."""
    frozen = _load(AUTO_GATE_FIXTURE)
    by_key = {(c["date"], c["time"]): (c["allowed"], c["reason"]) for c in frozen["cases"]}
    # Regular trading day: before the 17:30 settle → before_settle (the
    # settle floor subsumes the market-hours window); at/after → ok.
    assert by_key[("2024-01-02", "12:00:00")] == (False, "before_settle")
    assert by_key[("2024-01-02", "17:29:59")] == (False, "before_settle")
    assert by_key[("2024-01-02", "17:30:00")] == (True, "ok")
    assert by_key[("2024-01-02", "23:59:59")] == (True, "ok")
    # Weekend + holiday → non_trading_day at every probe.
    assert by_key[("2024-01-06", "12:00:00")] == (False, "non_trading_day")
    assert by_key[("2024-01-26", "18:00:00")] == (False, "non_trading_day")
    # Every LISTED Muhurat date is a holiday → defers at step 1 (pinned).
    assert by_key[("2024-11-01", "18:00:00")] == (False, "non_trading_day")
    assert by_key[("2025-10-21", "18:00:00")] == (False, "non_trading_day")
    # Budget special weekend sessions are TRADING days: post-settle → ok.
    assert by_key[("2026-02-01", "18:00:00")] == (True, "ok")
    assert by_key[("2026-02-01", "12:00:00")] == (False, "before_settle")
    # Out-of-coverage weekday proceeds post-settle (ruling 10); weekend defers.
    assert by_key[("2027-01-04", "18:00:00")] == (True, "ok")
    assert by_key[("2027-01-02", "18:00:00")] == (False, "non_trading_day")
    # Muhurat margined-window boundaries: the 2024 session (18:00-19:00 →
    # window 17:30 ≤ t < 19:05) + the timing-less 2026 date reads inactive.
    mu = {(c["date"], c["time"]): c["active"] for c in frozen["muhurat_active"]}
    assert mu[("2024-11-01", "17:29:59")] is False
    assert mu[("2024-11-01", "17:30:00")] is True
    assert mu[("2024-11-01", "19:04:59")] is True
    assert mu[("2024-11-01", "19:05:00")] is False
    assert mu[("2026-11-08", "18:30:00")] is False


def test_auto_gate_tsv_is_the_projection_of_the_json() -> None:
    """One truth, two encodings — the cargo-side TSV can never drift.

    The committed ``.tsv`` (consumed serde-free by
    ``rust/brutex-pull-state/tests/auto_pull_gate_golden.rs``) must be
    byte-equal to the harness projection of the committed ``.json`` — the
    ``pull_stall_verdicts.tsv`` sync-pin pattern.
    """
    frozen = _load(AUTO_GATE_FIXTURE)
    committed = AUTO_GATE_TSV_FIXTURE.read_text(encoding="utf-8")
    assert committed == compute_auto_gate_tsv(frozen)


def test_auto_gate_fixture_pins_wave2d_sections() -> None:
    """Spot-check the Wave-2D extension sections (anti-vacuous teeth)."""
    frozen = _load(AUTO_GATE_FIXTURE)
    # Settle-token tiers: floor-exact 15:35 is ALLOWED; 15:34 is below the
    # BLOCK_END floor; out-of-range + shape junk (incl. Unicode digits) refuse.
    tok = {c["token"]: (c["verdict"], c["minute"]) for c in frozen["settle_tokens"]}
    assert tok["17:30"] == ("ok", 17 * 60 + 30)
    assert tok["15:35"] == ("ok", 15 * 60 + 35)
    assert tok[" 17:30 "] == ("ok", 17 * 60 + 30)  # the str.strip law
    assert tok["15:34"] == ("below_floor", None)
    assert tok["00:00"] == ("below_floor", None)
    assert tok["24:00"] == ("not_wall_clock", None)
    assert tok["17:60"] == ("not_wall_clock", None)
    assert tok["7:30"] == ("bad_shape", None)
    assert tok["\u0661\u0667:\u0663\u0660"] == ("bad_shape", None)  # Unicode digits (re.ASCII)
    assert "unclassified" not in {v for v, _ in tok.values()}
    # The floor-exact settle grid: every in-window minute is before_settle
    # (the market-hours belt stays structurally unreachable); 15:35:00 flips
    # STRAIGHT to ok (settle passed AND the window just closed).
    grid = {
        (c["date"], c["time"]): (c["allowed"], c["reason"]) for c in frozen["cases_settle_1535"]
    }
    assert grid[("2024-01-02", "12:00:00")] == (False, "before_settle")
    assert grid[("2024-01-02", "15:34:59")] == (False, "before_settle")
    assert grid[("2024-01-02", "15:35:00")] == (True, "ok")
    assert grid[("2024-01-02", "23:59:59")] == (True, "ok")
    # Out-of-coverage weekday proceeds at/after the floor (ruling 10).
    assert grid[("2027-01-04", "15:35:00")] == (True, "ok")
    assert not any(reason == "market_hours_blocked" for _, reason in grid.values())
    # Kill-switch coherence: the belt closes an in-window Muhurat instant
    # ONLY while the guard switch is enabled (guard off => belt off).
    belt = {(c["date"], c["time"], c["guard"]): c["closed"] for c in frozen["belt_coherence"]}
    assert belt[("2024-11-01", "18:01:00", "unset")] is True
    assert belt[("2024-11-01", "18:01:00", "1")] is True
    assert belt[("2024-11-01", "18:01:00", "0")] is False
    assert belt[("2024-11-01", "17:29:59", "unset")] is False
