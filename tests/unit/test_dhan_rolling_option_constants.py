"""Tests for the Dhan rolling-ATM option constants + the honesty AST pins.

DEC-DHAN-ROLLING-OPTIONS-001.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

from brutex.adapters.dhan import rolling_option_constants as c

#: The honesty-marker sentinel names that NO decision branch may read (they must
#: never gate the pull on a fabricated "verified" flag). ``INSTRUMENT_OPTIDX`` is the
#: unverified alternate the adapter must NEVER auto-switch to.
_SENTINEL_NAMES = frozenset(
    {
        "DHAN_ROLLING_INSTRUMENT_OPTIDX_UNVERIFIED",
        "DHAN_ROLLING_SERVABILITY_UNVERIFIED",
    }
)


def test_verbatim_instrument_is_index_only() -> None:
    assert c.DHAN_ROLLING_INSTRUMENT == "INDEX"
    # OPTIDX is UNVERIFIED — recorded, never the requested value.
    assert c.DHAN_ROLLING_INSTRUMENT_OPTIDX_UNVERIFIED == "OPTIDX"


def test_required_data_is_the_verbatim_order() -> None:
    assert c.DHAN_ROLLING_REQUIRED_DATA == (
        "open",
        "high",
        "low",
        "close",
        "iv",
        "volume",
        "strike",
        "oi",
        "spot",
    )


def test_enums_and_band() -> None:
    assert frozenset({"WEEK", "MONTH"}) == c.DHAN_ROLLING_EXPIRY_FLAGS
    assert frozenset({"CALL", "PUT"}) == c.DHAN_ROLLING_OPTION_TYPES
    assert c.DHAN_ROLLING_EXPIRY_CODES == (0, 1, 2, 3)
    assert c.DHAN_ROLLING_STRIKE_BAND == 10
    assert c.DHAN_ROLLING_INTERVAL == 1
    assert c.DHAN_ROLLING_FETCH_INTERVAL == "1minute"
    assert c.DHAN_ROLLING_TIMEOUT_S == 60.0


def test_window_cap_is_30_days_distinct_from_spot_90() -> None:
    # Expired-options cap is 30 (conservative), DISTINCT from the spot 90.
    from brutex.adapters.dhan.constants import DHAN_INTRADAY_WINDOW_DAYS

    assert c.DHAN_ROLLING_WINDOW_DAYS == 30
    assert DHAN_INTRADAY_WINDOW_DAYS == 90
    assert c.DHAN_ROLLING_WINDOW_DAYS != DHAN_INTRADAY_WINDOW_DAYS


def test_lake_subtree_and_schema_and_namespace() -> None:
    assert c.DHAN_ROLLING_SUBDIR == "rolling_options"
    assert c.ROLLING_OPTIONS_SCHEMA_VERSION == 1
    # Own ledger namespace = the binding's reserved ``dhan-option`` prefix.
    assert c.DHAN_ROLLING_COVERAGE_MARKET == "dhan-option"


def test_underlyings_and_wire_addressing_lockstep() -> None:
    assert c.DHAN_ROLLING_UNDERLYINGS == ("NIFTY", "BANKNIFTY", "SENSEX")
    # Every underlying resolves an exchange + a segment (lockstep).
    for underlying in c.DHAN_ROLLING_UNDERLYINGS:
        assert underlying in c.DHAN_ROLLING_EXCHANGES
        assert underlying in c.DHAN_ROLLING_EXCHANGE_SEGMENTS
    assert c.DHAN_ROLLING_EXCHANGES == {"NIFTY": "NSE", "BANKNIFTY": "NSE", "SENSEX": "BSE"}
    assert c.DHAN_ROLLING_EXCHANGE_SEGMENTS == {
        "NIFTY": "NSE_FNO",
        "BANKNIFTY": "NSE_FNO",
        "SENSEX": "BSE_FNO",
    }


def test_history_floor_is_unverified_none_and_requested_is_a_marker() -> None:
    # Golden Rule #1: the real floor authority is None (UNVERIFIED); the requested
    # 2022-06-01 is a recorded, non-load-bearing marker.
    assert c.DHAN_ROLLING_HISTORY_FLOOR is None
    assert date(2022, 6, 1) == c.DHAN_ROLLING_HISTORY_FLOOR_REQUESTED
    assert c.DHAN_ROLLING_SERVABILITY_UNVERIFIED is None


def test_optidx_and_servability_sentinels_never_referenced() -> None:
    """AST pin: no rolling module (other than constants) references the OPTIDX /
    servability sentinels — the adapter ALWAYS requests INDEX and never silent-switches,
    and no decision branch gates on the servability marker (Golden Rule #1).
    """
    pkg_dir = Path(c.__file__).parent
    rolling_files = [
        *sorted(pkg_dir.glob("rolling_option_*.py")),
        pkg_dir / "run_dhan_rolling_option_ingest.py",
    ]
    scanned = [f for f in rolling_files if f.name != "rolling_option_constants.py"]
    assert scanned, "no rolling modules found to scan"
    for source_file in scanned:
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        referenced = (names | attrs) & _SENTINEL_NAMES
        assert not referenced, f"{source_file.name} references sentinel(s) {referenced}"
