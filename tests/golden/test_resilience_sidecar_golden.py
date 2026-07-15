"""Golden byte-equality oracle for the resilience sidecar + the frozen identity.

Pins the deterministic ``<stem>.resilience.parquet`` sha256 for a fixed
12-trade fixture + the exact ``to_record()`` values, and asserts the FROZEN
run-identity goldens (``_GOLDEN_RUN_ID`` / ``_GOLDEN_CONTENT_HASH``) are
UNMOVED — this annotation pass writes a NEW sibling sidecar and never touches
``ResultRow`` bytes.

Regenerate (deterministic) after a DELIBERATE schema change::

    .venv/bin/python -c "import tests.golden.test_resilience_sidecar_golden as g; g.print_digest()"
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
from brutex.engine.resilience_streaks import (
    ResilienceStreaks,
    fold_resilience,
    write_resilience_sidecar,
)
from brutex.engine.trade_store import SidecarPaths

if TYPE_CHECKING:
    from pathlib import Path

pytest.importorskip("polars")

_IST = timezone(timedelta(hours=5, minutes=30))

#: Locked sha256 of the rendered resilience parquet bytes (deliberate re-lock).
_GOLDEN_RESILIENCE_SHA256 = "2301893bb1bcf1c3a1b27d5281e2fc1fda6dac184ca513fba7dbc3beac1d6005"

#: The fixed 12-trade fixture (pnl paisa, IST year/month/day), spanning 2 years.
_FIXTURE = [
    (-100, 2023, 1, 2),
    (-50, 2023, 1, 3),
    (200, 2023, 2, 1),
    (-30, 2023, 2, 2),
    (-30, 2023, 2, 3),
    (-30, 2023, 2, 6),
    (400, 2023, 3, 1),
    (-80, 2024, 1, 2),
    (90, 2024, 1, 3),
    (-20, 2024, 1, 4),
    (-20, 2024, 1, 5),
    (300, 2024, 2, 1),
]


@dataclass(frozen=True, slots=True)
class _FakeTrade:
    pnl_paisa: int
    entry_ts: datetime
    exit_ts: datetime


def _record() -> ResilienceStreaks:
    trades = [
        _FakeTrade(
            pnl_paisa=pnl,
            entry_ts=datetime(y, m, d, 10, 0, tzinfo=_IST).astimezone(UTC),
            exit_ts=datetime(y, m, d, 10, 1, tzinfo=_IST).astimezone(UTC),
        )
        for pnl, y, m, d in _FIXTURE
    ]
    return fold_resilience(
        "m" * 16, trades, parity_ok=True, start_equity_paisa=1_000_000, fingerprint="fp-golden"
    )


def _rendered(tmp_path: Path) -> bytes:
    base = tmp_path / "d"
    base.mkdir()
    paths = SidecarPaths(
        trades=base / "x.trades.parquet",
        aggs=base / "x.aggs.parquet",
        equity=base / "x.equity.parquet",
        stamp=base / "x.stamp.json",
        resilience=base / "x.resilience.parquet",
        resilience_stamp=base / "x.resilience.stamp.json",
    )
    write_resilience_sidecar(paths, [_record()], store_content_hash="ab" * 32)
    return paths.resilience.read_bytes()


def print_digest() -> None:  # pragma: no cover - the regeneration helper
    import tempfile
    from pathlib import Path

    print(hashlib.sha256(_rendered(Path(tempfile.mkdtemp()))).hexdigest())


def test_golden_parquet_bytes_are_locked(tmp_path: Path) -> None:
    assert hashlib.sha256(_rendered(tmp_path)).hexdigest() == _GOLDEN_RESILIENCE_SHA256


def test_golden_record_values(tmp_path: Path) -> None:
    r = _record()
    assert r.n_trades == 12
    assert r.losing_streak_count == 4
    assert r.losing_streak_p50 == 2
    assert r.losing_streak_p95 == 3
    assert r.worst_streak_pnl_paisa == -150
    assert r.worst_streak_len == 2
    assert r.worst_streak_pnl_pct == pytest.approx(-0.015)
    assert r.longest_underwater_trades == 3
    assert r.longest_underwater_days == 307
    assert r.still_underwater_at_end is False
    assert r.losses_by_month_json == '{"2023-01":2,"2023-02":3,"2024-01":3}'
    assert r.worst_loss_month == "2023-02"
    assert r.worst_loss_month_count == 3
    assert r.win_rate_by_year_json == '{"2023":[2,7],"2024":[2,5]}'
    assert r.win_rate_year_spread is None  # no two years with >= 20 trades


def test_run_identity_goldens_are_unmoved() -> None:
    """The annotation pass NEVER touches ResultRow bytes — identity frozen."""
    from tests.golden.test_engine_sweep_run_golden import (
        _GOLDEN_CONTENT_HASH,
        _GOLDEN_RUN_ID,
    )

    assert _GOLDEN_RUN_ID == "f588194f3f107bec68ee48975171041dd77c8f5ceec7e0625534b352d2d5921a"
    assert (
        _GOLDEN_CONTENT_HASH == "2387cc884944768ee271fad117edb953f538d4d6d2d3b3c1c4b9ca3f4bd4d147"
    )
