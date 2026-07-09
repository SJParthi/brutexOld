"""Integration: the bounded bad-tick screen wired into ``arrow_table_to_bars``.

The engine read boundary (DEC-SWEEP-DATA-EDGE-GUARDS-001, D-MAJ-2 + the
head-seed fix DEC-BADTICK-HEAD-SEED-001): a self-consistent SCALED bar —
internally ordered + positive, so it passes the DEC-STORED-DATA-VALIDATION-001
positivity screen — is SKIPPED-and-COUNTED before it can reach Stage-1A / run
identity, INCLUDING a poison FIRST bar of a session (the head-seed regression:
pre-fix, a kept ×10 head became the baseline and cascade-dropped every healthy
successor, collapsing the session to the single poison bar), while every clean
regular-day table is returned byte-identically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest
from brutex.data.bad_tick import (
    BAD_TICK_HEAD_UNCONFIRMED_EVENT,
    BAD_TICK_SKIP_EVENT,
    NON_FINITE_SKIP_EVENT,
)
from brutex.feed.replay import arrow_table_to_bars
from structlog.testing import capture_logs

_BASE = datetime(2024, 1, 2, 3, 45, tzinfo=UTC)  # 09:15 IST


def _table(rows: list[tuple[datetime, float, float, float, float]]) -> pa.Table:
    """Build the canonical 7-column bar table from (ts, o, h, l, c) rows."""
    return pa.table(
        {
            "timestamp": pa.array([r[0] for r in rows], type=pa.timestamp("us", tz="UTC")),
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [10] * len(rows),
            "open_interest": [None] * len(rows),
        }
    )


def test_scaled_bar_skipped_with_one_warn() -> None:
    rows = [
        (_BASE, 27940.0, 27950.0, 27930.0, 27945.0),
        (_BASE + timedelta(minutes=1), 2794.5, 2795.0, 2793.0, 2794.5),  # ÷10 spike
        (_BASE + timedelta(minutes=2), 27946.0, 27960.0, 27940.0, 27955.0),  # good vs 27945
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows), source="NSE/INDEX/NSE-NIFTY/1minute")
    assert [float(b.close) for b in bars] == [27945.0, 27955.0]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 1
    assert warns[0]["source"] == "NSE/INDEX/NSE-NIFTY/1minute"


def test_poison_first_bar_dropped_session_survives() -> None:
    # THE DEC-BADTICK-HEAD-SEED-001 regression (fails with the fix reverted):
    # a ×10 poison FIRST bar must be dropped by the median-of-first-3 head
    # vote, and every healthy successor KEPT — never the pre-fix collapse
    # where the poison head became the baseline, the 5 healthy bars were
    # cascade-dropped, and Stage-1A absorbed the lone poison bar as the whole
    # day's OHLC (poisoning the NEXT day's pivot/PDH-PDL/Fib anchor).
    rows = [(_BASE, 279400.0, 279500.0, 279300.0, 279400.0)] + [
        (
            _BASE + timedelta(minutes=i),
            27940.0 + i,
            27945.0 + i,
            27935.0 + i,
            27940.0 + i,
        )
        for i in range(1, 6)
    ]
    # A healthy control session the next day.
    rows += [
        (
            _BASE + timedelta(days=1, minutes=i),
            27950.0 + i,
            27955.0 + i,
            27945.0 + i,
            27950.0 + i,
        )
        for i in range(3)
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows), source="NSE/INDEX/NSE-NIFTY/1minute")
    closes = [float(b.close) for b in bars]
    assert 279400.0 not in closes  # the poison head is GONE
    assert closes == [27941.0, 27942.0, 27943.0, 27944.0, 27945.0, 27950.0, 27951.0, 27952.0]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 1
    assert warns[0]["session"] == "2024-01-02"


def test_reopen_gap_first_bars_of_session_not_tripped() -> None:
    # A large overnight gap opening a new session is legitimate — the second
    # session's head bars agree with each other, so none is dropped (cross-
    # session comparison is forbidden).
    rows = [
        (_BASE, 27940.0, 27950.0, 27930.0, 27945.0),
        (_BASE + timedelta(days=1), 6000.0, 6010.0, 5990.0, 6005.0),  # new session, big gap
        (_BASE + timedelta(days=1, minutes=1), 6001.0, 6011.0, 5991.0, 6006.0),
        (_BASE + timedelta(days=1, minutes=2), 6002.0, 6012.0, 5992.0, 6007.0),
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows))
    assert [float(b.close) for b in bars] == [27945.0, 6005.0, 6006.0, 6007.0]
    assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]


def test_clean_table_byte_identical() -> None:
    rows = [(_BASE + timedelta(minutes=i), 100.0, 101.0, 99.0, 100.5) for i in range(5)]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows))
    assert len(bars) == 5
    assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]


def test_kill_switch_disables_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "off")
    rows = [
        (_BASE, 27940.0, 27950.0, 27930.0, 27945.0),
        (_BASE + timedelta(minutes=1), 2794.5, 2795.0, 2793.0, 2794.5),  # spike kept when off
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows))
    assert len(bars) == 2  # nothing skipped
    assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert not [e for e in logs if e.get("event") == BAD_TICK_HEAD_UNCONFIRMED_EVENT]


def test_source_none_still_screens() -> None:
    rows = [
        (_BASE, 100.0, 101.0, 99.0, 100.0),
        (_BASE + timedelta(minutes=1), 100.5, 101.5, 99.5, 100.5),
        (_BASE + timedelta(minutes=2), 101.0, 102.0, 100.0, 101.0),
        (_BASE + timedelta(minutes=3), 10.0, 10.1, 9.9, 10.0),  # ÷10 spike
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows))  # no source
    assert [float(b.close) for b in bars] == [100.0, 100.5, 101.0]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["source"] is None


def test_two_bar_session_inconsistent_kept_and_disclosed() -> None:
    # The disclosed unconfirmable-head limit end-to-end: a 2-bar session whose
    # bars are mutually ≥ 4x apart keeps BOTH bars (no offender identifiable)
    # and emits ONE head-unconfirmed WARN — never a silent poison keep, never a
    # 50/50 data destruction.
    rows = [
        (_BASE, 100.0, 101.0, 99.0, 100.0),
        (_BASE + timedelta(minutes=1), 1000.0, 1001.0, 999.0, 1000.0),
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows), source="src")
    assert [float(b.close) for b in bars] == [100.0, 1000.0]
    assert not [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    unconfirmed = [e for e in logs if e.get("event") == BAD_TICK_HEAD_UNCONFIRMED_EVENT]
    assert len(unconfirmed) == 1
    assert unconfirmed[0]["source"] == "src"


def test_non_positive_price_still_raises_before_the_screen_drop() -> None:
    # The positivity guard (DEC-STORED-DATA-VALIDATION-001) stays FIRST: a
    # non-positive stored bar raises loudly even when the bad-tick mask would
    # have dropped that row.
    rows = [
        (_BASE, 100.0, 101.0, 99.0, 100.0),
        (_BASE + timedelta(minutes=1), 100.5, 101.5, 99.5, 100.5),
        (_BASE + timedelta(minutes=2), 101.0, 102.0, 100.0, 101.0),
        (_BASE + timedelta(minutes=3), -1000.0, -999.0, -1001.0, -1000.0),
    ]
    with pytest.raises(ValueError, match="non-positive price"):
        arrow_table_to_bars(_table(rows))


def test_refused_table_emits_bad_tick_warns_before_the_positivity_raise() -> None:
    # SCOPE PIN for the "byte-identical to #1616's ordering" claim (round-1
    # review): the whole-table mask pre-pass runs BEFORE the row loop, so on a
    # table the positivity guard REFUSES the pre-pass has already emitted the
    # table's bad-tick WARNs — the pre-#1625 streaming screen emitted none past
    # the raising row. Strictly MORE disclosure on a refused table, never less;
    # the table still raises identically and no bars are served either way.
    rows = [
        (_BASE, 100.0, 101.0, 99.0, 100.0),
        (_BASE + timedelta(minutes=1), 100.5, 101.5, 99.5, 100.5),
        (_BASE + timedelta(minutes=2), 101.0, 102.0, 100.0, 101.0),
        (_BASE + timedelta(minutes=3), 10.0, 10.1, 9.9, 10.0),  # ÷10 spike (masked)
        (_BASE + timedelta(days=1), -5.0, -4.0, -6.0, -5.0),  # next session, refused
    ]
    with capture_logs() as logs, pytest.raises(ValueError, match="non-positive price"):
        arrow_table_to_bars(_table(rows), source="src")
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1  # the D1 spike's WARN landed before the raise
    assert warns[0]["skipped"] == 1
    assert warns[0]["session"] == "2024-01-02"


# ────────────────────────────────────────────────────────────────────────
# High/low arm end-to-end (DEC-BADTICK-HILO-SCREEN-001): replay.py passes the
# table's highs/lows into the screen, so the DATA-3b shape — a ×10 HIGH with a
# sane close, which passed is_bad_tick AND price_sanity and poisoned the next
# day's PDH anchor via TodayOHLC.absorb — is dropped at the read boundary.
# ────────────────────────────────────────────────────────────────────────


def test_high_only_scaled_bar_dropped_at_read_boundary() -> None:
    # DATA-3b: HIGH ×10 (open/low/close normal). Pre-arm this bar was SERVED
    # (its close is sane) and its captured high fed the next day's PDH.
    rows = [
        (_BASE + timedelta(minutes=i), 10050.0 + i, 10055.0 + i, 10045.0 + i, 10050.0 + i)
        for i in range(3)
    ] + [
        (_BASE + timedelta(minutes=3), 10053.0, 100500.0, 10048.0, 10053.0)  # high ×10
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows), source="src")
    assert [float(b.close) for b in bars] == [10050.0, 10051.0, 10052.0]
    assert all(float(b.high) < 100500.0 for b in bars)  # the poison high is GONE
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 1
    assert warns[0]["skip_reasons"] == {"high": 1}
    assert warns[0]["first_reason"] == "high"


def test_low_only_scaled_bar_dropped_at_read_boundary() -> None:
    rows = [
        (_BASE + timedelta(minutes=i), 21008.0 + i, 21013.0 + i, 21003.0 + i, 21008.0 + i)
        for i in range(3)
    ] + [
        (_BASE + timedelta(minutes=3), 21011.0, 21016.0, 2101.0, 21011.0)  # low ÷10
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows), source="src")
    assert [float(b.close) for b in bars] == [21008.0, 21009.0, 21010.0]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert warns[0]["skip_reasons"] == {"low": 1}


# ────────────────────────────────────────────────────────────────────────
# Non-finite screen end-to-end (DEC-READ-BOUNDARY-FINITE-001): inf/-inf/nan
# OHLC is skipped-and-counted BEFORE the positivity raise; -inf moves from
# raise-class to skip-class (the documented sanctioned change); the kill
# switch OFF restores the exact pre-#1616 raw path.
# ────────────────────────────────────────────────────────────────────────

_NAN = float("nan")
_INF = float("inf")


def test_nan_close_bar_skipped_not_crashed() -> None:
    # THE NaN-compares-false trap: `nan <= 0` is False, so the positivity
    # raise never fired, and Decimal("NaN") then blew up (or worse, survived)
    # downstream. The finite screen skips-and-counts it instead.
    rows = [
        (_BASE, 100.0, 101.0, 99.0, 100.0),
        (_BASE + timedelta(minutes=1), 100.5, 101.5, 99.5, _NAN),
        (_BASE + timedelta(minutes=2), 101.0, 102.0, 100.0, 101.0),
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows), source="src")
    assert [float(b.close) for b in bars] == [100.0, 101.0]
    warns = [e for e in logs if e.get("event") == NON_FINITE_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skipped"] == 1
    assert warns[0]["first_field"] == "close"
    assert warns[0]["first_value"] == "nan"
    assert warns[0]["source"] == "src"


def test_pos_inf_high_bar_skipped() -> None:
    rows = [
        (_BASE, 100.0, 101.0, 99.0, 100.0),
        (_BASE + timedelta(minutes=1), 100.5, _INF, 99.5, 100.5),
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows))
    assert [float(b.close) for b in bars] == [100.0]
    warns = [e for e in logs if e.get("event") == NON_FINITE_SKIP_EVENT]
    assert warns[0]["first_field"] == "high"


def test_neg_inf_moves_from_raise_class_to_skip_class() -> None:
    # SANCTIONED CHANGE (DEC-READ-BOUNDARY-FINITE-001, documented not silent):
    # a -inf low previously hit the positivity RAISE (`-inf <= 0` is True — a
    # loud whole-table crash); a non-finite bar is the same certain-corruption
    # class as NaN/+inf, so it now rides the skip class: the bar is dropped +
    # counted, the healthy rows are SERVED, and no ValueError fires.
    rows = [
        (_BASE, 100.0, 101.0, 99.0, 100.0),
        (_BASE + timedelta(minutes=1), 100.5, 101.5, -_INF, 100.5),
        (_BASE + timedelta(minutes=2), 101.0, 102.0, 100.0, 101.0),
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows), source="src")  # does NOT raise
    assert [float(b.close) for b in bars] == [100.0, 101.0]
    warns = [e for e in logs if e.get("event") == NON_FINITE_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["first_field"] == "low"
    assert warns[0]["first_value"] == "-inf"


def test_kill_switch_off_restores_neg_inf_positivity_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The ONE shared kill switch (documented decision): OFF = the exact
    # pre-#1616 raw read path for forensic replay — the finite screen stands
    # down WITH the ratio screen, so a -inf low hits the positivity raise
    # again (the pre-change envelope).
    monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "0")
    rows = [
        (_BASE, 100.0, 101.0, 99.0, 100.0),
        (_BASE + timedelta(minutes=1), 100.5, 101.5, -_INF, 100.5),
    ]
    with pytest.raises(ValueError, match="non-positive price"):
        arrow_table_to_bars(_table(rows))


def test_nan_row_does_not_poison_the_ratio_screen() -> None:
    # The filtration law: non-finite rows are EXCLUDED from the ratio screen's
    # input (a NaN reference compares false against everything — fed raw, a
    # NaN head bar would blind the median vote / baseline). With the NaN row
    # filtered, a later ×10 close spike in the SAME session is still caught.
    rows = [
        (_BASE, 100.0, 101.0, 99.0, _NAN),  # non-finite head row (filtered)
        (_BASE + timedelta(minutes=1), 100.0, 101.0, 99.0, 100.0),
        (_BASE + timedelta(minutes=2), 100.5, 101.5, 99.5, 100.5),
        (_BASE + timedelta(minutes=3), 101.0, 102.0, 100.0, 101.0),
        (_BASE + timedelta(minutes=4), 1010.0, 1011.0, 1009.0, 1010.0),  # ×10 spike
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows), source="src")
    assert [float(b.close) for b in bars] == [100.0, 100.5, 101.0]
    nf = [e for e in logs if e.get("event") == NON_FINITE_SKIP_EVENT]
    bt = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(nf) == 1 and nf[0]["skipped"] == 1
    assert len(bt) == 1 and bt[0]["skipped"] == 1
    assert bt[0]["skip_reasons"] == {"close": 1}


def test_screen_then_hash_kept_set_change_self_rekeys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SCREEN-THEN-HASH ordering pin (the verifier-B requirement): the
    # run-identity `data_snapshot_hash` (bxsnap2, stage3_wire) is computed
    # over the LOADED bar list — i.e. the `arrow_table_to_bars` OUTPUT, post
    # screen (`cmd_sweep_all` loads `slices[(symbol, interval)] = [event.bar
    # for event in feed]` via ReplayFeed._load_bars → Catalog.get_bars →
    # arrow_table_to_bars, then hashes those slices) — so a lake whose
    # kept-set changes under the screen SELF-RE-KEYS. Pinned at the seam: the
    # SAME stored table hashes differently with the screen ON (poison
    # dropped) vs OFF (raw bytes), proving the hash input is the screened
    # list, never the raw table (hash-before-screen would make these equal).
    from brutex.data.models import CandleBar
    from brutex.engine.stage3_wire import data_snapshot_hash

    rows = [
        (_BASE + timedelta(minutes=i), 100.0 + i, 105.0 + i, 95.0 + i, 100.0 + i) for i in range(3)
    ] + [
        (_BASE + timedelta(minutes=3), 1030.0, 1035.0, 1025.0, 1030.0)  # ×10 spike
    ]
    table = _table(rows)
    monkeypatch.delenv("BRUTEX_BAD_TICK_SCREEN", raising=False)
    bars_screened = arrow_table_to_bars(table)
    monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "0")
    bars_raw = arrow_table_to_bars(table)
    assert len(bars_screened) == 3 < len(bars_raw) == 4  # the kept-set changed

    def _hash(bars: list[CandleBar]) -> str:
        return data_snapshot_hash(
            exchange="NSE",
            segment="INDEX",
            start="2024-01-02",
            end="2024-01-03",
            slices={("NSE-NIFTY", "1minute"): bars},
        )

    assert _hash(bars_screened) != _hash(bars_raw)


# ────────────────────────────────────────────────────────────────────────
# Fixer round 1 — the mis-ordered raise→skip SANCTIONED-change pins (#1655):
# the hi/lo arm skips a mis-ordered bar whose corrupt field is ALSO
# ratio-out-of-band BEFORE the CandleBar ordering ValueError can fire — a
# disclosed loud→quiet narrowing, bounded (in-band mis-ordered bars still
# raise; screen OFF restores the raise for every mis-ordered bar).
# ────────────────────────────────────────────────────────────────────────


def test_misordered_out_of_band_low_skipped_not_raised() -> None:
    # A ×10-scaled LOW sitting above its own high — the typical hi/lo-corrupt
    # CAUSE of mis-ordering. Pre-additions this bar always reached the
    # constructor and raised the DEC-STORED-DATA-VALIDATION-001 whole-table
    # ordering ValueError; the hi/lo arm now SKIPS it (reason 'low') and the
    # table is served minus the bar with one skip WARN.
    rows = [
        (_BASE, 100.0, 105.0, 95.0, 100.0),
        (_BASE + timedelta(minutes=1), 100.5, 105.0, 96.0, 101.0),
        (_BASE + timedelta(minutes=2), 101.0, 106.0, 97.0, 102.0),
        (_BASE + timedelta(minutes=3), 100.0, 101.0, 990.0, 100.0),  # LOW ×10 > high
    ]
    with capture_logs() as logs:
        bars = arrow_table_to_bars(_table(rows), source="probe/misordered")
    assert [float(b.close) for b in bars] == [100.0, 101.0, 102.0]
    warns = [e for e in logs if e.get("event") == BAD_TICK_SKIP_EVENT]
    assert len(warns) == 1
    assert warns[0]["skip_reasons"] == {"low": 1}
    assert warns[0]["first_reason"] == "low"


def test_misordered_in_band_bar_still_raises_ordering_error() -> None:
    # The BOUNDED side of the narrowing: a mis-ordered bar whose fields all
    # stay IN-band is NOT the hi/lo arm's class — the loud whole-table
    # ordering refusal still fires.
    rows = [
        (_BASE, 100.0, 105.0, 95.0, 100.0),
        (_BASE + timedelta(minutes=1), 100.0, 99.0, 98.0, 100.0),  # high < open, in-band
    ]
    with pytest.raises(ValueError, match="corrupt stored bar"):
        arrow_table_to_bars(_table(rows), source="probe/misordered-inband")


def test_kill_switch_off_restores_misordered_ordering_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OFF = the exact pre-#1616 raw read path: EVERY mis-ordered bar raises
    # the ordering ValueError again, including the out-of-band class the arm
    # skips when ON.
    monkeypatch.setenv("BRUTEX_BAD_TICK_SCREEN", "0")
    rows = [
        (_BASE, 100.0, 105.0, 95.0, 100.0),
        (_BASE + timedelta(minutes=1), 100.0, 101.0, 990.0, 100.0),  # LOW ×10, mis-ordered
    ]
    with pytest.raises(ValueError, match="corrupt stored bar"):
        arrow_table_to_bars(_table(rows), source="probe/misordered-off")
