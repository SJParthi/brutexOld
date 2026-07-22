"""Coverage + honesty pins for the ``/db`` rolling-ATM discovery module.

DEC-DHAN-ROLLING-OPTIONS-001 (§5 /db) · DEC-DHAN-ROLLING-DB-VIEW-001. Pure
directory-walk discovery + column metadata — 100% line+branch on the new
module, plus the rolling-NOT-per-contract HONESTY teeth (§9 test plan): NO
path segment is a resolved date / absolute strike; ``resolved_expiry_date`` /
``resolved_strike`` are COLUMNS not keys; no label says "per-contract".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.adapters.dhan.rolling_option_constants import DHAN_ROLLING_SUBDIR
from brutex.adapters.dhan.rolling_option_writer import strike_offset_safe
from brutex.dashboard.dhan_rolling_db_view import (
    DHAN_ROLLING_OPTIONS_COLUMNS,
    dhan_rolling_options_schema,
    list_dhan_rolling_option_datasets,
    strike_offset_from_safe,
)


def _make_dataset(
    feed_root: Path,
    *,
    exchange: str = "NSE",
    underlying: str = "NIFTY",
    expiry_flag: str = "WEEK",
    ec: str = "EC0",
    strike_offset_safe_seg: str = "ATMp1",
    option_type: str = "CALL",
    interval: str = "1minute",
    year: str = "2024",
    month: str = "06",
    with_parquet: bool = True,
) -> Path:
    """Create one selector partition under the rolling-ATM scheme."""
    leaf = (
        feed_root
        / DHAN_ROLLING_SUBDIR
        / exchange
        / underlying
        / expiry_flag
        / ec
        / strike_offset_safe_seg
        / option_type
        / interval
    )
    (leaf / year).mkdir(parents=True, exist_ok=True)
    if with_parquet:
        (leaf / year / f"{month}.parquet").write_bytes(b"PAR1")
    return leaf


# ── strike_offset_from_safe: the inverse of the writer transform ──


@pytest.mark.parametrize(
    ("safe", "expected"),
    [
        ("ATM", "ATM"),
        ("ATMp1", "ATM+1"),
        ("ATMp10", "ATM+10"),
        ("ATMm1", "ATM-1"),
        ("ATMm10", "ATM-10"),
        ("WEIRD", "WEIRD"),  # legacy / unexpected → honest verbatim passthrough
    ],
)
def test_strike_offset_from_safe(safe: str, expected: str) -> None:
    assert strike_offset_from_safe(safe) == expected


@pytest.mark.parametrize("label", ["ATM", "ATM+1", "ATM-10", "ATM+10", "ATM-3"])
def test_strike_offset_round_trip_with_writer(label: str) -> None:
    # The reconstructed label must exactly invert the writer's safe transform.
    assert strike_offset_from_safe(strike_offset_safe(label)) == label


# ── schema / column metadata ──


def test_schema_projection_shape() -> None:
    schema = dhan_rolling_options_schema()
    assert len(schema) == len(DHAN_ROLLING_OPTIONS_COLUMNS)
    for entry, col in zip(schema, DHAN_ROLLING_OPTIONS_COLUMNS, strict=True):
        assert entry == {
            "name": col.name,
            "dtype": col.dtype,
            "source": col.source,
            "nullable": col.nullable,
            "doc": col.doc,
        }


def test_required_columns_present() -> None:
    names = {c.name for c in DHAN_ROLLING_OPTIONS_COLUMNS}
    for required in (
        "resolved_expiry_date",
        "resolved_strike",
        "strike_offset",
        "option_type",
        "expiry_flag",
        "expiry_code",
        "iv",
        "spot",
        "iv_used",
        "delta",
        "gamma",
        "theta",
        "vega",
        "rho",
        "iv_source",
    ):
        assert required in names, required


def test_resolved_metadata_are_bar_columns_not_partitions() -> None:
    by_name = {c.name: c for c in DHAN_ROLLING_OPTIONS_COLUMNS}
    # The rolling-ATM crux: resolved date/strike are per-ROW bar columns.
    assert by_name["resolved_expiry_date"].source == "bar"
    assert by_name["resolved_strike"].source == "bar"
    # The FIXED selectors are the partition-encoded ones.
    for sel in ("expiry_flag", "expiry_code", "strike_offset", "option_type", "interval"):
        assert by_name[sel].source == "partition"


def test_no_per_contract_labels_anywhere() -> None:
    # HONESTY label audit: nothing advertises a per-contract framing.
    for col in DHAN_ROLLING_OPTIONS_COLUMNS:
        blob = f"{col.name} {col.doc}".lower()
        assert "per-contract" not in blob
        assert "per contract" not in blob


def test_iv_is_not_money_snapped_dtype() -> None:
    by_name = {c.name: c for c in DHAN_ROLLING_OPTIONS_COLUMNS}
    assert by_name["iv"].dtype == "float"  # a volatility metric, never money-rounded


# ── discovery walk ──


def test_no_rolling_dir_returns_empty(tmp_path: Path) -> None:
    assert list_dhan_rolling_option_datasets(tmp_path) == []


def test_single_dataset_discovered(tmp_path: Path) -> None:
    _make_dataset(tmp_path)
    out = list_dhan_rolling_option_datasets(tmp_path)
    assert out == [
        {
            "kind": "dhan_rolling_options",
            "exchange": "NSE",
            "underlying": "NIFTY",
            "expiry_flag": "WEEK",
            "expiry_code": 0,
            "strike_offset": "ATM+1",
            "strike_offset_safe": "ATMp1",
            "option_type": "CALL",
            "interval": "1minute",
        }
    ]


def test_dir_without_parquet_is_skipped(tmp_path: Path) -> None:
    # A selector dir with a year subdir but NO parquet is not a dataset.
    _make_dataset(tmp_path, strike_offset_safe_seg="ATMm2", with_parquet=False)
    _make_dataset(tmp_path, strike_offset_safe_seg="ATM")
    out = list_dhan_rolling_option_datasets(tmp_path)
    assert [d["strike_offset"] for d in out] == ["ATM"]


def test_malformed_expiry_code_surfaced_as_none(tmp_path: Path) -> None:
    _make_dataset(tmp_path, ec="ECbad")
    _make_dataset(tmp_path, ec="NOTEC")
    out = list_dhan_rolling_option_datasets(tmp_path)
    assert {d["expiry_code"] for d in out} == {None}


def test_stray_file_at_a_level_never_raises(tmp_path: Path) -> None:
    _make_dataset(tmp_path)
    # A stray file beside the exchange dirs must be ignored (dir-only walk).
    (tmp_path / DHAN_ROLLING_SUBDIR / "README.txt").write_text("not a dir")
    out = list_dhan_rolling_option_datasets(tmp_path)
    assert len(out) == 1


def test_multiple_datasets_sorted_and_distinct_rolls(tmp_path: Path) -> None:
    # Two strike offsets + two option types under the SAME fixed selector set —
    # each a distinct rolling-ATM dataset, listed in stable path order.
    _make_dataset(tmp_path, strike_offset_safe_seg="ATM", option_type="PUT")
    _make_dataset(tmp_path, strike_offset_safe_seg="ATMp1", option_type="CALL")
    out = list_dhan_rolling_option_datasets(tmp_path)
    assert [(d["strike_offset"], d["option_type"]) for d in out] == [
        ("ATM", "PUT"),
        ("ATM+1", "CALL"),
    ]


def test_one_fixed_partition_spans_a_real_roll_across_months(tmp_path: Path) -> None:
    # HONESTY teeth: a FIXED (expiry_code, strike_offset) partition legitimately
    # holds bars across MANY months (a real roll) — the discovery still reports
    # exactly ONE dataset (the resolved date/strike live in COLUMNS, not keys).
    _make_dataset(tmp_path, year="2024", month="06")
    _make_dataset(tmp_path, year="2024", month="07")
    out = list_dhan_rolling_option_datasets(tmp_path)
    assert len(out) == 1
    entry = out[0]
    # No path-derived resolved date / absolute strike leaks into the descriptor.
    assert "resolved_expiry_date" not in entry
    assert "resolved_strike" not in entry
