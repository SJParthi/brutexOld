"""Unit tests for brutex.engine.baseline (DEC-SWEEP-RESULTS-DEPTH-001 WP2).

Covers: the buy-&-hold series from fixture bars (anchor / per-session closes /
full-precision total return), permuted-load reproducibility (sorted contract),
duplicate/empty/corrupt-input refusals, the ``BaselineSeries`` validation
matrix, the deterministic B8 JSON artifact (write-twice byte identity + a
LOCKED sha256 golden), the write/load round trip and the full load damage
matrix (every rejection → ``None``, never a half-trusted artifact).
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from brutex.calendars.nse import IST
from brutex.data.models import CandleBar
from brutex.engine.baseline import (
    BASELINE_SCHEMA_VERSION,
    BaselineError,
    BaselineSeries,
    baseline_filename,
    compute_baseline,
    load_baseline,
    write_baseline,
)

_SNAPSHOT = "5a" * 32

#: Locked golden digest of the B8 artifact bytes for the fixed fixture below.
#: Regeneration is a deliberate, ledgered act (the ``_GOLDEN_CONTENT_HASH``
#: discipline): python -c "import tests.unit.test_engine_baseline as t; print(t.golden_digest())"
_GOLDEN_BASELINE_SHA256 = "fb3d4390b4f827c36fbf9d21fd265240539467fa65cf2f46477922f30c39b072"


def _bar(d: date, hh: int, mm: int, o: str, c: str) -> CandleBar:
    ts = datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).astimezone(UTC)
    prices = sorted([Decimal(o), Decimal(c)])
    return CandleBar(
        timestamp=ts,
        open=Decimal(o),
        high=prices[1],
        low=prices[0],
        close=Decimal(c),
        volume=0,
        open_interest=None,
    )


_D1 = date(2024, 1, 2)
_D2 = date(2024, 1, 3)
_D3 = date(2024, 1, 5)

#: Fixed fixture: three sessions, two bars each — anchor = the FIRST bar's
#: OPEN (100.00); each session's point = its LAST bar CLOSE.
_BARS = [
    _bar(_D1, 9, 15, "100.00", "100.40"),
    _bar(_D1, 15, 29, "100.40", "101.20"),
    _bar(_D2, 9, 15, "101.10", "100.80"),
    _bar(_D2, 15, 29, "100.80", "99.95"),
    _bar(_D3, 9, 15, "100.10", "104.60"),
    _bar(_D3, 15, 29, "104.60", "105.55"),
]


def _series() -> BaselineSeries:
    return compute_baseline(_BARS, instrument="NIFTY")


def golden_digest() -> str:
    """Recompute the golden artifact digest (regeneration helper)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / baseline_filename("NIFTY")
        write_baseline(path, _series(), data_snapshot_hash=_SNAPSHOT)
        return hashlib.sha256(path.read_bytes()).hexdigest()


class TestComputeBaseline:
    def test_anchor_sessions_and_span(self) -> None:
        series = _series()
        assert series.instrument == "NIFTY"
        assert series.anchor_open == Decimal("100.00")
        assert series.start == _D1
        assert series.end == _D3
        assert series.sessions == (
            (_D1, Decimal("101.20")),
            (_D2, Decimal("99.95")),
            (_D3, Decimal("105.55")),
        )

    def test_total_return_full_precision(self) -> None:
        series = _series()
        expected = float((Decimal("105.55") - Decimal("100.00")) / Decimal("100.00") * 100)
        assert series.total_return_pct == expected
        assert series.total_return_pct == 5.55

    def test_source_note_names_the_computed_source(self) -> None:
        note = _series().source_note
        assert "first bar OPEN" in note
        assert "LAST bar CLOSE" in note
        assert "6 bars" in note
        assert "3 session(s)" in note

    def test_permuted_load_order_reproduces_identical_series(self) -> None:
        shuffled = list(_BARS)
        random.Random(0).shuffle(shuffled)
        assert compute_baseline(shuffled, instrument="NIFTY") == _series()
        assert compute_baseline(list(reversed(_BARS)), instrument="NIFTY") == _series()

    def test_empty_bars_rejected(self) -> None:
        with pytest.raises(BaselineError, match="no bars"):
            compute_baseline([], instrument="NIFTY")

    def test_duplicate_timestamp_rejected(self) -> None:
        with pytest.raises(BaselineError, match="duplicate bar timestamp"):
            compute_baseline([_BARS[0], _BARS[0]], instrument="NIFTY")

    def test_non_positive_anchor_rejected(self) -> None:
        corrupt = CandleBar(
            timestamp=_BARS[0].timestamp,
            open=Decimal("0"),
            high=Decimal("1"),
            low=Decimal("0"),
            close=Decimal("1"),
            volume=0,
            open_interest=None,
        )
        with pytest.raises(BaselineError, match="OPEN must be > 0"):
            compute_baseline([corrupt], instrument="NIFTY")


class TestBaselineSeriesValidation:
    def _kwargs(self) -> dict[str, Any]:
        return {
            "instrument": "NIFTY",
            "start": _D1,
            "end": _D2,
            "anchor_open": Decimal("100.00"),
            "sessions": ((_D1, Decimal("101.20")), (_D2, Decimal("99.95"))),
            "total_return_pct": -0.05,
            "source_note": "test",
        }

    def _make(self, **overrides: Any) -> BaselineSeries:
        kwargs: dict[str, Any] = {**self._kwargs(), **overrides}
        return BaselineSeries(**kwargs)

    def test_valid_constructs(self) -> None:
        series = self._make()
        assert series.end == _D2

    def test_empty_instrument_rejected(self) -> None:
        with pytest.raises(BaselineError, match="instrument"):
            self._make(instrument="")

    def test_empty_sessions_rejected(self) -> None:
        with pytest.raises(BaselineError, match="non-empty"):
            self._make(sessions=())

    def test_unsorted_sessions_rejected(self) -> None:
        kwargs = self._kwargs()
        with pytest.raises(BaselineError, match="strictly ascending"):
            self._make(sessions=tuple(reversed(kwargs["sessions"])), start=_D2, end=_D1)

    def test_start_mismatch_rejected(self) -> None:
        with pytest.raises(BaselineError, match="start/end"):
            self._make(start=_D3)

    def test_end_mismatch_rejected(self) -> None:
        with pytest.raises(BaselineError, match="start/end"):
            self._make(end=_D3)

    def test_non_positive_anchor_rejected(self) -> None:
        with pytest.raises(BaselineError, match="anchor_open"):
            self._make(anchor_open=Decimal("-1"))

    def test_non_finite_total_return_rejected(self) -> None:
        with pytest.raises(BaselineError, match="finite"):
            self._make(total_return_pct=float("nan"))

    def test_frozen(self) -> None:
        series = self._make()
        with pytest.raises(AttributeError):
            series.instrument = "X"  # type: ignore[misc]


class TestBaselineFilename:
    def test_pinned_shape(self) -> None:
        assert baseline_filename("NIFTY") == "baseline-NIFTY.json"

    def test_empty_instrument_rejected(self) -> None:
        with pytest.raises(BaselineError, match="non-empty"):
            baseline_filename("")


class TestWriteBaseline:
    def test_round_trip_equality(self, tmp_path: Path) -> None:
        path = tmp_path / baseline_filename("NIFTY")
        write_baseline(path, _series(), data_snapshot_hash=_SNAPSHOT)
        assert load_baseline(path) == _series()

    def test_bytes_deterministic_and_golden_locked(self, tmp_path: Path) -> None:
        first = tmp_path / "a.json"
        second = tmp_path / "b.json"
        write_baseline(first, _series(), data_snapshot_hash=_SNAPSHOT)
        write_baseline(second, _series(), data_snapshot_hash=_SNAPSHOT)
        assert first.read_bytes() == second.read_bytes()
        digest = hashlib.sha256(first.read_bytes()).hexdigest()
        assert digest == _GOLDEN_BASELINE_SHA256, (
            f"baseline artifact digest moved: {digest} — any B8 shape change "
            "must be a deliberate, ledgered re-lock"
        )

    def test_payload_shape_is_the_b8_contract(self, tmp_path: Path) -> None:
        path = tmp_path / "b8.json"
        write_baseline(path, _series(), data_snapshot_hash=_SNAPSHOT)
        raw = path.read_text(encoding="utf-8")
        assert raw.endswith("\n")
        payload = json.loads(raw)
        assert payload["schema_version"] == BASELINE_SCHEMA_VERSION
        assert payload["anchor_open"] == "100.00"
        assert payload["sessions"][0] == ["2024-01-02", "101.20"]
        assert payload["data_snapshot_hash"] == _SNAPSHOT
        assert payload["total_return_pct"] == 5.55

    def test_empty_snapshot_hash_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(BaselineError, match="data_snapshot_hash"):
            write_baseline(tmp_path / "x.json", _series(), data_snapshot_hash="")


def _payload() -> dict[str, Any]:
    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "instrument": "NIFTY",
        "start": "2024-01-02",
        "end": "2024-01-05",
        "anchor_open": "100.00",
        "sessions": [
            ["2024-01-02", "101.20"],
            ["2024-01-03", "99.95"],
            ["2024-01-05", "105.55"],
        ],
        "total_return_pct": 5.55,
        "data_snapshot_hash": _SNAPSHOT,
        "source_note": "test note",
    }


def _write_payload(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


class TestLoadBaselineDamageMatrix:
    def test_valid_payload_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "ok.json"
        _write_payload(path, _payload())
        series = load_baseline(path)
        assert series is not None
        assert series.sessions[2] == (date(2024, 1, 5), Decimal("105.55"))
        assert series.total_return_pct == 5.55

    def test_integer_total_return_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "int.json"
        _write_payload(path, {**_payload(), "total_return_pct": 5})
        series = load_baseline(path)
        assert series is not None
        assert series.total_return_pct == 5.0

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_baseline(tmp_path / "absent.json") is None

    def test_not_json_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "torn.json"
        path.write_text('{"schema_version": 1, "instr', encoding="utf-8")
        assert load_baseline(path) is None

    def test_not_object_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "list.json"
        _write_payload(path, [1, 2, 3])
        assert load_baseline(path) is None

    def test_missing_key_returns_none(self, tmp_path: Path) -> None:
        payload = _payload()
        del payload["source_note"]
        path = tmp_path / "missing.json"
        _write_payload(path, payload)
        assert load_baseline(path) is None

    def test_extra_key_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "extra.json"
        _write_payload(path, {**_payload(), "bonus": 1})
        assert load_baseline(path) is None

    def test_wrong_schema_version_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "v2.json"
        _write_payload(path, {**_payload(), "schema_version": 99})
        assert load_baseline(path) is None

    @pytest.mark.parametrize("bad_hash", ["", 7])
    def test_bad_snapshot_hash_returns_none(self, tmp_path: Path, bad_hash: object) -> None:
        path = tmp_path / "hash.json"
        _write_payload(path, {**_payload(), "data_snapshot_hash": bad_hash})
        assert load_baseline(path) is None

    @pytest.mark.parametrize(
        "sessions",
        [
            "not-a-list",
            [["2024-01-02"]],
            [["2024-01-02", "101.20", "extra"]],
            [[2, "101.20"]],
            [["2024-01-02", 101.2]],
            ["not-a-pair"],
        ],
    )
    def test_bad_sessions_shape_returns_none(self, tmp_path: Path, sessions: object) -> None:
        path = tmp_path / "sessions.json"
        _write_payload(path, {**_payload(), "sessions": sessions})
        assert load_baseline(path) is None

    @pytest.mark.parametrize("field", ["instrument", "start", "end", "anchor_open"])
    def test_non_string_scalar_returns_none(self, tmp_path: Path, field: str) -> None:
        path = tmp_path / "scalar.json"
        _write_payload(path, {**_payload(), field: 5})
        assert load_baseline(path) is None

    def test_non_string_source_note_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "note.json"
        _write_payload(path, {**_payload(), "source_note": 5})
        assert load_baseline(path) is None

    @pytest.mark.parametrize("value", ["5.5", True])
    def test_bad_total_return_type_returns_none(self, tmp_path: Path, value: object) -> None:
        path = tmp_path / "total.json"
        _write_payload(path, {**_payload(), "total_return_pct": value})
        assert load_baseline(path) is None

    def test_bad_iso_date_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "date.json"
        _write_payload(path, {**_payload(), "start": "02-01-2024"})
        assert load_baseline(path) is None

    def test_bad_decimal_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "decimal.json"
        _write_payload(path, {**_payload(), "anchor_open": "not-a-price"})
        assert load_baseline(path) is None

    def test_series_invariant_violation_returns_none(self, tmp_path: Path) -> None:
        payload = _payload()
        payload["sessions"] = list(reversed(payload["sessions"]))
        path = tmp_path / "unsorted.json"
        _write_payload(path, payload)
        assert load_baseline(path) is None

    def test_nan_total_return_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "nan.json"
        # json.loads accepts the non-standard NaN token — the BaselineSeries
        # finiteness invariant must still refuse it.
        path.write_text(json.dumps(_payload()).replace("5.55,", "NaN,", 1) + "\n", encoding="utf-8")
        assert load_baseline(path) is None


class TestPermutationProperty:
    def test_many_seeds_reproduce_identical_series(self) -> None:
        expected = _series()
        for seed in range(20):
            shuffled = list(_BARS)
            random.Random(seed).shuffle(shuffled)
            assert compute_baseline(shuffled, instrument="NIFTY") == expected
