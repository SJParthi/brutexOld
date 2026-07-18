"""Frozen session-integrity goldens (DEC-EXTENDED-SESSION-OUTAGE-001 +
DEC-SESSION-DENSITY-CENSUS-001).

* the SYNTHETIC 2021-02-24 extended-day trade list — byte-frozen: the trade
  rides through the OLD 15:20 boundary and force-exits at 16:50 (fails on the
  pre-fix resolver, which forced out at 15:20 — the reversion-gate teeth);
* the census verdict over the REAL 2024-01-02..04 anchor with the FABRICATED
  45-minute hole — byte-frozen flag shape/gap/abstain payload.

Regeneration is env-gated (``BRUTEX_REGEN_SESSION_INTEGRITY_GOLDEN=1`` in the
generator) — the frozen capture is the oracle (Golden Rule #1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.golden import generate_session_integrity_golden as _gen
from tests.golden._session_integrity_harness import (
    GOLDEN_CENSUS,
    GOLDEN_TRADES,
    census_payload,
    extended_day_trades_payload,
    render,
)


def test_extended_day_trade_list_matches_frozen_bytes() -> None:
    assert GOLDEN_TRADES.exists(), "run tests/golden/generate_session_integrity_golden.py once"
    assert render(extended_day_trades_payload()) == GOLDEN_TRADES.read_bytes()


def test_extended_day_trade_exits_at_1650_not_1520() -> None:
    # The semantic teeth on top of the byte pin: exactly one trade, exiting at
    # the 16:50 IST bar (11:20 UTC), pnl == the 16:50 open − the entry open.
    payload = extended_day_trades_payload()
    assert len(payload["trades"]) == 1
    trade = payload["trades"][0]
    assert trade["exit_ts"] == "2021-02-24T11:20:00+00:00"  # 16:50 IST
    assert trade["pnl_paisa"] == 60  # entry 100.10 @15:19-next-open ... forced @100.70


def test_census_verdict_matches_frozen_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Hermetic against the operator env knobs (review round-2): the harness
    # pins both thresholds explicitly; deleting the vars here is belt-and-
    # braces so a future harness edit can never silently re-couple the frozen
    # oracle to the shell (the test_session_density per-test pattern).
    monkeypatch.delenv("BRUTEX_SESSION_DENSITY_MIN_FRACTION", raising=False)
    monkeypatch.delenv("BRUTEX_SESSION_DENSITY_ABS_FLOOR", raising=False)
    assert GOLDEN_CENSUS.exists(), "run tests/golden/generate_session_integrity_golden.py once"
    assert render(census_payload()) == GOLDEN_CENSUS.read_bytes()


class TestRegenRefusal:
    """The generator's refusal guard is executed in-suite (review round-1):
    a future edit dropping the guard fails HERE, not only in a live run."""

    def _repoint(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
        fixtures = tmp_path / "fixtures"
        trades = fixtures / "extended_session_trades.json"
        census = fixtures / "session_density_census.json"
        monkeypatch.setattr(_gen, "FIXTURES", fixtures)
        monkeypatch.setattr(_gen, "GOLDEN_TRADES", trades)
        monkeypatch.setattr(_gen, "GOLDEN_CENSUS", census)
        return trades, census

    def test_refuses_existing_fixtures_without_the_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        trades, census = self._repoint(monkeypatch, tmp_path)
        trades.parent.mkdir(parents=True)
        trades.write_bytes(b"frozen-oracle-trades")
        census.write_bytes(b"frozen-oracle-census")
        monkeypatch.delenv(_gen.REGEN_ENV, raising=False)
        with pytest.raises(SystemExit) as excinfo:
            _gen.main()
        message = str(excinfo.value)
        assert "REFUSED" in message
        assert str(trades) in message and str(census) in message
        assert _gen.REGEN_ENV in message
        # Nothing was touched — the frozen oracle survives the refusal.
        assert trades.read_bytes() == b"frozen-oracle-trades"
        assert census.read_bytes() == b"frozen-oracle-census"

    def test_env_escape_hatch_overwrites_deliberately(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        trades, census = self._repoint(monkeypatch, tmp_path)
        trades.parent.mkdir(parents=True)
        trades.write_bytes(b"stale")
        census.write_bytes(b"stale")
        monkeypatch.setenv(_gen.REGEN_ENV, "1")
        _gen.main()
        assert trades.read_bytes() == render(extended_day_trades_payload())
        assert census.read_bytes() == render(census_payload())

    def test_fresh_directory_writes_without_the_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        trades, census = self._repoint(monkeypatch, tmp_path)
        monkeypatch.delenv(_gen.REGEN_ENV, raising=False)
        _gen.main()
        assert trades.read_bytes() == render(extended_day_trades_payload())
        assert census.read_bytes() == render(census_payload())
