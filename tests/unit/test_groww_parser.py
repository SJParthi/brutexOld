"""Unit tests for ``brutex.adapters.groww.parser`` (DEC-GROWW-DEPS-REINSTATE-001)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from brutex.adapters.groww import parser
from brutex.data.candle_fields import CandleParseError
from brutex.observability.correlation import pull_id_scope
from structlog.testing import capture_logs


# ── parse_timestamp_ist ─────────────────────────────────────────────────────
def test_iso_t_separator_localizes_ist_to_utc() -> None:
    # 10:30 IST == 05:00 UTC (fixed +05:30).
    assert parser.parse_timestamp_ist("2025-09-24T10:30:00") == datetime(
        2025, 9, 24, 5, 0, tzinfo=UTC
    )


def test_iso_space_separator() -> None:
    assert parser.parse_timestamp_ist("2025-09-24 10:30:00") == datetime(
        2025, 9, 24, 5, 0, tzinfo=UTC
    )


def test_epoch_int_is_utc() -> None:
    assert parser.parse_timestamp_ist(0) == datetime(1970, 1, 1, 0, 0, tzinfo=UTC)


def test_epoch_float_is_utc() -> None:
    ts = parser.parse_timestamp_ist(1_700_000_000.0)
    assert ts.tzinfo == UTC


def test_bool_timestamp_rejected() -> None:
    with pytest.raises(CandleParseError, match="must not be bool"):
        parser.parse_timestamp_ist(True)


def test_unparseable_string_rejected() -> None:
    with pytest.raises(CandleParseError, match="unparseable Groww timestamp"):
        parser.parse_timestamp_ist("not-a-date")


def test_non_finite_epoch_rejected() -> None:
    with pytest.raises(CandleParseError, match="non-finite"):
        parser.parse_timestamp_ist(float("inf"))


def test_out_of_range_epoch_rejected() -> None:
    with pytest.raises(CandleParseError, match="out-of-range"):
        parser.parse_timestamp_ist(1e30)


def test_out_of_range_string_year_rejected() -> None:
    # DEC-PULL-QUARANTINE-ROBUST-001 FIX 1: a parseable-but-pathological year-0001 string
    # raises OverflowError("date value out of range") on the IST→UTC shift (it pushes the
    # datetime below datetime.min). The string branch must map it to a quarantinable
    # CandleParseError (mirroring the epoch branch) — NOT let a raw OverflowError escape the
    # per-symbol quarantine and crash the whole multi-symbol pull.
    with pytest.raises(CandleParseError, match="out-of-range candle timestamp"):
        parser.parse_timestamp_ist("0001-01-01T00:00:00")


def test_wrong_type_timestamp_rejected() -> None:
    with pytest.raises(CandleParseError, match="must be a string or epoch"):
        parser.parse_timestamp_ist(["2025-09-24"])


# ── parse_candle_rows ───────────────────────────────────────────────────────
def _row(ts: str = "2025-09-24T10:30:00") -> list[object]:
    return [ts, 100.0, 110.0, 90.0, 105.0, 1000, None]


def test_parses_normal_rows() -> None:
    bars = parser.parse_candle_rows({"candles": [_row(), _row("2025-09-24T10:31:00")]})
    assert len(bars) == 2
    assert bars[0].open == Decimal("100.0")
    assert bars[0].high == Decimal("110.0")
    assert bars[0].volume == 1000


def test_no_trade_row_is_skipped() -> None:
    bars = parser.parse_candle_rows(
        {"candles": [_row(), ["2025-09-24T10:31:00", 0, 0, 0, 0, 0, None]]}
    )
    assert len(bars) == 1  # the empty bar is gracefully dropped


def test_drop_volume_zeros_volume() -> None:
    bars = parser.parse_candle_rows({"candles": [_row()]}, drop_volume=True)
    assert bars[0].volume == 0


def test_partial_negative_price_row_is_skipped() -> None:
    # DEC-ADVERSARIAL-HARDENING-001 FIX 3 via the PRODUCTION parse path: a row with a
    # NEGATIVE open among real prices is un-storable junk and must be gracefully dropped
    # (the parser's `except EmptyCandleRow: continue`), never silently stored with the
    # high=max/low=min clamp hiding the negative. The good row alongside it still parses.
    bars = parser.parse_candle_rows(
        {
            "candles": [
                _row(),
                ["2025-09-24T10:31:00", -5.0, 110.0, 90.0, 105.0, 1000, None],
            ]
        }
    )
    assert len(bars) == 1  # the negative-price bar is gracefully dropped
    assert bars[0].open == Decimal("100.0")


def test_fno_open_interest_preserved() -> None:
    bars = parser.parse_candle_rows({"candles": [["2025-09-24T10:30:00", 1, 2, 1, 2, 5, 42]]})
    assert bars[0].open_interest == 42


def test_current_session_six_element_row_parsed_not_quarantined() -> None:
    """DEC-GROWW-FORMING-BAR-PARSE-001: the current not-yet-finalised day's intraday
    bars arrive as a 6-element row (no trailing open_interest slot) — parse them with
    ``open_interest=None`` instead of quarantining the whole series."""
    # The EXACT row from the operator's brutex.log that quarantined all 4 indices'
    # 1-minute series — 6 elements, trailing volume is literally ``None`` (no OI slot).
    bars = parser.parse_candle_rows(
        {"candles": [["2026-06-19T09:15:00", 26466.25, 26480.9, 26422.2, 26457.85, None]]}
    )
    assert len(bars) == 1
    assert bars[0].open == Decimal("26466.25")
    assert bars[0].close == Decimal("26457.85")
    assert bars[0].open_interest is None  # padded — OI slot absent on the live edge


def test_mixed_seven_then_six_element_rows() -> None:
    """A settled 7-element bar followed by the live-edge 6-element bar both parse."""
    bars = parser.parse_candle_rows(
        {
            "candles": [
                ["2026-06-18T15:29:00", 100.0, 110.0, 90.0, 105.0, 1000, None],  # settled
                ["2026-06-19T09:15:00", 105.0, 106.0, 104.0, 105.5, 0],  # current session (6)
            ]
        }
    )
    assert len(bars) == 2
    assert bars[1].open_interest is None


def test_five_element_row_still_raises() -> None:
    """A row shorter than 6 (a genuinely malformed row) is STILL rejected — the
    tolerance is exactly the 6-element current-session shape, nothing shorter.

    The single error message names BOTH accepted shapes, so we anchor on the
    rejected ROW repr (length-discriminating) rather than the shape names — the
    test would otherwise pass even if the 5/8 cases were swapped (hostile-review NIT).
    """
    with pytest.raises(CandleParseError) as exc:
        parser.parse_candle_rows({"candles": [["2026-06-19T09:15:00", 1, 2, 3, 4]]})
    assert "1, 2, 3, 4]" in str(exc.value)  # the rejected 5-element row, discriminating
    assert "1, 2, 3, 4, 5" not in str(exc.value)


def test_eight_element_row_still_raises() -> None:
    """A row longer than 7 is STILL rejected (an unexpected vendor shape, fail-loud).

    Anchored on the rejected ROW repr (length-discriminating), not the shape names.
    """
    with pytest.raises(CandleParseError) as exc:
        parser.parse_candle_rows({"candles": [["2026-06-19T09:15:00", 1, 2, 3, 4, 5, 6, 7]]})
    assert "1, 2, 3, 4, 5, 6, 7]" in str(exc.value)  # the rejected 8-element row, discriminating


def test_missing_candles_key_raises() -> None:
    with pytest.raises(CandleParseError, match="missing required 'candles'"):
        parser.parse_candle_rows({})


def test_non_array_candles_raises() -> None:
    with pytest.raises(CandleParseError, match="must be an array"):
        parser.parse_candle_rows({"candles": "nope"})


def test_wrong_arity_row_raises() -> None:
    with pytest.raises(CandleParseError, match="7-element array"):
        parser.parse_candle_rows({"candles": [[1, 2, 3]]})


def test_non_sequence_row_raises() -> None:
    with pytest.raises(CandleParseError, match="7-element array"):
        parser.parse_candle_rows({"candles": [42]})


def test_malformed_nonempty_row_raises() -> None:
    # Non-empty (has prices) but an unparseable price → CandleParseError (not skipped).
    with pytest.raises(CandleParseError):
        parser.parse_candle_rows({"candles": [["2025-09-24T10:30:00", "x", 2, 1, 2, 5, None]]})


def test_partial_absent_open_row_is_skipped_not_fatal() -> None:
    # DEC-CANDLE-PARTIAL-ABSENT-SKIP-001: Groww's observed `open: None` with a
    # real high/low/close is an un-storable PARTIAL bar. It must be GRACEFULLY skipped (the
    # crash `unparseable open price: None` previously aborted the whole 779-symbol pull) —
    # the surrounding good rows survive, never lost to one bad neighbour.
    bars = parser.parse_candle_rows(
        {
            "candles": [
                _row(),
                ["2025-09-24T10:31:00", None, 110.0, 90.0, 105.0, 1000, None],
                _row("2025-09-24T10:32:00"),
            ]
        }
    )
    assert len(bars) == 2  # the partial bar is dropped; both good rows are kept


# ── once-per-pull epoch-timestamp WARNING (DEC-INGEST-BOUNDARY-INTEGRITY-001) ─


def _epoch_warns(logs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """The captured ``groww.parse.epoch_timestamp`` events only."""
    return [e for e in logs if e["event"] == "groww.parse.epoch_timestamp"]


class TestEpochTimestampWarnOncePerPull:
    """The DEFENSIVE epoch branch is LOUD exactly once per pull — never silent, never spam."""

    @pytest.fixture(autouse=True)
    def _reset_guard(self) -> Iterator[None]:
        # The module-level dedup persists per process — reset around each test so
        # "exactly one WARNING" assertions are isolated (the explicit reset seam).
        parser.reset_epoch_timestamp_warnings()
        yield
        parser.reset_epoch_timestamp_warnings()

    def test_many_epoch_rows_in_one_pull_warn_exactly_once(self) -> None:
        with pull_id_scope("pullaaaa0001"), capture_logs() as logs:
            for epoch in (1_700_000_000, 1_700_000_060, 1_700_000_120.0):
                parser.parse_timestamp_ist(epoch)
        warns = _epoch_warns(logs)
        assert len(warns) == 1
        assert warns[0]["pull_id"] == "pullaaaa0001"  # explicit field, not a contextvar
        assert warns[0]["assumed"] == "unix-utc-seconds"

    def test_two_distinct_pulls_warn_once_each(self) -> None:
        with capture_logs() as logs:
            with pull_id_scope("pullaaaa0001"):
                parser.parse_timestamp_ist(1_700_000_000)
                parser.parse_timestamp_ist(1_700_000_060)
            with pull_id_scope("pullbbbb0002"):
                parser.parse_timestamp_ist(1_700_000_000)
        warns = _epoch_warns(logs)
        assert [w["pull_id"] for w in warns] == ["pullaaaa0001", "pullbbbb0002"]

    def test_no_pull_id_bound_still_warns_once_under_empty_key(self) -> None:
        # DECIDED behavior: outside any pull scope the guard dedups under the ""
        # key — the anomaly is still LOUD (once per process, until reset).
        with capture_logs() as logs:
            parser.parse_timestamp_ist(1_700_000_000)
            parser.parse_timestamp_ist(1_700_000_060)  # deduped
        warns = _epoch_warns(logs)
        assert len(warns) == 1
        assert warns[0]["pull_id"] == ""

    def test_string_path_never_warns(self) -> None:
        with capture_logs() as logs:
            parser.parse_timestamp_ist("2025-09-24T10:30:00")
            parser.parse_timestamp_ist("2025-09-24 10:31:00")
        assert _epoch_warns(logs) == []

    def test_reset_seam_re_arms_the_warning(self) -> None:
        with capture_logs() as logs:
            parser.parse_timestamp_ist(1_700_000_000)
            parser.reset_epoch_timestamp_warnings()
            parser.parse_timestamp_ist(1_700_000_060)
        assert len(_epoch_warns(logs)) == 2

    def test_non_finite_epoch_still_warns_before_rejecting(self) -> None:
        # The branch FIRING is the anomaly — even a rejected epoch is flagged.
        with capture_logs() as logs, pytest.raises(CandleParseError, match="non-finite"):
            parser.parse_timestamp_ist(float("nan"))
        assert len(_epoch_warns(logs)) == 1

    def test_parallel_epoch_parses_warn_exactly_once(self) -> None:
        # The parser runs on PARALLEL window workers — the check-then-add is atomic.
        import threading

        barrier = threading.Barrier(8)

        def _parse() -> None:
            barrier.wait()
            parser.parse_timestamp_ist(1_700_000_000)

        with pull_id_scope("pullcccc0003"), capture_logs() as logs:
            threads = [threading.Thread(target=_parse) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        assert len(_epoch_warns(logs)) == 1

    def test_epoch_payload_through_parse_candle_rows_warns_once(self) -> None:
        # End-to-end: a whole epoch-timestamped payload (the real spam risk) emits
        # ONE warning for the pull, and the bars still parse (behavior unchanged).
        payload = {
            "candles": [
                [1_700_000_000, 100.0, 110.0, 90.0, 105.0, 10, 1],
                [1_700_000_060, 101.0, 111.0, 91.0, 106.0, 11, 2],
                [1_700_000_120, 102.0, 112.0, 92.0, 107.0, 12, 3],
            ]
        }
        with pull_id_scope("pulldddd0004"), capture_logs() as logs:
            bars = parser.parse_candle_rows(payload)
        assert len(bars) == 3
        assert len(_epoch_warns(logs)) == 1


class TestOhlcClampWarnPerWindow:
    """The vendor-OHLC clamp is COUNTED + WARNed once per parsed window payload.

    DEC-STORED-DATA-VALIDATION-001 (L1-gap-2). Fails-before: the
    DEC-OHLC-VENDOR-NORMALIZE-001 high=max/low=min clamp fired with NO counter
    and NO log, so a vendor systematically under-reporting highs never
    surfaced. Parse output stays byte-identical (observe-only).
    """

    @staticmethod
    def _clamp_warns(logs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
        return [e for e in logs if e["event"] == "groww.parse.ohlc_clamped"]

    def test_one_warn_per_window_with_count(self) -> None:
        payload = {
            "candles": [
                # close 105 > vendor high 101 → clamped.
                ["2025-09-24T10:30:00", 100.0, 101.0, 90.0, 105.0, 10, None],
                # consistent — not clamped.
                ["2025-09-24T10:31:00", 100.0, 110.0, 90.0, 105.0, 10, None],
                # open 100 < vendor low 102 → clamped.
                ["2025-09-24T10:32:00", 100.0, 110.0, 102.0, 105.0, 10, None],
            ]
        }
        with capture_logs() as logs:
            bars = parser.parse_candle_rows(payload)
        warns = self._clamp_warns(logs)
        assert len(warns) == 1
        assert warns[0]["clamped_bars"] == 2
        assert warns[0]["total_rows"] == 3
        # Parse behavior unchanged: the bars are the clamped bars of before.
        assert bars[0].high == Decimal("105.0")
        assert bars[2].low == Decimal("100.0")

    def test_clean_window_emits_no_warn(self) -> None:
        payload = {"candles": [["2025-09-24T10:30:00", 100.0, 110.0, 90.0, 105.0, 10, None]]}
        with capture_logs() as logs:
            parser.parse_candle_rows(payload)
        assert self._clamp_warns(logs) == []

    def test_two_windows_warn_independently(self) -> None:
        payload = {"candles": [["2025-09-24T10:30:00", 100.0, 101.0, 90.0, 105.0, 10, None]]}
        with capture_logs() as logs:
            parser.parse_candle_rows(payload)
            parser.parse_candle_rows(payload)
        assert len(self._clamp_warns(logs)) == 2  # one per window parse, never deduped away

    def test_warn_carries_pull_id_when_bound(self) -> None:
        payload = {"candles": [["2025-09-24T10:30:00", 100.0, 101.0, 90.0, 105.0, 10, None]]}
        with pull_id_scope("pullcccc0003"), capture_logs() as logs:
            parser.parse_candle_rows(payload)
        assert self._clamp_warns(logs)[0]["pull_id"] == "pullcccc0003"


class TestNegativeCountClampWarnPerWindow:
    """A vendor NEGATIVE volume / open_interest is COUNTED + WARNed once per window.

    DEC-AUDIT-ALERT-CLAMP-OBS-001 (DATA-1), the sibling of the OHLC-clamp
    counter above. Fails-before: the ``max(0, ...)`` count-field clamp
    (DEC-VENDOR-VOLUME-NORMALIZE-001) fired with NO counter and NO log — unlike
    the OHLC clamp — so a vendor systematically emitting negative counts never
    surfaced. Parse output stays byte-identical: the clamped value is ``0``
    either way; the WARNING only ADDS observability.
    """

    @staticmethod
    def _clamp_warns(logs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
        return [e for e in logs if e["event"] == "groww.parse.negative_volume_clamped"]

    def test_one_warn_per_window_with_count(self) -> None:
        payload = {
            "candles": [
                # negative volume → clamped to 0.
                ["2025-09-24T10:30:00", 100.0, 110.0, 90.0, 105.0, -5, None],
                # consistent — not clamped.
                ["2025-09-24T10:31:00", 100.0, 110.0, 90.0, 105.0, 10, 7],
                # negative open_interest → clamped to 0.
                ["2025-09-24T10:32:00", 100.0, 110.0, 90.0, 105.0, 10, -3],
            ]
        }
        with capture_logs() as logs:
            bars = parser.parse_candle_rows(payload)
        warns = self._clamp_warns(logs)
        assert len(warns) == 1
        assert warns[0]["negative_count_clamped_bars"] == 2
        assert warns[0]["total_rows"] == 3
        # Clamped VALUE is byte-identical (0) — only observability was added.
        assert bars[0].volume == 0
        assert bars[2].open_interest == 0
        # The untouched bar is unchanged.
        assert bars[1].volume == 10
        assert bars[1].open_interest == 7

    def test_both_negative_counts_on_one_bar_count_once(self) -> None:
        # A single bar with BOTH volume AND OI negative fires the per-bar hook
        # exactly once (mirrors on_clamp's per-bar semantics).
        payload = {"candles": [["2025-09-24T10:30:00", 100.0, 110.0, 90.0, 105.0, -5, -3]]}
        with capture_logs() as logs:
            bars = parser.parse_candle_rows(payload)
        warns = self._clamp_warns(logs)
        assert len(warns) == 1
        assert warns[0]["negative_count_clamped_bars"] == 1
        assert bars[0].volume == 0
        assert bars[0].open_interest == 0

    def test_positive_counts_emit_no_warn(self) -> None:
        payload = {"candles": [["2025-09-24T10:30:00", 100.0, 110.0, 90.0, 105.0, 10, 7]]}
        with capture_logs() as logs:
            bars = parser.parse_candle_rows(payload)
        assert self._clamp_warns(logs) == []
        assert bars[0].volume == 10
        assert bars[0].open_interest == 7

    def test_two_windows_warn_independently(self) -> None:
        payload = {"candles": [["2025-09-24T10:30:00", 100.0, 110.0, 90.0, 105.0, -5, None]]}
        with capture_logs() as logs:
            parser.parse_candle_rows(payload)
            parser.parse_candle_rows(payload)
        assert len(self._clamp_warns(logs)) == 2  # one per window parse, never deduped away

    def test_warn_carries_pull_id_when_bound(self) -> None:
        payload = {"candles": [["2025-09-24T10:30:00", 100.0, 110.0, 90.0, 105.0, 10, -3]]}
        with pull_id_scope("pullffff0006"), capture_logs() as logs:
            parser.parse_candle_rows(payload)
        assert self._clamp_warns(logs)[0]["pull_id"] == "pullffff0006"

    def test_drop_volume_index_negative_oi_still_counted(self) -> None:
        # An index pull passes drop_volume=True (volume forced to 0), but a
        # negative open_interest must still be counted.
        payload = {"candles": [["2025-09-24T10:30:00", 100.0, 110.0, 90.0, 105.0, 999, -3]]}
        with capture_logs() as logs:
            bars = parser.parse_candle_rows(payload, drop_volume=True)
        warns = self._clamp_warns(logs)
        assert len(warns) == 1
        assert warns[0]["negative_count_clamped_bars"] == 1
        assert bars[0].volume == 0  # dropped
        assert bars[0].open_interest == 0  # clamped from -3


class TestEpochTimestampVerdictPerWindow:
    """The deterministic epoch-interpretation VERDICT rides every epoch payload.

    DEC-GROWW-EPOCH-VERDICT-001 (the epoch-timestamp-verdict follow-up of
    DEC-INGEST-BOUNDARY-INTEGRITY-001 (b)): a payload carrying NUMERIC
    timestamps emits ONE ``groww.parse.epoch_timestamp_verdict`` WARNING per
    parsed window (the ohlc_clamped per-payload precedent) with the
    session-plausibility cross-check of the UNVERIFIED Unix-UTC assumption.
    OBSERVE-ONLY — parsed bars byte-identical; string payloads emit nothing.

    Sample epochs are computed against the REAL 2024-06-19 regular session
    (see ``tests/unit/test_groww_epoch_verdict.py`` for the derivations).
    """

    #: Unix-UTC reading = 10:30 IST in-session; IST-naive reading = 05:00 out.
    EPOCH_UNIX_UTC_1030 = datetime(2024, 6, 19, 5, 0, tzinfo=UTC).timestamp()
    #: Unix-UTC reading = 10:31 IST in-session (one bar later).
    EPOCH_UNIX_UTC_1031 = datetime(2024, 6, 19, 5, 1, tzinfo=UTC).timestamp()
    #: IST-naive reading = 10:30 IST in-session; Unix-UTC reading = 16:00 out.
    EPOCH_IST_NAIVE_1030 = datetime(2024, 6, 19, 10, 30, tzinfo=UTC).timestamp()

    @pytest.fixture(autouse=True)
    def _reset_guard(self) -> Iterator[None]:
        parser.reset_epoch_timestamp_warnings()
        yield
        parser.reset_epoch_timestamp_warnings()

    @staticmethod
    def _verdicts(logs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
        return [e for e in logs if e["event"] == "groww.parse.epoch_timestamp_verdict"]

    def test_epoch_payload_emits_one_verdict_with_counts(self) -> None:
        payload = {
            "candles": [
                [self.EPOCH_UNIX_UTC_1030, 100.0, 110.0, 90.0, 105.0, 10, 1],
                [self.EPOCH_UNIX_UTC_1031, 101.0, 111.0, 91.0, 106.0, 11, 2],
            ]
        }
        with pull_id_scope("pulleeee0005"), capture_logs() as logs:
            bars = parser.parse_candle_rows(payload)
        assert len(bars) == 2
        verdicts = self._verdicts(logs)
        assert len(verdicts) == 1
        v = verdicts[0]
        assert v["pull_id"] == "pulleeee0005"
        assert v["verdict"] == "unix_utc_consistent"
        assert v["supports_unix_utc"] == 2
        assert v["supports_ist_naive"] == 0
        assert v["epoch_rows"] == 2
        assert v["sampled"] == 2
        assert v["assumed"] == "unix-utc-seconds"
        assert v["runbook"] == "docs/runbooks/groww-epoch-timestamp-probe.md"

    def test_ist_naive_payload_reads_against_the_assumption(self) -> None:
        # The failure mode the verdict exists for: an IST-naive mis-send is a
        # DIRECTIONAL loud verdict, never a silent pass.
        payload = {"candles": [[self.EPOCH_IST_NAIVE_1030, 100.0, 110.0, 90.0, 105.0, 10, 1]]}
        with capture_logs() as logs:
            parser.parse_candle_rows(payload)
        verdicts = self._verdicts(logs)
        assert len(verdicts) == 1
        assert verdicts[0]["verdict"] == "ist_naive_consistent"
        assert verdicts[0]["supports_ist_naive"] == 1

    def test_string_payload_emits_no_verdict(self) -> None:
        # Byte-identity pin for the production path: the documented
        # string-timestamp payload adds ZERO new log events.
        with capture_logs() as logs:
            bars = parser.parse_candle_rows({"candles": [_row(), _row("2025-09-24T10:31:00")]})
        assert len(bars) == 2
        assert self._verdicts(logs) == []

    def test_epoch_bars_byte_identical_to_unwired_parse(self) -> None:
        # OBSERVE-ONLY pin: the verdict wiring never touches the parsed bar —
        # ts/OHLC are exactly what parse_timestamp_ist + assemble_candle
        # produced before this change.
        payload = {"candles": [[self.EPOCH_UNIX_UTC_1030, 100.0, 110.0, 90.0, 105.0, 10, 7]]}
        with capture_logs():
            bars = parser.parse_candle_rows(payload)
        assert bars[0].timestamp == datetime.fromtimestamp(self.EPOCH_UNIX_UTC_1030, tz=UTC)
        assert bars[0].open == Decimal("100.0")
        assert bars[0].high == Decimal("110.0")
        assert bars[0].low == Decimal("90.0")
        assert bars[0].close == Decimal("105.0")
        assert bars[0].volume == 10
        assert bars[0].open_interest == 7

    def test_all_no_trade_epoch_payload_still_emits_verdict(self) -> None:
        # Row-level collection: the timestamps are evidence even when every
        # bar is a no-trade skip (assemble_candle never parses their ts, so
        # the once-per-pull branch WARN cannot fire — the verdict still does).
        payload = {"candles": [[self.EPOCH_UNIX_UTC_1030, 0, 0, 0, 0, 0, None]]}
        with capture_logs() as logs:
            bars = parser.parse_candle_rows(payload)
        assert bars == []
        verdicts = self._verdicts(logs)
        assert len(verdicts) == 1
        assert verdicts[0]["verdict"] == "unix_utc_consistent"

    def test_six_element_epoch_rows_feed_the_verdict(self) -> None:
        # The current-session no-OI row shape (DEC-GROWW-FORMING-BAR-PARSE-001)
        # is collected too.
        payload = {"candles": [[self.EPOCH_UNIX_UTC_1030, 100.0, 110.0, 90.0, 105.0, 10]]}
        with capture_logs() as logs:
            bars = parser.parse_candle_rows(payload)
        assert len(bars) == 1
        assert len(self._verdicts(logs)) == 1

    def test_bool_timestamp_is_not_collected_and_still_rejects(self) -> None:
        # bool is an int subclass — excluded from collection; the row then
        # fails loudly in parse_timestamp_ist as before (behavior unchanged),
        # and the propagating error means no verdict is emitted.
        payload = {"candles": [[True, 100.0, 110.0, 90.0, 105.0, 10, 1]]}
        with capture_logs() as logs, pytest.raises(CandleParseError, match="must not be bool"):
            parser.parse_candle_rows(payload)
        assert self._verdicts(logs) == []

    def test_two_windows_emit_independent_verdicts(self) -> None:
        # Per-payload, never deduped away (mirrors ohlc_clamped) — each
        # fetched window carries its own evidence.
        payload = {"candles": [[self.EPOCH_UNIX_UTC_1030, 100.0, 110.0, 90.0, 105.0, 10, 1]]}
        with capture_logs() as logs:
            parser.parse_candle_rows(payload)
            parser.parse_candle_rows(payload)
        assert len(self._verdicts(logs)) == 2

    def test_mixed_string_and_epoch_rows_count_only_epochs(self) -> None:
        payload = {
            "candles": [
                _row(),
                [self.EPOCH_UNIX_UTC_1030, 100.0, 110.0, 90.0, 105.0, 10, 1],
            ]
        }
        with capture_logs() as logs:
            bars = parser.parse_candle_rows(payload)
        assert len(bars) == 2
        verdicts = self._verdicts(logs)
        assert len(verdicts) == 1
        assert verdicts[0]["epoch_rows"] == 1
